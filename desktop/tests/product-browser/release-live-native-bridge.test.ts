// @vitest-environment happy-dom

import { describe, expect, it } from "vitest";
import { installReleaseLiveNativeBridgeV2 } from "./release-live-native-bridge";

describe("release live native bridge", () => {
  it("projects the closed native startup status used by the release shell", async () => {
    installReleaseLiveNativeBridgeV2({
      negotiated_contract: {
        openapi_sha256: "a".repeat(64),
        event_schema_sha256: "b".repeat(64),
        release_version: "0.1.10",
      },
    });

    const invoke = (window as unknown as {
      __TAURI_INTERNALS__: {
        invoke(command: string, args?: Record<string, unknown>): Promise<unknown>;
      };
    }).__TAURI_INTERNALS__.invoke;

    await expect(invoke("sidecar_startup_status")).resolves.toEqual({
      schema_version: "2",
      startup_epoch: 1,
      status: "succeeded",
      phase: "ready",
      phase_index: 5,
      phase_total: 6,
      elapsed_milliseconds: 0,
      cancellable: false,
      failure: null,
    });
    await expect(invoke("unsupported_native_command")).rejects.toThrow(
      "Unexpected native command: unsupported_native_command",
    );
  });
});
