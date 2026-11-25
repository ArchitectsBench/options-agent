/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 8420,
    proxy: {
      "/api": "http://localhost:8421",
      "/ws": { target: "ws://localhost:8421", ws: true },
    },
  },
  // interaction regression suite — src/components/__tests__/ (run: npm test)
  test: {
    environment: "jsdom",
    globals: true,
  },
});
