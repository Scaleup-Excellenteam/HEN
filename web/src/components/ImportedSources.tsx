import { useId, useState } from "react";
import { formatBytes, withoutPrefix } from "../drive/api";
import type { DriveView } from "../drive/useDrive";
import type { ImportedDocument } from "../drive/types";

/**
 * Managing the documents imported from Google Drive.
 *
 * A disclosure rather than a dialog: it opens in place, under the search field,
 * so nothing is covered and there is no focus trap to get wrong. The button
 * carries `aria-expanded` and names the region it controls, which is all a
 * screen reader needs to understand it, and the region is a landmark so it can
 * be jumped to directly.
 *
 * The visual language is the project's own throughout: the same hairline
 * borders, the same four accents, the same rounded shapes as the search field
 * and the result rows. Google's name appears where it is a fact the reader
 * needs, and nowhere as decoration; none of Google's marks, colours or
 * letterforms are reproduced, and the only Google-designed surfaces the user
 * sees are Google's own sign-in and picker windows, which is where they belong.
 */

interface Props {
  drive: DriveView;
}

export function ImportedSources({ drive }: Props) {
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);
  const panelId = useId();

  const { status, documents, job, error, cancelled, connected, busy } = drive;

  // Nothing at all when the server does not offer the feature. A control that
  // cannot do anything is worse than no control.
  const offered = status?.enabled ?? false;

  if (!offered) return null;

  return (
    <div className="mt-6">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((was) => !was)}
        className="flex w-full items-center gap-2 rounded-xl border border-hairline px-4 py-2.5 text-left text-sm text-ink transition-colors hover:bg-page-sunken sm:w-auto"
      >
        <Chevron open={open} />
        <span className="font-medium">Imported sources</span>
        <span className="ml-auto text-ink-faint sm:ml-2">
          {documents.length === 0
            ? "none"
            : `${documents.length} document${documents.length === 1 ? "" : "s"}`}
        </span>
      </button>

      {open && (
        <section
          id={panelId}
          aria-label="Imported sources"
          className="mt-3 rounded-2xl border border-hairline p-4 sm:p-5"
        >
          {/* Everything that changes without the reader acting is announced. */}
          <p aria-live="polite" className="sr-only">
            {busy && job ? job.progress.detail : ""}
            {!busy && job?.state === "complete" ? job.progress.detail : ""}
            {error ? error.message : ""}
            {cancelled ? "Nothing was selected." : ""}
          </p>

          {!status?.configured ? (
            <Unconfigured detail={status?.detail ?? ""} />
          ) : (
            <>
              <Explanation limits={status.limits} />

              {status.load_error && (
                <Notice tone="error" title="The imported documents could not be loaded">
                  {status.load_error}
                </Notice>
              )}

              {busy && job && <Progress job={job} />}

              {!busy && error && (
                <Notice tone="error" title="That did not work">
                  {error.message}
                  <div className="mt-3 flex flex-wrap gap-2">
                    {error.retryable && job?.state === "failed" && (
                      <SmallButton onClick={() => void drive.retry()}>
                        Try again
                      </SmallButton>
                    )}
                    <SmallButton onClick={drive.dismissError}>Dismiss</SmallButton>
                  </div>
                </Notice>
              )}

              {!busy && !error && cancelled && (
                <Notice tone="neutral" title="Nothing was selected">
                  The Google window closed without any documents being chosen.
                  Nothing was imported and nothing changed.
                </Notice>
              )}

              {!busy && !error && job?.state === "failed" && (
                <Notice tone="error" title="The last change did not finish">
                  {job.error?.message ??
                    "It stopped before finishing. Everything imported before is still searchable."}
                  {job.error?.retryable && (
                    <div className="mt-3">
                      <SmallButton onClick={() => void drive.retry()}>
                        Try again
                      </SmallButton>
                    </div>
                  )}
                </Notice>
              )}

              <div className="mt-4 flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void drive.addFiles()}
                  className="h-11 rounded-full bg-accent-blue px-5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {connected ? "Add from Google Drive" : "Connect Google Drive"}
                </button>
                {connected && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={drive.disconnect}
                    className="h-11 rounded-full border border-hairline px-5 text-sm font-medium text-ink transition-colors hover:bg-page-sunken disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Disconnect
                  </button>
                )}
              </div>

              <DocumentTable
                documents={documents}
                prefix={status.source_prefix}
                busy={busy}
                confirming={confirming}
                onAskRemove={setConfirming}
                onCancelRemove={() => setConfirming(null)}
                // Closed as the removal starts, rather than when it finishes,
                // so no confirmation is left hanging over a row that has gone.
                onConfirmRemove={(id) => {
                  setConfirming(null);
                  void drive.remove(id);
                }}
              />
            </>
          )}
        </section>
      )}
    </div>
  );
}

function Unconfigured({ detail }: { detail: string }) {
  return (
    <div className="text-sm">
      <p className="font-medium text-ink">Not configured on this server</p>
      <p className="mt-2 leading-relaxed text-ink-soft">
        {detail || "Google Drive import is switched off."}
      </p>
      <p className="mt-2 leading-relaxed text-ink-faint">
        Searching the corpus works exactly as it does without this feature.
      </p>
    </div>
  );
}

function Explanation({ limits }: { limits: { max_files: number; max_file_bytes: number } }) {
  return (
    <div className="text-sm leading-relaxed text-ink-soft">
      <p>
        Add plain text files and Google Docs from your Drive, and their lines
        become searchable alongside the corpus.
      </p>
      <p className="mt-2 text-ink-faint">
        {`Only the documents you pick are ever read. This app asks Google for access to those files and nothing else, and cannot list or search the rest of your Drive. Up to ${limits.max_files} at a time, ${formatBytes(limits.max_file_bytes)} each.`}
      </p>
    </div>
  );
}

function Progress({ job }: { job: NonNullable<DriveView["job"]> }) {
  const { progress } = job;
  return (
    <div className="mt-4 rounded-xl bg-page-sunken p-4">
      <p className="flex items-center gap-2.5 text-sm font-medium text-ink">
        <span
          className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-hairline border-t-accent-blue"
          aria-hidden="true"
        />
        {job.progress.detail || "Working."}
      </p>
      {/* Counts, never a percentage: the cost of indexing is not known until it
          is done, so any bar would be invented. */}
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-ink-soft sm:grid-cols-4">
        <Count label="selected" value={progress.files_selected} />
        <Count label="downloaded" value={progress.files_downloaded} />
        <Count label="lines read" value={progress.lines_read} />
        <Count
          label="searchable"
          value={progress.sentences_indexed}
          pending={progress.sentences_indexed === 0}
        />
      </dl>
    </div>
  );
}

function Count({
  label,
  value,
  pending = false,
}: {
  label: string;
  value: number;
  pending?: boolean;
}) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="order-2 text-ink-faint">{label}</dt>
      <dd className="order-1 font-medium tabular-nums text-ink">
        {pending ? "—" : value.toLocaleString()}
      </dd>
    </div>
  );
}

function DocumentTable({
  documents,
  prefix,
  busy,
  confirming,
  onAskRemove,
  onCancelRemove,
  onConfirmRemove,
}: {
  documents: ImportedDocument[];
  prefix: string;
  busy: boolean;
  confirming: string | null;
  onAskRemove: (id: string) => void;
  onCancelRemove: () => void;
  onConfirmRemove: (id: string) => void;
}) {
  if (documents.length === 0) {
    return (
      <p className="mt-5 border-t border-hairline pt-5 text-sm text-ink-faint">
        No documents imported yet. Searches cover the corpus only.
      </p>
    );
  }

  return (
    <ul className="mt-5 space-y-0.5 border-t border-hairline pt-2">
      {documents.map((document) => (
        <li key={document.id} className="rounded-xl px-1 py-2.5">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <p className="min-w-0 flex-1 break-words text-[15px] text-ink">
              {document.name}
            </p>
            {confirming === document.id ? (
              <span className="flex gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => onConfirmRemove(document.id)}
                  className="h-9 rounded-full bg-accent-red px-4 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
                >
                  Remove it
                </button>
                <button
                  type="button"
                  onClick={onCancelRemove}
                  className="h-9 rounded-full border border-hairline px-4 text-xs font-medium text-ink transition-colors hover:bg-page-sunken"
                >
                  Keep it
                </button>
              </span>
            ) : (
              <button
                type="button"
                disabled={busy}
                onClick={() => onAskRemove(document.id)}
                className="h-9 shrink-0 rounded-full border border-hairline px-4 text-xs font-medium text-ink transition-colors hover:bg-page-sunken disabled:cursor-not-allowed disabled:opacity-50"
              >
                {/* The label is one string rather than a visible word beside a
                    hidden phrase: the accessible name is built by joining each
                    child's trimmed text, so the two would be announced run
                    together as one word. */}
                <span aria-hidden="true">Remove</span>
                <span className="sr-only">{`Remove ${document.name} from the search`}</span>
              </button>
            )}
          </div>

          <p className="mt-1 flex flex-wrap items-baseline gap-x-3 text-xs text-ink-faint">
            {/* The path is worth showing only where it differs from the name:
                a Google Doc gains a .txt suffix, and a second document of the
                same name gains a counter. Repeating an identical string twice
                would be noise in the page and in a screen reader. */}
            {searchedAs(document, prefix) && (
              <span className="font-mono">{searchedAs(document, prefix)}</span>
            )}
            <span className="tabular-nums">{countOfSentences(document.sentences)}</span>
            <span className="tabular-nums">{formatBytes(document.bytes)}</span>
          </p>

          {confirming === document.id && (
            <p className="mt-2 text-xs leading-relaxed text-accent-red">
              {`Removing this takes its ${countOfSentences(
                document.sentences,
              )} out of search results. The file in your Google Drive is not touched.`}
            </p>
          )}
        </li>
      ))}
    </ul>
  );
}

/** The name results report this document under, when it is not simply its name. */
function searchedAs(document: ImportedDocument, prefix: string): string {
  const shown = withoutPrefix(document.source_text, prefix);
  return shown === document.name ? "" : shown;
}


/** "1 sentence" or "12 sentences", as one string rather than three nodes. */
function countOfSentences(count: number): string {
  return `${count.toLocaleString()} sentence${count === 1 ? "" : "s"}`;
}


function Notice({
  tone,
  title,
  children,
}: {
  tone: "neutral" | "error";
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div
      role={tone === "error" ? "alert" : undefined}
      className={`mt-4 rounded-xl border p-4 text-sm ${
        tone === "error"
          ? "border-accent-red/30 bg-accent-red/5"
          : "border-hairline bg-page-sunken"
      }`}
    >
      <p className={`font-medium ${tone === "error" ? "text-accent-red" : "text-ink"}`}>
        {title}
      </p>
      <div className="mt-1.5 leading-relaxed text-ink-soft">{children}</div>
    </div>
  );
}

function SmallButton({
  onClick,
  children,
}: {
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="h-9 rounded-full border border-hairline bg-page px-4 text-xs font-medium text-ink transition-colors hover:bg-page-sunken"
    >
      {children}
    </button>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={`h-4 w-4 shrink-0 text-ink-faint transition-transform ${
        open ? "rotate-90" : ""
      }`}
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="m9 6 6 6-6 6" />
    </svg>
  );
}
