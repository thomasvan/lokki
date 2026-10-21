<script lang="ts">
  import { NOT_ANSWERING } from "../lib/api";
  import { headline } from "../lib/mode";
  import { app } from "../lib/state.svelte";
  import Icon from "./Icon.svelte";
  import ModeControl from "./ModeControl.svelte";
  import ModeSlider from "./ModeSlider.svelte";

  // On every screen: the three choices and the decision the core reports with its reason as given. On Home (full)
  // the bar is larger, and the mode slider sits under them at the bar's width.
  let { size }: { size: "full" | "compact" } = $props();

  // With no status there is no mode to show or to write: the bar says what it waits for instead of
  // the controls. The unreachable core has the UI's own sentence (api.ts), as the tray has (decision 10).
  const capital = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
  const waiting = $derived.by(() => {
    const e = app.error;
    if (!e) return { head: "Reading the core's status", icon: "spinner" as const, detail: null };
    if (e.kind === "network") return { head: capital(NOT_ANSWERING), icon: "error" as const, detail: null };
    if (e.kind === "auth" || e.kind === "forbidden") return { head: "Waiting for the operator token", icon: "lock" as const, detail: null };
    return { head: "The core did not give its status", icon: "error" as const, detail: e.message };
  });

  const status = $derived(app.status);
</script>

<header class="topbar {size}">
  {#if status}
    <div class="topbar-row">
      <ModeControl />
      <div class="decision" role="status">
        <span class="dot" class:dot-cutting={status.decision.relay_on} class:dot-passing={!status.decision.relay_on}></span>
        <span class="decision-head">{headline(status.decision.relay_on)}</span>
        <span class="decision-reason">{status.decision.reason}</span>
      </div>
    </div>
    {#if size === "full"}<ModeSlider />{/if}
  {:else}
    <div class="topbar-row">
      <div class="decision waiting" role="status">
        <Icon name={waiting.icon} size={16} />
        <span class="decision-head">{waiting.head}</span>
        {#if waiting.detail}<span class="decision-reason">{waiting.detail}</span>{/if}
      </div>
    </div>
  {/if}
</header>
