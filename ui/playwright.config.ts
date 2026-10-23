import { defineConfig, devices } from "@playwright/test";
import { mkdtempSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Traces, screenshots and videos stay off: the page holds the operator token. Playwright
// writes a page snapshot (error-context.md) into its output directory when a test fails and
// has no switch for it, so that directory is a mkdtemp under the temp root, never in the checkout.
//
// Playwright loads this file in the runner and again in every worker. The first load makes the
// directory and names it in the environment, which the workers inherit; only that load removes
// it, when the run ends green. A failed run keeps it and prints where, since the snapshot is what
// a developer reads.
let outputDir = process.env.LOKKI_E2E_OUTPUT;
if (!outputDir) {
  const made = mkdtempSync(join(realpathSync(tmpdir()), "lokki-pw-"));
  outputDir = process.env.LOKKI_E2E_OUTPUT = made;
  process.on("exit", (code) => {
    if (code === 0) rmSync(made, { recursive: true, force: true });
    else console.error(`the failed run's output is kept in ${made}`);
  });
}

export default defineConfig({
  testDir: "e2e",
  outputDir,
  workers: 1,
  timeout: 120_000,               // one long test
  expect: { timeout: 10_000 },
  reporter: [["list", { printSteps: true }]],
  use: { actionTimeout: 10_000, trace: "off", screenshot: "off", video: "off" },
  // `npm run e2e` runs chromium and webkit; `npm run screens` writes the screenshots (e2e/screens.ts).
  projects: [
    { name: "chromium", testMatch: "*.spec.ts", use: { ...devices["Desktop Chrome"] } },
    // WebKit is the engine of WKWebView, the macOS app's webview: the slider's drag only (e2e/slider.spec.ts).
    { name: "webkit", testMatch: "slider.spec.ts", use: { ...devices["Desktop Safari"] } },
    { name: "screens", testMatch: "screens.ts", use: { ...devices["Desktop Chrome"], viewport: { width: 1100, height: 760 } } },
  ],
});
