import { vi } from "vitest";
import type { BuildPhase, BuildStatus } from "../build/types";
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

const COLD_PLAN: BuildPhase[] = [
  "loading_configuration",
  "verifying_suffix_builder",
  "validating_corpus",
  "discovering_corpus",
  "reading_files",
  "normalizing_records",
  "building_suffix_array",
  "building_block_summaries",
  "writing_artifacts",
  "checksumming_artifacts",
  "publishing_generation",
  "ready",
];

const WARM_PLAN: BuildPhase[] = [
  "loading_configuration",
  "verifying_suffix_builder",
  "validating_corpus",
  "validating_artifacts",
  "loading_artifacts",
  "ready",
];

/**
 * A preparation snapshot. Ready by default, so every test that is not about
 * preparation sees the search interface, exactly as it did before this feature.
 */
export function buildStatus(overrides: Partial<BuildStatus> = {}): BuildStatus {
  const state = overrides.state ?? "ready";
  return {
    sequence: 1,
    state,
    phase: "ready",
    phase_label: "Ready",
    detail: "Ready to search.",
    determinate: false,
    current: 0,
    total: null,
    current_file: null,
    files_done: 1504,
    files_total: 1504,
    sentences: 2391950,
    bytes_done: 98700000,
    bytes_total: 98700000,
    completed_phases: [],
    phase_elapsed_seconds: 0,
    elapsed_seconds: 0.42,
    cache_mode: "warm_load",
    planned_phases: WARM_PLAN,
    index: {
      sentences: 2391950,
      files: 1504,
      searchable_bytes: 98733611,
      longest_sentence: 385,
      suffix_positions: 98733611,
      block_count: 24105,
      block_size: 4096,
      summary_width: 5,
    },
    error_code: null,
    error_message: null,
    recovery_hint: null,
    can_retry: state === "failed",
    ...overrides,
  };
}

/** A snapshot part-way through a first build. */
export function buildingStatus(overrides: Partial<BuildStatus> = {}): BuildStatus {
  return buildStatus({
    sequence: 7,
    state: "preparing",
    phase: "reading_files",
    phase_label: "Reading corpus files",
    detail: "Reading 1,504 corpus files.",
    determinate: true,
    current: 812,
    total: 1504,
    current_file: "rfc/rfc7707.txt",
    files_done: 812,
    files_total: 1504,
    sentences: 1_204_881,
    bytes_done: 51_200_000,
    bytes_total: 98_700_000,
    completed_phases: [
      { phase: "loading_configuration", label: "Loading configuration", seconds: 0.002 },
      {
        phase: "verifying_suffix_builder",
        label: "Verifying the suffix array builder",
        seconds: 0.011,
      },
      { phase: "discovering_corpus", label: "Discovering corpus files", seconds: 0.09 },
    ],
    phase_elapsed_seconds: 3.4,
    elapsed_seconds: 4.1,
    cache_mode: "cold_build",
    planned_phases: COLD_PLAN,
    index: null,
    ...overrides,
  });
}

export { COLD_PLAN, WARM_PLAN };

interface Route {
  health?: Health;
  results?: Completion[];
  /** Delay the completions reply, for testing loading and stale answers. */
  delayMs?: number;
  failHealth?: boolean;
  failComplete?: "network" | "preparing" | "server";
  /** The preparation snapshot /api/build/status answers with. */
  build?: BuildStatus;
  /** Successive snapshots, one per request, for watching a build progress. */
  buildSequence?: BuildStatus[];
  failBuild?: boolean;
  /** What POST /api/build/retry answers with. */
  retryStatus?: BuildStatus;
}

/** Stands in for the API, one route table per test. */
export function mockApi(route: Route = {}) {
  const calls: string[] = [];
  const buildCalls: string[] = [];
  const queued = [...(route.buildSequence ?? [])];

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);

    if (url.startsWith("/api/health")) {
      if (route.failHealth) throw new TypeError("Failed to fetch");
      return json(route.health ?? READY);
    }

    if (url.startsWith("/api/build")) {
      buildCalls.push(`${init?.method ?? "GET"} ${url}`);
      if (route.failBuild) throw new TypeError("Failed to fetch");
      if (url.startsWith("/api/build/retry")) {
        return json(route.retryStatus ?? buildingStatus({ sequence: 100 }));
      }
      // A queued snapshot per request, holding the last one once they run out,
      // which is what a finished build actually does.
      if (queued.length > 1) return json(queued.shift() as BuildStatus);
      if (queued.length === 1) return json(queued[0]);
      return json(route.build ?? buildStatus());
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
  return { fetchMock, calls, buildCalls };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}
