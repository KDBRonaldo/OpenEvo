import { defineDesktopProductReleaseContract } from "./provider";

// Updated only with the reviewed, checked-in Desktop Local API snapshot.
export const DESKTOP_PRODUCT_RELEASE_CONTRACT = defineDesktopProductReleaseContract({
  acceptedOpenApiDigests: [
    "5a571f32c547063677533be9b4ccae417e2037b11963b5770d245f6c5419830e",
  ],
  allowedProviderKinds: ["desktop_sidecar"],
  requiredFeatureFlags: [
    "remote_profiles",
    "project_validation",
    "operation_events",
    "run_observability",
    "artifact_inspection",
    "service_control",
    "diagnostics",
  ],
});
