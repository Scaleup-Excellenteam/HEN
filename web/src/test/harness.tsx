import { vi } from "vitest";
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

interface Route {
  health?: Health;
  results?: Completion[];
  /** Delay the completions reply, for testing loading and stale answers. */
  delayMs?: number;
  failHealth?: boolean;
  failComplete?: "network" | "preparing" | "server";
}

/** Stands in for the API, one route table per test. */
export function mockApi(route: Route = {}) {
  const calls: string[] = [];

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);

    if (url.startsWith("/api/health")) {
      if (route.failHealth) throw new TypeError("Failed to fetch");
      return json(route.health ?? READY);
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
  return { fetchMock, calls };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}
