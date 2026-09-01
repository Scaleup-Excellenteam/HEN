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

**Zero-downtime refresh.** ``autocomplete.cache`` publishes a build by flipping
a pointer onto a new generation directory, never editing one in place. Once the
first index is ready, the same background thread polls that pointer every
``Config.refresh_interval`` seconds (``0`` turns this off) and, when it names a
generation this process has not adopted, loads it and republishes ``state.index``
in one attribute assignment, the same handover used at start-up. A request that
is already in flight keeps the index it looked up; the next request sees the
new one. The reported generation always names what was actually loaded:
``load_current`` reads the pointer once and returns the index and its
generation name together, so the two can never disagree even if another
generation is published in between the cheap pointer check and the real load.
A generation that fails validation, a stale format version, a corpus hash that
no longer matches, a damaged file, is skipped rather than adopted, so a bad
build can never interrupt a service that is already running; that same
generation name is then not retried on later ticks, because every reason
validation fails is permanent, until the pointer names something new.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..config import Config, load_default_config
from ..data import AutoCompleteData
from ..engine import find_completions

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
    generation: str | None = Field(
        default=None,
        description=(
            "The cache generation currently being served, or null before one "
            "has been adopted. Changes when the background refresh loop picks "
            "up a newer offline build."
        ),
    )


@dataclass
class EngineState:
    """Holds the one prepared index, and what to say before it exists.

    The index is published by assigning it here once it is complete, so a
    request either sees nothing and is told the server is preparing, or sees a
    finished index. There is no state in between for a request to observe.
    Later, a newer generation is published the same way: one assignment,
    never a mutation of the index a request may already be holding.
    """

    index: object | None = None
    error: str | None = None
    _started: bool = False
    _lock: threading.Lock = None  # type: ignore[assignment]
    _generation: str | None = None
    _failed_generation: str | None = None

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

    @property
    def generation(self) -> str | None:
        """The cache generation the current index was loaded from."""
        return self._generation

    def prepare(self, config: Config) -> None:
        """Build or load the index, once, whoever asks first.

        Once it is ready, and ``config.refresh_interval`` is positive, the same
        background thread keeps polling the cache pointer and adopts a newer
        generation as it is published, for the life of the process.
        """
        with self._lock:
            if self._started:
                return
            self._started = True

        def work() -> None:
            try:
                from ..cache import build_or_load, current_generation_name

                logger.info("preparing the index from %s", config.corpus_root)
                index = build_or_load(config)
                self.index = index
                self._generation = current_generation_name(config.cache_dir)
                logger.info("index ready: %d sentences", len(index))
            except Exception as exc:  # reported through /api/health, not raised
                self.error = f"{type(exc).__name__}: {exc}"
                logger.error("index preparation failed: %s", self.error)
                return

            if config.refresh_interval > 0:
                self._watch(config)

        threading.Thread(target=work, name="index-preparation", daemon=True).start()

    def _watch(self, config: Config) -> None:
        """Poll for a newer generation and adopt it, for the life of the process.

        Runs in the same thread ``prepare`` starts, after the first index is
        ready. A single failed tick, a transient one or a build published only
        halfway, is logged and never stops the next one.
        """
        while True:
            time.sleep(config.refresh_interval)
            try:
                self.refresh(config)
            except Exception:
                logger.exception("index refresh failed; keeping the current one")

    def refresh(self, config: Config) -> None:
        """Adopt a newer generation if the cache pointer has moved on.

        A no-op when the pointer still names the generation already serving,
        or the generation that most recently failed validation: every reason
        :func:`~autocomplete.cache.load_current` rejects a generation (wrong
        format version, a corpus hash that no longer matches, a truncated
        file, ...) is permanent, so a generation that failed once will fail
        identically forever. Retrying it every tick, with no backoff, would
        just repeat the same failing work indefinitely; it is only worth
        trying again once the pointer names something new. The index already
        serving requests is always preferred over no index at all.
        """
        from ..cache import CacheMiss, current_generation_name, load_current

        latest = current_generation_name(config.cache_dir)
        if latest is None or latest in (self._generation, self._failed_generation):
            return

        corpus_hash = None
        if config.validation_level in ("content", "full"):
            from .. import corpus

            corpus_hash = corpus.fingerprint(config.corpus_root)

        try:
            index, generation = load_current(
                config.cache_dir,
                corpus_hash=corpus_hash,
                level=config.validation_level,
                use_mmap=config.use_mmap,
                summary_width=config.num_results,
            )
        except CacheMiss as exc:
            logger.warning("generation %s is not usable yet: %s", latest, exc)
            self._failed_generation = latest
            return

        self.index = index
        self._generation = generation
        self._failed_generation = None
        logger.info("adopted generation %s: %d sentences", generation, len(index))

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
        allow_methods=["GET"],
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
            generation=state.generation,
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

    return app
