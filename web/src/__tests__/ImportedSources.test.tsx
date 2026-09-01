import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { ImportedSources } from "../components/ImportedSources";
import { useDrive } from "../drive/useDrive";
import type { GoogleBridge } from "../drive/google";
import { GoogleFlowError } from "../drive/google";
import {
  completion,
  driveReady,
  importedDocument,
  job,
  mockApi,
} from "../test/harness";

/**
 * The import flow, exercised end to end in the browser without Google.
 *
 * Google's authorization and picker are behind one small interface, so a test
 * supplies its own. Nothing here loads a script, opens a window, or needs a
 * credential; the flow being tested is exactly the one that runs in production
 * up to that boundary.
 */

/** A stand-in for Google that grants a token and returns chosen files. */
function fakeGoogle(overrides: Partial<GoogleBridge> = {}): GoogleBridge & {
  tokenRequests: number;
  pickerOpens: number;
} {
  const bridge = {
    tokenRequests: 0,
    pickerOpens: 0,
    async requestAccessToken() {
      bridge.tokenRequests += 1;
      return "fake-access-token";
    },
    async pickFiles() {
      bridge.pickerOpens += 1;
      return ["file-1"];
    },
    ...overrides,
  };
  return bridge as GoogleBridge & { tokenRequests: number; pickerOpens: number };
}

/** The panel on its own, wired to a bridge the test controls. */
function Panel({ bridge }: { bridge: GoogleBridge }) {
  return <ImportedSources drive={useDrive({ bridge })} />;
}

async function openPanel(user: ReturnType<typeof userEvent.setup>) {
  const toggle = await screen.findByRole("button", { name: /imported sources/i });
  await user.click(toggle);
  return toggle;
}

beforeEach(() => {
  vi.useRealTimers();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("when the feature is switched off", () => {
  it("shows nothing at all", async () => {
    mockApi();
    render(<Panel bridge={fakeGoogle()} />);
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /imported sources/i })).toBeNull(),
    );
  });

  it("leaves the rest of the page working", async () => {
    mockApi({ results: [completion()] });
    const user = userEvent.setup();
    render(<App />);
    await user.type(await screen.findByRole("searchbox"), "this is");
    expect(await screen.findByText("Alpha: this is a demo.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /imported sources/i })).toBeNull();
  });
});

describe("when the feature is on but not configured", () => {
  it("says so instead of offering a broken control", async () => {
    mockApi({
      drive: driveReady({
        configured: false,
        state: "disabled",
        client_id: "",
        detail: "Google Drive import is switched on but HEN_DRIVE_API_KEY is not set.",
      }),
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);

    expect(await screen.findByText(/not configured on this server/i)).toBeInTheDocument();
    expect(screen.getByText(/HEN_DRIVE_API_KEY/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /connect google drive/i }),
    ).toBeNull();
  });
});

describe("opening the import flow", () => {
  it("explains that only chosen files are read, before anything is authorized", async () => {
    mockApi({ drive: driveReady() });
    const bridge = fakeGoogle();
    const user = userEvent.setup();
    render(<Panel bridge={bridge} />);
    await openPanel(user);

    expect(
      await screen.findByText(/only the documents you pick are ever read/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/cannot list or search the rest of your drive/i)).toBeInTheDocument();
    expect(bridge.tokenRequests).toBe(0);
  });

  it("shows the configured limits", async () => {
    mockApi({
      drive: driveReady({
        limits: {
          max_files: 3,
          max_file_bytes: 2048,
          max_total_bytes: 8192,
          supported_mime_types: ["text/plain"],
        },
      }),
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    expect(await screen.findByText(/up to 3 at a time, 2 kb each/i)).toBeInTheDocument();
  });

  it("offers to connect before anything is imported", async () => {
    mockApi({ drive: driveReady() });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    expect(
      await screen.findByRole("button", { name: /connect google drive/i }),
    ).toBeInTheDocument();
  });
});

describe("a successful import", () => {
  it("authorizes, opens the picker and sends the chosen identifiers", async () => {
    const { driveCalls } = mockApi({
      drive: driveReady(),
      documents: [],
      jobs: [job()],
    });
    const bridge = fakeGoogle();
    const user = userEvent.setup();
    render(<Panel bridge={bridge} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /connect google drive/i }));

    await waitFor(() => expect(bridge.pickerOpens).toBe(1));
    expect(bridge.tokenRequests).toBe(1);

    const posted = driveCalls.find((call) => call.method === "POST");
    expect(posted?.body).toEqual({ file_ids: ["file-1"] });
  });

  it("sends the token in a header, never in the url", async () => {
    const { driveCalls } = mockApi({ drive: driveReady(), jobs: [job()] });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /connect google drive/i }));

    await waitFor(() => {
      const posted = driveCalls.find((call) => call.method === "POST");
      expect(posted?.token).toBe("fake-access-token");
    });
    for (const call of driveCalls) {
      expect(call.url).not.toContain("fake-access-token");
      expect(call.url).not.toContain("access_token");
    }
  });

  it("lists what was imported and offers to add more", async () => {
    mockApi({
      drive: driveReady({ documents: 1, sentences: 2 }),
      documents: [importedDocument()],
      jobs: [job()],
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /connect google drive/i }));

    expect(await screen.findByRole("listitem")).toHaveTextContent("notes.txt");
    expect(await screen.findByText("2 sentences")).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: /add from google drive/i }),
    ).toBeInTheDocument();
  });

  it("does not ask Google to authorize a second time", async () => {
    mockApi({ drive: driveReady(), documents: [importedDocument()], jobs: [job(), job()] });
    const bridge = fakeGoogle();
    const user = userEvent.setup();
    render(<Panel bridge={bridge} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /connect google drive/i }));
    await screen.findByRole("button", { name: /add from google drive/i });
    await user.click(screen.getByRole("button", { name: /add from google drive/i }));

    await waitFor(() => expect(bridge.pickerOpens).toBe(2));
    expect(bridge.tokenRequests).toBe(1);
  });
});

describe("progress", () => {
  /**
   * The same running job every time it is polled, so a test about what the
   * running state renders is not racing the poll that would end it.
   */
  function stillRunning(state: "downloading" | "building" | "adopting", detail: string) {
    const running = job({ state, progress: { ...job().progress, detail } });
    return Array.from({ length: 10 }, () => running);
  }

  it("reports counted work and never an invented percentage", async () => {
    mockApi({
      drive: driveReady(),
      jobs: Array.from({ length: 10 }, () =>
        job({
          state: "downloading",
          progress: {
            files_selected: 3,
            files_downloaded: 1,
            files_reused: 0,
            bytes_downloaded: 120,
            lines_read: 9,
            sentences_indexed: 0,
            detail: "Downloaded 1 of 3 document(s).",
          },
        }),
      ),
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /connect google drive/i }));

    expect(
      await screen.findAllByText("Downloaded 1 of 3 document(s)."),
    ).not.toHaveLength(0);
    expect(screen.getByText("selected")).toBeInTheDocument();
    expect(screen.getByText("lines read")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(document.body.textContent).not.toMatch(/\d+%/);
  });

  it("announces what is happening to assistive technology", async () => {
    mockApi({
      drive: driveReady(),
      jobs: stillRunning("building", "Building the search index over the imported text."),
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /connect google drive/i }));

    const live = document.querySelector("[aria-live='polite']");
    await waitFor(() =>
      expect(live?.textContent).toContain("Building the search index"),
    );
  });

  it("disables the controls while something is running", async () => {
    mockApi({ drive: driveReady(), jobs: stillRunning("downloading", "Working.") });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /connect google drive/i }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /add from google drive/i })).toBeDisabled(),
    );
  });
});

describe("cancelling", () => {
  it("says nothing was selected, and imports nothing", async () => {
    const { driveCalls } = mockApi({ drive: driveReady() });
    const bridge = fakeGoogle({ async pickFiles() { return null; } });
    const user = userEvent.setup();
    render(<Panel bridge={bridge} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /connect google drive/i }));

    expect(
      await screen.findByText(/the google window closed without any documents/i),
    ).toBeInTheDocument();
    expect(driveCalls.some((call) => call.method === "POST")).toBe(false);
  });

  it("treats a closed sign-in window as a change of mind, not a failure", async () => {
    mockApi({ drive: driveReady() });
    const bridge = fakeGoogle({
      async requestAccessToken() {
        throw new GoogleFlowError("The Google sign-in window was closed.", "cancelled");
      },
    });
    const user = userEvent.setup();
    render(<Panel bridge={bridge} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /connect google drive/i }));

    expect(
      await screen.findByText(/the google window closed without any documents/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("failures", () => {
  it("reports a refused authorization without exposing anything", async () => {
    mockApi({ drive: driveReady() });
    const bridge = fakeGoogle({
      async requestAccessToken() {
        throw new GoogleFlowError("Google refused the authorization.", "denied");
      },
    });
    const user = userEvent.setup();
    render(<Panel bridge={bridge} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /connect google drive/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/google refused the authorization/i);
  });

  it("reports a picker that could not be opened", async () => {
    mockApi({ drive: driveReady() });
    const bridge = fakeGoogle({
      async pickFiles() {
        throw new GoogleFlowError(
          "Google's file picker could not be opened. Check that the browser API key allows this site.",
          "picker_failed",
        );
      },
    });
    const user = userEvent.setup();
    render(<Panel bridge={bridge} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /connect google drive/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/browser api key/i);
  });

  it.each([
    ["an unsupported file", 415, "unsupported", "This import handles plain text files and Google Docs only."],
    ["a file that is too large", 400, "too_large", "This file is over the 10,485,760 byte limit."],
    ["a quota response", 429, "quota", "Google Drive is rate limiting this project. Wait a moment and try again."],
  ])("reports %s", async (_name, status, code, message) => {
    mockApi({
      drive: driveReady(),
      failDrive: { status, code, message, retryable: code === "quota" },
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /connect google drive/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
  });

  it("reports an index build failure and offers to try again", async () => {
    mockApi({
      drive: driveReady(),
      jobs: [
        job({
          state: "failed",
          error: {
            code: "internal",
            message:
              "The change could not be completed. The previously imported documents are still searchable.",
            retryable: true,
          },
        }),
      ],
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /connect google drive/i }));

    expect(await screen.findByText(/did not finish/i)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("retries a failed import", async () => {
    const { driveCalls } = mockApi({
      drive: driveReady(),
      jobs: [job({ state: "failed", error: { code: "transport", message: "Drive could not be reached.", retryable: true } }), job()],
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /connect google drive/i }));
    await user.click(await screen.findByRole("button", { name: /try again/i }));

    await waitFor(() =>
      expect(driveCalls.some((call) => call.url.includes("/retry"))).toBe(true),
    );
  });

  it("never renders a credential or a token in an error", async () => {
    mockApi({
      drive: driveReady({ client_id: "SECRET-CLIENT-ID", api_key: "SECRET-API-KEY" }),
      failDrive: { status: 500, code: "internal", message: "Something went wrong." },
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /connect google drive/i }));

    await screen.findByRole("alert");
    expect(document.body.textContent).not.toContain("SECRET-CLIENT-ID");
    expect(document.body.textContent).not.toContain("SECRET-API-KEY");
    expect(document.body.textContent).not.toContain("fake-access-token");
  });
});

describe("the imported list", () => {
  it("says so when nothing is imported", async () => {
    mockApi({ drive: driveReady(), documents: [] });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    expect(
      await screen.findByText(/no documents imported yet/i),
    ).toBeInTheDocument();
  });

  it("shows each document's size and how much it contributed", async () => {
    mockApi({
      drive: driveReady({ documents: 2 }),
      documents: [
        importedDocument({ id: "a", name: "notes.txt", sentences: 12, bytes: 2048 }),
        importedDocument({
          id: "b",
          name: "Meeting Notes",
          source_text: "Google Drive/Meeting Notes.txt",
          sentences: 1,
          bytes: 40,
        }),
      ],
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);

    expect(await screen.findByText("notes.txt")).toBeInTheDocument();
    expect(screen.getByText("Meeting Notes")).toBeInTheDocument();
    expect(screen.getByText("12 sentences")).toBeInTheDocument();
    expect(screen.getByText("1 sentence")).toBeInTheDocument();
    expect(screen.getByText("2 KB")).toBeInTheDocument();
    // A Google Doc is searched under a name with .txt added, so the path it is
    // searched as is worth showing; a .txt file's is not, and is left out.
    expect(screen.getByText("Meeting Notes.txt")).toBeInTheDocument();
  });

  it("renders a document name as text, never as markup", async () => {
    mockApi({
      drive: driveReady({ documents: 1 }),
      documents: [importedDocument({ name: "<img src=x onerror=alert(1)>.txt" })],
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);

    expect(
      await screen.findByText("<img src=x onerror=alert(1)>.txt"),
    ).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
  });
});

describe("removing a document", () => {
  it("asks for confirmation before changing what is searchable", async () => {
    const { driveCalls } = mockApi({
      drive: driveReady({ documents: 1 }),
      documents: [importedDocument()],
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /remove notes\.txt/i }));

    expect(await screen.findByText(/takes its 2 sentences out of search results/i)).toBeInTheDocument();
    expect(screen.getByText(/file in your google drive is not touched/i)).toBeInTheDocument();
    expect(driveCalls.some((call) => call.method === "DELETE")).toBe(false);
  });

  it("keeps the document when the confirmation is declined", async () => {
    const { driveCalls } = mockApi({
      drive: driveReady({ documents: 1 }),
      documents: [importedDocument()],
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /remove notes\.txt/i }));
    await user.click(await screen.findByRole("button", { name: /keep it/i }));

    expect(screen.getByRole("listitem")).toHaveTextContent("notes.txt");
    expect(driveCalls.some((call) => call.method === "DELETE")).toBe(false);
  });

  it("removes it once confirmed", async () => {
    const { driveCalls } = mockApi({
      drive: driveReady({ documents: 1 }),
      documents: [importedDocument({ id: "abc123" })],
      jobs: [job({ kind: "remove" })],
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /remove notes\.txt/i }));
    await user.click(await screen.findByRole("button", { name: /remove it/i }));

    await waitFor(() =>
      expect(
        driveCalls.some(
          (call) => call.method === "DELETE" && call.url.endsWith("/abc123"),
        ),
      ).toBe(true),
    );
    await waitFor(() => expect(screen.queryByText("notes.txt")).toBeNull());
  });

  it("reports a removal that fails and leaves the document listed", async () => {
    mockApi({
      drive: driveReady({ documents: 1 }),
      documents: [importedDocument()],
      failDrive: {
        status: 500,
        code: "internal",
        message: "The change could not be completed.",
      },
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /remove notes\.txt/i }));
    await user.click(await screen.findByRole("button", { name: /remove it/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /could not be completed/i,
    );
    expect(screen.getByRole("listitem")).toHaveTextContent("notes.txt");
  });

  it("does not ask Google for anything to remove a document", async () => {
    mockApi({
      drive: driveReady({ documents: 1 }),
      documents: [importedDocument()],
      jobs: [job({ kind: "remove" })],
    });
    const bridge = fakeGoogle();
    const user = userEvent.setup();
    render(<Panel bridge={bridge} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /remove notes\.txt/i }));
    await user.click(await screen.findByRole("button", { name: /remove it/i }));

    await waitFor(() => expect(screen.queryByText("notes.txt")).toBeNull());
    expect(bridge.tokenRequests).toBe(0);
  });
});

describe("search results carrying imported sources", () => {
  it("marks a result that came from Drive", async () => {
    mockApi({
      drive: driveReady({ documents: 1, sentences: 2 }),
      documents: [importedDocument()],
      results: [
        completion({
          completed_sentence: "an imported line",
          source_text: "Google Drive/notes.txt",
        }),
        completion({ completed_sentence: "a corpus line", source_text: "corpus.txt" }),
      ],
    });
    const user = userEvent.setup();
    render(<App />);
    await user.type(await screen.findByRole("searchbox"), "line");

    const imported = (await screen.findByText("an imported line")).closest("li")!;
    expect(within(imported).getByText("Google Drive")).toBeInTheDocument();
    // The namespace is shown as a label, not repeated in the path.
    expect(within(imported).getByTitle("Google Drive/notes.txt")).toHaveTextContent(
      "notes.txt:1",
    );

    const corpus = screen.getByText("a corpus line").closest("li")!;
    expect(within(corpus).queryByText("Google Drive")).toBeNull();
  });

  it("marks nothing when nothing is imported", async () => {
    mockApi({
      drive: driveReady(),
      documents: [],
      results: [completion({ source_text: "Google Drive/notes.txt" })],
    });
    const user = userEvent.setup();
    render(<App />);
    await user.type(await screen.findByRole("searchbox"), "demo");

    await screen.findByText("Alpha: this is a demo.");
    expect(screen.queryByText(/^Google Drive$/)).toBeNull();
  });

  it("counts imported sentences in what the page says it is searching", async () => {
    mockApi({ drive: driveReady({ documents: 1, sentences: 50 }), documents: [importedDocument()] });
    render(<App />);
    expect(
      await screen.findByText(/2,392,000 sentences from 1,505 files/),
    ).toBeInTheDocument();
  });
});

describe("keyboard and accessibility", () => {
  it("the panel is a labelled disclosure", async () => {
    mockApi({ drive: driveReady() });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    const toggle = await screen.findByRole("button", { name: /imported sources/i });

    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByRole("region", { name: /imported sources/i }),
    ).toBeInTheDocument();
  });

  it("every control is reachable and operable from the keyboard alone", async () => {
    mockApi({
      drive: driveReady({ documents: 1 }),
      documents: [importedDocument()],
      jobs: [job({ kind: "remove" })],
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);

    const toggle = await screen.findByRole("button", { name: /imported sources/i });
    toggle.focus();
    await user.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    const remove = await screen.findByRole("button", { name: /remove notes\.txt/i });
    remove.focus();
    await user.keyboard("{Enter}");
    const confirm = await screen.findByRole("button", { name: /remove it/i });
    confirm.focus();
    await user.keyboard("{Enter}");

    await waitFor(() => expect(screen.queryByText("notes.txt")).toBeNull());
  });

  it("the remove control names what it removes", async () => {
    mockApi({
      drive: driveReady({ documents: 1 }),
      documents: [importedDocument({ name: "quarterly plan.txt" })],
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    expect(
      await screen.findByRole("button", { name: /remove quarterly plan\.txt from the search/i }),
    ).toBeInTheDocument();
  });

  it("a failure is announced as an alert", async () => {
    mockApi({
      drive: driveReady(),
      failDrive: { status: 415, code: "unsupported", message: "Not a supported type." },
    });
    const user = userEvent.setup();
    render(<Panel bridge={fakeGoogle()} />);
    await openPanel(user);
    await user.click(await screen.findByRole("button", { name: /connect google drive/i }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});
