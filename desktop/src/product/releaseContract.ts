import { defineDesktopProductReleaseContract } from "./provider";

// Updated only with the reviewed, checked-in Desktop Local API snapshot.
export const DESKTOP_PRODUCT_RELEASE_CONTRACT = defineDesktopProductReleaseContract({
  acceptedOpenApiDigests: [
    "3a86582d04dcd233096337c737ba91d75854746848aedc319025d86213a03d36",
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
