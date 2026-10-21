<script lang="ts">
  import { app, checkNow, errorText, installUpdate } from "../lib/state.svelte";
  import { offered, updateWords } from "../lib/update";
  const latest = "https://github.com/thomasvan/lokki/releases/latest";

  // The banner renders only once the shell has answered, and the shell injects every hook.
  // A rejected open is shown beside the buttons, as Settings shows the panel hooks'.
  let openError = $state("");
  async function open(url: string): Promise<void> {
    openError = "";
    try {
      await window.lokki!.openExternal!(url);
    } catch (e) {
      openError = `could not open it: ${errorText(e)}`;
    }
  }
</script>

{#if app.update}
  {@const u = app.update}
  {@const w = updateWords(u)}
  <aside class="update" role="status">
    {#if offered(u)}
      <strong>{w.line}</strong>
      <button onclick={() => open(u.notes_url ?? latest)}>Release notes</button>
      {#if u.can_install}
        <button onclick={installUpdate}>Install and restart</button>
      {:else}
        <button onclick={() => open(u.notes_url ?? latest)}>Download</button>
        {#if w.reason}<span>{w.reason}</span>{/if}
      {/if}
      <button onclick={checkNow}>Check now</button>
    {:else if u.state === "checking" || u.state === "downloading" || u.state === "installing"}
      <span>{w.line}</span>
    {:else}
      <span>{w.line}</span>
      {#if w.reason}<span>{w.reason}</span>{/if}
      <button onclick={checkNow}>Check now</button>
    {/if}
    {#if w.error}<span class="error">{w.error}</span>{/if}
    {#if openError}<span role="alert">{openError}</span>{/if}
  </aside>
{/if}
