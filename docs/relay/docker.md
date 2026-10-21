# The relay as a container

The relay (`core/`) can run as a Docker container, with the same code, the same `config.yaml` and the
same run log as the native process described in [`live-8381.md`](live-8381.md). This page is how the
container is built, what it is made of, and how to operate it.

| | |
| --- | --- |
| image | `clisaving-relay:<commit>`, built locally from a commit by `deploy/relay/build.sh`; nothing is pushed anywhere |
| definition | `deploy/docker-compose.relay-8381.yml` (`RELAY_SHA=<commit>` is required by `up`) |
| network | `network_mode: host`: it listens where `config.yaml` says (`127.0.0.1:8381` and the Tailscale address) |
| supervision | `restart: unless-stopped`; no crontab entry for the relay (the cliproxy has a boot line: [`live-8381.md`](live-8381.md)) |
| user | `1000:1000`, read-only root filesystem, no capabilities, `no-new-privileges`, `/tmp` a tmpfs |
| stop | `stop_grace_period: 30s` (the relay's own drain is 15 s) |
| logs | `docker logs relay-8381` (json-file, 10 MB x 5) |

## Layout

```
deploy/relay/Dockerfile          base (PyYAML) -> test (the offline suite) -> runtime
deploy/relay/build.sh            builds a COMMIT from `git archive`, tags it clisaving-relay:<commit>
deploy/relay/requirements.txt    PyYAML 6.0.3 with hashes (the relay is otherwise standard library)
deploy/relay/healthcheck.py      the container healthcheck: GET /healthz
deploy/docker-compose.relay-8381.yml
.dockerignore                    an allowlist, for a hand-run `docker build .` (build.sh builds from a tar)

in the image:   /app/core/{server.py, lokki_core/, healthcheck.py, REVISION}
                (an image built before 5192923 keeps the same files under /app/midstream-abort/, with a relay/
                package where lokki_core/ is; the compose healthcheck runs healthcheck.py from whichever exists)
on the host:
                ~/.local/state/cliproxy-8381/            read-write, at the same path in the container:
                                                         runs.jsonl, runs.jsonl.lock (the evidence chain), old logs, dated config copies
                ~/.local/state/cliproxy-8381-conf/       read-only at /etc/relay: config.yaml
                core/config.yaml              a symlink to ~/.local/state/cliproxy-8381-conf/config.yaml
```

The image has no `.git` and sets no `RELAY_REPO_ROOT`, so the relay's in-repository guard (run log, `capture_dir`,
`debug_log`) is off there; the code sits at `/app/core` for readability only. The `test` stage sets
`RELAY_REPO_ROOT=/src`, so the suite's guard tests run against the source tree.

`conf/` is a directory mount, not a file mount: an edit that replaces the file (an editor, `sed -i`) is seen by the
container on the next reload, where a single-file mount would keep the old inode. It sits beside the state directory
and not in it, so that the read-only mount means something. The state directory is mounted read-write and also holds
the dated copies of `config.yaml` (`config.yaml.bak-*`, 0600), the crontab backups and the old logs: the container
can read and change them (it runs as the same user).

**`runs_log:` must be an absolute path in the state directory** (the live file writes out the home directory:
`/home/<user>/.local/state/cliproxy-8381/runs.jsonl`). The container's `HOME` is `/nonexistent` on purpose: a config
without `runs_log:` (or one written with a `~`) would otherwise put the evidence chain under `$HOME`, which with a
writable `HOME` is a tmpfs that a restart empties. Checked: with `HOME=/tmp` such a relay starts and logs to
`/tmp/.local/state/cliproxy/runs.jsonl`; with `HOME=/nonexistent` it stops at start-up with a traceback and status 1 (`OSError ... Read-only file
system: '/nonexistent'`), which under `restart: unless-stopped` is a restart loop, not a quiet container. After any start, the start line in `docker logs` should say `runs=<the state directory>/runs.jsonl`.

## Environment

The image and compose set only these. Every other setting is in `config.yaml`, because **a `RELAY_*` variable overrides
the file** (for every setting but `capture_dir`, where the file wins) and would make a `SIGHUP` after an edit do nothing.

| variable | value | from |
| --- | --- | --- |
| `RELAY_CONFIG` | `/etc/relay/config.yaml` | image and compose |
| `RELAY_REVISION` | the commit the image was built from | image (build argument); also written to `/app/core/REVISION` |
| `HEALTHCHECK_URL` | `http://127.0.0.1:${RELAY_PORT:-8381}/healthz` | compose (read by `healthcheck.py`, not by the relay) |
| `HOME` | `/nonexistent` | compose |
| `PYTHONUNBUFFERED`, `PYTHONDONTWRITEBYTECODE` | `1` | image |

Compose reads these from the shell that runs it: `RELAY_SHA` (required by `up`), and for a second instance
`RELAY_NAME`, `RELAY_PORT`, `RELAY_STATE_DIR`, `RELAY_CONF_DIR`. The client token and the cliproxy key are in
`config.yaml` (0600) and nowhere else: not in the image, not in the environment. The variables the relay itself reads
(`grep -oh 'RELAY_[A-Z_]*' core/lokki_core/*.py`), none of them set here except the first two:
`RELAY_CONFIG`, `RELAY_REVISION`, `RELAY_TOKEN`, `RELAY_TOKENS`, `CLI_PROXY_API_KEY`, `CLIPROXY_URL`, `RELAY_LISTEN`,
`RELAY_RUNS_LOG`, `RELAY_SEG_BUDGET`, `RELAY_TAIL_CHARS`, `RELAY_MERGE_TEXT`, `RELAY_SESSION_PIN`, `RELAY_TEXT_CALLS`, `RELAY_TEXT_CALLS_PAD`,
`RELAY_IMAGEGEN_CUT`, `RELAY_PAYLOAD_CAP`, `RELAY_PACING`, `RELAY_MAX_CONCURRENT`, `RELAY_RETRIES`,
`RELAY_DRAIN_SECONDS`, `RELAY_ALLOW_CLIENT_TUNING`, `RELAY_ALLOW_PUBLIC_BIND`, `RELAY_REJECT_PREVIOUS_RESPONSE_ID`,
`RELAY_CAPTURE_DIR`, `RELAY_DEBUG_LOG`.

## Build

```
deploy/relay/build.sh --test <commit>      # the offline suite on the pinned base image, then the runtime image
```

The image is built from `git archive <commit>`: the tracked files of that commit, nothing from the working tree, so
the untracked `config.yaml`, `auths/` and `logs/` of a checkout cannot get in, and the revision inside the image is
the commit (the build refuses a `REVISION` that is not a hex commit id). Production builds the merge commit on `main`.
Keep the previous images: a rollback is a tag that is still there (`docker image prune -a` would remove it).

The base is `python:3.14-slim` pinned by digest in `Dockerfile` and `build.sh` (the same digest in both), so a rebuild of
the same commit is the same base. A security update of the base is a deliberate change: `docker pull python:3.14-slim`,
put the new digest in both files, and the build's `--test` stage runs the suite on it before an image exists. PyYAML is
pinned by hash; `pip` still reaches PyPI during a build.

## The config symlink

The container reads `conf/`, so it must exist with the config in it before the container first starts: **do not run `up -d`
before** (Docker would create `conf/` empty, owned by root, and the relay would refuse to start without a config). Make it with
`mkdir -m 700 ~/.local/state/cliproxy-8381-conf`, copy `core/config.yaml` into it, and replace the repository path with
a symlink to the copy (`ln -sfn ~/.local/state/cliproxy-8381-conf/config.yaml core/config.yaml`). The symlink is what
lets the native relay (`relay-up.sh`, the rollback) read the same file. `.gitignore` already covers `config.yaml`, symlink or not.
**Edit the real file, `~/.local/state/cliproxy-8381-conf/config.yaml`**, or use `sed -i --follow-symlinks` on the repo path: a plain
`sed -i` (or an editor that saves by replacing the file) on `core/config.yaml` replaces the symlink with a regular file,
the container keeps reading the old one and a `docker kill -s HUP` then reloads settings that no longer change, with no error.
`test -L core/config.yaml` says whether the link is still there; `cp -p <backup> core/config.yaml` writes
through it and is fine.

## Operate

All from the main checkout (`~/Sites/TOOL/lokki`), never from a worktree.

```
RELAY_SHA=<commit> docker compose -f deploy/docker-compose.relay-8381.yml up -d    # start, or upgrade to <commit>
docker ps -a --filter name=relay-8381             # state and health (-a also shows it after a stop)
docker logs --tail 20 relay-8381
docker kill -s HUP relay-8381                     # re-read config.yaml (look for "config: reloaded" in the log)
docker stop relay-8381                            # SIGTERM, 15 s drain; a manual stop: stays stopped until `docker start`, reboots included
docker rm relay-8381                              # remove it (what `compose down` does; that command needs RELAY_SHA too)
```

Check after any start: `curl -s http://127.0.0.1:8381/healthz`, and `/version` with an operator token shows the
revision and the configuration fingerprint (`revision` is the commit in the image's tag); the start line in the log
shows `runs=` (see above).

**Upgrade.** `git pull --ff-only origin main` in the checkout, `deploy/relay/build.sh --test <commit>`, then the
`up -d` line above with the new `RELAY_SHA`: Compose stops the old container (the 15 s drain, up to 30 s) and starts the
new one; for those seconds the port refuses connections, and a stream still running after 30 s (an image being made
takes 28 to 38 s) is cut. Do it when no stream is running.

**Roll back to the previous image:** the same `up -d` with the previous `RELAY_SHA`. **Back to the native relay:**
`docker stop relay-8381 && docker rm relay-8381`, then the `relay-up.sh` command in [`live-8381.md`](live-8381.md),
"Keeping it running": it reads `core/config.yaml`, which is the same file through the symlink, and the same run
log. The two cannot run together: the run log is a single-writer hash chain with a lock (`flock` on a file both see), and
the second to start refuses with status 2.

## At boot, and what it does not cover

With host networking there is no published port to be poisoned. At boot Docker starts the container before
`tailscale0` has its address; the relay requires every address in `listen:`, so it exits ("cannot bind", status 2) and
`restart: unless-stopped` starts it again, with Docker's growing back-off, until the address exists. That the
restarts go on until the bind succeeds follows from those two rules. Checked with a throwaway container
of this image (`--network host`, `--restart unless-stopped`, a required `listen:` address that the host does not have):
it exited with status 2 ("cannot bind ... Cannot assign requested address"), Docker restarted it 8 times in 25 seconds,
and nothing was left listening between tries (the loopback address it had bound is released). Not tried: a real reboot,
and the restart that follows once the address appears.

- **What the restart policy covers.** A crash: the container's init process (PID 1) killed with `kill -9` from the
  host, the relay answered again after 1.1 s. Not a manual stop: Docker's documentation says a manually stopped container
  is not restarted (`docker stop`), and a `docker kill -s KILL` was tried and left it down until `docker start`.
  (`docker kill -s HUP`, the reload, is a signal and not a stop.)
- The compose healthcheck is a `CMD-SHELL` that runs `/app/core/healthcheck.py` and, where that file is missing (an
  image built before 5192923), `/app/midstream-abort/healthcheck.py`. So the same `up -d` serves the image deployed
  before the move and any built after it. `core/test_compose_healthcheck.py` holds both paths.
- `/healthz` says the relay process serves. It does not look at the cliproxy behind it, so a healthy container over a
  dead cliproxy looks healthy. Docker marks a container `unhealthy` when the probe fails three times, and does nothing
  else about it: `restart: unless-stopped` acts on exits, not on health. Nothing alerts.
- One instance only: the run log is single-writer and the account is one.
- The memory limit (768 MiB) is a guess: the native relay held 29 MB; a shadow container held 17.7 MiB after a text
  session and one image answer, and the production container 35 MiB after a text session, an image and a reload (the
  peaks were not recorded); an image answer is allowed up to 128 MiB per image slot.
- `docker compose up -d` after the image or the compose file changed recreates the container; after nothing changed
  it does nothing. The cliproxy's image is not touched by anything here.
