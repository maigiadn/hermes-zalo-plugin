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
   ZALOUSER_HOME_CHANNEL=<uid or g:groupId>   # cron delivery target
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

Chat ids: a DM is the peer's uid; a group is `g:<groupId>`.

## Media

- Inbound photos are downloaded into the Hermes image cache (vision works);
  stickers arrive as `[sticker]`; voice/video/files arrive as a short
  placeholder (content not fetched yet).
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
