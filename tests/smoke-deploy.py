#!/usr/bin/env python3
"""Exercise the CLI with fake Docker and HTTP; never touch a daemon or account."""
import contextlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

CLI = Path(__file__).resolve().parents[1] / "bin/plow-agents"
main = runpy.run_path(str(CLI))["main"]
DIGEST = "sha256:" + "a" * 64
IMAGE = "ghcr.io/example/agent"


class Response(io.BytesIO):
    def __init__(self, body, code=200):
        super().__init__(json.dumps(body).encode())
        self.status = code


class Smoke(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = os.getcwd()
        work = Path(self.directory.name) / "work"
        work.mkdir()
        os.chdir(work)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(os.chdir, self.previous)
        self.token_file = str(Path(self.directory.name) / "token")
        Path(self.token_file).write_text("synthetic-account")
        Path("plow-agents.toml").write_text(f'image = "{IMAGE}:v1"\n')
        self.commands = []
        self.requests = []
        self.fail_up = False
        self.created = []
        self.deleted = []
        self.docker_patch = patch("subprocess.run", side_effect=self.docker)
        self.http_patch = patch("urllib.request.OpenerDirector.open", side_effect=self.http)
        self.docker_patch.start()
        self.http_patch.start()
        self.addCleanup(self.docker_patch.stop)
        self.addCleanup(self.http_patch.stop)

    def docker(self, argv, **kwargs):
        self.commands.append(argv)
        if argv[1:3] == ["compose", "up"]:
            self.assertIn("PLOW_AGENT_TOKEN=synthetic-agent", Path("plow-credentials").read_text())
        return subprocess.CompletedProcess(argv, int(self.fail_up), f"v1: digest: {DIGEST} size: 123\n", stderr="compose startup failed: fixture error\n" if self.fail_up else "")

    def http(self, req, *args, **kwargs):
        self.requests.append(req)
        url = req.full_url
        self.assertEqual(req.get_header("Authorization"), "Bearer synthetic-account")
        if url.endswith("/v1/chats"):
            return Response({"data": [{"status": "active", "participants": [{"type": "agent", "relationship": "self", "line": {"uid": "ln_free"}}]}]})
        if url.endswith("/v1/lines"):
            return Response({"data": [{"uid": "ln_free", "agent_uid": None}]})
        if req.method == "POST":
            self.created.append(json.loads(req.data))
            return Response({"agent": {"uid": "agt_test"}, "token": "synthetic-agent"})
        if req.method == "DELETE":
            self.deleted.append(url)
            return Response({})
        if url.endswith("/agt_test"):
            return Response({"provider": "self_hosted"})
        if url.endswith("/v1/agents"):
            return Response([{"line": {"uid": "ln_free"}, "provider": f"exe:{IMAGE}@{DIGEST}", "status": "failed", "failure_code": "image_pull_timeout"}])
        self.fail(f"unexpected request: {req.method} {url}")

    def run_cli(self, *args, success=True, expected_error=None):
        out, err = io.StringIO(), io.StringIO()
        token_args = ["--token-file", self.token_file] if self.token_file else []
        with patch.object(sys, "argv", [str(CLI), "--api-base", "https://api.example.test", *token_args, *args]):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = main()
                except SystemExit as error:
                    code = error.code
        self.assertEqual(code == 0, success, err.getvalue() or str(code))
        if expected_error is not None:
            self.assertEqual(code, expected_error)
        return out.getvalue(), err.getvalue()

    def test_image_build(self):
        self.run_cli("image", "build")
        self.assertEqual(self.commands, [["docker", "build", "--platform", "linux/amd64", "--tag", IMAGE + ":v1", "."]])
        Path("nested").mkdir()
        for credential in ("# plow-agent-uid: agt_test\n", "PLOW_AGENT_TOKEN=synthetic-agent\n"):
            Path("nested/custom.env").write_text(credential)
            self.commands.clear()
            self.run_cli("image", "build", success=False)
            self.assertFalse(self.commands)
            self.assertFalse(self.requests)

    def test_build_honors_dockerignore(self):
        credential = Path("plow-credentials")
        credential.write_text("PLOW_AGENT_TOKEN=synthetic-agent\n")
        cases = [
            (None, False), ("# plow-credentials\n", False),
            ("/plow-credentials\n", True), ("./plow-credentials/\n", True),
            ("plow-*\n", True), ("**/plow-credentials\n", True),
            ("plow-credential?\n", True), ("plow-credential[s]\n", True),
            ("plow-*\n!plow-credentials\n", False),
            ("*\n!plow-*\nplow-credentials\n", True),
        ]
        for rules, excluded in cases:
            with self.subTest(rules=rules):
                if rules is not None:
                    Path(".dockerignore").write_text(rules)
                self.commands.clear()
                self.run_cli("image", "build", success=excluded, expected_error=None if excluded else
                             "plow-agents: add plow-credentials to .dockerignore before building")
                self.assertEqual(bool(self.commands), excluded)
                self.assertEqual(credential.read_text(), "PLOW_AGENT_TOKEN=synthetic-agent\n")
        Path("Dockerfile.dockerignore").write_text("")
        self.commands.clear()
        self.run_cli("image", "build", success=False,
                     expected_error="plow-agents: add plow-credentials to Dockerfile.dockerignore before building")
        self.assertFalse(self.commands)
        Path("Dockerfile.dockerignore").write_text("plow-credentials\n")
        self.run_cli("image", "build")

    def test_build_scans_ignored_dockerfile(self):
        Path(".dockerignore").write_text("Dockerfile\n")
        Path("Dockerfile").write_text("FROM scratch\n# plow-agent-uid: agt_test\n")
        self.run_cli("image", "build", success=False,
                     expected_error="plow-agents: remove credentials from Dockerfile before building")
        self.assertFalse(self.commands)
        self.assertFalse(self.requests)

    def test_build_refuses_dockerfile_env_credential(self):
        Path("Dockerfile").write_text("FROM scratch\nENV PLOW_AGENT_TOKEN=synthetic-agent\n")
        self.run_cli("image", "build", success=False,
                     expected_error="plow-agents: remove credentials from Dockerfile before building")
        self.assertFalse(self.commands)

    def test_build_ignores_nested_credentials_and_account_token(self):
        Path("nested").mkdir()
        Path("nested/custom.env").write_text("# plow-agent-uid: agt_test\n")
        self.token_file = str(Path("nested/token").resolve())
        Path(self.token_file).write_text("synthetic-account")
        Path(".dockerignore").write_text("nested/\n")
        self.run_cli("image", "build")
        self.commands.clear()
        Path(".dockerignore").write_text("nested/\n!nested/token\n")
        self.run_cli("image", "build", success=False,
                     expected_error="plow-agents: add nested/token to .dockerignore before building")
        self.assertFalse(self.commands)

    def test_account_token_in_build_context(self):
        Path("nested/plow").mkdir(parents=True)
        account_file = Path("nested/plow/token").resolve()
        account_file.write_text("synthetic-account")
        for override in (str(account_file), None):
            self.token_file = override
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path("nested").resolve())}):
                self.run_cli("image", "build", success=False)
            self.assertFalse(self.commands)
            self.assertFalse(self.requests)

    def test_image_arguments_and_read_only_config(self):
        config = Path("plow-agents.toml")
        for contents in (None, f'image = "{IMAGE}:v1"\n', 'not valid toml'):
            for verb in ("build", "push"):
                with self.subTest(contents=contents, verb=verb):
                    if contents is None:
                        config.unlink(missing_ok=True)
                    else:
                        config.write_text(contents)
                    self.commands.clear()
                    with patch("os.replace", side_effect=AssertionError("must not write")):
                        out, _ = self.run_cli("image", verb, IMAGE + ":v2")
                    self.assertIn(IMAGE + ":v2", self.commands[-1])
                    if verb == "push":
                        self.assertEqual(out.splitlines()[-1], f"{IMAGE}@{DIGEST}")
                    self.assertEqual(config.read_text() if config.exists() else None, contents)
                    self.assertEqual(list(Path(".").iterdir()), [config] if config.exists() else [])
        config.write_text(f'image = "{IMAGE}:v1"\n')
        original = config.read_bytes()
        out, _ = self.run_cli("image", "push")
        self.assertEqual(out.splitlines()[-1], f"{IMAGE}@{DIGEST}")
        self.assertEqual(config.read_bytes(), original)
        config.unlink()
        for verb in ("build", "push"):
            self.run_cli("image", verb, success=False,
                         expected_error="plow-agents: pass IMAGE or set image in ./plow-agents.toml")

    def test_image_registry_host(self):
        for host in ("localhost", "localhost:5000", "registry:5000", "registry:5.0"):
            with self.subTest(host=host):
                Path("plow-agents.toml").write_text(f'image = "{host}/agent:v1"\n')
                self.run_cli("image", "build", success=False)
                self.assertFalse(self.commands)
        self.run_cli("image", "build", "registry.example:5000/agent:v1")
        self.assertIn("registry.example:5000/agent:v1", self.commands[-1])

    def test_deploy(self):
        Path("plow-agents.toml").write_text('not valid toml')
        for target, name in ((f"{IMAGE}@{DIGEST}", "agent"), ("exe:hermes", "hermes")):
            _, err = self.run_cli("deploy", target, "--line", "ln_explicit")
            self.assertIn("Requested", err)
            self.assertEqual(self.created[-1], {
                "name": name, "line_uid": "ln_explicit",
                "provider": target if target.startswith("exe:") else f"exe:{IMAGE}@{DIGEST}",
            })
        self.created.clear()
        self.requests.clear()
        Path("plow-agents.toml").write_text(f'image = "{IMAGE}:v1"\nlast_pushed = "{IMAGE}@{DIGEST}"\n')
        for target in ((), (DIGEST,), (IMAGE + ":v1",)):
            self.run_cli("deploy", *target, success=False)
            self.assertFalse(self.requests)

    def test_local_requires_credential_exclusion_before_mint(self):
        for rules in (None, "plow-*\n!plow-credentials\n"):
            with self.subTest(rules=rules):
                if rules is not None:
                    Path(".dockerignore").write_text(rules)
                self.run_cli("deploy", "--local", success=False,
                             expected_error="plow-agents: add plow-credentials to .dockerignore before building")
                self.assertFalse(self.requests)
                self.assertFalse(self.commands)
                self.assertFalse(Path("plow-credentials").exists())

    def test_deploy_local(self):
        Path(".dockerignore").write_text("plow-credentials\n")
        fixture = Path("tests/__pycache__/test_plow_init.cpython-311.pyc")
        fixture.parent.mkdir(parents=True)
        fixture.write_bytes(b"\x00\nPLOW_AGENT_TOKEN=synthetic-fixture\n")
        self.run_cli("deploy", "--local", "--agent-api-base", "http://host.docker.internal:8000/v1")
        self.assertIn("PLOW_API_BASE=http://host.docker.internal:8000\n", Path("plow-credentials").read_text())
        self.assertEqual(self.commands, [["docker", "compose", "up", "--build", "-d"]])
        self.assertEqual(self.created, [{"name": "plow-agent", "provider": "self_hosted", "line_uid": "ln_free"}])

    def test_docker_failure_prints_stderr(self):
        self.fail_up = True
        for verb in ("build", "push"):
            _, err = self.run_cli("image", verb, success=False)
            self.assertIn("compose startup failed: fixture error", err)

    def test_local_startup_failure_preserves_agent_and_credential(self):
        Path(".dockerignore").write_text("plow-credentials\n")
        self.fail_up = True
        _, err = self.run_cli("deploy", "--local", success=False,
                              expected_error="plow-agents: docker compose up --build -d failed (1)")
        self.assertIn("compose startup failed: fixture error", err)
        self.assertFalse(self.deleted)
        self.assertEqual(self.created, [{"name": "plow-agent", "provider": "self_hosted", "line_uid": "ln_free"}])
        self.assertEqual(Path("plow-credentials").read_text(),
                         "PLOW_API_BASE=https://api.example.test\nPLOW_AGENT_TOKEN=synthetic-agent\n# plow-agent-uid: agt_test\n")
        self.assertIn("Agent and plow-credentials kept", err)
        self.assertIn("plow-agents revoke", err)
        self.assertIn("docker compose up --build -d", err)

    def test_agents(self):
        out, _ = self.run_cli("agents")
        self.assertEqual(out, f"LINE\tTARGET\tSTATUS\nln_free\t{IMAGE}@{DIGEST}\tfailed(image_pull_timeout)\n")


if __name__ == "__main__":
    unittest.main()
