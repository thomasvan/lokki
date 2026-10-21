// One home for how the UI talks to the core: same origin, the operator header
// only when this page holds a token (in the Tauri window the shell's fetch
// wrapper adds it and the page holds none), the API's error envelope as one
// error type. Response shapes follow docs/app/api.md.
import type { Routing } from "./mode";

const TOKEN_KEY = "lokki.token";
let inMemory: string | null = null;      // the copy for a page whose storage throws (a private window)

export const token = {
  get(): string | null {
    try { return sessionStorage.getItem(TOKEN_KEY) ?? inMemory; } catch { return inMemory; }
  },
  set(t: string | null) {
    inMemory = t;
    try {
      if (t) sessionStorage.setItem(TOKEN_KEY, t);
      else sessionStorage.removeItem(TOKEN_KEY);
    } catch { /* storage refused: the in-memory copy serves this page */ }
  },
};

export type Kind = "auth" | "forbidden" | "refused" | "network" | "other";

// The UI's sentence for a core that cannot be reached; the top bar says it as a headline, the tray the same.
export const NOT_ANSWERING = "the core is not answering";

// The key of a refusal, to show beside its message; none when the message already names it.
export const keyToShow = (message: string, key?: string): string | undefined => (key && !message.includes(key) ? key : undefined);

export class ApiError extends Error {
  constructor(public status: number, message: string, public kind: Kind, public key?: string) { super(message); }
}

export type Decision = { relay_on: boolean; reason: string; computed_at?: string };
export type Poll = { last_ok_at: number | null; last_error: string | null; reads: number; running: boolean };
export type LowQuota = { threshold_percent: number; rule: "all" | "any"; accounts: string[]; models: string[]; poll_seconds: number };
export type Status = {
  revision: string; version: string; started_at: string; uptime_s: number;
  routing: Routing; low_quota: LowQuota;
  decision: Decision; poll: Poll;
  runs_since_start: Record<string, number>;
  cliproxy: { url: string; reachable: boolean; version: string | null };
  unknown_keys: string[];
};
export type Endpoint = { urls: string[]; key_masked: string | null; key?: string };
export type SchemaRow = {
  key: string; type: "int" | "float" | "bool" | "str" | "list" | "choice"; default: unknown; meaning: string;
  setting: string; env: string; minimum: number | null; maximum: number | null; choices: string[];
  startup_only: boolean; writable: boolean; secret: boolean;
  label: string; group: string; choice_labels: Record<string, string>;
};
export type SettingsView = Record<string, unknown> & {
  cliproxy: Record<string, unknown>; low_quota: LowQuota;
  env_overrides: string[]; writable: boolean; unknown_keys: string[];
};
export type PutResult = { applied: string[]; restart_required: string[]; comments_dropped: boolean };
export type Account = {
  auth_index: string; label: string; email_masked: string; enabled: boolean;
  used_percent: number | null; remaining_percent: number | null; reset_at: number | null;
  state: "low" | "ok" | "unknown"; error: string | null; in_scope: boolean;
};
export type Accounts = { read_at: number | null; age_s: number | null; accounts: Account[]; decision: Decision; poll: Poll };
export type OauthStart = { state: string; url: string };
export type OauthStatus = { status: "pending" | "ok" | "error"; error: string | null };

export type ActivityWindow = "24h" | "7d";
export type Bucket = { start: number; relay: number; passthrough: number; images: number; errors: number };
export type Percentiles = { p50: number | null; p90: number | null };
export type Outcome = "finalized" | "cut" | "client_gone" | "error" | "passed" | "delivered";
export type RecentRun = {
  ts: number; route: string; model: string | null; client: string | null; mode: string | null; cut: string | null;
  verdict: string | null; outcome: Outcome; status: number | null; elapsed_s: number | null; delivered_chars: number | null;
  segs: number | null; input_tokens: number | null; output_tokens: number | null; reason: string | null; error: string | null;
};
export type DecisionEvent = { at: number; relay_on: boolean; reason: string };
export type Activity = {
  window: ActivityWindow; now: number; from: number;
  coverage: { oldest: number | null; records: number; undated: number; skipped: number; complete: boolean };
  buckets: Bucket[];
  totals: { requests: number; relay: number; passthrough: number; images: number; errors: number };
  relay: { finalized: number; cut: number; client_gone: number; error: number; by_cut: Record<string, number> };
  passthrough: { by_reason: Record<string, number>; by_cut: Record<string, number> };
  tokens: { runs: number; input: number; output: number; cached: number; reasoning: number };
  models: Array<{ model: string; runs: number }>; clients: Array<{ client: string; runs: number }>;
  latency: { relay: Percentiles; passthrough: Percentiles; images: Percentiles; all: Percentiles };
  recent: RecentRun[]; events: DecisionEvent[];
};

type Envelope = { error?: { message?: string }; key?: string };

// The longest wait for the core: the accounts refresh may take up to 15 s (docs/app/api.md).
const TIMEOUT_MS = 20_000;

// `candidate` is a token not yet stored: the call that checks it carries it, and
// the caller stores it once the core has accepted it.
export async function request<T>(method: string, path: string, body?: unknown, candidate?: string): Promise<T> {
  const headers: Record<string, string> = {};
  const t = candidate ?? token.get();
  if (t) headers["X-Relay-Token"] = t;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, TIMEOUT_MS);
  let resp: Response;
  let text: string;
  try {
    resp = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal });
    text = await resp.text();
  } catch {
    throw new ApiError(0, timedOut ? "the core did not answer in time" : NOT_ANSWERING, "network");
  } finally {
    clearTimeout(timer);
  }
  let parsed: unknown = null;
  let json = true;
  if (text) { try { parsed = JSON.parse(text); } catch { json = false; } }
  if (resp.ok) {
    if (!json) throw new ApiError(resp.status, "the answer was not JSON", "other");
    return parsed as T;
  }
  const envelope = (parsed ?? {}) as Envelope;
  const message = envelope.error?.message ?? `HTTP ${resp.status}`;
  let kind: Kind = "other";
  if (resp.status === 401) kind = "auth";
  else if (resp.status === 403) kind = "forbidden";
  else if (resp.status === 409 || resp.status === 400) kind = "refused";
  throw new ApiError(resp.status, message, kind, envelope.key);
}

export const getStatus = (candidate?: string) => request<Status>("GET", "/api/status", undefined, candidate);
export const getEndpoint = (reveal = false) => request<Endpoint>("GET", reveal ? "/api/endpoint?reveal=1" : "/api/endpoint");
export const getSchema = () => request<SchemaRow[]>("GET", "/api/schema");
export const getSettings = () => request<SettingsView>("GET", "/api/settings");
export const putSettings = (patch: Record<string, unknown>) => request<PutResult>("PUT", "/api/settings", patch);
export const getActivity = (window: ActivityWindow) => request<Activity>("GET", `/api/activity?window=${window}`);
export const getModels = () => request<{ models: string[] }>("GET", "/api/models");
export const getAccounts = () => request<Accounts>("GET", "/api/accounts");
export const refreshAccounts = () => request<Accounts>("POST", "/api/accounts/refresh");
export const startOauth = () => request<OauthStart>("POST", "/api/accounts/oauth");
// The pasted address goes in the body only; the path holds the login's state.
export const sendOauthCallback = (state: string, url: string) =>
  request<{ handed_on: boolean }>("POST", `/api/accounts/oauth/${encodeURIComponent(state)}`, { url });
export const oauthStatus = (state: string) => request<OauthStatus>("GET", `/api/accounts/oauth/${encodeURIComponent(state)}`);
