<script lang="ts">
  import { count } from "../lib/format";

  // Horizontal bars, one row per name with its count. A name is one line cut with an ellipsis; its whole
  // text is in `title` and in the bar's accessible name. `tone` is a state token's name (cutting, passing, accent).
  let { rows, label, tone, mono = false, wide = false }: {
    rows: Array<{ name: string; n: number }>; label: string; tone: "cutting" | "passing" | "accent"; mono?: boolean; wide?: boolean;
  } = $props();

  const top = $derived(Math.max(1, ...rows.map((r) => r.n)));
</script>

{#if rows.length === 0}
  <p class="lbl">none in this window</p>
{:else}
<ul class="hb-list" aria-label={label}>
  {#each rows as row (row.name)}
    <li class="hb" class:wide>
      <span class="k trunc" class:mono title={row.name}>{row.name}</span>
      <span class="t" role="img" aria-label="{row.name}: {row.n} {row.n === 1 ? 'run' : 'runs'}">
        <span class="f" style:width="{(100 * row.n) / top}%" style:background="var(--{tone})"></span>
      </span>
      <span class="n">{count(row.n)}</span>
    </li>
  {/each}
</ul>
{/if}
