import { describe, expect, it, vi } from "vitest";
import type { DesktopApiClientV2 } from "../api/v2/client";
import type {
  DesktopStateV2,
  RemoteWorkspaceProfileV2,
  SshHostCatalogV2,
} from "../api/v2/schemas";
import {
  createLocalApiDesktopProductProviderV2,
  type LocalApiNativeBridgeV2,
} from "./localApiProviderV2";

const NOW = "2026-07-23T06:00:00Z";
const DIGEST = "a".repeat(64);
const ETAG = `"${"b".repeat(64)}"`;

function profile(
  overrides: Partial<RemoteWorkspaceProfileV2> = {},
): RemoteWorkspaceProfileV2 {
  return {
    schema_version: "2",
    profile_kind: "system_openssh",
    profile_id: "profile-lab",
    display_name: "Lab GPU",
    connection_authority: "system_openssh",
    ssh_host_alias: "lab-gpu",
    catalog_generation: 3,
    connection_generation: 4,
    connection_state: "disconnected",
    prompt: null,
    trust: {
      schema_version: "2",
      connection_generation: 4,
      state: "trusted",
      review_id: null,
      review_sha256: null,
      key_fingerprints: [],
      repair_support: "not_needed",
    },
    failure: null,
    active_project_id: null,
    core_api_major: null,
    core_openapi_sha256: null,
    core_event_schema_sha256: null,
    core_registry_sha256: null,
    created_at: NOW,
    updated_at: NOW,
    etag: ETAG,
    ...overrides,
  };
}

function catalog(): SshHostCatalogV2 {
  return {
    schema_version: "2",
    catalog_generation: 3,
    hosts: [{
      schema_version: "2",
      ssh_host_alias: "lab-gpu",
      availability: "selectable",
      source_kind: "literal_host",
    }],
    warnings: [],
    scanned_at: NOW,
  };
}

function state(profiles: RemoteWorkspaceProfileV2[] = []): DesktopStateV2 {
  return {
    schema_version: "2",
    profiles,
    active_profile_id: null,
    active_project_id: null,
    last_event_id: null,
    updated_at: NOW,
  };
}

function clientFixture(profiles: RemoteWorkspaceProfileV2[] = []) {
  const client = {
    state: vi.fn().mockResolvedValue(state(profiles)),
    listSshHosts: vi.fn().mockResolvedValue(catalog()),
    listProfiles: vi.fn().mockResolvedValue({
      schema_version: "2",
      items: profiles,
      next_cursor: null,
      has_more: false,
    }),
    listProjects: vi.fn(),
    listTasks: vi.fn(),
    listServices: vi.fn(),
    createProfile: vi.fn(),
    connectProfile: vi.fn(),
    eventStreamRequest: vi.fn(),
  } as unknown as DesktopApiClientV2;
  return client;
}

function nativeFixture(
  selected: unknown = {
    kind: "native_folder_snapshot",
    display_name: "My workspace",
    import_ref: {
      import_id: "import-1",
      content_sha256: DIGEST,
      byte_size: 1_024,
      entry_count: 1,
      extracted_byte_size: 32,
    },
  },
): LocalApiNativeBridgeV2 {
  return {
    selectProjectSource: vi.fn().mockResolvedValue(selected),
    cancelProjectSource: vi.fn(),
    settleProjectSource: vi.fn(),
  };
}

describe("Desktop v2 product provider", () => {
  it("does not call Core collections before an active project tunnel exists", async () => {
    const client = clientFixture();
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
    });

    const result = await provider.refresh();

    expect(result.status).toBe("fresh");
    expect(client.listProjects).not.toHaveBeenCalled();
    expect(client.listTasks).not.toHaveBeenCalled();
    expect(client.listServices).not.toHaveBeenCalled();
  });

  it("creates a profile from an alias only and uses catalog generation authority", async () => {
    const client = clientFixture();
    vi.mocked(client.createProfile).mockResolvedValue(profile());
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
    });
    const result = await provider.refresh();
    if (result.status !== "fresh") throw new Error("fixture refresh failed");

    await provider.createProfile("Lab GPU", "lab-gpu", {
      actionId: "create-profile-lab-0001",
      streamEpoch: result.snapshot.stream.epoch,
    });

    expect(client.createProfile).toHaveBeenCalledWith({
      schema_version: "2",
      display_name: "Lab GPU",
      connection_authority: "system_openssh",
      ssh_host_alias: "lab-gpu",
    }, {
      resourceGeneration: 3,
      idempotencyKey: "create-profile-lab-0001",
    });
    expect(JSON.stringify(vi.mocked(client.createProfile).mock.calls[0])).not.toMatch(
      /password|username|identity|private.key|host_path/i,
    );
  });

  it("derives connect CAS authority from the current profile", async () => {
    const current = profile();
    const client = clientFixture([current]);
    vi.mocked(client.connectProfile).mockResolvedValue({
      schema_version: "2",
      operation_id: "operation-connect-1",
      kind: "profile_connect",
      status: "queued",
      failure: null,
      created_at: NOW,
      updated_at: NOW,
    });
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
    });
    const result = await provider.refresh();
    if (result.status !== "fresh") throw new Error("fixture refresh failed");

    await provider.connectProfile(current.profile_id, {
      actionId: "connect-profile-lab-0001",
      streamEpoch: result.snapshot.stream.epoch,
    });

    expect(client.connectProfile).toHaveBeenCalledWith(current.profile_id, {
      schema_version: "2",
      expected_connection_generation: 4,
    }, {
      resourceGeneration: 4,
      ifMatch: ETAG,
      idempotencyKey: "connect-profile-lab-0001",
    });
  });

  it("keeps native import references out of the renderer model and rejects path canaries", async () => {
    const provider = createLocalApiDesktopProductProviderV2({
      client: clientFixture(),
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
    });
    const result = await provider.refresh();
    if (result.status !== "fresh") throw new Error("fixture refresh failed");
    const intent = {
      kind: "native_folder_snapshot" as const,
      actionId: "select-workspace-0001",
      streamEpoch: result.snapshot.stream.epoch,
    };

    await expect(provider.selectNativeWorkspace(intent)).resolves.toEqual({
      kind: "native_folder_snapshot",
      display_name: "My workspace",
    });

    const poisoned = createLocalApiDesktopProductProviderV2({
      client: clientFixture(),
      native: nativeFixture({
        kind: "native_folder_snapshot",
        display_name: "My workspace",
        import_ref: {
          import_id: "import-1",
          content_sha256: DIGEST,
          byte_size: 1_024,
          entry_count: 1,
          extracted_byte_size: 32,
        },
        selected_path: "/Users/researcher/secret-project",
      }),
      featureFlags: ["system_openssh_profiles"],
    });
    const poisonedRefresh = await poisoned.refresh();
    if (poisonedRefresh.status !== "fresh") throw new Error("fixture refresh failed");
    await expect(poisoned.selectNativeWorkspace({
      ...intent,
      streamEpoch: poisonedRefresh.snapshot.stream.epoch,
    })).rejects.toThrow();
  });

  it("rejects a stale renderer action before issuing a request", async () => {
    const client = clientFixture();
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
    });
    const result = await provider.refresh();
    if (result.status !== "fresh") throw new Error("fixture refresh failed");

    await expect(provider.createProfile("Lab GPU", "lab-gpu", {
      actionId: "create-profile-lab-0001",
      streamEpoch: result.snapshot.stream.epoch + 1,
    })).rejects.toThrow(/state changed/i);
    expect(client.createProfile).not.toHaveBeenCalled();
  });
});
