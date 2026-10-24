// The fixture: a real core and a fake CLIProxyAPI under a throwaway data directory.
import { test as base, expect } from "@playwright/test";
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { createServer, type AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join, relative, resolve, isAbsolute } from "node:path";

const UI = resolve(import.meta.dirname, "..");
const CHECKOUT = resolve(UI, "..");
const CORE_DIR = join(CHECKOUT, "core");
const FAKE = join(UI, "e2e", "fake_cliproxy.py");
const PYTHON = process.env.LOKKI_PYTHON || "python3";
const LIVE_PORTS = [8381, 8318];      // the live relay and its CLIProxyAPI on the workstation: never ours
const READ_WAIT_MS = 30_000;

// The core's drain is its schema default (the file does not name it); the fixture shipped with the UI is that listing.
export function drainSeconds(): number {
  const rows = JSON.parse(readFileSync(join(UI, "src", "views", "schema.fixture.json"), "utf8")) as Array<{ key: string; default: unknown }>;
  const value = rows.find((r) => r.key === "drain_seconds")?.default;
  if (typeof value !== "number") throw new Error("schema.fixture.json has no numeric default for drain_seconds");
  return value;
}

// A port another program holds on 127.0.0.1, as the OS picks it, until `close`; never one of the live pair's.
export async function holdPort(): Promise<{ port: number; close: () => Promise<void> }> {
  const server = createServer();
  await new Promise<void>((done, fail) => server.once("error", fail).listen(0, "127.0.0.1", done));
  const { port } = server.address() as AddressInfo;
  const close = () => new Promise<void>((done) => server.close(() => done()));
  if (LIVE_PORTS.includes(port)) {
    await close();
    throw new Error(`the OS picked port ${port}, which belongs to the live relay pair; refusing to run`);
  }
  return { port, close };
}

// A port nothing listens on, as the OS picks it.
export async function freePort(): Promise<number> {
  const held = await holdPort();
  await held.close();
  return held.port;
}

export interface Core {
  url: string;
  token: string;
  dataDir: string;
  fakePort: number;
  configPath: string;
  stop(): Promise<void>;
  setFile(patch: Record<string, unknown>): void;
  reload(): Promise<void>;
}

// Every secret `init` makes is 64 hex characters; a log line shown in an error has them removed.
const redact = (s: string) => s.replace(/[0-9a-f]{64}/g, "<secret>");

const MERGE = [
  "import sys, json, yaml",
  "path, patch = sys.argv[1], json.load(sys.stdin)",
  "doc = yaml.safe_load(open(path, encoding='utf-8')) or {}",
  "def merge(into, new):",
  "    for k, v in new.items():",
  "        if isinstance(v, dict) and isinstance(into.get(k), dict): merge(into[k], v)",
  "        else: into[k] = v",
  "merge(doc, patch)",
  "open(path, 'w', encoding='utf-8').write(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))",
].join("\n");

const OPERATOR_TOKEN = [
  "import sys, yaml",
  "doc = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))",
  "print(next(t['token'] for t in doc['auth']['tokens'] if t.get('name') == 'app' and t.get('operator')))",
].join("\n");

function python(args: string[], input?: string): string {
  return execFileSync(PYTHON, args, {
    cwd: CORE_DIR, input, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"],
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
}

// The core reads RELAY_* and CLIPROXY_URL from its environment and lets them win over the file;
// a developer's shell must not change what the run proves.
function cleanEnv(runsLog: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { PYTHONDONTWRITEBYTECODE: "1", PYTHONUNBUFFERED: "1" };
  for (const [k, v] of Object.entries(process.env)) {
    if (!/^(RELAY_|CLIPROXY_)/.test(k)) env[k] = v;
  }
  env.RELAY_RUNS_LOG = runsLog;
  return env;
}

function tail(lines: string[]): string {
  return redact(lines.slice(-20).join("\n"));
}

function exited(p: ChildProcess): Promise<void> {
  return new Promise((done) => {
    if (p.exitCode !== null || p.signalCode !== null) done();
    else p.once("exit", () => done());
  });
}

async function within(ms: number, p: Promise<unknown>): Promise<boolean> {
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<boolean>((done) => { timer = setTimeout(() => done(false), ms); });
  try {
    return await Promise.race([p.then(() => true), timeout]);
  } finally {
    clearTimeout(timer);
  }
}

async function until<T>(what: string, ms: number, probe: () => Promise<T | undefined>, onFail: () => string): Promise<T> {
  const end = Date.now() + ms;
  for (;;) {
    try {
      const got = await probe();
      if (got !== undefined) return got;
    } catch { /* not up yet */ }
    if (Date.now() > end) throw new Error(`${what} did not happen within ${ms / 1000} s\n${onFail()}`);
    await new Promise((r) => setTimeout(r, 200));
  }
}

async function start(): Promise<Core> {
  const root = realpathSync(tmpdir());
  const inside = relative(CHECKOUT, root);
  if (!inside.startsWith("..") && !isAbsolute(inside)) {
    throw new Error("TMPDIR is inside the checkout; the data directory must be outside it");
  }
  const dataDir = mkdtempSync(join(root, "lokki-e2e-"));
  const configPath = join(dataDir, "config.yaml");
  const log: string[] = [];
  let core: ChildProcess | undefined;
  let fake: ChildProcess | undefined;
  let token = "";
  let url = "";
  let stopped = false;

  const keep = (name: string, p: ChildProcess) => {
    for (const stream of [p.stdout, p.stderr]) {
      stream?.on("data", (d: Buffer) => log.push(...d.toString().split("\n").filter((l) => l).map((l) => `${name}: ${l}`)));
    }
  };

  const stop = async () => {
    if (stopped) return;
    stopped = true;
    try {
      if (core && core.exitCode === null && url) {
        await fetch(`${url}/api/shutdown`, { method: "POST", headers: { "X-Relay-Token": token } }).catch(() => undefined);
        if (!(await within((drainSeconds() + 5) * 1000, exited(core)))) core.kill("SIGKILL");
      }
    } finally {
      core?.kill("SIGKILL");
      fake?.kill("SIGKILL");
      await Promise.all([core, fake].map((p) => (p ? exited(p) : undefined)));
      rmSync(dataDir, { recursive: true, force: true });
    }
  };

  try {
    // 1. init: ports and paths come back, no secret
    const made = JSON.parse(python(["-m", "lokki_core", "init", "--data-dir", dataDir, "--core-port", "0", "--cliproxy-port", "0"]).trim());
    for (const port of [made.core_port, made.cliproxy_port]) {
      if (LIVE_PORTS.includes(port)) throw new Error(`init chose port ${port}, which belongs to the live relay pair; refusing to run`);
    }

    // 2. the fake, with the management secret by path
    fake = spawn(PYTHON, [FAKE, "--secret-file", join(dataDir, "secrets", "cliproxy-mgmt.secret")], {
      cwd: CORE_DIR, stdio: ["pipe", "pipe", "pipe"], env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONUNBUFFERED: "1" },
    });
    keep("fake", fake);
    const fakePort = await until("the fake's port", 15_000, async () => {
      const m = log.map((l) => /^fake: port=(\d+)$/.exec(l)).find((x) => x);
      return m ? Number(m[1]) : undefined;
    }, () => tail(log));
    if (LIVE_PORTS.includes(fakePort)) throw new Error(`the fake bound port ${fakePort}, which belongs to the live relay pair; refusing to run`);

    // 3. both files agree on the fake's port
    python(["-c", MERGE, configPath], JSON.stringify({ cliproxy: { port: fakePort } }));
    python(["-m", "lokki_core", "sync-cliproxy", "--data-dir", dataDir]);

    // 3b. the run log: three days of sample runs, written before the core starts (it chains onto them)
    python([join(UI, "e2e", "seed_runs.py"), "--out", join(dataDir, "runs.jsonl"), "--now", String(Math.floor(Date.now() / 1000))]);

    // 4. the operator token (the file is as init wrote it: Auto, with a readable secret)
    token = python(["-c", OPERATOR_TOKEN, configPath]).trim();
    url = `http://127.0.0.1:${made.core_port}`;

    // 5. the core, then its first quota read
    core = spawn(PYTHON, ["-m", "lokki_core", "--config", configPath], {
      cwd: CORE_DIR, stdio: ["ignore", "pipe", "pipe"], env: cleanEnv(join(dataDir, "runs.jsonl")),
    });
    keep("core", core);
    const dead = () => (core!.exitCode !== null ? `the core exited with status ${core!.exitCode}\n${tail(log)}` : "");
    await until("the core's /healthz", 30_000, async () => {
      if (core!.exitCode !== null) throw new Error(dead());
      return (await fetch(`${url}/healthz`)).ok ? true : undefined;
    }, () => tail(log)).catch((e) => { throw new Error(dead() || e.message); });
    const headers = { "X-Relay-Token": token };
    const reads = await until("the poller's first read", READ_WAIT_MS, async () => {
      const r = await fetch(`${url}/api/status`, { headers });
      const s = (await r.json()) as { poll: { reads: number } };
      return r.ok && s.poll.reads >= 1 ? s.poll.reads : undefined;
    }, () => tail(log));
    console.log(`core up on ${url} (fake CLIProxyAPI on 127.0.0.1:${fakePort}); poll.reads=${reads}`);
    // The core must be the one just started: it reports the port it listens on, and neither is a live port.
    const endpoint = (await (await fetch(`${url}/api/endpoint`, { headers })).json()) as { urls: string[] };
    const ports = endpoint.urls.map((u) => Number(new URL(u).port));
    if (!ports.includes(made.core_port) || ports.some((p) => LIVE_PORTS.includes(p))) {
      throw new Error(`the core reports ports ${ports.join(", ")}; refusing to run`);
    }

    return {
      url, token, dataDir, fakePort, configPath, stop,
      setFile: (patch) => { python(["-c", MERGE, configPath], JSON.stringify(patch)); },
      reload: async () => {
        const r = await fetch(`${url}/api/reload`, { method: "POST", headers });
        const body = (await r.json()) as { ok?: boolean };
        if (!r.ok || !body.ok) throw new Error(`reload failed: ${r.status} ${redact(JSON.stringify(body))}`);
      },
    };
  } catch (e) {
    await stop();
    throw e;
  }
}

export const test = base.extend<{ baseURL: string }, { core: Core }>({
  // eslint-disable-next-line no-empty-pattern -- Playwright reads the fixture's dependencies from this parameter
  core: [async ({}, use) => {
    const core = await start();
    try {
      await use(core);
    } finally {
      await core.stop();             // idempotent; runs when the test fails too
    }
  }, { scope: "worker" }],
  baseURL: async ({ core }, use) => { await use(core.url); },
});

export { expect };
