# claude-token-relay

`claude-token-relay` (short: `ctrelay`) puts an official long-lived Claude token from
`claude setup-token` where your tools need it. It never shows the token on screen or in logs.

- It runs `claude setup-token` for you, captures the token while masking it, and hands it to a
  **target**.
- It remembers when each token was put in, and `check` warns before the token expires (exit codes
  for cron and launchd).
- Python 3.9+ standard library only. No telemetry.

Targets today: **paperclip**. See the roadmap for others.

Not affiliated with Anthropic or Paperclip.

## Install

```bash
pipx install git+https://github.com/NguyenHuuPhuc/claude-token-relay
```

Other ways to run it:
- `uvx --from git+https://github.com/NguyenHuuPhuc/claude-token-relay ctrelay …`
- from a clone: `python3 -m claude_token_relay …`

`renew` needs the `claude` CLI on your PATH.

## Commands

```bash
ctrelay login                         # authorize with the target (Paperclip: browser approval)
ctrelay renew [--company all]         # run `claude setup-token`, capture the token hidden, put it in
ctrelay rotate --token-file F         # put a token you already have (or --stdin)
ctrelay check                         # exit 0 ok, 1 needs re-auth, 2 expiring, 3 error
ctrelay logout                        # revoke and delete the stored target credential
```

`--target` selects the target. The default is `paperclip`.

`check` assumes the token expires 365 days after the last time this machine put it in. The real
expiry is whatever Anthropic set for the token.

You can run `check` weekly from cron:

```
0 9 * * 1  ctrelay check >/dev/null || osascript -e 'display notification "Claude token needs attention" with title "ctrelay"'
```

## Target: paperclip

The problem: [Paperclip](https://github.com/paperclipai/paperclip) stores only the short-lived
(about 8 h) Claude OAuth access token of an Anthropic subscription AI connection. It never
refreshes the token, and the connection keeps showing "connected". Agent runs then fail with errors
such as `ACP agent reported a terminal access failure`.

- Upstream issues: paperclipai/paperclip#14182, #13805, #15270.
- paperclipai/paperclip#15237: a later **Reconnect overwrites** the long-lived token with a
  short-lived one again. Run `renew` after any Reconnect.
- Fixes in progress: #14698, #15319, #13726.

**Remove this target (and probably this tool) once one of those fixes ships in your Paperclip
version.**

How it works:
1. `login` gets a Paperclip board API key through Paperclip's own CLI approval flow, the same one
   `paperclipai auth login` uses. The approval page names the last company you used.
2. It finds the user secret behind your personal Anthropic subscription connection: user secret
   `ai_<grantId>` of the connection's grant.
3. It rotates that secret to the token with
   `POST /api/companies/:id/me/user-secrets/:secretId/rotate`.

Runs read the latest secret version, so no restart is needed. The tool does not change Paperclip's
code or database.

Options:
- `--url` sets the Paperclip URL (env `PAPERCLIP_URL`, default `http://localhost:3100`).
- `--company` takes an id, a name, a unique prefix, an issue prefix, or `all`.
  - With `all`, every company where you have an active connection gets the same token.
  - Companies without one are skipped, with one line saying why.
  - Each company gets its own result line. The tool carries on after a failure and exits non-zero
    if any company failed.
- `--connection` and `--secret-id` override discovery. Revoked and archived connections are
  ignored unless you pass `--all`.
- `--ephemeral` approves a one-off board key, uses it, and revokes it again, so nothing is stored.
- The last company and connection per URL are remembered.
  - In a terminal, a numbered picker shows them as the default; Enter keeps the default.
  - Without a TTY (cron, launchd, CI), the remembered choice is used, or the command exits with
    the list.
  - `CLAUDE_TOKEN_RELAY_NO_PROMPT=1` forces the no-TTY behaviour.

## Threat model

| What | Where | Notes |
|---|---|---|
| Target credential (Paperclip board API key) | `~/.config/claude-token-relay/paperclip.credentials.json`: directory 0700, file 0600. Override the location with `CLAUDE_TOKEN_RELAY_HOME` or `XDG_CONFIG_HOME`. | A Paperclip board key has your user's full board access; Paperclip board keys have no scopes. Use `--ephemeral` to store nothing. `logout` revokes the key. |
| Claude long-lived token | Never written to disk by this tool. It goes from `claude setup-token`'s output, through memory, to the target. | `claude setup-token` runs in a pseudo-terminal 1000 columns wide, so the token is printed on one line. Every `sk-ant-…` string in the output is shown as `sk-ant-***hidden***`. |
| `state.json`, `paperclip.prefs.json` | Same directory, 0600 | Rotation time, token length, and the ids you last chose. No secrets. |
| Output | stdout and stderr | Ids, versions, statuses and lengths only. For HTTP errors, only the server's short `error` string is shown. |

Settings from the earlier `paperclip-claude-token` tool (`~/.config/paperclip-claude-token`) are
moved here automatically on first run.

## Roadmap (targets not built yet)

- `.env` file: set `CLAUDE_CODE_OAUTH_TOKEN` in a local env file (0600).
- GitHub Actions secret, through `gh secret set`.
- Docker or Kubernetes secret.

A target is one small module in `claude_token_relay/targets/`. The interface is described in
`claude_token_relay/targets/__init__.py`.

## Uninstall

1. `ctrelay logout`: revokes the stored target credential.
2. `rm -rf ~/.config/claude-token-relay`
3. `pipx uninstall claude-token-relay`
4. Optional: revoke the long-lived token from your Claude account settings, then Reconnect the AI
   connection in Paperclip.

## Development

```bash
python3 -m unittest
```

The tests are offline. They use a fake Paperclip server and a fake `claude` binary that prints its
token in small pieces, and they check that no token or key appears in any output.

MIT licensed.
