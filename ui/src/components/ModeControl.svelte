<script lang="ts">
  import { keyToShow } from "../lib/api";
  import { MODES, UNWRITABLE, modeLabel, type Routing } from "../lib/mode";
  import { app, chooseMode } from "../lib/state.svelte";
  import Icon from "./Icon.svelte";

  // The top bar's three one-click choices, each mode's sentence the radio's description, shown on hover or focus.
  // The top bar draws it only once a status is read.
  const id = $props.id();

  // The checked mode is what the core last reported, never the click. Every choice goes through the window's one
  // mode write: while any mode control writes the group is busy, and a choice made meanwhile waits, marked
  // pending, and is sent after it (spec 3.7).
  const checked = $derived(app.status?.routing ?? null);
  const threshold = $derived(app.status?.low_quota.threshold_percent ?? 0);
  // While the settings are unread the file's writability is unknown, so the control stays
  // usable and a refused write says why (decision 9).
  const locked = $derived(app.settings?.writable === false);
  const busy = $derived(app.modeWrite.flight !== null);
  const pending = (mode: Routing) => [app.modeWrite.flight, app.modeWrite.waiting].some((c) => c !== null && "mode" in c && c.mode === mode);
  // A refusal of this control's choice, kept until the next mode write returns; any other failure is the shell's.
  const refused = $derived.by(() => {
    const f = app.modeWrite.failure;
    return f && "mode" in f.choice && !f.read && f.error.kind === "refused" ? { message: f.error.message, key: f.error.key } : null;
  });

  function choose(mode: Routing) {
    void chooseMode(mode).catch(() => {});
  }

  // Arrows move the focus; Space or Enter chooses, since every choice is a write.
  const radios: HTMLButtonElement[] = [];
  function move(event: KeyboardEvent) {
    const steps: Record<string, number> = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
    const at = radios.indexOf(document.activeElement as HTMLButtonElement);
    if (at < 0) return;
    let next: number;
    if (event.key in steps) next = (at + steps[event.key] + radios.length) % radios.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = radios.length - 1;
    else return;
    event.preventDefault();
    radios[next].focus();
  }
  const tabStop = (mode: Routing, i: number) => (checked === null ? i === 0 : checked === mode) ? 0 : -1;
  const describedBy = (mode: Routing) => `${id}-${mode}` + (locked ? ` ${id}-locked` : "");
</script>

<div class="mode-control">
  <div class="mode" role="radiogroup" aria-label="Mode" aria-busy={busy ? "true" : "false"} tabindex="-1" onkeydown={move}>
    {#each MODES as m, i (m.mode)}
      <span class="seg">
        <button bind:this={radios[i]} type="button" role="radio" aria-checked={checked === m.mode ? "true" : "false"}
          tabindex={tabStop(m.mode, i)} disabled={locked} aria-describedby={describedBy(m.mode)} onclick={() => choose(m.mode)}>
          {#if checked === m.mode}<Icon name="check" size={16} />{/if}
          {modeLabel(m.mode, threshold)}
          {#if pending(m.mode)}<Icon name="spinner" size={16} />{/if}
        </button>
        <span class="tip" id="{id}-{m.mode}">{m.sentence}</span>
      </span>
    {/each}
  </div>
  {#if locked}<p class="mode-why" id="{id}-locked"><Icon name="lock" size={16} />{UNWRITABLE}</p>{/if}
  {#if refused}{@const key = keyToShow(refused.message, refused.key)}<p class="mode-refused" role="alert">{key ? `${refused.message} ` : refused.message}{#if key}<code>{key}</code>{/if}</p>{/if}
</div>
