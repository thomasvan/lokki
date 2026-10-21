// What the views read: the last answers from the core, the one error that needs
// the shell (token prompt, unreachable core), and the polling. The UI keeps
// nothing it has not just read, so every write is followed by a read.
import * as api from "./api";
import type { Accounts, Activity, ActivityWindow, ApiError, Endpoint, PutResult, SchemaRow, SettingsView, Status } from "./api";
import { modeBody, position, positionBody, type Routing } from "./mode";
import { VERSION } from "./version";

export type Tab = "home" | "accounts" | "activity" | "settings";

export type Service = "core" | "cliproxy";

// The one Codex login in progress, or how the last one ended. `url` is the login
// page; it matters only while `status` is "pending".
export type OauthFlow = {
  state: string; url: string; status: "starting" | "pending" | "ok" | "error"; message: string;
};

// restart: a saved startup_only key is not live until its service restarts; the
// notice stays until window.lokki.restart succeeds or the page reloads.
// update: the shell's update state; null in a plain browser, where no banner shows.
// activityWindow: the window Activity wants, kept while the page lives; activityBusy: a read that
// a choice of window started has not answered.
// failed: the last failure of each read Settings waits on, until that read answers. app.error is
// cleared by any read that answers, a status poll included, so it cannot say this one failed.
// modeWrite: the window's one mode write (spec 3.7, decision 21), below.
export const app = $state<{
  status: Status | null; endpoint: Endpoint | null; accounts: Accounts | null;
  schema: SchemaRow[] | null; settings: SettingsView | null; error: ApiError | null; busy: boolean;
  restart: Record<Service, boolean>; oauth: OauthFlow | null; update: UpdateStatus | null; activity: Activity | null;
  activityWindow: ActivityWindow; activityBusy: boolean; failed: Record<Read, ApiError | null>;
  modeWrite: ModeWrite;
}>({ modeWrite: { flight: null, waiting: null, held: null, failure: null }, status: null, endpoint: null, accounts: null, schema: null, settings: null, error: null, busy: false, restart: { core: false, cliproxy: false }, oauth: null, update: null, activity: null, activityWindow: "24h", activityBusy: false, failed: { schema: null, settings: null, accounts: null } });

type Read = "schema" | "settings" | "accounts";

// A mode choice: a top-bar choice writes the mode alone, a slider position writes positionBody.
export type ModeChoice = { mode: Routing } | { position: number };
// flight: the write sent, until it and its reads return. waiting: the latest choice made meanwhile, from any
// mode control. held: the slider's position stepped or dragged to and not yet chosen. failure: how the last
// write that returned went wrong, kept until the next one returns; `read` when the write went through and its
// read back did not.
export type ModeWrite = {
  flight: ModeChoice | null; waiting: ModeChoice | null; held: number | null;
  failure: { choice: ModeChoice; error: ApiError; read: boolean } | null;
};

export const errorText = (e: unknown): string => (e instanceof Error ? e.message : String(e));

// What Home and Accounts say when GET /api/accounts itself failed: the shell's banner clears at the next read
// that answers, so the view keeps its own line until the accounts are read.
export const accountsUnread = (e: unknown): string => `The accounts could not be read: ${errorText(e)}`;

// Runs one call: a success clears the error, a failure lands in app.error and
// leaves the previous answer in place.
async function read<T>(call: () => Promise<T>, into: (value: T) => void, slot?: Read): Promise<void> {
  try {
    into(await call());
    app.error = null;
    if (slot) app.failed[slot] = null;
  } catch (e) {
    if (!(e instanceof api.ApiError)) throw e;
    app.error = e;
    if (slot) app.failed[slot] = e;
  }
}

export const loadStatus = (candidate?: string) => read(() => api.getStatus(candidate), (v) => { app.status = v; });
export const loadEndpoint = () => read(() => api.getEndpoint(), (v) => { app.endpoint = v; });
export const loadAccounts = () => read(api.getAccounts, (v) => { app.accounts = v; }, "accounts");
export const loadSchema = () => read(api.getSchema, (v) => { app.schema = v; }, "schema");
// One slot, kept to the window the open screen wants (null: no screen wants one): an answer for any
// other window, a late one after the user left included, is dropped and never drawn.
let wantedWindow: ActivityWindow | null = null;
export const loadActivity = (window: ActivityWindow) => read(() => api.getActivity(window), (v) => {
  if (v.window === wantedWindow) app.activity = v;
});
// The last choice wins: a read still out for an earlier one is dropped by the rule above when it lands,
// and the busy mark stays until the newest read answers.
let choice = 0;
export async function chooseWindow(window: ActivityWindow): Promise<void> {
  const mine = ++choice;
  app.activityWindow = window;
  wantedWindow = window;
  app.activityBusy = true;
  try {
    await loadActivity(window);
  } finally {
    if (mine === choice) app.activityBusy = false;
  }
}
export const loadSettings = () => read(api.getSettings, (v) => { app.settings = v; }, "settings");

export const emptyUpdate = (): UpdateStatus => ({
  state: "idle", current_version: VERSION, version: null, notes_url: null, can_install: false, reason: null, error: null,
});

const updateFailed = (e: unknown) => { app.update = { ...(app.update ?? emptyUpdate()), state: "error", error: errorText(e) }; };

// Reads the shell's update state when it has the hook, else leaves null: the shell
// answers from memory, so this costs one IPC call per status poll.
export async function loadUpdate(): Promise<void> {
  const read = window.lokki?.updateStatus;
  if (!read) return;
  try {
    app.update = await read();
  } catch (e) {
    updateFailed(e);
  }
}

export async function checkNow(): Promise<void> {
  const check = window.lokki?.checkForUpdate;
  if (!check) return;
  app.update = { ...(app.update ?? emptyUpdate()), state: "checking", error: null };
  try {
    app.update = await check();
  } catch (e) {
    updateFailed(e);
  }
}

// On success lokki restarts and the call never answers; a rejection is the sentence
// that says why not, and the update stays on offer.
export async function installUpdate(): Promise<void> {
  const install = window.lokki?.installUpdate;
  if (!install || !app.update) return;
  app.update = { ...app.update, state: "downloading", error: null };
  try {
    await install();
  } catch (e) {
    updateFailed(e);
  }
}

export async function refreshAccounts(): Promise<void> {
  app.busy = true;
  try {
    await read(api.refreshAccounts, (v) => { app.accounts = v; }, "accounts");
  } finally {
    app.busy = false;
  }
}

// A refused write (400, 409) is the caller's to show beside its form; any other
// failure is also the shell's.
export async function saveSettings(patch: Record<string, unknown>): Promise<PutResult> {
  let result: PutResult;
  try {
    result = await api.putSettings(patch);
  } catch (e) {
    if (e instanceof api.ApiError && e.kind !== "refused") app.error = e;
    throw e;
  }
  await loadSettings();
  const unread = app.error;       // the status read below would clear it, and the caller needs to tell
  await loadStatus();             // a write that went through from a read back that did not
  if (unread) app.error = unread;
  return result;
}

// One mode write at a time in the window, shared by the top bar's choices and the slider (spec 3.7, decision
// 21). A choice is sent at once when nothing is in flight; otherwise it waits, replacing any choice waiting,
// and is sent once the write in flight and its reads have returned, so the latest choice wins and none is
// swallowed. Each answers when its write returns: the result, null when it was replaced or there was nothing to
// write, or the write's error.
type Queued = { choice: ModeChoice; done: (r: PutResult | null) => void; fail: (e: unknown) => void };
let waiting: Queued | null = null;

const sameAsReported = (c: ModeChoice) =>
  "mode" in c ? c.mode === app.status?.routing : c.position === position(app.status);

async function send(q: Queued, compare = true): Promise<void> {
  // Compared when sent, not when chosen: a choice back to the mode the core reports, made while another write is
  // in flight, is still the latest choice. After a write whose read back failed the status is stale, so the
  // choice is sent without the comparison.
  if (compare && sameAsReported(q.choice)) { q.done(null); return; }
  app.modeWrite.flight = q.choice;
  try {
    const result = await saveSettings("mode" in q.choice ? modeBody(q.choice.mode) : positionBody(q.choice.position));
    // saveSettings leaves app.error set when the write went through and a read back did not.
    app.modeWrite.failure = app.error ? { choice: q.choice, error: app.error, read: true } : null;
    q.done(result);
  } catch (e) {
    q.fail(e);
    if (!(e instanceof api.ApiError)) throw e;
    app.modeWrite.failure = { choice: q.choice, error: e, read: false };
  } finally {
    app.modeWrite.flight = null;
    const next = waiting;
    waiting = null;
    app.modeWrite.waiting = null;
    if (next) void send(next, !app.modeWrite.failure?.read);
  }
}

function enqueue(choice: ModeChoice): Promise<PutResult | null> {
  return new Promise((done, fail) => {
    const q = { choice, done, fail };
    if (app.modeWrite.flight === null) { void send(q); return; }
    waiting?.done(null);
    waiting = q;
    app.modeWrite.waiting = choice;
  });
}

// The slider's key steps wait 500 ms after the last one (or its blur) before they are chosen. The timer lives here,
// beside `held`, so a choice from any control can cancel it when it takes the position.
const HOLD_MS = 500;
let holdTimer: ReturnType<typeof setTimeout> | undefined;

function takeHeld(): number | null {
  clearTimeout(holdTimer);
  const p = app.modeWrite.held;
  app.modeWrite.held = null;
  return p;
}

// A position stepped to by key, chosen 500 ms after the last step unless something chooses it first.
export function holdPosition(p: number): void {
  app.modeWrite.held = p;
  clearTimeout(holdTimer);
  holdTimer = setTimeout(() => { if (app.modeWrite.held !== null) void choosePosition(app.modeWrite.held).catch(() => {}); }, HOLD_MS);
}

// A position dragged to: held, with no timer, until the pointer lets go.
export function dragPosition(p: number): void {
  clearTimeout(holdTimer);
  app.modeWrite.held = p;
}

// The slider let go of a drag the browser cancelled: nothing is chosen.
export const dropHeld = () => { takeHeld(); };

// Every choice from a control goes through here: a position the slider holds is queued first, as a choice of its
// own, then the choice itself. No DOM event decides that order: WebKit does not focus a button on click, so a
// click need not blur the slider (decision 21).
export function choose(choice: ModeChoice): Promise<PutResult | null> {
  const held = takeHeld();
  if (held !== null) void enqueue({ position: held }).catch(() => {});
  return enqueue(choice);
}

export const chooseMode = (mode: Routing) => choose({ mode });

// The slider's own choice, from its timer, its blur or a drag let go: it is the held position, so it replaces it
// rather than queueing behind it.
export function choosePosition(p: number): Promise<PutResult | null> {
  takeHeld();
  return enqueue({ position: p });
}

// A core given no management secret file has no quota to read, and so no read to fail. While
// the settings are unread the file counts as set.
export const noSecretFile = () => app.settings !== null && !app.settings.cliproxy?.management_secret_file;

const OAUTH_POLL_MS = 2_000;
const OAUTH_WAIT_MS = 300_000;          // CLIProxyAPI's own wait for the provider's callback
const OAUTH_REFRESH_MS = 2_000;         // its file watcher needs a moment to see the new account
let oauthTimer: ReturnType<typeof setTimeout> | undefined;
let refreshTimer: ReturnType<typeof setTimeout> | undefined;     // its own: a new login must not drop it
let oauthRun = 0;                       // a flow that was cancelled or replaced ignores its late answers

export const oauthActive = () => app.oauth?.status === "starting" || app.oauth?.status === "pending";

// A failed call ends the flow: a 502 can be CLIProxyAPI refusing the secret, and
// polling on would walk into its ban. A 401 or 403 also brings up the token prompt.
function oauthFailed(e: unknown): void {
  if (e instanceof api.ApiError && (e.kind === "auth" || e.kind === "forbidden")) app.error = e;
  app.oauth = { state: "", url: "", status: "error", message: errorText(e) };
}

export function cancelOauth(): void {
  oauthRun++;
  clearTimeout(oauthTimer);
  app.oauth = null;
}

// Hands the address the other device's browser ended on to the pending login. The core's refusal is thrown as it
// is, for the card to show beside its field; a rejected token also brings up the prompt. The address is the
// caller's alone: it is neither kept here nor in app.oauth.
export async function sendCallback(url: string): Promise<void> {
  if (app.oauth?.status !== "pending") return;
  try {
    await api.sendOauthCallback(app.oauth.state, url);
  } catch (e) {
    if (e instanceof api.ApiError && (e.kind === "auth" || e.kind === "forbidden")) app.error = e;
    throw e;
  }
}

// Starts a login and returns its URL for the caller to open, or null when the
// start failed (the failure is in app.oauth) or a flow is already running.
export async function startOauth(): Promise<string | null> {
  if (oauthActive()) return null;
  const run = ++oauthRun;
  clearTimeout(oauthTimer);
  app.oauth = { state: "", url: "", status: "starting", message: "" };
  let started: api.OauthStart;
  try {
    started = await api.startOauth();
  } catch (e) {
    if (run === oauthRun) oauthFailed(e);
    return null;
  }
  if (run !== oauthRun) return null;
  app.oauth = { state: started.state, url: started.url, status: "pending", message: "" };
  const { state } = started;
  const deadline = Date.now() + OAUTH_WAIT_MS;
  const next = () => { oauthTimer = setTimeout(() => void poll(), OAUTH_POLL_MS); };
  async function poll(): Promise<void> {
    if (Date.now() >= deadline) {
      app.oauth = { state, url: "", status: "error", message: "the login timed out; start again" };
      return;
    }
    let answer: api.OauthStatus;
    try {
      answer = await api.oauthStatus(state);
    } catch (e) {
      if (run === oauthRun) oauthFailed(e);
      return;
    }
    if (run !== oauthRun) return;
    if (answer.status === "pending") return next();
    if (answer.status === "error") {
      app.oauth = { state, url: "", status: "error", message: answer.error ?? "the login failed" };
      return;
    }
    app.oauth = { state, url: "", status: "ok", message: "" };
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => void refreshAccounts(), OAUTH_REFRESH_MS);
  }
  next();
  return started.url;
}

export const canCopy = () => typeof navigator.clipboard?.writeText === "function";

// The key goes from the response to the clipboard and nowhere else. True when it
// was copied; a clipboard refusal is thrown for the caller to show.
export async function copyKey(): Promise<boolean> {
  let key: string | undefined;
  await read(() => api.getEndpoint(true), (v) => { key = v.key; });
  if (!key) return false;
  await navigator.clipboard.writeText(key);
  return true;
}

const STATUS_MS = 10_000;
const ACCOUNTS_MS = 30_000;
export const ACTIVITY_MS = 30_000;
let timers: Array<ReturnType<typeof setInterval>> = [];
let onVisible: (() => void) | null = null;

const visible = () => document.visibilityState === "visible";

export function stopPolling(): void {
  timers.forEach(clearInterval);
  timers = [];
  if (onVisible) document.removeEventListener("visibilitychange", onVisible);
  onVisible = null;
}

// A poll that runs only while the page is visible and the previous run has
// answered: a core that hangs must not pile up requests.
function polled(...loads: Array<() => Promise<void>>): () => void {
  let inFlight = false;
  return () => {
    if (!visible() || inFlight) return;
    inFlight = true;
    void Promise.all(loads.map((load) => load())).finally(() => { inFlight = false; });
  };
}

export function startPolling(tab: Tab): void {
  stopPolling();
  wantedWindow = tab === "home" ? "24h" : tab === "activity" ? app.activityWindow : null;
  // Home needs the endpoint (read until it has one) and, for its accounts
  // sentence, the accounts with every status.
  const endpointOnce = () => (app.endpoint ? Promise.resolve() : loadEndpoint());
  const status = polled(...(tab === "home" ? [loadStatus, loadUpdate, loadAccounts, endpointOnce] : [loadStatus, loadUpdate]));
  const accounts = tab === "accounts" ? polled(loadAccounts) : null;
  // Settings counts the watched accounts from one read on entry, and again when the page comes back into view.
  const watched = tab === "settings" ? polled(loadAccounts) : null;
  const activity = tab === "home" ? polled(() => loadActivity("24h"))
    : tab === "activity" ? polled(() => loadActivity(app.activityWindow)) : null;
  status();
  watched?.();
  timers.push(setInterval(status, STATUS_MS));
  if (activity) {
    activity();
    timers.push(setInterval(activity, ACTIVITY_MS));
  }
  if (accounts) {
    accounts();
    timers.push(setInterval(accounts, ACCOUNTS_MS));
  }
  onVisible = () => { status(); accounts?.(); activity?.(); watched?.(); };
  document.addEventListener("visibilitychange", onVisible);
}
