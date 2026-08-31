import { useCallback, useEffect, useRef, useState } from "react";
import { ApiUnavailable, fetchHealth } from "../api";
import type { Health } from "../types";

export type HealthState =
  | { kind: "checking" }
  | { kind: "ready"; health: Health }
  | { kind: "preparing"; health: Health }
  | { kind: "failed"; detail: string }
  | { kind: "offline" };

const POLL_INTERVAL_MS = 1500;

/**
 * Watches whether the server can answer searches.
 *
 * While the index is being prepared this keeps asking, so the page moves from
 * "getting ready" to usable on its own rather than needing a reload.
 */
export function useHealth(): { state: HealthState; recheck: () => void } {
  const [state, setState] = useState<HealthState>({ kind: "checking" });
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [attempt, setAttempt] = useState(0);

  const recheck = useCallback(() => setAttempt((value) => value + 1), []);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    const check = async () => {
      try {
        const health = await fetchHealth(controller.signal);
        if (cancelled) return;
        if (health.status === "ready") setState({ kind: "ready", health });
        else if (health.status === "failed")
          setState({ kind: "failed", detail: health.detail });
        else {
          setState({ kind: "preparing", health });
          timer.current = setTimeout(check, POLL_INTERVAL_MS);
        }
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiUnavailable) setState({ kind: "offline" });
        else setState({ kind: "offline" });
      }
    };

    void check();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer.current) clearTimeout(timer.current);
    };
  }, [attempt]);

  return { state, recheck };
}
