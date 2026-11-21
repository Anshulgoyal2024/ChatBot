import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backendTarget = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 3001,
    strictPort: true,
    proxy: {
      "/chat": backendTarget,
      "/health": backendTarget,
      "/sessions": backendTarget,
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
