// What the settings form is made of, from the schema rows: which group a row sits in,
// which input it gets, and the PUT body for the keys that changed. The schema gives each
// row its label, group and choice labels; the UI owns only the group titles and their order.
import type { SchemaRow, SettingsView } from "./api";

// In the order Settings shows them. `advanced` is the fold; the groups after it are what it holds.
// `about` is the UI's own group: no schema row has it.
export const GROUP_TITLES = {
  saving: { title: "Mode", note: "when Lokki cuts" },
  connection: { title: "Connection", note: "how clients reach Lokki" },
  about: { title: "Updates and About" },
  advanced: { title: "Advanced", note: "for a server operator, or with a measurement behind it" },
  address: { title: "The core's address" },
  cliproxy: { title: "CLIProxyAPI" },
  "tuning.quota_reading": { title: "Quota reading" },
  "tuning.cutting": { title: "Cutting" },
  "tuning.tool_calls": { title: "Tool calls" },
  "tuning.traffic": { title: "Traffic" },
  "tuning.other": { title: "Other" },          // a writable row of a group the UI does not know
  file: { title: "Set in the file", note: "read-only here" },
} as const;

export const TUNING = ["quota_reading", "cutting", "tool_calls", "traffic"] as const;
export type Tuning = Record<(typeof TUNING)[number] | "other", SchemaRow[]>;
export type Groups = {
  saving: SchemaRow[]; connection: SchemaRow[]; address: SchemaRow[]; cliproxy: SchemaRow[]; tuning: Tuning; file: SchemaRow[];
};

// Connection's Port is the UI's own field over `listen`: the core's one address with its port changed and its host
// kept. It writes `listen`; no key named `port` reaches the core.
export const PORT = "port";
const portOf = (address: unknown) => (typeof address === "string" ? Number(address.slice(address.lastIndexOf(":") + 1)) : undefined);
const withPort = (address: string, port: unknown) => `${address.slice(0, address.lastIndexOf(":"))}:${String(port)}`;
const portRow = (listen: SchemaRow): SchemaRow => ({
  ...listen, key: PORT, type: "int", default: portOf((listen.default as string[])[0]), label: "Port", minimum: 1, maximum: 65535,
  meaning: "where this window and your clients reach Lokki; after the core restarts on a new port, each client must be told it",
});

// A row of a group the UI does not know is never dropped: a writable one goes under Advanced › Other,
// a read-only one under Advanced › Set in the file. `hidden` rows (auth.*) are never shown.
export function group(rows: SchemaRow[]): Groups {
  const groups: Groups = {
    saving: [], connection: [], address: [], cliproxy: [], file: [],
    tuning: { quota_reading: [], cutting: [], tool_calls: [], traffic: [], other: [] },
  };
  const tuning = (g: string) => TUNING.find((t) => g === `tuning.${t}`);
  for (const row of rows) {
    const g = row.group;
    const sub = tuning(g);
    if (row.key === "listen") groups.connection.push(portRow(row));
    if (g === "hidden") continue;
    if (g === "saving" || g === "connection" || g === "address" || g === "cliproxy" || g === "file") groups[g].push(row);
    else if (sub) groups.tuning[sub].push(row);
    else if (row.writable) groups.tuning.other.push(row);
    else groups.file.push(row);
  }
  return groups;
}

export function widget(row: SchemaRow): "checkbox" | "number" | "select" | "text" {
  if (row.type === "bool") return "checkbox";
  if (row.type === "int" || row.type === "float") return "number";
  if (row.type === "choice") return "select";
  return "text";
}

// `listen` is a list in the schema and one address in the form; the Port goes into the address `view` holds.
export function patchOf(changes: Array<[string, unknown]>, view?: SettingsView): Record<string, unknown> {
  const patch: Record<string, unknown> = {};
  for (const [key, value] of changes) {
    if (key === PORT) {
      patch.listen = [withPort(String(view ? valueOf(view, "listen") : ""), value)];
      continue;
    }
    const path = key.split(".");
    const leaf = path.pop() as string;
    let node = patch;
    for (const part of path) node = (node[part] ??= {}) as Record<string, unknown>;
    node[leaf] = key === "listen" ? [value] : value;
  }
  return patch;
}

export function valueOf(view: SettingsView, key: string): unknown {
  if (key === PORT) return portOf(valueOf(view, "listen"));
  let node: unknown = view;
  for (const part of key.split(".")) node = (node as Record<string, unknown> | undefined)?.[part];
  return key === "listen" && Array.isArray(node) ? node[0] : node;
}
