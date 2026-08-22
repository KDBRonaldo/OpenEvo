import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const selfHostedWebuiBuild = mode === "openevo-self-hosted-webui";
  const sourceDevelopmentBuild =
    selfHostedWebuiBuild ||
    process.env.VITE_OPENEVO_SOURCE_DEVELOPMENT?.trim() === "1" ||
    env.VITE_OPENEVO_SOURCE_DEVELOPMENT?.trim() === "1";
  const developmentAgentMode = mode === "openevo-live-agent";
  const developmentAgentWebMode = mode === "openevo-live-agent-web";
  const developmentAgentToken = env.OPENEVO_DEV_AGENT_TOKEN?.trim();
  const developmentAgentTarget = env.OPENEVO_DEV_AGENT_URL?.trim() || "http://127.0.0.1:8765";
  const developmentWebTarget = env.OPENEVO_DEV_WEB_URL?.trim() || "http://127.0.0.1:8766";

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
    plugins: [
      react(),
      tailwindcss(),
      ...(selfHostedWebuiBuild || developmentAgentWebMode ? [{
        name: "openevo-web-layer-product-entry",
        transformIndexHtml: {
          order: "pre" as const,
          handler(html: string) {
            return html
              .replace(
                '<script type="module" src="/src/main.tsx"></script>',
                '<script type="module" src="/self-hosted-product-preview.tsx"></script>',
              )
              .replace(
                '<script type="module" src="/src/product/preview.tsx"></script>',
                '<script type="module" src="/self-hosted-product-preview.tsx"></script>',
              );
          },
        },
      }] : []),
    ],
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
        mode === "openevo-desktop" || mode === "openevo-self-hosted-webui"
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
      strictPort: true,
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
        ...(developmentAgentWebMode ? {
          "/openevo-dev-agent": {
            target: developmentWebTarget,
            changeOrigin: false,
          },
          "/desktop/v2": { target: developmentWebTarget, changeOrigin: false },
          "/version": { target: developmentWebTarget, changeOrigin: false },
          "/health": { target: developmentWebTarget, changeOrigin: false },
          "/openevo-native": { target: developmentWebTarget, changeOrigin: false },
        } : {}),
      },
    },
    build: {
      outDir: "dist",
      emptyOutDir: true,
    },
  };
});
