import type { CompletionsResponse, Health } from "./types";

/**
 * Talking to the Python API.
 *
 * Requests carry an AbortSignal so a search the user has already moved past can
 * be dropped rather than raced.
 */

export class ApiUnavailable extends Error {
  constructor(message = "The search service is not reachable.") {
    super(message);
    this.name = "ApiUnavailable";
  }
}

export class IndexUnavailable extends Error {
  readonly status: "preparing" | "failed";

  constructor(status: "preparing" | "failed", message: string) {
    super(message);
    this.name = "IndexUnavailable";
    this.status = status;
  }
}

async function readProblem(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (typeof detail === "string") return detail;
    if (detail?.message) return detail.message as string;
  } catch {
    // fall through to the generic message below
  }
  return `The search service replied with ${response.status}.`;
}

export async function fetchHealth(signal?: AbortSignal): Promise<Health> {
  let response: Response;
  try {
    response = await fetch("/api/health", { signal });
  } catch {
    throw new ApiUnavailable();
  }
  if (!response.ok) throw new ApiUnavailable();
  return (await response.json()) as Health;
}

export async function fetchCompletions(
  query: string,
  signal?: AbortSignal,
): Promise<CompletionsResponse> {
  let response: Response;
  try {
    response = await fetch(`/api/complete?q=${encodeURIComponent(query)}`, { signal });
  } catch (error) {
    // An aborted request is not a failure; let the caller recognise it.
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiUnavailable();
  }

  if (response.status === 503) {
    let status: "preparing" | "failed" = "preparing";
    let message = "The search index is still being prepared.";
    try {
      const body = await response.json();
      status = body?.detail?.status === "failed" ? "failed" : "preparing";
      message = body?.detail?.message ?? message;
    } catch {
      // keep the defaults
    }
    throw new IndexUnavailable(status, message);
  }

  if (!response.ok) throw new Error(await readProblem(response));
  return (await response.json()) as CompletionsResponse;
}
