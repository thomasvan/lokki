// The update state's words (the banner and Settings › Updates and About say them through here).
// An update is on offer while it is available, or while a failed install keeps it.

export const offered = (u: UpdateStatus): boolean => u.state === "available" || (u.state === "error" && !!u.version);

export function updateLine(u: UpdateStatus): string {
  if (offered(u)) return `Lokki ${u.version} is available.`;
  switch (u.state) {
    case "checking": return "Checking for an update…";
    case "downloading": return "Downloading the update…";
    case "installing": return "Installing the update; Lokki restarts when it is done.";
    case "up_to_date": return `Lokki ${u.current_version} is up to date.`;
    default: return `Lokki ${u.current_version}`;
  }
}

// What the banner and Settings say of an update, each in its own element: the line, the reason the
// shell gives for not installing here (beside an offer it cannot install, and beside "up to date"),
// and the error. The reason and the error are the shell's sentences, said as they come.
export function updateWords(u: UpdateStatus): { line: string; reason: string | null; error: string | null } {
  const reason = (offered(u) ? !u.can_install : u.state === "up_to_date") ? u.reason : null;
  return { line: updateLine(u), reason, error: u.error };
}
