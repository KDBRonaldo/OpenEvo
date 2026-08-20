import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const sourceDevelopmentBuild =
    process.env.VITE_OPENEVO_SOURCE_DEVELOPMENT?.trim() === "1" ||
    env.VITE_OPENEVO_SOURCE_DEVELOPMENT?.trim() === "1";
  const developmentAgentMode = mode === "openevo-live-agent";
  const developmentAgentToken = env.OPENEVO_DEV_AGENT_TOKEN?.trim();
  const developmentAgentTarget = env.OPENEVO_DEV_AGENT_URL?.trim() || "http://127.0.0.1:8765";

  if (developmentAgentMode) {
    if (!developmentAgentToken) {
      throw new Error("OPENEVO_DEV_AGENT_TOKEN is required for the live-agent development mode.");
    }
    const targetUrl = new URL(developmentAgentTarget);
    if (targetUrl.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(targetUrl.hostname)) {
      throw new Error("OPENEVO_DEV_AGENT_URL must be an HTTP loopback URL reached through the SSH tunnel.");
    }
  }

  return {
    plugins: [react(), tailwindcss()],
    // Source-development browser builds still exercise the formal Desktop
    // contract, but intentionally report a development build channel.  Make
    // the opt-in explicit in the compiled renderer instead of relying on
    // Vite's ambient env replacement, which is not guaranteed when the build
    // is launched through the Python preparation wrapper.
    define: {
      "import.meta.env.VITE_OPENEVO_SOURCE_DEVELOPMENT": JSON.stringify(
        sourceDevelopmentBuild ? "1" : "",
      ),
    },
    resolve: {
      alias:
        mode === "openevo-desktop"
          ? [
              {
                find: /^\.\/providerKinds$/,
                replacement: fileURLToPath(
                  new URL("./src/api/v1/providerKinds.release.ts", import.meta.url),
                ),
              },
            ]
          : [],
    },
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy: {
        "/api": {
          target: "http://127.0.0.1:8090",
          changeOrigin: true,
        },
        "/openevo-api": {
          target: "http://127.0.0.1:3766",
          changeOrigin: true,
        },
        ...(developmentAgentMode ? {
          "/openevo-dev-agent": {
            target: developmentAgentTarget,
            changeOrigin: false,
            headers: { Authorization: `Bearer ${developmentAgentToken}` },
          },
        } : {}),
      },
    },
    build: {
      outDir: "dist",
      emptyOutDir: true,
    },
  };
});
