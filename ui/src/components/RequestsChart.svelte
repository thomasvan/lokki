<script lang="ts" module>
  // The top of the value axis: the least of 4, 6, 10, 20, 40, 60, 100, … holding the tallest bar.
  // Always even, so the middle gridline is a whole number.
  export function niceTop(max: number): number {
    for (let scale = 1; ; scale *= 10) {
      for (const base of [4, 6, 10]) if (base * scale >= max) return base * scale;
    }
  }
</script>

<script lang="ts">
  import type { Activity } from "../lib/api";
  import { bucketLabel } from "../lib/format";

  // Requests over time as stacked bars, as many as the answer holds, labelled from its own window. The
  // routes stack (relay, pass-through, images); errors are counted inside them and marked above the bar.
  // The bars are drawn stretched to the box (the figures and labels are HTML), so the text keeps its size.
  let { activity }: { activity: Activity } = $props();

  // The plot's viewBox is 100 units high: the baseline at 100, the top gridline at 12, leaving room above for the error mark.
  const BASE = 100, TOP = 12, MARK = 4, SLOT = 10;
  const ROUTES = [
    { key: "relay", label: "Relay", css: "s-relay" },
    { key: "passthrough", label: "Pass-through", css: "s-pass" },
    { key: "images", label: "Images", css: "s-img" },
  ] as const;

  const n = $derived(activity.buckets.length);
  const top = $derived(niceTop(Math.max(0, ...activity.buckets.map((b) => b.relay + b.passthrough + b.images))));
  const unit = $derived((BASE - TOP) / top);
  const step = $derived(activity.window === "24h" ? "hour" : "6 hours");
  const span = $derived(activity.window === "24h" ? "24 hours" : "7 days");
  const grid = $derived([BASE, (BASE + TOP) / 2, TOP]);

  // Each bucket's segments from the baseline up, and where the stack ends.
  const bars = $derived(activity.buckets.map((b, k) => {
    let y = BASE;
    const segments = ROUTES.flatMap((r) => {
      const v = b[r.key];
      if (!v) return [];
      const h = v * unit;
      y -= h;
      return [{ css: r.css, label: r.label, v, y, h }];
    });
    return { k, b, segments, end: y, x: k * SLOT + SLOT * 0.19, w: SLOT * 0.62 };
  }));

  const legend = $derived([
    { label: "Relay", css: "s-relay", n: activity.totals.relay },
    { label: "Pass-through", css: "s-pass", n: activity.totals.passthrough },
    { label: "Images", css: "s-img", n: activity.totals.images },
    { label: "Errors, inside the bars", css: "s-err", n: activity.totals.errors },
  ]);
</script>

<div class="chart-card">
  <div class="chart-wrap">
    <div class="chart-y" aria-hidden="true">
      {#each [top, top / 2, 0] as v, i (i)}<span style:top="{grid[2 - i]}%">{v}</span>{/each}
    </div>
    <div class="chart-plot">
      <svg class="chart" viewBox="0 0 {n * SLOT} 100" preserveAspectRatio="none" role="img"
        aria-label="Requests per {step} over the last {span}, stacked by route">
        <g data-axes>
          {#each grid as y, i (i)}<line x1="0" x2={n * SLOT} y1={y} y2={y} />{/each}
        </g>
        {#each bars as bar (bar.k)}
          <g data-bucket={bar.k} data-errors={bar.b.errors || undefined}>
            {#each bar.segments as seg (seg.css)}
              <rect class={seg.css} x={bar.x} y={seg.y} width={bar.w} height={seg.h}><title>{seg.label}: {seg.v}</title></rect>
            {/each}
            {#if bar.b.errors}
              <rect class="s-err" x={bar.x} y={bar.end - MARK - 1} width={bar.w} height={MARK}><title>Errors: {bar.b.errors}</title></rect>
            {/if}
          </g>
        {/each}
      </svg>
      {#if activity.totals.requests === 0}<div class="chart-empty"><span>No requests yet</span></div>{/if}
    </div>
    <div class="chart-x" data-window={activity.window} aria-hidden="true">
      {#each activity.buckets as b, k (k)}
        <span>{#if bucketLabel(b.start, activity.window, k)}<i>{bucketLabel(b.start, activity.window, k)}</i>{/if}</span>
      {/each}
    </div>
  </div>
  <div class="legend">
    {#each legend as item (item.label)}
      <span><i class={item.css}></i><span>{item.label}</span> <b>{item.n}</b></span>
    {/each}
  </div>
</div>
