/**
 * Loading and driving Google's own authorization and file picker.
 *
 * Two things about the shape of this file are deliberate.
 *
 * **Google's scripts are fetched at runtime, not bundled.** They are loaded the
 * first time somebody actually asks to connect, so a build with the feature
 * switched off is byte-for-byte a build without it, and the page costs nothing
 * extra to load for anyone who never opens the panel.
 *
 * **Everything Google-shaped is behind one small interface.** `GoogleBridge` has
 * two methods, and the rest of the interface only ever sees those, so the whole
 * import flow can be exercised in tests with no network, no Google account and
 * no script tag. That is what makes "the tests need no credentials" true rather
 * than aspirational.
 *
 * This uses the current Google Identity Services token flow and the Google
 * Picker API. It asks for one scope, `drive.file`, which Google documents as
 * covering only the files a user opens with the app or shares with it through
 * the Picker: it cannot list, search or read anything else in their Drive.
 */

const GIS_SCRIPT = "https://accounts.google.com/gsi/client";
const GAPI_SCRIPT = "https://apis.google.com/js/api.js";

export interface PickerConfig {
  clientId: string;
  apiKey: string;
  appId: string;
  scope: string;
  mimeTypes: string[];
}

/** What the import flow needs from Google, and nothing more. */
export interface GoogleBridge {
  /**
   * Ask the user to authorize, returning an access token.
   *
   * Rejects with a {@link GoogleFlowError} if they decline or the flow fails.
   */
  requestAccessToken(config: PickerConfig): Promise<string>;

  /**
   * Show the picker, returning the identifiers of what was chosen.
   *
   * Resolves to `null` when the user closes it without choosing, which is a
   * normal outcome and not an error.
   */
  pickFiles(config: PickerConfig, accessToken: string): Promise<string[] | null>;
}

/** A failure in Google's own flow, separated from a failure of ours. */
export class GoogleFlowError extends Error {
  readonly code: string;

  constructor(message: string, code = "google_flow") {
    super(message);
    this.name = "GoogleFlowError";
    this.code = code;
  }
}

/* eslint-disable @typescript-eslint/no-explicit-any */
declare global {
  interface Window {
    google?: any;
    gapi?: any;
  }
}

const loaded = new Map<string, Promise<void>>();

/** Load one script once, whoever asks and however often. */
function loadScript(source: string): Promise<void> {
  const existing = loaded.get(source);
  if (existing) return existing;

  const pending = new Promise<void>((resolve, reject) => {
    const element = document.createElement("script");
    element.src = source;
    element.async = true;
    element.defer = true;
    element.onload = () => resolve();
    element.onerror = () => {
      // Let a later attempt try again rather than caching the failure: this is
      // usually a network problem, and retrying is exactly the right response.
      loaded.delete(source);
      reject(
        new GoogleFlowError(
          "Google's sign-in could not be loaded. Check the network connection and try again.",
          "script_blocked",
        ),
      );
    };
    document.head.appendChild(element);
  });

  loaded.set(source, pending);
  return pending;
}

async function identityServices(): Promise<any> {
  await loadScript(GIS_SCRIPT);
  const oauth2 = window.google?.accounts?.oauth2;
  if (!oauth2) {
    throw new GoogleFlowError(
      "Google's sign-in loaded but did not start. Reload the page and try again.",
      "script_blocked",
    );
  }
  return oauth2;
}

async function picker(): Promise<any> {
  await loadScript(GAPI_SCRIPT);
  if (!window.gapi?.load) {
    throw new GoogleFlowError(
      "Google's file picker could not be loaded. Check the network connection and try again.",
      "script_blocked",
    );
  }
  if (!window.google?.picker) {
    await new Promise<void>((resolve, reject) => {
      window.gapi.load("picker", {
        callback: () => resolve(),
        onerror: () =>
          reject(
            new GoogleFlowError(
              "Google's file picker could not be started. Try again.",
              "script_blocked",
            ),
          ),
      });
    });
  }
  if (!window.google?.picker) {
    throw new GoogleFlowError(
      "Google's file picker could not be started. Try again.",
      "script_blocked",
    );
  }
  return window.google.picker;
}

/** The real bridge, over Google Identity Services and the Google Picker API. */
export const googleBridge: GoogleBridge = {
  async requestAccessToken(config) {
    const oauth2 = await identityServices();
    return new Promise<string>((resolve, reject) => {
      const client = oauth2.initTokenClient({
        client_id: config.clientId,
        scope: config.scope,
        callback: (response: any) => {
          if (response?.access_token) resolve(response.access_token as string);
          else
            reject(
              new GoogleFlowError(
                "Google did not return an authorization. Try connecting again.",
                "no_token",
              ),
            );
        },
        error_callback: (error: any) => {
          const type = error?.type;
          if (type === "popup_closed" || type === "popup_failed_to_open") {
            reject(
              new GoogleFlowError(
                "The Google sign-in window was closed before finishing.",
                "cancelled",
              ),
            );
          } else {
            reject(
              new GoogleFlowError(
                "Google refused the authorization. Check that this site is listed as an authorized origin for the OAuth client.",
                "denied",
              ),
            );
          }
        },
      });
      client.requestAccessToken();
    });
  },

  async pickFiles(config, accessToken) {
    const api = await picker();
    return new Promise<string[] | null>((resolve, reject) => {
      try {
        const view = new api.DocsView(api.ViewId.DOCS)
          .setIncludeFolders(false)
          .setSelectFolderEnabled(false)
          .setMimeTypes(config.mimeTypes.join(","));

        const built = new api.PickerBuilder()
          .addView(view)
          .enableFeature(api.Feature.MULTISELECT_ENABLED)
          .setOAuthToken(accessToken)
          .setDeveloperKey(config.apiKey)
          // The Cloud project number. Without it the picker cannot grant this
          // application per-file access to what was chosen under drive.file.
          .setAppId(config.appId)
          .setTitle("Choose text documents to search")
          .setCallback((data: any) => {
            if (data.action === api.Action.PICKED) {
              const chosen = (data.docs ?? [])
                .map((document: any) => String(document?.id ?? ""))
                .filter(Boolean);
              resolve(chosen.length ? chosen : null);
            } else if (data.action === api.Action.CANCEL) {
              resolve(null);
            }
          })
          .build();
        built.setVisible(true);
      } catch {
        reject(
          new GoogleFlowError(
            "Google's file picker could not be opened. Check that the browser API key allows this site.",
            "picker_failed",
          ),
        );
      }
    });
  },
};
