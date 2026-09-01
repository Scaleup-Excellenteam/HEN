import type { DocumentList, DriveStatus, JobStatus } from "./types";

/**
 * Talking to the Drive import endpoints.
 *
 * The access token goes in a header, never in the query string, so it cannot be
 * written to a server access log or left in browser history. It is passed for
 * the one request that needs it and is never persisted here either: the only
 * copy this page holds lives in React state for as long as the tab is open.
 */

const TOKEN_HEADER = "X-Drive-Access-Token";

/** A failure the server described, carrying the code the interface branches on. */
export class DriveRequestError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly status: number;

  constructor(message: string, code: string, retryable: boolean, status: number) {
    super(message);
    this.name = "DriveRequestError";
    this.code = code;
    this.retryable = retryable;
    this.status = status;
  }
}

async function readError(response: Response): Promise<DriveRequestError> {
  let code = "unknown";
  let message = `The server replied with ${response.status}.`;
  let retryable = response.status >= 500 || response.status === 429;
  try {
    const detail = (await response.json())?.detail;
    if (typeof detail?.message === "string") message = detail.message;
    if (typeof detail?.code === "string") code = detail.code;
    if (typeof detail?.retryable === "boolean") retryable = detail.retryable;
    // A validation failure comes back as a list rather than an object; say
    // something usable rather than rendering pydantic's shape at the reader.
    if (Array.isArray(detail)) {
      code = "invalid_request";
      message = "That selection could not be sent. Try selecting the files again.";
    }
  } catch {
    // keep the defaults
  }
  return new DriveRequestError(message, code, retryable, response.status);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new DriveRequestError(
      "The search service is not reachable.",
      "offline",
      true,
      0,
    );
  }
  if (!response.ok) throw await readError(response);
  return (await response.json()) as T;
}

export function fetchDriveStatus(signal?: AbortSignal): Promise<DriveStatus> {
  return request<DriveStatus>("/api/drive/status", { signal });
}

export function fetchDocuments(signal?: AbortSignal): Promise<DocumentList> {
  return request<DocumentList>("/api/drive/documents", { signal });
}

export function startImport(
  fileIds: string[],
  accessToken: string,
  signal?: AbortSignal,
): Promise<JobStatus> {
  return request<JobStatus>("/api/drive/imports", {
    method: "POST",
    headers: { "content-type": "application/json", [TOKEN_HEADER]: accessToken },
    body: JSON.stringify({ file_ids: fileIds }),
    signal,
  });
}

export function fetchJob(jobId: string, signal?: AbortSignal): Promise<JobStatus> {
  return request<JobStatus>(`/api/drive/imports/${encodeURIComponent(jobId)}`, {
    signal,
  });
}

export function removeDocument(
  documentId: string,
  signal?: AbortSignal,
): Promise<JobStatus> {
  return request<JobStatus>(
    `/api/drive/documents/${encodeURIComponent(documentId)}`,
    { method: "DELETE", signal },
  );
}

export function retryLastJob(
  accessToken?: string,
  signal?: AbortSignal,
): Promise<JobStatus> {
  return request<JobStatus>("/api/drive/retry", {
    method: "POST",
    headers: accessToken ? { [TOKEN_HEADER]: accessToken } : undefined,
    signal,
  });
}

/** Whether a completion came from an imported document rather than the corpus. */
export function isImported(sourceText: string, prefix: string): boolean {
  return prefix.length > 0 && sourceText.startsWith(`${prefix}/`);
}

/** A source path with the imported namespace taken off, for display. */
export function withoutPrefix(sourceText: string, prefix: string): string {
  return isImported(sourceText, prefix)
    ? sourceText.slice(prefix.length + 1)
    : sourceText;
}

/** Bytes as something a person reads, for limits and document sizes. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
