/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// No `server.proxy`: spec 16 §4 has the browser talk to the API directly, and the
// backend's CORS list already allows http://localhost:5173.
export default defineConfig({
  plugins: [react()],
  // Vite 8 resolves the "@/…" alias from tsconfig itself -- no plugin.
  resolve: { tsconfigPaths: true },
  server: { port: 5173, strictPort: true },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: true,
    // Set here rather than `TZ=… vitest`, which is not valid PowerShell, so a
    // date test cannot pass on this machine and drift on a UTC runner.
    env: { TZ: "Europe/Berlin" },
  },
});
