import { describe, expect, it } from "vitest";
import { createFixtureDesktopProductProvider } from "./fixtureProvider";
import {
  ProductRefreshOrder,
  defineDesktopProductReleaseContract,
  unavailableDesktopProductProvider,
  type DesktopProductReleaseContract,
} from "./provider";

describe("explicit Preview/test provider boundary", () => {
  it("keeps the legacy preview release helper closed to a native provider", () => {
    expect(() => defineDesktopProductReleaseContract({
      acceptedOpenApiDigests: [],
      allowedProviderKinds: ["desktop_sidecar"],
      requiredFeatureFlags: [],
    } as unknown as DesktopProductReleaseContract)).toThrow("requires a checked-in OpenAPI digest");

    const contract = defineDesktopProductReleaseContract({
      acceptedOpenApiDigests: ["a".repeat(64)],
      allowedProviderKinds: ["desktop_sidecar"],
      requiredFeatureFlags: ["remote_profiles", "run_observability"],
    });
    expect(contract.allowedProviderKinds).toEqual(["desktop_sidecar"]);
    expect(Object.isFrozen(contract.acceptedOpenApiDigests)).toBe(true);
  });

  it("fails closed when a Preview provider is unavailable", async () => {
    await expect(unavailableDesktopProductProvider.retryRun!("run-fixture-1", {
      actionId: "renderer-action-retry-0001",
      streamEpoch: 7,
      etag: `"${"a".repeat(64)}"`,
    })).rejects.toThrow("local service");
  });

  it("keeps fixture Core operations observable from queued to terminal", async () => {
    const provider = createFixtureDesktopProductProvider({ startOnline: true });
    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("expected a fresh fixture");
    const service = refreshed.snapshot.services.find((item) => item.id === "service-runtime-fixture");
    if (!service) throw new Error("expected the fixture runtime service");

    const operation = await provider.restartService(service.id, {
      actionId: "fixture-service-restart-0001",
      streamEpoch: refreshed.snapshot.stream.epoch,
      etag: service.etag,
    });
    expect(operation.status).toBe("queued");
    await expect(provider.getCoreOperation(operation.id)).resolves.toMatchObject({
      id: operation.id,
      status: "succeeded",
    });
    provider.dispose();
  });

  it("rejects an older refresh result after a newer refresh has started", () => {
    const order = new ProductRefreshOrder();
    const older = order.begin();
    const newer = order.begin();

    expect(order.isCurrent(newer)).toBe(true);
    expect(order.isCurrent(older)).toBe(false);
  });
});
