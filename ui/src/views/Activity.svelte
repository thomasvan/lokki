<script lang="ts">
  import type { ActivityWindow, Outcome, RecentRun } from "../lib/api";
  import { count, seconds, stamp, tokens } from "../lib/format";
  import { headline } from "../lib/mode";
  import { useNow } from "../lib/now.svelte";
  import { app, chooseWindow, errorText, type Tab } from "../lib/state.svelte";
  import Breakdown from "../components/Breakdown.svelte";
  import Icon from "../components/Icon.svelte";
  import Latency from "../components/Latency.svelte";
  import RequestsChart from "../components/RequestsChart.svelte";

  let { ontab }: { ontab: (tab: Tab) => void } = $props();

  // Only an answer for the window chosen draws; until it lands the page says it is reading.
  const answer = $derived(app.activity?.window === app.activityWindow ? app.activity : null);
  const WINDOWS: Array<[ActivityWindow, string]> = [["24h", "24 h"], ["7d", "7 d"]];

  // The choice is a read, not a write: the buttons stay enabled and the group is busy while it runs, so a
  // focused button keeps the focus. A choice made meanwhile is read at once and its answer is the one drawn
  // (the state module drops the earlier read's); the choice already made reads nothing.
  function choose(window: ActivityWindow) {
    if (window !== app.activityWindow) void chooseWindow(window);
  }

  // Every time on the page is seen from now: "within the last day" turns over between two reads.
  const clock = useNow(() => { void app.activity; void app.status; });
  const nowS = $derived(clock.s);

  // The two sentences of spec 4.3, word for word; a complete log that reaches back past the window says nothing.
  const coverage = $derived.by(() => {
    const c = answer?.coverage;
    if (!answer || !c || c.oldest === null) return null;
    if (!c.complete) return `counted since ${stamp(c.oldest, nowS)}: Lokki reads the last 8 MiB of its run log; older runs in this window are not counted`;
    return c.oldest > answer.from ? `since ${stamp(c.oldest, nowS)}: the run log holds runs since then` : null;
  });

  const started = $derived.by(() => {
    const at = Date.parse(app.status?.started_at ?? "") / 1000;
    return Number.isFinite(at) ? `since Lokki started at ${stamp(at, nowS)}` : "since Lokki started";
  });

  const byCount = (table: Record<string, number>) =>
    Object.entries(table).map(([name, n]) => ({ name, n })).sort((a, b) => b.n - a.n || a.name.localeCompare(b.name));

  const OUTCOMES = [
    ["cut", "Cut by the relay before the provider finalized", "cutting"],
    ["finalized", "Finalized, billed as a direct request", "passing"],
    ["client_gone", "Client left", "unknown"],
    ["error", "Error", "error"],
  ] as const;

  const ROUTE_WORD: Record<string, string> = { relay: "relay", passthrough: "pass-through", images: "images" };
  const failed = (o: Outcome) => o === "error";
  const why = (r: RecentRun) => r.error ?? r.reason;
</script>

<div class="phead">
  <h2>Activity</h2>
  <span class="lbl">requests through Lokki</span>
  <div class="seg-sm" role="group" aria-label="Window" aria-busy={app.activityBusy ? "true" : "false"}>
    {#each WINDOWS as [id, label] (id)}
      <button type="button" aria-pressed={app.activityWindow === id ? "true" : "false"} onclick={() => choose(id)}>
        {#if app.activityBusy && app.activityWindow === id}<Icon name="spinner" size={16} />{/if}{label}
      </button>
    {/each}
  </div>
</div>

{#if coverage}<p class="since"><Icon name="info" size={16} />{coverage}</p>{/if}

<section aria-labelledby="act-chart">
  <div class="sh">
    <h2 id="act-chart">Requests over time</h2>
    {#if answer}<span class="lbl">{answer.window === "24h" ? "per hour, by route" : "per 6 hours, by route"}</span>{/if}
  </div>
  {#if answer}
    <RequestsChart activity={answer} />
  {:else if app.error && !app.activityBusy}
    <p role="alert">The run log could not be read: {errorText(app.error)}</p>
  {:else}
    <p class="lbl" role="status">Reading the run log…</p>
  {/if}
</section>

{#if answer && answer.totals.requests === 0}
  <div class="card empty">
    <span class="em-ic"><Icon name="activity" /></span>
    <div><h3>No requests yet</h3><p>Point your client at the endpoint on Home.</p></div>
    <button type="button" class="btn-primary" onclick={() => ontab("home")}>Open Home</button>
  </div>
{:else if answer}
  {@const relayRuns = answer.totals.relay}
  <div class="cols">
    <section aria-labelledby="act-relay">
      <div class="sh"><h2 id="act-relay">Relay outcomes</h2><span class="lbl">{count(relayRuns)} relay requests</span></div>
      <div class="split" role="img" aria-label={OUTCOMES.map(([k, name]) => `${answer.relay[k]} ${name.toLowerCase()}`).join(", ")}>
        {#each OUTCOMES as [key, , tone] (key)}
          {#if answer.relay[key] > 0}<span style:flex={answer.relay[key]} style:background="var(--{tone})"></span>{/if}
        {/each}
      </div>
      <ul class="kv">
        {#each OUTCOMES as [key, name, tone] (key)}
          <li><span class="sq" style:background="var(--{tone})"></span><span>{name}</span><span class="n">{count(answer.relay[key])}</span></li>
        {/each}
      </ul>
      <h3 class="sub-h">How the cut runs ended</h3>
      <Breakdown label="How the cut runs ended" rows={byCount(answer.relay.by_cut)} tone="cutting" mono />
    </section>

    <section aria-labelledby="act-pass">
      <div class="sh"><h2 id="act-pass">Pass-through reasons</h2><span class="lbl">{count(answer.totals.passthrough)} pass-through requests</span></div>
      <p class="lbl">The reason the core gave, verbatim.</p>
      <Breakdown label="Pass-through reasons" rows={byCount(answer.passthrough.by_reason)} tone="passing" mono wide />
      <h3 class="sub-h">How they ended</h3>
      <Breakdown label="How the pass-through runs ended" rows={byCount(answer.passthrough.by_cut)} tone="passing" mono />
    </section>
  </div>

  <section aria-labelledby="act-tokens">
    <div class="sh"><h2 id="act-tokens">Tokens the provider reported</h2></div>
    <dl class="stats">
      <div class="stat"><dt>Input</dt><dd class="big">{tokens(answer.tokens.input)}</dd></div>
      <div class="stat"><dt>Output</dt><dd class="big">{tokens(answer.tokens.output)}</dd></div>
      <div class="stat"><dt>Cached</dt><dd class="big">{tokens(answer.tokens.cached)}</dd></div>
      <div class="stat"><dt>Reasoning</dt><dd class="big">{tokens(answer.tokens.reasoning)}</dd></div>
    </dl>
    <p class="lbl">from {count(answer.tokens.runs)} runs that reported usage</p>
    <p class="foot-note"><Icon name="info" size={16} />Cut runs get no token count from the provider, and these totals count none for them.</p>
  </section>

  <div class="cols">
    <section aria-labelledby="act-mc">
      <div class="sh"><h2 id="act-mc">Models and clients</h2><span class="lbl">requests each</span></div>
      <h3 class="sub-h">Models</h3>
      <Breakdown label="Models" rows={answer.models.map((m) => ({ name: m.model, n: m.runs }))} tone="accent" mono wide />
      <h3 class="sub-h">Clients</h3>
      <Breakdown label="Clients" rows={answer.clients.map((c) => ({ name: c.client, n: c.runs }))} tone="accent" mono wide />
    </section>

    <section aria-labelledby="act-lat">
      <div class="sh"><h2 id="act-lat">Latency</h2><span class="lbl">start to last byte</span></div>
      <Latency rows={[
        { name: "Relay", tone: "cutting", at: answer.latency.relay },
        { name: "Pass-through", tone: "passing", at: answer.latency.passthrough },
        { name: "Images", tone: "images", at: answer.latency.images },
        { name: "All requests", tone: "accent", at: answer.latency.all },
      ]} />
    </section>
  </div>

  <section aria-labelledby="act-runs">
    <div class="sh"><h2 id="act-runs">Recent runs</h2><span class="lbl">newest first</span></div>
    <table class="runs" aria-labelledby="act-runs">
      <thead>
        <tr><th scope="col">Time</th><th scope="col">Route</th><th scope="col">Model</th><th scope="col">Client</th><th scope="col">Outcome</th><th scope="col" class="num">Status</th><th scope="col" class="num">Duration</th><th scope="col" class="num">Tokens out</th></tr>
      </thead>
      <tbody>
        {#each answer.recent as r, i (i)}
          <tr class:err={failed(r.outcome)}>
            <td class="t">{stamp(r.ts, nowS)}</td>
            <td class="rt"><span class="chip chip-{r.route === 'relay' ? 'cutting' : r.route === 'passthrough' ? 'passing' : 'images'}">{ROUTE_WORD[r.route] ?? r.route}</span></td>
            <td class="m"><span class="trunc" title={r.model ?? "unknown"}>{r.model ?? "unknown"}</span></td>
            <td class="m cl"><span class="trunc" title={r.client ?? "unknown"}>{r.client ?? "unknown"}</span></td>
            <td class="oc">
              <span class="out">{#if failed(r.outcome)}<Icon name="error" size={16} />{/if}{r.outcome}</span>
              {#if why(r)}<span class="why trunc" title={why(r)}>{why(r)}</span>{/if}
            </td>
            <td class="num st">{r.status ?? "—"}</td>
            <td class="num du">{r.elapsed_s === null ? "—" : seconds(r.elapsed_s)}</td>
            <td class="num tk">{#if r.output_tokens === null}<span title="no tokens reported">—</span>{:else}{count(r.output_tokens)}{/if}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </section>
{/if}

{#if answer}
  <section aria-labelledby="act-events">
    <div class="sh"><h2 id="act-events">Decision changes</h2><span class="lbl">{started}</span></div>
    {#if answer.events.length === 0}
      <p class="lbl">No decision yet.</p>
    {:else}
      <ol class="tl" aria-label="Decision changes">
        {#each answer.events as e, i (i)}
          <li>
            <span class="tm">{stamp(e.at, nowS)}</span>
            <span class="dot" class:dot-cutting={e.relay_on} class:dot-passing={!e.relay_on}></span>
            <span class="tx"><b>{headline(e.relay_on)}</b><span class="sep">·</span><span class="r">{e.reason}</span></span>
          </li>
        {/each}
      </ol>
    {/if}
  </section>
{/if}
