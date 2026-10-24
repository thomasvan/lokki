<script lang="ts">
  import { app, type Tab } from "../lib/state.svelte";
  import { VERSION } from "../lib/version";
  import Icon, { type IconName } from "./Icon.svelte";
  import Mark from "./Mark.svelte";
  import Wordmark from "./Wordmark.svelte";

  let { tab, ontab }: { tab: Tab; ontab: (tab: Tab) => void } = $props();

  // The em the wordmark is set at: above the destinations' 13 px, where the drawing's plain dot is used.
  const BRAND_SIZE = 18;

  const DESTINATIONS: Array<[Tab, string, IconName]> = [
    ["home", "Home", "home"], ["accounts", "Accounts", "accounts"], ["activity", "Activity", "activity"], ["settings", "Settings", "settings"],
  ];
</script>

<!-- The sidebar at 720 px and wider; below, the row of tabs under the top bar, the foot hidden and the brand out of sight but still the page's h1. -->
<nav class="sidebar" aria-label="Destinations">
  <h1 class="brand"><Mark /><Wordmark size={BRAND_SIZE} /></h1>
  <div class="destinations">
    {#each DESTINATIONS as [id, name, icon] (id)}
      <button type="button" class="dest" aria-current={tab === id ? "page" : undefined} onclick={() => ontab(id)}>
        <Icon name={icon} /><span class="dest-label">{name}</span>
      </button>
    {/each}
  </div>
  <div class="side-foot">
    {#if app.status}
      <span class="side-state">
        <span class="dot" class:dot-run={app.status.cliproxy.reachable} class:dot-down={!app.status.cliproxy.reachable}></span>
        CLIProxyAPI {app.status.cliproxy.reachable ? "running" : "not reachable"}
      </span>
    {/if}
    <span class="cap">Lokki {VERSION}</span>
  </div>
</nav>
