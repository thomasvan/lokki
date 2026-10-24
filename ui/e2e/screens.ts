// The screenshots a UI pull request carries (`npm run screens`): each state on the fixture
// core, in light and dark, written as <screen>-<state>-<light|dark>.png into $LOKKI_SCREENS.
// The page holds the operator token, so the directory must be outside the checkout, and
// every shot first checks that neither the token nor the client key is in the page.
import { chmodSync, mkdirSync, readFileSync, realpathSync } from "node:fs";
import { isAbsolute, join, relative, resolve, sep } from "node:path";
import type { Locator, Page, Route } from "@playwright/test";
import { NO_SECRET, NO_SECRET_AUTO, UNWATCHED_MODES, UNWRITABLE, headline } from "../src/lib/mode.ts";
import { freePort, holdPort, test, expect } from "./core.ts";

const CHECKOUT = resolve(import.meta.dirname, "..", "..");

// A path named `..foo` inside the checkout is inside it: only `..` itself or a `../` prefix leaves.
function inside(root: string, path: string): boolean {
  const rel = relative(root, path);
  return !rel.startsWith(".." + sep) && rel !== ".." && !isAbsolute(rel);
}

// Refused before anything is made, then checked again once the path resolves through any link.
function screensDir(): string {
  const named = process.env.LOKKI_SCREENS;
  if (!named) throw new Error("set LOKKI_SCREENS to a directory outside the checkout");
  const refuse = () => new Error("LOKKI_SCREENS is inside the checkout; the screenshots hold what the page shows and must be outside it");
  const checkout = realpathSync(CHECKOUT);
  if (inside(CHECKOUT, resolve(named)) || inside(checkout, resolve(named))) throw refuse();
  mkdirSync(named, { recursive: true });
  const dir = realpathSync(named);
  if (inside(checkout, dir)) throw refuse();
  return dir;
}

// A state the fixture cannot reach without a forbidden port, a dead process or a clock jump:
// the real answer for `path`, with `mutate` applied, in the browser only.
export async function patch(page: Page, path: string, mutate: (body: unknown) => unknown): Promise<void> {
  await page.route((url) => url.pathname === path, async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: mutate(await response.json()) });
  });
}

// A shell-only state: the one window.lokki hook it needs, `body` being the function's source.
export async function hook(page: Page, name: string, body: string): Promise<void> {
  await page.addInitScript({ content: `window.lokki = Object.assign(window.lokki || {}, { ${JSON.stringify(name)}: ${body} });` });
}

// The slider's write, patched in the browser: `hold` leaves each PUT unanswered until the returned `release`,
// which answers it and every later PUT with a 200 the core never sees, so nothing is written (a reload instead
// would cancel the held write and let the page send the choice waiting behind it as it unloads, past the route).
// `refuse` answers each PUT with the core's 409 sentence.
const REFUSED = "the settings file names no client token; the reload would refuse it, so nothing is written";
async function patchWrite(page: Page, how: "hold" | "refuse"): Promise<() => Promise<void>> {
  const held: Route[] = [];
  let released = false;
  const ok = (route: Route) => route.fulfill({ json: { applied: [], restart_required: [], comments_dropped: false } });
  await page.route((url) => url.pathname === "/api/settings", async (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    if (how === "refuse") return route.fulfill({ status: 409, json: { error: { message: REFUSED } } });
    if (released) return ok(route);
    held.push(route);
  });
  return async () => {
    released = true;
    for (const route of held.splice(0)) await ok(route);
  };
}

// Steps the slider with its keys and leaves it, which writes at once.
async function step(page: Page, scope: Page | Locator, key: string, times: number): Promise<void> {
  const slider = scope.getByRole("slider", { name: "Mode and threshold" });
  await slider.focus();
  for (let i = 0; i < times; i++) await page.keyboard.press(key);
  await slider.blur();
}

const TALL = { width: 1100, height: 1180 };        // Home's whole content; the 1100 × 760 window scrolls it
const NARROW = { width: 480, height: 600 };
const NARROW_TALL = { width: 480, height: 2600 };      // Activity at 480: its whole list, not the 600 px window
const ACTIVITY = { width: 1100, height: 2100 };       // Activity's whole content; the 1100 × 760 window scrolls it

test("the screenshots of Home", async ({ page, core }) => {
  const dir = screensDir();
  const headers = { "X-Relay-Token": core.token };
  const put = async (body: unknown) => expect((await page.request.put("/api/settings", { headers, data: body })).ok()).toBe(true);
  const refresh = async () => expect((await page.request.post("/api/accounts/refresh", { headers })).ok()).toBe(true);
  const { key } = (await (await page.request.get("/api/endpoint?reveal=1", { headers })).json()) as { key: string };
  expect(key).toBeTruthy();

  const decision = page.getByRole("banner").getByRole("status");
  async function shoot(name: string, head: string, reason: string, size = TALL) {
    await page.setViewportSize(size);
    await page.reload();
    await expect(decision).toContainText(head);
    await expect(decision).toContainText(reason);
    await expect(page.getByRole("region", { name: "Last 24 h" }).locator("[data-bucket]")).toHaveCount(24);
    await page.mouse.move(0, 0);
    for (const colorScheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme });
      const html = await page.content();
      expect(html).not.toContain(core.token);
      expect(html).not.toContain(key);
      await page.screenshot({ path: join(dir, `${name}-${colorScheme}.png`) });
    }
  }
  // Home, whole: the shell is on every Home shot.
  const both = (state: string, head: string, reason: string) => shoot(`home-${state}`, head, reason);
  // The page as it is now, for a state a reload would lose (a write in flight or just refused).
  async function snap(name: string) {
    await page.mouse.move(0, 0);
    for (const colorScheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme });
      const html = await page.content();
      expect(html).not.toContain(core.token);
      expect(html).not.toContain(key);
      await page.screenshot({ path: join(dir, `${name}-${colorScheme}.png`) });
    }
  }
  const slider = page.getByRole("slider", { name: "Mode and threshold" });

  await page.goto("/");
  await page.getByLabel("Operator token", { exact: true }).fill(core.token);
  await page.getByRole("button", { name: "Use", exact: true }).click();

  await both("auto-passing", headline(false), "not every watched account is low");
  await expect(page.locator(".slider-words")).toHaveText("Cut when every watched account has less than 30 % left · every model");

  // patched: the slider let go at 45 and its write held, then Relay chosen in the top bar while it is in flight.
  const release = await patchWrite(page, "hold");
  await step(page, page, "ArrowRight", 15);
  await expect(slider).toHaveAttribute("aria-busy", "true");
  await expect(slider).toHaveValue("45");
  await page.addStyleTag({ content: "*, *::before, *::after { transition: none !important; animation-play-state: paused !important; }" });
  await snap("home-slider-writing");
  await page.getByRole("radiogroup", { name: "Mode" }).getByRole("radio", { name: "Relay", exact: true }).click();
  await expect(page.getByRole("radio", { name: "Relay", exact: true }).locator('[data-icon="spinner"]')).toHaveCount(1);
  await snap("home-slider-waiting");
  await release();
  await expect(slider).toHaveAttribute("aria-busy", "false");
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.reload();
  await expect(slider).toHaveValue("30");
  expect((await (await page.request.get("/api/settings", { headers })).json()).routing).toBe("auto");

  // patched: the slider's write answered with the core's 409.
  await patchWrite(page, "refuse");
  await step(page, page, "ArrowRight", 15);
  await expect(page.getByRole("banner").getByRole("alert")).toHaveText(REFUSED);
  await expect(slider).toHaveValue("30");
  await snap("home-slider-refused");
  await page.unrouteAll({ behavior: "ignoreErrors" });

  await put({ low_quota: { accounts: ["a1"] } });
  await refresh();
  await both("auto-cutting", headline(true), "every watched account is low");
  await put({ low_quota: { accounts: [] } });
  await refresh();

  // patched: one watched account the core could not read.
  await patch(page, "/api/status", (body) => ({ ...(body as object), decision: { relay_on: false, reason: "a watched account could not be read" } }));
  await patch(page, "/api/accounts", (body) => {
    const accounts = body as { accounts: Array<{ auth_index: string }> };
    return { ...accounts, accounts: accounts.accounts.map((a) => (a.auth_index === "b2"
      ? { ...a, state: "unknown", error: "HTTP 500 on the usage route", used_percent: null, remaining_percent: null, reset_at: null } : a)) };
  });
  await both("auto-unread", headline(false), "a watched account could not be read");
  await page.unrouteAll({ behavior: "ignoreErrors" });

  // patched: the core's answer for "read, and none": a first run.
  await patch(page, "/api/status", (body) => ({ ...(body as object), decision: { relay_on: false, reason: "no account to watch" } }));
  await patch(page, "/api/accounts", (body) => ({ ...(body as object), accounts: [] }));
  await both("first-run", headline(false), "no account to watch");
  await expect(page.getByText("No account yet")).toBeVisible();
  await page.unrouteAll({ behavior: "ignoreErrors" });

  await put({ routing: "relay" });
  await both("relay", headline(true), "routing: relay");

  await put({ routing: "direct" });
  await both("direct", headline(false), "routing: direct");

  // patched: the fixture's fake answers every read, so a failed read with no snapshot is the real answers with the
  // failure written in and the snapshot taken out.
  const failure = "CLIProxyAPI did not answer (timeout)";
  await put({ routing: "auto" });
  await patch(page, "/api/status", (body) => {
    const status = body as { poll: object; decision: object };
    return { ...status, poll: { ...status.poll, last_error: failure }, decision: { relay_on: false, reason: "the quota cannot be read" } };
  });
  await patch(page, "/api/accounts", (body) => {
    const accounts = body as { poll: object };
    return { ...accounts, read_at: null, age_s: null, accounts: [], poll: { ...accounts.poll, last_error: failure } };
  });
  await shoot("home-read-error", headline(false), "the quota cannot be read");
  await expect(page.getByText("No quota read yet: the last read failed (above)")).toBeVisible();
  await page.unrouteAll({ behavior: "ignoreErrors" });

  // patched: a core given no management secret file, as a server deployment can be. The fixture's
  // core has one, so its settings, status and accounts answers are those of a core without.
  await patch(page, "/api/settings", (body) => {
    const settings = body as { cliproxy: object };
    return { ...settings, cliproxy: { ...settings.cliproxy, management_secret_file: "" } };
  });
  await patch(page, "/api/status", (body) => {
    const status = body as { poll: object };
    return { ...status, poll: { ...status.poll, last_error: "no management secret file is set" }, decision: { relay_on: false, reason: "the quota cannot be read" } };
  });
  await patch(page, "/api/accounts", (body) => ({ ...(body as object), read_at: null, age_s: null, accounts: [] }));
  await shoot("home-no-secret", headline(false), "the quota cannot be read");
  await expect(page.getByText(NO_SECRET + NO_SECRET_AUTO)).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.unrouteAll({ behavior: "ignoreErrors" });

  await shoot("home-narrow", headline(false), "not every watched account is low", NARROW);
  await expect(page.locator(".slider-words")).toHaveText(/^Cut when .+ % left · every model$/);

  // hooked: the shell's update state, the one hook the banner needs. Last, since the hook stays for the page's life.
  await put({ routing: "auto", low_quota: { accounts: [] } });
  await refresh();
  await hook(page, "updateStatus", `async () => ({ state: "available", current_version: "0.1.0", version: "0.2.0", notes_url: null, can_install: false, reason: "this build is updated by hand", error: null })`);
  await hook(page, "openExternal", "async () => {}");
  await shoot("home-update", headline(false), "not every watched account is low");
  await expect(page.getByText("Lokki 0.2.0 is available.")).toBeVisible();
});

test("the screenshots of Accounts", async ({ page, core }) => {
  const dir = screensDir();
  const headers = { "X-Relay-Token": core.token };
  const put = async (body: unknown) => expect((await page.request.put("/api/settings", { headers, data: body })).ok()).toBe(true);
  const { key } = (await (await page.request.get("/api/endpoint?reveal=1", { headers })).json()) as { key: string };
  expect(key).toBeTruthy();

  // patched: a login the core never starts. Every request to a login route is answered here, none is
  // continued to the core, and the test ends by comparing how many the browser sent with how many this
  // handler answered and by listing the routes it saw. The sentences are the paste route's, word for word
  // (core/lokki_core/control.py), and the address and code are made up.
  const LOGIN = "screens-login-1";
  const GONE = "CLIProxyAPI no longer holds this login: it expired, or it finished more than a minute ago; check Accounts, else start again";
  const PASTED = "http://localhost:1455/auth/callback?code=made-up-code&state=screens-login-1";
  let login: "pending" | "ok" | "error" = "pending";
  let sendAnswer: { status: number; json: unknown } = { status: 200, json: { handed_on: true } };
  const seen = new Set<string>();
  let sent = 0;
  let answered = 0;
  page.on("request", (r) => { if (new URL(r.url()).pathname.startsWith("/api/accounts/oauth")) sent++; });
  await page.addInitScript({ content: "window.open = () => ({});" });
  async function patchLogin() {
    await page.route((url) => url.pathname.startsWith("/api/accounts/oauth"), async (route) => {
      answered++;
      const { pathname } = new URL(route.request().url());
      seen.add(`${route.request().method()} ${pathname}`);
      if (pathname === "/api/accounts/oauth") {
        await route.fulfill({ json: { state: LOGIN, url: "https://auth.example.invalid/oauth/authorize?state=screens-login-1" } });
      } else if (route.request().method() === "POST") {
        expect(pathname).toBe(`/api/accounts/oauth/${LOGIN}`);
        await route.fulfill(sendAnswer);
      } else {
        expect(pathname).toBe(`/api/accounts/oauth/${LOGIN}`);
        await route.fulfill({ json: { status: login, error: login === "error" ? GONE : null } });
      }
    });
  }

  async function open() {
    await page.reload();
    await page.getByRole("button", { name: "Accounts", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Accounts", level: 2 })).toBeVisible();
    // The switches ease their colours: a shot taken as the scheme changes would catch them half way.
    await page.addStyleTag({ content: "*, *::before, *::after { transition: none !important; }" });
  }
  async function shoot(name: string, ready: () => Promise<void>) {
    await open();
    await ready();
    await page.mouse.move(0, 0);
    for (const colorScheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme });
      const html = await page.content();
      expect(html).not.toContain(core.token);
      expect(html).not.toContain(key);
      await page.screenshot({ path: join(dir, `${name}-${colorScheme}.png`) });
    }
  }
  const cards = (n: number) => async () => { await expect(page.locator("[data-key]")).toHaveCount(n); };
  const login_ = (name: string) => async () => {
    await page.getByRole("button", { name: "Add a Codex account" }).click();
    await expect(page.getByText(name)).toBeVisible();
  };
  const field = () => page.getByRole("textbox", { name: /Paste that address here/ });
  // The waiting card with an address pasted and Send pressed; `ready` waits for what the press shows.
  const pasted = (text: string, ready: () => Promise<void>) => async () => {
    await login_("Lokki waits up to 5 minutes.")();
    await field().fill(text);
    await page.getByRole("button", { name: "Send" }).click();
    await ready();
  };
  const refusal = (status: number, sentence: string, text = PASTED) => async () => {
    sendAnswer = { status, json: { error: { message: sentence } } };
    await pasted(text, async () => { await expect(page.getByRole("alert")).toHaveText(sentence); })();
    await expect(field()).toHaveValue(text);
  };

  await page.goto("/");
  await page.getByLabel("Operator token", { exact: true }).fill(core.token);
  await page.getByRole("button", { name: "Use", exact: true }).click();

  await shoot("accounts-every", cards(2));

  // real: the scope written through the API, then a read.
  await put({ low_quota: { accounts: ["a1"] } });
  expect((await page.request.post("/api/accounts/refresh", { headers })).ok()).toBe(true);
  await shoot("accounts-picked", async () => {
    await cards(2)();
    await expect(page.locator('[data-key="b2"]')).toContainText("not watched");
  });

  // real: a scope entry no account carries.
  await put({ low_quota: { accounts: ["a1", "zz9"] } });
  expect((await page.request.post("/api/accounts/refresh", { headers })).ok()).toBe(true);
  await shoot("accounts-not-found", async () => { await expect(page.getByText("Watched but not found:")).toBeVisible(); });
  await put({ low_quota: { accounts: [] } });
  expect((await page.request.post("/api/accounts/refresh", { headers })).ok()).toBe(true);

  // real: Relay, where Auto's scope and switches are not shown.
  await put({ routing: "relay" });
  await shoot("accounts-relay", async () => {
    await cards(2)();
    await expect(page.getByText(UNWATCHED_MODES)).toBeVisible();
    await expect(page.getByRole("switch")).toHaveCount(0);
  });
  await put({ routing: "auto" });

  // patched: a1's window has reset since the read, as policy.state_of counts a passed reset_at.
  await patch(page, "/api/accounts", (body) => {
    const accounts = body as { accounts: Array<{ auth_index: string }> };
    return { ...accounts, accounts: accounts.accounts.map((a) => (a.auth_index === "a1" ? { ...a, reset_at: Math.floor(Date.now() / 1000) - 300, state: "ok" } : a)) };
  });
  await shoot("accounts-window-reset", async () => { await expect(page.getByText("reset; the next read shows the new window")).toBeVisible(); });
  await page.unrouteAll({ behavior: "ignoreErrors" });

  // patched: the core's answer for "read, and none", with read_at kept as read.
  await patch(page, "/api/accounts", (body) => ({ ...(body as object), accounts: [] }));
  await shoot("accounts-none", async () => { await expect(page.getByText("No Codex account yet")).toBeVisible(); });
  await page.unrouteAll({ behavior: "ignoreErrors" });

  // patched: a core given no management secret file, as Home's shot of it.
  const noSecret = "no management secret file is set";
  await patch(page, "/api/settings", (body) => {
    const settings = body as { cliproxy: object };
    return { ...settings, cliproxy: { ...settings.cliproxy, management_secret_file: "" } };
  });
  await patch(page, "/api/status", (body) => {
    const status = body as { poll: object };
    return { ...status, poll: { ...status.poll, last_error: noSecret }, decision: { relay_on: false, reason: "the quota cannot be read" } };
  });
  await patch(page, "/api/accounts", (body) => {
    const accounts = body as { poll: object };
    return { ...accounts, read_at: null, age_s: null, accounts: [], poll: { ...accounts.poll, last_error: noSecret } };
  });
  await shoot("accounts-no-secret", async () => {
    await expect(page.getByText(NO_SECRET)).toBeVisible();
    await expect(page.getByRole("alert")).toHaveCount(0);
  });
  await page.unrouteAll({ behavior: "ignoreErrors" });

  // patched: the login states.
  await patchLogin();
  login = "pending";
  await shoot("accounts-login-waiting", login_("Lokki waits up to 5 minutes."));
  await shoot("accounts-login-refused-another", refusal(409, "this address is from another login; paste the address of the login started last"));
  await shoot("accounts-login-refused-address", refusal(400, "this is not the address the login ended on: it starts http://localhost:1455/auth/callback?", "http://localhost:1455/?code=made-up-code"));
  await shoot("accounts-login-refused-gone", refusal(409, "this login is no longer waiting; start again"));
  await shoot("accounts-login-refused-twice", refusal(409, "Lokki already has this login's answer; wait for it to finish"));
  await shoot("accounts-login-refused-expired", refusal(409, "this login expired; start again"));
  await shoot("accounts-login-refused-cliproxy", refusal(409, "CLIProxyAPI did not take this login; start again"));
  await shoot("accounts-login-empty", pasted("", async () => { await expect(page.getByRole("alert")).toHaveText("paste the address first"); }));
  sendAnswer = { status: 200, json: { handed_on: true } };
  await shoot("accounts-login-sent", pasted(PASTED, async () => {
    await expect(page.getByText("Sent; finishing the login…")).toBeVisible();
    await expect(field()).toHaveValue("");
  }));
  login = "ok";
  await shoot("accounts-login-done", async () => { await login_("Account added")(); });
  login = "error";
  await shoot("accounts-login-failed", async () => { await login_(GONE)(); });
  await page.unrouteAll({ behavior: "ignoreErrors" });

  expect(sent).toBeGreaterThan(0);
  expect(answered).toBe(sent);
  expect([...seen].sort()).toEqual([`GET /api/accounts/oauth/${LOGIN}`, "POST /api/accounts/oauth", `POST /api/accounts/oauth/${LOGIN}`]);
});

test("the screenshots of Activity", async ({ page, core }) => {
  const dir = screensDir();
  const headers = { "X-Relay-Token": core.token };
  const { key } = (await (await page.request.get("/api/endpoint?reveal=1", { headers })).json()) as { key: string };
  expect(key).toBeTruthy();

  type Answer = {
    window: string; from: number; coverage: { oldest: number | null; complete: boolean };
    buckets: Array<Record<string, number>>; totals: Record<string, number>;
  };
  async function shoot(name: string, ready: () => Promise<void>, size = ACTIVITY) {
    await page.setViewportSize(size);
    await page.reload();
    await page.getByRole("button", { name: "Activity", exact: true }).click();
    await ready();
    await page.addStyleTag({ content: "*, *::before, *::after { transition: none !important; }" });
    await page.mouse.move(0, 0);
    for (const colorScheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme });
      const html = await page.content();
      expect(html).not.toContain(core.token);
      expect(html).not.toContain(key);
      await page.screenshot({ path: join(dir, `${name}-${colorScheme}.png`) });
    }
  }
  const bars = (n: number) => async () => { await expect(page.locator("[data-bucket]")).toHaveCount(n); };
  const week = async () => {
    await bars(24)();
    await page.getByRole("button", { name: "7 d" }).click();
    await bars(28)();
    await expect(page.getByRole("group", { name: "Window" })).toHaveAttribute("aria-busy", "false");
  };

  await page.goto("/");
  await page.getByLabel("Operator token", { exact: true }).fill(core.token);
  await page.getByRole("button", { name: "Use", exact: true }).click();

  // real: the seeded run log, as the fixture core reads it.
  await shoot("activity-24h", bars(24));
  await shoot("activity-7d", week);               // the seed starts 72 h back, so this shot is the history-inside shot too
  await shoot("activity-narrow", bars(24), NARROW_TALL);

  // patched: the real answer with no run in it.
  await patch(page, "/api/activity", (body) => {
    const a = body as Answer;
    return {
      ...a, coverage: { ...a.coverage, oldest: null, records: 0 },
      buckets: a.buckets.map((b) => ({ ...b, relay: 0, passthrough: 0, images: 0, errors: 0 })),
      totals: { requests: 0, relay: 0, passthrough: 0, images: 0, errors: 0 },
      relay: { finalized: 0, cut: 0, client_gone: 0, error: 0, by_cut: {} }, passthrough: { by_reason: {}, by_cut: {} },
      tokens: { runs: 0, input: 0, output: 0, cached: 0, reasoning: 0 }, models: [], clients: [], recent: [],
      latency: Object.fromEntries(["relay", "passthrough", "images", "all"].map((k) => [k, { p50: null, p90: null }])),
    };
  });
  await shoot("activity-empty", async () => { await expect(page.getByText("Point your client at the endpoint on Home.")).toBeVisible(); }, { width: 1100, height: 760 });
  await page.unrouteAll({ behavior: "ignoreErrors" });

  // patched: the real answer, read by a core that stopped at its 8 MiB limit five hours into the window.
  await patch(page, "/api/activity", (body) => {
    const a = body as Answer;
    return { ...a, coverage: { ...a.coverage, complete: false, oldest: a.from + 5 * 3600 } };
  });
  await shoot("activity-read-limit", async () => {
    await bars(24)();
    await expect(page.getByText(/^counted since .+: Lokki reads the last 8 MiB of its run log; older runs in this window are not counted$/)).toBeVisible();
  });
});

test("the screenshots of Settings", async ({ page, core }) => {
  const dir = screensDir();
  const headers = { "X-Relay-Token": core.token };
  const put = async (body: unknown) => expect((await page.request.put("/api/settings", { headers, data: body })).ok()).toBe(true);
  const { key } = (await (await page.request.get("/api/endpoint?reveal=1", { headers })).json()) as { key: string };
  expect(key).toBeTruthy();
  // The panel's login takes this secret; the shell puts it on the clipboard and it never reaches the page.
  const secret = readFileSync(join(core.dataDir, "secrets", "cliproxy-mgmt.secret"), "utf8").trim();
  expect(secret).toBeTruthy();
  const region = (name: string) => page.getByRole("region", { name, exact: true });
  const saveIn = (name: string) => region(name).locator(".card-foot button");

  async function open() {
    await page.setViewportSize({ width: 1100, height: 760 });
    await page.reload();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Settings", level: 2 })).toBeVisible();
    await expect(region("Updates and About")).toContainText("CLIProxyAPI");
    await page.addStyleTag({ content: "*, *::before, *::after { transition: none !important; }" });
  }
  // The window grows to the whole page, so a shot shows every group; the page itself never scrolls sideways.
  async function shoot(name: string) {
    const height = await page.evaluate(() => {
      const content = document.querySelector<HTMLElement>("main.content")!;
      return innerHeight - content.clientHeight + content.scrollHeight;
    });
    await page.setViewportSize({ width: 1100, height: Math.max(760, Math.ceil(height)) });
    await page.mouse.move(0, 0);
    for (const colorScheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme });
      const html = await page.content();
      expect(html).not.toContain(core.token);
      expect(html).not.toContain(key);
      expect(html).not.toContain(secret);
      await page.screenshot({ path: join(dir, `settings-${name}-${colorScheme}.png`) });
    }
  }
  async function saved(name: string, label: string, value: string) {
    await region(name).getByLabel(label, { exact: true }).fill(value);
    await expect(saveIn(name)).toHaveText("Save 1 change");
    await saveIn(name).click();
    await expect(saveIn(name)).toHaveText("No changes");
    await expect(saveIn(name)).not.toHaveAttribute("aria-busy");
  }

  await page.goto("/");
  await page.getByLabel("Operator token", { exact: true }).fill(core.token);
  await page.getByRole("button", { name: "Use", exact: true }).click();
  await put({ routing: "auto", low_quota: { accounts: [] } });
  expect((await page.request.post("/api/accounts/refresh", { headers })).ok()).toBe(true);

  // The fold is closed until it is opened, as a user finds it.
  const unfold = () => region("Advanced").locator("summary").click();

  // real: the fixture as it starts, in a plain browser: no window.lokki, so no update line.
  await open();
  await expect(region("Mode")).toContainText(/Watched accounts: \d+ of \d+/);
  await expect(region("Updates and About").getByRole("button", { name: "Check now" })).toHaveCount(0);
  await shoot("browser");

  // hooked: the shell's update state, so Updates and About carries its line; a flag in sessionStorage turns it on for the pages that follow.
  await page.addInitScript({ content: `if (sessionStorage.getItem("shots-update")) window.lokki = Object.assign(window.lokki || {}, {
    updateStatus: async () => ({ state: "up_to_date", current_version: "0.1.0", version: null, notes_url: null, can_install: false, reason: null, error: null }),
    checkForUpdate: async () => ({ state: "up_to_date", current_version: "0.1.0", version: null, notes_url: null, can_install: false, reason: null, error: null }) });` });
  await page.evaluate(() => sessionStorage.setItem("shots-update", "1"));
  await open();
  await expect(region("Updates and About").getByRole("button", { name: "Check now" })).toBeVisible();
  await shoot("auto");
  await page.evaluate(() => sessionStorage.removeItem("shots-update"));

  // real: Relay and Direct, each chosen through the core; Auto's rows are gone and nothing was written but the mode.
  for (const routing of ["relay", "direct"] as const) {
    await put({ routing });
    await open();
    await expect(region("Mode").getByRole("slider", { name: "Mode and threshold" })).toHaveValue(routing === "relay" ? "100" : "0");
    await expect(region("Mode").getByText("Auto's rule")).toHaveCount(0);
    await shoot(routing);
  }
  await put({ routing: "auto" });

  // patched: a core given no management secret file, as a server deployment can be: its settings,
  // status and accounts answers, the accounts never read (read_at null), as Home's and Accounts' shots patch them.
  const noSecret = "no management secret file is set";
  await patch(page, "/api/settings", (body) => {
    const settings = body as { cliproxy: object };
    return { ...settings, cliproxy: { ...settings.cliproxy, management_secret_file: "" } };
  });
  await patch(page, "/api/status", (body) => {
    const status = body as { poll: object };
    return { ...status, poll: { ...status.poll, last_error: noSecret }, decision: { relay_on: false, reason: "the quota cannot be read" } };
  });
  await patch(page, "/api/accounts", (body) => {
    const accounts = body as { poll: object };
    return { ...accounts, read_at: null, age_s: null, accounts: [], poll: { ...accounts.poll, last_error: noSecret } };
  });
  await open();
  await expect(region("Mode").getByText(/^Watched accounts$/)).toBeVisible();
  await expect(region("Mode").getByText(NO_SECRET + NO_SECRET_AUTO)).toBeVisible();
  await shoot("no-secret");
  await page.unrouteAll({ behavior: "ignoreErrors" });

  // real: the models picker. The fake lists two ids; "Every model" is the default, and the picked-and-unlisted state
  // is a real write of one listed id and one the list lacks.
  const modelRow = () => region("Mode").getByRole("radiogroup", { name: "Models cut when low", exact: true });
  await open();
  await expect(modelRow().getByRole("radio", { name: "Every model" })).toBeChecked();
  await shoot("models-every");
  await put({ low_quota: { models: ["model-alpha", "model-retired"] } });
  await open();
  await expect(region("Mode").getByRole("checkbox", { name: "model-alpha" })).toBeChecked();
  await expect(region("Mode").getByText("Chosen but not listed:")).toBeVisible();
  await shoot("models-picked-unlisted");
  // patched: CLIProxyAPI's model list answers 502, which the fixture cannot make without a dead process; every chosen name stays removable.
  await page.route((u) => u.pathname === "/api/models", (route) => route.fulfill({ status: 502, json: { error: { message: "CLIProxyAPI did not answer" } } }));
  await open();
  await expect(region("Mode").getByText("Lokki could not read the models CLIProxyAPI offers: CLIProxyAPI did not answer")).toBeVisible();
  await expect(region("Mode").getByRole("button", { name: "Retry" })).toBeVisible();
  await expect(region("Mode").getByText("Chosen but not listed:")).toHaveCount(0);
  await shoot("models-unread");
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await put({ low_quota: { models: [] } });

  // real: the Advanced fold opened.
  await open();
  await unfold();
  await expect(region("Set in the file")).toBeVisible();
  await shoot("advanced");

  // patched: the slider's write answered with the core's 409, as on Home.
  await patchWrite(page, "refuse");
  await open();
  await step(page, region("Mode"), "ArrowRight", 15);
  await expect(region("Mode").getByRole("alert")).toHaveText(REFUSED);
  await shoot("slider-refused");
  await page.unrouteAll({ behavior: "ignoreErrors" });

  // real: a port another program holds, refused beside Connection's Port.
  const held = await holdPort();
  try {
    await open();
    await region("Connection").getByLabel("Port", { exact: true }).fill(String(held.port));
    await saveIn("Connection").click();
    await expect(region("Connection").getByRole("alert")).toHaveText(`port ${held.port} is in use by another program; choose another port — listen`);
    await shoot("port-refused");
  } finally {
    await held.close();
  }

  // real saves of CLIProxyAPI's Port, then the core's; the shell's restart hook is defined so the notices carry their buttons.
  await hook(page, "restart", "async () => {}");
  await open();
  await unfold();
  await saved("CLIProxyAPI", "Port", String(await freePort()));
  // The bar sits above the groups, outside the fold. (Settings.test.ts holds this placement; this shot only shows it.)
  await expect(page.locator(".groups > .restart")).toHaveCount(1);
  await expect(page.locator("details .restart")).toHaveCount(0);
  await shoot("restart-cliproxy");
  await saved("Connection", "Port", String(await freePort()));
  await expect(page.getByText("Restart the core to apply")).toBeVisible();
  await shoot("restart-both");

  // hooked: a restart that has not answered, then one that rejected, each after a real save of Port.
  const restartFrom = async (port: number) => {
    await open();
    await unfold();
    await saved("CLIProxyAPI", "Port", String(port));
    await page.getByRole("button", { name: "Restart CLIProxyAPI" }).click();
  };
  await hook(page, "restart", "() => new Promise(() => {})");
  await restartFrom(await freePort());
  await expect(page.getByRole("button", { name: "Restarting…" })).toHaveAttribute("aria-busy", "true");
  await shoot("restarting");
  await hook(page, "restart", `async () => { throw new Error("CLIProxyAPI did not answer on its new port") }`);
  await restartFrom(await freePort());
  await expect(page.getByText("could not restart CLIProxyAPI: CLIProxyAPI did not answer on its new port")).toBeVisible();
  await shoot("restart-failed");

  // real: the window the shell opens on the core's new port, which tells the person that clients must follow. The
  // file holds the port the core listens on, as it does after a move.
  await put({ listen: [`127.0.0.1:${new URL(core.url).port}`] });
  // The restart went back for CLIProxyAPI as the core moved, so the shell also gives the window what it said.
  const said = "port 9481 is already in use by another program, so Lokki did not start CLIProxyAPI; CLIProxyAPI is back on port 9482";
  await hook(page, "wentBack", JSON.stringify(said));
  await page.goto("/?moved_from=8371");
  await expect(page.getByText(`The restart also said: ${said}`)).toBeVisible();
  await expect(page.getByText("Lokki now listens on port")).toBeVisible();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await shoot("moved");

  // patched: a Cutting field the environment sets, in the real answer.
  await patch(page, "/api/settings", (body) => ({ ...(body as object), env_overrides: ["seg_budget"] }));
  await open();
  await unfold();
  await expect(region("Cutting").getByText("set by the environment")).toBeVisible();
  await shoot("env");
  await page.unrouteAll({ behavior: "ignoreErrors" });

  // real: the fixture's file made read-only; a save answers the core's 409, and the page shows the lock.
  chmodSync(core.configPath, 0o444);
  try {
    const refused = await page.request.put("/api/settings", { headers, data: { seg_budget: 900 } });
    expect(refused.status()).toBe(409);
    expect(((await refused.json()) as { error: { message: string } }).error.message).toMatch(/^settings are read-only here: .+ is not writable$/);
    await open();
    await expect(page.locator(".banner").getByText(UNWRITABLE)).toBeVisible();
    await shoot("read-only");
  } finally {
    chmodSync(core.configPath, 0o644);
  }
});
