<script lang="ts">
  import { onDestroy } from "svelte";
  import { keyToShow } from "../lib/api";
  import { UNWRITABLE, modeAt, modeLabel, position, positionText, positionWords, watchedCount } from "../lib/mode";
  import { app, choosePosition, dragPosition, dropHeld, errorText, holdPosition } from "../lib/state.svelte";
  import Icon from "./Icon.svelte";

  // The mode slider (spec 3.7, decision 21), one for Home's top bar and Settings › Mode: 0 is Direct, 100 is
  // Relay, a position between is Auto with that threshold. It shows what the core reports, unless a position is
  // held (stepped or dragged to, not yet chosen) or is the slider's own choice waiting or in flight. Every
  // write goes through the window's one mode write, so the slider is busy while any mode control writes.
  const id = $props.id();

  const reported = $derived(position(app.status));
  const w = $derived(app.modeWrite);
  // The slider's own choice: the one waiting, else the one in flight. A top-bar choice waiting behind the
  // slider's write leaves the slider on that write.
  const mine = $derived.by(() => {
    for (const c of [w.waiting, w.flight]) if (c && "position" in c) return c.position;
    return null;
  });
  const shown = $derived(w.held ?? mine ?? reported);
  const busy = $derived(w.flight !== null);
  // While the settings are unread the file's writability is unknown, so the slider stays usable and a
  // refused write says why.
  const locked = $derived(app.settings?.writable === false);
  const lowQuota = $derived(app.status?.low_quota);
  const watched = $derived(watchedCount(app.accounts));
  const failure = $derived(w.failure && "position" in w.failure.choice ? w.failure : null);

  // A pointer that went down on the slider and has not let go yet.
  let drag: number | null = null;
  // The drag's own pointer: a second touch neither starts another drag nor ends this one.
  const ownPointer = (event: PointerEvent) => drag !== null && event.pointerId === drag;

  const steps: Record<string, number> = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1, PageUp: 10, PageDown: -10 };
  // The keys are the component's, not the range's native step, whose page step differs between engines and which
  // fires `change` on every key.
  // A key with Alt, Ctrl or Meta is the browser's. A step during a drag moves the drag's position, which the
  // release writes.
  function keydown(event: KeyboardEvent) {
    if (shown === null || event.altKey || event.ctrlKey || event.metaKey) return;
    const to = event.key === "Home" ? 0 : event.key === "End" ? 100 : event.key in steps ? shown + steps[event.key] : null;
    if (to === null) return;
    event.preventDefault();
    const p = Math.max(0, Math.min(100, to));
    if (drag !== null) dragPosition(p);
    else holdPosition(p);
  }

  function pointerdown(event: PointerEvent) {
    if (drag !== null) return;
    drag = event.pointerId;
    // No pointer capture is taken: with one, the thumb did not follow a held move in Playwright's WebKit.
  }

  const valueOf = (event: Event) => Number((event.currentTarget as HTMLInputElement).value);

  // A drag moves the position; anything else that moves the range natively (assistive technology's
  // increment) counts as a key step.
  function input(event: Event) {
    if (drag !== null) dragPosition(valueOf(event));
    else holdPosition(valueOf(event));
  }

  // The pointer let go, through `change` or the drag's own `pointerup`, `lostpointercapture` (a touch's implicit
  // capture) or a move with no button down, in whichever order an engine fires them: the held position is chosen
  // once, by the first, and the others find nothing held. A press on the track is a drag of no length; assistive
  // technology's increment holds through `input` first.
  function release() {
    drag = null;
    if (w.held !== null) void choosePosition(w.held).catch(() => {});
  }

  // The drag's `pointerup` ends it even where no `change` or `lostpointercapture` follows. It is heard on the
  // window too, for the whole life of the slider, for a pointer let go outside the slider; with no drag down, or
  // from another pointer, it does nothing.
  function up(event: PointerEvent) {
    if (ownPointer(event)) release();
  }

  // A move with no button down is not part of the drag, so it ends it: the release was missed, and the thumb stays
  // where the drag last put it instead of following the pointer. A touch or pen in contact reports a button down.
  function move(event: PointerEvent) {
    if (ownPointer(event) && event.buttons === 0) release();
  }

  function lostcapture(event: PointerEvent) {
    if (ownPointer(event)) release();
  }

  // A slider that goes away mid-drag leaves nothing held for the next choice to send.
  onDestroy(() => { if (drag !== null) dropHeld(); });

  // A drag the browser cancelled writes nothing and goes back to what the core reports.
  function cancel(event: PointerEvent) {
    if (!ownPointer(event)) return;
    drag = null;
    dropHeld();
  }

  // Leaving the slider chooses a position its keys stepped to at once, as its timer would.
  function blur() {
    if (drag === null && w.held !== null) void choosePosition(w.held).catch(() => {});
  }

  const describedBy = $derived([locked ? `${id}-locked` : "", failure ? `${id}-failure` : ""].filter(Boolean).join(" ") || undefined);
</script>

<svelte:window onpointerup={up} onpointermove={move} />

<div class="slider">
  <div class="slider-head">
    <span class="lbl" id="{id}-name">Mode and threshold</span>
    <span class="slider-read" data-readout>
      {#if shown !== null}
        {#if mine !== null && w.held === null}<Icon name="spinner" size={16} />{/if}
        {modeLabel(modeAt(shown), shown)}
      {/if}
    </span>
  </div>
  <input class="range" type="range" min="0" max="100" step="1" aria-labelledby="{id}-name" aria-describedby={describedBy}
    value={shown ?? 0} style:--p={shown ?? 0} data-empty={shown === null ? "" : undefined}
    disabled={shown === null || locked} aria-busy={busy ? "true" : "false"}
    aria-valuetext={shown !== null && lowQuota ? positionText(shown, lowQuota, watched) : undefined}
    onkeydown={keydown} onpointerdown={pointerdown} onpointerup={up} oninput={input} onchange={release}
    onlostpointercapture={lostcapture} onpointercancel={cancel} onblur={blur} />
  <div class="slider-scale" aria-hidden="true"><span>Direct · 0 %</span><span>% left</span><span>100 % · Relay</span></div>
  <p class="slider-words" data-rule={shown !== null && modeAt(shown) === "auto" ? "" : undefined}>
    {#if shown !== null && lowQuota}{positionWords(shown, lowQuota, watched)}{/if}
  </p>
  {#if locked}<p class="mode-why" id="{id}-locked"><Icon name="lock" size={16} />{UNWRITABLE}</p>{/if}
  {#if failure}
    {@const message = failure.read ? `written, but the mode could not be read back: ${errorText(failure.error)}` : failure.error.message}
    {@const key = failure.read ? undefined : keyToShow(message, failure.error.key)}
    <p class="slider-refused" id="{id}-failure" role="alert"><Icon name="error" size={16} /><span>{key ? `${message} ` : message}{#if key}<code>{key}</code>{/if}</span></p>
  {/if}
</div>
