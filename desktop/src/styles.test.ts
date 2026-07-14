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
});
