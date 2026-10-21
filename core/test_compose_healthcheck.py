#!/usr/bin/env python3
"""Offline tests for the relay container's compose healthcheck.

An image built before 5192923 keeps the code, `healthcheck.py` included, under /app/midstream-abort/; one built at
or after it keeps it under /app/core/. The deployed image can be the older one, so the compose healthcheck has to
reach the script in both layouts, and still fail when the script does. Nothing here starts a container.

Run:  python3 core/test_compose_healthcheck.py
"""

import os
import shlex
import subprocess
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE = os.path.join(ROOT, "deploy", "docker-compose.relay-8381.yml")
DOCKERFILE = os.path.join(ROOT, "deploy", "relay", "Dockerfile")
LEGACY_DIR = "/app/midstream-abort"


def healthcheck_command():
    with open(COMPOSE, encoding="utf-8") as handle:
        test = yaml.safe_load(handle)["services"]["relay"]["healthcheck"]["test"]
    if test[0] == "CMD":
        return shlex.join(test[1:])
    # Compose writes a literal $ as $$; the shell in the container sees one.
    return test[1].replace("$$", "$")


def dockerfile_healthcheck_path():
    """Where the runtime stage puts healthcheck.py: its last WORKDIR before the COPY, joined with the target."""
    workdir = None
    with open(DOCKERFILE, encoding="utf-8") as handle:
        for line in handle:
            words = line.split()
            if words[:1] == ["WORKDIR"]:
                workdir = words[1]
            if words[:1] == ["COPY"] and words[1].endswith("healthcheck.py"):
                return os.path.join(workdir, os.path.basename(words[2]))
    raise AssertionError("the Dockerfile copies no healthcheck.py")


class ComposeHealthcheckTests(unittest.TestCase):
    def run_in_layout(self, directory, exit_code):
        """Run the compose command with /app moved to a temporary root holding a stub script at <directory>."""
        with tempfile.TemporaryDirectory() as app:
            if directory:
                os.makedirs(os.path.join(app, directory))
                with open(os.path.join(app, directory, "healthcheck.py"), "w", encoding="utf-8") as stub:
                    stub.write("import sys\nsys.exit(%d)\n" % exit_code)
            command = healthcheck_command().replace("/app/", app + "/")
            return subprocess.run(["sh", "-c", command], capture_output=True, check=False).returncode

    def test_reaches_the_path_the_dockerfile_copies_to(self):
        self.assertIn(dockerfile_healthcheck_path(), healthcheck_command())

    def test_reaches_the_pre_move_path(self):
        self.assertIn(LEGACY_DIR + "/healthcheck.py", healthcheck_command())

    def test_passes_in_both_layouts(self):
        current = os.path.dirname(dockerfile_healthcheck_path())[len("/app/"):]
        for directory in (current, LEGACY_DIR[len("/app/"):]):
            with self.subTest(directory=directory):
                self.assertEqual(self.run_in_layout(directory, 0), 0)

    def test_fails_in_both_layouts_when_the_script_does(self):
        current = os.path.dirname(dockerfile_healthcheck_path())[len("/app/"):]
        for directory in (current, LEGACY_DIR[len("/app/"):]):
            with self.subTest(directory=directory):
                self.assertEqual(self.run_in_layout(directory, 1), 1)

    def test_fails_when_no_script_exists(self):
        self.assertNotEqual(self.run_in_layout(None, 0), 0)


if __name__ == "__main__":
    unittest.main()
