"""HTTP endpoints over the completion engine.

The engine is the whole of the search; this layer only carries queries to it and
results back. Nothing here normalizes, rescores, reorders or deduplicates,
because doing any of that would mean the browser and the command line could
disagree about the same corpus.

**Index lifecycle.** Preparing the index reads the corpus or a cached build of
it, which takes seconds the first time. That happens once per process, in a
background thread started when the server starts, so the server can answer that
it is not ready yet rather than appearing hung. Until it finishes, completion
requests are refused with a state a caller can act on. Afterwards the prepared
index is read-only and shared, so concurrent requests need no locking beyond the
handover of the finished object.

**Watching it happen.** That background preparation reports itself through a
:class:`~autocomplete.progress.ProgressTracker`, which
``autocomplete/web/build_api.py`` serves as a snapshot and as a stream. The
index is still published by one assignment of a finished object, so a request
sees either no index or a complete one; progress is a separate, read-only view
of work in flight and can never expose a half-built index.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..config import Config, load_default_config
from ..data import AutoCompleteData
from ..engine import find_completions
from ..preparation import PreparationFailure, prepare
from ..progress import BuildState, ProgressTracker
from .build_api import create_build_router

__all__ = ["EngineState", "create_app"]

logger = logging.getLogger("autocomplete.web")

#: Where the development frontend runs. Kept to these, rather than opened up,
#: because the API is meant to be reached from a local browser only.
DEVELOPMENT_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
)

#: Long enough for any realistic sentence and short enough that a pasted file
#: cannot turn into work. The engine rejects anything longer than the corpus's
#: longest sentence anyway; this stops it before it is normalized.
MAX_QUERY_LENGTH = 2_000


class Completion(BaseModel):
    """One suggestion, carrying exactly the fields the engine produces."""

    completed_sentence: str
    source_text: str
    offset: int
    score: int

    @classmethod
    def of(cls, result: AutoCompleteData) -> "Completion":
        return cls(
            completed_sentence=result.completed_sentence,
            source_text=result.source_text,
            offset=result.offset,
            score=result.score,
        )


class CompletionsResponse(BaseModel):
    query: str = Field(description="The text as it was received, unmodified.")
    count: int
    results: list[Completion] = Field(
        description="Best first, in the order the engine returned them."
    )


class HealthResponse(BaseModel):
    """Whether the server can answer searches.

    The first five fields are the contract this endpoint has always had and are
    unchanged. The rest were added for the preparation screen and are optional,
    so a client written against the older shape is unaffected.
    """

    status: str = Field(description="One of: preparing, ready, failed.")
    ready: bool
    detail: str
    sentences: int | None = None
    sources: int | None = None
    phase: str | None = Field(default=None, description="The preparation phase.")
    phase_label: str | None = None
    cache_mode: str | None = Field(
        default=None,
        description="cold_build, warm_validation, warm_load, forced_rebuild or recovery.",
    )
    elapsed_seconds: float | None = Field(
        default=None, description="How long preparation took, or has taken so far."
    )
    searchable_bytes: int | None = None


@dataclass
class EngineState:
    """Holds the one prepared index, and what to say before it exists.

    The index is published by assigning it here once it is complete, so a
    request either sees nothing and is told the server is preparing, or sees a
    finished index. There is no state in between for a request to observe.

    It also owns the tracker that preparation reports into. The tracker is a
    view of the work, never a route to its result: no partially built index is
    reachable through it.
    """

    index: object | None = None
    error: str | None = None
    config: Config | None = None
    tracker: ProgressTracker = None  # type: ignore[assignment]
    _started: bool = False
    _lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        if self.tracker is None:
            self.tracker = ProgressTracker()
        # An index or an error handed in directly, as tests do, still leaves a
        # coherent story to report: the tracker would otherwise say "idle"
        # while the server was answering searches.
        if self.index is not None:
            self.tracker.finish(getattr(self.index, "stats", lambda: None)())
        elif self.error is not None:
            self.tracker.fail(
                "internal_error",
                "The search index could not be prepared.",
                hint="Check that corpus_root in config.yaml points at the text files.",
            )

    @property
    def status(self) -> str:
        if self.error is not None:
            return "failed"
        return "ready" if self.index is not None else "preparing"

    @property
    def ready(self) -> bool:
        return self.index is not None

    @property
    def can_retry(self) -> bool:
        """Whether another attempt would be accepted.

        Only after a failure: retrying while a build runs would start a second
        one, and retrying after success would discard a working index for no
        reason.
        """
        with self._lock:
            return (
                self.config is not None
                and self.index is None
                and self.tracker.snapshot().state is BuildState.FAILED
            )

    def prepare(self, config: Config) -> None:
        """Build or load the index, once, whoever asks first."""
        with self._lock:
            if self._started:
                return
            self._started = True
            self.config = config
        self._start(config)

    def retry(self) -> None:
        """Prepare again after a failure. Never starts a second concurrent build."""
        with self._lock:
            config = self.config
            if config is None or self.index is not None:
                return
            if self.tracker.snapshot().state is not BuildState.FAILED:
                return
            self.error = None
        self._start(config)

    def _start(self, config: Config) -> None:
        def work() -> None:
            try:
                logger.info("preparing the index")
                index = prepare(config, self.tracker)
                # The one assignment that publishes. Everything before it is
                # incomplete and unreachable; everything after it is finished.
                self.index = index
                logger.info("index ready: %d sentences", len(index))
            except PreparationFailure as failure:
                # This field is a diagnostic and never reaches a browser, so it
                # keeps naming the underlying exception type, which is what is
                # useful in a log. What a client sees is the stable code and the
                # rewritten sentence the tracker holds.
                cause = failure.__cause__
                self.error = type(cause).__name__ if cause else failure.code
                logger.error("index preparation failed: %s", failure.code)
            except Exception as exc:  # reported through /api/health, not raised
                self.error = type(exc).__name__
                logger.error("index preparation failed: %s", type(exc).__name__)

        threading.Thread(target=work, name="index-preparation", daemon=True).start()

    def require(self):
        """The prepared index, or an HTTP error explaining why there is none."""
        if self.error is not None:
            raise HTTPException(
                status_code=503,
                detail={
                    "status": "failed",
                    "message": (
                        "The search index could not be prepared. Check that "
                        "corpus_root in config.yaml points at the text files."
                    ),
                },
            )
        if self.index is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "status": "preparing",
                    "message": "The search index is still being prepared.",
                },
            )
        return self.index


def create_app(config: Config | None = None, *, prepare: bool = True) -> FastAPI:
    """Build the application.

    Args:
        config: Which corpus to search. Defaults to the project's own settings,
            so the API and the command line read the same configuration.
        prepare: Start preparing the index when the server starts. Turned off by
            tests that supply an index directly.
    """
    settings = config or load_default_config()
    state = EngineState()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if prepare:
            state.prepare(settings)
        yield

    app = FastAPI(
        title="HEN autocomplete",
        version="1.0.0",
        summary="Sentence completions from a text corpus, tolerating one typing error.",
        lifespan=lifespan,
    )
    app.state.engine = state

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DEVELOPMENT_ORIGINS),
        allow_credentials=False,
        # POST is the retry endpoint; everything else is still GET only.
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    def engine() -> EngineState:
        return app.state.engine

    @app.get("/api/health", response_model=HealthResponse, tags=["status"])
    def health(state: EngineState = Depends(engine)) -> HealthResponse:
        """Whether the server can answer searches yet."""
        if state.error is not None:
            return HealthResponse(
                status="failed",
                ready=False,
                detail=(
                    "The search index could not be prepared. Check that "
                    "corpus_root in config.yaml points at the text files."
                ),
            )
        if state.index is None:
            snapshot = state.tracker.snapshot()
            return HealthResponse(
                status="preparing",
                ready=False,
                detail="Reading the corpus and preparing the search index.",
                phase=snapshot.phase.value,
                phase_label=snapshot.phase_label,
                cache_mode=snapshot.cache_mode.value,
                elapsed_seconds=snapshot.elapsed_seconds,
            )
        snapshot = state.tracker.snapshot()
        return HealthResponse(
            status="ready",
            ready=True,
            detail="Ready to search.",
            sentences=len(state.index),
            sources=len(state.index.records.paths),
            phase=snapshot.phase.value,
            phase_label=snapshot.phase_label,
            cache_mode=snapshot.cache_mode.value,
            elapsed_seconds=snapshot.elapsed_seconds,
            searchable_bytes=len(state.index.records.norm_blob),
        )

    @app.get("/api/complete", response_model=CompletionsResponse, tags=["search"])
    def complete(
        q: str = Query(
            default="",
            max_length=MAX_QUERY_LENGTH,
            description="The text typed so far.",
        ),
        limit: int | None = Query(default=None, ge=1, le=50),
        state: EngineState = Depends(engine),
    ) -> CompletionsResponse:
        """Best completions for the text typed so far, best first."""
        index = state.require()

        if not q.strip():
            return CompletionsResponse(query=q, count=0, results=[])

        try:
            results = find_completions(index, q, limit)
        except ValueError as exc:
            # The only ValueError the engine raises here is asking for more
            # results than the index was built to answer.
            raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc

        return CompletionsResponse(
            query=q,
            count=len(results),
            results=[Completion.of(result) for result in results],
        )

    app.include_router(create_build_router())
    return app
