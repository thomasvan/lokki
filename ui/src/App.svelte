<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { token } from "./lib/api";
  import { app, errorText, loadSchema, loadSettings, loadStatus, startPolling, stopPolling, type Tab } from "./lib/state.svelte";
  import Icon from "./components/Icon.svelte";
  import Nav from "./components/Nav.svelte";
  import TopBar from "./components/TopBar.svelte";
  import Accounts from "./views/Accounts.svelte";
  import Activity from "./views/Activity.svelte";
  import Home from "./views/Home.svelte";
  import Settings from "./views/Settings.svelte";
  import Update from "./views/Update.svelte";

  let tab = $state<Tab>("home");
  let typed = $state("");
  let checking = $state(false);

  // Entering a tab reads what its view shows: the polling covers the status and, per
  // tab, the endpoint and the accounts; Settings needs the schema and the settings, and a
  // re-entry reads them again. Any screen reads the settings while they are unread, since
  // the mode control in the top bar is disabled by `writable: false`, which the status does
  // not carry.
  function enter(next: Tab) {
    tab = next;
    startPolling(next);
    if (app.settings === null || next === "settings") void loadSettings();
    if (next === "settings") void loadSchema();
  }

  onMount(() => enter("home"));
  onDestroy(stopPolling);

  const asksForToken = $derived(app.error?.kind === "auth" || app.error?.kind === "forbidden");

  // Use and Retry: one read, with the typed token for Use; the tab is entered again only if it answered.
  // True when it did.
  async function again(candidate?: string): Promise<boolean> {
    checking = true;
    try {
      await loadStatus(candidate);
      if (app.error) return false;
      if (candidate !== undefined) token.set(candidate);       // stored once the core has accepted it
      enter(tab);
      return true;
    } finally {
      checking = false;
    }
  }

  async function useToken(event: SubmitEvent) {
    event.preventDefault();
    if (await again(typed.trim())) typed = "";          // a refused token stays in the box to be corrected
  }

  let tokenBox = $state<HTMLInputElement>();
  $effect(() => { if (asksForToken) tokenBox?.focus(); });
</script>

<div class="window">
  <Update />

  <div class="shell" inert={asksForToken}>
    <Nav {tab} ontab={enter} />
    <TopBar size={tab === "home" ? "full" : "compact"} />
    <main class="content">
      {#if app.error && !asksForToken}
        <div class="banner banner-error" role="alert">
          <Icon name="error" />
          <p>{errorText(app.error)}</p>
          {#if app.error.kind === "network" || app.error.kind === "other"}
            <button type="button" class="btn-sm" disabled={checking} onclick={() => again()}>{checking ? "Checking…" : "Retry"}</button>
          {/if}
        </div>
      {/if}
      {#if tab === "home"}
        <Home ontab={enter} />
      {:else if tab === "accounts"}
        <Accounts />
      {:else if tab === "activity"}
        <Activity ontab={enter} />
      {:else}
        <Settings ontab={enter} />
      {/if}
    </main>
  </div>

  {#if asksForToken}
    <!-- Over the page, not instead of it: the views stay mounted, so a 401 mid-session keeps unsaved edits. -->
    <div class="scrim">
      <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="token-title">
        <form onsubmit={useToken}>
          <h2 id="token-title">The core needs its operator token</h2>
          {#if app.error}<p class="dialog-error" role="alert">{errorText(app.error)}</p>{/if}
          <label>Operator token <input type="password" bind:this={tokenBox} bind:value={typed} autocomplete="off" aria-describedby="token-why" /></label>
          <p id="token-why">paste the operator token the core was started with</p>
          <button type="submit" class="btn-primary" disabled={checking || typed.trim() === ""} aria-describedby={checking ? undefined : "token-why"}>{checking ? "Checking…" : "Use"}</button>
        </form>
      </div>
    </div>
  {/if}
</div>
