# Data directory

`python -m lokki_core init --data-dir DIR` creates this layout. Directories
start with mode `0700` and files with mode `0600` where the platform supports
POSIX modes. Both configuration files are written atomically; rerunning init
when either configuration file exists leaves the directory unchanged and says so
on stderr, except that on a `config.yaml` init made it applies the renames in
`lokki_core/migrate.py` and rewrites the file whole once; the JSON line lists them
as `migrated`. A `config.yaml` without the keys init writes is reported as not made
by init and is left alone. Ports are `0` (any free port) or 1 to 65535; anything
else is refused with exit status 2.

The data directory may already exist when it is empty, or when it holds only the
entries an interrupted first run left. One that holds anything else, such as a
`LOKKI_DATA_DIR` pointed at a folder in use, is refused: `init` prints one line
naming the directory and an entry it did not make, exits with status 1 and
changes nothing. The line says to point `LOKKI_DATA_DIR` at an empty or new
folder, or to move those files out and press Try again, which runs `init` again.
The app reads `LOKKI_DATA_DIR` once, when it starts (`run` in
`app/src-tauri/src/lib.rs` calls `paths::data_dir()`, which reads it in `data_dir_for`,
`app/src-tauri/logic/src/paths.rs`),
so an app opened from Finder, the Start menu or a launcher must be quit and opened
again with the variable set; Try again does not pick it up. Moving the files out and
pressing Try again needs no environment.
Making it `0700`, or on Windows giving it the DACL below, would shut other users
out of what it already holds.

Windows keeps no POSIX modes. There `init` gives the data directory and each
directory in the layout a protected DACL instead: full control for the user
running `init` and for SYSTEM, and for nobody else. Everything made inside them
inherits it, CLIProxyAPI's files included. An administrator can still take
ownership. On a volume that keeps no ACLs, such as FAT or exFAT, this step is
passed over. Anywhere else, if Windows refuses the DACL, `init` stops before it
writes any secret, prints one line on stderr and exits with status 1. A rerun on a
directory that already has a configuration file leaves the directories alone, so
one an earlier version created keeps the ACL it had. Only a development build
from before the DACL made such a directory; no released build did.

| Path | Purpose and owner | POSIX mode |
| --- | --- | --- |
| `config.yaml` | Lokki core settings, including its client credentials and the CLIProxyAPI connection details; owned by the core. A key an older version wrote under a name that moved is read under the old name and rewritten under the new one by the next `init`; a key this version does not know stays and is listed in `unknown_keys` | `0600` |
| `cliproxy/config.yaml` | CLIProxyAPI server, auth directory, API key and management settings; CLIProxyAPI owns it after initialization, including edits from its panel | `0600` |
| `cliproxy/auth/` | CLIProxyAPI OAuth/authentication state; owned by CLIProxyAPI | `0700` |
| `secrets/` | Lokki-managed secret files | `0700` |
| `secrets/cliproxy-mgmt.secret` | Plaintext management secret CLIProxyAPI needs at startup; owned by Lokki core | `0600` |
| `logs/` | Log directory for the sidecar processes; those processes own files they create | `0700` |
| `runs.jsonl` | Lokki core's run evidence log | `0600` |
| `lokki.lock` | Locked by the running shell, so that no second Lokki runs on this directory ([first run](first-run.md)); owned by the shell | `0600` |

`init` prints the selected core and CLIProxyAPI ports and configuration paths,
never the generated secrets. It writes `server.host: "127.0.0.1"` and
`management.disable-control-panel: true` into CLIProxyAPI's file and
`cliproxy.panel: false` into the core's.

`sync-cliproxy --data-dir DIR` reads `config.yaml` through the loader and writes
`server.host` (`127.0.0.1`), `server.port` (`cliproxy.port`) and
`management.disable-control-panel` (`not cliproxy.panel`) into CLIProxyAPI's
file, keeping every other key as loaded and the file's mode. It prints nothing,
and exits 1 with one sentence, writing nothing, for a file it cannot use. It and
`set-port` below are the only operations Lokki performs on CLIProxyAPI's file; the
rest of that document belongs to CLIProxyAPI. Both replace that file whole, so a
symlink there becomes a regular file.

`set-port --data-dir DIR --core-port N` or `--cliproxy-port N` (1 to 65535) is
the shell's way back when a moved port does not come up. It writes `listen` as one
address on the host it had, or `cliproxy.port` with CLIProxyAPI's `server.host`
(`127.0.0.1`) and `server.port`, keeping every other key as loaded and dropping
comments, as a settings write does. The core's `config.yaml` is written at its real
path, so a symlink there keeps its link. It prints nothing. It reads both files
before it writes either, and exits 1 with one sentence, writing neither, for a file
it cannot use or a `listen` of several addresses, and 2 without a port. A write that
fails also exits 1 with one sentence. CLIProxyAPI's file is written first, through a
sibling file that then replaces it, so a failure there changes neither file, and the
sentence ends "nothing changed". A failure writing `config.yaml` after it leaves
CLIProxyAPI's file on the new port while `config.yaml` holds the old one, and the
sentence says so; the next start's `sync-cliproxy` writes it back in step with
`config.yaml`. It
does not ask what listens on the port: the start that follows does.

`shell-config --data-dir DIR` prints one JSON line, `{core_port, cliproxy_port,
operator_token, management_secret_file, panel}`, read through the loader. It is
the one subcommand that prints a secret (the operator token), for the shell's
pipe; nothing logs that output.
