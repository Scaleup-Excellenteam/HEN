import { useCallback, useEffect, useRef, useState } from "react";
import {
  DriveRequestError,
  fetchDocuments,
  fetchDriveStatus,
  fetchJob,
  removeDocument,
  retryLastJob,
  startImport,
} from "./api";
import { GoogleFlowError, googleBridge } from "./google";
import type { GoogleBridge } from "./google";
import type { DriveState, DriveStatus, ImportedDocument, JobStatus } from "./types";

/**
 * The import flow, as one piece of state the panel renders.
 *
 * The access token lives here and nowhere else: in a ref, for the lifetime of
 * the tab. It is never written to `localStorage`, `sessionStorage` or a cookie,
 * so closing the tab ends the authorization and nothing survives to be found
 * later. A refresh means connecting again, which is the honest cost of not
 * keeping a long-lived credential in a browser.
 *
 * Polling only happens while something is running. When nothing is, the hook
 * makes no requests at all, so a page left open costs nothing.
 */

const POLL_INTERVAL_MS = 700;

export interface DriveView {
  /** What to render, including the one state only the browser knows. */
  state: DriveState;
  status: DriveStatus | null;
  documents: ImportedDocument[];
  job: JobStatus | null;
  /** A failure of ours or of Google's, whichever happened last. */
  error: { message: string; code: string; retryable: boolean } | null;
  /** Set when the user closed the picker without choosing. */
  cancelled: boolean;
  connected: boolean;
  busy: boolean;
  connect: () => Promise<void>;
  addFiles: () => Promise<void>;
  remove: (documentId: string) => Promise<void>;
  retry: () => Promise<void>;
  disconnect: () => void;
  dismissError: () => void;
}

interface Options {
  /** Substituted by tests, so no test needs Google or a network. */
  bridge?: GoogleBridge;
}

export function useDrive({ bridge = googleBridge }: Options = {}): DriveView {
  const [status, setStatus] = useState<DriveStatus | null>(null);
  const [documents, setDocuments] = useState<ImportedDocument[]>([]);
  const [job, setJob] = useState<JobStatus | null>(null);
  const [error, setError] = useState<DriveView["error"]>(null);
  const [cancelled, setCancelled] = useState(false);
  const [connected, setConnected] = useState(false);
  const [working, setWorking] = useState(false);

  // Held in memory only, for as long as this tab is open.
  const token = useRef<string | null>(null);
  const alive = useRef(true);
  const poll = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      if (poll.current) clearTimeout(poll.current);
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const next = await fetchDriveStatus();
      if (!alive.current) return next;
      setStatus(next);
      if (next.configured) {
        const list = await fetchDocuments();
        if (alive.current) setDocuments(list.documents);
      }
      return next;
    } catch (failure) {
      if (alive.current) setError(describe(failure));
      return null;
    }
  }, []);

  // Asking the server what it offers is subscribing to an external system, and
  // the request is made from inside the effect rather than through `refresh`
  // so that nothing sets state on the way into a render.
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    const look = async () => {
      try {
        const next = await fetchDriveStatus(controller.signal);
        if (cancelled) return;
        setStatus(next);
        if (!next.configured) return;
        const list = await fetchDocuments(controller.signal);
        if (!cancelled) setDocuments(list.documents);
      } catch (failure) {
        if (cancelled) return;
        if (failure instanceof DOMException && failure.name === "AbortError") return;
        setError(describe(failure));
      }
    };

    void look();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, []);

  const fail = useCallback((failure: unknown) => {
    setWorking(false);
    const described = describe(failure);
    // A closed picker or sign-in window is the user changing their mind, not a
    // failure to report as one.
    if (described.code === "cancelled") {
      setCancelled(true);
      return;
    }
    if (described.code === "auth_failed" || described.code === "denied") {
      token.current = null;
      setConnected(false);
    }
    setError(described);
  }, []);

  /** Follow a running job to its end, then refresh what is imported. */
  const follow = useCallback(
    (started: JobStatus) => {
      setJob(started);
      if (started.state === "complete" || started.state === "failed") {
        setWorking(false);
        void refresh();
        return;
      }
      const tick = async () => {
        try {
          const next = await fetchJob(started.id);
          if (!alive.current) return;
          setJob(next);
          if (next.state === "complete" || next.state === "failed") {
            setWorking(false);
            void refresh();
          } else {
            poll.current = setTimeout(() => void tick(), POLL_INTERVAL_MS);
          }
        } catch (failure) {
          if (alive.current) fail(failure);
        }
      };
      poll.current = setTimeout(() => void tick(), POLL_INTERVAL_MS);
    },
    [fail, refresh],
  );

  const authorize = useCallback(async (): Promise<string> => {
    const current = status;
    if (!current?.configured) {
      throw new GoogleFlowError(
        "Google Drive import is not configured on this server.",
        "not_configured",
      );
    }
    if (token.current) return token.current;

    const granted = await bridge.requestAccessToken({
      clientId: current.client_id,
      apiKey: current.api_key,
      appId: current.app_id,
      scope: current.scope,
      mimeTypes: current.limits.supported_mime_types,
    });
    token.current = granted;
    if (alive.current) setConnected(true);
    return granted;
  }, [bridge, status]);

  const connect = useCallback(async () => {
    setError(null);
    setCancelled(false);
    try {
      await authorize();
    } catch (failure) {
      fail(failure);
    }
  }, [authorize, fail]);

  const addFiles = useCallback(async () => {
    setError(null);
    setCancelled(false);
    const current = status;
    if (!current?.configured) return;

    try {
      const granted = await authorize();
      const chosen = await bridge.pickFiles(
        {
          clientId: current.client_id,
          apiKey: current.api_key,
          appId: current.app_id,
          scope: current.scope,
          mimeTypes: current.limits.supported_mime_types,
        },
        granted,
      );
      if (!chosen) {
        setCancelled(true);
        return;
      }
      setWorking(true);
      follow(await startImport(chosen, granted));
    } catch (failure) {
      fail(failure);
    }
  }, [authorize, bridge, fail, follow, status]);

  const remove = useCallback(
    async (documentId: string) => {
      setError(null);
      setCancelled(false);
      setWorking(true);
      try {
        follow(await removeDocument(documentId));
      } catch (failure) {
        fail(failure);
      }
    },
    [fail, follow],
  );

  const retry = useCallback(async () => {
    setError(null);
    setCancelled(false);
    try {
      // A removal can simply be repeated; an import needs a fresh authorization,
      // because none was kept anywhere.
      const needsToken = job?.needs_authorization ?? true;
      const granted = needsToken ? await authorize() : undefined;
      setWorking(true);
      follow(await retryLastJob(granted));
    } catch (failure) {
      fail(failure);
    }
  }, [authorize, fail, follow, job]);

  const disconnect = useCallback(() => {
    token.current = null;
    setConnected(false);
    setCancelled(false);
    setError(null);
  }, []);

  const dismissError = useCallback(() => {
    setError(null);
    setCancelled(false);
  }, []);

  const busy = working || isRunning(job);

  return {
    state: viewState(status, connected, busy, job),
    status,
    documents,
    job,
    error,
    cancelled,
    connected,
    busy,
    connect,
    addFiles,
    remove,
    retry,
    disconnect,
    dismissError,
  };
}

function isRunning(job: JobStatus | null): boolean {
  return job !== null && job.state !== "complete" && job.state !== "failed";
}

/**
 * What the person sees.
 *
 * The server reports what it is doing; whether this browser holds an
 * authorization is something only the browser knows, so `disconnected` is added
 * here rather than asked for.
 */
function viewState(
  status: DriveStatus | null,
  connected: boolean,
  busy: boolean,
  job: JobStatus | null,
): DriveState {
  if (!status || !status.configured) return "disabled";
  if (busy && job && job.state !== "complete" && job.state !== "failed") {
    return job.state === "pending" ? "downloading" : job.state;
  }
  if (job?.state === "failed") return "failed";
  if (!connected) return "disconnected";
  return "ready";
}

function describe(failure: unknown): NonNullable<DriveView["error"]> {
  if (failure instanceof DriveRequestError) {
    return {
      message: failure.message,
      code: failure.code,
      retryable: failure.retryable,
    };
  }
  if (failure instanceof GoogleFlowError) {
    return { message: failure.message, code: failure.code, retryable: true };
  }
  // Never render an unknown exception's own text: it can carry anything.
  return {
    message: "Something went wrong. Try again.",
    code: "unknown",
    retryable: true,
  };
}
