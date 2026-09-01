/**
 * The shapes the Drive import endpoints return.
 *
 * These mirror the Pydantic models in `autocomplete/web/drive_api.py`. Nothing
 * here is a secret: `client_id`, `api_key` and `app_id` are the public
 * configuration Google expects a browser to hold, and they are served by the
 * API rather than compiled into this bundle so a deployment can change them
 * without a rebuild.
 */

/** What the server is doing. */
export type DriveServerState =
  | "disabled"
  | "ready"
  | "downloading"
  | "building"
  | "adopting"
  | "failed";

/**
 * What the person sees, which is the server's state plus one the server cannot
 * know: whether this browser currently holds a Google authorization.
 */
export type DriveState = DriveServerState | "disconnected";

export interface DriveLimits {
  max_files: number;
  max_file_bytes: number;
  max_total_bytes: number;
  supported_mime_types: string[];
}

export interface JobProgress {
  files_selected: number;
  files_downloaded: number;
  files_reused: number;
  bytes_downloaded: number;
  /** Counted while downloading, so it is real before indexing starts. */
  lines_read: number;
  /** Known only once the index exists; zero until then. */
  sentences_indexed: number;
  detail: string;
}

export interface JobError {
  code: string;
  message: string;
  retryable: boolean;
}

export interface JobStatus {
  id: string;
  kind: "import" | "remove";
  state: "pending" | "downloading" | "building" | "adopting" | "complete" | "failed";
  progress: JobProgress;
  error: JobError | null;
  started_at: string;
  finished_at: string | null;
  needs_authorization: boolean;
}

export interface DriveStatus {
  enabled: boolean;
  configured: boolean;
  state: DriveServerState;
  detail: string;
  client_id: string;
  api_key: string;
  app_id: string;
  scope: string;
  /** The `source_text` namespace imported sentences appear under. */
  source_prefix: string;
  limits: DriveLimits;
  documents: number;
  sentences: number;
  total_bytes: number;
  job: JobStatus | null;
  load_error: string | null;
}

export interface ImportedDocument {
  id: string;
  name: string;
  mime_type: string;
  source_text: string;
  imported_at: string;
  modified_time: string | null;
  bytes: number;
  sentences: number;
  status: string;
}

export interface DocumentList {
  count: number;
  total_bytes: number;
  documents: ImportedDocument[];
}
