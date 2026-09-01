import { vi } from "vitest";
import type { DocumentList, DriveStatus, ImportedDocument, JobStatus } from "../drive/types";
import type { Completion, Health } from "../types";

export const READY: Health = {
  status: "ready",
  ready: true,
  detail: "Ready to search.",
  sentences: 2391950,
  sources: 1504,
};

export const PREPARING: Health = {
  status: "preparing",
  ready: false,
  detail: "Reading the corpus and preparing the search index.",
};

export function completion(overrides: Partial<Completion> = {}): Completion {
  return {
    completed_sentence: "Alpha: this is a demo.",
    source_text: "example.txt",
    offset: 1,
    score: 14,
    ...overrides,
  };
}

export function fiveCompletions(): Completion[] {
  return ["Alpha", "Beta", "Delta", "Gamma", "Omega"].map((name, index) =>
    completion({
      completed_sentence: `${name}: this is a demo.`,
      offset: index + 1,
    }),
  );
}

/**
 * The Drive feature as it is by default: switched off. Every test that does not
 * opt into it sees this, which is the same thing a server with no Google
 * configuration reports.
 */
export const DRIVE_DISABLED: DriveStatus = {
  enabled: false,
  configured: false,
  state: "disabled",
  detail: "Google Drive import is switched off on this server.",
  client_id: "",
  api_key: "",
  app_id: "",
  scope: "https://www.googleapis.com/auth/drive.file",
  source_prefix: "Google Drive",
  limits: {
    max_files: 10,
    max_file_bytes: 10485760,
    max_total_bytes: 52428800,
    supported_mime_types: ["text/plain", "application/vnd.google-apps.document"],
  },
  documents: 0,
  sentences: 0,
  total_bytes: 0,
  job: null,
  load_error: null,
};

export function driveReady(overrides: Partial<DriveStatus> = {}): DriveStatus {
  return {
    ...DRIVE_DISABLED,
    enabled: true,
    configured: true,
    state: "ready",
    detail: "No documents imported yet.",
    client_id: "client-id.apps.googleusercontent.com",
    api_key: "browser-api-key",
    app_id: "123456789",
    ...overrides,
  };
}

export function importedDocument(
  overrides: Partial<ImportedDocument> = {},
): ImportedDocument {
  return {
    id: "abc123",
    name: "notes.txt",
    mime_type: "text/plain",
    source_text: "Google Drive/notes.txt",
    imported_at: "2026-09-01T12:00:00+00:00",
    modified_time: "2026-09-01T09:00:00.000Z",
    bytes: 42,
    sentences: 2,
    status: "indexed",
    ...overrides,
  };
}

export function job(overrides: Partial<JobStatus> = {}): JobStatus {
  return {
    id: "job1",
    kind: "import",
    state: "complete",
    progress: {
      files_selected: 1,
      files_downloaded: 1,
      files_reused: 0,
      bytes_downloaded: 42,
      lines_read: 2,
      sentences_indexed: 2,
      detail: "1 document searchable.",
    },
    error: null,
    started_at: "2026-09-01T12:00:00+00:00",
    finished_at: "2026-09-01T12:00:01+00:00",
    needs_authorization: true,
    ...overrides,
  };
}

interface Route {
  health?: Health;
  results?: Completion[];
  /** Delay the completions reply, for testing loading and stale answers. */
  delayMs?: number;
  failHealth?: boolean;
  failComplete?: "network" | "preparing" | "server";
  /** The Drive feature. Off unless a test says otherwise. */
  drive?: DriveStatus;
  documents?: ImportedDocument[];
  /** Jobs the server hands back, in order, for import, removal and retry. */
  jobs?: JobStatus[];
  failDrive?: { status: number; code: string; message: string; retryable?: boolean };
}

/** Stands in for the API, one route table per test. */
export function mockApi(route: Route = {}) {
  const calls: string[] = [];
  const driveCalls: { method: string; url: string; body?: unknown; token?: string }[] = [];
  let documents = route.documents ?? [];
  const queued = [...(route.jobs ?? [])];

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);

    if (url.startsWith("/api/health")) {
      if (route.failHealth) throw new TypeError("Failed to fetch");
      return json(route.health ?? READY);
    }

    if (url.startsWith("/api/drive")) {
      const method = init?.method ?? "GET";
      const headers = new Headers(init?.headers as HeadersInit | undefined);
      driveCalls.push({
        method,
        url,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        token: headers.get("X-Drive-Access-Token") ?? undefined,
      });

      if (url.startsWith("/api/drive/status")) {
        return json(route.drive ?? DRIVE_DISABLED);
      }
      if (url.startsWith("/api/drive/documents") && method === "GET") {
        return json({
          count: documents.length,
          total_bytes: documents.reduce((sum, item) => sum + item.bytes, 0),
          documents,
        } satisfies DocumentList);
      }
      if (route.failDrive) {
        return json(
          {
            detail: {
              code: route.failDrive.code,
              message: route.failDrive.message,
              retryable: route.failDrive.retryable ?? false,
            },
          },
          route.failDrive.status,
        );
      }
      if (method === "DELETE") {
        const id = url.split("/").pop() ?? "";
        documents = documents.filter((item) => item.id !== id);
        return json(queued.shift() ?? job({ kind: "remove" }));
      }
      return json(queued.shift() ?? job());
    }

    const query = new URL(url, "http://localhost").searchParams.get("q") ?? "";
    calls.push(query);

    if (route.failComplete === "network") throw new TypeError("Failed to fetch");
    if (route.failComplete === "preparing")
      return json(
        { detail: { status: "preparing", message: "The search index is still being prepared." } },
        503,
      );
    if (route.failComplete === "server")
      return json({ detail: { message: "Something broke." } }, 500);

    if (route.delayMs) {
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(resolve, route.delayMs);
        init?.signal?.addEventListener("abort", () => {
          clearTimeout(timer);
          reject(new DOMException("Aborted", "AbortError"));
        });
      });
    }

    const results = route.results ?? [];
    return json({ query, count: results.length, results });
  });

  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls, driveCalls };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}
