#!/usr/bin/env python3
"""app.yml's release matrix and its --require list are deliberate copies of
release_manifest.PLATFORMS; this test holds them equal, so a platform cannot be added to
one and forgotten in the other. It also holds the release boundary: no build app.yml makes
can carry the updater's insecure transport flag or another public key; every macOS build
is sealed ad hoc without hardened runtime; and every workflow keeps a public repository's
supply chain closed to a fork's pull request."""
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import release_manifest as rm  # noqa: E402

WORKFLOWS_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)), ".github", "workflows")
WORKFLOW = os.path.join(WORKFLOWS_DIR, "app.yml")
SRC_TAURI = os.path.join(os.path.dirname(HERE), "src-tauri")
# What `tauri build` merges into every build: tauri.conf.json and any platform file beside it.
CONFIG_FILES = sorted(glob.glob(os.path.join(SRC_TAURI, "[Tt]auri*.conf.json*"))
                      + glob.glob(os.path.join(SRC_TAURI, "Tauri*.toml")))
SIDECARS_OUT = '"$RUNNER_TEMP/tauri.json"'


def _job(text, name):
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [\w-]+:\n|\Z)", text, re.S | re.M)
    if not m:
        raise AssertionError(f"app.yml has no job {name!r}")
    return m.group(1)


def _text():
    with open(WORKFLOW, encoding="utf-8") as f:
        return f.read()


def _indent(line):
    return len(line) - len(line.lstrip())


def _run_blocks(text):
    """Every step's `run:` body, a one-line value or a block scalar's lines."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"(\s*)(- )?run:\s*(.*)", line)
        if not m:
            continue
        key = len(m.group(1)) + (2 if m.group(2) else 0)
        body = [m.group(3)]
        for nxt in lines[i + 1:]:
            if nxt.strip() and _indent(nxt) <= key:
                break
            body.append(nxt)
        yield "\n".join(body)


class WorkflowTests(unittest.TestCase):
    def test_the_workflow_names_every_platform_once(self):
        text = _text()
        prepare, linux, hosted, publish = (_job(text, n) for n in ("prepare", "release-linux", "release-hosted", "publish"))
        keys = {key for key, _ in rm.PLATFORMS.values()}
        linux_triple = next(t for t in rm.PLATFORMS if "linux" in t)
        linux_key, linux_asset = rm.PLATFORMS[linux_triple]
        self.assertIn(f"--target {linux_triple}", linux)
        # The Linux job uploads the AppImage and its signature under the name the manifest looks for.
        for name in (linux_asset, linux_asset + ".sig"):
            self.assertIn(name.format(version="${VERSION}"), linux)
        # The hosted legs: each triple once, with the arch and bundles that produce the asset PLATFORMS expects.
        legs = re.findall(r'\{ os: [^,]+, triple: ([^\s,}]+), bundles: "([^"]+)", arch: ([^\s,}]+) \}', hosted)
        self.assertEqual(sorted(t for t, _, _ in legs), sorted(t for t in rm.PLATFORMS if "linux" not in t))
        for triple, bundles, arch in legs:
            asset = rm.PLATFORMS[triple][1].format(version="${VERSION}")
            self.assertIn(f"_{arch}", asset, f"{triple}: arch {arch} is not in {asset}")
            upload = asset.replace(f"_{arch}", "_${ARCH}", 1)
            # Quoted whole, so the .sig's line cannot stand in for the installer's.
            for name in (upload, upload + ".sig"):
                self.assertIn(f'/{name}"', hosted)
            self.assertIn("app" if "darwin" in triple else "nsis", bundles.split(","))
            if "darwin" in triple:
                # The updater archive has no version or arch in its name; the rename adds both, and its signature with it.
                for suffix in ("", ".sig"):
                    self.assertIn(f'mv macos/lokki.app.tar.gz{suffix} "macos/{upload}{suffix}"', hosted)
        # publish requires every platform, or Linux alone on a deliberate Linux-only release; --require carries it.
        m = re.search(r'if \[ "\$PLATFORMS" = linux \]; then REQ=([a-z0-9_,-]+); else REQ=([a-z0-9_,-]+); fi', publish)
        self.assertIsNotNone(m, "publish does not choose REQ by $PLATFORMS")
        self.assertEqual(set(m.group(1).split(",")), {linux_key})
        self.assertEqual(set(m.group(2).split(",")), keys)
        self.assertIn('--require "$REQ"', publish)
        # prepare lists the releases with their draft flag into the file --release-action reads, takes
        # the decision whole (nothing after the closing `)"` may swallow a refusal), edits a reused
        # draft's notes, and otherwise creates a draft on a tag that exists; a failing pipe fails the step.
        listed = re.search(r'^\s*gh release list [^\n|;&]*--json tagName,isDraft [^\n|;&]*> "\$RUNNER_TEMP/(\S+)"$', prepare, re.M)
        self.assertIsNotNone(listed, "prepare does not list the releases with tagName,isDraft into a file")
        self.assertRegex(prepare, r'(?m)^\s*ACTION="\$\(python3 release_manifest\.py --release-action "\$TAG" --releases "\$RUNNER_TEMP/'
                         + re.escape(listed.group(1)) + r'"\)"$')
        branches = re.search(r'if \[ "\$ACTION" = reuse \]; then\n\s*(gh release edit[^\n]*)\n\s*else\n\s*(gh release create[^\n]*)\n\s*fi', prepare)
        self.assertIsNotNone(branches, "prepare does not edit on reuse and create otherwise")
        self.assertIn("--notes-file", branches.group(1).split())
        self.assertLessEqual({"--draft", "--verify-tag"}, set(branches.group(2).split()))
        self.assertIn("set -o pipefail", prepare)
        # A dispatch on a tag ref with no tag input is a CI run, not a release.
        self.assertIn("github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')", prepare)

    def test_the_notes_have_a_line_per_merged_pull_request(self):
        # Each merge into main has main as its first parent: following first parents gives a line
        # per merge, and per commit pushed to main itself, never the commits a pull request carried.
        logs = re.findall(r"git log [^|;}]*", _job(_text(), "prepare"))
        self.assertEqual(len(logs), 1, logs)
        self.assertIn("--first-parent", logs[0].split())

    def test_inputs_and_signing_secrets_reach_only_where_they_are_safe(self):
        # A workflow input in a `run:` line is shell injection; the signing key belongs to the
        # build-and-sign step's `env` and nowhere a log, an upload or another step could read it.
        text = _text()
        # With the secret check because `set -x` would echo the expanded env, signing secrets included.
        self.assertNotIn("set -x", text)
        for body in _run_blocks(text):
            self.assertIsNone(re.search(r"\$\{\{[^}]*\binputs\b", body), f"an input in a run: line:\n{body}")
        steps = re.split(r"(?m)^(?=      - )", text)
        holders = [c for c in steps if "secrets.TAURI_SIGNING_PRIVATE_KEY" in c]
        self.assertEqual(len(holders), 2)
        for chunk in holders:
            self.assertIn("name: Build and sign the installers\n", chunk)
            self.assertNotIn("npm ci", chunk)
            lines = chunk.splitlines()
            for i, line in enumerate(lines):
                if "secrets." not in line:
                    continue
                parent = next(l for l in reversed(lines[:i]) if l.strip() and _indent(l) < _indent(line))
                self.assertEqual(parent.strip(), "env:")
                self.assertRegex(line.strip(), r"^(TAURI_SIGNING_PRIVATE_KEY(_PASSWORD)?): \$\{\{ secrets\.\1 \}\}$")
        self.assertEqual(text.count("secrets.TAURI_SIGNING_PRIVATE_KEY"), 4)

    def test_every_uploaded_artifact_names_how_long_it_is_kept(self):
        # The repository's artifact quota is finite and the default keeps a build 90 days.
        steps = re.split(r"(?m)^(?=      - )", _text())
        uploads = [c for c in steps if "actions/upload-artifact@" in c]
        self.assertEqual(len(uploads), 2)
        for chunk in uploads:
            self.assertRegex(chunk, r"(?m)^ +retention-days: \$\{\{ github\.event_name == 'pull_request' && 1 \|\| 7 \}\}$")


def _sidecars_overlay(*flags):
    """The text `sidecars.py --config-json` writes with these flags."""
    with tempfile.TemporaryDirectory(prefix="lokki-release-") as tmp:
        out = os.path.join(tmp, "tauri.json")
        subprocess.run([sys.executable, os.path.join(HERE, "sidecars.py"), "--config-json", *flags, "--out", out],
                       check=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        with open(out, encoding="utf-8") as f:
            return f.read()


def _tauri_builds(text):
    """(the step's run: body, the `tauri build` line's arguments) for each build app.yml runs."""
    for body in _run_blocks(text):
        for command in re.split(r"&&|;|\n", body):
            if "tauri build" in command:
                words = shlex.split(command)
                yield body, words[words.index("build") + 1:]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class ReleaseBoundaryTests(unittest.TestCase):
    """A build-time overlay that names the insecure transport flag, another endpoint or another
    public key is a different build, made only by the macOS hand test's script; these tests keep
    every build app.yml makes free of one, so a release accepts only `https` and only the
    configured key. They copy the boundary on purpose: moving it takes an edit here too."""

    def test_no_release_config_names_a_dangerous_updater_key(self):
        # `dangerous` in any case covers dangerousInsecureTransportProtocol, its kebab alias and
        # the plugin's other dangerous… switches.
        self.assertIn(os.path.join(SRC_TAURI, "tauri.conf.json"), CONFIG_FILES)
        sources = {path: _read(path) for path in CONFIG_FILES}
        sources["sidecars.py --config-json"] = _sidecars_overlay()
        sources["sidecars.py --config-json --release"] = _sidecars_overlay("--release")
        sources[WORKFLOW] = _text()
        for name, text in sources.items():
            self.assertEqual([line for line in text.splitlines() if "dangerous" in line.lower()], [], name)
        # Tauri reads TAURI_CONFIG as one more overlay; no step may set it.
        self.assertNotIn("TAURI_CONFIG", sources[WORKFLOW])

    def test_the_release_builds_take_one_config_and_it_names_no_plugin(self):
        text = _text()
        for flags in ([], ["--release"]):
            self.assertNotIn("plugins", json.loads(_sidecars_overlay(*flags)), flags)
        # Only tauri.conf.json, whose pubkey and endpoint are the release's, configures a plugin.
        for path in CONFIG_FILES:
            if os.path.basename(path) != "tauri.conf.json":
                self.assertNotIn("plugins", _read(path), path)
        builds = list(_tauri_builds(text))
        for job in ("linux", "release-linux", "release-hosted"):
            self.assertIn("tauri build", _job(text, job), job)
        self.assertEqual(len(builds), text.count("tauri build"), "a tauri build line this test did not read")
        release_jobs = _job(text, "release-linux") + _job(text, "release-hosted")
        for body, args in builds:
            configs = [a for a in args if a in ("--config", "-c") or a.startswith(("--config=", "-c"))]
            self.assertEqual(configs, ["--config"], body)
            # The one --config is the file sidecars.py --config-json writes just before, in the same step.
            self.assertEqual(args[args.index("--config") + 1], shlex.split(SIDECARS_OUT)[0], body)
            written = re.search(r"python3? scripts/sidecars\.py --config-json( --release)? --out " + re.escape(SIDECARS_OUT), body)
            self.assertIsNotNone(written, body)
            self.assertLess(written.start(), body.index("tauri build"), body)
            if body in release_jobs:
                self.assertEqual(written.group(1), " --release", body)



def _handtest_overlay():
    """The text mac-update-handtest.sh's overlay writes."""
    with tempfile.TemporaryDirectory(prefix="lokki-release-") as tmp:
        pubkey, out = os.path.join(tmp, "key.pub"), os.path.join(tmp, "overlay.json")
        with open(pubkey, "w", encoding="utf-8") as f:
            f.write("a stand-in key\n")
        subprocess.run(["bash", os.path.join(HERE, "mac-update-handtest.sh"), "overlay", "--out", out,
                        "--port", "1", "--pubkey", pubkey, "--version", "99.0.0"], check=True)
        return _read(out)


class MacSigningTests(unittest.TestCase):
    """Every macOS build is sealed ad hoc without hardened runtime (docs/app/update.md › "How a
    macOS build is signed"). tauri.conf.json says so, and nothing a build reads may say
    otherwise: no overlay, and no workflow variable Tauri signs or notarizes with. Tauri 2.12.2
    takes APPLE_SIGNING_IDENTITY over signingIdentity and APPLE_PROVIDER_SHORT_NAME over
    providerShortName (tauri-cli's interface/rust.rs), signs with APPLE_CERTIFICATE and
    APPLE_CERTIFICATE_PASSWORD, and notarizes with APPLE_ID, APPLE_PASSWORD and APPLE_TEAM_ID or
    APPLE_API_KEY, APPLE_API_ISSUER and APPLE_API_KEY_PATH (tauri-bundler's bundle/macos/sign.rs;
    tauri-cli's ENVIRONMENT_VARIABLES.md lists them all). Every one is named APPLE_…, so no
    workflow may name one; `--no-sign` would skip the seal."""

    def test_the_base_config_seals_every_macos_build_ad_hoc_without_hardened_runtime(self):
        macos = json.loads(_read(os.path.join(SRC_TAURI, "tauri.conf.json")))["bundle"]["macOS"]
        self.assertEqual(macos.get("signingIdentity"), "-")
        self.assertIs(macos.get("hardenedRuntime"), False)

    def test_no_overlay_names_the_macos_bundle_settings(self):
        overlays = {path: _read(path) for path in CONFIG_FILES if os.path.basename(path) != "tauri.conf.json"}
        overlays["sidecars.py --config-json"] = _sidecars_overlay()
        overlays["sidecars.py --config-json --release"] = _sidecars_overlay("--release")
        overlays["mac-update-handtest.sh overlay"] = _handtest_overlay()
        for name, text in overlays.items():
            for key in ("macOS", "signingIdentity", "hardenedRuntime", "signing-identity", "hardened-runtime"):
                self.assertNotIn(key, text, name)

    def test_no_workflow_sets_a_variable_tauri_signs_with_or_skips_the_seal(self):
        workflows = sorted(glob.glob(os.path.join(WORKFLOWS_DIR, "*.y*ml")))
        self.assertIn(WORKFLOW, workflows)
        for path in workflows:
            text = _read(path)
            self.assertEqual(re.findall(r"APPLE_\w*", text), [], path)
            self.assertNotIn("--no-sign", text, path)
            self.assertNotIn("TAURI_CONFIG", text, path)
        self.assertNotIn("--no-sign", _read(os.path.join(HERE, "mac-update-handtest.sh")))


class SupplyChainTests(unittest.TestCase):
    """The repository is public: anyone's pull request runs these workflows. So no job runs on
    a self-hosted runner, where a fork's code would run on the host; nothing runs a pull
    request's code with the base repository's token and secrets (`pull_request_target`); every
    action is the commit it was reviewed at; and a secret is read only by the release jobs,
    which run on a tag or a dispatch and never on a pull request."""

    HOSTED = r"(ubuntu|macos|windows)-[\w.-]+"

    def _workflows(self):
        paths = sorted(glob.glob(os.path.join(WORKFLOWS_DIR, "*.y*ml")))
        self.assertIn(WORKFLOW, paths)
        return {os.path.basename(p): _read(p) for p in paths}

    def test_every_job_runs_on_a_hosted_runner(self):
        for name, text in self._workflows().items():
            self.assertNotIn("self-hosted", text, name)
            for job_name, job in yaml.safe_load(text)["jobs"].items():
                where = f"{name}: {job_name}"
                runs_on = job.get("runs-on")
                labels = self._matrix_os(job, where) if runs_on == "${{ matrix.os }}" else [runs_on]
                for label in labels:
                    # A list, a `group:` or `labels:` mapping is refused whole: a hosted runner is one label.
                    self.assertIsInstance(label, str, f"{where}: runs-on {label!r}")
                    self.assertRegex(label, rf"^{self.HOSTED}$", where)

    def _matrix_os(self, job, where):
        """The labels a job's matrix gives `os`: one, a list, the quoted ones in a fromJSON
        expression, and each `include` entry's."""
        matrix = job.get("strategy", {}).get("matrix", {})
        os_ = matrix.get("os")
        labels = os_ if isinstance(os_, list) else [] if os_ is None else [os_]
        if isinstance(os_, str) and os_.startswith("${{"):
            labels = re.findall(r'"([^"]+)"', os_)
        labels += [entry["os"] for entry in matrix.get("include", []) if "os" in entry]
        self.assertTrue(labels, f"{where}: runs-on is matrix.os, and the matrix names none")
        return labels

    def test_no_workflow_runs_a_pull_request_with_the_base_repositorys_secrets(self):
        for name, text in self._workflows().items():
            self.assertNotIn("pull_request_target", text, name)
            self.assertNotIn("workflow_run", text, name)

    def test_every_action_is_pinned_by_its_full_commit_with_the_version_beside_it(self):
        for name, text in self._workflows().items():
            for line in re.findall(r"(?m)^\s*(?:- )?uses:.*$", text):
                self.assertRegex(line.strip(), r"^(- )?uses: [\w.-]+/[\w./-]+@[0-9a-f]{40} # \S+", name)

    def test_secrets_are_read_only_by_the_release_jobs(self):
        release = ("release-linux", "release-hosted")
        for name, text in self._workflows().items():
            if name != os.path.basename(WORKFLOW):
                self.assertNotIn("secrets.", text, name)
        text = _text()
        outside = text
        for job in release:
            body = _job(text, job)
            # prepare runs only on a tag push or a dispatch with a tag (WorkflowTests holds its if:).
            self.assertRegex(body, r"(?m)^    needs: prepare$", job)
            outside = outside.replace(body, "")
        self.assertNotIn("secrets.", outside)


APT_INSTALL = os.path.join(HERE, "apt-install.sh")
APT_INSTALL_RUN = "bash app/scripts/apt-install.sh"
# apt, apt-get or dpkg by name or by path, not a path or file named after one (`~/apt-debs`, `apt-install.sh`).
APT_CALL = re.compile(r"(?<![\w-])(?:apt-get|apt|dpkg)(?![\w.-])")


def _bounded_fetches():
    """(where, the fetch's shell text, the timeout of the shortest job that runs it) for each
    fetch from the apt mirror a workflow makes: every step of every workflow that names apt,
    apt-get or dpkg or runs Playwright's `--with-deps`, and apt-install.sh for the jobs that run it."""
    fetches, script = [], []
    for path in sorted(glob.glob(os.path.join(WORKFLOWS_DIR, "*.y*ml"))):
        for job_id, job in yaml.safe_load(_read(path))["jobs"].items():
            for step in job.get("steps", []):
                run = step.get("run", "")
                where = f"{os.path.basename(path)} {job_id} {step.get('name', run[:40])}"
                if APT_INSTALL_RUN in run:
                    script.append((job.get("timeout-minutes") or 0, job_id))
                if APT_CALL.search(run) or "--with-deps" in run:
                    fetches.append((where, run, job.get("timeout-minutes")))
    if script:
        fetches.append(("apt-install.sh", _read(APT_INSTALL), min(script)[0] or None))
    return fetches


class BoundedFetchTests(unittest.TestCase):
    """The apt mirror a runner fetches from can crawl for longer than a job may run. Every fetch
    from it, wherever a workflow makes one, has one bounded attempt around the fetch itself,
    called twice, the second through another mirror written between them. What a killed attempt
    leaves is killed, and its dpkg run finished within a bound. The sum stays well inside the
    job's timeout, and the retry failing fails loudly. No attempt lets apt install what it cannot
    authenticate."""

    def test_apt_installs_only_what_it_can_authenticate(self):
        for where, run, _ in _bounded_fetches():
            # Option and configuration spellings alike: `--allow-unauthenticated`, `APT::Get::AllowUnauthenticated`.
            flat = run.lower().replace("-", "")
            for bypass in ("trusted=yes", "allowunauthenticated", "allowinsecure"):
                self.assertNotIn(bypass, flat, where)

    def test_a_fetch_is_bounded_retried_once_through_another_mirror_and_inside_the_job_timeout(self):
        fetches = _bounded_fetches()
        # The search finds today's fetches, so a search that finds nothing cannot pass.
        self.assertLessEqual({"ui.yml", "apt-install.sh"}, {where.split()[0] for where, _, _ in fetches})
        for where, run, timeout_minutes in fetches:
            with self.subTest(where):
                self.assertIsNotNone(timeout_minutes, "a job that fetches has a timeout")
                timed = re.findall(r'timeout --kill-after=(\d+) "\$(?:1|bound)" (.*)', run)
                self.assertEqual(len(timed), 1, "one bounded attempt, called twice")
                grace, fetch = timed[0]
                self.assertRegex(fetch, r"^(npx playwright install --with-deps |sh -c 'apt-get update && apt-get install -y )")
                attempts = list(re.finditer(r'(?m)^\s*(?:if ! )?attempt (\d+)(?: "\$@")?[;\s|]', run))
                bounds = [int(m.group(1)) for m in attempts]
                self.assertEqual(len(bounds), 2, "one try and one retry")
                self.assertLessEqual(max(bounds), 300)
                self.assertIn("""gone() { for _ in $(seq "$1"); do pgrep -x 'apt|apt-get|dpkg' >/dev/null || return 0; sleep 2; done; return 1; }""", run)
                self.assertIn('if sessions=$(pgrep -d, -x dpkg); then sudo pkill -KILL -s "$sessions" || true; fi', run)
                self.assertIn("sudo pkill -KILL -x 'apt|apt-get|dpkg' || true", run)
                finish = re.findall(r"sudo timeout --kill-after=(\d+) (\d+) dpkg --configure -a", run)
                self.assertEqual(len(finish), 1, "the killed attempt's dpkg run is finished within a bound")
                # Two attempts with their kill grace, the waits before and after the kill (35 x 2 s) and the finish leave most
                # of the job for the rest.
                worst = (sum(bounds) + 2 * int(grace) + 70 + sum(int(n) for n in finish[0])) / 60
                self.assertLess(worst, timeout_minutes / 3)
                # The retry's mirror is written between the two attempts and put back when the step ends.
                trap = run.index("trap restore EXIT")
                mirror = run.index("https://archive.ubuntu.com/ubuntu/")
                self.assertLess(attempts[0].start(), trap)
                self.assertLess(trap, mirror)
                self.assertLess(mirror, attempts[1].start())
                # The retry failing fails the step, with the reason in the run's annotations.
                self.assertRegex(run, r'(?m)^\s*attempt \d+(?: "\$@")? \|\| \{ echo "::error::[^"]+"; exit [1-9]\d*; \}$')


class LinuxPackagesTests(unittest.TestCase):
    """app.yml's two Linux jobs install their packages through apt-install.sh, the one bounded
    fetch: nothing in app.yml calls apt itself, and the script calls apt-get only in its bounded
    attempt. The CI job hands apt an earlier run's .deb files, cached for its image and packages,
    when it has them. The release job takes no cache: a release builds from its tag and the
    archive alone. It installs the packages the CI job built with, less the smoke's display."""

    SMOKE = {"xvfb", "xauth", "dbus"}

    def setUp(self):
        self.jobs = yaml.safe_load(_text())["jobs"]

    def _install(self, job):
        runs = [s for s in self.jobs[job]["steps"] if APT_INSTALL_RUN in s.get("run", "")]
        self.assertEqual(len(runs), 1, job)
        return runs[0]

    def _packages(self, job):
        words = shlex.split(self._install(job)["run"])
        packages = words[words.index("app/scripts/apt-install.sh") + 1:]
        if packages[:1] == ["--debs"]:
            packages = packages[2:]
        if packages == ["$LINUX_PACKAGES"]:
            packages = self.jobs[job]["env"]["LINUX_PACKAGES"].split()
        return packages

    def test_nothing_in_app_yml_calls_apt_but_the_script(self):
        for body in _run_blocks(_text()):
            self.assertIsNone(APT_CALL.search(body), body)
        code = [l for l in _read(APT_INSTALL).splitlines() if not l.lstrip().startswith("#")]
        calls = [l for l in code if re.search(r"(?<![\w-])apt(-get)? +[a-z]", re.sub(r'"::error::[^"]*"', "", l))]
        self.assertEqual(len(calls), 1, calls)
        self.assertRegex(calls[0], r"""^\s*sudo timeout --kill-after=\d+ "\$bound" sh -c 'apt-get update && apt-get install -y "\$@"' """)

    def test_the_release_installs_what_ci_built_with_less_the_smokes_display_and_no_cache(self):
        ci, release = set(self._packages("linux")), set(self._packages("release-linux"))
        self.assertLessEqual({"libwebkit2gtk-4.1-dev", "libgtk-3-dev", "patchelf"}, ci)
        self.assertLessEqual(self.SMOKE, ci)
        self.assertEqual(release, ci - self.SMOKE)
        self.assertNotIn("--debs", self._install("release-linux")["run"])
        for step in self.jobs["release-linux"]["steps"]:
            self.assertNotRegex(step.get("uses", ""), r"^actions/cache[@/]")

    def test_ci_restores_the_deb_files_for_its_image_and_packages_before_installing(self):
        steps = self.jobs["linux"]["steps"]
        cache = next(s for s in steps if s.get("with", {}).get("path") == "~/apt-debs")
        self.assertRegex(cache["uses"], r"^actions/cache@[0-9a-f]{40}$")
        key = next(s for s in steps if s.get("id") == "debs")
        self.assertEqual(cache["with"]["key"], "${{ steps.debs.outputs.key }}")
        self.assertIn("$ImageOS-$ImageVersion", key["run"])
        self.assertIn('printf %s "$LINUX_PACKAGES" | sha256sum', key["run"])
        install = self._install("linux")
        self.assertIn("--debs ~/apt-debs ", install["run"])
        self.assertLess(steps.index(key), steps.index(cache))
        self.assertLess(steps.index(cache), steps.index(install))

    def test_a_cache_hit_is_installed_by_apt_from_its_archive_directory(self):
        # `dpkg -i` of the whole set fails on systemd's pre-dependency and leaves dpkg broken; apt
        # orders the install, and fetches only what the cached files lack.
        script = _read(APT_INSTALL)
        code = "\n".join(l for l in script.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("dpkg -i", code)
        self.assertLess(script.index('sudo cp "$debs"/*.deb /var/cache/apt/archives/'), script.index("if ! attempt"))
        self.assertIn("""find /var/cache/apt/archives -maxdepth 1 -name '*.deb' -exec cp -t "$debs" {} +""", script)


class UiWorkflowTests(unittest.TestCase):
    """A normal run of the Browser step installs from cached .deb files and fetches nothing from the
    apt mirror; BoundedFetchTests holds the fetch a miss makes."""

    def setUp(self):
        self.job = yaml.safe_load(_read(os.path.join(WORKFLOWS_DIR, "ui.yml")))["jobs"]["ui"]
        self.steps = self.job["steps"]
        self.run = next(s for s in self.steps if s.get("name") == "Browser")["run"]

    def test_the_system_libraries_are_cached_by_playwright_version_and_runner_image(self):
        names = [s.get("name") for s in self.steps]
        cache = next(s for s in self.steps if s.get("with", {}).get("path") == "~/playwright-debs")
        self.assertRegex(cache["uses"], r"^actions/cache@[0-9a-f]{40}$")
        self.assertIn("steps.libs.outputs.playwright", cache["with"]["key"])
        self.assertIn("steps.libs.outputs.image", cache["with"]["key"])
        libs = next(s for s in self.steps if s.get("id") == "libs")
        self.assertIn("playwright --version", libs["run"])
        self.assertIn("$ImageOS-$ImageVersion", libs["run"])
        self.assertLess(names.index(libs["name"]), self.steps.index(cache))
        self.assertLess(self.steps.index(cache), names.index("Browser"))

    def test_a_cache_hit_installs_without_downloading(self):
        self.assertRegex(self.run, r'sudo dpkg -i "\$debs"/\*\.deb')
        self.assertIn("""find /var/cache/apt/archives -maxdepth 1 -name '*.deb' -exec cp -t "$debs" {} +""", self.run)

    def test_a_cached_entry_that_does_not_install_is_named_and_fetched_past(self):
        # A hit is never saved again, so such an entry sends every run down the download path.
        warning = self.run.index("::warning::The cached .deb files did not install")
        self.assertLess(self.run.index("sudo dpkg -i"), warning)
        self.assertLess(warning, self.run.index("attempt()"))


if __name__ == "__main__":
    unittest.main()
