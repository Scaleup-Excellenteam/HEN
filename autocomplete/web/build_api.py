"""Reporting preparation progress to a browser.

Two endpoints, and one shape of data between them. ``/api/build/status`` answers
with the latest snapshot; ``/api/build/events`` streams snapshots as they are
published. Both serialize the same
:class:`~autocomplete.progress.ProgressSnapshot`, so a client that can render one
can render the other, and a client with no ``EventSource`` can poll the first and
lose nothing but immediacy.

Why Server-Sent Events, and not a socket
----------------------------------------

The traffic is one-directional, small, and text: the server has something to say
and the browser has nothing to reply. That is exactly what SSE is for, and it
arrives with reconnection already handled by the browser. A WebSocket would add
a second protocol, a second failure mode and a handshake, and would buy nothing.

How a slow client is kept harmless
----------------------------------

The stream does not own a queue. Each connection remembers the last sequence
number it sent and asks the tracker for whatever is newer, and the tracker keeps
a bounded history. So a client that stops reading cannot make anything grow: it
falls behind, and when it reads again it receives what is retained, which always
ends with the current state. The build itself never waits on a client, never
learns one exists, and one build serves every connection.

What is never sent
------------------

Only fields of the snapshot, which are built to be safe: the sole path among them
is the corpus-relative one the reader was given, and errors have already been
rewritten by :mod:`autocomplete.preparation` into codes and sentences. No
absolute path, no exception text and no configuration value passes through here.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..progress import (
    BuildState,
    ProgressSnapshot,
    ProgressTracker,
    expected_phases,
)

__all__ = [
    "HEARTBEAT_SECONDS",
    "POLL_SECONDS",
    "BuildStatus",
    "create_build_router",
    "snapshot_json",
]

#: How often the stream looks for something new. Short enough that completion
#: and failure reach the browser promptly, long enough that an idle connection
#: costs nothing worth measuring.
POLL_SECONDS = 0.1

#: How long a silent stream waits before sending a comment, so that a proxy or a
#: browser does not decide the connection has died.
HEARTBEAT_SECONDS = 15.0


class CompletedPhaseModel(BaseModel):
    phase: str
    label: str
    seconds: float


class IndexStatsModel(BaseModel):
    sentences: int
    files: int
    searchable_bytes: int
    longest_sentence: int
    suffix_positions: int
    block_count: int
    block_size: int
    summary_width: int


class BuildStatus(BaseModel):
    """One preparation snapshot, exactly as the stream sends it."""

    sequence: int = Field(
        description="Increases by one per snapshot. Discard anything not newer."
    )
    state: str = Field(description="idle, preparing, ready or failed.")
    phase: str
    phase_label: str
    detail: str
    determinate: bool = Field(
        description=(
            "Whether current/total mean anything here. False means the work "
            "cannot report its own progress, not that progress is unknown."
        )
    )
    current: int
    total: int | None = None
    current_file: str | None = Field(
        default=None,
        description="Path relative to the corpus root. Never a path on disk.",
    )
    files_done: int
    files_total: int | None = None
    sentences: int
    bytes_done: int
    bytes_total: int | None = None
    completed_phases: list[CompletedPhaseModel]
    phase_elapsed_seconds: float
    elapsed_seconds: float
    cache_mode: str
    #: The phases this route is expected to run, so an interface can show what
    #: is still to come rather than only what has happened.
    planned_phases: list[str]
    index: IndexStatsModel | None = None
    error_code: str | None = None
    error_message: str | None = None
    recovery_hint: str | None = None
    #: Whether asking for another attempt would be accepted right now.
    can_retry: bool


def snapshot_json(snapshot: ProgressSnapshot) -> dict:
    """Turn a snapshot into the object both endpoints send.

    Every field is copied explicitly. Nothing is reflected out of an object that
    might one day gain a field nobody meant to publish.
    """
    return {
        "sequence": snapshot.sequence,
        "state": snapshot.state.value,
        "phase": snapshot.phase.value,
        "phase_label": snapshot.phase_label,
        "detail": snapshot.detail,
        "determinate": snapshot.determinate,
        "current": snapshot.current,
        "total": snapshot.total,
        "current_file": snapshot.current_file,
        "files_done": snapshot.files_done,
        "files_total": snapshot.files_total,
        "sentences": snapshot.sentences,
        "bytes_done": snapshot.bytes_done,
        "bytes_total": snapshot.bytes_total,
        "completed_phases": [
            {"phase": item.phase.value, "label": item.label, "seconds": item.seconds}
            for item in snapshot.completed_phases
        ],
        "phase_elapsed_seconds": snapshot.phase_elapsed_seconds,
        "elapsed_seconds": snapshot.elapsed_seconds,
        "cache_mode": snapshot.cache_mode.value,
        "planned_phases": [
            phase.value for phase in expected_phases(snapshot.cache_mode)
        ],
        "index": asdict(snapshot.index) if snapshot.index is not None else None,
        "error_code": snapshot.error_code,
        "error_message": snapshot.error_message,
        "recovery_hint": snapshot.recovery_hint,
        "can_retry": snapshot.state is BuildState.FAILED,
    }


def _event(snapshot: ProgressSnapshot) -> str:
    """One SSE frame. The id lets a browser resume with Last-Event-ID."""
    payload = json.dumps(snapshot_json(snapshot), separators=(",", ":"))
    return f"id: {snapshot.sequence}\nevent: progress\ndata: {payload}\n\n"


def _tracker(request: Request) -> ProgressTracker:
    return request.app.state.engine.tracker


def create_build_router() -> APIRouter:
    """Build the router over the tracker held on the application."""
    router = APIRouter(prefix="/api/build", tags=["build"])

    @router.get("/status", response_model=BuildStatus)
    def status(request: Request) -> BuildStatus:
        """The latest preparation snapshot.

        Always answers, in every state, so a client with no streaming support
        can poll this and behave identically but less promptly.
        """
        return BuildStatus(**snapshot_json(_tracker(request).snapshot()))

    @router.get("/events")
    async def events(
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        """Snapshots as they are published, as Server-Sent Events.

        The current state is sent immediately on connection, so a browser that
        arrives late, or reconnects, is never left with a blank screen waiting
        for the next change.
        """
        tracker = _tracker(request)

        # A browser resuming a dropped connection tells us what it already has.
        # Anything unparseable is treated as "nothing", which replays only the
        # bounded history rather than failing the request.
        try:
            resume_from = int(last_event_id) if last_event_id else 0
        except ValueError:
            resume_from = 0

        async def stream():
            sent = resume_from
            silent_for = 0.0
            try:
                while True:
                    if await request.is_disconnected():
                        return

                    pending = tracker.since(sent)
                    if pending:
                        silent_for = 0.0
                        for snapshot in pending:
                            sent = snapshot.sequence
                            yield _event(snapshot)
                        if pending[-1].state.finished:
                            # Nothing more will ever be published for this
                            # preparation. The client closes on a terminal
                            # state; ending here means an idle connection is
                            # not held open for a build that is over.
                            return
                    else:
                        silent_for += POLL_SECONDS
                        if silent_for >= HEARTBEAT_SECONDS:
                            silent_for = 0.0
                            yield ": keep-alive\n\n"

                    await asyncio.sleep(POLL_SECONDS)
            except asyncio.CancelledError:
                # The client went away mid-write. Nothing to clean up: this
                # connection owns no queue and no build.
                raise

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @router.post("/retry", response_model=BuildStatus, status_code=202)
    def retry(request: Request) -> BuildStatus:
        """Attempt preparation again after it failed.

        Deliberately narrow. It takes no input at all, so there is no path or
        setting for a caller to influence; it reuses the configuration the
        server started with; it is refused unless the last attempt failed, so it
        cannot start a second build beside a running one or discard a working
        index; and it never forces a rebuild, so a valid cache is reused rather
        than deleted.
        """
        engine = request.app.state.engine
        if not engine.can_retry:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "not_retryable",
                    "message": (
                        "There is nothing to retry: preparation is either "
                        "running or has already succeeded."
                    ),
                },
            )
        engine.retry()
        return BuildStatus(**snapshot_json(engine.tracker.snapshot()))

    return router
