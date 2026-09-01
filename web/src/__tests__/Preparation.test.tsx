import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { PreparationScreen } from "../components/PreparationScreen";
import {
  COLD_PLAN,
  buildStatus,
  buildingStatus,
  completion,
  mockApi,
} from "../test/harness";
import type { BuildStatus } from "../build/types";

/**
 * The preparation screen, and the transition out of it.
 *
 * Two things are tested throughout. That every number shown came from the
 * server, so nothing here advances on its own; and that a snapshot older than
 * the one already held is discarded, so a reconnect that replays part of the
 * history cannot move the screen backwards.
 */

/** Stands in for EventSource, which jsdom does not provide. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readonly url: string;
  closed = false;
  private listeners: Record<string, ((event: Event) => void)[]> = {};

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, handler: (event: Event) => void) {
    (this.listeners[type] ??= []).push(handler);
  }

  close() {
    this.closed = true;
  }

  /** Deliver one snapshot, as the server would. */
  send(status: BuildStatus) {
    const event = new MessageEvent("progress", { data: JSON.stringify(status) });
    for (const handler of this.listeners["progress"] ?? []) handler(event);
  }

  /** Deliver something that is not a snapshot. */
  sendRaw(data: string) {
    const event = new MessageEvent("progress", { data });
    for (const handler of this.listeners["progress"] ?? []) handler(event);
  }

  /** Drop the connection, as a proxy or a restart would. */
  fail() {
    for (const handler of this.listeners["error"] ?? []) handler(new Event("error"));
  }

  static latest() {
    return FakeEventSource.instances[FakeEventSource.instances.length - 1];
  }
}

function useFakeEventSource() {
  FakeEventSource.instances = [];
  vi.stubGlobal(
    "EventSource",
    FakeEventSource as unknown as typeof globalThis.EventSource,
  );
}

beforeEach(() => {
  FakeEventSource.instances = [];
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** Wait until the app has left the preparation screen. */
async function searchable() {
  return screen.findByRole("searchbox");
}

describe("the preparation screen", () => {
  it("appears while the server is preparing", async () => {
    mockApi({ build: buildingStatus() });
    render(<App />);
    expect(await screen.findByText("Preparing mission data")).toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).toBeNull();
  });

  it("shows the phase the server reported", async () => {
    mockApi({ build: buildingStatus() });
    render(<App />);
    const active = within(
      await screen.findByRole("region", { name: /current phase/i }),
    );
    expect(active.getByRole("heading", { level: 2 })).toHaveTextContent(
      "Reading corpus files",
    );
    expect(active.getByText("Reading 1,504 corpus files.")).toBeInTheDocument();
  });

  it("shows the file being read, relative to the corpus root", async () => {
    mockApi({ build: buildingStatus({ current_file: "rfc/rfc7707.txt" }) });
    render(<App />);
    expect(await screen.findByText("rfc/rfc7707.txt")).toBeInTheDocument();
  });

  it("shows the counters the server measured", async () => {
    mockApi({ build: buildingStatus() });
    render(<App />);
    expect(await screen.findByText("812 / 1,504")).toBeInTheDocument();
    expect(screen.getByText("1,204,881")).toBeInTheDocument();
    expect(screen.getByText("48.8 MB")).toBeInTheDocument();
  });

  it("names which route the server is taking", async () => {
    mockApi({ build: buildingStatus() });
    render(<App />);
    expect(await screen.findByText("First build")).toBeInTheDocument();
  });

  it("shows a warm start as a warm start", async () => {
    mockApi({
      build: buildingStatus({
        cache_mode: "warm_validation",
        phase: "validating_artifacts",
        phase_label: "Validating cached artifacts",
      }),
    });
    render(<App />);
    expect(await screen.findByText("Checking the cache")).toBeInTheDocument();
  });
});

describe("progress bars", () => {
  it("a determinate phase reports its position", async () => {
    mockApi({ build: buildingStatus({ current: 812, total: 1504 }) });
    render(<App />);
    const bar = await screen.findByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "54");
    expect(bar).toHaveAttribute("aria-valuemin", "0");
    expect(bar).toHaveAttribute("aria-valuemax", "100");
    expect(screen.getByText("812 of 1,504 — 54%")).toBeInTheDocument();
  });

  it("an indeterminate phase claims no position at all", async () => {
    mockApi({
      build: buildingStatus({
        phase: "building_suffix_array",
        phase_label: "Building the suffix array",
        determinate: false,
        total: null,
        current: 0,
      }),
    });
    render(<App />);
    const bar = await screen.findByRole("progressbar");
    expect(bar).not.toHaveAttribute("aria-valuenow");
    expect(
      screen.getByText("Working. This step cannot report how much is left."),
    ).toBeInTheDocument();
  });

  it("never shows an estimated time remaining", async () => {
    mockApi({ build: buildingStatus() });
    render(<App />);
    await screen.findByText("Preparing mission data");
    const text = document.body.textContent ?? "";
    for (const forbidden of ["remaining", "ETA", "eta", "estimated", "left)"]) {
      expect(text).not.toContain(forbidden);
    }
  });

  it("shows elapsed time, which is measured rather than predicted", async () => {
    mockApi({ build: buildingStatus({ elapsed_seconds: 4.1 }) });
    render(<App />);
    expect(await screen.findByText("4.1 s elapsed")).toBeInTheDocument();
  });
});

describe("the phase tracker", () => {
  it("lists the phases this route will run", async () => {
    mockApi({ build: buildingStatus() });
    render(<App />);
    const list = within(
      await screen.findByRole("region", { name: /preparation phases/i }),
    );
    expect(list.getByText("Building the suffix array")).toBeInTheDocument();
    expect(list.getByText("Publishing the index")).toBeInTheDocument();
  });

  it("does not list building phases on a warm route", async () => {
    mockApi({
      build: buildingStatus({
        cache_mode: "warm_validation",
        phase: "validating_artifacts",
        planned_phases: [
          "loading_configuration",
          "verifying_suffix_builder",
          "validating_corpus",
          "validating_artifacts",
          "loading_artifacts",
          "ready",
        ],
      }),
    });
    render(<App />);
    const list = within(
      await screen.findByRole("region", { name: /preparation phases/i }),
    );
    expect(list.queryByText("Building the suffix array")).toBeNull();
    expect(list.getByText("Loading the cached index")).toBeInTheDocument();
  });

  it("shows how long each completed phase took", async () => {
    mockApi({ build: buildingStatus() });
    render(<App />);
    const list = within(
      await screen.findByRole("region", { name: /preparation phases/i }),
    );
    expect(list.getByText("90 ms")).toBeInTheDocument();
  });

  it("marks a phase the route skipped rather than pretending it ran", async () => {
    mockApi({
      build: buildingStatus({
        phase: "building_suffix_array",
        phase_label: "Building the suffix array",
        // normalizing_records is behind the active phase and never completed.
        completed_phases: [
          { phase: "reading_files", label: "Reading corpus files", seconds: 6.1 },
        ],
        planned_phases: COLD_PLAN,
      }),
    });
    render(<App />);
    expect((await screen.findAllByText("not needed")).length).toBeGreaterThan(0);
  });
});

describe("live updates", () => {
  it("applies snapshots as the stream delivers them", async () => {
    useFakeEventSource();
    mockApi({ build: buildingStatus({ sequence: 1, current: 100 }) });
    render(<App />);
    await screen.findByText("Preparing mission data");
    await waitFor(() => expect(FakeEventSource.latest()).toBeDefined());

    FakeEventSource.latest().send(
      buildingStatus({
        sequence: 2,
        current: 900,
        total: 1504,
        current_file: "later/file.txt",
      }),
    );
    expect(await screen.findByText("later/file.txt")).toBeInTheDocument();
  });

  it("ignores a snapshot older than the one it holds", async () => {
    useFakeEventSource();
    mockApi({ build: buildingStatus({ sequence: 10, current_file: "current.txt" }) });
    render(<App />);
    await screen.findByText("current.txt");
    await waitFor(() => expect(FakeEventSource.latest()).toBeDefined());

    FakeEventSource.latest().send(
      buildingStatus({ sequence: 3, current_file: "stale.txt" }),
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByText("current.txt")).toBeInTheDocument();
    expect(screen.queryByText("stale.txt")).toBeNull();
  });

  it("ignores a repeated snapshot", async () => {
    useFakeEventSource();
    mockApi({ build: buildingStatus({ sequence: 5, current_file: "one.txt" }) });
    render(<App />);
    await screen.findByText("one.txt");
    await waitFor(() => expect(FakeEventSource.latest()).toBeDefined());

    FakeEventSource.latest().send(
      buildingStatus({ sequence: 5, current_file: "other.txt" }),
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByText("one.txt")).toBeInTheDocument();
  });

  it("survives a frame that is not a snapshot", async () => {
    useFakeEventSource();
    mockApi({ build: buildingStatus({ current_file: "kept.txt" }) });
    render(<App />);
    await screen.findByText("kept.txt");
    await waitFor(() => expect(FakeEventSource.latest()).toBeDefined());

    FakeEventSource.latest().sendRaw("not json at all");
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByText("kept.txt")).toBeInTheDocument();
  });

  it("reopens a stream that drops mid-build", async () => {
    useFakeEventSource();
    mockApi({ build: buildingStatus() });
    render(<App />);
    await screen.findByText("Preparing mission data");
    await waitFor(() => expect(FakeEventSource.latest()).toBeDefined());
    const first = FakeEventSource.latest();

    first.fail();
    await waitFor(
      () => expect(FakeEventSource.instances.length).toBeGreaterThan(1),
      { timeout: 3000 },
    );
    expect(first.closed).toBe(true);
  });

  it("closes the stream once preparation is over", async () => {
    useFakeEventSource();
    mockApi({ build: buildingStatus() });
    render(<App />);
    await screen.findByText("Preparing mission data");
    await waitFor(() => expect(FakeEventSource.latest()).toBeDefined());
    const source = FakeEventSource.latest();

    source.send(buildStatus({ sequence: 999 }));
    await searchable();
    expect(source.closed).toBe(true);
  });

  it("polls when the browser has no EventSource", async () => {
    // Stated rather than assumed: whether the test environment provides one is
    // not this test's subject, and the fallback is.
    vi.stubGlobal("EventSource", undefined);
    const { buildCalls } = mockApi({
      buildSequence: [
        buildingStatus({ sequence: 1, current_file: "first.txt" }),
        buildingStatus({ sequence: 2, current_file: "second.txt" }),
      ],
    });
    render(<App />);

    // The second snapshot can only have arrived by asking again.
    expect(await screen.findByText("second.txt")).toBeInTheDocument();
    expect(
      buildCalls.filter((call) => call.includes("/status")).length,
    ).toBeGreaterThan(1);
  });
});

describe("becoming ready", () => {
  it("replaces the preparation screen with the search interface", async () => {
    mockApi({ build: buildStatus() });
    render(<App />);
    expect(await searchable()).toBeInTheDocument();
    expect(screen.queryByText("Preparing mission data")).toBeNull();
  });

  it("search works once ready", async () => {
    mockApi({ build: buildStatus(), results: [completion()] });
    const user = userEvent.setup();
    render(<App />);
    await user.type(await searchable(), "this is");
    expect(await screen.findByText("Alpha: this is a demo.")).toBeInTheDocument();
  });

  it("transitions on its own when the stream reports readiness", async () => {
    useFakeEventSource();
    mockApi({ build: buildingStatus() });
    render(<App />);
    await screen.findByText("Preparing mission data");
    await waitFor(() => expect(FakeEventSource.latest()).toBeDefined());

    FakeEventSource.latest().send(buildStatus({ sequence: 500 }));
    expect(await searchable()).toBeInTheDocument();
  });
});

describe("the system status view", () => {
  it("is one quiet line until it is opened", async () => {
    mockApi({ build: buildStatus() });
    render(<App />);
    const toggle = await screen.findByRole("button", { name: /system ready/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Searchable text")).toBeNull();
  });

  it("reports how the system started", async () => {
    mockApi({ build: buildStatus() });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: /system ready/i }));

    expect(screen.getByText("Searchable text")).toBeInTheDocument();
    expect(screen.getByText("94.2 MB")).toBeInTheDocument();
    expect(screen.getByText("Warm start")).toBeInTheDocument();
    expect(screen.getByText("420 ms")).toBeInTheDocument();
    expect(screen.getByText("1,504")).toBeInTheDocument();
  });

  it("is absent while preparation is still running", async () => {
    mockApi({ build: buildingStatus() });
    render(<App />);
    await screen.findByText("Preparing mission data");
    expect(screen.queryByRole("button", { name: /system ready/i })).toBeNull();
  });
});

describe("failure", () => {
  const failure = () =>
    buildStatus({
      sequence: 20,
      state: "failed",
      phase: "validating_corpus",
      phase_label: "Fingerprinting the corpus",
      error_code: "corpus_missing",
      error_message: "The corpus directory was not found.",
      recovery_hint: "Set corpus_root in config.yaml to the text files.",
      can_retry: true,
      index: null,
      completed_phases: [
        { phase: "loading_configuration", label: "Loading configuration", seconds: 0.01 },
      ],
    });

  it("says what happened and where it stopped", async () => {
    mockApi({ build: failure() });
    render(<App />);
    expect(await screen.findByText("Preparation stopped")).toBeInTheDocument();
    expect(
      screen.getByText("The corpus directory was not found."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Stopped during: Fingerprinting the corpus"),
    ).toBeInTheDocument();
  });

  it("suggests what to do about it", async () => {
    mockApi({ build: failure() });
    render(<App />);
    expect(
      await screen.findByText(/set corpus_root in config\.yaml/i),
    ).toBeInTheDocument();
  });

  it("announces the failure assertively", async () => {
    mockApi({ build: failure() });
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The corpus directory was not found.",
    );
  });

  it("offers what completed before it stopped", async () => {
    mockApi({ build: failure() });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByText(/what completed before it stopped/i));
    expect(screen.getByText("Loading configuration")).toBeInTheDocument();
  });

  it("retries, and can succeed", async () => {
    const { buildCalls } = mockApi({
      buildSequence: [failure(), failure()],
      retryStatus: buildingStatus({ sequence: 30 }),
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: /try again/i }));

    await waitFor(() =>
      expect(buildCalls.some((call) => call.startsWith("POST /api/build/retry"))).toBe(
        true,
      ),
    );
    expect(await screen.findByText("Preparing mission data")).toBeInTheDocument();
  });

  it("offers no retry when the server says it cannot be retried", async () => {
    mockApi({ build: failure() && { ...failure(), can_retry: false } });
    render(<App />);
    await screen.findByText("Preparation stopped");
    expect(screen.queryByRole("button", { name: /try again/i })).toBeNull();
  });

  it("never renders a path or a traceback", async () => {
    mockApi({ build: failure() });
    render(<App />);
    await screen.findByText("Preparation stopped");
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("Traceback");
    expect(text).not.toMatch(/\/(Users|home)\//);
  });
});

describe("recovering from a server that was not up yet", () => {
  it("re-checks health after a failed preparation is retried", async () => {
    // Health answered "failed" while preparation was failing, and holds it: it
    // only polls while preparing, so nothing would refresh it on its own.
    let healthy = false;
    const failed = buildStatus({
      state: "failed",
      error_message: "It stopped.",
      can_retry: true,
      index: null,
    });
    const original = mockApi({
      buildSequence: [failed, buildStatus({ sequence: 900 })],
      retryStatus: buildingStatus({ sequence: 800 }),
    });
    const previous = original.fetchMock.getMockImplementation();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).startsWith("/api/health")) {
          return new Response(
            JSON.stringify(
              healthy
                ? { status: "ready", ready: true, detail: "Ready to search." }
                : {
                    status: "failed",
                    ready: false,
                    detail: "The search index could not be prepared.",
                  },
            ),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return previous!(input, init);
      }),
    );

    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: /try again/i }));

    // The retry succeeds, and the next look at the build finds it ready.
    healthy = true;

    await searchable();
    await waitFor(() =>
      expect(screen.queryByText(/the index could not be prepared/i)).toBeNull(),
    );
  });

  it("re-checks health once preparation reports readiness", async () => {
    useFakeEventSource();
    // The page opens before the server does: health fails, and the build
    // stream is still trying.
    let healthUp = false;
    const original = mockApi({ build: buildingStatus() });
    const previous = original.fetchMock.getMockImplementation();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).startsWith("/api/health") && !healthUp) {
          throw new TypeError("Failed to fetch");
        }
        return previous!(input, init);
      }),
    );

    render(<App />);
    await screen.findByText("Preparing mission data");
    await waitFor(() => expect(FakeEventSource.latest()).toBeDefined());

    // The server finishes preparing and is now answering.
    healthUp = true;
    FakeEventSource.latest().send(buildStatus({ sequence: 900 }));

    // Without the re-check the page would say the system is ready and that the
    // service is not running, at the same time.
    await searchable();
    await waitFor(() =>
      expect(screen.queryByText(/the search service is not running/i)).toBeNull(),
    );
  });
});

describe("the server being unreachable", () => {
  it("does not claim the build failed", async () => {
    mockApi({ failBuild: true });
    render(<App />);
    // Nothing is known, so nothing is asserted about the build: the page falls
    // through to the search interface, which reports the service being down.
    await waitFor(() =>
      expect(screen.queryByText("Preparing mission data")).toBeNull(),
    );
    expect(screen.queryByText("Preparation stopped")).toBeNull();
  });
});

describe("accessibility", () => {
  it("announces phase changes politely, and not every file", async () => {
    const { rerender } = render(
      <PreparationScreen status={buildingStatus()} onRetry={() => {}} />,
    );
    const live = document.querySelector("[aria-live='polite']");
    expect(live?.textContent).toContain("Reading corpus files");

    // The file changes; the announcement is about the phase, not the file.
    rerender(
      <PreparationScreen
        status={buildingStatus({ current_file: "another/file.txt" })}
        onRetry={() => {}}
      />,
    );
    expect(live?.textContent).not.toContain("another/file.txt");
  });

  it("labels the progress bar with what it is measuring", async () => {
    render(<PreparationScreen status={buildingStatus()} onRetry={() => {}} />);
    expect(
      screen.getByRole("progressbar", { name: /reading corpus files: 812 of 1504/i }),
    ).toBeInTheDocument();
  });

  it("labels an indeterminate bar as unable to report a position", async () => {
    render(
      <PreparationScreen
        status={buildingStatus({ determinate: false, total: null })}
        onRetry={() => {}}
      />,
    );
    expect(
      screen.getByRole("progressbar", { name: /cannot report how much is left/i }),
    ).toBeInTheDocument();
  });

  it("gives the phases and the current phase their own landmarks", async () => {
    render(<PreparationScreen status={buildingStatus()} onRetry={() => {}} />);
    expect(
      screen.getByRole("region", { name: /current phase/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: /preparation phases/i }),
    ).toBeInTheDocument();
  });

  it("carries every state in words as well as colour", async () => {
    render(
      <PreparationScreen
        status={buildingStatus({ determinate: false, total: null })}
        onRetry={() => {}}
      />,
    );
    expect(
      screen.getByText("Working. This step cannot report how much is left."),
    ).toBeInTheDocument();
  });

  it("the retry button is reachable and operable from the keyboard", async () => {
    const retried = vi.fn();
    const user = userEvent.setup();
    render(
      <PreparationScreen
        status={buildStatus({
          state: "failed",
          error_message: "It stopped.",
          can_retry: true,
          index: null,
        })}
        onRetry={retried}
      />,
    );
    const button = screen.getByRole("button", { name: /try again/i });
    button.focus();
    expect(button).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(retried).toHaveBeenCalled();
  });

  it("renders a hostile file name as text and never as markup", async () => {
    render(
      <PreparationScreen
        status={buildingStatus({
          current_file: "<img src=x onerror=alert(1)>.txt",
        })}
        onRetry={() => {}}
      />,
    );
    expect(
      screen.getByText("<img src=x onerror=alert(1)>.txt"),
    ).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
  });

  it("keeps a very long path readable and available in full", async () => {
    const long = `${"deeply/nested/".repeat(8)}${"n".repeat(90)}.txt`;
    render(
      <PreparationScreen status={buildingStatus({ current_file: long })} onRetry={() => {}} />,
    );
    const shown = screen.getByText(long);
    // Wrapped rather than clipped, and carrying its whole value where a
    // pointer cannot reach it.
    expect(shown).toHaveClass("break-all");
    expect(shown).toHaveAttribute("title", long);
  });
});
