import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const styles = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

describe("Desktop product responsive CSS", () => {
  it("bounds the System workspace at the 760px minimum window", () => {
    expect(styles).toMatch(/\.system-grid\s*>\s*\*\s*{\s*min-width:\s*0;/);
    expect(styles).toMatch(/\.definition-list dd\s*{[\s\S]*?overflow-wrap:\s*anywhere;/);
    expect(styles).toMatch(
      /@media\s*\(max-width:\s*1000px\)[\s\S]*?\.system-grid\s*{\s*grid-template-columns:\s*minmax\(0,\s*1fr\);/,
    );
  });

  it("projects target switch keyboard focus onto the visible track without layout shift", () => {
    expect(styles).toMatch(/--focus-ring:\s*#7abca5;/);

    const focusRule = styles.match(
      /\.target-toggle input:not\(:disabled\):focus-visible\s*\+\s*\.switch-track\s*{([^}]*)}/,
    )?.[1];
    expect(focusRule).toBeDefined();
    expect(focusRule).toMatch(/outline:\s*3px solid var\(--focus-ring\);/);
    expect(focusRule).toMatch(/outline-offset:\s*2px;/);
    expect(focusRule).not.toMatch(/(?:border|margin|padding|width|height):/);
  });

  it("keeps history state visible and wraps long research metadata at compact widths", () => {
    expect(styles).toMatch(/\.session-table-head\s*>\s*span:last-child,\s*\.session-table-row\s*>\s*span:last-child\s*{\s*display:\s*none;/);
    expect(styles).not.toMatch(/\.session-table-row\s+span:last-child\s*{\s*display:\s*none;/);
  });

  it("keeps Session composers visually continuous while preserving the inspector split", () => {
    expect(styles).toMatch(/\.session-composer-title::after\s*{[\s\S]*?linear-gradient/);
    expect(styles).toMatch(/\.product-shell \.session-composer-title input:focus-visible,[\s\S]*?outline:\s*0;/);
    expect(styles).toMatch(/\.product-v2-shell \.v2-task-result-detail\s*{[\s\S]*?grid-template-columns:[\s\S]*?gap:\s*16px;/);
    expect(styles).toMatch(/\.session-chat-composer-box\s*{[\s\S]*?min-height:\s*148px;/);
  });

  it("renders Session startup as a lightweight status rail instead of a notification card", () => {
    const statusRule = styles.match(/\.session-submit-progress\s*{([^}]*)}/)?.[1];
    expect(statusRule).toBeDefined();
    expect(statusRule).toMatch(/width:\s*max-content;/);
    expect(statusRule).toMatch(/border:\s*0;/);
    expect(statusRule).toMatch(/background:\s*transparent;/);
    expect(styles).toMatch(/\.session-submit-progress::after\s*{[\s\S]*?linear-gradient/);
  });

  it("uses a wider, softly divided Session inspector instead of nested cards", () => {
    const inspectorRule = styles.match(/\.session-inspector-pane\s*{([^}]*)}/)?.[1];
    expect(inspectorRule).toBeDefined();
    expect(inspectorRule).toMatch(/border:\s*0;/);
    expect(inspectorRule).toMatch(/background:\s*linear-gradient/);
    const sectionRule = styles.match(/\.session-inspector-section\s*{([^}]*)}/)?.[1];
    expect(sectionRule).toMatch(/border:\s*0;/);
    expect(sectionRule).toMatch(/background:\s*transparent;/);
    expect(styles).toMatch(/--session-inspector-width,\s*420px/);
  });

  it("uses custom animated selectors and a motion-safe running state", () => {
    expect(styles).toMatch(/\.soft-select-menu\s*{[\s\S]*?backdrop-filter:\s*blur\(18px\)/);
    expect(styles).toMatch(/@keyframes\s+soft-select-in/);
    expect(styles).toMatch(/\.v2-agent-running-text\s*{[\s\S]*?background-clip:\s*text;[\s\S]*?agent-running-sheen/);
    expect(styles).toMatch(/@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.v2-agent-running-text\s*{[\s\S]*?animation:\s*none;/);
  });

  it("keeps the Project Head menu inside the Session workspace", () => {
    const menuRule = styles.match(/\.session-head-picker \.soft-select-menu\s*{([^}]*)}/)?.[1];
    expect(menuRule).toBeDefined();
    expect(menuRule).toMatch(/right:\s*auto;/);
    expect(menuRule).toMatch(/left:\s*0;/);
    expect(menuRule).toMatch(/max-width:\s*min\(330px,\s*calc\(100vw - 32px\)\);/);
  });
});
