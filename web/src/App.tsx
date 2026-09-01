import { useCallback, useEffect, useRef, useState } from "react";
import { useBuildProgress } from "./build/useBuildProgress";
import { Wordmark } from "./components/Logo";
import { PreparationScreen } from "./components/PreparationScreen";
import { ResultList } from "./components/ResultList";
import { SearchField } from "./components/SearchField";
import { StatusPanel } from "./components/StatusPanel";
import { SystemStatus } from "./components/SystemStatus";
import { useCompletions } from "./hooks/useCompletions";
import { useHealth } from "./hooks/useHealth";
import type { Completion } from "./types";

export default function App() {
  const [query, setQuery] = useState("");
  const { state: health, recheck } = useHealth();
  const { state: build, retry: retryBuild } = useBuildProgress();
  const { state: search, search: searchNow, searchSoon, reset } = useCompletions();

  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLOListElement>(null);

  const backendReady = health.kind === "ready";

  // While the server is preparing, the whole page is the preparation screen.
  // It is shown only on the server's own word: the state below comes from
  // /api/build, so nothing appears before there is something real to report,
  // and a warm start passes through it too quickly to be seen rather than
  // being held open to show it off.
  const preparing =
    build.kind === "watching" &&
    (build.status.state === "preparing" || build.status.state === "failed");

  // The page can now be open before the server is ready, and can watch a failed
  // preparation being retried, so the health check may be holding an answer
  // that has since stopped being true. When preparation reports readiness, ask
  // again: without this the page would say the system is ready and that the
  // index could not be prepared, at the same time.
  const buildReady = build.kind === "watching" && build.status.state === "ready";
  const healthStale = health.kind === "offline" || health.kind === "failed";
  useEffect(() => {
    if (buildReady && healthStale) recheck();
  }, [buildReady, healthStale, recheck]);
  // Before the first search the page is one centred prompt; afterwards the
  // field moves up and the sentences take the space.
  const working =
    search.kind === "results" || search.kind === "empty" || search.kind === "searching";

  const change = useCallback(
    (value: string) => {
      setQuery(value);
      if (backendReady) searchSoon(value);
    },
    [backendReady, searchSoon],
  );

  const submit = useCallback(() => {
    if (backendReady) searchNow(query);
  }, [backendReady, query, searchNow]);

  const clear = useCallback(() => {
    setQuery("");
    reset();
    inputRef.current?.focus();
  }, [reset]);

  const accept = useCallback(
    (result: Completion) => {
      setQuery(result.completed_sentence);
      inputRef.current?.focus();
      searchNow(result.completed_sentence);
    },
    [searchNow],
  );

  const focusFirstResult = useCallback(() => {
    listRef.current?.querySelector<HTMLButtonElement>("button[data-result]")?.focus();
  }, []);

  const focusInput = useCallback(() => inputRef.current?.focus(), []);

  // Escape anywhere returns to the box, which is where typing continues.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && document.activeElement !== inputRef.current) {
        focusInput();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focusInput]);

  const corpusSize =
    backendReady && health.health.sentences
      ? `${health.health.sentences.toLocaleString()} sentences from ${health.health.sources?.toLocaleString()} files`
      : null;

  if (preparing && build.kind === "watching") {
    return <PreparationScreen status={build.status} onRetry={retryBuild} />;
  }

  return (
    <div className="min-h-dvh bg-page">
      <a
        href="#query"
        className="sr-only rounded-lg bg-accent-blue px-4 py-2 text-white focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-10"
      >
        Skip to search
      </a>

      <div
        className={`mx-auto w-full max-w-2xl px-6 ${
          working ? "pt-8 pb-24" : "flex min-h-dvh flex-col justify-center pb-32"
        }`}
      >
        <header className={working ? "mb-6" : "mb-10 flex flex-col items-center"}>
          <Wordmark compact={working} />
          {!working && (
            <p className="mt-5 text-[15px] text-ink-soft">
              Find a sentence from a fragment of it.
            </p>
          )}
        </header>

        <SearchField
          ref={inputRef}
          value={query}
          onChange={change}
          onSubmit={submit}
          onClear={clear}
          onArrowIntoResults={focusFirstResult}
          busy={search.kind === "searching"}
          disabled={!backendReady && health.kind !== "checking"}
        />

        {/* The one line of guidance, shown only where there is nothing else to
            read, and doubling as the field's description everywhere. */}
        <p
          id="query-hint"
          className={
            working
              ? "sr-only"
              : "mt-5 text-center text-sm text-ink-faint"
          }
        >
          One mistyped character is fine.
          {corpusSize && !working ? ` Searching ${corpusSize}.` : ""}
        </p>

        {/* Everything that changes without the user acting is announced here. */}
        <p aria-live="polite" className="sr-only">
          {health.kind === "preparing" && "Preparing the search index."}
          {health.kind === "offline" && "The search service is not reachable."}
          {search.kind === "searching" && "Searching."}
          {search.kind === "results" &&
            `${search.results.length} suggestion${search.results.length === 1 ? "" : "s"}.`}
          {search.kind === "empty" && "No suggestions found."}
          {search.kind === "error" && "Something went wrong."}
        </p>

        {health.kind === "preparing" && (
          <StatusPanel
            busy
            title="Getting the corpus ready"
            detail="Reading the text files and preparing the index. This happens once, and searching starts on its own."
          />
        )}

        {health.kind === "offline" && (
          <StatusPanel
            tone="error"
            title="The search service is not running"
            detail="Start it with ./run.sh, or with uvicorn autocomplete.web:create_app --factory --port 8000"
            action={{ label: "Try again", onClick: recheck }}
          />
        )}

        {health.kind === "failed" && (
          <StatusPanel
            tone="error"
            title="The index could not be prepared"
            detail={health.detail}
            action={{ label: "Try again", onClick: recheck }}
          />
        )}

        {backendReady && (
          <>
            {search.kind === "results" && (
              <ResultList
                ref={listRef}
                results={search.results}
                onAccept={accept}
                onLeaveTop={focusInput}
              />
            )}

            {search.kind === "empty" && (
              <StatusPanel
                title="No suggestions"
                detail="Nothing matches that, even allowing for one mistyped character. Try a shorter fragment."
              />
            )}

            {search.kind === "error" && (
              <StatusPanel
                tone="error"
                title="Something went wrong"
                detail={search.message}
                action={{ label: "Try again", onClick: submit }}
              />
            )}

            {search.kind === "offline" && (
              <StatusPanel
                tone="error"
                title="The search service stopped responding"
                detail="It may have been shut down. Start it again and retry."
                action={{ label: "Try again", onClick: submit }}
              />
            )}

            {search.kind === "unavailable" && (
              <StatusPanel
                tone="warning"
                title="The index is not ready"
                detail={search.message}
                action={{ label: "Try again", onClick: submit }}
              />
            )}
          </>
        )}

        {/* How the system started, once it has. Closed by default: after
            readiness this is something to be able to check, not something to
            keep occupying the screen. */}
        {build.kind === "watching" && <SystemStatus status={build.status} />}
      </div>
    </div>
  );
}
