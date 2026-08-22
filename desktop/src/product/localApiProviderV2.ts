import { z } from "zod";
import type {
  DesktopApiClientV2,
  FetchLikeV2,
  ListRequestOptionsV2,
} from "../api/v2/client";
import {
  DesktopApiErrorV2,
  DesktopContractErrorV2,
} from "../api/v2/client";
import {
  canonicalJsonV2,
  compareUtcTimestampsV2,
  lifecycleOperationV2Schema,
  opaqueIdV2Schema,
  scienceProjectConfigV2Schema,
  sha256Utf8V2,
  type ArtifactV2,
  type CoreEventEnvelopeV2,
  type DesktopErrorV2,
  type DesktopEventEnvelopeV2,
  type DesktopStateV2,
  type DiagnosticV2,
  type LifecycleOperationKindV2,
  type LifecycleOperationV2,
  type LocalOperationV2,
  type OperationV2,
  type HostKeyReviewRequestV2,
  type ProjectV2,
  type RemoteProfileV2,
  type RemoteWorkspaceProfileV2,
  type ScienceProjectConfigV2,
  type ServiceV2,
  type SuccessorTransitionV2,
  type TaskV2,
} from "../api/v2/schemas";
import {
  DesktopEventReplayAuthorityV2,
  parseEventStreamFailureV2,
  parseSseFrameV2,
} from "../api/v2/sse";
import type {
  DesktopProductProviderV2,
  DesktopProductSnapshotV2,
  NativeWorkspaceSelectionIntentV2,
  NativeWorkspaceSourceV2,
  ProductMutationIntentV2,
  ProductOperationV2,
  ProductRefreshResultV2,
  ProductStreamStateV2,
  ProductSubscriptionSignalV2,
  ProjectDraftV2,
} from "./providerV2";
import {
  MutationIntentConflictV2,
  MutationIntentCoordinatorV2,
  type MutationChainStepV2,
  type MutationKindV2,
  type PendingMutationIntentV2,
} from "./mutationIntentJournalV2";
import {
  CoreOperationControllerV2,
  LifecycleOperationControllerV2,
  isLifecycleTerminalV2,
  type LifecycleOperationStateV2,
} from "./lifecycleOperationsV2";

const PAGE_LIMIT = 100;
const MAX_COLLECTION_PAGES = 100;
const MAX_REFRESH_RESOURCES = 20_000;
const MAX_SSE_BUFFER_BYTES = 1_048_580;
const DEFAULT_RECONNECT_DELAYS_MS = [250, 500, 1_000, 2_000, 4_000] as const;
const RESOURCE_POLL_DELAYS_MS = [500, 1_000, 2_000, 4_000] as const;

export interface LocalApiNativeBridgeV2 {
  selectProjectSource(intent: NativeWorkspaceSelectionIntentV2): Promise<unknown>;
  cancelProjectSource(actionId: string): Promise<unknown>;
  settleProjectSource(actionId: string, outcome: "adopt" | "discard"): Promise<unknown>;
  readMutationIntentJournalV2(): Promise<string | null>;
  compareAndSwapMutationIntentJournalV2(
    expectedValue: string | null,
    newValue: string | null,
  ): Promise<void>;
}

export interface LocalApiDesktopProductProviderOptionsV2 {
  readonly client: DesktopApiClientV2;
  readonly native: LocalApiNativeBridgeV2;
  readonly featureFlags: readonly string[];
  readonly providerStreamInstance: string;
  readonly fetch?: FetchLikeV2;
  readonly reconnectDelaysMs?: readonly number[];
}

export class LocalApiDesktopProductProviderV2 implements DesktopProductProviderV2 {
  readonly apiVersion = 2 as const;
  readonly providerKind = "desktop_sidecar" as const;
  readonly featureFlags: readonly string[];

  private readonly client: DesktopApiClientV2;
  private readonly native: LocalApiNativeBridgeV2;
  private readonly fetch: FetchLikeV2;
  private readonly reconnectDelaysMs: readonly number[];
  private readonly providerStreamInstance: string;
  private readonly mutationIntents: MutationIntentCoordinatorV2;
  private readonly lifecycleOperations: LifecycleOperationControllerV2;
  private readonly coreOperations: CoreOperationControllerV2;
  private readonly listeners = new Set<(signal: ProductSubscriptionSignalV2) => void>();
  private readonly replay = new DesktopEventReplayAuthorityV2();
  private readonly lifecyclePolls = new Map<string, Promise<void>>();
  private readonly corePolls = new Map<string, Promise<void>>();
  private readonly diagnostics = new Map<string, DiagnosticV2>();
  private readonly diagnosticPolls = new Map<string, Promise<void>>();
  private refreshSequence = 0;
  private epoch = 0;
  private snapshot: DesktopProductSnapshotV2 | null = null;
  private validation: DesktopProductSnapshotV2["validation"] = null;
  private activeOperation: ProductOperationV2 | null = null;
  private streamAbort: AbortController | null = null;
  private streamPromise: Promise<void> | null = null;
  private waitingForRefresh = false;

  constructor(options: LocalApiDesktopProductProviderOptionsV2) {
    this.client = options.client;
    this.native = options.native;
    this.featureFlags = Object.freeze([...options.featureFlags]);
    this.providerStreamInstance = opaqueIdV2Schema.parse(options.providerStreamInstance);
    this.mutationIntents = new MutationIntentCoordinatorV2(options.native);
    this.lifecycleOperations = new LifecycleOperationControllerV2(options.client);
    this.coreOperations = new CoreOperationControllerV2(options.client, () => this.activeCoreAuthorityV2());
    this.fetch = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.reconnectDelaysMs = options.reconnectDelaysMs ?? DEFAULT_RECONNECT_DELAYS_MS;
  }

  async refresh(): Promise<ProductRefreshResultV2> {
    const sequence = ++this.refreshSequence;
    // A refresh is the authority boundary for the event stream. Keep the
    // stream paused from the moment the snapshot load starts so a subscription
    // registered by the renderer cannot observe an event and supersede the
    // initial refresh before it publishes its first authoritative snapshot.
    this.waitingForRefresh = true;
    try {
      await this.mutationIntents.initialize();
      const loaded = await this.loadSnapshot();
      await this.lifecycleOperations.synchronize(loaded.state.pending_operations);
      for (const state of this.lifecycleOperations.list()) this.ensureLifecyclePollingV2(state.operation);
      if (sequence !== this.refreshSequence) {
        return { status: "stale", stream: { status: "stale", epoch: this.epoch, reason: "refresh_pending" } };
      }
      this.epoch += 1;
      let snapshot: DesktopProductSnapshotV2 = {
        ...loaded,
        activeOperation: latestLifecycleOperationV2(this.lifecycleOperations.list())?.operation ?? this.activeOperation,
        stream: { status: "fresh", epoch: this.epoch, lastEventId: this.replay.lastEventId },
      };
      this.snapshot = snapshot;
      await this.reconcilePendingOperationsV2(snapshot);
      for (const operation of this.coreOperations.list()) this.ensureCorePollingV2(operation);
      for (const diagnostic of this.diagnostics.values()) this.ensureDiagnosticPollingV2(diagnostic);
      snapshot = this.snapshot ?? snapshot;
      this.waitingForRefresh = false;
      this.ensureEventStream();
      return { status: "fresh", snapshot };
    } catch (error) {
      if (
        import.meta.env.DEV
        || import.meta.env.VITE_OPENEVO_SOURCE_DEVELOPMENT === "1"
      ) {
        console.error("OpenEvo Desktop authoritative refresh failed", error);
      }
      if (sequence !== this.refreshSequence) {
        return { status: "stale", stream: { status: "stale", epoch: this.epoch, reason: "refresh_pending" } };
      }
      const stream: Extract<ProductStreamStateV2, { status: "error" }> = {
        status: "error",
        epoch: this.epoch,
        error: apiErrorOfV2(error),
      };
      this.setSnapshotStream(stream);
      return { status: "error", stream };
    }
  }

  subscribe(listener: (signal: ProductSubscriptionSignalV2) => void): () => void {
    this.listeners.add(listener);
    this.ensureEventStream();
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) {
        this.streamAbort?.abort();
        this.streamAbort = null;
      }
    };
  }

  async rescanSshHosts(intent: ProductMutationIntentV2) {
    const snapshot = this.requireIntent(intent);
    const request = { schema_version: "2" as const };
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "ssh_catalog_rescan",
      resourceScope: "catalog:ssh",
      request,
      authority: { resource_generation: snapshot.catalog.catalog_generation, etag: null },
      send: (actionId) => this.client.rescanSshHosts(request, {
        resourceGeneration: snapshot.catalog.catalog_generation,
        idempotencyKey: actionId,
      }),
    });
    const catalog = dispatched.value;
    await this.completeDirectMutationV2(dispatched.entry, catalog, async () => {
      const authoritative = await this.client.listSshHosts();
      if (authoritative.catalog_generation < catalog.catalog_generation) {
        throw new DesktopContractErrorV2("SSH catalog rescan is absent from authoritative Desktop state");
      }
    });
    this.invalidate();
    return catalog;
  }

  async createProfile(displayName: string, sshHostAlias: string, intent: ProductMutationIntentV2) {
    const snapshot = this.requireIntent(intent);
    const request = {
      schema_version: "2",
      display_name: displayName,
      connection_authority: "system_openssh",
      ssh_host_alias: sshHostAlias,
    } as const;
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "profile_create",
      resourceScope: `profile:new:${sshHostAlias}`,
      request,
      authority: { resource_generation: snapshot.catalog.catalog_generation, etag: null },
      send: (actionId) => this.client.createProfile(request, {
        resourceGeneration: snapshot.catalog.catalog_generation,
        idempotencyKey: actionId,
      }),
    });
    const profile = dispatched.value;
    await this.completeDirectMutationV2(dispatched.entry, profile, async () => {
      const authoritative = await this.client.getProfile(profile.profile_id);
      if (authoritative.profile_kind !== "system_openssh"
        || authoritative.display_name !== profile.display_name
        || authoritative.ssh_host_alias !== profile.ssh_host_alias) {
        throw new DesktopContractErrorV2("Created profile is absent from authoritative Desktop state");
      }
    });
    this.invalidate();
    return profile;
  }

  async renameProfile(profileId: string, input: { schema_version?: "2"; display_name: string }, intent: ProductMutationIntentV2) {
    const profile = this.requireProfile(profileId, intent);
    const snapshot = this.requireSnapshot();
    const generation = profile.profile_kind === "system_openssh" ? profile.connection_generation : 0;
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "profile_update",
      resourceScope: `profile:${profileId}`,
      request: input,
      authority: { resource_generation: generation, etag: profile.etag },
      send: (actionId) => this.client.updateProfile(profileId, input, {
        resourceGeneration: generation,
        ifMatch: profile.etag,
        idempotencyKey: actionId,
      }),
    });
    const result = dispatched.value;
    await this.completeDirectMutationV2(dispatched.entry, result, async () => {
      const authoritative = await this.client.getProfile(profileId);
      if (authoritative.profile_id !== result.profile_id
        || authoritative.display_name !== result.display_name) {
        throw new DesktopContractErrorV2("Renamed profile is absent from authoritative Desktop state");
      }
    });
    this.invalidate();
    return result;
  }

  async deleteProfile(profileId: string, intent: ProductMutationIntentV2): Promise<void> {
    const profile = this.requireProfile(profileId, intent);
    const snapshot = this.requireSnapshot();
    const generation = profile.profile_kind === "system_openssh" ? profile.connection_generation : 0;
    const request = { schema_version: "2", expected_profile_id: profileId } as const;
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "profile_delete",
      resourceScope: `profile:${profileId}`,
      request,
      authority: { resource_generation: generation, etag: profile.etag },
      send: async (actionId) => {
        await this.client.deleteProfile(profileId, {
          resourceGeneration: generation,
          ifMatch: profile.etag,
          idempotencyKey: actionId,
        });
        return null;
      },
    });
    await this.completeDirectMutationV2(dispatched.entry, null, async () => {
      const profiles = await collectPages((options) => this.client.listProfiles(options));
      if (profiles.some((candidate) => candidate.profile_id === profileId)) {
        throw new DesktopContractErrorV2("Deleted profile remains in authoritative Desktop state");
      }
    });
    this.invalidate();
  }

  async rebindProfile(profileId: string, sshHostAlias: string, intent: ProductMutationIntentV2) {
    const snapshot = this.requireIntent(intent);
    const profile = snapshot.profiles.find((candidate) => candidate.profile_id === profileId);
    if (profile?.profile_kind !== "legacy_explicit") throw new DesktopContractErrorV2("Only a retained Preview profile can be rebound");
    const request = {
      schema_version: "2",
      connection_authority: "system_openssh",
      ssh_host_alias: sshHostAlias,
      catalog_generation: snapshot.catalog.catalog_generation,
    } as const;
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "profile_rebind",
      resourceScope: `profile:${profileId}`,
      request,
      authority: { resource_generation: 0, etag: profile.etag },
      send: (actionId) => this.client.rebindProfile(profileId, request, {
        resourceGeneration: 0,
        ifMatch: profile.etag,
        idempotencyKey: actionId,
      }),
    });
    const result = dispatched.value;
    await this.completeDirectMutationV2(dispatched.entry, result, async () => {
      const authoritative = await this.client.getProfile(profileId);
      if (authoritative.profile_kind !== "system_openssh"
        || authoritative.profile_id !== result.profile_id
        || authoritative.ssh_host_alias !== result.ssh_host_alias) {
        throw new DesktopContractErrorV2("Rebound profile is absent from authoritative Desktop state");
      }
    });
    this.invalidate();
    return result;
  }

  async connectProfile(profileId: string, intent: ProductMutationIntentV2) {
    const profile = this.requireSystemProfile(profileId, intent);
    const snapshot = this.requireSnapshot();
    const request = {
      schema_version: "2",
      expected_connection_generation: profile.connection_generation,
    } as const;
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "profile_connect",
      resourceScope: `profile:${profileId}`,
      request,
      authority: { resource_generation: profile.connection_generation, etag: profile.etag },
      operationAuthority: "lifecycle",
      send: (actionId) => this.client.connectProfile(profileId, request, {
        resourceGeneration: profile.connection_generation,
        ifMatch: profile.etag,
        idempotencyKey: actionId,
      }),
    });
    return this.observeOperation(dispatched.value);
  }

  async disconnectProfile(profileId: string, intent: ProductMutationIntentV2) {
    const profile = this.requireSystemProfile(profileId, intent);
    const snapshot = this.requireSnapshot();
    const request = {
      schema_version: "2",
      expected_connection_generation: profile.connection_generation,
    } as const;
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "profile_disconnect",
      resourceScope: `profile:${profileId}`,
      request,
      authority: { resource_generation: profile.connection_generation, etag: profile.etag },
      operationAuthority: "lifecycle",
      send: (actionId) => this.client.disconnectProfile(profileId, request, {
        resourceGeneration: profile.connection_generation,
        ifMatch: profile.etag,
        idempotencyKey: actionId,
      }),
    });
    return this.observeOperation(dispatched.value);
  }

  async reviewHostKey(profileId: string, action: HostKeyReviewRequestV2["action"], intent: ProductMutationIntentV2) {
    const profile = this.requireSystemProfile(profileId, intent);
    const reviewId = profile.trust.review_id;
    const reviewSha256 = profile.trust.review_sha256;
    if (reviewId === null || reviewSha256 === null) throw new DesktopContractErrorV2("Profile has no current host-key review authority");
    const snapshot = this.requireSnapshot();
    const request = {
      schema_version: "2",
      expected_connection_generation: profile.connection_generation,
      review_id: reviewId,
      review_sha256: reviewSha256,
      action,
    } as const;
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "host_key_review",
      resourceScope: `profile:${profileId}`,
      request,
      authority: { resource_generation: profile.connection_generation, etag: profile.etag },
      operationAuthority: "lifecycle",
      send: (actionId) => this.client.reviewProfileHostKey(profileId, request, {
        resourceGeneration: profile.connection_generation,
        ifMatch: profile.etag,
        idempotencyKey: actionId,
      }),
    });
    return this.observeOperation(dispatched.value);
  }

  listLifecycleOperations(): readonly LifecycleOperationStateV2[] {
    const pendingIds = new Set(this.snapshot?.state.pending_operations.map((operation) => operation.operation_id) ?? []);
    const unresolvedIds = new Set(this.mutationIntents.list().flatMap((entry) => [
      ...(entry.accepted_operation_id === null ? [] : [entry.accepted_operation_id]),
      ...entry.completed_operation_ids,
    ]));
    return this.lifecycleOperations.list().filter((state) => pendingIds.has(state.operation.operation_id)
      || unresolvedIds.has(state.operation.operation_id)
      || !isLifecycleTerminalV2(state.operation));
  }

  async getLifecycleOperation(operationId: string): Promise<LifecycleOperationV2> {
    return this.lifecycleOperations.refresh(opaqueIdV2Schema.parse(operationId));
  }

  async loadLifecycleLogs(operationId: string): Promise<LifecycleOperationStateV2> {
    const id = opaqueIdV2Schema.parse(operationId);
    if (this.lifecycleOperations.get(id) === null) await this.lifecycleOperations.refresh(id);
    return this.lifecycleOperations.loadLogs(id);
  }

  async loadOlderLifecycleLogs(operationId: string): Promise<LifecycleOperationStateV2> {
    const id = opaqueIdV2Schema.parse(operationId);
    if (this.lifecycleOperations.get(id) === null) await this.lifecycleOperations.refresh(id);
    return this.lifecycleOperations.loadOlderLogs(id);
  }

  async loadLatestLifecycleLogs(operationId: string): Promise<LifecycleOperationStateV2> {
    const id = opaqueIdV2Schema.parse(operationId);
    if (this.lifecycleOperations.get(id) === null) await this.lifecycleOperations.refresh(id);
    return this.lifecycleOperations.loadLatestLogs(id);
  }

  async cancelLifecycleOperation(operationId: string, intent: ProductMutationIntentV2): Promise<LifecycleOperationV2> {
    const snapshot = this.requireIntent(intent);
    const id = opaqueIdV2Schema.parse(operationId);
    const resourceScope = `lifecycle_operation:${id}`;
    const reserved = this.mutationIntents.list().find((entry) => entry.state === "reserved"
      && entry.mutation_kind === "lifecycle_cancel"
      && entry.resource_scope === resourceScope);
    if (reserved !== undefined) {
      return this.observeOperation(await this.replayReservedLifecycleCancellationV2(reserved));
    }
    const current = this.lifecycleOperations.get(id)?.operation ?? await this.lifecycleOperations.refresh(id);
    const request = { schema_version: "2", expected_operation_id: id } as const;
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "lifecycle_cancel",
      resourceScope,
      request,
      authority: { resource_generation: 0, etag: current.etag },
      send: (actionId) => this.sendLifecycleCancellationWithRefreshV2(id, actionId),
    });
    await this.completeDirectMutationV2(dispatched.entry, dispatched.value, async () => {
      await this.lifecycleOperations.refresh(id);
    });
    return this.observeOperation(dispatched.value);
  }

  listMutationIntents(): readonly PendingMutationIntentV2[] {
    return this.mutationIntents.list();
  }

  async resumeMutationIntent(actionId: string): Promise<void> {
    const action = actionIdV2(actionId);
    let entry = this.mutationIntents.list().find((candidate) => candidate.action_id === action);
    if (entry === undefined) return;
    if (entry.accepted_operation_id === null) {
      const recovered = await this.recoverReservedLifecycleOperationV2(entry);
      if (recovered === null) {
        if (await this.reconcileReservedCancellationV2(entry)) return;
        throw new MutationIntentConflictV2(
          "Return to the original action to retry this exact unresolved mutation",
          entry,
        );
      }
      entry = recovered.entry;
    }
    const operationId = entry.accepted_operation_id;
    if (operationId === null) {
      throw new DesktopContractErrorV2("Recovered mutation has no operation authority");
    }
    if (isDesktopLifecycleMutationV2(entry.mutation_kind)) {
      const operation = await this.lifecycleOperations.refresh(operationId);
      if (!isLifecycleTerminalV2(operation)) {
        await this.lifecycleOperations.pollUntilTerminal(operation.operation_id);
      }
    } else if (entry.mutation_kind === "diagnostic_create") {
      const diagnostic = await this.refreshDiagnosticV2(operationId);
      if (!isDiagnosticTerminalV2(diagnostic)) {
        await this.pollDiagnosticUntilTerminalV2(diagnostic.diagnostic_id);
      }
    } else {
      const operation = await this.coreOperations.refresh(operationId);
      if (!isCoreOperationTerminalV2(operation)) {
        await this.coreOperations.pollUntilTerminal(operation.operation_id);
      }
    }
    const refreshed = await this.refresh();
    if (refreshed.status !== "fresh") {
      throw new DesktopContractErrorV2("Mutation reconciliation could not refresh authoritative Desktop state");
    }
  }

  async selectNativeWorkspace(intent: NativeWorkspaceSelectionIntentV2): Promise<NativeWorkspaceSourceV2> {
    const profile = this.requireSystemProfile(intent.draft.profileId, intent);
    const snapshot = this.requireSnapshot();
    if (profile.connection_state !== "connected") {
      throw new DesktopContractErrorV2("Native workspace preparation requires a connected system-OpenSSH profile");
    }
    if (intent.profileAuthority.profileId !== profile.profile_id
      || intent.profileAuthority.connectionGeneration !== profile.connection_generation
      || intent.profileAuthority.etag !== profile.etag) {
      throw new DesktopContractErrorV2("Native workspace profile authority changed before folder selection");
    }
    const request = projectCreateRequestV2(intent.draft, profile);
    if (request.config.workspace.kind !== "native_folder_snapshot") {
      throw new DesktopContractErrorV2("Native workspace selection requires a native-folder project draft");
    }
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "project_create",
      resourceScope: `project:new:${profile.profile_id}`,
      request,
      authority: { resource_generation: profile.connection_generation, etag: profile.etag },
      chainStep: "native_workspace_prepare",
      includeStreamAuthority: false,
      operationAuthority: "lifecycle",
      send: async (actionId) => lifecycleOperationV2Schema.parse(await this.native.selectProjectSource({
        ...intent,
        actionId,
      })),
    });
    const operation = dispatched.value;
    if (operation.kind !== "native_workspace_prepare"
      || operation.resource.resource_kind !== "native_workspace") {
      throw new DesktopContractErrorV2("Native workspace selection returned another lifecycle operation");
    }
    const terminal = await this.waitForLifecycleTerminal(this.observeOperation(operation));
    if (terminal.status !== "succeeded" || terminal.result?.result_kind !== "native_workspace") {
      await this.completeTerminalOperationV2(dispatched.entry, terminal.operation_id);
      await this.acknowledgeLifecycleTerminalV2(terminal);
      throw lifecycleTerminalError(terminal, "Native workspace preparation did not succeed");
    }
    await this.mutationIntents.markTerminalObserved(dispatched.entry.action_id, terminal.operation_id);
    await this.mutationIntents.advanceNativeProjectChain(dispatched.entry.action_id, terminal.operation_id);
    await this.acknowledgeLifecycleTerminalV2(terminal);
    return { kind: "native_folder_snapshot", display_name: terminal.result.display_name };
  }

  async cancelNativeWorkspace(actionId: string): Promise<void> {
    await this.native.cancelProjectSource(actionIdV2(actionId));
  }

  async settleNativeWorkspace(actionId: string, outcome: "adopt" | "discard"): Promise<void> {
    const action = actionIdV2(actionId);
    await this.native.settleProjectSource(action, outcome);
    if (outcome === "discard") await this.mutationIntents.discardNativeProjectChain(action);
  }

  async createProject(draft: ProjectDraftV2, intent: ProductMutationIntentV2) {
    const profile = this.requireSystemProfile(draft.profileId, intent);
    const snapshot = this.requireSnapshot();
    if (profile.connection_state !== "connected") throw new DesktopContractErrorV2("Project creation requires a connected system-OpenSSH profile");
    const request = projectCreateRequestV2(draft, profile);
    const nativeProjectChain = request.config.workspace.kind === "native_folder_snapshot";
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "project_create",
      resourceScope: `project:new:${profile.profile_id}`,
      request,
      authority: { resource_generation: profile.connection_generation, etag: profile.etag },
      chainStep: nativeProjectChain ? "project_create" : "single",
      includeStreamAuthority: !nativeProjectChain,
      operationAuthority: "lifecycle",
      send: (actionId) => this.client.createProject(request, {
        resourceGeneration: profile.connection_generation,
        idempotencyKey: actionId,
      }),
    });
    return this.observeOperation(dispatched.value);
  }

  async updateProject(projectId: string, displayName: string, configInput: ScienceProjectConfigV2, intent: ProductMutationIntentV2) {
    const project = this.requireProject(projectId, intent);
    const snapshot = this.requireSnapshot();
    const config = scienceProjectConfigV2Schema.parse(configInput);
    const head = project.active_project_head;
    const request = {
      schema_version: "2",
      expected_project_head_id: head?.project_head_id ?? null,
      expected_project_head_manifest_sha256: head?.manifest_sha256 ?? null,
      expected_project_config_sha256: project.project_config_sha256,
      display_name: displayName,
      config,
    } as const;
    const generation = project.active_project_head?.generation ?? 0;
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "project_update",
      resourceScope: `project:${projectId}`,
      request,
      authority: { resource_generation: generation, etag: project.etag },
      send: (actionId) => this.client.updateProject(projectId, request, {
        resourceGeneration: generation,
        ifMatch: project.etag,
        idempotencyKey: actionId,
      }),
    });
    const result = dispatched.value;
    await this.completeDirectMutationV2(dispatched.entry, result, async () => {
      const authoritative = await this.client.getProject(projectId);
      if (authoritative.project_id !== result.project_id
        || authoritative.display_name !== result.display_name
        || authoritative.project_config_sha256 !== result.project_config_sha256) {
        throw new DesktopContractErrorV2("Updated project is absent from authoritative Core state");
      }
    });
    this.validation = null;
    this.invalidate();
    return result;
  }

  async activateProject(projectId: string, intent: ProductMutationIntentV2) {
    const project = this.requireProject(projectId, intent);
    const snapshot = this.requireSnapshot();
    const head = requireProjectHead(project);
    const request = {
      schema_version: "2",
      expected_project_head_id: head.project_head_id,
      expected_project_head_manifest_sha256: head.manifest_sha256,
    } as const;
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "project_activate",
      resourceScope: `project:${projectId}`,
      request,
      authority: { resource_generation: head.generation, etag: project.etag },
      operationAuthority: "lifecycle",
      send: (actionId) => this.client.activateProject(projectId, request, {
        resourceGeneration: head.generation,
        ifMatch: project.etag,
        idempotencyKey: actionId,
      }),
    });
    return this.observeOperation(dispatched.value);
  }

  async loadProjectCapabilities(projectId: string) {
    const snapshot = this.requireSnapshot();
    const project = snapshot.projects.find((candidate) => candidate.project_id === projectId);
    if (!project) throw new DesktopContractErrorV2("Project capabilities reference an unknown project");
    const capability = await this.client.projectCapabilities(projectId);
    this.snapshot = this.snapshot === null ? null : { ...this.snapshot, capability };
    return capability;
  }

  async validateProject(projectId: string, intent: ProductMutationIntentV2) {
    const project = this.requireProject(projectId, intent);
    const snapshot = this.requireSnapshot();
    const head = requireProjectHead(project);
    const capability = snapshot.capability;
    if (capability === null || capability.project_id !== projectId || capability.execution_mode !== project.config.execution.mode) {
      throw new DesktopContractErrorV2("Project validation requires current remote capabilities");
    }
    const request = {
      schema_version: "2",
      expected_project_head_id: head.project_head_id,
      expected_project_head_manifest_sha256: head.manifest_sha256,
      expected_project_config_sha256: project.project_config_sha256,
      capability_registry_sha256: capability.registry_sha256,
    } as const;
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "project_validate",
      resourceScope: `project:${projectId}`,
      request,
      authority: { resource_generation: head.generation, etag: project.etag },
      send: (actionId) => this.client.validateProject(projectId, request, {
        resourceGeneration: head.generation,
        ifMatch: project.etag,
        idempotencyKey: actionId,
      }),
    });
    const validation = dispatched.value;
    await this.completeDirectMutationV2(dispatched.entry, validation);
    this.validation = validation;
    if (this.snapshot !== null) this.snapshot = { ...this.snapshot, validation };
    return validation;
  }

  async submitTask(projectId: string, intent: ProductMutationIntentV2) {
    const project = this.requireProject(projectId, intent);
    const snapshot = this.requireSnapshot();
    const head = requireProjectHead(project);
    if (project.state !== "ready" || project.admission_etag === null) {
      throw new DesktopContractErrorV2("Project successor is not ready for task admission");
    }
    const capability = snapshot.capability;
    const validation = this.validation;
    if (capability === null || validation === null || validation.project_id !== projectId
      || validation.registry_sha256 !== capability.registry_sha256 || !validation.valid) {
      throw new DesktopContractErrorV2("Project must pass current remote validation before task admission");
    }
    const request = {
      schema_version: "2",
      project_id: projectId,
      expected_project_admission_etag: project.admission_etag,
      expected_project_head_id: head.project_head_id,
      expected_project_head_manifest_sha256: head.manifest_sha256,
      expected_project_config_sha256: project.project_config_sha256,
    } as const;
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "task_submit",
      resourceScope: `project:${projectId}:task:new`,
      request,
      authority: { resource_generation: head.generation, etag: project.admission_etag },
      send: (actionId) => this.client.submitTask(request, {
        resourceGeneration: head.generation,
        idempotencyKey: actionId,
      }),
    });
    const task = dispatched.value;
    await this.completeDirectMutationV2(dispatched.entry, task, async () => {
      const authoritative = await this.client.getTask(task.task_id);
      if (authoritative.task_id !== task.task_id
        || authoritative.admission.admission_sha256 !== task.admission.admission_sha256) {
        throw new DesktopContractErrorV2("Submitted Task is absent from authoritative Core state");
      }
    });
    this.invalidate();
    return task;
  }

  async cancelTask(taskId: string, intent: ProductMutationIntentV2) {
    const task = this.requireTask(taskId, intent);
    const snapshot = this.requireSnapshot();
    const request = taskAction(task);
    const mutation = taskMutationAuthority(task);
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "task_cancel",
      resourceScope: `task:${taskId}`,
      request,
      authority: mutation,
      operationAuthority: "core",
      send: (actionId) => this.client.cancelTask(taskId, request, {
        resourceGeneration: mutation.resource_generation,
        ifMatch: mutation.etag,
        idempotencyKey: actionId,
      }),
    });
    return this.observeOperation(dispatched.value);
  }

  async retryTask(taskId: string, intent: ProductMutationIntentV2) {
    const task = this.requireTask(taskId, intent);
    const snapshot = this.requireSnapshot();
    const request = taskAction(task);
    const mutation = taskMutationAuthority(task);
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "task_retry",
      resourceScope: `task:${taskId}`,
      request,
      authority: mutation,
      send: (actionId) => this.client.retryTask(taskId, request, {
        resourceGeneration: mutation.resource_generation,
        ifMatch: mutation.etag,
        idempotencyKey: actionId,
      }),
    });
    await this.completeDirectMutationV2(dispatched.entry, dispatched.value, async () => {
      const authoritative = await this.client.getTask(taskId);
      const expectedOrdinal = task.attempts.length + 1;
      if (authoritative.admission.admission_sha256 !== task.admission.admission_sha256
        || authoritative.attempts.at(-1)?.ordinal !== expectedOrdinal) {
        throw new DesktopContractErrorV2("Retried Task Attempt is absent from authoritative Core state");
      }
    });
    return this.observeOperation(dispatched.value);
  }

  async getProjectHead(projectHeadId: string) {
    return this.client.getProjectHead(projectHeadId);
  }

  async getEvolutionRevision(evolutionRevisionId: string) {
    return this.client.getEvolutionRevision(evolutionRevisionId);
  }

  async getRuntimeContext(runtimeContextSnapshotId: string) {
    return this.client.getRuntimeContext(runtimeContextSnapshotId);
  }

  async retryTransition(transitionId: string, intent: ProductMutationIntentV2) {
    const { transition, project } = this.requireTransition(transitionId, intent);
    const snapshot = this.requireSnapshot();
    const request = transitionAction(transition);
    const authority = transitionMutationAuthority(transition, project);
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "transition_retry",
      resourceScope: `transition:${transitionId}`,
      request,
      authority,
      operationAuthority: "core",
      send: (actionId) => this.client.retryTransition(transitionId, request, {
        resourceGeneration: authority.resource_generation,
        ifMatch: authority.etag,
        idempotencyKey: actionId,
      }),
    });
    return this.observeOperation(dispatched.value);
  }

  async replaceTransition(transitionId: string, intent: ProductMutationIntentV2) {
    const { transition, project } = this.requireTransition(transitionId, intent);
    const snapshot = this.requireSnapshot();
    const request = {
      ...transitionAction(transition),
      replacement_plan_sha256: transition.transition.plan_sha256,
    };
    const authority = transitionMutationAuthority(transition, project);
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "transition_replace",
      resourceScope: `transition:${transitionId}`,
      request,
      authority,
      send: (actionId) => this.client.replaceTransition(transitionId, request, {
        resourceGeneration: authority.resource_generation,
        ifMatch: authority.etag,
        idempotencyKey: actionId,
      }),
    });
    await this.completeDirectMutationV2(dispatched.entry, dispatched.value, async () => {
      const authoritative = await this.client.getTransition(transitionId);
      if (authoritative.transition.successor_transition_id !== transitionId) {
        throw new DesktopContractErrorV2("Replaced successor transition is absent from authoritative Core state");
      }
    });
    return this.observeOperation(dispatched.value);
  }

  async abandonTransition(transitionId: string, intent: ProductMutationIntentV2) {
    const { transition, project } = this.requireTransition(transitionId, intent);
    const snapshot = this.requireSnapshot();
    const request = transitionAction(transition);
    const authority = transitionMutationAuthority(transition, project);
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "transition_abandon",
      resourceScope: `transition:${transitionId}`,
      request,
      authority,
      operationAuthority: "core",
      send: (actionId) => this.client.abandonTransition(transitionId, request, {
        resourceGeneration: authority.resource_generation,
        ifMatch: authority.etag,
        idempotencyKey: actionId,
      }),
    });
    return this.observeOperation(dispatched.value);
  }

  async getArtifactContent(artifactId: string) {
    this.requireSnapshot().artifacts.find((artifact) => artifact.artifact_id === artifactId) ?? fail("Artifact content references an unknown artifact");
    return this.client.artifactContent(artifactId);
  }

  async getArtifactDiff(artifactId: string, previousArtifactId?: string) {
    this.requireSnapshot().artifacts.find((artifact) => artifact.artifact_id === artifactId) ?? fail("Artifact diff references an unknown artifact");
    return this.client.artifactDiff(artifactId, previousArtifactId === undefined ? undefined : { previousArtifactId });
  }

  async restartService(serviceId: string, intent: ProductMutationIntentV2) {
    const snapshot = this.requireIntent(intent);
    const service = snapshot.services.find((candidate) => candidate.service_id === serviceId);
    if (!service) throw new DesktopContractErrorV2("Service restart references an unknown service");
    const profile = activeConnectedProfile(snapshot);
    const request = {
      schema_version: "2",
      expected_service_id: serviceId,
    } as const;
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "service_restart",
      resourceScope: `service:${serviceId}`,
      request,
      authority: { resource_generation: profile.connection_generation, etag: service.etag },
      operationAuthority: "core",
      send: (actionId) => this.client.restartService(serviceId, request, {
        resourceGeneration: profile.connection_generation,
        ifMatch: service.etag,
        idempotencyKey: actionId,
      }),
    });
    return this.observeOperation(dispatched.value);
  }

  listCoreOperations(): readonly OperationV2[] {
    const unresolvedIds = new Set(this.mutationIntents.list().flatMap((entry) => [
      ...(entry.accepted_operation_id === null ? [] : [entry.accepted_operation_id]),
      ...entry.completed_operation_ids,
    ]));
    return this.coreOperations.list().filter((operation) => unresolvedIds.has(operation.operation_id)
      || !isCoreOperationTerminalV2(operation)
      || this.activeOperation?.operation_id === operation.operation_id);
  }

  async getCoreOperation(operationId: string): Promise<OperationV2> {
    return this.coreOperations.refresh(opaqueIdV2Schema.parse(operationId));
  }

  async cancelCoreOperation(operationId: string, intent: ProductMutationIntentV2): Promise<OperationV2> {
    const snapshot = this.requireIntent(intent);
    const id = opaqueIdV2Schema.parse(operationId);
    const current = this.coreOperations.get(id) ?? await this.coreOperations.refresh(id);
    const authority = this.activeCoreAuthorityV2();
    if (authority === null) throw new DesktopContractErrorV2("Core cancellation requires an active project tunnel");
    const request = { schema_version: "2", expected_operation_id: id } as const;
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "core_operation_cancel",
      resourceScope: `core_operation:${id}`,
      request,
      authority: { resource_generation: authority.resourceGeneration, etag: current.etag },
      send: (actionId) => this.coreOperations.cancel(id, actionId),
    });
    await this.completeDirectMutationV2(dispatched.entry, dispatched.value, async () => {
      await this.coreOperations.refresh(id);
    });
    return this.observeOperation(dispatched.value);
  }

  async loadTaskLogs(taskId: string, options?: ListRequestOptionsV2) {
    const id = opaqueIdV2Schema.parse(taskId);
    if (!this.requireSnapshot().tasks.some((task) => task.task_id === id)) {
      throw new DesktopContractErrorV2("Task logs reference an unknown active Task");
    }
    return this.client.taskLogs(id, options);
  }

  async loadServiceLogs(serviceId: string, options?: ListRequestOptionsV2) {
    const id = opaqueIdV2Schema.parse(serviceId);
    if (!this.requireSnapshot().services.some((service) => service.service_id === id)) {
      throw new DesktopContractErrorV2("Service logs reference an unknown active service");
    }
    return this.client.serviceLogs(id, options);
  }

  async cleanupCaches(intent: ProductMutationIntentV2): Promise<OperationV2> {
    const snapshot = this.requireIntent(intent);
    const authority = this.activeCoreAuthorityV2();
    if (authority === null) throw new DesktopContractErrorV2("Cache cleanup requires an active project tunnel");
    const request = { schema_version: "2", scope: "safe_unreferenced" } as const;
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "cache_cleanup",
      resourceScope: `maintenance:${snapshot.state.active_project_id ?? "none"}`,
      request,
      authority: { resource_generation: authority.resourceGeneration, etag: null },
      operationAuthority: "core",
      send: (actionId) => this.client.cleanupCaches(request, {
        resourceGeneration: authority.resourceGeneration,
        idempotencyKey: actionId,
      }),
    });
    return this.observeOperation(dispatched.value);
  }

  async createDiagnostic(
    input: { scope: "system" | "project" | "task" | "transition" | "service"; resource_id: string | null },
    intent: ProductMutationIntentV2,
  ) {
    const snapshot = this.requireIntent(intent);
    const profile = activeConnectedProfile(snapshot);
    const request = {
      schema_version: "2",
      profile_id: profile.profile_id,
      profile_connection_generation: profile.connection_generation,
      scope: input.scope,
      resource_id: input.resource_id,
    } as const;
    const dispatched = await this.dispatchMutationV2({
      snapshot,
      intent,
      mutationKind: "diagnostic_create",
      resourceScope: `diagnostic:${input.scope}:${input.resource_id ?? "system"}`,
      request,
      authority: { resource_generation: profile.connection_generation, etag: null },
      operationAuthority: "diagnostic",
      send: (actionId) => this.client.createDiagnostic(request, {
        resourceGeneration: profile.connection_generation,
        idempotencyKey: actionId,
      }),
    });
    return this.observeDiagnosticV2(dispatched.value);
  }

  listDiagnostics(): readonly DiagnosticV2[] {
    return Object.freeze([...this.diagnostics.values()]
      .sort((left, right) => compareUtcTimestampsV2(left.created_at, right.created_at)));
  }

  async getDiagnostic(diagnosticId: string) {
    return this.refreshDiagnosticV2(opaqueIdV2Schema.parse(diagnosticId));
  }

  private async loadSnapshot(): Promise<Omit<DesktopProductSnapshotV2, "activeOperation" | "stream">> {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await this.loadSnapshotAttemptV2();
      } catch (error) {
        // Project creation changes the bridge tunnel and the persisted local
        // authority in two serialized steps.  A refresh that began just before
        // the lifecycle reservation can still hit that very small hand-off
        // window, so re-read local authority instead of surfacing a false 409.
        const retryableAuthorityRead = error instanceof DesktopApiErrorV2
          && (error.apiError.code === "active_project_mismatch"
            || (error.apiError.retryable && [502, 503, 504].includes(error.status)));
        if (!retryableAuthorityRead || attempt >= 5) throw error;
        // A Task admission or project activation can rotate the verified Core
        // authority while the renderer is loading its read model.  The remote
        // mutation is already durable at that point; treat a short tunnel or
        // service hand-off as an internal snapshot retry instead of telling the
        // user that the Project/Session failed.  Keep the bound small so a real
        // outage is still surfaced promptly.
        await new Promise<void>((resolve) => {
          globalThis.setTimeout(resolve, Math.min(800, 50 * (2 ** attempt)));
        });
      }
    }
  }

  private async loadSnapshotAttemptV2(): Promise<Omit<DesktopProductSnapshotV2, "activeOperation" | "stream">> {
    let [state, catalog, profiles] = await Promise.all([
      this.client.state(),
      this.client.listSshHosts(),
      collectPages((options) => this.client.listProfiles(options)),
    ]);
    try {
      assertProfileAuthority(state, profiles);
    } catch (error) {
      if (!(error instanceof DesktopContractErrorV2)) throw error;
      [state, catalog, profiles] = await Promise.all([
        this.client.state(),
        this.client.listSshHosts(),
        collectPages((options) => this.client.listProfiles(options)),
      ]);
      assertProfileAuthority(state, profiles);
    }
    if (state.active_project_id === null) {
      return this.localOnlySnapshot(state, catalog, profiles);
    }
    const activeProfile = profiles.find((candidate) => candidate.profile_id === state.active_profile_id);
    if (activeProfile?.profile_kind !== "system_openssh") {
      throw new DesktopContractErrorV2("Active project authority has no system-OpenSSH profile");
    }
    if (activeProfile.connection_state !== "connected") {
      return this.localOnlySnapshot(state, catalog, profiles);
    }
    if (hasUnboundProjectTunnelTransitionV2(state)) {
      // The lifecycle operation is authoritative while the bridge changes
      // tunnels.  Do not issue old-project Core reads through the new tunnel;
      // the operation poll will trigger another snapshot as soon as the local
      // active project binding catches up.
      return this.localOnlySnapshot(state, catalog, profiles);
    }
    activeConnectedProfile({ state, profiles });
    // All remote reads share one generation-bound system-OpenSSH tunnel.  Keep
    // one ordered read stream here, matching the proven development daemon's
    // single-state-snapshot behavior.  Parallel HTTP reads caused intermittent
    // 503s from an otherwise healthy tunnel immediately after Task admission.
    const projects = await collectPages((options) => this.client.listProjects(options));
    const tasks = await collectPages((options) => this.client.listTasks({
      ...options,
      projectId: state.active_project_id!,
    }));
    const services = await collectPages((options) => this.client.listServices(options));
    const activeProject = projects.find((project) => project.project_id === state.active_project_id);
    if (!activeProject) throw new DesktopContractErrorV2("Active project is absent from the remote project collection");
    if (projects.some((project) => project.project_id !== state.active_project_id)) {
      throw new DesktopContractErrorV2("Active project tunnel returned another project");
    }
    const capability = await this.client.projectCapabilities(activeProject.project_id);
    const taskDetails = [];
    for (const task of tasks) {
      const timeline = await collectPages((options) => this.client.taskTimeline(task.task_id, options));
      const artifacts = await collectPages((options) => this.client.taskArtifacts(task.task_id, options));
      const transition = task.successor_transition === null
        ? null
        : await this.client.getTransition(task.successor_transition.successor_transition_id);
      taskDetails.push({ task, timeline, artifacts, transition });
    }
    const timelines: Record<string, readonly CoreEventEnvelopeV2[]> = {};
    const transitions: Record<string, SuccessorTransitionV2> = {};
    const artifactsById = new Map<string, ArtifactV2>();
    for (const detail of taskDetails) {
      timelines[detail.task.task_id] = detail.timeline;
      if (detail.transition !== null) transitions[detail.transition.transition.successor_transition_id] = detail.transition;
      for (const artifact of detail.artifacts) {
        if (artifact.project_id !== activeProject.project_id) {
          throw new DesktopContractErrorV2("Task artifact belongs to another project");
        }
        const existing = artifactsById.get(artifact.artifact_id);
        if (existing !== undefined && JSON.stringify(existing) !== JSON.stringify(artifact)) {
          throw new DesktopContractErrorV2("Task artifact authority drifted across one refresh");
        }
        artifactsById.set(artifact.artifact_id, artifact);
      }
    }
    if (this.validation !== null && (
      this.validation.project_id !== activeProject.project_id
      || this.validation.registry_sha256 !== capability.registry_sha256
    )) this.validation = null;
    return {
      state,
      catalog,
      profiles,
      projects,
      tasks,
      transitions,
      timelines,
      artifacts: [...artifactsById.values()].sort((left, right) => (
        compareUtcTimestampsV2(left.created_at, right.created_at)
        || left.artifact_id.localeCompare(right.artifact_id)
      )),
      services,
      capability,
      validation: this.validation,
    };
  }

  private localOnlySnapshot(
    state: DesktopStateV2,
    catalog: DesktopProductSnapshotV2["catalog"],
    profiles: readonly RemoteProfileV2[],
  ): Omit<DesktopProductSnapshotV2, "activeOperation" | "stream"> {
    this.validation = null;
    return {
      state,
      catalog,
      profiles,
      projects: [],
      tasks: [],
      transitions: {},
      timelines: {},
      artifacts: [],
      services: [],
      capability: null,
      validation: null,
    };
  }

  private requireSnapshot(): DesktopProductSnapshotV2 {
    if (this.snapshot === null || this.snapshot.stream.status !== "fresh") throw new DesktopContractErrorV2("Desktop v2 snapshot is not current");
    return this.snapshot;
  }

  private requireIntent(intent: ProductMutationIntentV2): DesktopProductSnapshotV2 {
    actionIdV2(intent.actionId);
    const snapshot = this.requireSnapshot();
    if (intent.streamEpoch !== snapshot.stream.epoch) throw new DesktopContractErrorV2("Desktop state changed before this action");
    return snapshot;
  }

  private requireProfile(profileId: string, intent: ProductMutationIntentV2): RemoteProfileV2 {
    const profile = this.requireIntent(intent).profiles.find((candidate) => candidate.profile_id === profileId);
    if (!profile) throw new DesktopContractErrorV2("Profile action references an unknown profile");
    return profile;
  }

  private requireSystemProfile(profileId: string, intent: ProductMutationIntentV2): RemoteWorkspaceProfileV2 {
    const profile = this.requireProfile(profileId, intent);
    if (profile.profile_kind !== "system_openssh") throw new DesktopContractErrorV2("Preview profile must be rebound before connection");
    return profile;
  }

  private requireProject(projectId: string, intent: ProductMutationIntentV2): ProjectV2 {
    const project = this.requireIntent(intent).projects.find((candidate) => candidate.project_id === projectId);
    if (!project) throw new DesktopContractErrorV2("Project action references an unknown active project");
    return project;
  }

  private requireTask(taskId: string, intent: ProductMutationIntentV2): TaskV2 {
    const task = this.requireIntent(intent).tasks.find((candidate) => candidate.task_id === taskId);
    if (!task) throw new DesktopContractErrorV2("Task action references an unknown task");
    return task;
  }

  private requireTransition(transitionId: string, intent: ProductMutationIntentV2) {
    const snapshot = this.requireIntent(intent);
    const transition = snapshot.transitions[transitionId];
    const project = snapshot.projects.find((candidate) => candidate.project_id === transition?.transition.project_id);
    if (!transition || !project) throw new DesktopContractErrorV2("Transition action references unknown authority");
    return { transition, project };
  }

  private activeCoreAuthorityV2(): { readonly key: string; readonly resourceGeneration: number } | null {
    const snapshot = this.snapshot;
    if (snapshot === null || snapshot.state.active_profile_id === null || snapshot.state.active_project_id === null) {
      return null;
    }
    const profile = snapshot.profiles.find((candidate) => candidate.profile_id === snapshot.state.active_profile_id);
    const project = snapshot.projects.find((candidate) => candidate.project_id === snapshot.state.active_project_id);
    if (profile?.profile_kind !== "system_openssh" || profile.connection_state !== "connected" || project === undefined) {
      return null;
    }
    const head = project.active_project_head;
    return {
      key: canonicalJsonV2({
        profile_id: profile.profile_id,
        connection_generation: profile.connection_generation,
        project_id: project.project_id,
        project_head_id: head?.project_head_id ?? null,
        project_head_manifest_sha256: head?.manifest_sha256 ?? null,
      }),
      resourceGeneration: profile.connection_generation,
    };
  }

  private activeDiagnosticAuthorityV2(): string | null {
    const snapshot = this.snapshot;
    if (snapshot === null || snapshot.state.active_profile_id === null) return null;
    const profile = snapshot.profiles.find((candidate) => candidate.profile_id === snapshot.state.active_profile_id);
    if (profile?.profile_kind !== "system_openssh" || profile.connection_state !== "connected") return null;
    return canonicalJsonV2({
      profile_id: profile.profile_id,
      connection_generation: profile.connection_generation,
    });
  }

  private async reconcilePendingOperationsV2(snapshot: DesktopProductSnapshotV2): Promise<void> {
    const entries = [...this.mutationIntents.list()];
    const acknowledged = new Set<string>();
    for (const originalEntry of entries) {
      let entry = originalEntry;
      if (entry.accepted_operation_id === null) {
        const recovered = await this.recoverReservedLifecycleOperationV2(entry);
        if (recovered === null) {
          await this.reconcileReservedCancellationV2(entry);
          continue;
        }
        entry = recovered.entry;
      }
      const operationId = entry.accepted_operation_id;
      if (operationId === null) {
        throw new DesktopContractErrorV2("Recovered mutation has no operation authority");
      }
      if (isDesktopLifecycleMutationV2(entry.mutation_kind)) {
        const operation = this.lifecycleOperations.get(operationId)?.operation
          ?? await this.lifecycleOperations.refresh(operationId);
        if (isLifecycleTerminalV2(operation)) {
          await this.reconcileLifecycleTerminalV2(entry, operation, snapshot);
          acknowledged.add(operation.operation_id);
        }
      } else if (entry.mutation_kind === "diagnostic_create") {
        const diagnostic = await this.refreshDiagnosticV2(operationId);
        if (isDiagnosticTerminalV2(diagnostic)) {
          if (entry.state === "accepted") {
            await this.mutationIntents.markTerminalObserved(entry.action_id, diagnostic.diagnostic_id);
          }
          await this.mutationIntents.clearTerminalObserved(entry.action_id, diagnostic.diagnostic_id);
        }
      } else if (isCoreOperationMutationV2(entry.mutation_kind)) {
        const operation = this.coreOperations.get(operationId)
          ?? await this.coreOperations.refresh(operationId);
        if (isCoreOperationTerminalV2(operation)) {
          if (entry.state === "accepted") {
            await this.mutationIntents.markTerminalObserved(entry.action_id, operation.operation_id);
          }
          await this.mutationIntents.clearTerminalObserved(entry.action_id, operation.operation_id);
        }
      }
    }

    const retained = this.mutationIntents.list();
    const currentOperationIds = new Set(retained.flatMap((entry) => [
      ...(entry.accepted_operation_id === null ? [] : [entry.accepted_operation_id]),
      ...entry.completed_operation_ids,
    ]));
    for (const state of this.lifecycleOperations.list()) {
      if (!isLifecycleTerminalV2(state.operation)) continue;
      if (acknowledged.has(state.operation.operation_id)) continue;
      const result = state.operation.result;
      const isInactiveHistoricalProjectResult = (
        !currentOperationIds.has(state.operation.operation_id)
        && state.operation.status === "succeeded"
        && result !== null
        && result.result_kind === "project"
        && result.project_id !== snapshot.state.active_project_id
      );
      if (
        !currentOperationIds.has(state.operation.operation_id)
        && !isInactiveHistoricalProjectResult
      ) {
        await this.validateLifecycleResultV2(state.operation, snapshot);
      }
      await this.acknowledgeLifecycleTerminalV2(state.operation);
    }
  }

  private async recoverReservedLifecycleOperationV2(
    entry: PendingMutationIntentV2,
  ): Promise<{
    readonly entry: PendingMutationIntentV2;
    readonly operation: LifecycleOperationV2;
  } | null> {
    if (entry.state !== "reserved" || !isDesktopLifecycleMutationV2(entry.mutation_kind)) {
      return null;
    }
    let operation: LifecycleOperationV2;
    try {
      operation = await this.client.getLifecycleOperationByAction(
        entry.action_id,
        expectedLifecycleKindForMutationIntentV2(entry),
      );
    } catch (error) {
      if (error instanceof DesktopApiErrorV2 && error.status === 404) return null;
      throw error;
    }
    assertLifecycleOperationMatchesMutationIntentV2(entry, operation);
    const accepted = await this.mutationIntents.bindAcceptedOperation(
      entry.action_id,
      operation.operation_id,
    );
    return { entry: accepted, operation: this.observeOperation(operation) };
  }

  private async reconcileReservedCancellationV2(entry: PendingMutationIntentV2): Promise<boolean> {
    if (entry.state !== "reserved") return false;
    if (entry.mutation_kind === "lifecycle_cancel" && entry.resource_scope.startsWith("lifecycle_operation:")) {
      await this.replayReservedLifecycleCancellationV2(entry);
      return true;
    }
    if (entry.mutation_kind === "core_operation_cancel" && entry.resource_scope.startsWith("core_operation:")) {
      const operationId = opaqueIdV2Schema.parse(entry.resource_scope.slice("core_operation:".length));
      const operation = this.coreOperations.get(operationId) ?? await this.coreOperations.refresh(operationId);
      if (isCoreOperationTerminalV2(operation)) {
        await this.completeDirectMutationV2(entry, operation);
        return true;
      }
      return false;
    }
    return false;
  }

  private async replayReservedLifecycleCancellationV2(
    entry: PendingMutationIntentV2,
  ): Promise<LifecycleOperationV2> {
    if (entry.state !== "reserved"
      || entry.mutation_kind !== "lifecycle_cancel"
      || !entry.resource_scope.startsWith("lifecycle_operation:")) {
      throw new DesktopContractErrorV2("Lifecycle cancellation retry identity is invalid");
    }
    const operationId = opaqueIdV2Schema.parse(entry.resource_scope.slice("lifecycle_operation:".length));
    const expectedRequest = { schema_version: "2", expected_operation_id: operationId } as const;
    if (entry.request_sha256 !== sha256Utf8V2(canonicalJsonV2(expectedRequest))) {
      throw new DesktopContractErrorV2("Lifecycle cancellation request identity changed");
    }
    const operation = this.lifecycleOperations.get(operationId)?.operation
      ?? await this.lifecycleOperations.refresh(operationId);
    if (isLifecycleTerminalV2(operation)) {
      await this.completeDirectMutationV2(entry, operation);
      return operation;
    }
    const cancelled = await this.sendLifecycleCancellationWithRefreshV2(
      operationId,
      entry.action_id,
    );
    await this.completeDirectMutationV2(entry, cancelled, async () => {
      await this.lifecycleOperations.refresh(operationId);
    });
    return cancelled;
  }

  private async sendLifecycleCancellationWithRefreshV2(
    operationId: string,
    actionId: string,
  ): Promise<LifecycleOperationV2> {
    try {
      return await this.lifecycleOperations.cancel(operationId, actionId);
    } catch (error) {
      if (!(error instanceof DesktopApiErrorV2) || error.status !== 412) throw error;
      const refreshed = await this.lifecycleOperations.refresh(operationId);
      if (isLifecycleTerminalV2(refreshed)) return refreshed;
      return this.lifecycleOperations.cancel(operationId, actionId);
    }
  }

  private async reconcileLifecycleTerminalV2(
    entry: PendingMutationIntentV2,
    operation: LifecycleOperationV2,
    snapshot: DesktopProductSnapshotV2,
  ): Promise<void> {
    await this.validateLifecycleResultV2(operation, snapshot);
    if (entry.state === "accepted") {
      await this.mutationIntents.markTerminalObserved(entry.action_id, operation.operation_id);
    }
    if (entry.mutation_kind === "project_create"
      && entry.chain_step === "native_workspace_prepare"
      && operation.status === "succeeded") {
      await this.mutationIntents.advanceNativeProjectChain(entry.action_id, operation.operation_id);
    } else {
      if (entry.mutation_kind === "project_create" && entry.chain_step === "project_create") {
        await this.native.settleProjectSource(
          entry.action_id,
          operation.status === "succeeded" ? "adopt" : "discard",
        );
      }
      await this.mutationIntents.clearTerminalObserved(entry.action_id, operation.operation_id);
    }
    await this.acknowledgeLifecycleTerminalV2(operation);
  }

  private async validateLifecycleResultV2(
    operation: LifecycleOperationV2,
    snapshot: DesktopProductSnapshotV2,
  ): Promise<void> {
    if (operation.status !== "succeeded") return;
    const result = operation.result;
    if (result === null) throw new DesktopContractErrorV2("Succeeded lifecycle operation has no result authority");
    if (result.result_kind === "profile") {
      const profile = await this.client.getProfile(result.profile_id);
      // A terminal result records its historical generation. Later lifecycle
      // operations may have monotonically advanced the same profile.
      if (profile.profile_kind !== "system_openssh"
        || profile.connection_generation < result.connection_generation) {
        throw new DesktopContractErrorV2("Lifecycle profile result is absent from authoritative refresh");
      }
      return;
    }
    if (result.result_kind === "project") {
      // A successful project_create result is not necessarily the active
      // project yet, so the active-project endpoint cannot validate it.
      // Project activation is a separate lifecycle operation and is checked
      // against both the endpoint and the authoritative snapshot below.
      if (operation.kind === "project_create") return;
      const project = await this.client.getProject(result.project_id);
      if (project.project_id !== result.project_id
        || (operation.kind === "project_activate" && snapshot.state.active_project_id !== result.project_id)) {
        throw new DesktopContractErrorV2("Lifecycle project result is absent from authoritative refresh");
      }
    }
  }

  private async acknowledgeLifecycleTerminalV2(operation: LifecycleOperationV2): Promise<void> {
    if (!isLifecycleTerminalV2(operation)) return;
    try {
      await this.client.acknowledgeLifecycleOperation(operation.operation_id, {
        schema_version: "2",
        expected_operation_id: operation.operation_id,
        expected_terminal_status: operation.status,
      }, {
        resourceGeneration: 0,
        ifMatch: operation.etag,
        idempotencyKey: `lifecycle-ack-${operation.operation_id}`,
      });
    } catch (error) {
      if (isDeterministicMutationRejectionV2(error)) throw error;
    }
  }

  private async dispatchMutationV2<T>(input: {
    readonly snapshot: DesktopProductSnapshotV2;
    readonly intent: ProductMutationIntentV2;
    readonly mutationKind: MutationKindV2;
    readonly resourceScope: string;
    readonly request: unknown;
    readonly authority: Readonly<Record<string, unknown>>;
    readonly chainStep?: MutationChainStepV2;
    readonly includeStreamAuthority?: boolean;
    readonly operationAuthority?: "lifecycle" | "core" | "diagnostic";
    readonly send: (actionId: string) => Promise<T>;
  }): Promise<{ readonly entry: PendingMutationIntentV2; readonly value: T }> {
    const stream = input.snapshot.stream;
    if (stream.status !== "fresh") throw new DesktopContractErrorV2("Mutation requires a fresh provider stream authority");
    const entry = await this.mutationIntents.reserve({
      proposedActionId: input.intent.actionId,
      mutationKind: input.mutationKind,
      resourceScope: input.resourceScope,
      request: input.request,
      authority: input.includeStreamAuthority === false
        ? { schema_version: "2", ...input.authority }
        : {
            schema_version: "2",
            provider_stream_last_event_id: stream.lastEventId,
            desktop_state_updated_at: input.snapshot.state.updated_at,
            ...input.authority,
          },
      providerStreamInstance: this.providerStreamInstance,
      providerStreamEpoch: stream.epoch,
      chainStep: input.chainStep,
    });
    if (entry.state === "deterministic_rejection") {
      await this.mutationIntents.markDirectResponseObserved(
        entry.action_id,
        deterministicRejectionDigestV2(),
      );
      throw new MutationIntentConflictV2("This exact mutation was deterministically rejected", entry);
    }
    try {
      const value = await input.send(entry.action_id);
      if (input.operationAuthority !== undefined) {
        const operationId = input.operationAuthority === "diagnostic"
          ? diagnosticIdOfV2(value)
          : operationIdOfV2(value);
        if (operationId === null) {
          throw new DesktopContractErrorV2(`${input.operationAuthority} mutation did not return operation authority`);
        }
        await this.mutationIntents.bindAcceptedOperation(entry.action_id, operationId);
      }
      return { entry, value };
    } catch (error) {
      if (isDeterministicMutationRejectionV2(error)) {
        await this.mutationIntents.markDeterministicRejection(entry.action_id);
        await this.mutationIntents.markDirectResponseObserved(
          entry.action_id,
          deterministicRejectionDigestV2(),
        );
      }
      throw error;
    }
  }

  private async completeDirectMutationV2(
    entry: PendingMutationIntentV2,
    value: unknown,
    verify?: () => Promise<void>,
  ): Promise<void> {
    await verify?.();
    await this.mutationIntents.markDirectResponseObserved(
      entry.action_id,
      sha256Utf8V2(canonicalJsonV2(value)),
    );
  }

  private async completeTerminalOperationV2(
    entry: PendingMutationIntentV2,
    operationId: string,
  ): Promise<void> {
    await this.mutationIntents.markTerminalObserved(entry.action_id, operationId);
    await this.mutationIntents.clearTerminalObserved(entry.action_id, operationId);
  }

  private observeOperation<T extends LocalOperationV2 | LifecycleOperationV2 | OperationV2>(operation: T): T {
    if ("phase" in operation) {
      const lifecycle = this.lifecycleOperations.observe(operation as LifecycleOperationV2);
      this.ensureLifecyclePollingV2(lifecycle);
    }
    if ("progress_completed" in operation) {
      const core = this.coreOperations.observe(operation as OperationV2);
      this.ensureCorePollingV2(core);
    }
    this.activeOperation = operation;
    this.invalidate();
    return operation;
  }

  private ensureLifecyclePollingV2(operation: LifecycleOperationV2): void {
    if (isLifecycleTerminalV2(operation) || this.lifecyclePolls.has(operation.operation_id)) return;
    const polling = this.lifecycleOperations.pollUntilTerminal(
      operation.operation_id,
      undefined,
      async (observed) => {
        this.activeOperation = observed;
        if (this.snapshot !== null) this.snapshot = { ...this.snapshot, activeOperation: observed };
        this.emit({ kind: "snapshot_changed" });
      },
    ).then(async (terminal) => {
      this.activeOperation = terminal;
      await this.lifecycleOperations.loadLogs(terminal.operation_id);
      this.emit({ kind: "snapshot_changed" });
    }).catch((error) => {
      const apiError = apiErrorOfV2(error);
      this.emit({ kind: "stream_error", error: apiError });
    }).finally(() => {
      this.lifecyclePolls.delete(operation.operation_id);
    });
    this.lifecyclePolls.set(operation.operation_id, polling);
  }

  private ensureCorePollingV2(operation: OperationV2): void {
    if (isCoreOperationTerminalV2(operation) || this.corePolls.has(operation.operation_id)) return;
    const polling = this.coreOperations.pollUntilTerminal(
      operation.operation_id,
      undefined,
      async (observed) => {
        this.activeOperation = observed;
        if (this.snapshot !== null) this.snapshot = { ...this.snapshot, activeOperation: observed };
        this.emit({ kind: "snapshot_changed" });
      },
    ).then((terminal) => {
      this.activeOperation = terminal;
      if (this.snapshot !== null) this.snapshot = { ...this.snapshot, activeOperation: terminal };
      this.emit({ kind: "snapshot_changed" });
    }).catch((error) => {
      this.emit({ kind: "stream_error", error: apiErrorOfV2(error) });
    }).finally(() => {
      this.corePolls.delete(operation.operation_id);
    });
    this.corePolls.set(operation.operation_id, polling);
  }

  private observeDiagnosticV2(diagnostic: DiagnosticV2): DiagnosticV2 {
    const previous = this.diagnostics.get(diagnostic.diagnostic_id);
    if (previous !== undefined) assertDiagnosticDoesNotRegressV2(previous, diagnostic);
    if (previous !== undefined && canonicalJsonV2(previous) === canonicalJsonV2(diagnostic)) {
      return previous;
    }
    this.diagnostics.set(diagnostic.diagnostic_id, diagnostic);
    this.ensureDiagnosticPollingV2(diagnostic);
    this.emit({ kind: "snapshot_changed" });
    return diagnostic;
  }

  private async refreshDiagnosticV2(diagnosticId: string): Promise<DiagnosticV2> {
    const authority = this.activeDiagnosticAuthorityV2();
    if (authority === null) throw new DesktopContractErrorV2("Diagnostic lookup requires a connected system-OpenSSH profile");
    const diagnostic = await this.client.getDiagnostic(diagnosticId);
    if (this.activeDiagnosticAuthorityV2() !== authority) {
      throw new DesktopContractErrorV2("Active diagnostic profile authority changed");
    }
    if (diagnostic.diagnostic_id !== diagnosticId) {
      throw new DesktopContractErrorV2("Diagnostic lookup returned another diagnostic");
    }
    return this.observeDiagnosticV2(diagnostic);
  }

  private ensureDiagnosticPollingV2(diagnostic: DiagnosticV2): void {
    if (isDiagnosticTerminalV2(diagnostic) || this.diagnosticPolls.has(diagnostic.diagnostic_id)) return;
    const polling = this.pollDiagnosticUntilTerminalV2(diagnostic.diagnostic_id)
      .then(() => undefined)
      .catch((error) => {
        this.emit({ kind: "stream_error", error: apiErrorOfV2(error) });
      })
      .finally(() => {
        this.diagnosticPolls.delete(diagnostic.diagnostic_id);
      });
    this.diagnosticPolls.set(diagnostic.diagnostic_id, polling);
  }

  private async pollDiagnosticUntilTerminalV2(diagnosticId: string): Promise<DiagnosticV2> {
    let diagnostic = this.diagnostics.get(diagnosticId) ?? await this.refreshDiagnosticV2(diagnosticId);
    let delayIndex = 0;
    while (!isDiagnosticTerminalV2(diagnostic)) {
      await waitForV2(RESOURCE_POLL_DELAYS_MS[delayIndex]!);
      const before = canonicalJsonV2({ status: diagnostic.status, updated_at: diagnostic.updated_at });
      diagnostic = await this.refreshDiagnosticV2(diagnosticId);
      delayIndex = canonicalJsonV2({ status: diagnostic.status, updated_at: diagnostic.updated_at }) === before
        ? Math.min(delayIndex + 1, RESOURCE_POLL_DELAYS_MS.length - 1)
        : 0;
    }
    return diagnostic;
  }

  private async waitForLifecycleTerminal(initial: LifecycleOperationV2): Promise<LifecycleOperationV2> {
    this.lifecycleOperations.observe(initial);
    return this.lifecycleOperations.pollUntilTerminal(initial.operation_id);
  }

  private invalidate(): void {
    this.waitingForRefresh = true;
    if (this.snapshot !== null) {
      const stream: ProductStreamStateV2 = { status: "stale", epoch: this.epoch, reason: "refresh_pending" };
      this.snapshot = { ...this.snapshot, stream };
    }
    this.emit({ kind: "snapshot_changed" });
  }

  private setSnapshotStream(stream: ProductStreamStateV2): void {
    if (this.snapshot !== null) this.snapshot = { ...this.snapshot, stream };
  }

  private emit(signal: ProductSubscriptionSignalV2): void {
    for (const listener of this.listeners) listener(signal);
  }

  private ensureEventStream(): void {
    if (this.listeners.size === 0 || this.waitingForRefresh || this.streamPromise !== null) return;
    this.streamPromise = this.runEventStream()
      .catch((error) => {
        this.waitingForRefresh = true;
        const apiError = apiErrorOfV2(error);
        this.setSnapshotStream({ status: "error", epoch: this.epoch, error: apiError });
        this.emit({ kind: "stream_error", error: apiError });
      })
      .finally(() => {
        this.streamPromise = null;
        this.streamAbort = null;
        if (this.listeners.size > 0 && !this.waitingForRefresh) queueMicrotask(() => this.ensureEventStream());
      });
  }

  private async runEventStream(): Promise<void> {
    let attempt = 0;
    while (this.listeners.size > 0 && attempt <= this.reconnectDelaysMs.length) {
      const controller = new AbortController();
      this.streamAbort = controller;
      try {
        const streamRequest = await this.client.eventStreamRequest(this.replay.lastEventId ?? undefined);
        const response = await this.fetch(streamRequest.url, {
          method: "GET",
          headers: streamRequest.headers,
          credentials: "omit",
          cache: "no-store",
          redirect: "error",
          referrerPolicy: "no-referrer",
          signal: controller.signal,
        });
        if (!response.ok) {
          const payload = await readStreamErrorV2(response);
          const recovery = parseEventStreamFailureV2(response.status, payload);
          this.replay.reset();
          this.waitingForRefresh = true;
          this.setSnapshotStream({ status: "cursor_reset", epoch: this.epoch, resumeFromEventId: recovery.resumeFromEventId });
          this.emit({ kind: "cursor_reset", resumeFromEventId: null });
          return;
        }
        if (!(response.headers.get("Content-Type") ?? "").toLowerCase().includes("text/event-stream")) {
          throw new DesktopContractErrorV2("Desktop v2 event response is not an event stream", { status: response.status });
        }
        if (response.body === null) throw new DesktopContractErrorV2("Desktop v2 event response has no body");
        const sawEvent = await this.consumeEventStream(response.body, controller);
        if (sawEvent) attempt = 0;
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof DesktopApiErrorV2 || error instanceof DesktopContractErrorV2) throw error;
      }
      if (attempt >= this.reconnectDelaysMs.length) throw new DesktopContractErrorV2("Desktop v2 event stream exhausted its reconnect budget");
      await delay(this.reconnectDelaysMs[attempt]!, controller.signal);
      attempt += 1;
    }
  }

  private async consumeEventStream(stream: ReadableStream<Uint8Array>, controller: AbortController): Promise<boolean> {
    const reader = stream.getReader();
    const decoder = new TextDecoder("utf-8", { fatal: true });
    let buffer = "";
    let sawEvent = false;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        if (new TextEncoder().encode(buffer).byteLength > MAX_SSE_BUFFER_BYTES) throw new DesktopContractErrorV2("Desktop v2 event stream exceeded the buffer limit");
        let boundary = findFrameBoundary(buffer);
        while (boundary !== null) {
          const frame = buffer.slice(0, boundary.index);
          buffer = buffer.slice(boundary.index + boundary.length);
          if (frame !== "") {
            const parsed = parseSseFrameV2(frame);
            if (parsed.kind === "event") {
              sawEvent = true;
              try {
                const observation = this.replay.observe(parsed.envelope);
                if (observation.kind === "accepted") {
                  if (observation.event.payload.payload_kind === "lifecycle_operation_changed") {
                    const operation = await this.lifecycleOperations.refresh(observation.event.payload.operation_id);
                    await this.lifecycleOperations.loadLogs(operation.operation_id);
                    this.activeOperation = operation;
                  }
                  this.waitingForRefresh = true;
                  this.emit({ kind: "snapshot_changed" });
                  controller.abort();
                  return true;
                }
              } catch (error) {
                this.waitingForRefresh = true;
                this.setSnapshotStream({ status: "stale", epoch: this.epoch, reason: "event_gap" });
                this.emit({ kind: "stream_stale", reason: "event_gap" });
                this.emit({ kind: "snapshot_changed" });
                controller.abort();
                return true;
              }
            }
          }
          boundary = findFrameBoundary(buffer);
        }
      }
      buffer += decoder.decode();
      if (buffer.trim() !== "") throw new DesktopContractErrorV2("Desktop v2 event stream ended with an incomplete frame");
      return sawEvent;
    } finally {
      reader.releaseLock();
    }
  }
}

export function createLocalApiDesktopProductProviderV2(options: LocalApiDesktopProductProviderOptionsV2): DesktopProductProviderV2 {
  return new LocalApiDesktopProductProviderV2(options);
}

async function collectPages<T>(load: (options: ListRequestOptionsV2) => Promise<{ items: T[]; has_more: boolean; next_cursor: string | null }>): Promise<T[]> {
  const items: T[] = [];
  const cursors = new Set<string>();
  let after: string | undefined;
  for (let page = 0; page < MAX_COLLECTION_PAGES; page += 1) {
    const result = await load({ limit: PAGE_LIMIT, ...(after === undefined ? {} : { after }) });
    items.push(...result.items);
    if (items.length > MAX_REFRESH_RESOURCES) throw new DesktopContractErrorV2("Desktop v2 snapshot exceeded the resource budget");
    if (!result.has_more) return items;
    if (result.next_cursor === null || cursors.has(result.next_cursor)) throw new DesktopContractErrorV2("Desktop v2 pagination cursor cycle detected");
    cursors.add(result.next_cursor);
    after = result.next_cursor;
  }
  throw new DesktopContractErrorV2("Desktop v2 collection exceeded the page budget");
}

function assertProfileAuthority(state: DesktopStateV2, profiles: readonly RemoteProfileV2[]): void {
  const stateById = new Map(state.profiles.map((profile) => [profile.profile_id, JSON.stringify(profile)]));
  if (stateById.size !== state.profiles.length || profiles.length !== state.profiles.length) {
    throw new DesktopContractErrorV2("Desktop state and profile collection disagree");
  }
  for (const profile of profiles) {
    if (stateById.get(profile.profile_id) !== JSON.stringify(profile)) throw new DesktopContractErrorV2("Desktop profile authority drifted across one refresh");
  }
}

function hasUnboundProjectTunnelTransitionV2(state: DesktopStateV2): boolean {
  return state.pending_operations.some((operation) => (
    operation.kind === "project_create"
    && (operation.status === "queued" || operation.status === "running")
    && operation.resource.resource_kind === "project"
    && operation.resource.resource_id !== state.active_project_id
  ));
}

function activeConnectedProfile(snapshot: Pick<DesktopProductSnapshotV2, "state" | "profiles">): RemoteWorkspaceProfileV2 {
  const profile = snapshot.profiles.find((candidate) => candidate.profile_id === snapshot.state.active_profile_id);
  if (profile?.profile_kind !== "system_openssh" || profile.connection_state !== "connected") {
    throw new DesktopContractErrorV2("Active Core authority has no connected system-OpenSSH profile");
  }
  return profile;
}

function projectCreateRequestV2(draft: ProjectDraftV2, profile: RemoteWorkspaceProfileV2) {
  return {
    schema_version: "2" as const,
    profile_id: profile.profile_id,
    profile_connection_generation: profile.connection_generation,
    display_name: draft.displayName,
    config: scienceProjectConfigV2Schema.parse(draft.config),
  };
}

function taskAction(task: TaskV2) {
  return {
    schema_version: "2" as const,
    task_admission_id: task.admission.task_admission_id,
    admission_sha256: task.admission.admission_sha256,
    predecessor_project_head_id: task.admission.predecessor_project_head.project_head_id,
  };
}

function taskMutationAuthority(task: TaskV2) {
  const attempt = task.attempts.at(-1);
  if (!attempt) throw new DesktopContractErrorV2("Task has no infrastructure attempt authority");
  return { resource_generation: attempt.ordinal, etag: task.etag };
}

function transitionAction(transition: SuccessorTransitionV2) {
  return {
    schema_version: "2" as const,
    expected_predecessor_project_head_id: transition.transition.predecessor_project_head.project_head_id,
    plan_sha256: transition.transition.plan_sha256,
  };
}

function transitionMutationAuthority(transition: SuccessorTransitionV2, project: ProjectV2) {
  return {
    resource_generation: transition.transition.expected_successor_generation,
    etag: project.etag,
  };
}

function requireProjectHead(project: ProjectV2) {
  if (project.active_project_head === null) throw new DesktopContractErrorV2("Project has no active Project Head");
  return project.active_project_head;
}

function operationIdOfV2(value: unknown): string | null {
  if (typeof value !== "object" || value === null || !("operation_id" in value)) return null;
  const operationId = (value as { readonly operation_id?: unknown }).operation_id;
  return typeof operationId === "string" ? opaqueIdV2Schema.parse(operationId) : null;
}

function diagnosticIdOfV2(value: unknown): string | null {
  if (typeof value !== "object" || value === null || !("diagnostic_id" in value)) return null;
  const diagnosticId = (value as { readonly diagnostic_id?: unknown }).diagnostic_id;
  return typeof diagnosticId === "string" ? opaqueIdV2Schema.parse(diagnosticId) : null;
}

function isDeterministicMutationRejectionV2(error: unknown): boolean {
  return error instanceof DesktopApiErrorV2
    && (!error.apiError.retryable || [400, 403, 404, 409, 412, 422, 426, 501].includes(error.status));
}

function deterministicRejectionDigestV2(): string {
  return sha256Utf8V2(canonicalJsonV2({ status: "deterministic_rejection" }));
}

function lifecycleTerminalError(operation: LifecycleOperationV2, fallback: string): DesktopContractErrorV2 {
  return new DesktopContractErrorV2(operation.failure?.summary ?? fallback);
}

function latestLifecycleOperationV2(
  states: readonly LifecycleOperationStateV2[],
): LifecycleOperationStateV2 | null {
  return states.at(-1) ?? null;
}

function isDesktopLifecycleMutationV2(kind: MutationKindV2): boolean {
  return [
    "profile_connect",
    "profile_disconnect",
    "host_key_review",
    "project_create",
    "project_activate",
  ].includes(kind);
}

function assertLifecycleOperationMatchesMutationIntentV2(
  entry: PendingMutationIntentV2,
  operation: LifecycleOperationV2,
): void {
  const expectedKind = expectedLifecycleKindForMutationIntentV2(entry);
  if (operation.kind !== expectedKind) {
    throw new DesktopContractErrorV2("Lifecycle action lookup returned another mutation kind");
  }
  if (["profile_connect", "profile_disconnect", "host_key_review"].includes(entry.mutation_kind)) {
    const expectedProfileId = entry.resource_scope.startsWith("profile:")
      ? entry.resource_scope.slice("profile:".length)
      : null;
    if (expectedProfileId === null
      || operation.resource.resource_kind !== "profile"
      || operation.resource.resource_id !== expectedProfileId) {
      throw new DesktopContractErrorV2("Lifecycle action lookup returned another profile authority");
    }
  }
  if (entry.mutation_kind === "project_activate") {
    const expectedProjectId = entry.resource_scope.startsWith("project:")
      ? entry.resource_scope.slice("project:".length)
      : null;
    if (expectedProjectId === null
      || operation.resource.resource_kind !== "project"
      || operation.resource.resource_id !== expectedProjectId) {
      throw new DesktopContractErrorV2("Lifecycle action lookup returned another project authority");
    }
  }
}

function expectedLifecycleKindForMutationIntentV2(
  entry: PendingMutationIntentV2,
): LifecycleOperationKindV2 {
  if (entry.mutation_kind === "project_create") {
    return entry.chain_step === "native_workspace_prepare"
      ? "native_workspace_prepare"
      : "project_create";
  }
  if (["profile_connect", "profile_disconnect", "host_key_review", "project_activate"].includes(entry.mutation_kind)) {
    return entry.mutation_kind as LifecycleOperationKindV2;
  }
  throw new DesktopContractErrorV2("Mutation intent is not a Desktop lifecycle operation");
}

function isCoreOperationMutationV2(kind: MutationKindV2): boolean {
  return [
    "task_cancel",
    "transition_retry",
    "transition_abandon",
    "service_restart",
    "cache_cleanup",
  ].includes(kind);
}

function isCoreOperationTerminalV2(operation: OperationV2): boolean {
  return operation.status === "succeeded"
    || operation.status === "failed"
    || operation.status === "cancelled";
}

function isDiagnosticTerminalV2(diagnostic: DiagnosticV2): boolean {
  return diagnostic.status === "ready" || diagnostic.status === "failed";
}

function assertDiagnosticDoesNotRegressV2(previous: DiagnosticV2, next: DiagnosticV2): void {
  const ranks: Record<DiagnosticV2["status"], number> = {
    queued: 0,
    running: 1,
    ready: 2,
    failed: 2,
  };
  if (previous.diagnostic_id !== next.diagnostic_id
    || previous.scope !== next.scope
    || previous.resource_id !== next.resource_id
    || previous.created_at !== next.created_at
    || compareUtcTimestampsV2(next.updated_at, previous.updated_at) < 0
    || ranks[next.status] < ranks[previous.status]) {
    throw new DesktopContractErrorV2("Diagnostic authority regressed");
  }
  if (isDiagnosticTerminalV2(previous) && canonicalJsonV2(previous) !== canonicalJsonV2(next)) {
    throw new DesktopContractErrorV2("Terminal diagnostic authority changed");
  }
  const sameDocument = canonicalJsonV2({ ...previous, etag: null })
    === canonicalJsonV2({ ...next, etag: null });
  if (sameDocument !== (previous.etag === next.etag)) {
    throw new DesktopContractErrorV2("Diagnostic ETag authority drifted");
  }
}

function actionIdV2(value: string): string {
  return z.string().min(16).max(256).refine((item) => item === item.trim() && !/[\u0000-\u001f\u007f]/.test(item)).parse(value);
}

function waitForV2(milliseconds: number): Promise<void> {
  return new Promise((resolve) => globalThis.setTimeout(resolve, milliseconds));
}

function apiErrorOfV2(error: unknown): DesktopErrorV2 | null {
  if (error instanceof DesktopApiErrorV2) return error.apiError;
  if (error instanceof DesktopContractErrorV2) {
    const summary = error.message
      .replace(/[\u0000-\u001f\u007f]/g, " ")
      .trim()
      .slice(0, 768);
    return {
      schema_version: "2",
      code: "desktop_snapshot_invalid",
      summary: summary || "Desktop state failed contract validation.",
      retryable: true,
      action: "retry",
      affected_resource_id: null,
    };
  }
  return null;
}

async function readStreamErrorV2(response: Response): Promise<unknown> {
  const text = await response.text();
  if (new TextEncoder().encode(text).byteLength > 1_048_576) throw new DesktopContractErrorV2("Desktop v2 event error exceeds the byte limit");
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new DesktopContractErrorV2("Desktop v2 event error contains malformed JSON", { cause: error, status: response.status });
  }
}

function findFrameBoundary(value: string): { index: number; length: number } | null {
  const candidates = ["\n\n", "\r\n\r\n", "\r\r"]
    .map((separator) => ({ index: value.indexOf(separator), length: separator.length }))
    .filter((candidate) => candidate.index >= 0)
    .sort((left, right) => left.index - right.index);
  return candidates[0] ?? null;
}

function delay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(new DOMException("Aborted", "AbortError"));
    const timeout = setTimeout(resolve, milliseconds);
    signal.addEventListener("abort", () => {
      clearTimeout(timeout);
      reject(new DOMException("Aborted", "AbortError"));
    }, { once: true });
  });
}

function fail(message: string): never {
  throw new DesktopContractErrorV2(message);
}
