import { afterEach, describe, expect, it, vi } from "vitest";
import { toDraftPayload } from "../routes/openevoDesktopModel";
import {
  activateOpenEvoProjectConfig,
  fetchOpenEvoBackendArtifactPreview,
  fetchOpenEvoBackendRunArtifacts,
  fetchOpenEvoBackendRunTimeline,
  fetchOpenEvoDesktopCapabilities,
  fetchOpenEvoProjectConfigs,
  fetchOpenEvoDesktopShellModel,
  pollOpenEvoRunStatus,
  runOpenEvoBootstrap,
  runOpenEvoServices,
  runOpenEvoStartRun,
  runOpenEvoWorkspaceSync,
  saveOpenEvoProjectConfig,
  toOpenEvoBootstrapResponse,
  toOpenEvoDesktopShellModel,
} from "./openevo";

describe("OpenEvo sidecar client", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("maps sidecar shell status to the route model", () => {
    const model = toOpenEvoDesktopShellModel({
      remote: {
        id: "lab-gpu",
        host: "gpu.example.edu",
        port: 2222,
        user: "alice",
        auth: {
          method: "private_key",
          private_key_path: "/home/alice/.ssh/openevo",
          password_ref: null,
          passphrase_ref: "keyring://openevo/lab-gpu",
        },
        workspace_root: "/data/openevo/workspaces",
        proxy: {
          http_proxy: "http://127.0.0.1:7890",
          https_proxy: "http://127.0.0.1:7890",
          no_proxy: "localhost,127.0.0.1",
          pip_index_url: "https://pypi.tuna.tsinghua.edu.cn/simple",
          huggingface_endpoint: "https://hf-mirror.com",
          hf_home: "/data/hf-cache",
        },
      },
      project: {
        name: "Protein Folding Literature Sprint",
        task_id: "folding-baseline",
        source: "Git repository: github.com/example/protein-workflows",
        objective: "Survey papers.",
        evolution_targets: evolutionTargets(),
      },
      execution: {
        mode: "codex_subscription_transcript",
        model: "gpt-5.1-codex-mini",
        token_metrics_available: false,
      },
      bootstrap: {
        ready: true,
        state_root: "/home/alice/.openevo/runs/protein/folding",
        workspace_root: "/home/alice/.openevo/workspaces",
        readiness_notes: ["Codex subscription login available"],
      },
      services: [
        {
          id: "ssh",
          label: "SSH transport",
          state: "ready",
          detail: "Remote command execution available",
        },
      ],
      evolution: [
        {
          id: "transcript",
          label: "Transcript capture",
          state: "complete",
          detail: "Transcript captured.",
        },
      ],
      developer_mode: {
        enabled: false,
        benchmark_controls_visible: false,
      },
      sidecar: {
        mutation_token: "token-123",
        transport: {
          id: "ssh",
          label: "SSH transport",
          supports_password_ref: false,
          supports_passphrase_ref: false,
        },
      },
    });

    expect(model.remote.proxy.httpsProxy).toBe("http://127.0.0.1:7890");
    expect(model.remote.port).toBe(2222);
    expect(model.remote.auth.privateKeyPath).toBe("/home/alice/.ssh/openevo");
    expect(model.remote.auth.passphraseRef).toBe("keyring://openevo/lab-gpu");
    expect(model.remote.workspaceRoot).toBe("/data/openevo/workspaces");
    expect(model.remote.proxy.noProxy).toBe("localhost,127.0.0.1");
    expect(model.remote.proxy.pipIndexUrl).toBe(
      "https://pypi.tuna.tsinghua.edu.cn/simple",
    );
    expect(model.remote.proxy.hfHome).toBe("/data/hf-cache");
    expect(model.project.taskId).toBe("folding-baseline");
    expect(model.project.evolutionTargets).toEqual(evolutionTargets());
    expect(model.execution.tokenMetricsAvailable).toBe(false);
    expect(model.bootstrap.readinessNotes).toEqual([
      "Codex subscription login available",
    ]);
    expect(model.sidecar.transport).toEqual({
      id: "ssh",
      label: "SSH transport",
      supportsPasswordRef: false,
      supportsPassphraseRef: false,
    });
    expect(model.developerMode.benchmarkControlsVisible).toBe(false);
  });

  it("maps public self-deployed shell status to the route model", () => {
    const payload = sidecarShellPayload("token-123");

    const model = toOpenEvoDesktopShellModel({
      ...payload,
      execution: {
        mode: "self-deployed",
        model: "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        token_metrics_available: false,
      },
    });

    expect(model.execution).toEqual({
      mode: "self-deployed",
      model: "Qwen/Qwen3-Coder-30B-A3B-Instruct",
      tokenMetricsAvailable: false,
    });
  });

  it("normalizes legacy managed local inference shell status", () => {
    const payload = sidecarShellPayload("token-123");

    const model = toOpenEvoDesktopShellModel({
      ...payload,
      execution: {
        mode: "codex_managed_local_inference",
        model: "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        token_metrics_available: false,
      },
    });

    expect(model.execution.mode).toBe("self-deployed");
  });

  it("maps bootstrap response status to the route model", () => {
    const response = toOpenEvoBootstrapResponse({
      bootstrap: {
        ready: true,
        state_root: "/home/alice/.openevo/runs/protein/folding",
        workspace_root: "/home/alice/.openevo/workspaces",
        readiness_notes: ["Remote bootstrap is ready."],
      },
      report: {
        ready: true,
        prepared_paths: {
          bootstrap_manifest: "/home/alice/.openevo/runs/protein/folding/bootstrap.json",
        },
      },
      status: {
        remote: {
          id: "lab-gpu",
          host: "gpu.example.edu",
          port: 22,
          user: "alice",
          auth: {
            method: "ssh_agent",
            private_key_path: null,
            password_ref: null,
            passphrase_ref: null,
          },
          workspace_root: "/home/alice/.openevo/workspaces",
          proxy: {
            http_proxy: null,
            https_proxy: "http://127.0.0.1:7890",
            no_proxy: null,
            pip_index_url: null,
            huggingface_endpoint: null,
            hf_home: null,
          },
        },
        project: {
          name: "Protein Folding Literature Sprint",
          task_id: "folding-baseline",
          source: "Remote path: /datasets/folding-baseline",
          objective: "Improve folding.",
          evolution_targets: evolutionTargets(),
        },
        execution: {
          mode: "codex_subscription_transcript",
          model: "gpt-5.1-codex-mini",
          token_metrics_available: false,
        },
        bootstrap: {
          ready: true,
          state_root: "/home/alice/.openevo/runs/protein/folding",
          workspace_root: "/home/alice/.openevo/workspaces",
          readiness_notes: ["Remote bootstrap is ready."],
        },
        services: [],
        evolution: [],
        developer_mode: {
          enabled: false,
          benchmark_controls_visible: false,
        },
      },
    });

    expect(response.bootstrap.ready).toBe(true);
    expect(response.status.bootstrap.readinessNotes).toEqual([
      "Remote bootstrap is ready.",
    ]);
    expect(response.report.prepared_paths).toEqual({
      bootstrap_manifest: "/home/alice/.openevo/runs/protein/folding/bootstrap.json",
    });
  });

  it("sends the sidecar mutation token on bootstrap requests", async () => {
    const calls: Array<{ path: string; headers: Headers }> = [];
    const shellPayload = sidecarShellPayload("token-123");
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const path = String(input);
        calls.push({
          path,
          headers: new Headers(init?.headers),
        });
        if (path === "/openevo-api/desktop/shell") {
          return jsonResponse(shellPayload);
        }
        if (path === "/openevo-api/desktop/bootstrap") {
          return jsonResponse({
            bootstrap: shellPayload.bootstrap,
            report: { ready: true },
            status: shellPayload,
          });
        }
        return new Response("not found", { status: 404 });
      },
    );

    await fetchOpenEvoDesktopShellModel();
    await runOpenEvoBootstrap();

    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({ path: "/openevo-api/desktop/bootstrap" });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe("token-123");
  });

  it("sends the sidecar mutation token on workspace sync requests", async () => {
    const calls: Array<{ path: string; headers: Headers }> = [];
    const shellPayload = sidecarShellPayload("workspace-token");
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const path = String(input);
        calls.push({
          path,
          headers: new Headers(init?.headers),
        });
        if (path === "/openevo-api/desktop/shell") {
          return jsonResponse(shellPayload);
        }
        if (path === "/openevo-api/desktop/workspace") {
          return jsonResponse({
            workspace: { ready: true, actions: [] },
            report: { ready: true },
            status: shellPayload,
          });
        }
        return new Response("not found", { status: 404 });
      },
    );

    await fetchOpenEvoDesktopShellModel();
    const response = await runOpenEvoWorkspaceSync();

    expect(response.workspace.ready).toBe(true);
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({ path: "/openevo-api/desktop/workspace" });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe(
      "workspace-token",
    );
  });

  it("sends the sidecar mutation token on service start requests", async () => {
    const calls: Array<{ path: string; headers: Headers }> = [];
    const shellPayload = sidecarShellPayload("services-token");
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const path = String(input);
        calls.push({
          path,
          headers: new Headers(init?.headers),
        });
        if (path === "/openevo-api/desktop/shell") {
          return jsonResponse(shellPayload);
        }
        if (path === "/openevo-api/desktop/services") {
          return jsonResponse({
            services: {
              ready: true,
              state_root: "/home/alice/.openevo/runs/protein/folding",
              topology_path:
                "/home/alice/.openevo/runs/protein/folding/services/topology.yaml",
            },
            report: {
              ready: true,
              steps: [{ id: "rollout", status: "pass", message: "ready" }],
            },
            status: shellPayload,
          });
        }
        return new Response("not found", { status: 404 });
      },
    );

    await fetchOpenEvoDesktopShellModel();
    const response = await runOpenEvoServices();

    expect(response.services.ready).toBe(true);
    expect(response.services.topology_path).toContain("topology.yaml");
    expect(response.report.steps[0].id).toBe("rollout");
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({ path: "/openevo-api/desktop/services" });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe(
      "services-token",
    );
  });

  it("sends the sidecar mutation token on start run requests", async () => {
    const calls: Array<{ path: string; headers: Headers }> = [];
    const shellPayload = sidecarShellPayload("run-token");
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const path = String(input);
        calls.push({
          path,
          headers: new Headers(init?.headers),
        });
        if (path === "/openevo-api/desktop/shell") {
          return jsonResponse(shellPayload);
        }
        if (path === "/openevo-api/desktop/run") {
          return jsonResponse({
            run: sidecarRunReport(),
            status: shellPayload,
          });
        }
        return new Response("not found", { status: 404 });
      },
    );

    await fetchOpenEvoDesktopShellModel();
    const response = await runOpenEvoStartRun();

    expect(response.run.id).toBe("run_20260707170000000000");
    expect(response.run.state).toBe("running");
    expect(response.run.ready).toBe(false);
    expect(response.run.outputDir).toBe(
      "/home/alice/.openevo/runs/protein/folding/runs/run_20260707170000000000",
    );
    expect(response.run.finishedAt).toBeNull();
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({ path: "/openevo-api/desktop/run" });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe("run-token");
  });

  it("sends the sidecar mutation token on run status polling", async () => {
    const calls: Array<{ path: string; headers: Headers; method: string }> = [];
    const shellPayload = sidecarShellPayload("poll-token");
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const path = String(input);
        calls.push({
          path,
          headers: new Headers(init?.headers),
          method: init?.method ?? "GET",
        });
        if (path === "/openevo-api/desktop/shell") {
          return jsonResponse(shellPayload);
        }
        if (path === "/openevo-api/desktop/run") {
          return jsonResponse({
            run: sidecarRunReport({ state: "succeeded" }),
            status: shellPayload,
          });
        }
        return new Response("not found", { status: 404 });
      },
    );

    await fetchOpenEvoDesktopShellModel();
    const response = await pollOpenEvoRunStatus();

    expect(response.run.state).toBe("succeeded");
    expect(response.run.ready).toBe(true);
    expect(response.run.returnCode).toBe(0);
    expect(response.run.finishedAt).toBe("2026-07-07T17:01:00+00:00");
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({
      path: "/openevo-api/desktop/run",
      method: "GET",
    });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe("poll-token");
  });

  it("maps backend run artifacts after terminal runs", async () => {
    const calls: Array<{ path: string; headers: Headers; method: string }> = [];
    const shellPayload = sidecarShellPayload("artifact-token");
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const path = String(input);
        calls.push({
          path,
          headers: new Headers(init?.headers),
          method: init?.method ?? "GET",
        });
        if (path === "/openevo-api/desktop/shell") {
          return jsonResponse(shellPayload);
        }
        if (
          path ===
          "/openevo-api/backend/runs/run_20260707170000000000/artifacts"
        ) {
          return jsonResponse(backendRunArtifactsPayload());
        }
        return new Response("not found", { status: 404 });
      },
    );

    await fetchOpenEvoDesktopShellModel();
    const artifacts = await fetchOpenEvoBackendRunArtifacts(
      "run_20260707170000000000",
    );

    expect(artifacts).toEqual([
      {
        id: "artifact-text-memory",
        runId: "run_20260707170000000000",
        artifactType: "text_memory",
        title: "Initial memory draft",
        promoted: true,
        lineage: {
          method: "text_memory_reflector",
          dataset_id: "dataset-artifact-1",
        },
      },
    ]);
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({
      path: "/openevo-api/backend/runs/run_20260707170000000000/artifacts",
      method: "GET",
    });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe(
      "artifact-token",
    );
  });

  it("maps backend timeline events after terminal runs", async () => {
    const calls: Array<{ path: string; headers: Headers; method: string }> = [];
    const shellPayload = sidecarShellPayload("timeline-token");
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const path = String(input);
        calls.push({
          path,
          headers: new Headers(init?.headers),
          method: init?.method ?? "GET",
        });
        if (path === "/openevo-api/desktop/shell") {
          return jsonResponse(shellPayload);
        }
        if (
          path ===
          "/openevo-api/backend/runs/run_20260707170000000000/timeline"
        ) {
          return jsonResponse(backendRunTimelinePayload());
        }
        return new Response("not found", { status: 404 });
      },
    );

    await fetchOpenEvoDesktopShellModel();
    const timeline = await fetchOpenEvoBackendRunTimeline(
      "run_20260707170000000000",
    );

    expect(timeline).toEqual([
      {
        id: "event-memory",
        phase: "evolution",
        label: "Memory updated",
        message: "Text memory worker promoted one artifact.",
        artifactIds: ["artifact-text-memory"],
      },
    ]);
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({
      path: "/openevo-api/backend/runs/run_20260707170000000000/timeline",
      method: "GET",
    });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe(
      "timeline-token",
    );
  });

  it("fetches and parses target-rooted remote capabilities for an execution mode", async () => {
    const calls: Array<{ path: string; method: string; headers: Headers }> = [];
    const shellPayload = sidecarShellPayload("capabilities-token");
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = String(input);
      calls.push({
        path,
        method: init?.method ?? "GET",
        headers: new Headers(init?.headers),
      });
      if (path === "/openevo-api/desktop/shell") {
        return jsonResponse(shellPayload);
      }
      if (
        path ===
        "/openevo-api/desktop/capabilities?execution_mode=codex_subscription_transcript"
      ) {
        return jsonResponse(remoteCapabilitiesPayload());
      }
      return new Response("not found", { status: 404 });
    });

    await fetchOpenEvoDesktopShellModel();
    const capabilities = await fetchOpenEvoDesktopCapabilities(
      "codex_subscription_transcript",
    );

    expect(capabilities.schemaVersion).toBe("1");
    expect(capabilities.registryDigest).toBe("a".repeat(64));
    expect(capabilities.evaluatedProfile.executionMode).toBe("subscription");
    expect(capabilities.targets[0]).toMatchObject({
      targetId: "text_memory",
      artifactType: "text_memory",
      configuredDefaultMethodId: "text_memory_reflector",
      effectiveDefaultMethodId: "text_memory_reflector",
      implementationIdentityDigest: "b".repeat(64),
      handlerIdentityDigest: "c".repeat(64),
    });
    expect(capabilities.targets[0]?.methods[0]).toMatchObject({
      methodId: "text_memory_reflector",
      configSchema: {
        additionalProperties: false,
        properties: { threshold: { type: "number" } },
        type: "object",
      },
      defaultConfig: { threshold: 0.75 },
      implementationIdentityDigest: "d".repeat(64),
      support: { overall: "supported" },
    });
    expect(capabilities.targets[0]?.methods[0]?.configSchemaJson).toBe(
      '{"additionalProperties":false,"properties":{"threshold":{"type":"number"}},"type":"object"}',
    );
    expect(capabilities.targets[0]?.methods[0]?.defaultConfigJson).toBe(
      '{"threshold":0.75}',
    );
    expect(calls[1]?.headers.get("X-OpenEvo-Sidecar-Token")).toBe(
      "capabilities-token",
    );
    expect(calls.map(({ path, method }) => ({ path, method }))).toEqual([
      { path: "/openevo-api/desktop/shell", method: "GET" },
      {
        path:
          "/openevo-api/desktop/capabilities?execution_mode=codex_subscription_transcript",
        method: "GET",
      },
    ]);
  });

  it("preserves unsupported reasons and a missing effective default", async () => {
    const payload = remoteCapabilitiesPayload();
    payload.targets[0].effective_default_method_id = null;
    payload.targets[0].configured_default_support = unsupportedSupport(
      "unsupported_execution_mode",
      "The configured default does not support self-deployed execution.",
    );
    payload.targets[0].methods[0].support =
      payload.targets[0].configured_default_support;

    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(payload));
    const capabilities = await fetchOpenEvoDesktopCapabilities("self-deployed");

    expect(capabilities.targets[0]?.effectiveDefaultMethodId).toBeNull();
    expect(capabilities.targets[0]?.configuredDefaultSupport).toMatchObject({
      overall: "unsupported",
      execution: {
        state: "unsupported",
        reasonCode: "unsupported_execution_mode",
        message: "The configured default does not support self-deployed execution.",
      },
    });
  });

  it("rejects malformed capability config JSON", async () => {
    const payload = remoteCapabilitiesPayload();
    payload.targets[0].methods[0].default_config_json = '{"threshold":';
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(payload));

    await expect(
      fetchOpenEvoDesktopCapabilities("codex_subscription_transcript"),
    ).rejects.toThrow("default_config_json must contain canonical JSON");
  });

  it("accepts Python canonical numeric spellings in validated config JSON", async () => {
    const payload = remoteCapabilitiesPayload();
    payload.targets[0].methods[0].config_schema_json =
      '{"additionalProperties":false,"properties":{"float":{"type":"number"},"negative_zero":{"type":"number"},"small":{"type":"number"}},"type":"object"}';
    payload.targets[0].methods[0].default_config_json =
      '{"float":1.0,"negative_zero":-0.0,"small":1e-07}';
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(payload));

    const capabilities = await fetchOpenEvoDesktopCapabilities(
      "codex_subscription_transcript",
    );

    const config = capabilities.targets[0]?.methods[0]?.defaultConfig;
    expect(config).toMatchObject({ float: 1, small: 1e-7 });
    expect(Object.is(config?.negative_zero, -0)).toBe(true);
  });

  it.each(["null", "[]", "1", '"value"'])(
    "rejects non-object capability config JSON %s",
    async (encoded) => {
      const payload = remoteCapabilitiesPayload();
      payload.targets[0].methods[0].default_config_json = encoded;
      vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(payload));

      await expect(
        fetchOpenEvoDesktopCapabilities("codex_subscription_transcript"),
      ).rejects.toThrow("default_config_json must contain canonical JSON");
    },
  );

  it.each([
    [
      "config_schema_json",
      '{"additionalProperties":false,"patternProperties":{},"properties":{},"type":"object"}',
      "unsupported schema keyword",
    ],
    ["default_config_json", '{"unknown":true}', "unknown property"],
    [
      "default_config_json",
      '{"threshold":9007199254740992}',
      "safe integer range",
    ],
  ])(
    "rejects an unrenderable capability %s contract",
    async (field, encoded, message) => {
      const payload = remoteCapabilitiesPayload();
      payload.targets[0].methods[0][field] = encoded;
      vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(payload));

      await expect(
        fetchOpenEvoDesktopCapabilities("codex_subscription_transcript"),
      ).rejects.toThrow(message);
    },
  );

  it("fetches backend artifact preview with the sidecar token", async () => {
    const calls: Array<{ path: string; headers: Headers; method: string }> = [];
    const shellPayload = sidecarShellPayload("content-token");
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = String(input);
      calls.push({
        path,
        headers: new Headers(init?.headers),
        method: init?.method ?? "GET",
      });
      if (path === "/openevo-api/desktop/shell") {
        return jsonResponse(shellPayload);
      }
      if (path === "/openevo-api/backend/artifacts/artifact-text-memory/content") {
        return jsonResponse({
          id: "artifact-text-memory",
          artifact_type: "text_memory",
          content: "# Learned Memory\n\n- Prefer stable folds.\n",
          metadata: {
            target_path: "memory.md",
            lineage: {
              method: "text_memory_reflector",
            },
          },
        });
      }
      if (path === "/openevo-api/backend/artifacts/artifact-text-memory/diff") {
        return jsonResponse({
          id: "artifact-text-memory",
          before: "",
          after: "# Learned Memory\n\n- Prefer stable folds.\n",
          format: "unified_text",
        });
      }
      return new Response("not found", { status: 404 });
    });

    await fetchOpenEvoDesktopShellModel();
    const preview = await fetchOpenEvoBackendArtifactPreview(
      "artifact-text-memory",
    );

    expect(preview).toEqual({
      id: "artifact-text-memory",
      kind: "text_memory",
      body: "# Learned Memory\n\n- Prefer stable folds.\n",
      targetPath: "memory.md",
      lineage: {
        method: "text_memory_reflector",
      },
      diff: {
        id: "artifact-text-memory",
        before: "",
        after: "# Learned Memory\n\n- Prefer stable folds.\n",
        format: "unified_text",
      },
    });
    expect(calls[1]).toMatchObject({
      path: "/openevo-api/backend/artifacts/artifact-text-memory/content",
      method: "GET",
    });
    expect(calls[2]).toMatchObject({
      path: "/openevo-api/backend/artifacts/artifact-text-memory/diff",
      method: "GET",
    });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe(
      "content-token",
    );
    expect(calls[2].headers.get("X-OpenEvo-Sidecar-Token")).toBe(
      "content-token",
    );
  });

  it("exercises the ordinary-user desktop API route set with sidecar token preservation", async () => {
    const calls: Array<{ path: string; headers: Headers; method: string }> = [];
    const shellPayload = sidecarShellPayload("smoke-token");
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      calls.push({
        path,
        headers: new Headers(init?.headers),
        method,
      });
      if (
        path ===
        "/openevo-api/desktop/capabilities?execution_mode=codex_subscription_transcript"
      ) {
        return jsonResponse(remoteCapabilitiesPayload());
      }
      if (path === "/openevo-api/desktop/shell") {
        return jsonResponse(shellPayload);
      }
      if (path === "/openevo-api/desktop/project-config") {
        return jsonResponse({
          config: {
            science_config_path:
              "/home/alice/.openevo/desktop/projects/protein/science.yaml",
            remote_profile_path:
              "/home/alice/.openevo/desktop/profiles/science-team.yaml",
          },
          status: shellPayload,
        });
      }
      if (path === "/openevo-api/desktop/workspace") {
        return jsonResponse({
          workspace: { ready: true },
          report: { ready: true },
          status: shellPayload,
        });
      }
      if (path === "/openevo-api/desktop/bootstrap") {
        return jsonResponse({
          bootstrap: shellPayload.bootstrap,
          report: { ready: true },
          status: shellPayload,
        });
      }
      if (path === "/openevo-api/desktop/services") {
        return jsonResponse({
          services: { ready: true },
          report: { ready: true },
          status: shellPayload,
        });
      }
      if (path === "/openevo-api/desktop/run") {
        return jsonResponse({
          run: sidecarRunReport({
            state: method === "GET" ? "succeeded" : "running",
          }),
          status: shellPayload,
        });
      }
      if (
        path ===
        "/openevo-api/backend/runs/run_20260707170000000000/timeline"
      ) {
        return jsonResponse(backendRunTimelinePayload());
      }
      if (
        path ===
        "/openevo-api/backend/runs/run_20260707170000000000/artifacts"
      ) {
        return jsonResponse(backendRunArtifactsPayload());
      }
      if (path === "/openevo-api/backend/artifacts/artifact-text-memory/content") {
        return jsonResponse({
          id: "artifact-text-memory",
          artifact_type: "text_memory",
          content: "# Learned Memory\n\n- Prefer stable folds.\n",
          metadata: {
            target_path: "memory.md",
            lineage: {
              method: "text_memory_reflector",
            },
          },
        });
      }
      if (path === "/openevo-api/backend/artifacts/artifact-text-memory/diff") {
        return jsonResponse({
          id: "artifact-text-memory",
          before: "",
          after: "# Learned Memory\n\n- Prefer stable folds.\n",
          format: "unified_text",
        });
      }
      return new Response("not found", { status: 404 });
    });

    await fetchOpenEvoDesktopShellModel();
    const capabilities = await fetchOpenEvoDesktopCapabilities(
      "codex_subscription_transcript",
    );
    await saveOpenEvoProjectConfig(projectConfigDraft());
    await runOpenEvoWorkspaceSync();
    await runOpenEvoBootstrap();
    await runOpenEvoServices();
    await runOpenEvoStartRun();
    await pollOpenEvoRunStatus();
    const timeline = await fetchOpenEvoBackendRunTimeline(
      "run_20260707170000000000",
    );
    const artifacts = await fetchOpenEvoBackendRunArtifacts(
      "run_20260707170000000000",
    );
    const content = await fetchOpenEvoBackendArtifactPreview(
      "artifact-text-memory",
    );

    expect(capabilities.targets[0]?.methods[0]?.methodId).toBe(
      "text_memory_reflector",
    );
    expect(timeline[0]?.artifactIds).toEqual(["artifact-text-memory"]);
    expect(artifacts[0]?.artifactType).toBe("text_memory");
    expect(content.body).toContain("Prefer stable folds");
    expect(calls.map((call) => `${call.method} ${call.path}`)).toEqual([
      "GET /openevo-api/desktop/shell",
      "GET /openevo-api/desktop/capabilities?execution_mode=codex_subscription_transcript",
      "POST /openevo-api/desktop/project-config",
      "POST /openevo-api/desktop/workspace",
      "POST /openevo-api/desktop/bootstrap",
      "POST /openevo-api/desktop/services",
      "POST /openevo-api/desktop/run",
      "GET /openevo-api/desktop/run",
      "GET /openevo-api/backend/runs/run_20260707170000000000/timeline",
      "GET /openevo-api/backend/runs/run_20260707170000000000/artifacts",
      "GET /openevo-api/backend/artifacts/artifact-text-memory/content",
      "GET /openevo-api/backend/artifacts/artifact-text-memory/diff",
    ]);
    for (const call of calls.slice(1)) {
      expect(call.headers.get("X-OpenEvo-Sidecar-Token")).toBe("smoke-token");
    }
  });

  it("sends the sidecar mutation token on project config requests", async () => {
    const calls: Array<{ path: string; headers: Headers; body: any }> = [];
    const shellPayload = sidecarShellPayload("config-token");
    const savedPayload = {
      ...shellPayload,
      project: {
        ...shellPayload.project,
        name: "Configured Project",
      },
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const path = String(input);
        calls.push({
          path,
          headers: new Headers(init?.headers),
          body: init?.body ? JSON.parse(String(init.body)) : undefined,
        });
        if (path === "/openevo-api/desktop/shell") {
          return jsonResponse(shellPayload);
        }
        if (path === "/openevo-api/desktop/project-config") {
          return jsonResponse({
            config: {
              science_config_path:
                "/home/alice/.openevo/desktop/projects/configured/science.yaml",
              remote_profile_path:
                "/home/alice/.openevo/desktop/profiles/science-team.yaml",
            },
            status: savedPayload,
          });
        }
        return new Response("not found", { status: 404 });
      },
    );

    const shellModel = await fetchOpenEvoDesktopShellModel();
    const response = await saveOpenEvoProjectConfig(toDraftPayload(shellModel));

    expect(response.config.science_config_path).toContain("science.yaml");
    expect(response.status.project.name).toBe("Configured Project");
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({
      path: "/openevo-api/desktop/project-config",
    });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe(
      "config-token",
    );
    expect(calls[1].body.remote_host).toBe("gpu.example.edu");
    expect(calls[1].body.evolution).toEqual({
      targets: evolutionTargets(),
    });
    expect(calls[1].body).not.toHaveProperty("text_memory");
    expect(calls[1].body).not.toHaveProperty("skill_bundle");
    expect(calls[1].body).not.toHaveProperty("agent_system");
  });

  it("sends self-deployed as the public project config mode", async () => {
    const calls: Array<{ path: string; headers: Headers; body: any }> = [];
    const shellPayload = sidecarShellPayload("config-token");
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const path = String(input);
        calls.push({
          path,
          headers: new Headers(init?.headers),
          body: init?.body ? JSON.parse(String(init.body)) : undefined,
        });
        if (path === "/openevo-api/desktop/shell") {
          return jsonResponse(shellPayload);
        }
        if (path === "/openevo-api/desktop/project-config") {
          return jsonResponse({
            config: {
              science_config_path:
                "/home/alice/.openevo/desktop/projects/configured/science.yaml",
              remote_profile_path:
                "/home/alice/.openevo/desktop/profiles/science-team.yaml",
            },
            status: {
              ...shellPayload,
              execution: {
                mode: "self-deployed",
                model: "Qwen/Qwen3-Coder-30B-A3B-Instruct",
                token_metrics_available: false,
              },
            },
          });
        }
        return new Response("not found", { status: 404 });
      },
    );

    await fetchOpenEvoDesktopShellModel();
    await saveOpenEvoProjectConfig({
      ...projectConfigDraft(),
      execution_mode: "self-deployed",
      codex_model: null,
      hf_model: "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    });

    expect(calls[1].body).toMatchObject({
      execution_mode: "self-deployed",
      codex_model: null,
      hf_model: "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    });
  });

  it("fetches saved project config summaries", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = String(input);
      if (path === "/openevo-api/desktop/project-configs") {
        return jsonResponse({
          configs: [
            {
              project_slug: "protein-design",
              valid: true,
              error: null,
              project_name: "Protein Design",
              task_id: "folding-baseline",
              objective: "Improve the folding baseline.",
              source_type: "git_repository",
              source_label: "https://example.com/repo.git@main",
              remote_profile_id: "science-team",
              remote_host: "gpu.example.edu",
              remote_user: "alice",
              science_config_path:
                "/home/alice/.openevo/desktop/projects/protein-design/science.yaml",
              remote_profile_path:
                "/home/alice/.openevo/desktop/profiles/science-team.yaml",
            },
            {
              project_slug: "broken-project",
              valid: false,
              error: "profiles/broken.yaml: not found",
              project_name: "Broken Project",
              task_id: "broken-task",
              objective: "Repair the config.",
              source_type: "remote_path",
              source_label: "/datasets/broken",
              remote_profile_id: "broken",
              remote_host: null,
              remote_user: null,
              science_config_path:
                "/home/alice/.openevo/desktop/projects/broken-project/science.yaml",
              remote_profile_path:
                "/home/alice/.openevo/desktop/profiles/broken.yaml",
            },
          ],
        });
      }
      return new Response("not found", { status: 404 });
    });

    const configs = await fetchOpenEvoProjectConfigs();

    expect(configs).toHaveLength(2);
    expect(configs[0]).toMatchObject({
      projectSlug: "protein-design",
      valid: true,
      projectName: "Protein Design",
      sourceLabel: "https://example.com/repo.git@main",
      remoteHost: "gpu.example.edu",
      scienceConfigPath:
        "/home/alice/.openevo/desktop/projects/protein-design/science.yaml",
    });
    expect(configs[1]).toMatchObject({
      projectSlug: "broken-project",
      valid: false,
      error: "profiles/broken.yaml: not found",
      remoteHost: null,
      remoteUser: null,
    });
  });

  it("activates a saved project config with the sidecar mutation token", async () => {
    const calls: Array<{ path: string; headers: Headers; method: string }> = [];
    const shellPayload = sidecarShellPayload("activate-token");
    const activatedPayload = {
      ...shellPayload,
      project: {
        ...shellPayload.project,
        name: "Activated Science Project",
      },
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const path = String(input);
        calls.push({
          path,
          headers: new Headers(init?.headers),
          method: init?.method ?? "GET",
        });
        if (path === "/openevo-api/desktop/shell") {
          return jsonResponse(shellPayload);
        }
        if (
          path ===
          "/openevo-api/desktop/project-configs/protein-design/activate"
        ) {
          return jsonResponse({
            config: {
              science_config_path:
                "/home/alice/.openevo/desktop/projects/protein-design/science.yaml",
              remote_profile_path:
                "/home/alice/.openevo/desktop/profiles/science-team.yaml",
            },
            status: activatedPayload,
          });
        }
        return new Response("not found", { status: 404 });
      },
    );

    await fetchOpenEvoDesktopShellModel();
    const response = await activateOpenEvoProjectConfig("protein-design");

    expect(response.status.project.name).toBe("Activated Science Project");
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({
      path: "/openevo-api/desktop/project-configs/protein-design/activate",
      method: "POST",
    });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe(
      "activate-token",
    );
  });
});

function sidecarShellPayload(mutationToken: string) {
  return {
    remote: {
      id: "lab-gpu",
      host: "gpu.example.edu",
      port: 22,
      user: "alice",
      auth: {
        method: "ssh_agent" as const,
        private_key_path: null,
        password_ref: null,
        passphrase_ref: null,
      },
      workspace_root: "/home/alice/.openevo/workspaces",
      proxy: {
        http_proxy: null,
        https_proxy: "http://127.0.0.1:7890",
        no_proxy: null,
        pip_index_url: null,
        huggingface_endpoint: null,
        hf_home: null,
      },
    },
    project: {
      name: "Protein Folding Literature Sprint",
      task_id: "folding-baseline",
      source: "Remote path: /datasets/folding-baseline",
      objective: "Improve folding.",
      evolution_targets: evolutionTargets(),
    },
    execution: {
      mode: "codex_subscription_transcript" as const,
      model: "gpt-5.1-codex-mini",
      token_metrics_available: false,
    },
    bootstrap: {
      ready: true,
      state_root: "/home/alice/.openevo/runs/protein/folding",
      workspace_root: "/home/alice/.openevo/workspaces",
      readiness_notes: ["Remote bootstrap is ready."],
    },
    services: [],
    evolution: [],
    developer_mode: {
      enabled: false,
      benchmark_controls_visible: false,
    },
    sidecar: {
      mutation_token: mutationToken,
    },
  };
}

function sidecarRunReport({
  state = "running",
}: {
  state?: "running" | "succeeded" | "failed";
} = {}) {
  const succeeded = state === "succeeded";
  const failed = state === "failed";
  return {
    id: "run_20260707170000000000",
    state,
    ready: succeeded,
    command:
      "openevo-backend run /home/alice/.openevo/runs/protein/folding/experiment.json --output-dir /home/alice/.openevo/runs/protein/folding/runs/run_20260707170000000000 --artifact-root /home/alice/.openevo/runs/protein/folding/evolution/artifacts --json",
    return_code: state === "running" ? null : succeeded ? 0 : 2,
    stdout: succeeded ? "ok" : "",
    stderr: failed ? "run failed" : "",
    output_dir:
      "/home/alice/.openevo/runs/protein/folding/runs/run_20260707170000000000",
    experiment_snapshot: "/home/alice/.openevo/runs/protein/folding/experiment.json",
    started_at: "2026-07-07T16:00:00+00:00",
    finished_at: state === "running" ? null : "2026-07-07T17:01:00+00:00",
  };
}

function backendRunTimelinePayload() {
  return [
    {
      id: "event-memory",
      phase: "evolution",
      title: "Memory updated",
      message: "Text memory worker promoted one artifact.",
      artifact_ids: ["artifact-text-memory"],
    },
  ];
}

function backendRunArtifactsPayload() {
  return [
    {
      id: "artifact-text-memory",
      run_id: "run_20260707170000000000",
      artifact_type: "text_memory",
      title: "Initial memory draft",
      promoted: true,
      lineage: {
        method: "text_memory_reflector",
        dataset_id: "dataset-artifact-1",
      },
    },
  ];
}

function projectConfigDraft() {
  return {
    project_name: "Protein Design",
    task_id: "folding-baseline",
    objective: "Improve the folding baseline.",
    source_type: "remote_path" as const,
    source_path: "/datasets/folding-baseline",
    remote_profile_id: "science-team",
    remote_host: "gpu.example.edu",
    remote_port: 22,
    remote_user: "alice",
    auth_method: "ssh_agent" as const,
    workspace_root: "/home/alice/.openevo/workspaces",
    https_proxy: "http://127.0.0.1:7890",
    huggingface_endpoint: "https://hf-mirror.com",
    execution_mode: "codex_subscription_transcript" as const,
    codex_model: "gpt-5.1-codex-mini",
    evolution: { targets: evolutionTargets() },
  };
}

function evolutionTargets() {
  return {
    text_memory: {
      enabled: true,
      method: "custom_memory_method",
      config: { threshold: 0.75, nested: { mode: "strict" } },
    },
    skill_bundle: {
      enabled: false,
      method: null,
      config: { draft_prompt: "retain me" },
    },
    agent_system: {
      enabled: true,
      method: "auto",
      config: { target_path: "CLAUDE.md" },
    },
    future_target: {
      enabled: false,
      method: "future_method",
      config: { opaque: [1, 2, 3] },
    },
  };
}

function remoteCapabilitiesPayload(): any {
  const support = supportedSupport();
  return {
    schema_version: "1",
    core_version: "0.1.0",
    registry_digest: "a".repeat(64),
    evaluated_profile: {
      execution_mode: "subscription",
      capture_mode: "transcript",
      harness_id: "codex",
      harness_capabilities: ["stable_transcript"],
      runtime_capabilities: [],
    },
    targets: [
      {
        target_id: "text_memory",
        display_name: "Text Memory",
        description: "Reusable natural-language memory.",
        artifact_type: "text_memory",
        exposure: "desktop",
        maturity: "stable",
        handler_id: "text_memory_handler",
        configured_default_method_id: "text_memory_reflector",
        effective_default_method_id: "text_memory_reflector",
        configured_default_support: support,
        renderer_kind: "markdown",
        renderer_contract_version: "1",
        contribution_contract_version: "1",
        context_order: 10,
        implementation_identity_digest: "b".repeat(64),
        handler_identity_digest: "c".repeat(64),
        accepted_methods: [
          {
            method_id: "text_memory_reflector",
            implementation_identity_digest: "d".repeat(64),
            support,
          },
        ],
        selection_resolvers: [],
        methods: [
          {
            method_id: "text_memory_reflector",
            display_name: "Text Memory Reflector",
            description: "Reflects transcripts into reusable memory.",
            exposure: "desktop",
            maturity: "stable",
            execution_modes: ["subscription", "self_deployed"],
            capture_modes: ["transcript"],
            supported_harness_ids: ["codex"],
            harness_requirements: ["stable_transcript"],
            runtime_requirements: [],
            input_bindings: [
              {
                binding_id: "dataset",
                source: "current_dataset",
                artifact_type: "dataset",
                min_count: 1,
                max_count: null,
              },
            ],
            output_artifact_types: ["text_memory"],
            config_schema_json:
              '{"additionalProperties":false,"properties":{"threshold":{"type":"number"}},"type":"object"}',
            default_config_json: '{"threshold":0.75}',
            implementation_identity_digest: "d".repeat(64),
            support,
          },
        ],
      },
    ],
  };
}

function supportedSupport() {
  const axis = {
    state: "supported",
    reason_code: null,
    message: "Supported.",
    missing_requirements: [],
  };
  return {
    overall: "supported",
    execution: { ...axis },
    capture: { ...axis },
    harness: { ...axis },
    runtime: { ...axis },
  };
}

function unsupportedSupport(reasonCode: string, message: string) {
  const support = supportedSupport();
  return {
    ...support,
    overall: "unsupported",
    execution: {
      state: "unsupported",
      reason_code: reasonCode,
      message,
      missing_requirements: [],
    },
  };
}

function jsonResponse(payload: object): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
