import { useCallback, useEffect, useRef, useState } from "react";
import { ApiUnavailable, IndexUnavailable, fetchCompletions } from "../api";
import type { Completion } from "../types";

export type SearchState =
  | { kind: "idle" }
  | { kind: "searching" }
  | { kind: "results"; query: string; results: Completion[] }
  | { kind: "empty"; query: string }
  | { kind: "unavailable"; status: "preparing" | "failed"; message: string }
  | { kind: "offline" }
  | { kind: "error"; message: string };

export const DEBOUNCE_MS = 180;

/**
 * Runs searches for the text typed so far.
 *
 * Two things keep the displayed answer honest while typing. Each request
 * carries a sequence number, and a reply is ignored unless it belongs to the
 * most recent one, so a slow answer to an earlier query can never replace a
 * newer one. And the previous request is aborted when a new one starts, so the
 * server is not asked to finish work nobody is waiting for.
 */
export function useCompletions() {
  const [state, setState] = useState<SearchState>({ kind: "idle" });
  const latest = useRef(0);
  const inFlight = useRef<AbortController | null>(null);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  const cancelPending = useCallback(() => {
    if (debounce.current) {
      clearTimeout(debounce.current);
      debounce.current = null;
    }
    inFlight.current?.abort();
    inFlight.current = null;
  }, []);

  const run = useCallback(
    async (query: string) => {
      const trimmed = query.trim();
      if (!trimmed) {
        cancelPending();
        latest.current += 1;
        setState({ kind: "idle" });
        return;
      }

      inFlight.current?.abort();
      const controller = new AbortController();
      inFlight.current = controller;

      const sequence = ++latest.current;
      setState({ kind: "searching" });

      try {
        const body = await fetchCompletions(query, controller.signal);
        if (sequence !== latest.current) return; // a newer search has started
        setState(
          body.results.length
            ? { kind: "results", query, results: body.results }
            : { kind: "empty", query },
        );
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (sequence !== latest.current) return;
        if (error instanceof IndexUnavailable)
          setState({ kind: "unavailable", status: error.status, message: error.message });
        else if (error instanceof ApiUnavailable) setState({ kind: "offline" });
        else
          setState({
            kind: "error",
            message: error instanceof Error ? error.message : "Something went wrong.",
          });
      }
    },
    [cancelPending],
  );

  /** Search now, for Enter and the search button. */
  const search = useCallback(
    (query: string) => {
      if (debounce.current) clearTimeout(debounce.current);
      void run(query);
    },
    [run],
  );

  /** Search shortly, for typing. */
  const searchSoon = useCallback(
    (query: string) => {
      if (debounce.current) clearTimeout(debounce.current);
      if (!query.trim()) {
        void run(query);
        return;
      }
      debounce.current = setTimeout(() => void run(query), DEBOUNCE_MS);
    },
    [run],
  );

  const reset = useCallback(() => {
    cancelPending();
    latest.current += 1;
    setState({ kind: "idle" });
  }, [cancelPending]);

  useEffect(() => cancelPending, [cancelPending]);

  return { state, search, searchSoon, reset };
}
