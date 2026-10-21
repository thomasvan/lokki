<script lang="ts">
  import type { Snippet } from "svelte";
  import type { Account } from "../lib/api";
  import { gaugeOf } from "../lib/gauge";
  import { percent } from "../lib/format";

  // A thin pill of the quota left. In Auto a watched account's gauge has a tick at the threshold: "low" is the
  // fill falling below the tick. With no threshold (an account Auto does not watch, or Relay and Direct, where
  // the threshold decides nothing) there is no tick and no low tone; an account Auto does not watch is drawn
  // in the off tone. A window that has reset reads full (gauge.ts). `aside` sits opposite the figure, as the
  // chip does.
  let { account, nowS, threshold = null, watched = true, aside }: {
    account: Account; nowS: number; threshold?: number | null; watched?: boolean; aside?: Snippet;
  } = $props();

  const reading = $derived(gaugeOf(account, nowS));
  const tone = $derived(!watched ? "off" : threshold !== null && account.state === "low" ? "low" : "ok");
  // Named by the account, so several meters on a page can be told apart.
  const label = $derived(`${account.email_masked}: ` + (reading.percent === null ? "no reading"
    : `${percent(reading.percent)} remaining${reading.windowReset ? ", window reset" : ""}${threshold !== null ? `, low below ${percent(threshold)}` : ""}`));
</script>

<div class="gauge">
  <div class="gauge-top">
    {#if reading.percent === null}
      <span class="gauge-figure none">no reading</span>
    {:else}
      <span class="gauge-figure">{percent(reading.percent)}</span>
    {/if}
    {#if reading.windowReset}<span class="gauge-note">window reset</span>{/if}
    {#if aside}<span class="gauge-aside">{@render aside()}</span>{/if}
  </div>
  <div class="bar" class:noread={reading.percent === null} role="meter" aria-label={label}
    aria-valuemin={reading.percent === null ? undefined : 0} aria-valuemax={reading.percent === null ? undefined : 100}
    aria-valuenow={reading.percent === null ? undefined : reading.percent}>
    {#if reading.percent !== null}<span class="fill tone-{tone}" style:width="{reading.percent}%"></span>{/if}
    {#if threshold !== null}<span class="tick" data-tick style:left="{threshold}%"></span>{/if}
  </div>
</div>
