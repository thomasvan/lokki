<script lang="ts" module>
  // The icon set of spec 7, drawn on a 20 px grid with a 1.5 px stroke and round caps in
  // currentColor; the drawings are those of docs/design/ux/system.html. A name without a
  // drawing fails the type check.
  export type IconName =
    | "home" | "accounts" | "activity" | "settings" | "copy" | "refresh" | "external" | "info" | "check"
    | "warning" | "error" | "chevron" | "plus" | "close" | "restart" | "lock" | "spinner";

  type Shape =
    | { d: string }
    | { cx: number; cy: number; r: number; dash?: string }
    | { x: number; y: number; width: number; height: number; rx: number };

  const DRAWINGS: Record<IconName, Shape[]> = {
    home: [{ d: "M3.25 9.25 10 3.5l6.75 5.75" }, { d: "M5 8v8.25h3.5V12h3v4.25H15V8" }],
    accounts: [{ cx: 8, cy: 7, r: 3 }, { d: "M2.75 16.25c.55-2.85 2.65-4.5 5.25-4.5s4.7 1.65 5.25 4.5" },
      { d: "M12.75 4.15a3 3 0 0 1 0 5.7" }, { d: "M14.75 11.9c1.4.6 2.3 2.1 2.5 4.35" }],
    activity: [{ d: "M4.75 16.25V11M8.25 16.25V6M11.75 16.25V9M15.25 16.25V3.75" }],
    settings: [{ d: "M3.25 6H9m4 0h3.75" }, { cx: 11, cy: 6, r: 2 }, { d: "M3.25 14H7m4 0h5.75" }, { cx: 9, cy: 14, r: 2 }],
    copy: [{ x: 7, y: 7, width: 9.25, height: 9.25, rx: 1.75 },
      { d: "M13 7V5a1.25 1.25 0 0 0-1.25-1.25H5A1.25 1.25 0 0 0 3.75 5v6.75A1.25 1.25 0 0 0 5 13h2" }],
    refresh: [{ d: "M4 8.5a6.25 6.25 0 0 1 11.6-1.25" }, { d: "M15.75 3.75V7.5H12" }, { d: "M16 11.5a6.25 6.25 0 0 1-11.6 1.25" },
      { d: "M4.25 16.25V12.5H8" }],
    external: [{ d: "M11 3.75h5.25V9" }, { d: "M16.25 3.75 9.5 10.5" },
      { d: "M14 11.75v3.5a1 1 0 0 1-1 1H4.75a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h3.5" }],
    info: [{ cx: 10, cy: 10, r: 7.25 }, { d: "M10 9.25V14" }, { d: "M10 6.5v.01" }],
    check: [{ d: "m4.5 10.5 3.75 3.75L15.5 6" }],
    warning: [{ d: "M10 3.25 17.25 16H2.75Z" }, { d: "M10 8.25v3.5" }, { d: "M10 13.75v.01" }],
    error: [{ cx: 10, cy: 10, r: 7.25 }, { d: "m7.5 7.5 5 5m0-5-5 5" }],
    chevron: [{ d: "m8 5 5 5-5 5" }],
    plus: [{ d: "M10 4.5v11M4.5 10h11" }],
    close: [{ d: "m5.5 5.5 9 9m0-9-9 9" }],
    restart: [{ d: "M15.75 10a5.75 5.75 0 1 1-2.55-4.78" }, { d: "M13.5 2.25V5.5h-3.25" }, { d: "M8.75 7.75 12 10l-3.25 2.25Z" }],
    lock: [{ x: 4.75, y: 9, width: 10.5, height: 7.5, rx: 1.5 }, { d: "M7 9V6.75a3 3 0 0 1 6 0V9" }],
    spinner: [{ cx: 10, cy: 10, r: 6.5, dash: "28 13" }],
  };
</script>

<script lang="ts">
  let { name, size = 20 }: { name: IconName; size?: 16 | 20 } = $props();
</script>

<svg class="ic" class:spin={name === "spinner"} data-icon={name} width={size} height={size} viewBox="0 0 20 20" aria-hidden="true">
  {#each DRAWINGS[name] as shape, i (i)}
    {#if "d" in shape}
      <path d={shape.d} />
    {:else if "r" in shape}
      <circle cx={shape.cx} cy={shape.cy} r={shape.r} stroke-dasharray={shape.dash} />
    {:else}
      <rect x={shape.x} y={shape.y} width={shape.width} height={shape.height} rx={shape.rx} />
    {/if}
  {/each}
</svg>
