import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const source = readFileSync(
  new URL("./release-live-observability.pw.ts", import.meta.url),
  "utf8",
);

describe("release-live native contract", () => {
  it("provides the durable mutation journal commands before rejecting unknown native calls", () => {
    const bridgeStart = source.indexOf("async function installNativeBridge(");
    const bridgeEnd = source.indexOf("\nasync function readNativeObservation(", bridgeStart);
    expect(bridgeStart).toBeGreaterThanOrEqual(0);
    expect(bridgeEnd).toBeGreaterThan(bridgeStart);
    const bridge = source.slice(bridgeStart, bridgeEnd);

    const readCommand = bridge.indexOf('command === "read_mutation_intent_journal_v2"');
    const compareAndSwapCommand = bridge.indexOf(
      'command === "compare_and_swap_mutation_intent_journal_v2"',
    );
    const unknownCommand = bridge.indexOf('observation.unexpected.push("native_command")');

    expect(bridge).toContain("let mutationIntentJournal: string | null = null;");
    expect(readCommand).toBeGreaterThanOrEqual(0);
    expect(compareAndSwapCommand).toBeGreaterThan(readCommand);
    expect(bridge).toContain('code: "mutation_intent_journal_conflict"');
    expect(unknownCommand).toBeGreaterThan(compareAndSwapCommand);
    expect(bridge).not.toContain('command === "start_sidecar"');
  });
});
