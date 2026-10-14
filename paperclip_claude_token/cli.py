"""paperclip-claude-token: keep a Paperclip Claude-subscription AI connection on a long-lived token.

Paperclip stores only the short-lived (~8 h) Claude OAuth access token of an Anthropic subscription
AI connection and never refreshes it (paperclipai/paperclip #14182, #13805, #15270). This tool puts
an official long-lived token from `claude setup-token` into that connection's user secret, through
Paperclip's own API. Remove it once an upstream fix ships.

Never prints a token or key: only lengths, ids, versions and statuses.
"""
import argparse
import json
import os
import re
import select
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone

__version__ = "0.1.0"
TOKEN_RE = re.compile(rb"sk-ant-[A-Za-z0-9_\-]+")
TOKEN_PREFIX = b"sk-ant-"
TOKEN_LIFETIME_DAYS = 365  # what `claude setup-token` documents; an estimate, the server is the truth
MASK = b"sk-ant-***hidden***"
OK, ATTENTION, EXPIRING, ERROR = 0, 1, 2, 3


class Fail(Exception):
    pass


def say(msg):
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- local config (0700 dir, 0600 files)

def config_dir():
    base = os.environ.get("PAPERCLIP_CLAUDE_TOKEN_HOME") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "paperclip-claude-token")
    os.makedirs(base, mode=0o700, exist_ok=True)
    os.chmod(base, 0o700)
    return base


def load(name):
    try:
        with open(os.path.join(config_dir(), name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(name, data):
    path = os.path.join(config_dir(), name)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- HTTP

def http(url, method="GET", body=None, key=None):
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:  # only the server's short error string, never a body
            msg = json.loads(e.read()).get("error")
        except (ValueError, AttributeError):
            msg = None
        path = urllib.parse.urlsplit(url).path
        raise Fail("HTTP %s on %s %s%s" % (e.code, method, path, ": %s" % str(msg)[:160] if isinstance(msg, str) else ""))
    except urllib.error.URLError as e:
        raise Fail("cannot reach %s (%s)" % (url.split("/api/")[0], e.reason))
    return json.loads(raw) if raw else {}


class Paperclip:
    def __init__(self, url, key=None):
        self.url, self.key = url.rstrip("/"), key

    def api(self, method, path, body=None):
        return http(self.url + "/api" + path, method, body, self.key)


# ---------------------------------------------------------------- board key: Paperclip's CLI-auth flow

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def login_flow(url, open_browser=True, poll_s=1.0, company_id=None):
    """Same flow as `paperclipai auth login`: challenge → browser approval → board API key.

    company_id (a GUID, e.g. the last used company) only labels the approval page with that
    company's name; Paperclip's page has no company selector.
    """
    pc = Paperclip(url)
    ch = pc.api("POST", "/cli-auth/challenges", {
        "command": "paperclip-claude-token login", "clientName": "paperclip-claude-token",
        "requestedAccess": "board",
        "requestedCompanyId": company_id if company_id and GUID_RE.match(company_id) else None})
    approval = ch.get("approvalUrl") or url.rstrip("/") + ch["approvalPath"]
    say("Approve access for paperclip-claude-token in your browser:\n  %s" % approval)
    if open_browser and not os.environ.get("PAPERCLIP_NO_BROWSER"):
        webbrowser.open(approval)
    deadline = time.time() + 15 * 60
    try:
        deadline = datetime.fromisoformat(ch["expiresAt"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        pass
    poll = "%s?token=%s" % (ch["pollPath"], urllib.parse.quote(ch["token"]))
    while time.time() < deadline:
        status = pc.api("GET", poll).get("status")
        if status == "approved":
            key = ch["boardApiToken"]
            me = Paperclip(url, key).api("GET", "/cli-auth/me")
            return key, me.get("userId") or (me.get("user") or {}).get("id")
        if status in ("cancelled", "expired"):
            raise Fail("approval %s" % status)
        time.sleep(max(poll_s, ch.get("suggestedPollIntervalMs", 1000) / 1000))
    raise Fail("approval expired")


def stored_key(url):
    return (load("credentials.json").get(url.rstrip("/")) or {}).get("token")


# ---------------------------------------------------------------- discovery

def interactive():
    return sys.stdin.isatty() and sys.stderr.isatty() and not os.environ.get("PAPERCLIP_CLAUDE_TOKEN_NO_PROMPT")


def choose(rows, what, show, default=None, flag="", ask=None, allow_all=False):
    """Pick one row. Interactive: numbered list, Enter keeps the default. Otherwise the default,
    or an error with the list. allow_all adds "a) all", returned as {"id": ALL}."""
    if len(rows) == 1:
        return rows[0]
    if allow_all:
        rows = rows + [{"id": ALL}]
    ids = [r.get("id") for r in rows]
    listing = "\n".join("  %s) %s%s" % ("a" if r.get("id") == ALL else i + 1,
                                        "all %s" % what if r.get("id") == ALL else show(r),
                                        "  (default)" if r.get("id") == default else "")
                        for i, r in enumerate(rows))
    if ask is None:
        if not interactive():
            if default in ids:
                return rows[ids.index(default)]
            raise Fail("several %s; pass %s:\n%s" % (what, flag, listing))
        ask = lambda prompt: (say(prompt), sys.stdin.readline())[1]  # noqa: E731
    for _ in range(3):
        hint = ("Enter = %s" % ("a" if default == ALL else ids.index(default) + 1)) if default in ids else "number"
        answer = ask("Several %s:\n%s\nChoose [%s]: " % (what, listing, hint)).strip()
        if not answer and default in ids:
            return rows[ids.index(default)]
        if allow_all and answer.lower() == "a":
            return rows[-1]
        if answer.isdigit() and 1 <= int(answer) <= len(rows) - allow_all:
            return rows[int(answer) - 1]
    raise Fail("no %s chosen" % what)


ALL = "all"


def pick_companies(pc, wanted, remembered=None):
    """(companies, choice): one company from an id, a name or a unique name/issue prefix, or every
    company for "all"; else the picker (default: the remembered choice) or the only one."""
    rows = pc.api("GET", "/companies")
    rows = rows.get("companies", rows) if isinstance(rows, dict) else rows
    listing = "\n".join("  %s  %s" % (r.get("id"), r.get("name")) for r in rows)
    if not rows:
        raise Fail("this user is in no company")
    if (wanted or "").lower() == ALL:
        return rows, ALL
    if wanted:
        w = wanted.lower()
        for test in (lambda r: r.get("id") == wanted,
                     lambda r: str(r.get("name", "")).lower() == w,
                     lambda r: str(r.get("name", "")).lower().startswith(w)
                     or str(r.get("issuePrefix", "")).lower() == w):
            hits = [r for r in rows if test(r)]
            if len(hits) == 1:
                return hits, hits[0]["id"]
            if hits:
                raise Fail("--company %r matches several companies:\n%s" % (wanted, "\n".join(
                    "  %s  %s" % (r.get("id"), r.get("name")) for r in hits)))
        raise Fail("no company matches %r:\n%s" % (wanted, listing))
    row = choose(rows, "companies", lambda r: "%s  %s" % (r.get("name"), r.get("id")), remembered,
                 "--company <id, name, prefix or all>", allow_all=True)
    return (rows, ALL) if row["id"] == ALL else ([row], row["id"])


INACTIVE = ("revoked", "archived", "deleted")


class NoConnection(Fail):
    pass


def discover(pc, company, connection_id=None, secret_id=None, include_inactive=False, remembered=None):
    """The Anthropic subscription connection and the user secret behind its grant.

    Paperclip keys a connection grant's credential as user secret `ai_<grantId with _>`.
    Revoked/archived connections are ignored unless include_inactive (--all).
    """
    data = pc.api("GET", "/companies/%s/ai-connections" % company)
    me = data.get("currentUserId")
    every = data.get("connections", [])
    if connection_id:
        conns = [c for c in every if c.get("id") == connection_id]
    else:
        conns = [c for c in every if c.get("provider") == "anthropic" and c.get("method") == "subscription"
                 and c.get("ownership") == "personal" and c.get("ownerUserId") in (None, me)
                 and (include_inactive or c.get("status") not in INACTIVE)]
    if not conns:
        raise NoConnection("no %spersonal Anthropic subscription AI connection for this user%s" % (
            "" if include_inactive else "active ", " with id %s" % connection_id if connection_id else
            " (revoked ones are hidden; --all shows them)"))
    conn = choose(conns, "connections", lambda c: "%s  %s  %s" % (c.get("name"), c.get("status"), c.get("id")),
                  remembered, "--connection <id>")
    rows = pc.api("GET", "/companies/%s/me/user-secrets" % company)
    want_key = "ai_" + str(conn.get("grantId", "")).replace("-", "_")
    hits = [r["secret"] for r in rows if r.get("secret") and (
        r["secret"].get("id") == secret_id if secret_id else (r.get("definition") or {}).get("key") == want_key)]
    if len(hits) != 1:
        cands = ["  %s  key=%s  version=%s  status=%s" % (
            r["secret"]["id"], (r.get("definition") or {}).get("key"), r["secret"].get("latestVersion"),
            r["secret"].get("status")) for r in rows if r.get("secret")]
        raise Fail("cannot tell which user secret holds the token (looked for %s); pass --secret-id. "
                   "Your user secrets:\n%s" % (secret_id or want_key, "\n".join(cands) or "  (none)"))
    return conn, hits[0]


def targets(args, pc):
    """[(company, connection, secret, error)] for the chosen company, or every company with --company all.

    In "all" mode a company without an active connection is skipped with one line, and other errors
    are kept per company so the caller can go on. Remembers the choice per URL (ids only).
    """
    url = args.url.rstrip("/")
    prefs = load("prefs.json")
    mine = prefs.get(url, {})
    companies, choice = pick_companies(pc, args.company, mine.get("company"))
    multi = choice == ALL
    if multi and (args.connection or args.secret_id):
        raise Fail("--connection and --secret-id need a single company")
    out = []
    for co in companies:
        remembered = mine.get("connection") if not multi and mine.get("company") == co["id"] else None
        try:
            conn, secret = discover(pc, co["id"], args.connection, args.secret_id, args.all, remembered)
            out.append((co, conn, secret, None))
        except NoConnection as e:
            if not multi:
                raise
            print("%s: skipped (%s)" % (co.get("name"), str(e).split(" (")[0]))
        except Fail as e:
            if not multi:
                raise
            out.append((co, None, None, str(e).splitlines()[0]))
    if not out:
        raise Fail("no company has an active personal Anthropic subscription AI connection")
    prefs[url] = {"company": choice, "connection": None if multi else out[0][1]["id"]}
    save("prefs.json", prefs)
    return out


# ---------------------------------------------------------------- claude setup-token in a pty, masked

class Masker:
    """Pass terminal output through, replacing every sk-ant-… token; keep the tokens seen."""

    def __init__(self):
        self.pending, self.tokens = b"", []

    def feed(self, data, final=False):
        buf = self.pending + data
        matches = list(TOKEN_RE.finditer(buf))
        cut = len(buf)  # bytes from here on may still be part of a token: hold them back
        if not final:
            if matches and matches[-1].end() == len(buf):
                cut = matches.pop().start()
            else:
                for n in range(len(TOKEN_PREFIX), 0, -1):  # a split prefix like b"sk-a"
                    if buf.endswith(TOKEN_PREFIX[:n]):
                        cut = len(buf) - n
                        break
        out, pos = b"", 0
        for m in matches:
            out += buf[pos:m.start()] + MASK
            self.tokens.append(m.group().decode())
            pos = m.end()
        self.pending = buf[cut:]
        return out + buf[pos:cut]


def run_setup_token(claude="claude"):
    """Run `claude setup-token` interactively in a pty; return the token it printed (never shown)."""
    import fcntl
    import pty
    import struct
    import termios
    import tty

    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execvp(claude, [claude, "setup-token"])
        finally:
            os._exit(127)
    # Very wide terminal: the token is printed on one line, so the masker sees it whole.
    rows = 40
    try:
        rows = struct.unpack("hh", fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 4))[0] or 40
    except OSError:
        pass
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, 1000, 0, 0))
    masker, out = Masker(), sys.stdout.buffer
    stdin_tty = sys.stdin.isatty()
    old = termios.tcgetattr(sys.stdin.fileno()) if stdin_tty else None
    try:
        if stdin_tty:
            tty.setraw(sys.stdin.fileno())
        fds = [fd, sys.stdin.fileno()]
        while True:
            r, _, _ = select.select(fds, [], [])
            if fd in r:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    data = b""
                if not data:
                    break
                out.write(masker.feed(data))
                out.flush()
            if sys.stdin.fileno() in r:
                data = os.read(sys.stdin.fileno(), 1024)
                if data:
                    os.write(fd, data)
                else:
                    fds.remove(sys.stdin.fileno())
        out.write(masker.feed(b"", final=True))
        out.flush()
    finally:
        if old:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
        _, status = os.waitpid(pid, 0)
    tokens = [t for t in masker.tokens if t.startswith("sk-ant-oat")] or masker.tokens
    if not tokens:
        raise Fail("`claude setup-token` printed no token (exit %s)" % os.waitstatus_to_exitcode(status)
                   if hasattr(os, "waitstatus_to_exitcode") else "`claude setup-token` printed no token")
    return tokens[-1]


# ---------------------------------------------------------------- commands

def session(args):
    """(Paperclip client, cleanup) using the stored key, or an ephemeral one revoked afterwards."""
    if args.ephemeral:
        key, _ = login_flow(args.url, not args.no_browser, company_id=company_hint(args))
        pc = Paperclip(args.url, key)
        return pc, lambda: pc.api("POST", "/cli-auth/revoke-current", {})
    key = stored_key(args.url)
    if not key:
        raise Fail("not logged in to %s; run `paperclip-claude-token login` (or pass --ephemeral)" % args.url)
    return Paperclip(args.url, key), lambda: None


def check_token(token):
    token = token.strip()
    if not token.startswith("sk-ant-"):
        raise Fail("that does not look like a Claude token (expected sk-ant-…, got %d chars)" % len(token))
    return token


def do_rotate(args, pc, token, found):
    """Rotate the same token into every target; go on after a failure, exit non-zero if any failed."""
    failed = 0
    for co, conn, secret, error in found:
        name = co.get("name")
        if error:
            print("%s: FAILED %s" % (name, error))
            failed += 1
            continue
        try:
            out = pc.api("POST", "/companies/%s/me/user-secrets/%s/rotate" % (co["id"], secret["id"]),
                         {"value": token})
        except Fail as e:
            print("%s: FAILED %s" % (name, e))
            failed += 1
            continue
        state = load("state.json")
        state["%s|%s" % (args.url.rstrip("/"), secret["id"])] = {
            "rotatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "connection": conn["id"],
            "tokenLength": len(token)}
        save("state.json", state)
        print("%s: rotated secret %s (connection %s) version %s -> %s, token %d chars" % (
            name, secret["id"], conn.get("name"), secret.get("latestVersion"), out.get("latestVersion"), len(token)))
        if conn.get("status") != "connected":
            print("%s: note: connection status is %s; if runs still fail, Reconnect it in the UI and run "
                  "`renew` again (a Reconnect replaces the long-lived token, paperclipai/paperclip#15237)"
                  % (name, conn.get("status")))
    if len(found) > 1:
        print("%d rotated, %d failed" % (len(found) - failed, failed))
    return ERROR if failed else OK


def company_hint(args):
    """The company to name on the approval page: --company if it is an id, else the last used one."""
    wanted = getattr(args, "company", None)
    if wanted and GUID_RE.match(wanted):
        return wanted
    return (load("prefs.json").get(args.url.rstrip("/")) or {}).get("company")


def cmd_login(args):
    key, user = login_flow(args.url, not args.no_browser, company_id=company_hint(args))
    creds = load("credentials.json")
    creds[args.url.rstrip("/")] = {"token": key, "userId": user,
                                   "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    save("credentials.json", creds)
    print("logged in to %s as user %s; board key stored in %s (0600)" % (
        args.url, user, os.path.join(config_dir(), "credentials.json")))
    return OK


def cmd_logout(args):
    key = stored_key(args.url)
    if not key:
        print("not logged in to %s" % args.url)
        return OK
    try:
        Paperclip(args.url, key).api("POST", "/cli-auth/revoke-current", {})
        print("board key revoked on %s" % args.url)
    except Fail as e:
        say("could not revoke on the server (%s); revoke it in the Paperclip UI" % e)
    creds = load("credentials.json")
    creds.pop(args.url.rstrip("/"), None)
    save("credentials.json", creds)
    print("local key removed")
    return OK


def cmd_rotate(args):
    token = check_token(sys.stdin.read() if args.stdin else open(args.token_file).read())
    pc, done = session(args)
    try:
        return do_rotate(args, pc, token, targets(args, pc))
    finally:
        done()


def cmd_renew(args):
    pc, done = session(args)
    try:
        found = targets(args, pc)  # fail before the browser dance
        if not any(conn for _, conn, _, _ in found):
            return do_rotate(args, pc, "", found)  # only failures: report them, no token needed
        say("Running `%s setup-token`; complete the login in your browser. The token is captured and hidden."
            % args.claude)
        token = check_token(run_setup_token(args.claude))  # one token for every company
        return do_rotate(args, pc, token, found)
    finally:
        done()


def cmd_check(args):
    pc, done = session(args)
    try:
        found = targets(args, pc)
    finally:
        done()
    worst = OK
    for co, conn, secret, error in found:
        name = co.get("name")
        if error:
            print("%s: FAILED %s" % (name, error))
            worst = ERROR
            continue
        code = OK
        print("%s: connection %s (%s): status %s%s, grant %s" % (
            name, conn["id"], conn.get("name"), conn.get("status"),
            " (%s)" % conn["unavailableReason"] if conn.get("unavailableReason") else "", conn.get("grantId")))
        if conn.get("status") != "connected":
            print("%s: ATTENTION: needs re-authorization; Reconnect in the UI, then run `renew`" % name)
            code = ATTENTION
        print("%s: secret %s: version %s, status %s" % (
            name, secret["id"], secret.get("latestVersion"), secret.get("status")))
        st = load("state.json").get("%s|%s" % (args.url.rstrip("/"), secret["id"]))
        if not st:
            print("%s: token age: unknown (not rotated by this tool on this machine)" % name)
        else:
            rotated = datetime.fromisoformat(st["rotatedAt"])
            expires = rotated + timedelta(days=TOKEN_LIFETIME_DAYS)
            left = (expires - datetime.now(timezone.utc)).days
            print("%s: token rotated %s; estimated expiry %s (%d days left)" % (
                name, rotated.date(), expires.date(), left))
            if left <= args.warn_days and code == OK:
                print("%s: EXPIRING: run `renew` soon" % name)
                code = EXPIRING
        worst = max(worst, code, key=[OK, EXPIRING, ATTENTION, ERROR].index)  # most urgent wins
    return worst


def parser():
    p = argparse.ArgumentParser(prog="paperclip-claude-token", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=os.environ.get("PAPERCLIP_URL", "http://localhost:3100"),
                        help="Paperclip base URL (env PAPERCLIP_URL)")
    common.add_argument("--no-browser", action="store_true", help="print the approval URL, do not open it")
    target = argparse.ArgumentParser(add_help=False, parents=[common])
    target.add_argument("--company", default=os.environ.get("PAPERCLIP_COMPANY_ID"),
                        help="company id, name, unique prefix, or 'all' (remembered per URL; auto when only one)")
    target.add_argument("--connection", help="AI connection id (auto: your personal Anthropic subscription)")
    target.add_argument("--all", action="store_true", help="include revoked/archived connections")
    target.add_argument("--secret-id", help="user secret id override (skips discovery)")
    target.add_argument("--ephemeral", action="store_true",
                        help="approve a one-off board key for this command and revoke it afterwards")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", parents=[common], help="get a board API key via browser approval").set_defaults(fn=cmd_login)
    sub.add_parser("logout", parents=[common], help="revoke and delete the stored board key").set_defaults(fn=cmd_logout)
    r = sub.add_parser("renew", parents=[target], help="run `claude setup-token` and rotate its token in")
    r.add_argument("--claude", default="claude", help="claude binary (default: claude on PATH)")
    r.set_defaults(fn=cmd_renew)
    r = sub.add_parser("rotate", parents=[target], help="rotate a token you already have")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--token-file")
    g.add_argument("--stdin", action="store_true")
    r.set_defaults(fn=cmd_rotate)
    r = sub.add_parser("check", parents=[target],
                       help="status for cron/launchd: exit 0 ok, 1 needs re-auth, 2 expiring, 3 error")
    r.add_argument("--warn-days", type=int, default=30)
    r.set_defaults(fn=cmd_check)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.fn(args)
    except Fail as e:
        say("error: %s" % e)
        return ERROR
    except KeyboardInterrupt:
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
