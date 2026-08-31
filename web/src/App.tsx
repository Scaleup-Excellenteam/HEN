import { useCallback, useEffect, useRef, useState } from "react";
import { Wordmark } from "./components/Logo";
import { ResultList } from "./components/ResultList";
import { SearchField } from "./components/SearchField";
import { StatusPanel } from "./components/StatusPanel";
import { useCompletions } from "./hooks/useCompletions";
import { useHealth } from "./hooks/useHealth";
import type { Completion } from "./types";

export default function App() {
  const [query, setQuery] = useState("");
  const { state: health, recheck } = useHealth();
  const { state: search, search: searchNow, searchSoon, reset } = useCompletions();

  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLOListElement>(null);

  const backendReady = health.kind === "ready";
  const hasResults = search.kind === "results";
  // Once a search has happened the page becomes a compact working view; before
  // that it is a single centred prompt.
  const settled = hasResults || search.kind === "empty" || search.kind === "searching";

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

  return (
    <div className="flex min-h-dvh flex-col bg-page">
      <a
        href="#query"
        className="sr-only rounded-lg bg-accent-blue px-4 py-2 text-white focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-10"
      >
        Skip to search
      </a>

      <header className="flex min-h-14 items-center justify-between px-5 py-4 sm:px-8">
        {/* Before a search the mark is the centred one below, so the header
            stays empty rather than showing it twice. */}
        {settled ? <Wordmark compact /> : <span />}
        <p className="text-xs text-ink-faint">
          {backendReady && health.health.sentences
            ? `${health.health.sentences.toLocaleString()} sentences indexed`
            : " "}
        </p>
      </header>

      <main
        className={`mx-auto w-full max-w-3xl flex-1 px-5 pb-16 sm:px-8 ${
          settled ? "pt-2" : "flex flex-col justify-center pt-4 sm:-mt-20"
        }`}
      >
        {!settled && (
          <div className="mb-10 flex flex-col items-center text-center">
            <Wordmark />
            <p className="mt-4 max-w-md text-balance text-base leading-relaxed text-ink-soft">
              Find a sentence from a fragment of it.
            </p>
          </div>
        )}

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
            tone="neutral"
            title="Getting the corpus ready"
            detail="Reading the text files and preparing the search index. This happens once; searching will start on its own."
          />
        )}

        {health.kind === "offline" && (
          <StatusPanel
            tone="error"
            title="The search service is not running"
            detail="Start it with: uvicorn autocomplete.web:create_app --factory --port 8000"
            action={{ label: "Try again", onClick: recheck }}
          />
        )}

        {health.kind === "failed" && (
          <StatusPanel
            tone="error"
            title="The search index could not be prepared"
            detail={health.detail}
            action={{ label: "Try again", onClick: recheck }}
          />
        )}

        {backendReady && (
          <>
            {search.kind === "results" && (
              <>
                <p className="mt-8 px-3 text-xs text-ink-faint sm:px-4">
                  {search.results.length} suggestion
                  {search.results.length === 1 ? "" : "s"}
                </p>
                <ResultList
                  ref={listRef}
                  results={search.results}
                  onAccept={accept}
                  onLeaveTop={focusInput}
                />
              </>
            )}

            {search.kind === "empty" && (
              <StatusPanel
                tone="neutral"
                title="No suggestions"
                detail="Nothing in the corpus matches that, even allowing for one mistyped character. Try a shorter fragment."
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
      </main>

      <footer className="px-5 pb-8 text-center text-xs leading-relaxed text-ink-faint sm:px-8">
        An optional browser interface over the project's command-line
        autocomplete. Results, scores and ordering come from the same engine.
      </footer>
    </div>
  );
}
