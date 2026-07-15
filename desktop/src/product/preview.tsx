import React from "react";
import ReactDOM from "react-dom/client";
import "../styles.css";
import { DesktopProductApp } from "./DesktopProductApp";
import {
  createFixtureDesktopProductProvider,
  type FixtureProviderOptions,
} from "./fixtureProvider";

type PreviewScenario = "new-user" | "offline" | "online" | "completed" | "degraded";

const scenarios: Record<PreviewScenario, FixtureProviderOptions> = {
  "new-user": { newUser: true, releaseExecutionModes: true },
  offline: { newUser: false, releaseExecutionModes: true },
  online: { startOnline: true, releaseExecutionModes: true },
  completed: { startOnline: true, seedCompletedRun: true, releaseExecutionModes: true },
  degraded: { startOnline: true, degraded: true, seedCompletedRun: true, releaseExecutionModes: true },
};

function previewScenario(): PreviewScenario {
  const requested = new URLSearchParams(window.location.search).get("scenario");
  return requested && Object.hasOwn(scenarios, requested)
    ? requested as PreviewScenario
    : "completed";
}

if (!import.meta.env.DEV) {
  throw new Error("The product preview is available only from the Vite development server.");
}

const provider = createFixtureDesktopProductProvider(scenarios[previewScenario()]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <DesktopProductApp provider={provider} />
  </React.StrictMode>,
);
