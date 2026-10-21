// The init script runs in a vm context with a stub `fetch`, `location` and `__TAURI__`,
// filled the way `window.rs` fills it (the filling is written at the top of inject.js).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const TEMPLATE = readFileSync(new URL("./inject.js", import.meta.url), "utf8");
const BUILD_RS = readFileSync(new URL("../src-tauri/build.rs", import.meta.url), "utf8");
const LOKKI_D_TS = readFileSync(new URL("../../ui/src/lib/lokki.d.ts", import.meta.url), "utf8");
const UPDATE_RS = readFileSync(new URL("../src-tauri/src/update.rs", import.meta.url), "utf8");

const ORIGIN = "http://127.0.0.1:8381";
const TOKEN = "tok-0123456789abcdef";

// The filling: each placeholder appears once; origin first, then token, so the token's
// text is never scanned for a placeholder; both as JSON string literals.
function fill(token, origin) {
  for (const p of ["__ORIGIN__", "__TOKEN__"]) {
    assert.equal(TEMPLATE.split(p).length, 2, `${p} must appear exactly once`);
  }
  return TEMPLATE.replace("__ORIGIN__", () => JSON.stringify(origin)).replace("__TOKEN__", () => JSON.stringify(token));
}

function load({ token = TOKEN, origin = ORIGIN, pageOrigin = ORIGIN } = {}) {
  const fetched = [];
  const invoked = [];
  const logged = [];
  const stubFetch = (...args) => {
    fetched.push(args);
    return Promise.resolve("stub-response");
  };
  const invokeResult = { value: Promise.resolve("invoked") };
  const log = (...args) => logged.push(args);
  const ctx = {
    fetch: stubFetch,
    Headers,
    Request,
    URL,
    location: { origin: pageOrigin, href: `${pageOrigin}/` },
    console: { log, info: log, warn: log, error: log, debug: log, trace: log },
    __TAURI__: { core: { invoke: (...args) => (invoked.push(args), invokeResult.value) } },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fill(token, origin), ctx);
  return { ctx, stubFetch, fetched, invoked, logged, invokeResult };
}

const headerOf = (init, name = "X-Relay-Token") => (init?.headers === undefined ? null : new Headers(init.headers).get(name));

test("adds_the_header_only_on_the_core_origin", async () => {
  const inputs = {
    absolute: `${ORIGIN}/api/status`,
    relative: "/api/status",
    "url object": new URL(`${ORIGIN}/api/status`),
    request: new Request(`${ORIGIN}/api/status`, { headers: { "X-Own": "kept" } }),
    "other port": "http://127.0.0.1:8317/",
    "other host": "https://example.com/api/status",
    "other scheme": "https://127.0.0.1:8381/api/status",
    "origin as a prefix of the host": "http://127.0.0.1:8381.evil.test/",
    "origin as userinfo": "http://127.0.0.1:8381@evil.test/",
    "protocol-relative": "//example.com/api/status",
  };
  const onCore = new Set(["absolute", "relative", "url object", "request"]);
  const headerForms = {
    none: undefined,
    Headers: () => ({ headers: new Headers({ "X-Own": "kept" }) }),
    object: () => ({ headers: { "X-Own": "kept" } }),
    array: () => ({ headers: [["X-Own", "kept"]] }),
    "a stale token": () => ({ headers: { "X-Relay-Token": "stale" } }),
  };
  for (const [what, input] of Object.entries(inputs)) {
    for (const [form, make] of Object.entries(headerForms)) {
      const { ctx, fetched } = load();
      const init = make?.();
      const result = await ctx.window.fetch(input, init);
      const label = `${what} with headers ${form}`;
      assert.equal(result, "stub-response", label);
      assert.equal(fetched.length, 1, label);
      const [sentInput, sentInit] = fetched[0];
      if (onCore.has(what)) {
        assert.equal(headerOf(sentInit), TOKEN, label);
        if (form === "none" && what === "request") assert.equal(headerOf(sentInit, "X-Own"), "kept", label);
        if (init && form !== "a stale token") assert.equal(headerOf(sentInit, "X-Own"), "kept", label);
      } else {
        assert.equal(sentInput, input, label);
        assert.equal(sentInit, init, label);
      }
    }
  }
});

test("an_origin_that_extends_the_core_origin_gets_no_header", async () => {
  const { ctx, fetched } = load({ origin: "http://127.0.0.1:80", pageOrigin: "http://127.0.0.1:80" });
  await ctx.window.fetch("http://127.0.0.1:8081/api/status");
  assert.equal(headerOf(fetched[0][1]), null);
});

test("a_request_keeps_the_options_it_was_given", async () => {
  const { ctx, fetched } = load();
  const signal = new AbortController().signal;
  await ctx.window.fetch(`${ORIGIN}/api/settings`, { method: "PUT", body: "{}", signal });
  const [, init] = fetched[0];
  assert.equal(init.method, "PUT");
  assert.equal(init.body, "{}");
  assert.equal(init.signal, signal);
});

test("does_nothing_on_another_page_origin", async () => {
  const { ctx, stubFetch, fetched, invoked } = load({ pageOrigin: "http://127.0.0.1:8317" });
  assert.equal(ctx.window.fetch, stubFetch);
  assert.equal(ctx.window.lokki, undefined);
  await ctx.window.fetch(`${ORIGIN}/api/status`);
  assert.equal(headerOf(fetched[0][1]), null);
  assert.deepEqual(invoked, []);
});

test("the_token_is_not_in_fetch_to_string", () => {
  const { ctx, stubFetch } = load();
  assert.notEqual(ctx.window.fetch, stubFetch);
  const surfaces = [ctx.window.fetch, ...Object.values(ctx.window.lokki)].map((f) => f.toString());
  for (const source of surfaces) assert.ok(!source.includes(TOKEN), source);
  assert.ok(!Object.values(ctx).includes(TOKEN));
});

test("a_hostile_token_stays_inside_its_literal", async () => {
  const hostile = [
    `"; globalThis.pwned = 1; //`,
    `'; globalThis.pwned = 1; //`,
    "\\",
    "\\\"; globalThis.pwned = 1; //",
    "</script><script>globalThis.pwned = 1</script>",
    "`${globalThis.pwned = 1}`",
    "__ORIGIN__",
    "__TOKEN__",
  ];
  for (const token of hostile) {
    const { ctx, fetched } = load({ token });
    await ctx.window.fetch(`${ORIGIN}/api/status`);
    assert.equal(headerOf(fetched[0][1]), token);
    assert.equal(ctx.pwned, undefined, token);
  }
  // A header value cannot hold these, so only the script's own survival is checked.
  const { ctx: unsendable } = load({ token: "line\nbreak\r\u2028\u2029\"" });
  assert.equal(typeof unsendable.window.lokki.restart, "function");
  const { ctx, fetched } = load({ origin: "http://127.0.0.1:9", pageOrigin: "http://127.0.0.1:9" });
  await ctx.window.fetch("/api/status");
  assert.equal(headerOf(fetched[0][1]), TOKEN);
});

test("lokki_hooks_invoke_their_commands", async () => {
  const hooks = [
    ["restart", ["cliproxy"], "restart_service", { service: "cliproxy" }],
    ["openExternal", ["https://example.com/login"], "open_external", { url: "https://example.com/login" }],
    ["openPanel", [], "open_panel", undefined],
    ["copyManagementSecret", [], "copy_management_secret", undefined],
    ["updateStatus", [], "update_status", undefined],
    ["checkForUpdate", [], "check_for_update", undefined],
    ["installUpdate", [], "install_update", undefined],
  ];
  const manifest = BUILD_RS.match(/COMMANDS: &\[&str\] = &\[([^\]]*)\]/)[1].match(/"([a-z_]+)"/g).map((s) => s.slice(1, -1));
  const typed = [...LOKKI_D_TS.matchAll(/^\s+(\w+)\?:\s*\(/gm)].map((m) => m[1]);
  const { ctx, invoked, invokeResult } = load();
  assert.deepEqual(typed.sort(), Object.keys(ctx.window.lokki).sort(), "lokki.d.ts types exactly the hooks inject.js defines");
  assert.deepEqual(Object.keys(ctx.window.lokki).sort(), hooks.map(([name]) => name).sort());
  // The machine's states are `&'static str` literals (`state: "idle"`, `i.state = "…"`, `self.set("…")`) in the
  // source above its tests; the page's type must name exactly those.
  const machine = UPDATE_RS.split(/^#\[cfg\(test\)\]/m)[0];
  const states = [...new Set([...machine.matchAll(/(?:\bstate: |\.state = |\bself\.set\()"([a-z_]+)"/g)].map((m) => m[1]))];
  const typedStates = [...LOKKI_D_TS.match(/state:\s*([^;]+);/)[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
  assert.deepEqual(typedStates.sort(), states.sort(), "lokki.d.ts's UpdateStatus.state names the machine's states in update.rs");
  for (const [name, args, command, invokeArgs] of hooks) {
    assert.ok(manifest.includes(command), `${command} is declared in build.rs`);
    invoked.length = 0;
    const promise = invokeResult.value;
    assert.equal(ctx.window.lokki[name](...args), promise, `${name} returns the invoke promise`);
    // JSON, because the arguments are built inside the vm context, with its own prototypes.
    assert.equal(JSON.stringify(invoked), JSON.stringify([[command, invokeArgs]]), name);
  }
  const failure = new Error("could not restart");
  invokeResult.value = Promise.reject(failure);
  await assert.rejects(ctx.window.lokki.restart("core"), failure);
});

test("the_script_never_logs", async () => {
  const { ctx, logged } = load();
  await ctx.window.fetch(`${ORIGIN}/api/status`, { headers: { "X-Relay-Token": "stale" } });
  await ctx.window.fetch("https://example.com/");
  await ctx.window.lokki.restart("core");
  await ctx.window.lokki.openPanel();
  load({ pageOrigin: "http://127.0.0.1:8317" });
  assert.deepEqual(logged, []);
});
