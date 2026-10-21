<script lang="ts">
  import { onDestroy } from "svelte";
  import { age, resetsIn } from "../lib/format";
  import { gaugeOf } from "../lib/gauge";
  import { NO_SECRET, NO_SECRET_AUTO, READ_FAILED_AUTO } from "../lib/mode";
  import { accountsUnread, app, canCopy, copyKey, errorText, noSecretFile, type Tab } from "../lib/state.svelte";
  import Gauge from "../components/Gauge.svelte";
  import Icon from "../components/Icon.svelte";
  import RequestsChart from "../components/RequestsChart.svelte";

  let { ontab }: { ontab: (tab: Tab) => void } = $props();

  const COPIED_MS = 3_000;
  let copyNote = $state("");
  let noteTimer: ReturnType<typeof setTimeout> | undefined;
  onDestroy(() => clearTimeout(noteTimer));

  async function copy() {
    clearTimeout(noteTimer);
    try {
      copyNote = (await copyKey()) ? "copied" : "";
    } catch (e) {
      copyNote = `could not copy: ${errorText(e)}`;
      return;
    }
    noteTimer = setTimeout(() => { copyNote = ""; }, COPIED_MS);
  }

  const status = $derived(app.status);
  const auto = $derived(status?.routing === "auto");
  const noSecret = $derived(noSecretFile());
  // In Auto the watched accounts come first; the others follow, outlined. In Relay and Direct the threshold
  // decides nothing, so the rows keep the core's order.
  const rows = $derived.by(() => {
    const list = app.accounts?.accounts ?? [];
    return auto ? [...list.filter((a) => a.in_scope), ...list.filter((a) => !a.in_scope)] : list;
  });
  // The strip's one state. With no snapshot a failed read wins: the banner above says it.
  const strip = $derived.by(() => {
    if (noSecret) return "no-secret";
    if (!app.accounts && app.failed.accounts) return "unanswered";
    if (!app.accounts || app.accounts.read_at === null) return status?.poll.last_error ? "failed" : "unread";
    return rows.length === 0 ? "empty" : "gauges";
  });
  const nowS = () => Math.floor(Date.now() / 1000);
  const resetLine = (a: (typeof rows)[number]) => {
    const now = nowS();
    if (gaugeOf(a, now).windowReset) return "";
    return a.reset_at !== null && a.reset_at > now ? `resets in ${resetsIn(a.reset_at - now)}` : "reset time not read yet";
  };

  const ROUTES = [["relay", "Relay", "s-relay"], ["passthrough", "Pass-through", "s-pass"], ["images", "Images", "s-img"]] as const;
  const runs = $derived(status?.runs_since_start ?? {});
  const total = $derived(ROUTES.reduce((sum, [route]) => sum + (runs[route] ?? 0), 0));
  const chart = $derived(app.activity?.window === "24h" ? app.activity : null);
</script>

{#if status}
  {#if !noSecret && status.poll.last_error}
    <div class="banner banner-error" role="alert">
      <Icon name="error" />
      <p>The last quota read failed: {status.poll.last_error}{auto ? READ_FAILED_AUTO : ""}</p>
    </div>
  {/if}

  <section aria-labelledby="home-endpoint">
    <div class="sh"><h2 id="home-endpoint">Endpoint</h2><span class="lbl">point your coding client at this address, with this key</span></div>
    {#if app.endpoint}
      <div class="endpoint">
        <ul class="ep">{#each app.endpoint.urls as url, i (i)}<li>{url}</li>{/each}</ul>
        {#if app.endpoint.key_masked}
          <p class="ep-key">Key <code>{app.endpoint.key_masked}</code>
            <button type="button" disabled={!canCopy()} aria-describedby={canCopy() ? undefined : "copy-why"} onclick={copy}><Icon name={copyNote === "copied" ? "check" : "copy"} size={16} />Copy key</button>
            {#if copyNote}<span role="status">{copyNote}</span>{/if}</p>
        {/if}
      </div>
      {#if app.endpoint.key_masked && !canCopy()}<p class="lbl" id="copy-why">copying needs a secure origin; the key is in the settings file</p>{/if}
    {/if}
  </section>

  <section aria-labelledby="home-accounts">
    <div class="sh">
      <h2 id="home-accounts">Accounts</h2>
      {#if strip === "gauges" && app.accounts}<span class="lbl">last read {age(app.accounts.age_s)}</span>{/if}
    </div>
    {#if strip === "no-secret"}
      <div class="off-panel"><Icon name="info" /><p>{NO_SECRET}{auto ? NO_SECRET_AUTO : ""}</p></div>
    {:else if strip === "unanswered"}
      <div class="off-panel"><Icon name="error" /><p>{accountsUnread(app.failed.accounts)}</p></div>
    {:else if strip === "unread"}
      <p class="lbl" role="status">Reading the quota…</p>
    {:else if strip === "failed"}
      <div class="off-panel"><Icon name="info" /><p>No quota read yet: the last read failed (above)</p></div>
    {:else if strip === "empty"}
      <div class="off-panel">
        <Icon name="accounts" /><p>No account yet</p>
        <span class="acts"><button type="button" class="btn-sm" onclick={() => ontab("accounts")}>Add an account</button></span>
      </div>
    {:else}
      <ul class="gstrip">
        {#each rows as a (a.auth_index)}
          {@const watched = !auto || a.in_scope}
          <li class="gmini" class:unwatched={!watched}>
            <span class="em" title={a.email_masked}>{a.email_masked}</span>
            <Gauge account={a} nowS={nowS()} threshold={auto && a.in_scope ? status.low_quota.threshold_percent : null} {watched}>
              {#snippet aside()}
                {#if a.state === "unknown" && watched}<span class="chip chip-unknown" title={a.error ?? undefined}><span class="dot"></span>unknown</span>
                {:else if !watched}<span class="chip chip-off"><span class="ring"></span>not watched</span>
                {:else if auto && a.state === "low"}<span class="chip chip-cutting"><Icon name="warning" size={16} />low</span>
                {:else if auto && a.state === "ok"}<span class="chip chip-ok"><Icon name="check" size={16} />ok</span>{/if}
              {/snippet}
            </Gauge>
            {#if resetLine(a)}<span class="rs">{resetLine(a)}</span>{/if}
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <section aria-labelledby="home-since">
    <div class="sh">
      <h2 id="home-since">Since start</h2>
      <span class="run"><span class="dot" class:dot-run={status.cliproxy.reachable} class:dot-down={!status.cliproxy.reachable}></span>CLIProxyAPI {status.cliproxy.reachable ? "running" : "not reachable"}</span>
    </div>
    <dl class="stats">
      <div class="stat"><dt>All requests</dt><dd class="big">{total}</dd></div>
      {#each ROUTES as [route, label, css] (route)}
        <div class="stat"><dt><i class={css}></i>{label}</dt><dd class="big">{runs[route] ?? 0}</dd></div>
      {/each}
    </dl>
  </section>

  <section aria-labelledby="home-24h">
    <div class="sh">
      <h2 id="home-24h">Last 24 h</h2><span class="lbl">requests per hour, by route</span>
      <span class="acts"><button type="button" class="btn-sm" onclick={() => ontab("activity")}>Open Activity</button></span>
    </div>
    {#if chart}<RequestsChart activity={chart} />{/if}
  </section>
{/if}
