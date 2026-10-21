import { readFileSync } from "node:fs";
import { join } from "node:path";
import { UNWATCHED_MODES, headline } from "../src/lib/mode.ts";
import { test, expect } from "./core.ts";

// One test, in steps: a fresh page per test would lose the token in sessionStorage.
test("the UI against a real core and a fake CLIProxyAPI", async ({ page, core }) => {
  const api = (path: string) => page.request.get(path, { headers: { "X-Relay-Token": core.token } });
  const settings = async () => (await (await api("/api/settings")).json()) as { routing: string; low_quota: { accounts: string[]; models: string[]; threshold_percent: number } };
  const accountRow = (key: string) => page.locator(`[data-key="${key}"]`);
  const region = (name: string) => page.getByRole("region", { name, exact: true });
  const tab = (name: string) => page.getByRole("button", { name, exact: true }).click();
  const url = new URL(core.url);
  const headers = { "X-Relay-Token": core.token };
  const decision = page.getByRole("banner").getByRole("status");
  const slider = page.getByRole("slider", { name: "Mode and threshold" });
  const words = page.locator(".slider-words");
  const puts: string[] = [];
  page.on("request", (r) => { if (r.method() === "PUT" && new URL(r.url()).pathname === "/api/settings") puts.push(r.postData() ?? ""); });

  await test.step("the token prompt, then Home with the decision after the first read", async () => {
    await page.goto("/");
    await page.getByLabel("Operator token", { exact: true }).fill(core.token);
    await page.getByRole("button", { name: "Use", exact: true }).click();
    await expect(page.getByText(`http://127.0.0.1:${url.port}`).first()).toBeVisible();
    await expect(page.locator("code")).toContainText("…");        // the masked key; the key itself is never shown
    await expect(page.locator("body")).not.toContainText(core.token);
    await expect(decision).toContainText(headline(false));
    await expect(decision.getByText("not every watched account is low", { exact: true })).toBeVisible();
    await expect(words).toHaveText("Cut when every watched account has less than 30 % left · every model");
  });

  await test.step("Home's 24 h bars are the activity answer", async () => {
    const answer = (await (await page.request.get("/api/activity?window=24h", { headers })).json()) as {
      buckets: unknown[]; totals: { relay: number; passthrough: number; images: number; errors: number; requests: number };
    };
    expect(answer.totals.requests).toBeGreaterThan(0);          // the seeded log has runs in the last day
    const chart = page.getByRole("region", { name: "Last 24 h" });
    await expect(chart.locator("[data-bucket]")).toHaveCount(answer.buckets.length);
    for (const [name, n] of [["Relay", answer.totals.relay], ["Pass-through", answer.totals.passthrough], ["Images", answer.totals.images], ["Errors, inside the bars", answer.totals.errors]] as const) {
      await expect(chart.locator(".legend > span").filter({ hasText: name })).toContainText(String(n));
    }
    await expect(chart.getByText("No requests yet")).toHaveCount(0);
  });

  await test.step("Activity shows the run log's counts, in both windows", async () => {
    type Answer = {
      buckets: unknown[]; coverage: { oldest: number | null; complete: boolean }; from: number;
      totals: { relay: number; passthrough: number; images: number; errors: number; requests: number };
      recent: Array<{ outcome: string; route: string }>; events: unknown[];
    };
    const read = async (window: string) => (await (await page.request.get(`/api/activity?window=${window}`, { headers })).json()) as Answer;
    const since = page.getByText(/^(counted )?since .+: (the run log holds runs since then|lokki reads)/);

    await page.getByRole("region", { name: "Last 24 h" }).getByRole("button", { name: "Open Activity" }).click();
    await expect(page.getByRole("heading", { name: "Activity", level: 2 })).toBeVisible();
    const day = await read("24h");
    await expect(page.locator("[data-bucket]")).toHaveCount(day.buckets.length);
    for (const [name, n] of [["Relay", day.totals.relay], ["Pass-through", day.totals.passthrough], ["Images", day.totals.images], ["Errors, inside the bars", day.totals.errors]] as const) {
      await expect(page.locator(".legend > span").filter({ hasText: name })).toContainText(String(n));
    }
    // The first recent row is the API's first, and shows its outcome as the route gave it.
    const rows = page.getByRole("table", { name: "Recent runs" }).locator("tbody tr");
    await expect(rows).toHaveCount(day.recent.length);
    await expect(rows.first().locator(".out")).toHaveText(day.recent[0].outcome);
    await expect(page.getByRole("list", { name: "Decision changes" }).getByRole("listitem")).toHaveCount(day.events.length);
    expect(day.coverage.complete && day.coverage.oldest !== null && day.coverage.oldest > day.from).toBe(false);
    await expect(since).toHaveCount(0);

    await page.getByRole("button", { name: "7 d" }).click();
    await expect(page.locator("[data-bucket]")).toHaveCount(28);
    const week = await read("7d");
    await expect(page.locator(".chart-x i")).toHaveCount(7);
    // The seed starts 72 h back, inside the week: the log starts inside the window and says since when.
    expect(week.coverage.complete && week.coverage.oldest !== null && week.coverage.oldest > week.from).toBe(true);
    await expect(since).toHaveText(/^since (\p{L}+ )?\d\d:\d\d: the run log holds runs since then$/u);
    for (const [name, n] of [["Relay", week.totals.relay], ["Pass-through", week.totals.passthrough], ["Images", week.totals.images]] as const) {
      await expect(page.locator(".legend > span").filter({ hasText: name })).toContainText(String(n));
    }
    await expect(rows.first().locator(".out")).toHaveText(week.recent[0].outcome);
    await page.getByRole("button", { name: "24 h" }).click();
    await expect(page.locator("[data-bucket]")).toHaveCount(24);
    await tab("Home");
  });

  await test.step("Home's gauges are the poller's snapshot", async () => {
    const strip = page.getByRole("region", { name: "Accounts", exact: true });
    await expect(strip.getByRole("meter")).toHaveCount(2);
    await expect(strip.getByRole("meter").first()).toHaveAttribute("aria-valuenow", "20");     // a1, 80 % used
  });

  await test.step("Accounts: two cards from the poller's snapshot", async () => {
    await tab("Accounts");
    await expect(accountRow("a1")).toContainText("20 %");
    await expect(accountRow("a1")).toContainText("low");
    await expect(accountRow("b2")).toContainText("90 %");
    await expect(accountRow("b2")).toContainText("ok");
  });

  await test.step("the scope is written to the file and read back", async () => {
    await page.getByRole("radio", { name: "Only the ones I pick" }).check();
    await accountRow("a1").getByRole("switch", { name: /^Watched/ }).click();
    await expect.poll(async () => (await settings()).low_quota.accounts).toEqual(["a1"]);
    expect(readFileSync(core.configPath, "utf8")).toMatch(/accounts:\s*\n\s*- a1/);
    await page.getByRole("button", { name: "Read now" }).click();
    await expect(accountRow("b2")).toContainText("not watched");
    await expect(accountRow("b2").getByRole("switch")).toHaveAttribute("aria-checked", "false");
    await tab("Home");
    await expect(decision).toContainText(headline(true));
    await expect(decision.getByText("every watched account is low", { exact: true })).toBeVisible();
    await expect(words).toHaveText("Cut when the watched account has less than 30 % left · every model");
  });

  await test.step("the quota is read in every mode", async () => {
    const modes = page.getByRole("radiogroup", { name: "Mode" });
    const answered = () => expect(modes).toHaveAttribute("aria-busy", "false");
    const reads = async () => ((await (await api("/api/status")).json()) as { poll: { reads: number } }).poll.reads;
    await tab("Home");
    await modes.getByRole("radio", { name: "Direct", exact: true }).click();
    await expect(modes.getByRole("radio", { name: "Direct", exact: true })).toHaveAttribute("aria-checked", "true");
    await answered();
    const before = await reads();
    expect((await page.request.post("/api/accounts/refresh", { headers })).ok()).toBe(true);
    expect(await reads()).toBeGreaterThan(before);
    await page.reload();
    await expect(page.getByRole("region", { name: "Accounts", exact: true }).getByRole("meter")).toHaveCount(2);
    await modes.getByRole("radio", { name: /^Auto/ }).click();
    await expect(modes.getByRole("radio", { name: /^Auto/ })).toHaveAttribute("aria-checked", "true");
    await answered();
    expect((await settings()).routing).toBe("auto");
  });

  await test.step("the slider writes a mode at its ends and Auto with its threshold between them", async () => {
    const modes = page.getByRole("radiogroup", { name: "Mode" });
    // A run of key steps writes once when its presses land within 500 ms of each other; a loaded host can space
    // them further, and then a step it passed through is written too. So a run here holds its last body and that
    // every PUT is a position it passed through; ModeSlider.test.ts holds the exactly-once with fake timers.
    const auto = (p: number) => `{"routing":"auto","low_quota":{"threshold_percent":${p}}}`;
    const through = (...ps: number[]) => expect(puts.every((b) => ps.map(auto).includes(b)), puts.join(" ")).toBe(true);
    // Each run of keys is one write; the next waits for it and its reads to answer.
    const answered = () => expect(slider).toHaveAttribute("aria-busy", "false");
    await tab("Home");
    await expect(slider).toHaveValue("30");
    puts.length = 0;
    await slider.focus();
    await page.keyboard.press("End");
    await expect.poll(() => puts.length).toBe(1);
    await answered();
    expect(puts).toEqual(['{"routing":"relay"}']);
    let now = await settings();
    expect([now.routing, now.low_quota.threshold_percent]).toEqual(["relay", 30]);     // an end leaves the threshold
    await expect(modes.getByRole("radio", { name: "Relay", exact: true })).toHaveAttribute("aria-checked", "true");

    await modes.getByRole("radio", { name: "Auto 30 %", exact: true }).click();
    await expect(modes.getByRole("radio", { name: "Auto 30 %", exact: true })).toHaveAttribute("aria-checked", "true");
    await answered();
    now = await settings();
    expect([now.routing, now.low_quota.threshold_percent]).toEqual(["auto", 30]);
    await expect(slider).toHaveValue("30");

    puts.length = 0;
    await slider.focus();
    for (let i = 0; i < 5; i++) await page.keyboard.press("ArrowRight");
    await expect(modes.getByRole("radio", { name: "Auto 35 %", exact: true })).toHaveAttribute("aria-checked", "true");
    await answered();
    expect(puts.at(-1)).toBe(auto(35));
    through(31, 32, 33, 34, 35);
    expect(((await (await api("/api/status")).json()) as { low_quota: { threshold_percent: number } }).low_quota.threshold_percent).toBe(35);

    puts.length = 0;
    await page.keyboard.press("PageDown");
    await expect(modes.getByRole("radio", { name: "Auto 25 %", exact: true })).toHaveAttribute("aria-checked", "true");
    await answered();
    expect(puts).toEqual([auto(25)]);                               // one press, one write
    puts.length = 0;
    for (let i = 0; i < 5; i++) await page.keyboard.press("ArrowRight");
    await expect(modes.getByRole("radio", { name: "Auto 30 %", exact: true })).toHaveAttribute("aria-checked", "true");
    await answered();
    expect(puts.at(-1)).toBe(auto(30));
    through(26, 27, 28, 29, 30);
    expect((await settings()).low_quota.threshold_percent).toBe(30);

    // A drag writes once, when the pointer lets go: from 30 to the track's 60 %, then back.
    const box = (await slider.boundingBox())!;
    const at = (p: number) => box.x + 10 + ((box.width - 20) * p) / 100;
    puts.length = 0;
    await page.mouse.move(at(30), box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(at(45), box.y + box.height / 2, { steps: 5 });
    await page.mouse.move(at(60), box.y + box.height / 2, { steps: 5 });
    expect(puts).toEqual([]);
    await page.mouse.up();
    await expect.poll(() => puts.length).toBe(1);
    await answered();
    const dragged = Number(await slider.inputValue());
    expect(Math.abs(dragged - 60)).toBeLessThanOrEqual(1);
    expect(puts).toEqual([`{"routing":"auto","low_quota":{"threshold_percent":${dragged}}}`]);
    await slider.focus();
    await page.keyboard.press("PageDown");
    await page.keyboard.press("PageDown");
    await page.keyboard.press("PageDown");
    for (let i = 0; i < dragged - 60; i++) await page.keyboard.press("ArrowLeft");
    for (let i = 0; i < 60 - dragged; i++) await page.keyboard.press("ArrowRight");
    await expect.poll(async () => (await settings()).low_quota.threshold_percent).toBe(30);
    await answered();
  });

  await test.step("Settings: the slider sets the threshold with its keys", async () => {
    await tab("Settings");
    const saving = region("Mode");
    const own = saving.getByRole("slider", { name: "Mode and threshold" });
    await expect(saving.getByLabel("Cut below, % left", { exact: true })).toHaveCount(0);
    await own.focus();
    for (let i = 0; i < 5; i++) await page.keyboard.press("ArrowLeft");
    await expect(saving.locator(".slider-words")).toHaveText("Cut when the watched account has less than 25 % left · every model");
    await own.blur();                                               // leaving it writes at once
    await expect.poll(async () => (await settings()).low_quota.threshold_percent).toBe(25);
    await expect(own).toHaveAttribute("aria-busy", "false");
    await expect(saving.locator(".card-foot button")).toHaveText("No changes");
  });

  await test.step("the models picker writes low_quota.models", async () => {
    const saving = region("Mode");
    const group = saving.getByRole("radiogroup", { name: "Models cut when low", exact: true });
    const models = async () => (await settings()).low_quota.models;
    const answered = () => expect(group).not.toHaveAttribute("aria-busy");
    await group.getByRole("radio", { name: "Only the ones I pick" }).check();
    // The fake serves two ids; nothing is written until one is picked.
    await expect(saving.getByRole("checkbox")).toHaveCount(2);
    await expect(saving.getByRole("checkbox", { name: "model-alpha" })).toBeVisible();
    await expect(saving.getByRole("checkbox", { name: "model-beta" })).toBeVisible();
    await expect(saving.getByText("Nothing is picked yet, so every model is still cut when low")).toBeVisible();
    expect(await models()).toEqual([]);
    // The box shows what the core reports, not the click, so it is clicked, not checked.
    await saving.getByRole("checkbox", { name: "model-alpha" }).click();
    await expect.poll(models).toEqual(["model-alpha"]);
    await answered();
    expect(readFileSync(core.configPath, "utf8")).toMatch(/models:\s*\n\s*- model-alpha/);
    await expect(saving.getByRole("checkbox", { name: "model-alpha" })).toBeChecked();
    await group.getByRole("radio", { name: "Every model" }).click();
    await expect.poll(models).toEqual([]);
    await answered();
    await expect(saving.getByRole("checkbox")).toHaveCount(0);
  });

  await test.step("a 409: an existing cliproxy.url wins over a cliproxy.port save", async () => {
    core.setFile({ cliproxy: { url: `http://127.0.0.1:${core.fakePort}` } });
    await core.reload();
    await tab("Home");
    await tab("Settings");
    await region("Advanced").locator("summary").click();              // the fold is closed until it is opened
    const cli = region("CLIProxyAPI");
    const port = cli.getByLabel("Port", { exact: true });
    await port.fill(String(core.fakePort + 1));
    const save = cli.locator(".card-foot button");
    await expect(save).toHaveText("Save 1 change");
    await save.click();
    const refused = cli.getByRole("alert");
    // The core's sentence names its key, so the key is not repeated beside it.
    await expect(refused).toHaveText("cliproxy.url is set and decides the upstream; clear it in the same body to use cliproxy.port");
    await expect(refused.locator("code")).toHaveCount(0);
    await expect(port).toHaveAttribute("aria-invalid", "true");
    await expect(port).toHaveValue(String(core.fakePort + 1));
    await expect(save).not.toHaveAttribute("aria-busy");
    await expect(save).toHaveText("Save 1 change");               // the refused draft stays, and so does its count
  });

  await test.step("the narrow window: the destinations fold into a row of tabs and nothing leaves the window", async () => {
    await page.setViewportSize({ width: 480, height: 600 });
    const overflowing = () => page.evaluate(() => {
      const out: string[] = [];
      for (const el of document.querySelectorAll<HTMLElement>("body *")) {
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        if (r.width === 0 || r.height === 0 || style.visibility === "hidden" || !el.checkVisibility()) continue;
        const what = `${el.tagName.toLowerCase()}.${el.getAttribute("class") ?? ""} "${(el.textContent ?? "").slice(0, 40)}"`;
        if (r.right > innerWidth + 0.5) out.push(`${what} ends at ${r.right}`);
        if (["auto", "scroll"].includes(style.overflowX) && el.scrollWidth > el.clientWidth) out.push(`${what} scrolls sideways`);
      }
      return out;
    });
    for (const where of ["Home", "Accounts", "Activity", "Settings"]) {
      await tab(where);
      if (where === "Settings") await region("Advanced").locator("details").evaluate((d: HTMLDetailsElement) => { d.open = true; });
      await expect(decision).toBeVisible();
      // The brand folds away from sight, and its h1 stays the page heading for assistive technology.
      const brand = await page.locator(".brand").boundingBox();
      expect(brand && brand.width <= 1 && brand.height <= 1, where).toBe(true);
      await expect(page.getByRole("heading", { level: 1, name: "lokki" })).toHaveCount(1);
      const fold = await page.evaluate(() => {
        const bar = document.querySelector("header.topbar")!.getBoundingClientRect();
        const tabs = [...document.querySelectorAll("nav[aria-label=Destinations] button")].map((b) => b.getBoundingClientRect());
        return { below: tabs.every((t) => t.top >= bar.bottom - 1), tops: new Set(tabs.map((t) => Math.round(t.top))).size };
      });
      expect(fold, where).toEqual({ below: true, tops: 1 });
      // Inner scrollers count too: a page's scrollWidth does not see a wide table inside a card.
      const overflow = await overflowing();
      expect(overflow, where).toEqual([]);
    }

    // Activity with a model and a client of 300 characters, written into the real answer: each stays on one line.
    const long = "x".repeat(300);
    await page.route((u) => u.pathname === "/api/activity", async (route) => {
      const response = await route.fetch();
      const body = (await response.json()) as { models: Array<{ model: string }>; clients: Array<{ client: string }>; recent: Array<{ model: string; client: string }> };
      body.models[0].model = long;
      body.clients[0].client = long;
      for (const r of body.recent) { r.model = long; r.client = long; }
      await route.fulfill({ response, json: body });
    });
    await tab("Activity");
    await expect(page.getByTitle(long)).not.toHaveCount(0);
    expect(await overflowing(), "Activity with long names").toEqual([]);
    for (const el of await page.getByTitle(long).all()) {
      expect(await el.evaluate((n) => n.getBoundingClientRect().height < 2 * parseFloat(getComputedStyle(n).lineHeight || "20")), "one line").toBe(true);
    }
    await page.unrouteAll({ behavior: "ignoreErrors" });

    // Home's full-size top bar, with the slider and its rule line, still leaves room for its content.
    await tab("Home");
    await expect(slider).toBeVisible();
    await expect(words).toHaveText(/^Cut when .+ % left · every model$/);
    expect(await page.locator("main.content").evaluate((el) => el.clientHeight)).toBeGreaterThanOrEqual(240);
    await page.setViewportSize({ width: 1280, height: 720 });
  });

  await test.step("the mode control writes and follows the core", async () => {
    const modes = page.getByRole("radiogroup", { name: "Mode" });
    // Auto's label is the threshold it returns to: the one the Settings step saved.
    const mode = (name: string) => modes.getByRole("radio", { name: name === "Auto" ? "Auto 25 %" : name, exact: true });
    // A press while a write and its reads are out waits and is sent after them; each press here waits for the last.
    const answered = () => expect(modes).toHaveAttribute("aria-busy", "false");
    await tab("Home");
    await mode("Direct").click();
    await expect(mode("Direct")).toHaveAttribute("aria-checked", "true");
    await answered();
    expect((await (await api("/api/settings")).json()).routing).toBe("direct");
    expect((await (await page.request.get("/healthz")).json()).routing.reason).toBe("routing: direct");
    await expect(decision).toContainText("routing: direct");
    await tab("Accounts");
    await expect(page.getByText(UNWATCHED_MODES)).toBeVisible();
    await expect(page.getByRole("switch")).toHaveCount(0);
    await expect(page.locator("[data-key]")).toHaveCount(2);
    await tab("Home");

    await mode("Auto").click();
    await expect(mode("Auto")).toHaveAttribute("aria-checked", "true");
    await answered();
    const after = await settings();
    expect([after.routing, after.low_quota.threshold_percent]).toEqual(["auto", 25]);      // the choice wrote the mode alone
    await expect(decision.getByText("every watched account is low", { exact: true })).toBeVisible();

    // By keyboard, in a real browser, which turns Enter and Space on a button into a click: the
    // arrows only move the focus, and each choice is one PUT.
    puts.length = 0;
    await mode("Auto").focus();
    for (const [key, focused] of [["ArrowRight", "Relay"], ["Home", "Direct"], ["ArrowLeft", "Relay"]]) {
      await page.keyboard.press(key);
      await expect(mode(focused)).toBeFocused();
    }
    expect(puts).toEqual([]);
    await page.keyboard.press("Enter");
    await expect(mode("Relay")).toHaveAttribute("aria-checked", "true");
    await answered();
    expect(puts).toEqual(['{"routing":"relay"}']);
    await page.keyboard.press("ArrowLeft");
    await expect(mode("Auto")).toBeFocused();
    await page.keyboard.press("Space");
    await expect(mode("Auto")).toHaveAttribute("aria-checked", "true");
    expect(puts).toEqual(['{"routing":"relay"}', '{"routing":"auto"}']);
    await expect(decision.getByText("every watched account is low", { exact: true })).toBeVisible();
  });

  await test.step("the schema the UI was built from is the schema the core serves", async () => {
    const fixture = JSON.parse(readFileSync(join(import.meta.dirname, "..", "src", "views", "schema.fixture.json"), "utf8"));
    expect(await (await api("/api/schema")).json()).toEqual(fixture);
  });

  await test.step("/healthz agrees with Home", async () => {
    const health = await (await page.request.get("/healthz")).json();
    expect(health.routing.relay_on).toBe(true);
    expect(health.routing.reason).toBe("every watched account is low");
  });
});
