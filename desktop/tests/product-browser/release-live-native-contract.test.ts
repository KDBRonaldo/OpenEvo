import { runInNewContext } from "node:vm";
import { describe, expect, it } from "vitest";
import {
  installReleaseLiveNativeBridgeV2,
  type ReleaseLiveNativeObservationV2,
} from "./release-live-native-bridge";

const bootstrap = {
  negotiated_contract: {
    openapi_sha256: "a".repeat(64),
    event_schema_sha256: "b".repeat(64),
    release_version: "0.1.10",
  },
} as const;

describe("release-live native contract", () => {
  it("implements exact mutation journal CAS semantics and remains fail-closed", async () => {
    const sandbox = {} as {
      window: unknown;
      __TAURI_INTERNALS__?: {
        invoke(command: string, args?: Record<string, unknown>): Promise<unknown>;
      };
      __OPENEVO_LIVE_NATIVE_OBSERVATION__?: ReleaseLiveNativeObservationV2;
    };
    sandbox.window = sandbox;
    const install = runInNewContext(
      `(${installReleaseLiveNativeBridgeV2.toString()})`,
      sandbox,
    ) as typeof installReleaseLiveNativeBridgeV2;
    install(bootstrap);
    const tauri = sandbox.__TAURI_INTERNALS__;
    expect(tauri).toBeDefined();

    const initial = await tauri!.invoke("read_mutation_intent_journal_v2");
    await tauri!.invoke("compare_and_swap_mutation_intent_journal_v2", {
      expectedValue: null,
      newValue: '{"schema_version":"2"}',
    });
    const stored = await tauri!.invoke("read_mutation_intent_journal_v2");
    let conflictCode: unknown = null;
    try {
      await tauri!.invoke("compare_and_swap_mutation_intent_journal_v2", {
        expectedValue: null,
        newValue: null,
      });
    } catch (error) {
      conflictCode = (error as { code?: unknown }).code;
    }
    const retainedAfterConflict = await tauri!.invoke("read_mutation_intent_journal_v2");
    await tauri!.invoke("compare_and_swap_mutation_intent_journal_v2", {
      expectedValue: '{"schema_version":"2"}',
      newValue: null,
    });
    const removed = await tauri!.invoke("read_mutation_intent_journal_v2");
    let invalidArgumentsRejected = false;
    try {
      await tauri!.invoke("compare_and_swap_mutation_intent_journal_v2", {
        expectedValue: 1,
        newValue: null,
      });
    } catch {
      invalidArgumentsRejected = true;
    }
    let unsupportedCommandRejected = false;
    try {
      await tauri!.invoke("start_sidecar");
    } catch {
      unsupportedCommandRejected = true;
    }
    expect({
      initial,
      stored,
      conflictCode,
      retainedAfterConflict,
      removed,
      invalidArgumentsRejected,
      unsupportedCommandRejected,
      unexpected: sandbox.__OPENEVO_LIVE_NATIVE_OBSERVATION__?.unexpected,
    }).toEqual({
      initial: null,
      stored: '{"schema_version":"2"}',
      conflictCode: "mutation_intent_journal_conflict",
      retainedAfterConflict: '{"schema_version":"2"}',
      removed: null,
      invalidArgumentsRejected: true,
      unsupportedCommandRejected: true,
      unexpected: ["mutation_journal_arguments", "native_command"],
    });
  });
});
