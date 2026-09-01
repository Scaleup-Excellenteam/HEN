import { useCallback, useEffect, useRef, useState } from "react";
import type { BuildStatus } from "./types";

/**
 * Watching the server prepare its index.
 *
 * Streams snapshots over Server-Sent Events, and falls back to polling the
 * snapshot endpoint where `EventSource` is unavailable. Both paths produce the
 * same values, so the interface never has to know which one is running.
 *
 * Three things keep the displayed state honest.
 *
 * **Sequence numbers decide what is current.** A snapshot is applied only if it
 * is newer than the one held. A reconnect that replays part of the history, or
 * a poll answering out of order, therefore cannot move the screen backwards.
 *
 * **The stream is closed once preparation is over.** The server ends it after a
 * terminal snapshot, and the browser would otherwise reconnect to a finished
 * build for as long as the page stayed open.
 *
 * **Nothing here invents anything.** No timer advances a number, and no
 * percentage is computed for a phase the server marked indeterminate.
 */

const STATUS_URL = "/api/build/status";
const EVENTS_URL = "/api/build/events";
const RETRY_URL = "/api/build/retry";

/** How often the fallback asks, when it is the fallback that is running. */
export const POLL_INTERVAL_MS = 400;

/** How long to wait before reopening a stream that dropped mid-build. */
export const RECONNECT_DELAY_MS = 1000;

export type BuildProgressState =
  | { kind: "connecting" }
  | { kind: "watching"; status: BuildStatus }
  | { kind: "offline" };

interface Options {
  /** Substituted by tests. Returning null forces the polling fallback. */
  createEventSource?: (url: string) => EventSource | null;
}

function defaultEventSource(url: string): EventSource | null {
  if (typeof EventSource === "undefined") return null;
  try {
    return new EventSource(url);
  } catch {
    return null;
  }
}

export function useBuildProgress({
  createEventSource = defaultEventSource,
}: Options = {}) {
  const [state, setState] = useState<BuildProgressState>({ kind: "connecting" });
  const [attempt, setAttempt] = useState(0);

  // The highest sequence applied. Held in a ref so the stream handler can read
  // it without the effect depending on state and tearing itself down per event.
  const latest = useRef(0);
  const alive = useRef(true);

  const apply = useCallback((status: BuildStatus) => {
    if (!alive.current) return;
    // Stale or duplicated snapshots are dropped rather than rendered: this is
    // what makes a reconnect that replays history harmless.
    if (status.sequence <= latest.current) return;
    latest.current = status.sequence;
    setState({ kind: "watching", status });
  }, []);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    let closed = false;
    let source: EventSource | null = null;
    let poller: ReturnType<typeof setTimeout> | null = null;
    let reconnect: ReturnType<typeof setTimeout> | null = null;
    const controller = new AbortController();

    const finished = (status: BuildStatus) =>
      status.state === "ready" || status.state === "failed";

    const stop = () => {
      closed = true;
      source?.close();
      source = null;
      if (poller) clearTimeout(poller);
      if (reconnect) clearTimeout(reconnect);
      controller.abort();
    };

    /** Ask once for the current snapshot; the fallback, and the first read. */
    const askOnce = async (): Promise<BuildStatus | null> => {
      try {
        const response = await fetch(STATUS_URL, { signal: controller.signal });
        if (!response.ok) throw new Error(String(response.status));
        const status = (await response.json()) as BuildStatus;
        apply(status);
        return status;
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return null;
        if (!closed && alive.current) setState({ kind: "offline" });
        return null;
      }
    };

    const poll = async () => {
      if (closed) return;
      const status = await askOnce();
      if (closed) return;
      // Stop asking once preparation is over: there will never be another
      // snapshot, and a page left open should cost nothing.
      if (status && finished(status)) return;
      poller = setTimeout(() => void poll(), POLL_INTERVAL_MS);
    };

    const listen = () => {
      if (closed) return;
      source = createEventSource(EVENTS_URL);
      if (!source) {
        void poll();
        return;
      }

      source.addEventListener("progress", (event) => {
        try {
          const status = JSON.parse((event as MessageEvent).data) as BuildStatus;
          apply(status);
          if (finished(status)) {
            // The server has ended the stream. Close ours too, or the browser
            // will keep reopening it for a build that is over.
            stop();
          }
        } catch {
          // A frame that is not the shape we expect is ignored rather than
          // allowed to blank the screen; the next one will be complete.
        }
      });

      source.addEventListener("error", () => {
        if (closed) return;
        source?.close();
        source = null;
        // A dropped stream is not a failed build. Ask for the current state at
        // once, then reopen; if the server has gone, askOnce reports offline.
        void askOnce().then((status) => {
          if (closed || (status && finished(status))) return;
          reconnect = setTimeout(listen, RECONNECT_DELAY_MS);
        });
      });
    };

    // The first read is a plain request, so the screen has something to show
    // immediately even if the stream takes a moment to open.
    void askOnce().then((status) => {
      if (closed || (status && finished(status))) return;
      listen();
    });

    return stop;
  }, [apply, attempt, createEventSource]);

  /** Ask the server to prepare again after a failure. */
  const retry = useCallback(async () => {
    try {
      const response = await fetch(RETRY_URL, { method: "POST" });
      if (response.ok) {
        const status = (await response.json()) as BuildStatus;
        // A new preparation restarts the sequence from where it left off, but
        // never below it, so applying by sequence stays correct.
        apply(status);
      }
    } catch {
      // Reported by the watch loop below, which reconnects either way.
    }
    setAttempt((value) => value + 1);
  }, [apply]);

  return { state, retry };
}
