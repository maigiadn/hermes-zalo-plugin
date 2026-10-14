# hermes-zalo-plugin

Zalo platform plugins for [Hermes Agent](https://github.com/NousResearch/hermes-agent):
chat with your Hermes agent over Zalo, Vietnam's most used messaging app.

| Plugin | Platform name | Connects | Transport |
|---|---|---|---|
| [`zalo/`](zalo/) | `zalo` | a **Zalo Bot** (official Zalo Bot API) | long-polling or webhook |
| [`zalouser/`](zalouser/) | `zalouser` | a **personal Zalo account** | Node bridge using the unofficial [`zca-js`](https://github.com/RFS-ADRENO/zca-js) |

Both are ordinary Hermes user plugins: no change to Hermes core.

## Features

**`zalo` (Bot API)**
- Text and photos in DMs and groups; inbound photos reach the agent's vision tools.
- Outbound images (URL, or local files via a small built-in media server).
- Allowlist (`ZALO_ALLOWED_USERS`), group allowlist, cron delivery target.

**`zalouser` (personal account)**
- One-off QR login; session stored locally (mode 600).
- Strict intake policy: DMs only from `ZALOUSER_ALLOWED_USERS`, groups only when allowlisted and the
  account is @mentioned or replied to. Never sends pairing codes to strangers.
- **Follow-up window**: after an allowlisted user mentions the account in a group, their next
  messages there are answered without a mention for 30 minutes (configurable). Messages aimed at
  other people are skipped, and the agent may stay silent (`[SILENT]`) on the rest.
- **Quotes**: a quoted message reaches the agent with its sender, full text and saved attachment.
- **Slash commands in groups**: `@Account /new` is recognised as `/new`.
- **Passive group log** (opt-in): every message and attachment of an allowed group is saved to daily
  log files the agent can read ("summarise the group", "read the file X sent"), auto-deleted after
  30 days.
- Outbound images, documents and videos; long replies split at ~2000 chars; Markdown stripped.

## Requirements

- Hermes Agent with platform plugin support (`ctx.register_platform`), running the messaging gateway.
- `zalouser`: Node.js 18+ and npm.

## Install

```bash
git clone https://github.com/maigiadn/hermes-zalo-plugin.git
cd hermes-zalo-plugin

# Zalo Bot API
cp -r zalo ~/.hermes/plugins/zalo
hermes plugins enable zalo

# Personal Zalo account
cp -r zalouser ~/.hermes/plugins/zalouser
(cd ~/.hermes/plugins/zalouser/bridge && npm install)
hermes plugins enable zalouser
```

To keep the installed copy tied to this repo, symlink instead of copying
(`ln -s "$PWD/zalouser" ~/.hermes/plugins/zalouser`).

Then follow each plugin's README for credentials and environment variables:
[`zalo/README.md`](zalo/README.md) · [`zalouser/README.md`](zalouser/README.md).
Restart the gateway after any change (`hermes gateway restart`, or
`systemctl --user restart hermes-gateway.service`).

## Security and privacy

- `zalouser` uses an **unofficial** API. Zalo may lock or restrict accounts automated this way;
  prefer a secondary account. Do not open the account on Zalo Web/PC while the bridge runs.
- `~/.hermes/zalouser/credentials.json` is equivalent to a password. Never commit it.
- With multi-profile routing, every user in `ZALOUSER_ALLOWED_USERS` needs a `profile_routes`
  entry; an allowed user without a route lands in the default profile with its full toolset.
  Route guests to a profile with a minimal toolset (see `zalouser/README.md`).
- The passive group log stores **other people's messages** on your machine. Tell the group before
  enabling it.

## License

[MIT](LICENSE). Not affiliated with Zalo or VNG.
