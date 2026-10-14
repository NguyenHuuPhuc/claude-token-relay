"""Offline tests: a fake Paperclip, a fake `claude` binary, fake tokens only. Run: python3 -m unittest"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paperclip_claude_token.cli import MASK, Masker  # noqa: E402

TOKEN = "sk-ant-oat01-FAKEfake_TOKEN-0123456789abcdefghijklmnopqrstuvwxyz"
BOARD = "pcp_board_FAKEKEY123"
GRANT = "00000000-1111-2222-3333-444444444444"


class Fake(BaseHTTPRequestHandler):
    state = {}

    def log_message(self, *a):
        pass

    def reply(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authed(self):
        if self.headers.get("Authorization") != "Bearer " + BOARD or self.state["revoked"]:
            self.reply(401, {"error": "Unauthorized"})
            return False
        return True

    def do_GET(self):
        s, p = self.state, self.path
        if p.startswith("/api/cli-auth/challenges/ch1?token="):
            s["polls"] += 1
            return self.reply(200, {"status": "approved" if s["polls"] >= 2 else "pending"})
        if not self.authed():
            return
        if p == "/api/cli-auth/me":
            return self.reply(200, {"userId": "u1"})
        if p == "/api/companies":
            return self.reply(200, [{"id": "co", "name": "Agent Factory"}])
        if p == "/api/companies/co/ai-connections":
            return self.reply(200, {"currentUserId": "u1", "connections": [
                {"id": "c-openai", "provider": "openai", "method": "subscription", "ownership": "personal",
                 "grantId": "g0", "status": "connected"},
                {"id": "conn-claude", "name": "My Claude subscription", "provider": "anthropic",
                 "method": "subscription", "ownership": "personal", "ownerUserId": "u1", "grantId": GRANT,
                 "status": s["status"]}]})
        if p == "/api/companies/co/me/user-secrets":
            return self.reply(200, [
                {"definition": {"key": "CLAUDE_CODE_OAUTH_TOKEN"}, "secret": {"id": "stale", "latestVersion": 9}},
                {"definition": {"key": "ai_" + GRANT.replace("-", "_")},
                 "secret": {"id": "secret-1", "latestVersion": s["version"], "status": "active"}}])
        self.reply(404, {"error": "nope"})

    def do_POST(self):
        s, p = self.state, self.path
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if p == "/api/cli-auth/challenges":
            s["challenge"] = body
            return self.reply(201, {"id": "ch1", "token": "chsecret", "boardApiToken": BOARD,
                                    "approvalPath": "/cli-auth/ch1?token=chsecret", "approvalUrl": None,
                                    "pollPath": "/cli-auth/challenges/ch1", "expiresAt": "2999-01-01T00:00:00Z",
                                    "suggestedPollIntervalMs": 10})
        if not self.authed():
            return
        if p == "/api/cli-auth/revoke-current":
            s["revoked"] = True
            return self.reply(200, {"revoked": True})
        if p == "/api/companies/co/me/user-secrets/secret-1/rotate":
            s["rotated"].append(body)
            s["version"] += 1
            return self.reply(200, {"id": "secret-1", "latestVersion": s["version"]})
        self.reply(404, {"error": "nope"})


FAKE_CLAUDE = textwrap.dedent("""\
    #!%s
    import sys, time
    assert sys.argv[1:] == ["setup-token"], sys.argv
    out = sys.stdout
    out.write("Open https://claude.ai/oauth/authorize?x=1 and paste the code: "); out.flush()
    code = sys.stdin.readline().strip()
    assert code == "CODE123", code
    tok = "%s"
    out.write("\\r\\nYour token:\\r\\n\\x1b[32m"); out.flush()
    for i in range(0, len(tok), 5):  # dribble it out in pieces
        out.write(tok[i:i + 5]); out.flush(); time.sleep(0.005)
    out.write("\\x1b[39m\\r\\nStore it safely.\\r\\n"); out.flush()
    """) % (sys.executable, TOKEN)


class MaskerTest(unittest.TestCase):
    def test_every_split(self):
        text = ("before " + TOKEN + " middle sk-ant-api03-OTHER_x after sk-a").encode()
        for size in range(1, 12):
            m = Masker()
            out = b"".join(m.feed(text[i:i + size]) for i in range(0, len(text), size)) + m.feed(b"", final=True)
            self.assertNotIn(b"FAKE", out)
            self.assertNotIn(b"OTHER", out)
            self.assertEqual(out, b"before " + MASK + b" middle " + MASK + b" after sk-a")
            self.assertEqual(m.tokens, [TOKEN, "sk-ant-api03-OTHER_x"])


class CliTest(unittest.TestCase):
    def setUp(self):
        Fake.state = {"polls": 0, "version": 4, "rotated": [], "revoked": False, "status": "connected"}
        self.srv = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.home = tempfile.mkdtemp()
        self.url = "http://127.0.0.1:%d" % self.srv.server_port
        self.claude = Path(self.home) / "fake-claude"
        self.claude.write_text(FAKE_CLAUDE)
        self.claude.chmod(0o755)

    def tearDown(self):
        self.srv.shutdown()

    def run_cli(self, *args, stdin=""):
        env = {**os.environ, "PAPERCLIP_CLAUDE_TOKEN_HOME": os.path.join(self.home, "cfg"),
               "PAPERCLIP_URL": self.url, "PAPERCLIP_NO_BROWSER": "1"}
        env.pop("PAPERCLIP_COMPANY_ID", None)
        r = subprocess.run([sys.executable, "-m", "paperclip_claude_token", *args], cwd=ROOT, env=env,
                           input=stdin, capture_output=True, text=True, timeout=30)
        for secret in ("FAKEfake", BOARD):  # the approval URL carries the challenge secret by design
            self.assertNotIn(secret, r.stdout + r.stderr)
        return r

    def test_login_check_renew_logout(self):
        r = self.run_cli("login")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Fake.state["challenge"]["requestedAccess"], "board")
        cred = Path(self.home, "cfg", "credentials.json")
        self.assertEqual(stat.S_IMODE(cred.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(cred.parent.stat().st_mode), 0o700)

        r = self.run_cli("check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("secret secret-1: version 4", r.stdout)
        self.assertIn("token age: unknown", r.stdout)

        r = self.run_cli("renew", "--claude", str(self.claude), stdin="CODE123\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(MASK.decode(), r.stdout)
        self.assertIn("paste the code", r.stdout)
        self.assertIn("version 4 -> 5, token %d chars" % len(TOKEN), r.stdout)
        self.assertEqual(Fake.state["rotated"], [{"value": TOKEN}])
        state = Path(self.home, "cfg", "state.json")
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        self.assertNotIn("FAKEfake", state.read_text())

        r = self.run_cli("check", "--warn-days", "400")
        self.assertEqual(r.returncode, 2, r.stdout)
        Fake.state["status"] = "needs_attention"
        r = self.run_cli("check")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("needs re-authorization", r.stdout)

        r = self.run_cli("logout")
        self.assertTrue(Fake.state["revoked"])
        self.assertNotIn("FAKEKEY", cred.read_text())

    def test_rotate_stdin_ephemeral_and_overrides(self):
        r = self.run_cli("rotate", "--stdin", "--ephemeral", stdin=TOKEN + "\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Fake.state["rotated"], [{"value": TOKEN}])
        self.assertTrue(Fake.state["revoked"])  # one-off key revoked afterwards
        self.assertFalse(Path(self.home, "cfg", "credentials.json").exists())

    def test_errors_are_clear(self):
        r = self.run_cli("check")
        self.assertEqual(r.returncode, 3)
        self.assertIn("not logged in", r.stderr)
        self.run_cli("login")
        r = self.run_cli("rotate", "--stdin", stdin="not-a-token")
        self.assertEqual(r.returncode, 3)
        self.assertIn("does not look like a Claude token", r.stderr)
        r = self.run_cli("check", "--secret-id", "missing")
        self.assertEqual(r.returncode, 3)
        self.assertIn("secret-1  key=ai_00000000", r.stderr)  # candidates listed, no values
        self.assertEqual(Fake.state["rotated"], [])


if __name__ == "__main__":
    unittest.main()
