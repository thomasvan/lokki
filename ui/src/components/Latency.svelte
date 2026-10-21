<script lang="ts">
  import type { Percentiles } from "../lib/api";
  import { seconds } from "../lib/format";
  import { niceTop } from "./RequestsChart.svelte";

  // Time from start to last byte per route: a solid bar to the median (p50) inside an outline to p90.
  // A route with no run has no bar and says so.
  let { rows }: { rows: Array<{ name: string; tone: "cutting" | "passing" | "accent" | "images"; at: Percentiles }> } = $props();

  const top = $derived(niceTop(Math.max(0, ...rows.map((r) => r.at.p90 ?? 0))));
  const ticks = $derived([0, top / 2, top]);
  const any = $derived(rows.some((r) => r.at.p90 !== null));
</script>

<div class="lat">
  {#each rows as row (row.name)}
    {@const p50 = row.at.p50}
    {@const p90 = row.at.p90}
    <div class="lr">
      <span class="k"><i style:background="var(--{row.tone})"></i>{row.name}</span>
      {#if p50 !== null && p90 !== null}
        <span class="lt" role="img" aria-label="{row.name}: median {seconds(p50)}, 90th percentile {seconds(p90)}">
          <span class="p90" style:width="{(100 * p90) / top}%" style:box-shadow="inset 0 0 0 1px var(--{row.tone})"></span>
          <span class="p50" style:width="{(100 * p50) / top}%" style:background="var(--{row.tone})"></span>
        </span>
        <span class="lv"><span>p50 <b>{seconds(p50)}</b></span><span>p90 <b>{seconds(p90)}</b></span></span>
      {:else}
        <span class="lt none" role="img" aria-label="{row.name}: no runs"></span>
        <span class="lv">no runs</span>
      {/if}
    </div>
  {/each}
  {#if any}
    <div class="lr" aria-hidden="true">
      <span></span>
      <span class="lax">{#each ticks as t, i (i)}<span style:left="{(100 * t) / top}%">{t} s</span>{/each}</span>
    </div>
    <div class="lkey"><span><i class="solid"></i>solid to the median (p50)</span><span><i class="outline"></i>outline to p90</span></div>
  {/if}
</div>
