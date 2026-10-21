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

| Path | Purpose and owner | Mode |
| --- | --- | --- |
| `config.yaml` | lokki core settings, including its client credentials and the CLIProxyAPI connection details; owned by the core. A key an older version wrote under a name that moved is read under the old name and rewritten under the new one by the next `init`; a key this version does not know stays and is listed in `unknown_keys` | `0600` |
| `cliproxy/config.yaml` | CLIProxyAPI server, auth directory, API key and management settings; CLIProxyAPI owns it after initialization, including edits from its panel | `0600` |
| `cliproxy/auth/` | CLIProxyAPI OAuth/authentication state; owned by CLIProxyAPI | `0700` |
| `secrets/` | lokki-managed secret files | `0700` |
| `secrets/cliproxy-mgmt.secret` | Plaintext management secret CLIProxyAPI needs at startup; owned by lokki core | `0600` |
| `logs/` | Log directory for the sidecar processes; those processes own files they create | `0700` |
| `runs.jsonl` | lokki core's run evidence log | `0600` |

`init` prints the selected core and CLIProxyAPI ports and configuration paths,
never the generated secrets. It writes `server.host: "127.0.0.1"` and
`management.disable-control-panel: true` into CLIProxyAPI's file and
`cliproxy.panel: false` into the core's.

`sync-cliproxy --data-dir DIR` reads `config.yaml` through the loader and writes
`server.host` (`127.0.0.1`), `server.port` (`cliproxy.port`) and
`management.disable-control-panel` (`not cliproxy.panel`) into CLIProxyAPI's
file, keeping every other key as loaded and the file's mode. It prints nothing,
and exits 1 with one sentence, writing nothing, for a file it cannot use. It is
the only operation lokki performs on CLIProxyAPI's file; the rest of that
document belongs to CLIProxyAPI.

`shell-config --data-dir DIR` prints one JSON line, `{core_port, cliproxy_port,
operator_token, management_secret_file, panel}`, read through the loader. It is
the one subcommand that prints a secret (the operator token), for the shell's
pipe; nothing logs that output.
