#!/usr/bin/env python3
"""`lines`, `mint`, `rotate`, and `revoke` against a stub API.

Standard library only, no network, no Plow account: drive the real CLI against
a local stub with `--api-base`.

    python3 tests/smoke-line-in-use.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

CLI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "plow-agents")

FREE, CLOUD, SELF_HOSTED, OTHER = "ln_free", "ln_cloud", "ln_self_hosted", "ln_other"
MAILBOX = "ln_mail"
PHOTO_URL = "https://api.example.com/v1/profile-photos/2b0f9c1e-0000-4000-8000-000000000001"


def line(uid: str, name: str) -> dict:
    return {"uid": uid, "display_name": name, "provider_key": f"+1555{uid[-4:]}", "provider_type": "imessage"}


# `/v1/lines` serves mailboxes beside phone numbers, and both carry a
# `provider_type` -- so the stub does too, or the CLI is tested against a shape
# the API never sends. The mailbox is free and unheld: nothing but its type
# should keep it out of `lines` and out of `mint`.
LINES = {"data": [dict(line(uid, name), agent_uid=agent_uid) for uid, name, agent_uid in (
    (FREE, "Free", None), (CLOUD, "Cloud", "agt_cloud"), (SELF_HOSTED, "Self hosted", "agt_self_hosted"),
    (OTHER, "Other account", "agt_other"),
)] + [{"uid": MAILBOX, "display_name": "Free", "provider_key": "free@plow.co",
       "provider_type": "email", "agent_uid": None}]}
AGENTS = {
    "agt_other": {"uid": "agt_other", "provider": "self_hosted"},
    "agt_cloud": {"uid": "agt_cloud", "provider": "exe:life"},
    "agt_self_hosted": {"uid": "agt_self_hosted", "provider": "self_hosted"},
}


class Stub(BaseHTTPRequestHandler):
    lines: dict = LINES
    include_available = True
    agent_get_status = 200
    rotate_status = 200
    delete_status = 200
    posts: list[str] = []
    minted: list[dict] = []
    revoked: list[str] = []
    profile_updates: list[dict] = []
    photo_uploads: list[dict[str, tuple[str, bytes]]] = []
    requests: list[str] = []
    profile_get_status = 200

    def _send(self, status: int, payload: object) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
        Stub.requests.append(f"GET {self.path}")
        if self.path == "/v1/lines":
            other_account = self.headers.get("Authorization") == "Bearer acct_other"
            rows = []
            for stored in Stub.lines["data"]:
                row = dict(stored)
                if Stub.include_available:
                    row["available"] = not bool(stored["agent_uid"])
                if (stored["agent_uid"] == "agt_other") != other_account:
                    row["agent_uid"] = None
                rows.append(row)
            return self._send(200, {"data": rows})
        if self.path.startswith("/v1/agents/"):
            agent = AGENTS.get(self.path.rsplit("/", 1)[1])
            return self._send(Stub.agent_get_status if agent else 404, agent)
        if self.path == "/v1/auth/profile":
            return self._send(Stub.profile_get_status, {"display_name": "Ada", "photo_url": "https://example.com/ada.jpg"})
        self._send(404, {"detail": self.path})

    def do_PATCH(self) -> None:  # noqa: N802
        Stub.requests.append(f"PATCH {self.path}")
        if self.path == "/v1/auth/profile":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            Stub.profile_updates.append(body)
            return self._send(200, body)
        self._send(404, {"detail": self.path})

    def do_POST(self) -> None:  # noqa: N802
        Stub.requests.append(f"POST {self.path}")
        Stub.posts.append(self.path)
        if self.path == "/v1/auth/activate":
            return self._send(200, {"display_code": "test", "activation_secret": "synthetic", "send_to": "+15555555555"})
        if self.path == "/v1/auth/activate/redeem":
            return self._send(200, {"status": "verified", "token": "synthetic_account_token"})
        if self.path == "/v1/auth/profile/photo":
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            # The part's own bytes, recovered the way a server does: split on
            # the boundary the Content-Type declared, then past the blank line
            # that ends the part's headers.
            boundary = self.headers["Content-Type"].split("boundary=", 1)[1].encode()
            parts = {}
            for part in raw.split(b"--" + boundary)[1:-1]:
                headers, _, content = part.partition(b"\r\n\r\n")
                field = headers.decode().partition('name="')[2].partition('"')[0]
                parts[field] = (headers.decode(), content.rpartition(b"\r\n")[0])
            Stub.photo_uploads.append(parts)
            name = parts["display_name"][1].decode() if "display_name" in parts else None
            return self._send(200, {"display_name": name, "photo_url": PHOTO_URL})
        if self.path == "/v1/agents":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            Stub.minted.append(body)
            selected = next(row for row in Stub.lines["data"] if row["uid"] == body["line_uid"])
            if selected["agent_uid"]:
                return self._send(409, {"detail": {"code": "AGENT_EXISTS", "agent_uid": selected["agent_uid"], "message": "Rotate its credential or delete it first"}})
            selected["agent_uid"] = "d2e048a4cbefdc491657eaddc9c7657a"
            agent = {"uid": "d2e048a4cbefdc491657eaddc9c7657a", "name": body["name"], "provider": body["provider"],
                     "credential": {"id": 99, "scopes": ["chats:use", "relay:call"]}}
            AGENTS["d2e048a4cbefdc491657eaddc9c7657a"] = agent
            return self._send(201, {"agent": agent, "token": "plow_minted99_token"})
        if self.path == "/v1/agents/d2e048a4cbefdc491657eaddc9c7657a/credential":
            return self._send(Stub.rotate_status, {"credential": {"id": 100}, "token": "plow_rotated100_token"})
        self._send(404, {"detail": self.path})

    def do_DELETE(self) -> None:  # noqa: N802
        Stub.requests.append(f"DELETE {self.path}")
        if self.path.startswith("/v1/agents/"):
            if Stub.delete_status != 200:
                return self._send(Stub.delete_status, {"detail": "delete failed"})
            uid = self.path.rsplit("/", 1)[1]
            Stub.revoked.append(uid)
            AGENTS.pop(uid, None)
            for row in Stub.lines["data"]:
                if row["agent_uid"] == uid:
                    row["agent_uid"] = None
            return self._send(200, {"status": "deleted"})
        self._send(404, {"detail": self.path})

    def log_message(self, *_: object) -> None:
        pass


class Proxy(BaseHTTPRequestHandler):
    received: list[tuple[str, str | None, bytes]] = []

    def do_GET(self) -> None:  # noqa: N802
        Proxy.received.append((self.path, self.headers.get("Authorization"), self.rfile.read(int(self.headers.get("Content-Length", 0)))))
        self.send_response(502)
        self.end_headers()

    do_POST = do_GET

    def log_message(self, *_: object) -> None:
        pass


def run(*argv: str, cwd: str, base: str, token: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, CLI, "--api-base", base, "--token-file", token, *argv], cwd=cwd, env=env, capture_output=True, text=True)


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    failures = []

    def check(what: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{what}\n  want: {want!r}\n  got:  {got!r}")
        print(f"{'ok  ' if got == want else 'FAIL'} {what}")

    with tempfile.TemporaryDirectory() as work:
        token = os.path.join(work, "token")
        with open(token, "w") as handle:
            handle.write("acct_stub\n")

        Stub.requests.clear()
        profile = run("profile", "--name", "Ada", "--photo", "https://example.com/ada.jpg", cwd=work, base=base, token=token)
        check("profile update exits 0", profile.returncode, 0)
        check("profile update sends name and photo", Stub.profile_updates[-1], {"display_name": "Ada", "photo_url": "https://example.com/ada.jpg"})

        check("a photo already hosted is one PATCH and no upload", Stub.requests, ["PATCH /v1/auth/profile"])

        # Schemes are case-insensitive. A prefix test that was not read
        # HTTPS://... as a filename and went looking for it on disk.
        for written in ("HTTPS://example.com/ada.jpg", "Https://example.com/ada.jpg", "HTTP://example.com/ada.jpg"):
            Stub.requests.clear()
            cased = run("profile", "--name", "Ada", "--photo", written, cwd=work, base=base, token=token)
            check(f"{written.split(':')[0]} is a URL, not a filename", Stub.requests, ["PATCH /v1/auth/profile"])
            check(f"and {written.split(':')[0]} is sent as given", cased.returncode == 0 and Stub.profile_updates[-1]["photo_url"] == written, True)

        # A local file is uploaded, and that route stores the bytes and sets
        # photo_url in one transaction -- so the upload is the whole write and
        # nothing may follow it. A PATCH afterwards that failed would report an
        # error for a photo already stored and already public.
        photo = os.path.join(work, "ada photo.png")
        with open(photo, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\nada")
        Stub.requests.clear()
        uploaded = run("profile", "--photo", photo, cwd=work, base=base, token=token)
        check("a local file exits 0", uploaded.returncode, 0)
        check("and is exactly one request", Stub.requests, ["POST /v1/auth/profile/photo"])
        check("with the file's own bytes", Stub.photo_uploads[-1]["file"][1], b"\x89PNG\r\n\x1a\nada")
        check("under a filename with nothing that could break the header", 'filename="ada_photo.png"' in Stub.photo_uploads[-1]["file"][0], True)
        check("and prints the profile the route answered with", json.loads(uploaded.stdout)["photo_url"], PHOTO_URL)

        # A name alongside a file rides in the same request, because the route
        # writes both as it stores the bytes. Two calls meant a failure at the
        # second left the photo public beside the name it was sent to replace.
        Stub.requests.clear()
        both = run("profile", "--name", "Ada", "--photo", photo, cwd=work, base=base, token=token)
        check("a name beside a file is still one request", Stub.requests, ["POST /v1/auth/profile/photo"])
        check("carrying both parts", Stub.photo_uploads[-1]["display_name"][1], b"Ada")
        check("and it exits 0", both.returncode, 0)
        check("and prints the profile with both halves set", json.loads(both.stdout), {"display_name": "Ada", "photo_url": PHOTO_URL})

        # One harness for every way a file can be unusable: each is refused
        # before the first request, so the account is left exactly as it was.
        huge = b"\x89PNG\r\n\x1a\n" + b"\0" * (5 * 1024 * 1024)
        for label, filename, blob, said in (
            ("a path that is not there", "nope.png", None, "cannot read"),
            ("a file that is not an image", "notes.txt", b"dear diary", "not a PNG, JPEG, GIF, or WebP"),
            ("a file over the cap", "huge.png", huge, "larger than 5 MB"),
        ):
            unusable = os.path.join(work, filename)
            if blob is not None:
                with open(unusable, "wb") as handle:
                    handle.write(blob)
            Stub.requests.clear()
            refused = run("profile", "--name", "Ada", "--photo", unusable, cwd=work, base=base, token=token)
            check(f"{label} is refused", refused.returncode != 0 and said in refused.stderr, True)
            check(f"and {label} makes zero requests", Stub.requests, [])

        Stub.requests.clear()
        named = run("profile", "--name", "Ada", cwd=work, base=base, token=token)
        check("a name on its own is one PATCH", Stub.requests, ["PATCH /v1/auth/profile"])
        check("and exits 0", named.returncode, 0)
        neither = run("profile", cwd=work, base=base, token=token)
        check("and asking for nothing is refused", neither.returncode != 0 and "needs --name, --photo, or --show" in neither.stderr, True)

        shown = run("profile", "--show", cwd=work, base=base, token=token)
        check("profile show exits 0", shown.returncode, 0)
        Stub.profile_get_status = 500
        failed_show = run("profile", "--show", cwd=work, base=base, token=token)
        check("profile show fails on a failed GET", failed_show.returncode != 0, True)
        Stub.profile_get_status = 200

        Stub.requests.clear()
        listed = run("lines", cwd=work, base=base, token=token)
        check("lines exits 0", listed.returncode, 0)
        check("lines reads availability from line occupancy", Stub.requests, ["GET /v1/lines"])
        rows = {row.split("\t")[0]: row.split("\t")[3] for row in listed.stdout.splitlines()}
        check("chat-less free line is free", rows.get(FREE), "free")
        check("cloud line names its agent", rows.get(CLOUD), "agt_cloud")
        check("self_hosted line names its agent", rows.get(SELF_HOSTED), "agt_self_hosted")

        check("another account's held line is in use", rows.get(OTHER), "in use")
        check("a mailbox is not listed", rows.get(MAILBOX), None)
        mailbox = run("mint", MAILBOX, cwd=work, base=base, token=token)
        check("and minting one is refused as not a phone line",
              mailbox.returncode != 0 and "not a phone line" in mailbox.stderr, True)
        check("mailbox refusal creates no credential", os.path.exists(os.path.join(work, "plow-credentials")), False)
        other_token = os.path.join(work, "other-token")
        with open(other_token, "w") as handle:
            handle.write("acct_other\n")
        other_listed = run("lines", cwd=work, base=base, token=other_token)
        other_rows = {row.split("\t")[0]: row.split("\t")[3] for row in other_listed.stdout.splitlines()}
        check("second account sees its own holder", other_rows.get(OTHER), "agt_other")
        check("second account sees the first account's line in use", other_rows.get(CLOUD), "in use")
        check("both accounts see the unheld line as free", other_rows.get(FREE), "free")
        for held_line, caller_token in ((OTHER, token), (CLOUD, other_token)):
            Stub.requests.clear()
            refused = run("mint", held_line, cwd=work, base=base, token=caller_token)
            check("another account's line is refused before create", refused.returncode != 0 and Stub.requests == ["GET /v1/lines"], True)
            check("cross-account refusal creates no credential", os.path.exists(os.path.join(work, "plow-credentials")), False)
        Stub.include_available = False
        for caller_token, foreign_line, own_line, own_agent in (
            (token, OTHER, CLOUD, "agt_cloud"), (other_token, CLOUD, OTHER, "agt_other"),
        ):
            listed = run("lines", cwd=work, base=base, token=caller_token)
            rows = {row.split("\t")[0]: row.split("\t")[3] for row in listed.stdout.splitlines()}
            check("missing availability labels a foreign-held line unknown", rows.get(foreign_line), "unknown")
            check("missing availability labels an unheld line unknown", rows.get(FREE), "unknown")
            check("missing availability still names the caller's agent", rows.get(own_line), own_agent)
        Stub.requests.clear()
        refused = run("mint", OTHER, cwd=work, base=base, token=token)
        check("missing availability lets the API decide", refused.returncode != 0 and "POST /v1/agents" in Stub.requests, True)
        check("API refusal leaves no credential", os.path.exists(os.path.join(work, "plow-credentials")), False)
        Stub.include_available = True

        Stub.requests.clear()
        logged_in = run("login", cwd=work, base=base, token=token)
        check("login finishes without checking pool lines", logged_in.returncode == 0 and Stub.requests == ["POST /v1/auth/activate", "POST /v1/auth/activate/redeem"], True)
        credential = os.path.join(work, "plow-credentials")
        os.mkdir(credential)
        directory = run("mint", FREE, cwd=work, base=base, token=token)
        check("credential directory has recovery instructions", directory.returncode != 0 and f"rmdir {os.path.realpath(credential)}" in directory.stderr, True)
        os.rmdir(credential)
        Stub.requests.clear()
        occupied = run("mint", CLOUD, cwd=work, base=base, token=token)
        check("occupied line is refused without a file", occupied.returncode != 0 and not os.path.exists(credential), True)
        check("cloud-held line names its agent and suggests revoke", "agt_cloud" in occupied.stderr and f"revoke {CLOUD}" in occupied.stderr, True)
        held = run("mint", SELF_HOSTED, cwd=work, base=base, token=token)
        check("self-hosted holder is pointed at revoke", held.returncode != 0 and "agt_self_hosted" in held.stderr and f"revoke {SELF_HOSTED}" in held.stderr, True)
        # The refusal is this tool's, not the server's: nothing is created and
        # the create endpoint is never reached.
        check("occupied line is refused before any create", [r for r in Stub.requests if r.startswith("POST")], [])
        unknown = run("mint", "ln_nope", cwd=work, base=base, token=token)
        check("unknown line is refused by name", unknown.returncode != 0 and "unknown line:ln_nope" in unknown.stderr, True)
        Stub.requests.clear()
        minted = run("mint", FREE, "--agent-api-base", "http://host.docker.internal:8000", cwd=work, base=base, token=token)
        check("mint on a chat-less free line succeeds", minted.returncode, 0)
        check("chat-less mint checks lines then creates the agent", Stub.requests, ["GET /v1/lines", "POST /v1/agents"])
        check("mint creates a self_hosted agent", Stub.minted[-1] if Stub.minted else None, {"name": "plow-agent", "provider": "self_hosted", "line_uid": FREE})
        if not os.path.isfile(credential):
            failures.append("mint did not create credential file")
        else:
            with open(credential) as handle:
                original = handle.read()
            check("credential records agent identity", "# plow-agent-uid: d2e048a4cbefdc491657eaddc9c7657a\n" in original, True)
            check("only image-supported settings are emitted", {line.split("=", 1)[0] for line in original.splitlines() if line and not line.startswith("#")}, {"PLOW_API_BASE", "PLOW_AGENT_TOKEN"})
            unsupported = os.path.join(work, "unsupported-credential")
            content = "PLOW_API_BASE=https://api.example.com\nPLOW_AGENT_TOKEN=unused\nPLOW_AGENT_UID=unsupported-agent\n"
            with open(unsupported, "w") as handle:
                handle.write(content)
            Stub.requests.clear()
            for verb in ("rotate", "revoke"):
                refused = run(verb, "--credential-file", unsupported, cwd=work, base=base, token=token)
                with open(unsupported) as handle:
                    check(f"{verb} requires comment identity before API calls", refused.returncode != 0 and not Stub.requests and handle.read() == content, True)
            removed = run("fix-credentials", cwd=work, base=base, token=token)
            check("repair verb is unavailable", removed.returncode == 2 and "invalid choice" in removed.stderr, True)
            check("credential has mode 600", os.stat(credential).st_mode & 0o777, 0o600)
            check("mint never prints token", "plow_minted99_token" in minted.stdout + minted.stderr, False)
            Stub.requests.clear()
            repeated = run("mint", FREE, cwd=work, base=base, token=token)
            check("re-mint directs to explicit rotation", repeated.returncode != 0 and "rotate" in repeated.stderr, True)
            check("re-mint names the credential file", credential in repeated.stderr, True)
            check("re-mint makes no requests", Stub.requests, [])
            Stub.rotate_status = 500
            failed = run("rotate", cwd=work, base=base, token=token)
            with open(credential) as handle:
                check("failed rotation preserves credential", failed.returncode != 0 and handle.read() == original, True)
            Stub.rotate_status = 200
            rotated = run("rotate", cwd=work, base=base, token=token)
            check("rotation succeeds", rotated.returncode, 0)
            with open(credential) as handle:
                updated = handle.read()
            check("rotation installs new token", "PLOW_AGENT_TOKEN=plow_rotated100_token\n" in updated, True)
            check("rotation preserves container API base", "PLOW_API_BASE=http://host.docker.internal:8000\n" in updated, True)
            check("rotation preserves identity", "# plow-agent-uid: d2e048a4cbefdc491657eaddc9c7657a\n" in updated, True)
            check("rotation never prints token", "plow_rotated100_token" in rotated.stdout + rotated.stderr, False)
            check("rotation keeps mode 600", os.stat(credential).st_mode & 0o777, 0o600)
            with open(credential, "w") as handle:
                handle.write("# plow-agent-uid: agt_cloud\n")
            Stub.requests.clear()
            cloud_file = run("revoke", cwd=work, base=base, token=token)
            check("credential-file revoke still refuses cloud agents", cloud_file.returncode != 0 and not any(req.startswith("DELETE ") for req in Stub.requests), True)
            with open(credential, "w") as handle:
                handle.write(updated)
            Stub.delete_status = 500
            failed_cloud = run("revoke", CLOUD, cwd=work, base=base, token=token)
            check("line revoke HTTP 500 gives incomplete-retirement recovery", failed_cloud.returncode != 0 and "retirement may be incomplete" in failed_cloud.stderr and f"re-run `plow-agents revoke {CLOUD}`" in failed_cloud.stderr, True)
            check("line revoke HTTP 500 does not report success", "Retired agent" in failed_cloud.stderr, False)
            with open(credential) as handle:
                check("line revoke HTTP 500 preserves credential", handle.read(), updated)
            Stub.delete_status = 200
            cloud = run("revoke", CLOUD, cwd=work, base=base, token=token)
            check("line revoke retires cloud agent", cloud.returncode == 0 and "agt_cloud" in Stub.revoked and "agt_cloud" not in AGENTS, True)
            check("line revoke prints retired cloud agent and line", f"Retired agent agt_cloud (exe:life) on line:{CLOUD}." in cloud.stderr, True)
            check("line revoke explains conditional chat retirement", f"If this agent had authenticated, your chats on line:{CLOUD} were retired too." in cloud.stderr, True)
            check("retired cloud line is free", next(row for row in Stub.lines["data"] if row["uid"] == CLOUD)["agent_uid"], None)
            with open(credential) as handle:
                check("cloud revoke preserves unrelated credential", handle.read(), updated)
            recovered = run("revoke", SELF_HOSTED, cwd=work, base=base, token=token)
            check("line recovery retires its self_hosted agent", recovered.returncode == 0 and "agt_self_hosted" in Stub.revoked, True)
            check("line recovery leaves a different credential", os.path.exists(credential), True)
            Stub.delete_status = 500
            failed = run("revoke", cwd=work, base=base, token=token)
            check("failed revoke leaves credential", failed.returncode != 0 and os.path.exists(credential), True)
            Stub.delete_status = 200
            revoked = run("revoke", cwd=work, base=base, token=token)
            check("revoke retires the agent", revoked.returncode == 0 and "d2e048a4cbefdc491657eaddc9c7657a" in Stub.revoked, True)
            check("revoke removes credential", os.path.exists(credential), False)
            listed = run("lines", cwd=work, base=base, token=token)
            check("retired line is free", f"{FREE}\tFree\t+1555free\tfree" in listed.stdout, True)
        legacy = "PLOW_API_BASE=x\nPLOW_AGENT_TOKEN=plow_legacy_token\n"
        with open(credential, "w") as handle:
            handle.write(legacy)
        free_revoke = run("revoke", FREE, cwd=work, base=base, token=token)
        check("free-line revoke refuses to guess legacy ownership", free_revoke.returncode != 0 and os.path.exists(credential), True)
        with open(credential, "w") as handle:
            handle.write(legacy)
        AGENTS["agt_self_hosted"] = {"uid": "agt_self_hosted", "provider": "self_hosted"}
        next(row for row in Stub.lines["data"] if row["uid"] == SELF_HOSTED)["agent_uid"] = "agt_self_hosted"
        recovered = run("revoke", SELF_HOSTED, cwd=work, base=base, token=token)
        check("retiring a line preserves a legacy file of unknown ownership", recovered.returncode == 0 and os.path.exists(credential), True)
        if os.path.exists(credential):
            os.unlink(credential)
        created = run("mint", FREE, cwd=work, base=base, token=token)
        check("create agent for lost-response retry", created.returncode, 0)
        Stub.agent_get_status = 404
        Stub.delete_status = 404
        Stub.requests.clear()
        wrong_account = run("revoke", cwd=work, base=base, token=token)
        check("account-switch 404 preserves credential and sends no DELETE", wrong_account.returncode != 0 and os.path.exists(credential) and not any(req.startswith("DELETE ") for req in Stub.requests), True)
        Stub.agent_get_status = 200
        retired = run("revoke", cwd=work, base=base, token=token)
        check("DELETE 404 after ownership proof removes the matching credential", retired.returncode == 0 and not os.path.exists(credential), True)
        Stub.delete_status = 200
        if os.path.exists(credential):
            os.unlink(credential)
        next(row for row in Stub.lines["data"] if row["uid"] == FREE)["agent_uid"] = None
        AGENTS.pop("d2e048a4cbefdc491657eaddc9c7657a", None)
        for bad_base in ('http://container/"', 'http://container/\\', 'http://container/\n', '/', '/v1'):
            Stub.requests.clear()
            refused = run("mint", FREE, "--agent-api-base", bad_base, cwd=work, base=base, token=token)
            check("invalid container base is refused before mint POST", refused.returncode != 0 and not Stub.requests, True)
            next(row for row in Stub.lines["data"] if row["uid"] == FREE)["agent_uid"] = None
            AGENTS.pop("d2e048a4cbefdc491657eaddc9c7657a", None)
            content = f"PLOW_API_BASE={bad_base}\n# plow-agent-uid: agt_self_hosted\nPLOW_AGENT_TOKEN=plow_old_token\n"
            if "\n" not in bad_base:
                with open(credential, "w") as handle:
                    handle.write(content)
                Stub.requests.clear()
                refused = run("rotate", cwd=work, base=base, token=token)
                with open(credential) as handle:
                    check("invalid saved base is refused before rotation POST", refused.returncode != 0 and not Stub.requests and handle.read() == content, True)
                os.unlink(credential)
        Stub.requests.clear()
        refused = run("mint", FREE, cwd=work, base=base+'"', token=token)
        check("invalid host API base is refused without requests", refused.returncode != 0 and not Stub.requests, True)
        cli_main = runpy.run_path(CLI)["main"]
        for root, permitted in (
            ("https://api.example.com", True),
            ("http://localhost:8000", True),
            ("http://127.0.0.1:8000", True),
            ("http://[::1]:8000", True),
            ("http://api.orb.local:8000", True),
            ("http://api.example.com", False),
            ("http://localhost.example.com", False),
            ("http://api.orb.local.example.com", False),
            ("http://localhost@api.example.com", False),
        ):
            response = io.BytesIO(b'{"data": []}')
            response.status = 200
            argv = [CLI, "--api-base", root, "--token-file", token, "lines"]
            with patch.object(sys, "argv", argv), patch("urllib.request.OpenerDirector.open", return_value=response) as transport:
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    try:
                        code = cli_main()
                    except SystemExit as error:
                        code = error.code
            check(f"account token transport policy for {root}", (code == 0, transport.called), (permitted, permitted))
        proxy = HTTPServer(("127.0.0.1", 0), Proxy)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
        cli_request = runpy.run_path(CLI)["request"]
        resolve = socket.getaddrinfo
        proxy_env = {"http_proxy": proxy_url, "HTTP_PROXY": proxy_url, "no_proxy": "", "NO_PROXY": ""}
        try:
            with patch.dict(os.environ, proxy_env), patch("urllib.request._opener", None), patch("urllib.request.proxy_bypass", return_value=False):
                for host in ("localhost", "127.0.0.1", "[::1]", "api.orb.local"):
                    dev_root = f"http://{host}:{server.server_address[1]}"
                    Stub.requests.clear()
                    with patch("socket.getaddrinfo", side_effect=lambda host, port, *args, **kwargs: resolve("127.0.0.1", port, *args, **kwargs)):
                        cli_request("GET", dev_root + "/v1/lines", token="synthetic_account_token")
                        cli_request("POST", dev_root + "/v1/auth/activate/redeem", body={"activation_secret": "synthetic_activation_secret"})
                    check(f"{host} sends both secrets directly to the origin", Stub.requests, ["GET /v1/lines", "POST /v1/auth/activate/redeem"])
                check("configured proxy receives no request or secret", Proxy.received, [])
        finally:
            proxy.shutdown()
            proxy.server_close()
        custom = os.path.join(work, "nested", "agent-credential")
        named = run("mint", FREE, "--credential-file", custom, cwd=work, base=base, token=token)
        check("mint supports a named credential file", named.returncode == 0 and os.path.isfile(custom), True)
        if os.path.isfile(custom):
            check("named mint does not create default file", os.path.exists(credential), False)
            rotated = run("rotate", "--credential-file", custom, cwd=work, base=base, token=token)
            with open(custom) as handle:
                check("rotate updates the named credential", rotated.returncode == 0 and "plow_rotated100_token" in handle.read(), True)
            retired = run("revoke", "--credential-file", custom, cwd=work, base=base, token=token)
            check("revoke removes the named credential", retired.returncode == 0 and not os.path.exists(custom), True)
        blocker = os.path.join(work, "not-a-directory")
        with open(blocker, "w") as handle:
            handle.write("parent is a file")
        impossible = os.path.join(blocker, "credential")
        seen = len(Stub.revoked)
        failed = run("mint", FREE, "--credential-file", impossible, cwd=work, base=base, token=token)
        check("failed credential installation retires the new agent", failed.returncode != 0 and len(Stub.revoked) == seen + 1, True)
        Stub.delete_status = 500
        failed = run("mint", FREE, "--credential-file", impossible, cwd=work, base=base, token=token)
        check("failed installation and retirement identify the destination", impossible in failed.stderr, True)
        Stub.delete_status = 200
        Stub.lines = {"data": []}
        empty = run("lines", cwd=work, base=base, token=token)
        check("empty pool prints an empty table without activation guidance", empty.returncode == 0 and len(empty.stdout.splitlines()) == 1 and not empty.stderr, True)
        logged_in = run("login", cwd=work, base=base, token=token)
        check("login without pool lines does not suggest creating one", logged_in.returncode == 0 and "login --new-line" not in logged_in.stderr, True)

    server.shutdown()
    for failure in failures:
        print(f"\n{failure}", file=sys.stderr)
    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(failures)} failing")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
