# paperclip-claude-token

Keeps a [Paperclip](https://github.com/paperclipai/paperclip) **Anthropic subscription AI connection**
running on an official long-lived token from `claude setup-token`. It is a small CLI that uses only
the Python standard library.

## The problem

Paperclip stores only the short-lived (about 8 h) Claude OAuth access token of a subscription
connection. It never refreshes that token, and the connection keeps showing "connected". Agent runs
then fail with errors such as `ACP agent reported a terminal access failure`.

- paperclipai/paperclip#14182, #13805, #15270: the token is never refreshed.
- paperclipai/paperclip#15237: a later **Reconnect overwrites** the long-lived token with a
  short-lived one again.
- Fixes in progress upstream: #14698, #15319, #13726.

**Remove this tool once one of those fixes ships in your Paperclip version.**

## What it does

`claude setup-token` is Anthropic's own way to get a long-lived (about one year) token for a Claude
subscription. This tool:

1. gets a Paperclip board API key through Paperclip's own CLI approval flow, the same one
   `paperclipai auth login` uses;
2. finds the user secret behind your Anthropic subscription connection: user secret
   `ai_<grantId>` of the connection's grant;
3. rotates that secret to the long-lived token with
   `POST /api/companies/:id/me/user-secrets/:secretId/rotate`.

Runs read the secret at its latest version, so no restart is needed. The tool does not call any
Anthropic endpoint itself, and it does not touch Paperclip's code or database.

## Install

```bash
pipx install git+https://github.com/NguyenHuuPhuc/paperclip-claude-token
```

You can also run it with `uvx --from git+https://github.com/NguyenHuuPhuc/paperclip-claude-token paperclip-claude-token …`,
or from a clone with `python3 -m paperclip_claude_token …`. It needs Python 3.9+ and has no
dependencies. `renew` also needs the `claude` CLI on your PATH.

## Use

```bash
paperclip-claude-token login             # approve in the browser; stores a board key (0600)
paperclip-claude-token renew             # runs `claude setup-token`, captures the token hidden, rotates it in
paperclip-claude-token check             # status; exit 0 ok, 1 needs re-auth, 2 expiring, 3 error
paperclip-claude-token logout            # revokes the board key on the server and deletes it locally
```

- If you already have a token: `paperclip-claude-token rotate --token-file FILE` or `… rotate --stdin`.
- `--ephemeral` on `renew`, `rotate` or `check` approves a one-off board key, uses it, and revokes
  it again, so nothing is stored.
- Paperclip: `--url` (env `PAPERCLIP_URL`, default `http://localhost:3100`).
- Company: `--company` takes an id, a name, a name prefix or an issue prefix (env
  `PAPERCLIP_COMPANY_ID`). It is picked automatically when you have only one.
- All companies: `--company all`, or `a` in the picker. `check`, `rotate` and `renew` then cover
  every company where you have an active Anthropic subscription connection.
  - Companies without such a connection are skipped, with one line saying why.
  - `renew` runs `claude setup-token` once and rotates the same token into every company.
  - Each company gets its own result line (secret id, version before -> after). The tool carries
    on after a failure and exits non-zero if any company failed.
  - `all` is remembered as the default, like a single company.
- Connection: `--connection`. By default the tool uses your one personal Anthropic subscription
  connection. Revoked and archived connections are ignored; `--all` includes them. It asks only
  when more than one candidate remains.
- The last company and connection used for each URL are remembered in `prefs.json` (ids only).
  - In a terminal, when there are several companies or connections, a numbered picker appears
    with the remembered one marked as the default; Enter keeps it.
  - Without a TTY (cron, launchd, CI), the remembered choice is used. With no remembered choice,
    the command exits with the list. `PAPERCLIP_CLAUDE_TOKEN_NO_PROMPT=1` forces this mode.
  - `login` and `--ephemeral` send the last used company as `requestedCompanyId`, so Paperclip's
    approval page shows its name. Paperclip's page has no company selector of its own.
- `--secret-id` skips discovery. When discovery is ambiguous, the tool lists your user secrets by
  id, key and version, never by value.

Reminder, for example as a weekly cron job:

```
0 9 * * 1  paperclip-claude-token check >/dev/null || osascript -e 'display notification "Paperclip Claude token needs attention" with title "paperclip-claude-token"'
```

`check` estimates expiry as 365 days after the last rotation done on this machine. The real expiry
is whatever Anthropic set for the token.

## Threat model

| What | Where | Notes |
|---|---|---|
| Board API key | `~/.config/paperclip-claude-token/credentials.json`: directory 0700, file 0600. Override the location with `PAPERCLIP_CLAUDE_TOKEN_HOME` or `XDG_CONFIG_HOME`. | It is a full board key of your Paperclip user: Paperclip board keys have no scopes. Use `--ephemeral` if you do not want one stored. Revoke it with `logout`, or in the Paperclip UI. |
| Claude long-lived token | Never written to disk by this tool. It goes from `claude setup-token`'s output, through memory, to Paperclip's encrypted secret store. | While `renew` runs, every `sk-ant-…` string in the terminal output is replaced with `sk-ant-***hidden***`. The tool runs the CLI in a pseudo-terminal 1000 columns wide, so the token is printed on one line and the masking sees it whole. |
| `state.json`, `prefs.json` | Same directory, 0600 | Rotation time, token length, and the company and connection ids you last used. No secrets. |
| Output | stdout and stderr | Ids, versions, statuses and lengths only. For HTTP errors, only the server's short `error` string is shown. |

Anyone who can run code as your OS user can read the board key, the same as the Paperclip CLI's own
key store. The long-lived token gives access to your Claude subscription, so treat the Paperclip
instance that holds it accordingly.

## Uninstall

1. `paperclip-claude-token logout`: revokes the board key and removes it from local storage.
2. `rm -rf ~/.config/paperclip-claude-token`
3. `pipx uninstall paperclip-claude-token`
4. Optional: revoke the long-lived token from your Claude account settings. After that, Reconnect
   the AI connection in Paperclip.

## Development

```bash
python3 -m unittest
```

The tests are offline. They use a fake Paperclip server and a fake `claude` binary that prints its
token in small pieces, and they check that no token or key appears in any output.

MIT licensed. Not affiliated with Paperclip or Anthropic.
