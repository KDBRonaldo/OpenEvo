import React from "react";
import ReactDOM from "react-dom/client";
import "../styles.css";
import { DesktopProductApp } from "./DesktopProductApp";
import {
  createFixtureDesktopProductProvider,
  type FixtureProviderOptions,
} from "./fixtureProvider";

export type PreviewScenario = "new-user" | "offline" | "online" | "completed" | "degraded" | "failed";

export const PRODUCT_PREVIEW_SCENARIOS: Readonly<Record<PreviewScenario, FixtureProviderOptions>> = {
  "new-user": { newUser: true, releaseExecutionModes: true },
  offline: { newUser: false, releaseExecutionModes: true, projectExecutionMode: "codex_subscription_transcript" },
  online: { startOnline: true, releaseExecutionModes: true, projectExecutionMode: "codex_subscription_transcript" },
  completed: { startOnline: true, seedCompletedRun: true, releaseExecutionModes: true, projectExecutionMode: "codex_subscription_transcript" },
  degraded: { startOnline: true, degraded: true, seedCompletedRun: true, releaseExecutionModes: true, projectExecutionMode: "codex_subscription_transcript" },
  failed: { startOnline: true, seedFailedRun: true, releaseExecutionModes: true, projectExecutionMode: "codex_subscription_transcript" },
};

export function previewScenario(search = window.location.search): PreviewScenario {
  const requested = new URLSearchParams(search).get("scenario");
  return requested && Object.hasOwn(PRODUCT_PREVIEW_SCENARIOS, requested)
    ? requested as PreviewScenario
    : "completed";
}

export function createProductPreviewProvider(scenario: PreviewScenario) {
  return createFixtureDesktopProductProvider(PRODUCT_PREVIEW_SCENARIOS[scenario]);
}

if (!import.meta.env.DEV) {
  throw new Error("The product preview is available only from the Vite development server.");
}

const provider = createProductPreviewProvider(previewScenario());

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <DesktopProductApp provider={provider} />
  </React.StrictMode>,
);
