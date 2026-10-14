"""Paperclip target: rotate the token into the user secret behind a Paperclip Anthropic subscription
AI connection, through Paperclip's own API.

Paperclip stores only the short-lived (~8 h) Claude OAuth access token of such a connection and
never refreshes it (paperclipai/paperclip #14182, #13805, #15270; fixes in #14698, #15319, #13726).
Remove this target once an upstream fix ships.
"""
import os
import re
import time
import json
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone

from ..core import (ALL, ATTENTION, ERROR, OK, URGENCY, Fail, choose, config_dir, load, record_rotation,
                    save, say, token_age)

NAME = "paperclip"
CREDS, PREFS = "paperclip.credentials.json", "paperclip.prefs.json"


def add_arguments(p, scope):
    """scope "auth": login/logout flags; "use": flags for renew/rotate/check."""
    p.add_argument("--url", default=os.environ.get("PAPERCLIP_URL", "http://localhost:3100"),
                   help="Paperclip base URL (env PAPERCLIP_URL)")
    p.add_argument("--no-browser", action="store_true", help="print the approval URL, do not open it")
    if scope == "auth":
        return
    p.add_argument("--company", default=os.environ.get("PAPERCLIP_COMPANY_ID"),
                   help="company id, name, unique prefix, or 'all' (remembered per URL; auto when only one)")
    p.add_argument("--connection", help="AI connection id (auto: your personal Anthropic subscription)")
    p.add_argument("--all", action="store_true", help="include revoked/archived connections")
    p.add_argument("--secret-id", help="user secret id override (skips discovery)")
    p.add_argument("--ephemeral", action="store_true",
                   help="approve a one-off board key for this command and revoke it afterwards")


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
        "command": "ctrelay login --target paperclip", "clientName": "claude-token-relay",
        "requestedAccess": "board",
        "requestedCompanyId": company_id if company_id and GUID_RE.match(company_id) else None})
    approval = ch.get("approvalUrl") or url.rstrip("/") + ch["approvalPath"]
    say("Approve access for claude-token-relay in your browser:\n  %s" % approval)
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
    return (load(CREDS).get(url.rstrip("/")) or {}).get("token")

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
    prefs = load(PREFS)
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
    save(PREFS, prefs)
    return out



# ---------------------------------------------------------------- target interface

class Session:
    def __init__(self, args):
        self.args = args
        if args.ephemeral:
            key, _ = login_flow(args.url, not args.no_browser, company_id=company_hint(args))
            self.pc = Paperclip(args.url, key)
            self.close = lambda: self.pc.api("POST", "/cli-auth/revoke-current", {})
        else:
            key = stored_key(args.url)
            if not key:
                raise Fail("not logged in to %s; run `ctrelay login` (or pass --ephemeral)" % args.url)
            self.pc, self.close = Paperclip(args.url, key), (lambda: None)
        try:
            self.found = targets(args, self.pc)  # discovery happens before any token is created
        except BaseException:
            self.close()
            raise

    def usable(self):
        return any(conn for _, conn, _, _ in self.found)


def open_session(args):
    return Session(args)


def put(s, token):
    """Rotate the same token into every target; go on after a failure, exit non-zero if any failed."""
    failed = 0
    for co, conn, secret, error in s.found:
        name = co.get("name")
        if error:
            print("%s: FAILED %s" % (name, error))
            failed += 1
            continue
        try:
            out = s.pc.api("POST", "/companies/%s/me/user-secrets/%s/rotate" % (co["id"], secret["id"]),
                           {"value": token})
        except Fail as e:
            print("%s: FAILED %s" % (name, e))
            failed += 1
            continue
        record_rotation(state_key(s.args, secret), token, connection=conn["id"])
        print("%s: rotated secret %s (connection %s) version %s -> %s, token %d chars" % (
            name, secret["id"], conn.get("name"), secret.get("latestVersion"), out.get("latestVersion"), len(token)))
        if conn.get("status") != "connected":
            print("%s: note: connection status is %s; if runs still fail, Reconnect it in the UI and run "
                  "`renew` again (a Reconnect replaces the long-lived token, paperclipai/paperclip#15237)"
                  % (name, conn.get("status")))
    if len(s.found) > 1:
        print("%d rotated, %d failed" % (len(s.found) - failed, failed))
    return ERROR if failed else OK


def check(s, warn_days):
    worst = OK
    for co, conn, secret, error in s.found:
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
        line, age = token_age(state_key(s.args, secret), warn_days)
        for part in line.split("\n"):
            print("%s: %s" % (name, part))
        worst = max(worst, code, age, key=URGENCY.index)
    return worst


def state_key(args, secret):
    return "paperclip|%s|%s" % (args.url.rstrip("/"), secret["id"])


def company_hint(args):
    """The company to name on the approval page: --company if it is an id, else the last used one."""
    wanted = getattr(args, "company", None)
    if wanted and GUID_RE.match(wanted):
        return wanted
    return (load(PREFS).get(args.url.rstrip("/")) or {}).get("company")


def login(args):
    key, user = login_flow(args.url, not args.no_browser, company_id=company_hint(args))
    creds = load(CREDS)
    creds[args.url.rstrip("/")] = {"token": key, "userId": user,
                                   "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    save(CREDS, creds)
    print("logged in to %s as user %s; board key stored in %s (0600)" % (
        args.url, user, os.path.join(config_dir(), CREDS)))
    return OK


def logout(args):
    key = stored_key(args.url)
    if not key:
        print("not logged in to %s" % args.url)
        return OK
    try:
        Paperclip(args.url, key).api("POST", "/cli-auth/revoke-current", {})
        print("board key revoked on %s" % args.url)
    except Fail as e:
        say("could not revoke on the server (%s); revoke it in the Paperclip UI" % e)
    creds = load(CREDS)
    creds.pop(args.url.rstrip("/"), None)
    save(CREDS, creds)
    print("local key removed")
    return OK
