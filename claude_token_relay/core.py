"""Core of claude-token-relay: capture a long-lived Claude token from `claude setup-token` without
showing it, track its age, and pick things in the terminal. Targets (where the token goes) live in
claude_token_relay/targets/.

Never prints a token or key: only lengths, ids, versions and statuses.
"""
import json
import os
import re
import select
import shutil
import sys
from datetime import datetime, timedelta, timezone

TOKEN_RE = re.compile(rb"sk-ant-[A-Za-z0-9_\-]+")
TOKEN_PREFIX = b"sk-ant-"
TOKEN_LIFETIME_DAYS = 365  # what `claude setup-token` documents; an estimate, the server is the truth
MASK = b"sk-ant-***hidden***"
OK, ATTENTION, EXPIRING, ERROR = 0, 1, 2, 3


class Fail(Exception):
    pass


def say(msg):
    print(msg, file=sys.stderr, flush=True)

OK, ATTENTION, EXPIRING, ERROR = 0, 1, 2, 3
URGENCY = [OK, EXPIRING, ATTENTION, ERROR]  # max(..., key=URGENCY.index): most urgent wins


# ---------------------------------------------------------------- local config (0700 dir, 0600 files)

OLD_FILES = {"credentials.json": "paperclip.credentials.json", "prefs.json": "paperclip.prefs.json"}
_migrated = False


def config_dir():
    base = os.environ.get("CLAUDE_TOKEN_RELAY_HOME") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "claude-token-relay")
    os.makedirs(base, mode=0o700, exist_ok=True)
    os.chmod(base, 0o700)
    global _migrated
    if not _migrated:
        _migrated = True
        migrate_old_config(base)
    return base


def migrate_old_config(new):
    """Move settings of the old `paperclip-claude-token` tool here once: board keys and remembered
    choices become the paperclip target's files, token-age records get a "paperclip|" prefix."""
    old = os.environ.get("PAPERCLIP_CLAUDE_TOKEN_HOME") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "paperclip-claude-token")
    if not os.path.isdir(old) or os.path.abspath(old) == os.path.abspath(new):
        return

    def read(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    for name, new_name in OLD_FILES.items():
        data = read(os.path.join(old, name))
        if data is not None and not os.path.exists(os.path.join(new, new_name)):
            write(os.path.join(new, new_name), data)
    data = read(os.path.join(old, "state.json"))
    if data:
        state = read(os.path.join(new, "state.json")) or {}
        for key, value in data.items():
            state.setdefault("paperclip|" + key, value)
        write(os.path.join(new, "state.json"), state)
    shutil.rmtree(old, ignore_errors=True)
    say("moved settings from %s to %s" % (old, new))


def write(path, data):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load(name):
    try:
        with open(os.path.join(config_dir(), name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(name, data):
    write(os.path.join(config_dir(), name), data)


# ---------------------------------------------------------------- token age (local record, no values)

def record_rotation(key, token, **meta):
    state = load("state.json")
    state[key] = {"rotatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "tokenLength": len(token), **meta}
    save("state.json", state)


def token_age(key, warn_days):
    """(line, code) about the token last put at `key` by this machine."""
    st = load("state.json").get(key)
    if not st:
        return "token age: unknown (not rotated by this tool on this machine)", OK
    rotated = datetime.fromisoformat(st["rotatedAt"])
    expires = rotated + timedelta(days=TOKEN_LIFETIME_DAYS)
    left = (expires - datetime.now(timezone.utc)).days
    line = "token rotated %s; estimated expiry %s (%d days left)" % (rotated.date(), expires.date(), left)
    if left <= warn_days:
        return line + "\nEXPIRING: run `renew` soon", EXPIRING
    return line, OK


def check_token(token):
    token = token.strip()
    if not token.startswith("sk-ant-"):
        raise Fail("that does not look like a Claude token (expected sk-ant-…, got %d chars)" % len(token))
    return token


# ---------------------------------------------------------------- terminal picker

def interactive():
    return sys.stdin.isatty() and sys.stderr.isatty() and not os.environ.get("CLAUDE_TOKEN_RELAY_NO_PROMPT")


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


def term_rows(fd, default=40):
    """Rows of the terminal on fd; default when fd is not a TTY or the ioctl fails.

    struct winsize is four unsigned shorts (8 bytes): rows, cols, xpixel, ypixel.
    """
    import fcntl
    import struct
    import termios

    try:
        return struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))[0] or default
    except (OSError, SystemError, ValueError):
        return default


def set_winsize(fd, rows, cols):
    import fcntl
    import struct
    import termios

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def run_setup_token(claude="claude"):
    """Run `claude setup-token` interactively in a pty; return the token it printed (never shown)."""
    import pty
    import termios
    import tty

    rows = term_rows(sys.stdin.fileno())
    pid, fd = pty.fork()
    if pid == 0:
        try:
            # Very wide terminal, set before exec: the token is printed on one line, so the masker
            # sees it whole.
            set_winsize(0, rows, 1000)
            os.execvp(claude, [claude, "setup-token"])
        finally:
            os._exit(127)
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


