import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";

export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss()],
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
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
}));
