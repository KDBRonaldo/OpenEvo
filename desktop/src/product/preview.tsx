import React from "react";
import ReactDOM from "react-dom/client";
import "../styles.css";
import { DesktopProductApp } from "./DesktopProductApp";
import { createDevelopmentAgentProvider } from "./developmentAgentProvider";
import { createFixtureDesktopProductProvider } from "./fixtureProvider";

export function createProductPreviewProvider(mode = import.meta.env.MODE) {
  return mode === "openevo-live-agent"
    ? createDevelopmentAgentProvider()
    : createFixtureDesktopProductProvider();
}

if (!import.meta.env.DEV) {
  throw new Error("The product preview is available only from the Vite development server.");
}

const provider = createProductPreviewProvider();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <DesktopProductApp provider={provider} />
  </React.StrictMode>,
);
