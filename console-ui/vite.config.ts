/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  test: {
    // A35.7/A35.9: the contrast contract and the token lint read the
    // stylesheets as text. Vitest hands every other CSS import an empty
    // module, so only the token sources are passed through.
    css: { include: [/src\/styles\/[\w-]+\.css(\?|$)/] },
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8100",
      "/healthz": "http://127.0.0.1:8100",
    },
  },
});
