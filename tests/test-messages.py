from __future__ import annotations

import subprocess
import sys
import unittest
from importlib.util import module_from_spec, spec_from_loader
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


CLI = Path(__file__).parents[1] / "bin" / "plow-agents"
loader = SourceFileLoader("plow_agents_cli", str(CLI))
spec = spec_from_loader(loader.name, loader)
assert spec is not None
cli = module_from_spec(spec)
loader.exec_module(cli)


class MessagesPreparationTests(unittest.TestCase):
    def interactive(self):
        return (
            patch.object(cli.sys, "platform", "darwin"),
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch.object(cli.sys.stdout, "isatty", return_value=True),
            patch("builtins.input", return_value=""),
        )

    def test_sms_url_contains_recipient_and_encoded_body(self):
        patches = self.interactive()
        with patches[0], patches[1], patches[2], patches[3], patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            cli.offer_imessage_activation("AB\"12", "+1 (628) 246-3032")

        self.assertEqual(
            run.call_args.args[0],
            ["open", "sms:+1%20%28628%29%20246-3032?&body=Plow%20Activate%3A%20AB%2212"],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_timeout_falls_back_without_raising(self):
        patches = self.interactive()
        with patches[0], patches[1], patches[2], patches[3], patch.object(
            cli.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["open"], 5),
        ):
            cli.offer_imessage_activation("AB12", "+16282463032")

    def test_noninteractive_stdin_does_not_open_messages(self):
        with patch.object(cli.sys, "platform", "darwin"), patch.object(cli.sys.stdin, "isatty", return_value=False), patch.object(cli.subprocess, "run") as run:
            cli.offer_imessage_activation("AB12", "+16282463032")
        run.assert_not_called()

    def test_noninteractive_stdout_does_not_open_messages(self):
        with patch.object(cli.sys, "platform", "darwin"), patch.object(cli.sys.stdin, "isatty", return_value=True), patch.object(cli.sys.stdout, "isatty", return_value=False), patch.object(cli.subprocess, "run") as run:
            cli.offer_imessage_activation("AB12", "+16282463032")
        run.assert_not_called()

    def test_non_macos_does_not_open_messages(self):
        with patch.object(cli.sys, "platform", "linux"), patch.object(cli.subprocess, "run") as run:
            cli.offer_imessage_activation("AB12", "+16282463032")
        run.assert_not_called()

    def test_open_failure_falls_back_without_raising(self):
        patches = self.interactive()
        failed = subprocess.CompletedProcess([], 1, "", "no handler")
        with patches[0], patches[1], patches[2], patches[3], patch.object(cli.subprocess, "run", return_value=failed):
            cli.offer_imessage_activation("AB12", "+16282463032")

    def test_missing_open_command_falls_back_without_raising(self):
        patches = self.interactive()
        with patches[0], patches[1], patches[2], patches[3], patch.object(cli.subprocess, "run", side_effect=OSError("missing")):
            cli.offer_imessage_activation("AB12", "+16282463032")

    def test_user_can_skip_messages(self):
        with patch.object(cli.sys, "platform", "darwin"), patch.object(cli.sys.stdin, "isatty", return_value=True), patch.object(cli.sys.stdout, "isatty", return_value=True), patch("builtins.input", return_value="n"), patch.object(cli.subprocess, "run") as run:
            cli.offer_imessage_activation("AB12", "+16282463032")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
