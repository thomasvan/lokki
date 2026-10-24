<script lang="ts" module>
  // The wordmark "Lokki" (spec 8): the letters are stroked centrelines in currentColor, the dot of the i is
  // drawn in --accent. `size` is the em the text wordmark would be set at; the drawing is 0.8 em tall.
  // The dot follows the mark's rule: the gull at 64 px and up, the gull's 16 px drawing from 28 px, a plain
  // dot below, where a gull of 2 px does not read.
  // This is its home; error.html carries a copy of the drawing (Wordmark.test.ts keeps the two equal).
  export type Dot = "gull" | "gull16" | "dot";
  export const dotFor = (size: number): Dot => (size >= 64 ? "gull" : size >= 28 ? "gull16" : "dot");
  const WIDTH: Record<Dot, number> = { gull: 212, gull16: 212, dot: 196 };
</script>

<script lang="ts">
  let { size = 18 }: { size?: number } = $props();
  const dot = $derived(dotFor(size));
  const width = $derived((size * 0.8 * WIDTH[dot]) / 80);
</script>

<svg class="wordmark" data-wordmark={dot} role="img" aria-label="Lokki" width={width} height={size * 0.8}
  viewBox="0 -6 {WIDTH[dot]} 80" style="overflow: visible">
  <path fill="none" stroke="currentColor" stroke-width="13" stroke-linecap="round" stroke-linejoin="round" d="M6.5 6.5V65.5H30M42.5 47a19.5 19.5 0 1 0 39 0a19.5 19.5 0 1 0 -39 0M99.5 6.5V65.5M99.5 58L124 28.5M110 45.4L127 65.5M143.5 6.5V65.5M143.5 58L168 28.5M154 45.4L171 65.5M188.5 28.5V65.5" />
  {#if dot === "gull"}
    <path style="fill: var(--accent)" transform="translate(188.5 16) scale(.78) translate(-32 -42)" d="M4 36C12 24 22 20 32 34C42 20 52 24 60 36C50 30 40 32 32 42C24 32 14 30 4 36Z" />
  {:else if dot === "gull16"}
    <path style="fill: var(--accent)" transform="translate(188.5 16.5) scale(2.7) translate(-8 -13)" d="M1 9.5C3 4.25 5.75 3.25 8 7.25C10.25 3.25 13 4.25 15 9.5C12.5 7.75 10.25 8.5 8 13C5.75 8.5 3.5 7.75 1 9.5Z" />
  {:else}
    <path style="fill: var(--accent)" d="M181 7.5a7.5 7.5 0 1 0 15 0a7.5 7.5 0 1 0 -15 0Z" />
  {/if}
</svg>
