<script lang="ts">
  // An on/off setting. `checked` is what the caller passes in: for a switch that takes effect when
  // clicked, what the core last reported, never the click (the caller writes, reads back, and passes
  // the answer in); for a field of a form that saves later, the draft. `busy` marks a write in flight
  // and leaves the switch enabled, so a focused switch keeps the focus; the caller ignores a click that
  // lands meanwhile. `describedby` names the elements that describe it (its meaning, why it is locked).
  let { checked, label, name, disabled = false, busy = false, title, describedby, onchange }: {
    checked: boolean; label: string; name?: string; disabled?: boolean; busy?: boolean;
    title?: string; describedby?: string; onchange: (next: boolean) => void;
  } = $props();
</script>

<button type="button" class="switch" role="switch" aria-checked={checked} aria-label={name} aria-busy={busy ? "true" : undefined}
  aria-describedby={describedby} {title} {disabled} onclick={() => onchange(!checked)}>
  <span class="trk"></span><span aria-hidden={name ? "true" : undefined}>{label}</span>
</button>
