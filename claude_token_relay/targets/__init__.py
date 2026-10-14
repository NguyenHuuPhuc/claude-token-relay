"""Targets: where a captured token goes. One module each, registered below.

A target module provides:
  NAME
  add_arguments(parser, scope)   scope "auth" (login/logout) or "use" (renew/rotate/check)
  login(args) / logout(args)     -> exit code
  open_session(args)             -> session with .usable() and .close(); discovers before any token exists
  put(session, token)            -> exit code; never prints the token
  check(session, warn_days)      -> exit code (core OK/ATTENTION/EXPIRING/ERROR)
"""
from . import paperclip

TARGETS = {m.NAME: m for m in (paperclip,)}
DEFAULT = paperclip.NAME
