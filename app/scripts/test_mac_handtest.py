#!/usr/bin/env python3
"""mac-update-handtest.sh's overlay is the one place a build names the insecure transport flag
and another public key; these tests hold what it writes and where. Its other subcommands act
on a Mac and are run by hand (docs/app/update.md › "Testing an install on macOS"); with the
Mac's tools stubbed, these tests also hold which signatures `before` and `after` verify on
either CPU, which check they add on Apple silicon, and what `download` and `clean` do with
the quarantined .dmg. A stub's answer models codesign; it proves nothing of macOS."""
import http.server
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SCRIPT = os.path.join(HERE, "mac-update-handtest.sh")
# A made-up public half; no key is generated off a Mac.
PUBKEY = "bm90IGEga2V5OyBhIHRlc3QncyBzdGFuZC1pbg=="


def _run(*args, env=None):
    return subprocess.run([shutil.which("bash"), SCRIPT, *args], capture_output=True, text=True, env=env)


class OverlayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="lokki-handtest-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.pubkey = os.path.join(self.tmp, "handtest.key.pub")
        with open(self.pubkey, "w", encoding="utf-8") as f:
            f.write(PUBKEY + "\n")

    def overlay(self, out):
        return _run("overlay", "--out", out, "--port", "40123", "--pubkey", self.pubkey, "--version", "99.0.0")

    def test_the_overlay_names_only_the_test_endpoint_its_key_and_the_version(self):
        out = os.path.join(self.tmp, "overlay.json")
        r = self.overlay(out)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(out, encoding="utf-8") as f:
            overlay = json.load(f)
        self.assertEqual(overlay, {
            "version": "99.0.0",
            "plugins": {"updater": {
                "pubkey": PUBKEY,
                "endpoints": ["http://127.0.0.1:40123/latest.json"],
                "dangerousInsecureTransportProtocol": True,
            }},
        })
        endpoint = urlsplit(overlay["plugins"]["updater"]["endpoints"][0])
        self.assertEqual((endpoint.scheme, endpoint.hostname), ("http", "127.0.0.1"))

    def test_the_overlay_is_refused_inside_the_checkout(self):
        # Through a link from outside too: the check reads where the file would really land.
        link = os.path.join(self.tmp, "checkout")
        os.symlink(os.path.join(REPO, "app"), link)
        for out in (os.path.join(REPO, "app", "handtest-overlay.json"), os.path.join(link, "handtest-overlay.json")):
            with self.subTest(out=out):
                r = self.overlay(out)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("outside the checkout", r.stderr)
                self.assertFalse(os.path.exists(os.path.join(REPO, "app", "handtest-overlay.json")))

    def test_an_option_without_its_value_prints_the_usage(self):
        for flag in ("--out", "--port", "--pubkey", "--version"):
            with self.subTest(flag=flag):
                r = _run("overlay", "--out", os.path.join(self.tmp, "o.json"), "--port", "1", "--pubkey", self.pubkey, flag)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("usage:", r.stderr)

    def test_an_unknown_subcommand_is_named_as_unknown_on_any_os(self):
        r = _run("install")
        self.assertEqual(r.returncode, 2)
        self.assertIn("install is not a subcommand", r.stderr)
        self.assertNotIn("macOS only", r.stderr)

    @unittest.skipIf(platform.system() == "Darwin", "a stub uname stands in for a Mac")
    def test_clean_refuses_before_it_stops_or_removes_anything(self):
        # uname answers Darwin, so clean runs here; HOME and the scratch directory are this
        # test's. The decoy is named http.server, as the server clean stops by its pid.
        bin_dir = os.path.join(self.tmp, "bin")
        os.mkdir(bin_dir)
        with open(os.path.join(bin_dir, "uname"), "w") as f:
            f.write("#!/bin/sh\necho Darwin\n")
        os.chmod(os.path.join(bin_dir, "uname"), 0o755)
        decoy = subprocess.Popen(["bash", "-c", "exec -a http.server sleep 300"])
        self.addCleanup(decoy.wait)
        self.addCleanup(decoy.kill)
        scratch = os.path.join(self.tmp, "scratch")
        copies = os.path.join(self.tmp, "Downloads", "lokki-handtest")
        download = os.path.join(self.tmp, "Downloads", "lokki-handtest-download")
        os.makedirs(scratch)
        os.makedirs(copies)
        os.makedirs(download)
        with open(os.path.join(scratch, "server.pid"), "w") as f:
            f.write(f"{decoy.pid}\n")
        env = {**os.environ, "PATH": bin_dir + os.pathsep + os.environ["PATH"], "HOME": self.tmp,
               "LOKKI_HANDTEST_DIR": scratch}
        # (label, the directory left unmarked)
        for label, unmarked in (("the Downloads copy", copies), ("the downloaded .dmg", download),
                                ("the scratch directory", scratch)):
            with self.subTest(label):
                for d in (scratch, copies, download):
                    mark = os.path.join(d, ".lokki-handtest")
                    if d == unmarked and os.path.exists(mark):
                        os.remove(mark)
                    elif d != unmarked:
                        open(mark, "w").close()
                r = _run("clean", env=env)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn(f"FAIL {unmarked} is not the test's", r.stdout)
                self.assertIsNone(decoy.poll(), "the server was stopped before the refusal")
                self.assertTrue(os.path.exists(os.path.join(scratch, "server.pid")))
                self.assertTrue(os.path.isdir(copies))
                self.assertTrue(os.path.isdir(download))

    @unittest.skipIf(platform.system() == "Darwin", "these run on a Mac")
    def test_every_other_subcommand_is_refused_off_macos(self):
        # PATH holds only uname, so a subcommand that got past the refusal could run nothing.
        bin_dir = os.path.join(self.tmp, "bin")
        os.mkdir(bin_dir)
        os.symlink(shutil.which("uname"), os.path.join(bin_dir, "uname"))
        env = {"PATH": bin_dir, "HOME": self.tmp}
        for args in (["build"], ["serve"], ["download"], ["before"], ["after"], ["copy", "dmg"], ["clean"]):
            with self.subTest(args=args):
                r = _run(*args, env=env)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("runs on macOS only", r.stderr)
                self.assertEqual(sorted(os.listdir(self.tmp)), ["bin", "handtest.key.pub"])


TOKEN = "op-token-for-the-test"


class _Core(http.server.BaseHTTPRequestHandler):
    """The installed core: /api/status answers 200 to the operator token only."""
    seen = []

    def do_GET(self):
        _Core.seen.append(self.path)
        ok = self.path == "/api/status" and self.headers.get("Authorization") == f"Bearer {TOKEN}"
        self.send_response(200 if ok else 401)
        self.end_headers()

    def log_message(self, *args):
        pass


def _stub(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, 0o755)


@unittest.skipIf(platform.system() == "Darwin", "stubs stand in for a Mac's tools")
class MacStepTests(unittest.TestCase):
    """The steps on an installed bundle whose tools are stubs: sysctl says which Mac this is,
    codesign and xattr log what they are asked, and a loopback server is the core."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="lokki-handtest-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        self.codesign_log = os.path.join(self.tmp, "codesign.log")
        # uname -m answers UNAME_M, arm64 unless a row says otherwise.
        _stub(os.path.join(self.bin, "uname"), 'case "$1" in -m) echo "${UNAME_M:-arm64}" ;; *) echo Darwin ;; esac\n')
        self.xattr_log = os.path.join(self.tmp, "xattr.log")
        _stub(os.path.join(self.bin, "xattr"), f'echo "$*" >>{self.xattr_log}\n')
        _stub(os.path.join(self.bin, "pgrep"), 'echo "${PGREP-4242}"\n')
        # A sealed build, modelled: `-dv` reads the same signature everywhere. Verifying the
        # bundle refuses it when REFUSE is `bundle`. A Mach-O is refused by its content when
        # REFUSE names it, and the shell is refused outside a bundle, since its signature names
        # the bundle's sealed resources.
        _stub(os.path.join(self.bin, "codesign"), f"""echo "$*" >>{self.codesign_log}
for path; do :; done
case "$1" in
  -dv) echo "Identifier=x" >&2; echo "Signature=adhoc" >&2 ;;
  -v | --verify)
    if [ -d "$path" ]; then
      [ "${{REFUSE:-}}" != bundle ] || {{ echo "a sealed resource is missing or invalid" >&2; exit 1; }}
      exit 0
    fi
    case "$path" in
      *.app/Contents/MacOS/*) ;;
      *) ! grep -qx "# lokki" "$path" || {{ echo "code has no resources but signature indicates they must be present" >&2; exit 1; }} ;;
    esac
    ! grep -qx "# ${{REFUSE:-}}" "$path" || {{ echo "invalid signature" >&2; exit 1; }} ;;
esac
""")
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Core)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        _Core.seen = []
        self.installed = os.path.join(self.tmp, "Applications", "lokki.app")
        self.scratch = os.path.join(self.tmp, "scratch")
        self.data = os.path.join(self.tmp, "data")
        self.port = self.server.server_address[1]
        macos = os.path.join(self.installed, "Contents", "MacOS")
        os.makedirs(macos)
        _stub(os.path.join(macos, "lokki"), "# lokki\nexit 0\n")
        _stub(os.path.join(macos, "cliproxy-x"), "# cliproxy-x\nexit 0\n")
        # The core records the environment it was given, and answers the port in core.port:
        # files, since shell-config gets no variable of the test's.
        self.core_env = os.path.join(self.tmp, "core.env")
        self.core_port = os.path.join(self.tmp, "core.port")
        with open(self.core_port, "w") as f:
            f.write(f"{self.port}\n")
        _stub(os.path.join(macos, "lokki-core-x"), f"""# lokki-core-x
[ "$1" = shell-config ] || exit 2
env >{self.core_env}
echo '{{"core_port": '"$(cat {self.core_port})"', "cliproxy_port": 1, "operator_token": "{TOKEN}"}}'
""")
        os.makedirs(os.path.join(self.scratch, "A", "lokki.app"))
        os.makedirs(os.path.join(self.scratch, "www"))
        os.makedirs(os.path.join(self.scratch, "B"))
        shutil.copytree(self.installed, os.path.join(self.scratch, "B", "lokki.app"))
        for name in ("port", ".lokki-handtest", os.path.join("www", "latest.json")):
            open(os.path.join(self.scratch, name), "w").close()
        os.makedirs(os.path.join(self.data, "logs"))
        with open(os.path.join(self.data, "config.yaml"), "w") as f:
            f.write("listen: x\n")
        with open(os.path.join(self.data, "logs", "app.log"), "w") as f:
            f.write("update install asked\n"
                    "core stopped, pid 1: drained=true killed=false\n"
                    "cliproxy stopped, pid 2: drained=true killed=false\n"
                    "update installed; restarting\n"
                    f"ready: core on port {self.port} pid 3, cliproxy on port 1 pid 4\n")

    def run_step(self, step, arm64, refuse="", env=None, trace=False):
        """`step` (a subcommand's function, or before, then after, for `after`) with sysctl
        answering `arm64`, or failing when it is None, and `env` added to the caller's
        environment; `trace` runs it under `set -x`. What only a Mac has is redefined after the
        script is sourced. Returns the result and each verify codesign was asked, as (its
        options, the path). For `after`, REFUSE reaches step 6 only."""
        sysctl = "exit 1\n" if arm64 is None else f'[ "$*" = "-n hw.optional.arm64" ] && echo {arm64}\n'
        _stub(os.path.join(self.bin, "sysctl"), sysctl)
        steps = {"after": "REFUSE= before >/dev/null; v=99.0.1; : >$CODESIGN_LOG; after"}.get(step, step)
        if trace:
            steps = "set -x\n" + steps
        for log in (self.codesign_log, self.xattr_log):
            if os.path.exists(log):
                os.remove(log)
        # Sourced with a subcommand the script knows, which runs nothing.
        script = f"""source "$SCRIPT" {step if step in ("build", "download", "before", "after", "clean") else "before"}
INSTALLED="$T_INSTALLED" DATA="$T_DATA" v=99.0.0
plist_version() {{ echo "$v"; }}
pids_of() {{ echo 4242; }}
exe_of() {{ echo "$INSTALLED/Contents/MacOS/$name"; }}
{steps}
"""
        env = {**os.environ, "PATH": self.bin + os.pathsep + os.environ["PATH"], "HOME": self.tmp,
               "LOKKI_HANDTEST_DIR": self.scratch, "SCRIPT": SCRIPT, "T_INSTALLED": self.installed,
               "T_DATA": self.data, "CODESIGN_LOG": self.codesign_log, "REFUSE": refuse, **(env or {})}
        r = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, env=env,
                           timeout=60)
        verified = []
        if os.path.exists(self.codesign_log):
            with open(self.codesign_log) as f:
                for words in (line.split() for line in f):
                    if words[0] in ("-v", "--verify"):
                        verified.append((" ".join(words[:-1]), words[-1]))
        return r, verified

    def test_steps_4_and_6_verify_the_sealed_bundle_and_each_sidecar_in_place_on_either_cpu(self):
        # The bundle whole, then each sidecar where it is; the shell only as part of the bundle.
        macos = os.path.join(self.installed, "Contents", "MacOS")
        expected = [("--verify --deep --strict", self.installed),
                    ("-v", os.path.join(macos, "cliproxy-x")), ("-v", os.path.join(macos, "lokki-core-x"))]
        for label, arm64 in (("Apple silicon", 1), ("Intel", 0), ("sysctl fails", None)):
            for step in ("before", "after"):
                with self.subTest(label, step=step):
                    _Core.seen = []
                    r, verified = self.run_step(step, arm64)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertEqual(verified, expected)
                    self.assertIn(f"PASS codesign --verify --deep --strict: {self.installed}", r.stdout)
                    for name in ("cliproxy-x", "lokki-core-x"):
                        self.assertIn(f"PASS codesign -v: {name}", r.stdout)
                    self.assertNotIn(TOKEN, r.stdout + r.stderr)
                    # Only Apple silicon asks the core.
                    if step == "after" and arm64 == 1:
                        self.assertIn(f"PASS the core answers /api/status on 127.0.0.1:{self.port}", r.stdout)
                        self.assertEqual(_Core.seen, ["/api/status"])
                    else:
                        self.assertEqual(_Core.seen, [])

    def test_a_refused_bundle_or_sidecar_fails_the_step_on_either_cpu(self):
        for arm64 in (1, 0):
            for refuse, line in (("bundle", f"FAIL codesign refuses {self.installed}: a sealed resource is missing or invalid"),
                                 ("cliproxy-x", "FAIL codesign -v refuses cliproxy-x: invalid signature")):
                for step in ("before", "after"):
                    with self.subTest(refuse, arm64=arm64, step=step):
                        r, _ = self.run_step(step, arm64, refuse=refuse)
                        self.assertNotEqual(r.returncode, 0)
                        self.assertIn(line, r.stdout)

    def test_step_4_prints_the_quarantine_attribute_the_approved_bundle_carries(self):
        r, _ = self.run_step("before", 1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.xattr_log) as f:
            self.assertIn(f"-p com.apple.quarantine {self.installed}\n", f.readlines())
        self.assertIn("the bundle's com.apple.quarantine: ", r.stdout)

    def test_the_operator_token_is_in_no_trace(self):
        # A trace prints every assignment and every command's arguments.
        r, _ = self.run_step("after", 1, trace=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"PASS the core answers /api/status on 127.0.0.1:{self.port}", r.stdout)
        self.assertIn("+ ", r.stderr)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)

    def test_shell_config_gets_only_what_the_shell_gives_the_core(self):
        # The loader reads RELAY_* and CLIPROXY_URL, so a Terminal's could change its answer.
        extra = {"RELAY_LISTEN": "127.0.0.1:1", "RELAY_TOKENS": "x", "CLIPROXY_URL": "http://x",
                 "LC_ALL": "C", "HTTPS_PROXY": "http://proxy"}
        r, _ = self.run_step("after", 1, env=extra)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.core_env) as f:
            names = {line.split("=", 1)[0] for line in f if "=" in line}
        self.assertFalse(names & {"RELAY_LISTEN", "RELAY_TOKENS", "CLIPROXY_URL", "SCRIPT", "REFUSE"}, names)
        self.assertLessEqual({"PATH", "HOME", "LC_ALL", "HTTPS_PROXY"}, names)

    def test_a_core_that_never_answers_fails_step_6_in_its_wait(self):
        # It accepts the connection and never answers.
        silent = socket.socket()
        self.addCleanup(silent.close)
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        with open(self.core_port, "w") as f:
            f.write(f"{silent.getsockname()[1]}\n")
        start = time.monotonic()
        r, _ = self.run_step("after", 1, env={"LOKKI_HANDTEST_STATUS_WAIT": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"FAIL /api/status answers nothing on 127.0.0.1:{silent.getsockname()[1]}", r.stdout)
        self.assertLess(time.monotonic() - start, 30)

    def test_a_rosetta_shell_fails_at_step_0_and_a_native_or_intel_one_goes_on(self):
        # (label, hw.optional.arm64, uname -m, the step 0 line)
        rows = (
            ("Rosetta", 1, "x86_64", "FAIL this Mac is Apple silicon, but this shell runs under Rosetta "
             "(uname -m says x86_64): open a Terminal that runs natively, then run build again"),
            ("native", 1, "arm64", "this Mac is Apple silicon: steps 4 and 6 verify the bundle's signature "
             "and each sidecar's, and step 6 also asks the core's /api/status"),
            ("Intel", 0, "x86_64", "this Mac has an Intel CPU: steps 4 and 6 verify the bundle's signature "
             "and each sidecar's"),
        )
        for label, arm64, machine, line in rows:
            with self.subTest(label):
                r, _ = self.run_step("build", arm64, env={"UNAME_M": machine, "PGREP": ""})
                self.assertIn(line, r.stdout.splitlines())
                self.assertEqual("PASS no lokki process" in r.stdout, label != "Rosetta", r.stdout)
                self.assertNotEqual(r.returncode, 0)  # the scratch directory exists, later in step 0

    def test_the_build_drops_every_apple_signing_variable_before_it_builds(self):
        # Tauri signs with APPLE_SIGNING_IDENTITY or APPLE_CERTIFICATE, and notarizes with
        # APPLE_ID or APPLE_API_KEY, over what tauri.conf.json says.
        extra = {"APPLE_SIGNING_IDENTITY": "Developer ID Application: x", "APPLE_CERTIFICATE": "x",
                 "APPLE_ID": "x", "APPLES": "kept"}
        r, _ = self.run_step('drop_apple_signing; env; declare -f build', 1, env=extra)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        names = {line.split("=", 1)[0] for line in r.stdout.splitlines() if "=" in line and not line.startswith(" ")}
        self.assertFalse({n for n in names if n.startswith("APPLE_")}, names)
        self.assertIn("APPLES", names)
        for name in ("APPLE_SIGNING_IDENTITY", "APPLE_CERTIFICATE", "APPLE_ID"):
            self.assertIn(f"unset {name}: the test build is signed as a release is", r.stdout)
        build = r.stdout[r.stdout.index("build ()"):]
        self.assertLess(build.index("drop_apple_signing"), build.index("tauri build"))
        self.assertNotIn("--no-sign", build)

    def test_download_puts_a_dmg_in_downloads_as_a_browser_leaves_it(self):
        dmg = os.path.join(self.scratch, "A", "lokki-99.0.0.dmg")
        with open(dmg, "w") as f:
            f.write("a disk image\n")
        folder = os.path.join(self.tmp, "Downloads", "lokki-handtest-download")
        r, _ = self.run_step("download", 1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        copy = os.path.join(folder, "lokki-99.0.0.dmg")
        with open(copy) as f:
            self.assertEqual(f.read(), "a disk image\n")
        self.assertTrue(os.path.isfile(os.path.join(folder, ".lokki-handtest")))
        with open(self.xattr_log) as f:
            (call,) = f.read().splitlines()
        self.assertRegex(call, r"^-w com\.apple\.quarantine 0081;[0-9a-f]+;Safari; " + re.escape(copy) + "$")
        self.assertIn(f"PASS {copy}, quarantined as a browser leaves it", r.stdout)
        # A second download is refused, and clean removes the marked folder.
        r, _ = self.run_step("download", 1)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"FAIL {folder} exists: run clean first", r.stdout)
        r, _ = self.run_step("clean", 1, env={"T_INSTALLED": os.path.join(self.tmp, "none", "lokki.app")})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PASS no downloaded .dmg", r.stdout)
        self.assertFalse(os.path.exists(folder))

    def test_a_core_that_does_not_take_the_token_fails_step_6(self):
        # The token shell-config prints is not the one the core takes.
        core = os.path.join(self.installed, "Contents", "MacOS", "lokki-core-x")
        with open(core) as f:
            body = f.read().replace(TOKEN, "another-token")
        _stub(core, body.removeprefix("#!/bin/sh\n"))
        shutil.copy(core, os.path.join(self.scratch, "B", "lokki.app", "Contents", "MacOS"))
        r, _ = self.run_step("after", 1)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"FAIL /api/status answers 401 on 127.0.0.1:{self.port}", r.stdout)


if __name__ == "__main__":
    unittest.main()
