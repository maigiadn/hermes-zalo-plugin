# Zalo personal-account platform plugin for Hermes Agent

Lets Hermes chat from a **regular (personal) Zalo account**, not a Zalo Bot.
Uses [`zca-js`](https://github.com/RFS-ADRENO/zca-js) — an **unofficial**
Zalo Web API — inside a small Node bridge (`bridge/bridge.mjs`) that the
Python adapter spawns and talks to over stdin/stdout JSON lines.

> Zalo may lock or restrict accounts automated this way. Use a secondary
> ("nick phụ") account, not your main one.

For the official Zalo Bot API integration see `../zalo/`.

## Setup

1. Bridge deps (already installed):

   ```
   cd ~/.hermes/plugins/zalouser/bridge && npm install
   ```

2. QR login (one-off; re-run when the session expires):

   ```
   cd ~/.hermes/plugins/zalouser/bridge && node bridge.mjs login
   ```

   It writes the QR to `~/.hermes/zalouser/qr.png`. Open it and scan with the
   Zalo app **of the account Hermes should use**, then confirm on the phone.
   The session (cookies, imei, user agent) is saved to
   `~/.hermes/zalouser/credentials.json` (mode 600) — treat it like a
   password. Check it with `node bridge.mjs whoami`.

3. Allowlist (in `~/.hermes/.env`):

   ```
   ZALOUSER_ALLOWED_USERS=<uid>[,<uid>...]   # who Hermes answers in DMs
   # optional:
   ZALOUSER_ALLOWED_GROUPS=<groupId>          # groups it may answer in
   ZALOUSER_GROUP_REQUIRE_MENTION=true        # default: only when @mentioned / replied to
   ZALOUSER_GROUP_FOLLOWUP_MINUTES=30         # answer follow-ups without a mention (0 = off)
   ZALOUSER_HOME_CHANNEL=<uid or g:groupId>   # cron delivery target
   # passive group log (off by default, see below):
   ZALOUSER_GROUP_LOG=false
   ZALOUSER_GROUP_LOG_DIR=<dir>               # default $HERMES_HOME/zalouser/groups
   ZALOUSER_GROUP_LOG_RETENTION_DAYS=30
   ```

   Don't know the uid? Leave the allowlist empty, restart, and message the
   account: the gateway log shows
   `[zalouser] ignored DM from <uid> (<name>) — add to ZALOUSER_ALLOWED_USERS`.

4. Enable + restart:

   ```
   hermes plugins enable zalouser
   systemctl --user restart hermes-gateway.service
   ```

   The platform auto-enables once `credentials.json` exists
   (`ZALOUSER_DISABLED=true` keeps it off). Log line on success:
   `[zalouser] connected as <name> (uid <id>) ...`.

## Access policy

A personal account receives messages from everyone, so the adapter gates at
intake and **never sends pairing codes**:

- DMs: forwarded only from `ZALOUSER_ALLOWED_USERS` (or everyone with
  `ZALOUSER_ALLOW_ALL_USERS=true` — not recommended). Others are dropped
  silently; their uid is logged at most once an hour.
- Groups: ignored unless the group id is in `ZALOUSER_ALLOWED_GROUPS`; there,
  only messages that @mention the account or reply to it are forwarded. When
  `ZALOUSER_ALLOWED_USERS` is set, only those users can trigger it in groups.
- Messages sent by the account itself (e.g. from its phone) are ignored.
- Slash commands work in groups: a leading self-mention is dropped when a
  command follows, so `@Account /new` reaches the gateway as `/new`.

Chat ids: a DM is the peer's uid; a group is `g:<groupId>`.

### Follow-up window

After an allowlisted user @mentions (or replies to) the account in a group,
their next messages in that group are forwarded **without a mention** for
`ZALOUSER_GROUP_FOLLOWUP_MINUTES` (default 30) counted from the latest
mention. Not counted: messages that @mention someone else, quote someone
else's message, or are stickers. Windows live in memory (lost on restart).

Follow-ups are sent with `reply_expected=False`, so the agent may answer with
a bare `[SILENT]` when the message was not meant for it and the gateway sends
nothing. Tell the agent this in its persona (e.g. "a group message without
@<name> is a follow-up; reply exactly `[SILENT]` if it isn't for you").

### Quotes

When a user quotes a message and addresses the account, the quoted message is
passed to the gateway as `reply_to_text` (the agent sees
`[Replying to: "<sender>: <text>"]`). The adapter keeps the last 1000 group
messages in memory to resolve the quote to the original sender, full text and
saved attachment path; otherwise it falls back to the short text Zalo sends
with the quote.

### Routing users to different profiles

With `gateway.multiplex_profiles`, route chats or **individual senders** to a
profile in `config.yaml` (a `user_id` route is the most specific):

```yaml
gateway:
  profile_routes:
    - {name: owner-dm,  platform: zalouser, chat_id: '<owner uid>',  profile: assistant}
    - {name: group,     platform: zalouser, chat_id: 'g:<groupId>',  profile: assistant}
    - {name: guest,     platform: zalouser, user_id: '<guest uid>',  profile: assistant-guest}
```

Give the guest profile a minimal toolset (e.g. `platform_toolsets: {zalouser: [web]}`).
Anyone in `ZALOUSER_ALLOWED_USERS` **without** a route lands in the default
profile with its full toolset — add a route for every allowed user.

Also set `platform_toolsets.zalouser` explicitly in the target profile: there
is no built-in `hermes-zalouser` bundle, so without it the agent gets no tools.

## Passive group log (opt-in)

With `ZALOUSER_GROUP_LOG=true`, every message in an allowed group — from any
member, mentioned or not — is appended to
`<ZALOUSER_GROUP_LOG_DIR>/<groupId>/YYYY-MM-DD.log`
(`[time] Name (uid) *to-bot*: text`, plus quoted text and saved attachment
paths), and photos/files/voice/video are downloaded to `<groupId>/files/`
(https + public addresses only, 30 MB cap). Only messages addressed to the
account trigger the agent; the agent can read the log with its file/terminal
tools ("summarise the group", "read the file X sent"). Logs and attachments
older than `ZALOUSER_GROUP_LOG_RETENTION_DAYS` (default 30) are deleted at
startup and every 6 hours.

> **Privacy:** this stores other members' messages on your machine. Tell the
> group before enabling it.

## Media

- Inbound photos are downloaded into the Hermes image cache (vision works);
  stickers arrive as `[sticker]`. Voice/video/files arrive as a placeholder,
  or — with the group log on — as `[file … saved at <path>]` (content of
  voice/video is not transcribed).
- Outbound images (local file or URL), documents and videos are uploaded
  natively. Long text is split at ~2000 chars; Markdown is stripped.

## Caveats

- Only **one Zalo Web listener** per account: opening the account on Zalo
  Web/PC kicks the bridge (it reconnects, which in turn kicks the web
  session). Use the phone app for the bot account.
- Session expiry → gateway shows `zalouser_session_expired`; re-run step 2.
- No out-of-process cron sender: cron delivery works through the running
  gateway only.
- `hermes update` doesn't touch this user plugin, but a profile with its own
  `plugins/` dir won't see it (only the `default` profile loads it).
