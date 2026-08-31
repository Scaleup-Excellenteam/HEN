/** One suggestion, exactly as the Python engine produces it. */
export interface Completion {
  completed_sentence: string;
  source_text: string;
  offset: number;
  score: number;
}

export interface CompletionsResponse {
  query: string;
  count: number;
  results: Completion[];
}

export type BackendStatus = "preparing" | "ready" | "failed";

export interface Health {
  status: BackendStatus;
  ready: boolean;
  detail: string;
  sentences?: number | null;
  sources?: number | null;
}
