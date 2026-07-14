import { describe, expect, it } from "vitest";
import {
  defineDesktopProductReleaseContract,
  type DesktopProductReleaseContract,
} from "./provider";
import { DESKTOP_PRODUCT_RELEASE_CONTRACT } from "./releaseContract";

describe("Desktop product provider boundary", () => {
  it("requires checked-in release digests and the native provider", () => {
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
    expect(DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests).toEqual([
      "5a571f32c547063677533be9b4ccae417e2037b11963b5770d245f6c5419830e",
    ]);
  });
});
