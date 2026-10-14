"""claude-token-relay (ctrelay): put a long-lived Claude token from `claude setup-token` where it is
needed, without ever showing it. Targets: paperclip.
"""
import argparse
import sys

from . import __version__
from .core import ERROR, Fail, check_token, run_setup_token, say
from .targets import DEFAULT, TARGETS


def with_session(args, fn):
    s = TARGETS[args.target].open_session(args)
    try:
        return fn(s)
    finally:
        s.close()


def cmd_login(args):
    return TARGETS[args.target].login(args)


def cmd_logout(args):
    return TARGETS[args.target].logout(args)


def cmd_rotate(args):
    token = check_token(sys.stdin.read() if args.stdin else open(args.token_file).read())
    return with_session(args, lambda s: TARGETS[args.target].put(s, token))


def cmd_renew(args):
    target = TARGETS[args.target]

    def run(s):
        if not s.usable():
            return target.put(s, "")  # only failures: report them, no token needed
        say("Running `%s setup-token`; complete the login in your browser. The token is captured and hidden."
            % args.claude)
        return target.put(s, check_token(run_setup_token(args.claude)))  # one token for every destination
    return with_session(args, run)


def cmd_check(args):
    return with_session(args, lambda s: TARGETS[args.target].check(s, args.warn_days))


def parser():
    p = argparse.ArgumentParser(prog="claude-token-relay", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def command(name, scope, help_, fn):
        c = sub.add_parser(name, help=help_)
        c.add_argument("--target", choices=sorted(TARGETS), default=DEFAULT, help="where the token goes")
        for t in TARGETS.values():
            t.add_arguments(c, scope)
        c.set_defaults(fn=fn)
        return c

    command("login", "auth", "authorize this tool with the target (Paperclip: browser approval)", cmd_login)
    command("logout", "auth", "revoke and delete the stored target credential", cmd_logout)
    c = command("renew", "use", "run `claude setup-token` and put its token in the target", cmd_renew)
    c.add_argument("--claude", default="claude", help="claude binary (default: claude on PATH)")
    c = command("rotate", "use", "put a token you already have", cmd_rotate)
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--token-file")
    g.add_argument("--stdin", action="store_true")
    c = command("check", "use", "status for cron/launchd: exit 0 ok, 1 needs re-auth, 2 expiring, 3 error", cmd_check)
    c.add_argument("--warn-days", type=int, default=30)
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
