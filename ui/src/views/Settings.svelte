<script lang="ts">
  import { onDestroy, onMount, untrack } from "svelte";
  import { getModels, keyToShow, type SchemaRow } from "../lib/api";
  import { GROUP_TITLES, PORT, TUNING, group, patchOf, valueOf, widget, type Tuning } from "../lib/fields";
  import { MODELS_LEVEL, MODELS_NONE, NO_SECRET, NO_SECRET_AUTO, UNWRITABLE, watchedCount } from "../lib/mode";
  import { panelUrl } from "../lib/panel";
  import { app, checkNow, errorText, loadSchema, loadSettings, noSecretFile, saveSettings, type Service, type Tab } from "../lib/state.svelte";
  import { updateWords } from "../lib/update";
  import { VERSION } from "../lib/version";
  import Icon from "../components/Icon.svelte";
  import ModeSlider from "../components/ModeSlider.svelte";
  import Switch from "../components/Switch.svelte";

  let { ontab }: { ontab: (tab: Tab) => void } = $props();

  // The groups that save: Mode, Connection, and each subgroup of the Advanced fold on its own.
  type Name = "saving" | "connection" | "address" | "cliproxy" | keyof Tuning;
  const SUBGROUPS = [...TUNING, "other"] as const;
  type Failure = { message: string; key?: string };

  const NAMES = { core: "the core", cliproxy: "CLIProxyAPI" } as const;
  const BUTTONS = { core: "Restart the core", cliproxy: "Restart CLIProxyAPI" } as const;
  // The mode slider writes the mode and the threshold, and the models write at once, each with one PUT, and never
  // draft; the watched accounts are written on Accounts. Every other field drafts, and each group saves its own.
  const AT_ONCE = ["routing", "low_quota.threshold_percent", "low_quota.accounts", "low_quota.models"];
  // The Mode group's rows in the order spec 6.1 shows them; the schema keeps its own. A row not named here follows.
  const MODE_ORDER = ["routing", "low_quota.threshold_percent", "low_quota.accounts", "low_quota.rule", "low_quota.models"];
  const rank = (key: string) => (MODE_ORDER.includes(key) ? MODE_ORDER.indexOf(key) : MODE_ORDER.length);

  const groups = $derived(group(app.schema ?? []));
  const modeOrder = $derived([...groups.saving].sort((a, b) => rank(a.key) - rank(b.key)));
  const settings = $derived(app.settings);
  const writable = $derived(settings?.writable ?? false);
  // Auto's rows show while Auto is the mode. A draft on a row that is not shown stays, but is neither counted nor sent.
  const auto = $derived(app.status?.routing === "auto");
  // A core with no secret file reads nothing, so there is no count to show; Auto's rows stay editable for when it has one.
  const noSecret = $derived(noSecretFile());
  const count = $derived(noSecret ? null : watchedCount(app.accounts));
  const showsRule = $derived(count === null || count > 1);
  const modeRows = $derived(groups.saving.filter((r) => !AT_ONCE.includes(r.key) && auto && (r.key !== "low_quota.rule" || showsRule)));
  const drafted = $derived<Record<Name, SchemaRow[]>>({
    saving: modeRows,
    connection: groups.connection,
    address: groups.address,
    cliproxy: groups.cliproxy,
    ...groups.tuning,
  });

  // What the user typed and the core has not accepted yet, by key.
  let drafts = $state<Record<string, unknown>>({});
  const blank = () => ({ busy: false, failure: null as Failure | null });
  const forms = $state<Record<Name, ReturnType<typeof blank>>>({
    saving: blank(), connection: blank(), address: blank(), cliproxy: blank(), quota_reading: blank(), cutting: blank(), tool_calls: blank(), traffic: blank(), other: blank(),
  });

  const current = (key: string) => (settings ? valueOf(settings, key) : undefined);
  const shown = (key: string) => (several(key) ? (key === "listen" ? addresses?.join(", ") : "") : key in drafts ? drafts[key] : current(key));
  // A cleared number field is no change: the core has no empty number to take.
  const changed = (rows: SchemaRow[]) => rows.filter((r) => r.writable &&
    r.key in drafts && drafts[r.key] !== current(r.key) && !(drafts[r.key] === "" && widget(r) === "number"));
  // A list of several addresses cannot be edited as one.
  const addresses = $derived.by(() => {
    const list = settings?.listen;
    return Array.isArray(list) && list.length > 1 ? (list as string[]) : null;
  });
  // The Port is `listen`'s: locked, refused and restarted as it is.
  const settingOf = (key: string) => (key === PORT ? "listen" : key);
  const several = (key: string) => settingOf(key) === "listen" && addresses !== null;
  const fromEnv = (key: string) => (settings?.env_overrides ?? []).includes(settingOf(key));
  const locked = (key: string) => !writable || fromEnv(key) || several(key);
  const why = (key: string) => (fromEnv(key) ? "set by the environment" : several(key) ? "edit the file to change several addresses" : null);
  const describedBy = (key: string) => [`meaning-${key}`, why(key) ? `why-${key}` : "", !writable ? "file-why" : ""].filter(Boolean).join(" ");
  const serviceOf = (key: string): Service => (key.startsWith("cliproxy.") ? "cliproxy" : "core");
  const keyOf = (e: unknown) => (e && typeof e === "object" && "key" in e && typeof e.key === "string" ? e.key : undefined);

  function edit(row: SchemaRow, el: HTMLInputElement | HTMLSelectElement) {
    drafts[row.key] = widget(row) === "number" && el.value !== "" ? Number(el.value) : el.value;
  }

  // The button stays enabled while its save runs, so it keeps the focus; a press meanwhile saves nothing.
  async function save(name: Name) {
    const f = forms[name];
    const sent = changed(drafted[name]).map((r) => [r.key, drafts[r.key]] as [string, unknown]);
    if (f.busy || !writable || sent.length === 0) return;
    f.busy = true;
    f.failure = null;
    try {
      const result = await saveSettings(patchOf(sent, settings ?? undefined));
      for (const k of result.restart_required) app.restart[serviceOf(k)] = true;
      // A draft goes once the read back holds the value sent; a field typed into again meanwhile keeps its draft.
      const unconfirmed: string[] = [];
      for (const [k, v] of sent) {
        if (drafts[k] !== v) continue;
        if (current(k) === v) delete drafts[k];
        else unconfirmed.push(k);
      }
      if (unconfirmed.length > 0) {
        const reason = app.failed.settings ? errorText(app.failed.settings) : "the core still reports the old value";
        f.failure = { message: `saved, but the settings could not be read back: ${reason}` };
      }
    } catch (e) {
      f.failure = { message: errorText(e), key: keyOf(e) };
    } finally {
      f.busy = false;
    }
  }

  // The models Auto cuts when low. The list of models comes from CLIProxyAPI (GET /api/models), not from the
  // quota read, so it is read on entering Settings in Auto and on Retry, whether or not the core reads a quota.
  // A choice writes at once, one write at a time; the controls stay enabled while it runs, so a focused one
  // keeps the focus, and a press meanwhile writes nothing. "Only the ones I pick" with nothing picked is the
  // same as "Every model", so it is local until one is picked.
  const cutModels = $derived(app.status?.low_quota.models ?? []);
  let listed = $state<string[] | null>(null);
  let listFailure = $state<string | null>(null);
  let listReading = $state(false);
  let listAsked = false;
  async function readModels() {
    if (listReading) return;
    listAsked = true;
    listReading = true;
    try {
      listed = (await getModels()).models;
      listFailure = null;
    } catch (e) {
      listed = null;
      listFailure = errorText(e);
    } finally {
      listReading = false;
    }
  }
  $effect(() => { if (auto && !untrack(() => listAsked)) void untrack(readModels); });
  const unlisted = $derived(listed === null ? [] : cutModels.filter((m) => !listed!.includes(m)));
  let modelsChoice = $state<"every" | "only" | null>(null);
  const picking = $derived((modelsChoice ?? (cutModels.length > 0 ? "only" : "every")) === "only");
  let modelsSaving = $state(false);
  let modelsFailure = $state("");
  let everyRadio = $state<HTMLInputElement>();
  let onlyRadio = $state<HTMLInputElement>();
  // The radios and boxes show what the core reports, not the click: a press that wrote nothing, or a write that failed, puts them back.
  const resync = () => { if (everyRadio) everyRadio.checked = !picking; if (onlyRadio) onlyRadio.checked = picking; };
  async function writeModels(models: string[], after: "only" | null) {
    if (modelsSaving || !writable) return;
    modelsSaving = true;
    modelsFailure = "";
    try {
      await saveSettings({ low_quota: { models } });
      modelsChoice = after;
    } catch (e) {
      modelsFailure = errorText(e) + (keyOf(e) ? ` (${keyOf(e)})` : "");
    } finally {
      modelsSaving = false;
      resync();
    }
  }
  const toggleModel = (id: string) => writeModels(cutModels.includes(id) ? cutModels.filter((m) => m !== id) : [...cutModels, id], "only");
  const removeModel = (id: string) => writeModels(cutModels.filter((m) => m !== id), "only");
  function chooseEveryModel() {
    if (cutModels.length > 0) void writeModels([], null);
    else modelsChoice = null;
    resync();
  }
  function chooseOnlyModels() {
    modelsChoice = "only";
  }

  // The watched count is the rows of the last accounts read that the core marks in scope.
  const watched = $derived.by(() => {
    if (noSecret) return { count: null, note: null };
    const a = app.accounts;
    if (!a) return { count: null, note: app.failed.accounts ? `the accounts could not be read: ${errorText(app.failed.accounts)}` : "Reading the accounts…" };
    if (count === null) return { count: null, note: "No account read yet" };
    const stale = app.failed.accounts ? `counted at the last read; the accounts could not be read again: ${errorText(app.failed.accounts)}` : null;
    return { count: `${count} of ${a.accounts.length}`, note: stale };
  });

  // One restart at a time: a press while one runs does nothing. A failure stays beside its notice.
  const pending = $derived((["cliproxy", "core"] as const).filter((s) => app.restart[s]));
  let restarting = $state<Service | null>(null);
  let restartFailure = $state<{ service: Service; message: string } | null>(null);
  // CLIProxyAPI's restart restarts the core after it, so it applies the core's keys too. A failed one is read
  // back: a moved port that did not come up has been put back in the file.
  async function restart(service: Service) {
    if (restarting) return;
    restarting = service;
    restartFailure = null;
    try {
      await window.lokki?.restart?.(service);
      app.restart[service] = false;
      if (service === "cliproxy") app.restart.core = false;
    } catch (e) {
      restartFailure = { service, message: `could not restart ${NAMES[service]}: ${errorText(e)}` };
      void loadSettings();
    } finally {
      restarting = null;
    }
  }

  // The panel opens in the system browser through the shell, or by link in a browser. Each hook the
  // shell lacks leaves what a browser shows. One hook call at a time; a rejection is shown beside them.
  const link = $derived(settings ? panelUrl(settings, location) : null);
  const panelOn = $derived(current("cliproxy.panel") === true);
  const COPIED_MS = 3_000;
  let hookBusy = $state<"open" | "copy" | null>(null);
  let hookError = $state("");
  let copied = $state(false);
  let copiedTimer: ReturnType<typeof setTimeout> | undefined;
  onDestroy(() => clearTimeout(copiedTimer));
  async function hook(which: "open" | "copy", call: (() => Promise<void>) | undefined) {
    if (hookBusy || !call) return;
    hookBusy = which;
    hookError = "";
    try {
      await call();
      if (which === "copy") {
        copied = true;
        clearTimeout(copiedTimer);
        copiedTimer = setTimeout(() => { copied = false; }, COPIED_MS);
      }
    } catch (e) {
      hookError = `could not do that: ${errorText(e)}`;
    } finally {
      hookBusy = null;
    }
  }

  const text = (v: unknown) => (Array.isArray(v) ? (v.length ? v.join(", ") : "not set")
    : v === true ? "on" : v === false ? "off" : v === "" || v == null ? "not set" : String(v));
  const unset = (v: unknown) => text(v) === "not set";

  // The groups' own links: each scrolls its group into view, opening the Advanced fold when the group is in it.
  function reveal(id: string) {
    const card = document.getElementById(id);
    if (!card) return;
    for (let d = card.closest("details"); d; d = d.parentElement?.closest("details") ?? null) d.open = true;
    const own = card.querySelector(":scope > details");
    if (own instanceof HTMLDetailsElement) own.open = true;
    card.scrollIntoView?.({ block: "start" });
    document.getElementById(`g-${id.replace(/^card-/, "")}`)?.focus({ preventScroll: true });
  }
  function go(event: MouseEvent, id: string) {
    event.preventDefault();
    reveal(`card-${id}`);
  }
  // A link to a group inside the fold, from anywhere, opens it.
  const followHash = () => { if (location.hash.startsWith("#card-")) reveal(location.hash.slice(1)); };
  onMount(() => {
    window.addEventListener("hashchange", followHash);
    return () => window.removeEventListener("hashchange", followHash);
  });
  // Nothing polls the settings, so a failed read offers its own Retry, which reads again what is missing or
  // failed; it stays enabled while it reads.
  let retrying = $state(false);
  async function retry() {
    if (retrying) return;
    retrying = true;
    try {
      await Promise.all([app.schema && !app.failed.schema ? null : loadSchema(), app.settings && !app.failed.settings ? null : loadSettings()]);
    } finally {
      retrying = false;
    }
  }

  const NAV = ["saving", "connection", "about", "advanced"] as const;
  // Unsaved changes inside the fold, said on its link and on its head, which stays when the fold is closed.
  const unsaved = $derived((["address", "cliproxy", ...SUBGROUPS] as Name[]).reduce((n, g) => n + changed(drafted[g]).length, 0));
  const unsavedWords = (n: number) => `${n} unsaved ${n === 1 ? "change" : "changes"}`;
</script>

{#snippet field(name: Name, row: SchemaRow)}
  {@const kind = widget(row)}
  {@const reason = why(row.key)}
  {@const invalid = forms[name].failure?.key === settingOf(row.key)}
  <div class="srow" class:dirty={changed([row]).length > 0}>
    <div class="srow-text">
      {#if kind === "checkbox"}<span class="t">{row.label}</span>{:else}<label class="t" for="f-{row.key}">{row.label}</label>{/if}
      <p class="h" id="meaning-{row.key}">{row.meaning}</p>
      {#if reason}<p class="why" id="why-{row.key}"><Icon name="lock" size={16} />{reason}</p>{/if}
    </div>
    {#if kind === "checkbox"}
      <Switch checked={shown(row.key) === true} label={shown(row.key) === true ? "On" : "Off"} name={row.label}
        disabled={locked(row.key)} describedby={describedBy(row.key)} onchange={(next) => { drafts[row.key] = next; }} />
    {:else if kind === "select"}
      <span class="sel">
        <select id="f-{row.key}" value={shown(row.key) as string} disabled={locked(row.key)} aria-invalid={invalid ? "true" : undefined}
          aria-describedby={describedBy(row.key)} onchange={(e) => edit(row, e.currentTarget)}>
          {#each row.choices as choice (choice)}<option value={choice}>{row.choice_labels[choice] ?? choice}</option>{/each}
        </select>
        <Icon name="chevron" size={16} />
      </span>
    {:else}
      <input id="f-{row.key}" class:num={kind === "number"} type={kind} value={shown(row.key) as string | number} disabled={locked(row.key)}
        spellcheck={kind === "text" ? "false" : undefined} aria-invalid={invalid ? "true" : undefined}
        placeholder={kind === "number" ? String(current(row.key)) : undefined} min={row.minimum ?? undefined} max={row.maximum ?? undefined} step={row.type === "float" ? "any" : undefined}
        aria-describedby={describedBy(row.key)} oninput={(e) => edit(row, e.currentTarget)} />
    {/if}
  </div>
{/snippet}

<!-- A row no one may set here, inside a group that is otherwise written: its value as text, outside the count. -->
{#snippet readOnly(row: SchemaRow)}
  <div class="srow">
    <div class="srow-text">
      <span class="t">{row.label}</span>
      <p class="h">{row.meaning}</p>
      <p class="why"><Icon name="lock" size={16} />read-only</p>
    </div>
    <span class="ro" class:none={unset(current(row.key))}>{text(current(row.key))}</span>
  </div>
{/snippet}

{#snippet rows(name: Name, list: SchemaRow[])}
  {#each list as row (row.key)}
    {#if !row.writable}{@render readOnly(row)}{:else}{@render field(name, row)}{/if}
    {#if row.key === "cliproxy.panel"}{@render panel()}{/if}
  {/each}
{/snippet}

{#snippet models(row: SchemaRow)}
  <div class="srow">
    <div class="srow-text">
      <span class="t" id="lbl-models">{row.label}{#if modelsSaving}<Icon name="spinner" size={16} />{/if}</span>
      {#if picking}
        <p class="h">A model not picked always passes through at full price</p>
        <p class="h">{MODELS_LEVEL}</p>
      {:else}
        <p class="h">Every model is cut when the quota is low.</p>
      {/if}
    </div>
    <div class="scope" role="radiogroup" aria-labelledby="lbl-models" aria-busy={modelsSaving ? "true" : undefined}>
      <label><input type="radio" name="models" bind:this={everyRadio} checked={!picking} disabled={!writable} onchange={chooseEveryModel} /><span>Every model</span></label>
      <label><input type="radio" name="models" bind:this={onlyRadio} checked={picking} disabled={!writable} onchange={chooseOnlyModels} /><span>Only the ones I pick</span></label>
    </div>
  </div>
  {#if picking}
    <div class="mpick" role="group" aria-labelledby="lbl-models" aria-busy={modelsSaving ? "true" : undefined}>
      {#if listFailure}
        <div class="g-err in" role="alert"><Icon name="error" size={16} /><p>lokki could not read the models CLIProxyAPI offers: {listFailure}</p>
          <button type="button" class="btn-sm" aria-busy={listReading ? "true" : undefined} onclick={readModels}>Retry</button></div>
      {/if}
      {#if listed !== null}
        {#if listed.length === 0}<p class="mnote">CLIProxyAPI lists no models</p>{/if}
        <div class="mlist">
          {#each listed as id (id)}
            <label class="mitem">
              <input type="checkbox" checked={cutModels.includes(id)} disabled={!writable}
                onchange={(e) => { e.currentTarget.checked = cutModels.includes(id); void toggleModel(id); }} /><span class="mono">{id}</span>
            </label>
          {/each}
        </div>
        {#each unlisted as id (id)}
          <div class="stale">
            <Icon name="warning" size={16} /><span>Chosen but not listed:</span> <code>{id}</code>
            <button type="button" class="btn-sm" disabled={!writable} aria-busy={modelsSaving ? "true" : undefined} aria-label="Remove {id} from the models cut when low" onclick={() => removeModel(id)}>Remove</button>
          </div>
        {/each}
      {:else}
        <!-- No list to compare against, so none is "not listed"; every chosen name stays removable -->
        {#if !listFailure}<p class="mnote" role="status">Reading the models…</p>{/if}
        {#each cutModels as id (id)}
          <div class="mchosen"><span class="mono">{id}</span>
            <button type="button" class="btn-sm" disabled={!writable} aria-busy={modelsSaving ? "true" : undefined} aria-label="Remove {id} from the models cut when low" onclick={() => removeModel(id)}>Remove</button>
          </div>
        {/each}
      {/if}
      {#if cutModels.length === 0 && !listFailure}<p class="mnote">{MODELS_NONE}</p>{/if}
      {#if modelsFailure}<p class="fail" role="alert"><Icon name="error" size={16} />{modelsFailure}</p>{/if}
    </div>
  {/if}
{/snippet}

{#snippet panel()}
  <div class="srow sub">
    {#if !panelOn}
      <p class="h">Turn it on and save to open the panel; CLIProxyAPI needs a restart to apply it</p>
    {:else}
      {#if window.lokki?.copyManagementSecret}<p class="h">the panel asks for the management secret; paste it from the clipboard</p>{/if}
      <div class="acts">
        {#if window.lokki?.openPanel}
          <button type="button" class="btn-sm" aria-busy={hookBusy === "open" ? "true" : undefined} onclick={() => hook("open", window.lokki?.openPanel)}>
            <Icon name={hookBusy === "open" ? "spinner" : "external"} size={16} />Open the panel
          </button>
        {:else if link === null}
          <p class="lbl">{String(settings?.cliproxy.url)} — not a web address</p>
        {:else}
          <a class="btn-link" href={link} target="_blank" rel="noopener"><Icon name="external" size={16} />Open the panel</a>
          <span class="lbl">opens in a new tab</span>
        {/if}
        {#if window.lokki?.copyManagementSecret}
          <button type="button" class="btn-sm" aria-busy={hookBusy === "copy" ? "true" : undefined} onclick={() => hook("copy", window.lokki?.copyManagementSecret)}>
            <Icon name={hookBusy === "copy" ? "spinner" : copied ? "check" : "copy"} size={16} />{copied ? "Copied" : "Copy the management secret"}
          </button>
        {/if}
      </div>
    {/if}
    {#if hookError}<p class="fail" role="alert"><Icon name="error" size={16} />{hookError}</p>{/if}
  </div>
{/snippet}

{#snippet head(id: "saving" | "connection" | "about" | "advanced", fold = false)}
  {@const g = GROUP_TITLES[id]}
  <div class="g-head">
    {#if fold}<span class="chev"><Icon name="chevron" size={16} /></span>{/if}
    <h3 id="g-{id}" tabindex="-1">{g.title}</h3>
    {#if "note" in g}<span class="lbl">{g.note}</span>{/if}
    {#if id === "advanced" && unsaved > 0}<span class="flag">{unsavedWords(unsaved)}</span>{/if}
  </div>
{/snippet}

<!-- One subgroup of the Advanced fold: its own rows, its own refusal and its own Save. -->
{#snippet sub(name: Name, id: string, list: SchemaRow[])}
  <section class="adv" id="card-{id}" aria-labelledby="g-{id}">
    <h4 class="sg-h" id="g-{id}" tabindex="-1">{GROUP_TITLES[id as keyof typeof GROUP_TITLES].title}</h4>
    {@render refusal(name)}
    {@render rows(name, list)}
    {@render foot(name)}
  </section>
{/snippet}

{#snippet refusal(name: Name)}
  {@const f = forms[name].failure}
  {#if f}
    {@const key = keyToShow(f.message, f.key)}
    <div class="g-err" role="alert"><Icon name="error" size={16} /><p>{key ? `${f.message} — ` : f.message}{#if key}<code>{key}</code>{/if}</p></div>
  {/if}
{/snippet}

<!-- "No changes" stays focusable (aria-disabled) so the focus survives a save that empties the count;
     a file no one may write disables it outright. -->
{#snippet foot(name: Name)}
  {@const f = forms[name]}
  {@const n = changed(drafted[name]).length}
  <footer class="card-foot">
    <button type="button" class="btn-primary btn-sm save" disabled={!writable} aria-disabled={writable && !f.busy && n === 0 ? "true" : undefined}
      aria-busy={f.busy ? "true" : undefined} aria-describedby={writable ? undefined : "file-why"} onclick={() => save(name)}>
      {#if f.busy}<Icon name="spinner" size={16} />Saving…{:else if n === 0}No changes{:else}Save {n} {n === 1 ? "change" : "changes"}{/if}
    </button>
  </footer>
{/snippet}

{#if app.schema && settings}
  <div class="phead start"><h2>Settings</h2><span class="lbl">each group saves on its own</span></div>

  {#if !writable}<div class="banner banner-warning"><Icon name="lock" /><p id="file-why">{UNWRITABLE}</p></div>{/if}

  <!-- A later read that failed: the form keeps the last read's values and says so until a read answers. -->
  {#if app.failed.settings ?? app.failed.schema}
    <p class="read-fail" role="alert">The settings could not be read again: {errorText(app.failed.settings ?? app.failed.schema)}; these are the values of the last read
      <button type="button" class="btn-sm" aria-busy={retrying ? "true" : undefined} onclick={retry}>Retry</button></p>
  {/if}

  <div class="sgrid">
    <nav class="snav" aria-label="Settings groups">
      {#each NAV as id (id)}
        {@const n = id === "advanced" ? unsaved : 0}
        <a href="#card-{id}" aria-label={n > 0 ? `${GROUP_TITLES[id].title}, ${unsavedWords(n)}` : undefined} onclick={(e) => go(e, id)}>
          {GROUP_TITLES[id].title}{#if n > 0}<span class="flag" aria-hidden="true">{n}</span>{/if}
        </a>
      {/each}
    </nav>

    <div class="groups">
      <!-- In the groups' column, so the bar can stick above the groups without covering their links. -->
      {#if pending.length > 0}
        <div class="restart" role="status">
          {#each pending as service (service)}
            <div class="rb-line">
              <Icon name="restart" />
              <div class="rb-text">
                <p>Restart {NAMES[service]} to apply</p>
                {#if restartFailure?.service === service}<p class="fail" role="alert"><Icon name="error" size={16} />{restartFailure.message}</p>{/if}
              </div>
              {#if window.lokki?.restart}
                <button type="button" class="btn-primary btn-sm" aria-busy={restarting === service ? "true" : undefined} onclick={() => restart(service)}>
                  {#if restarting === service}<Icon name="spinner" size={16} />Restarting…{:else}{BUTTONS[service]}{/if}
                </button>
              {/if}
            </div>
          {/each}
        </div>
      {/if}
      <section class="card sgroup" id="card-saving" aria-labelledby="g-saving">
        {@render head("saving")}
        {@render refusal("saving")}
        {#each modeOrder as row (row.key)}
          {#if row.key === "routing"}
            <!-- The mode slider at the card's width: the mode and Auto's threshold in one control -->
            <div class="srow slider-row">
              <ModeSlider />
              {#if fromEnv(row.key)}<p class="why"><Icon name="lock" size={16} />set by the environment</p>{/if}
            </div>
            {#if auto}
              {#if noSecret}<p class="nosecret"><Icon name="info" size={16} />{NO_SECRET}{NO_SECRET_AUTO}</p>{/if}
              <h4 class="sg-h">Auto's rule</h4>
            {/if}
          {:else if !auto || row.key === "low_quota.threshold_percent"}
            <!-- Auto's rows are not shown between the slider's ends; their drafts stay and are not counted. The
                 threshold is the slider's. -->
          {:else if row.key === "low_quota.accounts"}
            <div class="srow">
              <div class="srow-text">
                <span class="t">{watched.count ? `${row.label}: ${watched.count}` : row.label}</span>
                <p class="h">{watched.note ?? row.meaning}</p>
              </div>
              <button type="button" class="linkbtn" onclick={() => ontab("accounts")}>Manage on Accounts<Icon name="chevron" size={16} /></button>
            </div>
          {:else if row.key === "low_quota.models"}
            {@render models(row)}
          {:else if row.key !== "low_quota.rule" || showsRule}
            {@render rows("saving", [row])}
          {/if}
        {/each}
        {@render foot("saving")}
      </section>

      <section class="card sgroup" id="card-connection" aria-labelledby="g-connection">
        {@render head("connection")}
        {@render refusal("connection")}
        {@render rows("connection", groups.connection)}
        {@render foot("connection")}
      </section>

      <section class="card sgroup" id="card-about" aria-labelledby="g-about">
        {@render head("about")}
        {#if app.update}
          {@const u = app.update}
          {@const w = updateWords(u)}
          <div class="srow">
            <div class="srow-text">
              <span class="t">{w.line}</span>
              {#if w.reason}<p class="h">{w.reason}</p>{/if}
              {#if w.error}<p class="why err">{w.error}</p>{/if}
              <p class="h">lokki checks once a day; a new release shows a banner at the top</p>
            </div>
            <!-- As the banner: none while a download or install runs; busy, with no second check, while one runs -->
            {#if u.state !== "downloading" && u.state !== "installing"}
              <button type="button" class="btn-sm" aria-busy={u.state === "checking" ? "true" : undefined} onclick={() => { if (u.state !== "checking") checkNow(); }}>
                {#if u.state === "checking"}<Icon name="spinner" size={16} />{/if}Check now
              </button>
            {/if}
          </div>
        {/if}
        <dl class="kvlist">
          {#if app.status}
            <dt>Core</dt><dd>{app.status.version} <span class="lbl">(revision {app.status.revision})</span></dd>
          {/if}
          <dt>UI</dt><dd>{VERSION}</dd>
          {#if app.status}
            <dt>CLIProxyAPI</dt><dd class:none={!app.status.cliproxy.version}>{app.status.cliproxy.version ?? "not reported"}</dd>
            <dt>Status</dt>
            <dd class="run"><span class="dot" class:dot-run={app.status.cliproxy.reachable} class:dot-down={!app.status.cliproxy.reachable}></span>CLIProxyAPI {app.status.cliproxy.reachable ? "running" : "not reachable"}</dd>
          {/if}
        </dl>
      </section>

      <section class="card sgroup" id="card-advanced" aria-labelledby="g-advanced">
        <details>
          <summary>{@render head("advanced", true)}</summary>
          {@render sub("address", "address", groups.address)}
          {@render sub("cliproxy", "cliproxy", groups.cliproxy)}
          {#each SUBGROUPS as g (g)}
            {#if groups.tuning[g].length > 0}{@render sub(g, `tuning.${g}`, groups.tuning[g])}{/if}
          {/each}
          <section class="adv" id="card-file" aria-labelledby="g-file">
            <h4 class="sg-h"><span id="g-file">{GROUP_TITLES.file.title}</span><span class="lbl">{GROUP_TITLES.file.note}</span></h4>
            <dl class="kvlist">
              {#each groups.file as row (row.key)}
                <dt>{row.label}</dt><dd class:none={unset(current(row.key))}>{text(current(row.key))}</dd>
              {/each}
            </dl>
            <p class="ro-note"><Icon name="lock" size={16} />Change these in the settings file itself.</p>
          </section>
        </details>
      </section>
    </div>
  </div>
{:else if app.failed.settings ?? app.failed.schema}
  <p class="read-fail" role="alert">The settings could not be read: {errorText(app.failed.settings ?? app.failed.schema)}
    <button type="button" class="btn-sm" aria-busy={retrying ? "true" : undefined} onclick={retry}>Retry</button></p>
{:else}
  <p class="lbl" role="status">Reading the settings…</p>
{/if}
