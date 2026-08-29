import React from "react";
import ReactDOM from "react-dom/client";
import "./src/styles.css";
import { DesktopProductApp } from "./src/product/DesktopProductApp";
import { createSelfHostedFormalProvider } from "./src/product/selfHostedFormalProvider";
import type { DesktopBootstrapContextV2 } from "./src/api/v2/schemas";

const BOOTSTRAP_TOKEN_PATTERN = /^[0-9a-f]{64}$/;
const SESSION_STORAGE_KEY = "openevo.self-hosted.browser-session.v1";
const DEVELOPMENT_AGENT_PREFIX = "/openevo-dev-agent/";
const DEVELOPMENT_WEB_TOKEN_HEADER = "X-OpenEvo-Development-Web-Token";

function parseBrowserSession(value: unknown): DesktopBootstrapContextV2 {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("The Web Layer returned an invalid browser session.");
  }
  const record = value as Record<string, unknown>;
  if (
    record.schema_version !== "2"
    || record.endpoint !== window.location.origin
    || typeof record.session_token !== "string"
    || !BOOTSTRAP_TOKEN_PATTERN.test(record.session_token)
  ) {
    throw new Error("The Web Layer returned an invalid browser session.");
  }
  if (typeof record.negotiated_contract !== "object" || record.negotiated_contract === null) {
    throw new Error("The Web Layer returned no negotiated Desktop v2 contract.");
  }
  return value as DesktopBootstrapContextV2;
}

function loadStoredBrowserSession(): DesktopBootstrapContextV2 | null {
  try {
    const serialized = window.sessionStorage.getItem(SESSION_STORAGE_KEY);
    return serialized === null ? null : parseBrowserSession(JSON.parse(serialized));
  } catch {
    window.sessionStorage.removeItem(SESSION_STORAGE_KEY);
    return null;
  }
}

async function establishBrowserSession(): Promise<DesktopBootstrapContextV2> {
  const parameters = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const bootstrapToken = parameters.get("browser-bootstrap");
  if (bootstrapToken === null) {
    const stored = loadStoredBrowserSession();
    if (stored !== null) return stored;
    throw new Error("EvoLab must be opened from the remote development launcher.");
  }
  if (!BOOTSTRAP_TOKEN_PATTERN.test(bootstrapToken)) {
    throw new Error("The browser bootstrap token is invalid.");
  }

  const response = await window.fetch("/openevo-native/browser/bootstrap", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ schema_version: "2", bootstrap_token: bootstrapToken }),
    cache: "no-store",
    credentials: "omit",
    referrerPolicy: "no-referrer",
  });
  if (!response.ok) {
    throw new Error(`The Web Layer rejected browser bootstrap (${response.status}).`);
  }

  const session = parseBrowserSession(await response.json());
  window.sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session));
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  return session;
}

function installDevelopmentAgentTransport(sessionToken: string): void {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const request = new Request(input, init);
    const url = new URL(request.url, window.location.href);
    if (url.origin !== window.location.origin || !url.pathname.startsWith(DEVELOPMENT_AGENT_PREFIX)) {
      return nativeFetch(request);
    }
    const headers = new Headers(request.headers);
    headers.set(DEVELOPMENT_WEB_TOKEN_HEADER, sessionToken);
    return nativeFetch(new Request(request, { headers }));
  };
}

async function main(): Promise<void> {
  const session = await establishBrowserSession();
  installDevelopmentAgentTransport(session.session_token);
  const provider = await createSelfHostedFormalProvider(session);
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <DesktopProductApp provider={provider} />
    </React.StrictMode>,
  );
}

void main().catch((error: unknown) => {
  const message = error instanceof Error ? error.message : "The Web Layer could not start.";
  document.getElementById("root")!.textContent = message;
});
