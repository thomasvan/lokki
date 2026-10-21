#!/usr/bin/env python3
"""Build the core sidecar: the UI, then PyInstaller's one-file build of core_entry.py.
`--run` then exercises the built file once. The build environment must not have lokki-core
installed, or the bundle would read an installed version instead of its own VERSION."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sidecars  # noqa: E402

REPO = sidecars.REPO
TIMEOUT = 30


def args(core_dir, version, triple, out, revision_dir):
    """The PyInstaller command line."""
    core = os.path.abspath(core_dir)
    sep = os.pathsep
    return [
        "pyinstaller", "--noconfirm", "--onefile",
        "--name", f"{sidecars.core_name(version)}-{triple}",
        "--paths", core,
        "--add-data", os.path.join(core, "lokki_core", "static") + sep + "lokki_core/static",
        "--add-data", os.path.join(core, "VERSION") + sep + ".",
        "--add-data", os.path.join(os.path.abspath(revision_dir), "REVISION") + sep + ".",
        "--hidden-import", "yaml",
        "--distpath", os.path.abspath(out),
        os.path.join(HERE, "core_entry.py"),
    ]


def revision():
    sha = os.environ.get("GITHUB_SHA", "").strip()
    if sha:
        return sha
    return subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def _npm():
    return "npm.cmd" if sys.platform == "win32" else "npm"


def build(out, triple):
    core_version, _ = sidecars.read_versions(REPO)
    try:
        subprocess.run([_npm(), "run", "build"], cwd=os.path.join(REPO, "ui"), check=True)
    except (subprocess.CalledProcessError, OSError) as e:
        raise SystemExit(f"`npm run build` in ui/ failed ({e}); run `npm ci` there first")
    with tempfile.TemporaryDirectory(prefix="lokki-revision-") as rev, tempfile.TemporaryDirectory(prefix="lokki-pyi-") as work:
        with open(os.path.join(rev, "REVISION"), "w", encoding="utf-8") as f:
            f.write(revision() + "\n")
        cmd = args(os.path.join(REPO, "core"), core_version, triple, out, rev)
        # PyInstaller's build and spec files go to a temp directory, not the checkout.
        subprocess.run(cmd[:1] + ["--workpath", os.path.join(work, "build"), "--specpath", work] + cmd[1:], check=True)
    exe = ".exe" if "windows" in triple else ""
    return os.path.join(os.path.abspath(out), f"{sidecars.core_name(core_version)}-{triple}{exe}")


def _env():
    """The parent's environment without the core's own knobs: a RELAY_REVISION would hide a
    bundle that lost its REVISION file."""
    return {k: v for k, v in os.environ.items() if not k.startswith(("RELAY_", "CLIPROXY_", "CLI_PROXY"))}


def _call(exe, *argv, data_dir):
    done = subprocess.run([exe, *argv, "--data-dir", data_dir], capture_output=True, text=True, timeout=TIMEOUT, env=_env(), stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise SystemExit(f"{os.path.basename(exe)} {argv[0]} exited {done.returncode}: {done.stderr.strip()}")
    return done.stdout


def _http(method, url, token=None):
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    if token:
        req.add_header("X-Relay-Token", token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=10) as r:
        return r.status, r.read()


def run(exe):
    """init with ports 0, shell-config, serve until /healthz, /api/status, /api/shutdown.
    The token stays in memory: nothing here prints it."""
    core_version, _ = sidecars.read_versions(REPO)
    with tempfile.TemporaryDirectory(prefix="lokki-bundle-run-") as data:
        init = json.loads(_call(exe, "init", "--core-port", "0", "--cliproxy-port", "0", data_dir=data))
        print(f"init: created={init['created']} core_port={init['core_port']}")
        conf = json.loads(_call(exe, "shell-config", data_dir=data))
        base = f"http://127.0.0.1:{conf['core_port']}"
        proc = subprocess.Popen([exe, "--config", os.path.join(data, "config.yaml")], env=_env(), cwd=data,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + TIMEOUT
            while True:
                try:
                    if _http("GET", base + "/healthz")[0] == 200:
                        break
                except (urllib.error.URLError, OSError):
                    pass
                if proc.poll() is not None:
                    raise SystemExit(f"the core exited {proc.returncode} before /healthz answered")
                if time.monotonic() > deadline:
                    raise SystemExit(f"/healthz did not answer 200 within {TIMEOUT} s")
                time.sleep(0.2)
            status = json.loads(_http("GET", base + "/api/status", conf["operator_token"])[1])
            if status.get("version") != core_version:
                raise SystemExit(f"/api/status version is {status.get('version')!r}, not the bundled {core_version!r}")
            rev = status.get("revision")
            if not (isinstance(rev, str) and 7 <= len(rev) <= 64 and all(c in "0123456789abcdef" for c in rev)):
                raise SystemExit("/api/status carries no hex revision: REVISION is not at the bundle's root")
            print(f"status: version={status['version']} revision={rev}")
            code = _http("POST", base + "/api/shutdown", conf["operator_token"])[0]
            if code != 202:
                raise SystemExit(f"POST /api/shutdown answered {code}, not 202")
            proc.wait(timeout=TIMEOUT)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", required=True)
    p.add_argument("--triple", required=True)
    p.add_argument("--run", action="store_true")
    a = p.parse_args(argv)
    exe = build(a.out, a.triple)
    print(exe)
    if a.run:
        run(exe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
