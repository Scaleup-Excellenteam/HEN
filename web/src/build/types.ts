/**
 * The preparation snapshot, exactly as `/api/build/status` and
 * `/api/build/events` send it.
 *
 * Mirrors `BuildStatus` in `autocomplete/web/build_api.py`. Every value here
 * came from work the server actually did; there is no estimate and no
 * percentage in this shape, and `determinate` says whether `current`/`total`
 * mean anything at all for the phase that is running.
 */

export type BuildState = "idle" | "preparing" | "ready" | "failed";

/** Every phase the preparation can report. */
export type BuildPhase =
  | "loading_configuration"
  | "verifying_suffix_builder"
  | "discovering_corpus"
  | "validating_corpus"
  | "validating_artifacts"
  | "loading_artifacts"
  | "reading_files"
  | "normalizing_records"
  | "building_suffix_array"
  | "building_block_summaries"
  | "writing_artifacts"
  | "checksumming_artifacts"
  | "publishing_generation"
  | "ready";

/** Which route through preparation is being taken. */
export type CacheMode =
  | "unknown"
  | "cold_build"
  | "warm_validation"
  | "warm_load"
  | "forced_rebuild"
  | "recovery";

export interface CompletedPhase {
  phase: BuildPhase;
  label: string;
  seconds: number;
}

export interface IndexStats {
  sentences: number;
  files: number;
  searchable_bytes: number;
  longest_sentence: number;
  suffix_positions: number;
  block_count: number;
  block_size: number;
  summary_width: number;
}

export interface BuildStatus {
  /** Increases by one per snapshot. Anything not newer is discarded. */
  sequence: number;
  state: BuildState;
  phase: BuildPhase;
  phase_label: string;
  detail: string;
  /**
   * Whether `current` and `total` mean anything for this phase. False means the
   * work cannot report its own progress — not that the server does not know.
   */
  determinate: boolean;
  current: number;
  total: number | null;
  /** Relative to the corpus root. Never a path on the server's disk. */
  current_file: string | null;
  files_done: number;
  files_total: number | null;
  sentences: number;
  bytes_done: number;
  bytes_total: number | null;
  completed_phases: CompletedPhase[];
  phase_elapsed_seconds: number;
  elapsed_seconds: number;
  cache_mode: CacheMode;
  /** The phases this route is expected to run, in order. */
  planned_phases: BuildPhase[];
  index: IndexStats | null;
  error_code: string | null;
  error_message: string | null;
  recovery_hint: string | null;
  can_retry: boolean;
}

/** How the caller should describe what is happening, in one word. */
export const CACHE_MODE_LABELS: Record<CacheMode, string> = {
  unknown: "Starting up",
  cold_build: "First build",
  warm_validation: "Checking the cache",
  warm_load: "Warm start",
  forced_rebuild: "Rebuilding",
  recovery: "Rebuilding after a bad cache",
};
