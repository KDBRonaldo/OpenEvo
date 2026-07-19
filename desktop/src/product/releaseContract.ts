import { defineDesktopProductReleaseContract } from "./provider";
import releaseContract from "../../release-contract.json";

// Updated only with the reviewed, checked-in Desktop Local API snapshot.
export const DESKTOP_PRODUCT_RELEASE_CONTRACT = defineDesktopProductReleaseContract({
  acceptedOpenApiDigests: releaseContract.accepted_openapi_digests as [string, ...string[]],
  allowedProviderKinds: releaseContract.allowed_provider_kinds as ["desktop_sidecar"],
  requiredFeatureFlags: releaseContract.required_feature_flags as (
    | "remote_profiles"
    | "project_validation"
    | "operation_events"
    | "run_observability"
    | "artifact_inspection"
    | "service_control"
    | "diagnostics"
    | "maintenance"
  )[],
});
