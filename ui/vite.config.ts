import { defineConfig } from "vitest/config";
import type { ProxyOptions } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { svelteTesting } from "@testing-library/svelte/vite";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// core/VERSION is the one home of the product version (spec 8); the build reads it.
const version = readFileSync(resolve(import.meta.dirname, "../core/VERSION"), "utf8").trim();

export default defineConfig(() => {
  // No default for the dev proxy: a default would point a developer's browser at
  // whatever relay listens on this host, and on the workstation that is the live
  // one. `scripts/require-core.mjs` (the `predev` script) refuses `npm run dev`
  // without it; here an unset variable only means no proxy (Vitest and
  // svelte-check load this config too, and must not fail on it).
  const core = process.env.LOKKI_CORE;
  const coreProxy = (target: string): ProxyOptions => ({
    target,
    changeOrigin: true,
    configure(proxy) {
      const origin = new URL(target).origin;
      proxy.on("proxyReq", (request) => request.setHeader("origin", origin));
    },
  });
  return {
    plugins: [svelte(), svelteTesting()],
    base: "/static/",                       // the core serves index.html at / and the rest under /static/
    // The engines the macOS floor names (docs/app/ui.md, "Engines"): the WKWebView of macOS
    // 12.4 and WebView2 at its oklch() floor. Vite's own default leaves syntax newer than
    // Safari 15.5 unlowered, a SyntaxError and a blank window there.
    build: { outDir: "../core/lokki_core/static", emptyOutDir: false, target: ["safari15.5", "chrome111"] },
    define: { __LOKKI_VERSION__: JSON.stringify(version) },
    server: core ? { proxy: {
      "/api": coreProxy(core),
      "/healthz": coreProxy(core),
      "/version": coreProxy(core),
    } } : undefined,
    // The suite runs on a shared workstation and on CI's hosted runners, both slower than a quiet machine:
    // the limits are there to catch a hang, not to time a test.
    test: {
      environment: "jsdom", setupFiles: ["./src/test-setup.ts"], include: ["src/**/*.test.ts"],
      testTimeout: 30_000, hookTimeout: 30_000,
    },
  };
});
