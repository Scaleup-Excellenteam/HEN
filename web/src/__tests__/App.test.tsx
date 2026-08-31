import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { PREPARING, READY, completion, fiveCompletions, mockApi } from "../test/harness";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function ready() {
  await screen.findByRole("searchbox", { name: /text to complete/i });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: /^search$/i })).toBeEnabled(),
  );
}

describe("first view", () => {
  it("shows the product, a search box and what the search tolerates", async () => {
    mockApi();
    render(<App />);
    await ready();

    expect(screen.getAllByText("HEN").length).toBeGreaterThan(0);
    expect(screen.getByRole("searchbox", { name: /text to complete/i })).toBeInTheDocument();
    expect(screen.getByText(/one mistyped character is fine/i)).toBeInTheDocument();
  });

  it("says how much is being searched", async () => {
    mockApi();
    render(<App />);
    expect(
      await screen.findByText(/2,391,950 sentences from 1,504 files/),
    ).toBeInTheDocument();
  });

  it("asks for nothing before anything is typed", async () => {
    const { calls } = mockApi();
    render(<App />);
    await ready();
    expect(calls).toEqual([]);
  });
});

describe("searching", () => {
  it("searches when Enter is pressed", async () => {
    const { calls } = mockApi({ results: [completion()] });
    render(<App />);
    await ready();

    await userEvent.type(screen.getByRole("searchbox"), "this is{Enter}");
    expect(await screen.findByText("Alpha: this is a demo.")).toBeInTheDocument();
    expect(calls).toContain("this is");
  });

  it("searches when the search button is used", async () => {
    mockApi({ results: [completion()] });
    render(<App />);
    await ready();

    await userEvent.type(screen.getByRole("searchbox"), "this is");
    await userEvent.click(screen.getByRole("button", { name: /^search$/i }));
    expect(await screen.findByText("Alpha: this is a demo.")).toBeInTheDocument();
  });

  it("shows that it is working while the answer is awaited", async () => {
    mockApi({ results: [completion()], delayMs: 60 });
    render(<App />);
    await ready();

    await userEvent.type(screen.getByRole("searchbox"), "this is{Enter}");
    expect(await screen.findByText("Searching.")).toBeInTheDocument();
    expect(await screen.findByText("Alpha: this is a demo.")).toBeInTheDocument();
  });

  it("keeps what was typed exactly as it was typed", async () => {
    mockApi({ results: [completion()] });
    render(<App />);
    await ready();

    const box = screen.getByRole("searchbox");
    await userEvent.type(box, "  ThIs   Is,  {Enter}");
    expect(box).toHaveValue("  ThIs   Is,  ");
  });
});

describe("results", () => {
  it("renders one suggestion with all of its details", async () => {
    mockApi({
      results: [
        completion({
          completed_sentence: "one of a kind",
          source_text: "deep/more.txt",
          offset: 42,
          score: 12,
        }),
      ],
    });
    render(<App />);
    await ready();
    await userEvent.type(screen.getByRole("searchbox"), "one of{Enter}");

    const item = await screen.findByRole("listitem");
    expect(within(item).getByText("one of a kind")).toBeInTheDocument();
    // File and line are written together, the way the command line writes them.
    expect(within(item).getByTitle("deep/more.txt")).toHaveTextContent(
      "deep/more.txt:42",
    );
    expect(within(item).getByText(/score/)).toHaveTextContent("score 12");
  });

  it("renders five suggestions in the order the API gave them", async () => {
    mockApi({ results: fiveCompletions() });
    render(<App />);
    await ready();
    await userEvent.type(screen.getByRole("searchbox"), "this is{Enter}");

    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(5);
    expect(items.map((item) => item.textContent?.split(":")[0])).toEqual([
      "Alpha",
      "Beta",
      "Delta",
      "Gamma",
      "Omega",
    ]);
  });

  it("says so when nothing matches", async () => {
    mockApi({ results: [] });
    render(<App />);
    await ready();
    await userEvent.type(screen.getByRole("searchbox"), "zzzz{Enter}");

    expect(await screen.findByText("No suggestions")).toBeInTheDocument();
  });

  it("puts a chosen suggestion into the box", async () => {
    mockApi({ results: fiveCompletions() });
    render(<App />);
    await ready();
    await userEvent.type(screen.getByRole("searchbox"), "this is{Enter}");

    await userEvent.click(await screen.findByRole("button", { name: /Beta: this is a demo/ }));
    expect(screen.getByRole("searchbox")).toHaveValue("Beta: this is a demo.");
  });
});

describe("clearing", () => {
  it("empties the box and the results", async () => {
    mockApi({ results: fiveCompletions() });
    render(<App />);
    await ready();
    await userEvent.type(screen.getByRole("searchbox"), "this is{Enter}");
    await screen.findAllByRole("listitem");

    await userEvent.click(screen.getByRole("button", { name: /clear and start a new sentence/i }));

    expect(screen.getByRole("searchbox")).toHaveValue("");
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
  });

  it("clears with Escape from the box", async () => {
    mockApi({ results: fiveCompletions() });
    render(<App />);
    await ready();

    const box = screen.getByRole("searchbox");
    await userEvent.type(box, "this is{Enter}");
    await screen.findAllByRole("listitem");
    await userEvent.type(box, "{Escape}");

    expect(box).toHaveValue("");
  });
});

describe("keyboard", () => {
  it("moves from the box into the results and back", async () => {
    mockApi({ results: fiveCompletions() });
    render(<App />);
    await ready();

    const box = screen.getByRole("searchbox");
    await userEvent.type(box, "this is{Enter}");
    const items = await screen.findAllByRole("button", { name: /this is a demo/ });

    await userEvent.type(box, "{ArrowDown}");
    expect(items[0]).toHaveFocus();

    await userEvent.keyboard("{ArrowDown}");
    expect(items[1]).toHaveFocus();

    await userEvent.keyboard("{ArrowUp}");
    expect(items[0]).toHaveFocus();

    await userEvent.keyboard("{ArrowUp}");
    expect(box).toHaveFocus();
  });

  it("returns to the box on Escape from a result", async () => {
    mockApi({ results: fiveCompletions() });
    render(<App />);
    await ready();

    const box = screen.getByRole("searchbox");
    await userEvent.type(box, "this is{Enter}");
    await screen.findAllByRole("listitem");

    await userEvent.type(box, "{ArrowDown}");
    await userEvent.keyboard("{Escape}");
    expect(box).toHaveFocus();
  });

  it("accepts a suggestion with the keyboard", async () => {
    mockApi({ results: fiveCompletions() });
    render(<App />);
    await ready();

    const box = screen.getByRole("searchbox");
    await userEvent.type(box, "this is{Enter}");
    await screen.findAllByRole("listitem");

    await userEvent.type(box, "{ArrowDown}");
    await userEvent.keyboard("{Enter}");
    expect(box).toHaveValue("Alpha: this is a demo.");
  });
});

describe("states outside a normal answer", () => {
  it("explains that the index is being prepared", async () => {
    mockApi({ health: PREPARING });
    render(<App />);
    expect(await screen.findByText(/getting the corpus ready/i)).toBeInTheDocument();
  });

  it("explains that the service is not running, and how to start it", async () => {
    mockApi({ failHealth: true });
    render(<App />);
    expect(await screen.findByText(/search service is not running/i)).toBeInTheDocument();
    expect(screen.getByText(/uvicorn autocomplete\.web/)).toBeInTheDocument();
  });

  it("offers a retry when the service is unreachable", async () => {
    mockApi({ failHealth: true });
    render(<App />);
    expect(await screen.findByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("reports an unexpected failure without a stack trace", async () => {
    mockApi({ failComplete: "server" });
    render(<App />);
    await ready();
    await userEvent.type(screen.getByRole("searchbox"), "this is{Enter}");

    // Once visibly and once in the live region, which is the intent.
    const shown = await screen.findAllByText(/something went wrong/i);
    expect(shown.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
    expect(screen.queryByText(/Traceback/)).not.toBeInTheDocument();
  });

  it("reports that the index is not ready if it becomes unavailable mid-session", async () => {
    mockApi({ failComplete: "preparing" });
    render(<App />);
    await ready();
    await userEvent.type(screen.getByRole("searchbox"), "this is{Enter}");

    expect(await screen.findByText(/index is not ready/i)).toBeInTheDocument();
  });
});

describe("live search", () => {
  it("searches while typing, without a request per keystroke", async () => {
    const { calls } = mockApi({ results: [completion()] });
    render(<App />);
    await ready();

    await userEvent.type(screen.getByRole("searchbox"), "this is");
    await screen.findByText("Alpha: this is a demo.");

    // Debounced: far fewer requests than the seven characters typed.
    expect(calls.length).toBeLessThan(7);
    expect(calls.at(-1)).toBe("this is");
  });

  it("never lets a slower earlier answer replace a newer one", async () => {
    // The first search is held open; the second returns at once. The first is
    // aborted when the second starts, so the newer answer must be what shows,
    // and must stay showing.
    let call = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/health"))
        return new Response(JSON.stringify(READY), {
          status: 200,
          headers: { "content-type": "application/json" },
        });

      const query = new URL(url, "http://localhost").searchParams.get("q") ?? "";
      const slow = call++ === 0;
      if (slow) {
        await new Promise<void>((resolve, reject) => {
          const timer = setTimeout(resolve, 200);
          init?.signal?.addEventListener("abort", () => {
            clearTimeout(timer);
            reject(new DOMException("Aborted", "AbortError"));
          });
        });
      }
      const results = [completion({ completed_sentence: `answer for ${query}` })];
      return new Response(JSON.stringify({ query, count: 1, results }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    await ready();

    const box = screen.getByRole("searchbox");
    await userEvent.type(box, "aa{Enter}");
    await userEvent.clear(box);
    await userEvent.type(box, "bb{Enter}");

    expect(await screen.findByText("answer for bb")).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(screen.getByText("answer for bb")).toBeInTheDocument();
    expect(screen.queryByText("answer for aa")).not.toBeInTheDocument();
  });
});

describe("restraint", () => {
  // The interface earned a complaint about clutter. These keep the removals
  // deliberate rather than letting them drift back in.

  it("puts nothing beside the field competing with it", async () => {
    mockApi();
    const { container } = render(<App />);
    await ready();

    // Searching happens on typing and on Enter, so the submit control exists
    // for assistive technology but takes no visual weight.
    const submit = container.querySelector("button[type='submit']");
    expect(submit).toHaveClass("sr-only");
  });

  it("shows no clear button until there is something to clear", async () => {
    mockApi();
    render(<App />);
    await ready();

    expect(
      screen.queryByRole("button", { name: /clear and start a new sentence/i }),
    ).not.toBeInTheDocument();

    await userEvent.type(screen.getByRole("searchbox"), "a");
    expect(
      screen.getByRole("button", { name: /clear and start a new sentence/i }),
    ).toBeInTheDocument();
  });

  it("does not number the results, since a list already reads in order", async () => {
    mockApi({ results: fiveCompletions() });
    render(<App />);
    await ready();
    await userEvent.type(screen.getByRole("searchbox"), "this is{Enter}");

    const items = await screen.findAllByRole("listitem");
    expect(items[0].textContent).not.toMatch(/^\s*1\b/);
  });

  it("keeps the count out of the page and in the announcement", async () => {
    mockApi({ results: fiveCompletions() });
    const { container } = render(<App />);
    await ready();
    await userEvent.type(screen.getByRole("searchbox"), "this is{Enter}");
    await screen.findAllByRole("listitem");

    const live = container.querySelector("[aria-live='polite']");
    expect(live?.textContent).toContain("5 suggestions.");
    const visible = screen.queryAllByText(/5 suggestions/).filter(
      (node) => !node.closest("[aria-live]"),
    );
    expect(visible).toEqual([]);
  });

  it("hides the guidance once there are sentences to read", async () => {
    mockApi({ results: fiveCompletions() });
    render(<App />);
    await ready();
    expect(screen.getByText(/one mistyped character is fine/i)).toBeVisible();

    await userEvent.type(screen.getByRole("searchbox"), "this is{Enter}");
    await screen.findAllByRole("listitem");
    expect(screen.getByText(/one mistyped character is fine/i)).toHaveClass("sr-only");
  });
});

describe("accessibility", () => {
  it("labels the search box and describes what it does", async () => {
    mockApi();
    render(<App />);
    await ready();

    const box = screen.getByRole("searchbox", { name: /text to complete/i });
    expect(box).toHaveAccessibleDescription(/one mistyped character is fine/i);
  });

  it("announces the number of suggestions politely", async () => {
    mockApi({ results: fiveCompletions() });
    const { container } = render(<App />);
    await ready();
    await userEvent.type(screen.getByRole("searchbox"), "this is{Enter}");

    await waitFor(() => {
      const live = container.querySelector("[aria-live='polite']");
      expect(live?.textContent).toContain("5 suggestions.");
    });
  });

  it("announces that nothing was found", async () => {
    mockApi({ results: [] });
    const { container } = render(<App />);
    await ready();
    await userEvent.type(screen.getByRole("searchbox"), "zzzz{Enter}");

    await waitFor(() => {
      const live = container.querySelector("[aria-live='polite']");
      expect(live?.textContent).toContain("No suggestions found.");
    });
  });

  it("offers a skip link to the search", async () => {
    mockApi();
    render(<App />);
    await ready();
    expect(screen.getByRole("link", { name: /skip to search/i })).toBeInTheDocument();
  });

  it("uses a real search landmark", async () => {
    mockApi();
    render(<App />);
    await ready();
    expect(screen.getByRole("search")).toBeInTheDocument();
  });
});
