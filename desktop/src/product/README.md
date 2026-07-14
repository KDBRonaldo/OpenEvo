# Desktop Product Renderer Boundary

The product renderer consumes only `DesktopProductProvider`. Mutations carry the
renderer-observed stream epoch, resource ETag, and a stable action identity.
`startRun` intentionally carries only project identity and intent metadata; the
Local API owner must perform project snapshot, capability, validation, and
revision handshakes.

Release startup has one entry point: `createReleaseDesktopProductProvider`.
It accepts a provider only after the Tauri bootstrap and `DesktopApiClientV1`
agree on contract major, checked-in OpenAPI digest, provider kind, and required
features. The contract simulator is test-only and is not a release fallback.

The current native host does not yet return `DesktopBootstrapContextV1`, and no
release adapter implements the converged high-level run or native source
selection operations. Release startup therefore remains fail closed. Native
folder selection must eventually return only `ProjectSourceV1` with an opaque
`ContentRef`; renderer file inputs and raw filesystem strings are not accepted.
