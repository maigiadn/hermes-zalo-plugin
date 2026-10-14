# Zalo Bot API platform plugin for Hermes Agent

Connects a **Zalo Bot** (Zalo Bot API, `https://bot.zaloplatforms.com`) to the
Hermes gateway. Telegram-Bot-API-shaped: `getMe`, `sendMessage`, `sendPhoto`,
`sendChatAction`, `getUpdates`, `setWebhook`.

This is the **bot** integration (Zalo Official Account / Bot), not personal
Zalo. For a personal account see `../zalouser/`.

## Setup

1. Get the bot token (`<botId>:<secret>`) from https://bot.zaloplatforms.com.

2. Add it to `~/.hermes/.env`:

   ```
   ZALO_BOT_TOKEN=<botId>:<secret>
   # optional:
   ZALO_ALLOWED_USERS=<your-zalo-user-id>        # allowlist; omit + set ALLOW_ALL for open
   ZALO_ALLOW_ALL_USERS=false
   ZALO_HOME_CHANNEL=<chat-id-for-cron-delivery>
   ```

3. Enable + restart:

   ```
   hermes plugins enable zalo
   hermes gateway restart
   ```

4. Check: `hermes status` → "Zalo ✓ configured (plugin)" and the gateway log
   should show `[zalo] connected bot=... mode=polling`.

## Transport

- **Long-polling (default)** — no public URL needed. `getUpdates` returns one
  update per call, error_code 408 = "no message in the window".
- **Webhook** — set `ZALO_WEBHOOK_URL=https://.../webhooks/zalo` (plus
  `ZALO_WEBHOOK_PORT`, optional `ZALO_WEBHOOK_SECRET`). The adapter runs an
  aiohttp server and calls `setWebhook`.

## Media

- **Inbound images** — downloaded from `message.photo_url` into the Hermes
  image cache (SSRF-safe) and attached to the message, so the agent's vision
  tools can read them. The `caption` becomes the message text. Works in
  polling mode, no config needed.
- **Outbound — public HTTPS image URL** — works directly (`sendPhoto`).
- **Outbound — local image file** — Zalo's `sendPhoto` is URL-only, so the
  adapter runs a tiny media server (`ZALO_MEDIA_HOST:ZALO_MEDIA_PORT`,
  default `127.0.0.1:8088`) that serves the file over a tokened path with a
  10-minute TTL, and hands Zalo `${ZALO_PUBLIC_MEDIA_URL}/z/<token>`.
  **Requires `ZALO_PUBLIC_MEDIA_URL`** — a public HTTPS base that routes to
  that port (e.g. add a cloudflared ingress rule
  `zalo-media.<domain>` → `http://localhost:8088`). Without it, local-file
  sends fall back to a short "couldn't deliver" notice (the host path is
  never leaked into chat).

## Not yet implemented

- Inbound stickers/voice/video (stickers show as `[sticker]`, others ignored).
- Group mention gating (relies on the gateway allowlist only).
