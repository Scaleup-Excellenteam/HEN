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

**Imported documents.** When the optional Google Drive feature is configured,
documents a user has imported are held in a second index and searched alongside
the corpus by :func:`autocomplete.composite.search`, which returns the true best
results over both. With the feature off, or with nothing imported, ``/api/complete``
calls the engine directly and answers exactly what it answered before the feature
existed: same code path, same response, same fields.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .drive_api import create_drive_router

from .. import composite
from ..config import Config, load_default_config
from ..data import AutoCompleteData

if TYPE_CHECKING:  # pragma: no cover - import cost avoided at runtime
    from ..drive.jobs import DriveService

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
    status: str = Field(description="One of: preparing, ready, failed.")
    ready: bool
    detail: str
    sentences: int | None = None
    sources: int | None = None


@dataclass
class EngineState:
    """Holds the one prepared index, and what to say before it exists.

    The index is published by assigning it here once it is complete, so a
    request either sees nothing and is told the server is preparing, or sees a
    finished index. There is no state in between for a request to observe.
    """

    index: object | None = None
    error: str | None = None
    _started: bool = False
    _lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    @property
    def status(self) -> str:
        if self.error is not None:
            return "failed"
        return "ready" if self.index is not None else "preparing"

    @property
    def ready(self) -> bool:
        return self.index is not None

    def prepare(self, config: Config) -> None:
        """Build or load the index, once, whoever asks first."""
        with self._lock:
            if self._started:
                return
            self._started = True

        def work() -> None:
            try:
                from ..cache import build_or_load

                logger.info("preparing the index from %s", config.corpus_root)
                index = build_or_load(config)
                self.index = index
                logger.info("index ready: %d sentences", len(index))
            except Exception as exc:  # reported through /api/health, not raised
                self.error = f"{type(exc).__name__}: {exc}"
                logger.error("index preparation failed: %s", self.error)

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


def create_app(
    config: Config | None = None,
    *,
    prepare: bool = True,
    drive: "DriveService | None" = None,
) -> FastAPI:
    """Build the application.

    Args:
        config: Which corpus to search. Defaults to the project's own settings,
            so the API and the command line read the same configuration.
        prepare: Start preparing the index when the server starts. Turned off by
            tests that supply an index directly.
        drive: The Google Drive import feature. Defaults to reading its own
            settings from the environment, where it is off unless configured.
            Tests pass one wired to a fake Drive.
    """
    settings = config or load_default_config()
    state = EngineState()
    drive_service = drive if drive is not None else _drive_service(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if prepare:
            state.prepare(settings)
        # Adopt whatever was imported before this process started, so imported
        # documents survive a restart. Never raises: a state that cannot be read
        # is reported through /api/drive/status, and the corpus search is
        # unaffected either way.
        app.state.drive.load_published_state()
        yield

    app = FastAPI(
        title="HEN autocomplete",
        version="1.0.0",
        summary="Sentence completions from a text corpus, tolerating one typing error.",
        lifespan=lifespan,
    )
    app.state.engine = state
    app.state.drive = drive_service

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DEVELOPMENT_ORIGINS),
        allow_credentials=False,
        # POST and DELETE are the import and removal endpoints; the search
        # endpoints are still GET only.
        allow_methods=["GET", "POST", "DELETE"],
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
            return HealthResponse(
                status="preparing",
                ready=False,
                detail="Reading the corpus and preparing the search index.",
            )
        return HealthResponse(
            status="ready",
            ready=True,
            detail="Ready to search.",
            sentences=len(state.index),
            sources=len(state.index.records.paths),
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
            # The imported index, if there is one, is read once here. Publishing
            # it is a single assignment of a finished object, so this is either
            # the state before an import or the state after it, never a mixture.
            results = composite.search(index, app.state.drive.overlay, q, limit)
        except ValueError as exc:
            # The only ValueError the engine raises here is asking for more
            # results than the index was built to answer.
            raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc

        return CompletionsResponse(
            query=q,
            count=len(results),
            results=[Completion.of(result) for result in results],
        )

    app.include_router(create_drive_router())
    return app


def _drive_service(settings: Config) -> "DriveService":
    """Build the Drive feature from the environment.

    Imported here rather than at module scope so that the import cost, and the
    module itself, are only reached by the server. The command line never
    touches this file.
    """
    from ..drive.jobs import DriveService

    return DriveService(config=settings)
