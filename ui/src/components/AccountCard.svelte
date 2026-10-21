<script lang="ts">
  import type { Account } from "../lib/api";
  import { clock, resetsIn } from "../lib/format";
  import { gaugeOf } from "../lib/gauge";
  import Gauge from "./Gauge.svelte";
  import Icon from "./Icon.svelte";
  import Switch from "./Switch.svelte";

  // One account (spec 4.2). `watched` is the scope the screen holds for it; the switch is `live`
  // only under "Only the ones I pick", and says why it is not otherwise. Out of Auto (`auto` false) the
  // threshold decides nothing: no tick, no low or ok chip and no switch, and an account that could not be read
  // keeps unknown with its error (decision 13).
  let { account, nowS, threshold, auto = true, watched, live, busy = false, onwatch }: {
    account: Account; nowS: number; threshold: number; auto?: boolean; watched: boolean; live: boolean; busy?: boolean;
    onwatch: (index: string) => void;
  } = $props();

  const reading = $derived(gaugeOf(account, nowS));
  const resetAt = $derived(account.reset_at !== null && account.reset_at > 0 ? account.reset_at : null);
  const note = $derived(!account.enabled ? "disabled in CLIProxyAPI, so not watched" : live ? "" : "set by the scope above");
</script>

<div class="acard" role="group" aria-label={account.email_masked} class:off={!account.enabled} data-key={account.auth_index}
  aria-disabled={account.enabled ? undefined : "true"}>
  <div class="ah">
    <h3 title={account.email_masked}>{account.email_masked}</h3>
    {#if !auto}{#if account.state === "unknown"}<span class="chip chip-unknown"><span class="dot"></span>unknown</span>{/if}
    {:else if !account.in_scope}<span class="chip chip-off"><span class="ring"></span>not watched</span>
    {:else if account.state === "low"}<span class="chip chip-cutting"><Icon name="warning" size={16} />low</span>
    {:else if account.state === "ok"}<span class="chip chip-ok"><Icon name="check" size={16} />ok</span>
    {:else}<span class="chip chip-unknown"><span class="dot"></span>unknown</span>{/if}
  </div>
  <Gauge {account} {nowS} threshold={auto ? threshold : null} watched={!auto || account.in_scope} />
  <p class="under">
    {#if reading.windowReset}reset; the next read shows the new window
    {:else if resetAt !== null && resetAt > nowS}resets in {resetsIn(resetAt - nowS)} · {clock(resetAt)}
    {:else}reset time not read yet{/if}
  </p>
  {#if account.error && (account.in_scope || !auto)}<p class="aerr"><Icon name="error" size={16} /><span>The last read failed: <code>{account.error}</code></span></p>{/if}
  {#if auto}<div class="af">
    <Switch checked={watched} label="Watched" name="Watched: {account.email_masked}" disabled={!live} {busy}
      title={live ? undefined : "the switches apply once you pick accounts"} onchange={() => onwatch(account.auth_index)} />
    {#if note}<span class="lbl">{note}</span>{/if}
  </div>{/if}
</div>
