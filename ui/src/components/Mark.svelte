<script lang="ts">
  // The seagull mark (spec 8): one filled path on a 64-unit grid, drawn in currentColor.
  // This is its home; the prototypes in docs/design/ux/ carry frozen copies.
  let { size = 26 }: { size?: number } = $props();
</script>

<svg class="mark" data-mark width={size} height={size} viewBox="0 0 64 64" aria-hidden="true">
  <path fill="currentColor" d="M4 36C12 24 22 20 32 34C42 20 52 24 60 36C50 30 40 32 32 42C24 32 14 30 4 36Z" />
</svg>
