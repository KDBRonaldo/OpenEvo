import React from "react";
import ReactDOM from "react-dom/client";
import "../styles.css";
import { DesktopProductApp } from "./DesktopProductApp";
import { createDevelopmentAgentProvider } from "./developmentAgentProvider";

export function createProductPreviewProvider(mode = import.meta.env.MODE) {
  if (mode !== "openevo-live-agent") {
    throw new Error(
      "The Desktop development renderer requires the real remote-agent mode. "
      + "Start it with npm run dev:agent:remote or npm run dev:agent.",
    );
  }
  return createDevelopmentAgentProvider();
}

async function createPreviewProvider() {
  if (import.meta.env.MODE === "openevo-live-agent-web") {
    // Keep the complete, proven development product behavior while changing only its
    // transport: Vite now sends every daemon API request to the local Web Layer.
    return createDevelopmentAgentProvider();
  }
  return createProductPreviewProvider();
}

if (!import.meta.env.DEV) {
  throw new Error("The product preview is available only from the Vite development server.");
}

void createPreviewProvider().then((provider) => {
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <DesktopProductApp provider={provider} />
    </React.StrictMode>,
  );
});
