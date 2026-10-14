# AGENTS.md

Notes for contributors and coding agents.

- Python 3.9+, standard library only. No new dependencies.
- Run `python3 -m unittest` before every commit; it is offline (fake Paperclip, fake `claude`).
- Never print, log or store a Claude token or a target credential. Output is ids, versions,
  statuses and lengths only. Every new output path gets a test that asserts the fake secret is absent.
- Test values must be obviously fake (`FAKE…`, zero UUIDs). No real hosts, ids, paths or emails.
- Do not commit `.claude/` or any personal config.
- A new target is one module in `claude_token_relay/targets/` with the interface described in
  `targets/__init__.py`, one line in `TARGETS`, and tests with a fake for its service.
