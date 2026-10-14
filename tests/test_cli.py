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
from paperclip_claude_token.cli import ALL, MASK, Fail, Masker, choose, set_winsize, term_rows  # noqa: E402

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
            return self.reply(200, s["companies"])
        if p == "/api/companies/co2/ai-connections":
            return self.reply(200, {"currentUserId": "u1", "connections": []})
        if p == "/api/companies/co3/ai-connections":
            return self.reply(500, {"error": "boom"})
        if p == "/api/companies/co4/ai-connections":
            return self.reply(200, {"currentUserId": "u1", "connections": [
                {"id": "conn-4", "name": "Claude 4", "provider": "anthropic", "method": "subscription",
                 "ownership": "personal", "ownerUserId": "u1", "grantId": "g-4", "status": "connected"}]})
        if p == "/api/companies/co4/me/user-secrets":
            return self.reply(200, [{"definition": {"key": "ai_g_4"}, "secret": {"id": "secret-4", "latestVersion": 1}}])
        if p == "/api/companies/co/ai-connections":
            return self.reply(200, {"currentUserId": "u1", "connections": [
                {"id": "c-openai", "provider": "openai", "method": "subscription", "ownership": "personal",
                 "grantId": "g0", "status": "connected"},
                {"id": "conn-claude", "name": "My Claude subscription", "provider": "anthropic",
                 "method": "subscription", "ownership": "personal", "ownerUserId": "u1", "grantId": GRANT,
                 "status": s["status"]}] + s["extra"]})
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
        if p == "/api/companies/co4/me/user-secrets/secret-4/rotate":
            s["rotated4"].append(body)
            return self.reply(200, {"id": "secret-4", "latestVersion": 2})
        if p == "/api/companies/co/me/user-secrets/secret-1/rotate":
            s["rotated"].append(body)
            s["version"] += 1
            return self.reply(200, {"id": "secret-1", "latestVersion": s["version"]})
        self.reply(404, {"error": "nope"})


FAKE_CLAUDE = textwrap.dedent("""\
    #!%s
    import sys, time
    assert sys.argv[1:] == ["setup-token"], sys.argv
    import fcntl, struct, termios
    cols = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\\0" * 8))[1]
    assert cols == 1000, cols
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


class TerminalTest(unittest.TestCase):
    def test_winsize_roundtrip_on_a_real_pty(self):
        import pty
        master, slave = pty.openpty()
        try:
            set_winsize(slave, 37, 1000)
            self.assertEqual(term_rows(slave), 37)
            self.assertEqual(term_rows(master), 37)
        finally:
            os.close(master)
            os.close(slave)

    def test_not_a_tty_falls_back(self):
        r, w = os.pipe()
        try:
            self.assertEqual(term_rows(r), 40)
            self.assertEqual(term_rows(r, default=24), 24)
        finally:
            os.close(r)
            os.close(w)
        self.assertEqual(term_rows(9999), 40)  # bad fd


class PickerTest(unittest.TestCase):
    rows = [{"id": "a", "name": "Alpha"}, {"id": "b", "name": "Beta"}, {"id": "c", "name": "Gamma"}]

    def pick(self, answers, default=None):
        answers, prompts = list(answers), []

        def ask(prompt):
            prompts.append(prompt)
            return answers.pop(0) + "\n"
        row = choose(self.rows, "companies", lambda r: r["name"], default, "--company", ask)
        return row["id"], prompts

    def test_enter_keeps_default_and_marks_it(self):
        got, prompts = self.pick([""], default="b")
        self.assertEqual(got, "b")
        self.assertIn("2) Beta  (default)", prompts[0])
        self.assertIn("[Enter = 2]", prompts[0])

    def test_number_and_reprompt(self):
        self.assertEqual(self.pick(["3"], default="b")[0], "c")
        got, prompts = self.pick(["", "9", "1"])  # no default: Enter and out-of-range ask again
        self.assertEqual((got, len(prompts)), ("a", 3))
        with self.assertRaises(Fail):
            self.pick(["x", "y", "z"])

    def test_all_entry(self):
        pick = lambda answers, default: choose(  # noqa: E731
            self.rows, "companies", lambda r: r["name"], default, "", lambda p: answers.pop(0), allow_all=True)
        self.assertEqual(pick(["a"], "b")["id"], ALL)
        prompts = []
        self.assertEqual(choose(self.rows, "companies", lambda r: r["name"], ALL, "",
                                lambda p: prompts.append(p) or "", allow_all=True)["id"], ALL)
        self.assertIn("a) all companies  (default)", prompts[0])
        self.assertIn("[Enter = a]", prompts[0])
        self.assertEqual(pick(["2"], ALL)["id"], "b")  # a number changes it

    def test_single_row_needs_no_prompt(self):
        self.assertEqual(choose(self.rows[:1], "companies", str, None, "", ask=lambda p: 1 / 0)["id"], "a")


class CliTest(unittest.TestCase):
    def setUp(self):
        Fake.state = {"polls": 0, "version": 4, "rotated": [], "revoked": False, "status": "connected",
                      "companies": [{"id": "co", "name": "Agent Factory"}], "extra": [], "rotated4": []}
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

    def test_company_by_name_and_remembered(self):
        Fake.state["companies"] = [{"id": "co", "name": "Agent Factory", "issuePrefix": "AGE"},
                                   {"id": "co2", "name": "Other Co", "issuePrefix": "OTH"}]
        self.run_cli("login")
        r = self.run_cli("check")
        self.assertEqual(r.returncode, 3)
        self.assertIn("several companies", r.stderr)
        self.assertIn("2) Other Co  co2", r.stderr)  # no TTY: error with the list, no prompt
        r = self.run_cli("check", "--company", "nope")
        self.assertIn("no company matches", r.stderr)
        for wanted in ("co", "agent factory", "Agent", "age"):
            self.assertEqual(self.run_cli("check", "--company", wanted).returncode, 0, wanted)
        r = self.run_cli("check")  # remembered for this URL
        self.assertEqual(r.returncode, 0, r.stderr)
        prefs = Path(self.home, "cfg", "prefs.json")
        self.assertEqual(json.loads(prefs.read_text())[self.url], {"company": "co", "connection": "conn-claude"})
        self.assertEqual(stat.S_IMODE(prefs.stat().st_mode), 0o600)

    def test_approval_page_names_last_company(self):
        guid = "11111111-2222-3333-4444-555555555555"
        cfg = Path(self.home, "cfg")
        cfg.mkdir(mode=0o700)
        (cfg / "prefs.json").write_text(json.dumps({self.url: {"company": guid, "connection": "x"}}))
        self.run_cli("login")
        self.assertEqual(Fake.state["challenge"]["requestedCompanyId"], guid)
        (cfg / "prefs.json").write_text(json.dumps({self.url: {"company": "not-a-guid"}}))
        self.run_cli("login")
        self.assertIsNone(Fake.state["challenge"]["requestedCompanyId"])

    def test_all_companies(self):
        Fake.state["companies"] = [{"id": "co", "name": "Agent Factory"}, {"id": "co2", "name": "Other Co"},
                                   {"id": "co3", "name": "Broken Co"}, {"id": "co4", "name": "Fourth"}]
        self.run_cli("login")
        r = self.run_cli("renew", "--company", "all", "--claude", str(self.claude), stdin="CODE123\n")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)  # one company failed
        self.assertEqual(r.stdout.count(MASK.decode()), 1)  # setup-token ran once
        self.assertEqual(Fake.state["rotated"], [{"value": TOKEN}])  # the same token ...
        self.assertEqual(Fake.state["rotated4"], [{"value": TOKEN}])  # ... into both companies
        self.assertIn("Agent Factory: rotated secret secret-1 (connection My Claude subscription) version 4 -> 5", r.stdout)
        self.assertIn("Fourth: rotated secret secret-4 (connection Claude 4) version 1 -> 2", r.stdout)
        self.assertIn("Other Co: skipped (no active personal Anthropic subscription", r.stdout)
        self.assertIn("Broken Co: FAILED HTTP 500", r.stdout)
        self.assertIn("2 rotated, 1 failed", r.stdout)
        # "all" is remembered: the next check covers every company again without flags.
        prefs = json.loads(Path(self.home, "cfg", "prefs.json").read_text())[self.url]
        self.assertEqual(prefs["company"], "all")
        r = self.run_cli("check")
        self.assertEqual(r.returncode, 3)
        self.assertIn("Fourth: secret secret-4", r.stdout)
        self.assertIn("Agent Factory: secret secret-1", r.stdout)
        # Without the broken company everything passes.
        Fake.state["companies"].pop(2)
        self.assertEqual(self.run_cli("check").returncode, 0)
        r = self.run_cli("rotate", "--stdin", "--company", "all", "--secret-id", "x", stdin=TOKEN)
        self.assertIn("need a single company", r.stderr)

    def test_revoked_connections_ignored(self):
        def conn(i, status):
            return {"id": "old-%d" % i, "name": "old %d" % i, "provider": "anthropic", "method": "subscription",
                    "ownership": "personal", "ownerUserId": "u1", "grantId": "g-old-%d" % i, "status": status}
        Fake.state["extra"] = [conn(1, "revoked"), conn(2, "revoked"), conn(3, "revoked")]
        self.run_cli("login")
        r = self.run_cli("check", "--all")  # nothing remembered yet: revoked ones listed
        self.assertEqual(r.returncode, 3)
        self.assertIn("old-3", r.stderr)
        r = self.run_cli("check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("connection conn-claude", r.stdout)
        # Two active ones: the remembered one wins; with no memory, ask, then remember the choice.
        Fake.state["extra"].append(conn(4, "connected"))
        self.assertIn("connection conn-claude", self.run_cli("check").stdout)
        Path(self.home, "cfg", "prefs.json").unlink()
        r = self.run_cli("check")
        self.assertIn("several connections", r.stderr)
        self.assertIn("old-4", r.stderr)
        self.assertNotIn("old-1", r.stderr)
        self.assertEqual(self.run_cli("check", "--connection", "conn-claude").returncode, 0)
        r = self.run_cli("check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("connection conn-claude", r.stdout)


if __name__ == "__main__":
    unittest.main()
