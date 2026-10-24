<script lang="ts">
  import { onDestroy } from "svelte";
  import { age } from "../lib/format";
  import { useNow } from "../lib/now.svelte";
  import { NO_SECRET, UNWATCHED_MODES } from "../lib/mode";
  import {
    accountsUnread, app, cancelOauth, canCopy, errorText, loadAccounts, noSecretFile, oauthActive, refreshAccounts, saveSettings, sendCallback, startOauth,
  } from "../lib/state.svelte";
  import AccountCard from "../components/AccountCard.svelte";
  import Icon from "../components/Icon.svelte";

  const scope = $derived(app.status?.low_quota.accounts ?? []);
  // Auto watches accounts; in Relay and Direct the threshold and the scope decide nothing (decision 13).
  const auto = $derived(app.status?.routing === "auto");
  const threshold = $derived(app.status?.low_quota.threshold_percent ?? 0);
  const rows = $derived(app.accounts?.accounts ?? []);
  const noSecret = $derived(noSecretFile());
  const read = $derived(app.accounts !== null && app.accounts.read_at !== null);
  // Entries of the scope that no row of the last read carries; before the first
  // read every entry would look missing.
  const missing = $derived(read ? scope.filter((i) => !rows.some((a) => a.auth_index === i)) : []);
  // "Only the ones I pick" with nothing picked is the same list as "Every enabled account", so the
  // choice is local until a switch is on.
  let chosen = $state<"every" | "only" | null>(null);
  const picking = $derived((chosen ?? (scope.length > 0 ? "only" : "every")) === "only");
  let saving = $state(false);
  let failure = $state("");

  // A refused write (400, 409) is shown beside the controls; any other failure is the shell's.
  // The controls stay enabled while a write runs, so a focused switch keeps the focus; a click that
  // lands meanwhile writes nothing.
  async function write(patch: Record<string, unknown>, after?: () => void) {
    if (saving) return;
    saving = true;
    failure = "";
    try {
      await saveSettings(patch);
      await loadAccounts();
      after?.();
    } catch (e) {
      failure = errorText(e) + (e && typeof e === "object" && "key" in e && e.key ? ` (${String(e.key)})` : "");
    } finally {
      saving = false;
    }
  }

  const writeScope = (accounts: string[]) => write({ low_quota: { accounts } }, () => { chosen = null; });
  const toggle = (index: string) => writeScope(scope.includes(index) ? scope.filter((i) => i !== index) : [...scope, index]);

  function chooseEvery() {
    if (scope.length > 0) void writeScope([]);
    else chosen = null;
  }

  let refreshFailure = $state("");
  async function refresh() {
    refreshFailure = "";
    try {
      await refreshAccounts();
    } catch (e) {
      refreshFailure = `could not refresh: ${errorText(e)}`;
    }
  }

  // The login. A rejected hook is shown beside the button; the flow itself goes on. True when the call went
  // through; a fallback answers for itself.
  let hookError = $state("");
  async function hook(call: (() => Promise<void>) | undefined, fallback?: () => boolean): Promise<boolean> {
    hookError = "";
    try {
      if (call) { await call(); return true; }
      return fallback?.() ?? true;
    } catch (e) {
      hookError = `could not do that: ${errorText(e)}`;
      return false;
    }
  }

  // The card says the login page opened only when the shell's openExternal resolved or window.open returned a
  // window (a blocked popup returns none, and says nothing).
  let opened = $state(false);
  async function addAccount() {
    hookError = "";
    opened = false;
    const url = await startOauth();
    if (url === null) return;
    const open = window.lokki?.openExternal;
    // "noopener" makes window.open return null even when the tab opens, so the opened page is cut off by hand.
    const did = await hook(open && (() => open(url)), () => {
      const tab = window.open(url, "_blank");
      if (!tab) return false;
      tab.opener = null;
      return true;
    });
    opened = did && app.oauth?.url === url;
  }

  const COPIED_MS = 3_000;
  let copied = $state(false);
  let copiedTimer: ReturnType<typeof setTimeout> | undefined;
  onDestroy(() => clearTimeout(copiedTimer));
  async function copyLink(url: string) {
    await hook(() => navigator.clipboard.writeText(url));
    if (hookError) return;
    copied = true;
    clearTimeout(copiedTimer);
    copiedTimer = setTimeout(() => { copied = false; }, COPIED_MS);
  }

  const oauth = $derived(app.oauth);

  // The address pasted from another device lives here and nowhere else (spec 4.5): not in app.oauth, not in
  // storage, not in a URL or a log. It goes with the login it was pasted for, whether that ends by Cancel, an
  // answer or a failure.
  let pasted = $state("");
  let pasteFailure = $state("");
  let sent = $state(false);
  let sending = $state(false);
  const login = $derived(oauth?.status === "pending" ? oauth.state : "");
  $effect(() => {
    void login;
    pasted = "";
    pasteFailure = "";
    sent = false;
  });

  // Send stays enabled while it runs, so a focused button keeps the focus; a press meanwhile sends nothing.
  async function sendPasted() {
    if (sending) return;
    sent = false;
    const address = pasted.trim();
    if (address === "") { pasteFailure = "paste the address first"; return; }
    pasteFailure = "";
    const state = login;
    sending = true;
    try {
      await sendCallback(address);
      if (state !== login) return;
      pasted = "";
      sent = true;
    } catch (e) {
      if (state === login) pasteFailure = errorText(e);
    } finally {
      sending = false;
    }
  }
  // The empty card carries the one "Add" button when there is nothing else to show.
  const none = $derived(read && rows.length === 0 && oauth === null);
  // The countdown and a passed reset follow the clock, not only the reads: the accounts come every
  // 30 s, and a reset can fall between two of them.
  const clock = useNow(() => { void app.accounts; });
</script>

<div class="phead">
  <h2>Accounts</h2>
  {#if app.status && read && app.accounts}<span class="lbl">last read {age(app.accounts.age_s)}</span>{/if}
  <button type="button" class="btn-sm" disabled={app.busy || !app.status} aria-busy={app.busy ? "true" : undefined} aria-describedby={app.status ? undefined : "refresh-why"} onclick={refresh}>
    <Icon name={app.busy ? "spinner" : "refresh"} size={16} />{app.busy ? "Reading…" : "Read now"}
  </button>
  {#if !none}
    <span class="addbox">
      <button type="button" class="btn-primary" disabled={oauthActive()} aria-busy={oauthActive() ? "true" : undefined} onclick={addAccount}>
        <Icon name={oauthActive() ? "spinner" : "plus"} size={16} />Add a Codex account
      </button>
      <span class="lbl">Opens the provider’s login page in your browser</span>
    </span>
  {/if}
</div>
{#if refreshFailure}<p role="alert">{refreshFailure}</p>{/if}
{#if hookError}<p role="alert">{hookError}</p>{/if}

{#if oauth?.status === "starting"}
  <div class="card login" role="status"><Icon name="spinner" /><p>Starting the login…</p></div>
{:else if oauth?.status === "pending"}
  <div class="card login" aria-live="polite">
    <div class="lg-top">
      <Icon name="spinner" />
      <div>
        <h3>Finish the login in a browser</h3>
        <p class="lbl">
          {#if opened}<span>Lokki opened the login page in this computer’s browser.</span>{/if}
          <span>Lokki waits up to 5 minutes.</span>
        </p>
      </div>
    </div>
    <div class="lg-fb">
      <span class="lbl">Logging in from another device? Open this link there:</span>
      <div class="row">
        <code class="linkbox">{oauth.url}</code>
        <button type="button" class="btn-sm" disabled={!canCopy()} onclick={() => copyLink(oauth.url)}><Icon name={copied ? "check" : "copy"} size={16} />{copied ? "Copied" : "Copy login link"}</button>
        <button type="button" class="btn-sm" onclick={cancelOauth}>Cancel</button>
      </div>
    </div>
    <div class="lg-paste">
      <label class="lbl" for="paste-address">When the login ends, that browser shows a page that does not load, at an address that starts <code>http://localhost:1455/auth/callback</code>. Paste that address here:</label>
      <div class="row">
        <input id="paste-address" class="pastebox" type="text" autocomplete="off" spellcheck="false" inputmode="url"
          placeholder="http://localhost:1455/auth/callback?code=…&state=…" bind:value={pasted}
          aria-invalid={pasteFailure ? "true" : undefined} aria-describedby={pasteFailure ? "paste-failure" : undefined}
          onkeydown={(e) => { if (e.key === "Enter") void sendPasted(); }} />
        <button type="button" class="btn-primary btn-sm" aria-busy={sending ? "true" : undefined} onclick={sendPasted}>
          {#if sending}<Icon name="spinner" size={16} />{/if}Send
        </button>
      </div>
      {#if pasteFailure}<p class="fail" role="alert" id="paste-failure"><Icon name="error" size={16} />{pasteFailure}</p>{/if}
      {#if sent}<p class="sent" role="status"><Icon name="spinner" size={16} />Sent; finishing the login…</p>{/if}
    </div>
  </div>
{:else if oauth?.status === "ok"}
  <div class="card login" role="status">
    <div class="lg-top">
      <span class="ok-ic"><Icon name="check" size={16} /></span>
      <div>
        <h3>Account added</h3>
      </div>
      <span class="acts">
        <button type="button" class="btn-sm icon-btn" aria-label="Dismiss" onclick={cancelOauth}><Icon name="close" size={16} /></button>
      </span>
    </div>
  </div>
{:else if oauth?.status === "error"}
  <p class="fail" role="alert"><Icon name="error" size={16} />{oauth.message}</p>
{/if}

{#if !app.status}
  <p class="lbl" id="refresh-why">loading…</p>
{:else if !auto}
  <p class="lbl unwatched-modes">{UNWATCHED_MODES}</p>
{:else}
  <div class="card strip">
    <div class="cell">
      <div class="t" id="scope-label">Auto watches these accounts</div>
      <div class="scope" role="radiogroup" aria-labelledby="scope-label" aria-busy={saving ? "true" : undefined}>
        <label><input type="radio" name="scope" checked={!picking} onchange={chooseEvery} /><span>Every enabled account</span></label>
        <label><input type="radio" name="scope" checked={picking} onchange={() => { chosen = "only"; }} /><span>Only the ones I pick</span></label>
      </div>
      {#if !picking}<p class="h" id="scope-why">the switches apply once you pick accounts</p>
      {:else if scope.length === 0}<p class="h">nothing is picked yet, so every enabled account is still watched</p>{/if}
    </div>
    {#each missing as index (index)}
      <div class="stale">
        <Icon name="warning" size={16} /><span>Watched but not found:</span> <code>{index}</code>
        <button type="button" class="btn-sm" aria-busy={saving ? "true" : undefined} aria-label="Remove {index} from the scope" onclick={() => writeScope(scope.filter((i) => i !== index))}>Remove</button>
      </div>
    {/each}
  </div>
  {#if failure}<p role="alert">{failure}</p>{/if}
{/if}

{#if app.status}
  {#if noSecret}
    <div class="off-panel"><Icon name="info" /><p>{NO_SECRET}</p></div>
  {:else if !app.accounts && app.failed.accounts}
    <div class="off-panel"><Icon name="error" /><p>{accountsUnread(app.failed.accounts)}</p></div>
  {:else if !read}
    <p class="lbl">No account read yet</p>
  {:else if app.accounts?.poll.last_error}
    <div class="banner banner-error" role="alert"><Icon name="error" /><p>The last read failed: {app.accounts.poll.last_error}</p></div>
  {/if}

  {#if none}
    <div class="card empty">
      <span class="em-ic"><Icon name="accounts" /></span>
      <div><h3>No Codex account yet</h3><p>Add the account your coding client uses; Lokki reads its quota in every mode.</p></div>
      <button type="button" class="btn-primary" onclick={addAccount}><Icon name="plus" size={16} />Add a Codex account</button>
    </div>
  {:else if rows.length > 0}
    <div class="acards">
      {#each rows as a (a.auth_index)}
        <AccountCard account={a} nowS={clock.s} {threshold} {auto} live={picking} busy={saving}
          watched={picking ? scope.includes(a.auth_index) : a.in_scope} onwatch={toggle} />
      {/each}
    </div>
  {/if}
{/if}
