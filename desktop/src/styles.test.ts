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
});
