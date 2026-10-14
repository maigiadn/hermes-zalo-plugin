"""Zalo Bot API platform adapter for Hermes Agent.

Connects a Zalo bot (created at https://bot.zaloplatforms.com) to the Hermes
gateway. The Zalo Bot API is Telegram-Bot-API-shaped:

    POST {api_base}/bot{token}/{method}
    -> {"ok": bool, "result": {...}, "error_code": int, "description": str}

Methods used: getMe, sendMessage, sendPhoto, sendChatAction, getUpdates,
setWebhook, deleteWebhook, getWebhookInfo.

Transport:
  * Long-polling (default) — getUpdates returns ONE update per call and
    blocks up to `timeout` seconds; error_code 408 means "no update".
    No public URL required.
  * Webhook (when ZALO_WEBHOOK_URL is set) — runs a small aiohttp server;
    Zalo POSTs `{"ok":true,"result":<update>}` and echoes the secret in the
    `X-Bot-Api-Secret-Token` header.

Env vars (see plugin.yaml):
  ZALO_BOT_TOKEN            required, "<botId>:<secret>"
  ZALO_ALLOWED_USERS        comma-separated user IDs (gateway allowlist)
  ZALO_ALLOW_ALL_USERS      "true" disables the allowlist
  ZALO_ALLOWED_GROUPS       comma-separated group IDs
  ZALO_HOME_CHANNEL         default chat ID for cron delivery
  ZALO_WEBHOOK_URL          public HTTPS URL -> enables webhook mode
  ZALO_WEBHOOK_HOST         webhook bind host (default 127.0.0.1)
  ZALO_WEBHOOK_PORT         webhook port (default 8087)
  ZALO_WEBHOOK_SECRET       secret token (auto-generated if empty)
  ZALO_API_BASE             API base override
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import secrets
import time
from typing import Any, Dict, Optional, Tuple

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

try:  # markdown -> plain text; Zalo renders markdown literally
    from gateway.platforms.helpers import strip_markdown as _strip_markdown
except Exception:  # pragma: no cover - defensive
    def _strip_markdown(text: str) -> str:
        return text

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret


logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://bot-api.zaloplatforms.com"
DEFAULT_WEBHOOK_HOST = "127.0.0.1"
DEFAULT_WEBHOOK_PORT = 8087
DEFAULT_MEDIA_HOST = "127.0.0.1"
DEFAULT_MEDIA_PORT = 8088
MAX_ZALO_TEXT = 2000
_POLL_TIMEOUT_SEC = 30
_WEBHOOK_MAX_BODY_BYTES = 1_048_576  # 1 MiB
_MEDIA_TTL_SEC = 600  # how long a hosted outbound file stays fetchable


def _get_scoped_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """Scope-aware credential read with the default-profile startup fallback.

    Mirrors ``plugins/platforms/sms/adapter.py``: secondary profiles construct
    under a profile secret scope (authoritative); the DEFAULT profile
    constructs unscoped where a bare ``get_secret`` would raise.
    """
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


def _token() -> str:
    return (_get_scoped_secret("ZALO_BOT_TOKEN", "") or "").strip()


def _api_base() -> str:
    return (os.getenv("ZALO_API_BASE", "") or DEFAULT_API_BASE).rstrip("/")


def _redact_token(token: str) -> str:
    if not token:
        return "<unset>"
    head = token.split(":", 1)[0]
    return f"{head}:***"


def check_zalo_requirements() -> bool:
    """Passive dependency probe — deps importable + token present."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return bool(_token())


class ZaloApiError(Exception):
    def __init__(self, message: str, error_code: Optional[int] = None):
        super().__init__(message)
        self.error_code = error_code

    @property
    def is_polling_timeout(self) -> bool:
        return self.error_code == 408


class ZaloAdapter(BasePlatformAdapter):
    """Zalo Bot API <-> Hermes gateway adapter (multi-tenant per chat_id)."""

    MAX_MESSAGE_LENGTH = MAX_ZALO_TEXT
    splits_long_messages = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("zalo"))
        self._token: str = _token()
        self._api_base: str = _api_base()
        self._webhook_url: str = (os.getenv("ZALO_WEBHOOK_URL", "") or "").strip()
        self._webhook_host: str = os.getenv("ZALO_WEBHOOK_HOST", DEFAULT_WEBHOOK_HOST)
        self._webhook_port: int = int(
            os.getenv("ZALO_WEBHOOK_PORT", str(DEFAULT_WEBHOOK_PORT)) or DEFAULT_WEBHOOK_PORT
        )
        self._webhook_secret: str = (os.getenv("ZALO_WEBHOOK_SECRET", "") or "").strip()
        self._use_webhook: bool = bool(self._webhook_url)
        self._bot_id: str = ""
        self._bot_name: str = ""
        self._poll_task: Optional[asyncio.Task] = None
        self._runner = None  # aiohttp AppRunner (webhook mode)
        self._http: Optional["aiohttp.ClientSession"] = None

        # Outbound local-file hosting. Zalo's sendPhoto only accepts a URL,
        # so to send a local image we serve it over a short-lived tokened
        # route and hand Zalo the public URL. Requires ZALO_PUBLIC_MEDIA_URL
        # (a public base that reaches ZALO_MEDIA_HOST:ZALO_MEDIA_PORT, e.g.
        # via the existing cloudflared tunnel).
        self._public_media_url: str = (os.getenv("ZALO_PUBLIC_MEDIA_URL", "") or "").strip().rstrip("/")
        self._media_host: str = os.getenv("ZALO_MEDIA_HOST", DEFAULT_MEDIA_HOST)
        self._media_port: int = int(
            os.getenv("ZALO_MEDIA_PORT", str(DEFAULT_MEDIA_PORT)) or DEFAULT_MEDIA_PORT
        )
        self._media_runner = None  # aiohttp AppRunner for the media server
        self._hosted_media: Dict[str, Tuple[str, float, str]] = {}  # token -> (path, expiry, ctype)

    # ------------------------------------------------------------------
    # Zalo Bot API client
    # ------------------------------------------------------------------

    async def _call(
        self,
        method: str,
        body: Optional[dict] = None,
        *,
        timeout_sec: float = 30.0,
    ) -> dict:
        import aiohttp

        url = f"{self._api_base}/bot{self._token}/{method}"
        session = self._http
        created = None
        if session is None:
            created = aiohttp.ClientSession(trust_env=True)
            session = created
        try:
            async with session.post(
                url,
                json=body if body is not None else None,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=timeout_sec),
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    text = await resp.text()
                    raise ZaloApiError(f"{method}: non-JSON response ({resp.status}): {text[:200]}")
            if not isinstance(data, dict) or not data.get("ok"):
                desc = (data or {}).get("description") or f"Zalo API error: {method}"
                raise ZaloApiError(desc, (data or {}).get("error_code"))
            return data
        finally:
            if created is not None:
                await created.close()

    async def _get_me(self) -> dict:
        data = await self._call("getMe", timeout_sec=10.0)
        return data.get("result", {}) or {}

    # ------------------------------------------------------------------
    # Required abstract methods
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        import aiohttp

        if not self._token:
            msg = "[zalo] ZALO_BOT_TOKEN not set — cannot connect"
            logger.error(msg)
            self._set_fatal_error("zalo_missing_token", msg, retryable=False)
            return False

        self._http = aiohttp.ClientSession(trust_env=True)

        # Validate token / fetch bot identity.
        try:
            me = await self._get_me()
            self._bot_id = str(me.get("id") or me.get("bot_id") or "")
            self._bot_name = str(me.get("name") or me.get("display_name") or "")
        except ZaloApiError as e:
            msg = f"[zalo] getMe failed: {e} (code={e.error_code})"
            logger.error(msg)
            self._set_fatal_error("zalo_bad_token", msg, retryable=False)
            await self._close_http()
            return False
        except Exception as e:  # network etc. — retryable
            logger.warning("[zalo] getMe error (will retry): %s", e)
            await self._close_http()
            return False

        if self._use_webhook:
            ok = await self._start_webhook()
            if not ok:
                await self._close_http()
                return False
        else:
            # Make sure no stale webhook is registered — it would starve
            # getUpdates of events.
            try:
                info = await self._call("getWebhookInfo", timeout_sec=10.0)
                if (info.get("result") or {}).get("url"):
                    await self._call("deleteWebhook", timeout_sec=10.0)
                    logger.info("[zalo] removed stale webhook; using long-polling")
            except Exception as e:
                logger.debug("[zalo] getWebhookInfo/deleteWebhook skipped: %s", e)
            self._poll_task = asyncio.create_task(self._poll_loop())
            self._background_tasks.add(self._poll_task)
            self._poll_task.add_done_callback(self._background_tasks.discard)

        if self._public_media_url:
            try:
                await self._start_media_server()
            except Exception as e:
                logger.warning("[zalo] media server failed to start (local-file sends disabled): %s", e)
                self._public_media_url = ""

        self._running = True
        logger.info(
            "[zalo] connected bot=%s%s mode=%s media=%s",
            _redact_token(self._token),
            f" ({self._bot_name})" if self._bot_name else "",
            "webhook" if self._use_webhook else "polling",
            "hosted" if self._public_media_url else "url-only",
        )
        self._wire_plugin_handlers(None)
        return True

    async def _start_media_server(self) -> None:
        from aiohttp import web

        async def _serve(request):
            token = request.match_info.get("token", "")
            entry = self._hosted_media.get(token)
            if not entry:
                return web.Response(status=404, text="not found")
            path, expiry, ctype = entry
            if time.time() > expiry or not os.path.isfile(path):
                self._hosted_media.pop(token, None)
                return web.Response(status=404, text="expired")
            return web.FileResponse(path, headers={"Content-Type": ctype})

        app = web.Application()
        app.router.add_get("/z/{token}", _serve)
        self._media_runner = web.AppRunner(app)
        await self._media_runner.setup()
        site = web.TCPSite(self._media_runner, self._media_host, self._media_port)
        await site.start()
        logger.info(
            "[zalo] media server on %s:%d -> %s",
            self._media_host, self._media_port, self._public_media_url,
        )

    async def disconnect(self) -> None:
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
        self._poll_task = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None
            # Best-effort: drop the webhook so a later polling start works.
            try:
                await self._call("deleteWebhook", timeout_sec=10.0)
            except Exception:
                pass
        if self._media_runner is not None:
            try:
                await self._media_runner.cleanup()
            except Exception:
                pass
            self._media_runner = None
        self._hosted_media.clear()
        await self._close_http()
        logger.info("[zalo] disconnected")

    async def _close_http(self) -> None:
        if self._http is not None:
            try:
                await self._http.close()
            except Exception:
                pass
            self._http = None

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, max_length=MAX_ZALO_TEXT)
        last = SendResult(success=True)
        for chunk in chunks:
            try:
                data = await self._call(
                    "sendMessage", {"chat_id": str(chat_id), "text": chunk}
                )
                msg_id = str((data.get("result") or {}).get("message_id") or "")
                last = SendResult(success=True, message_id=msg_id)
            except ZaloApiError as e:
                logger.error("[zalo] sendMessage failed to %s: %s", chat_id, e)
                return SendResult(success=False, error=str(e))
            except Exception as e:
                logger.error("[zalo] sendMessage error to %s: %s", chat_id, e)
                return SendResult(success=False, error=str(e), retryable=True)
        return last

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        try:
            await self._call(
                "sendChatAction",
                {"chat_id": str(chat_id), "action": "typing"},
                timeout_sec=8.0,
            )
        except Exception as e:
            logger.debug("[zalo] sendChatAction failed for %s: %s", chat_id, e)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        url = (image_url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            # Zalo needs an absolute URL; fall back to text.
            return await self.send(chat_id, f"{caption}\n{url}" if caption else url)
        body = {"chat_id": str(chat_id), "photo": url}
        if caption:
            body["caption"] = self.format_message(caption)[:MAX_ZALO_TEXT]
        try:
            data = await self._call("sendPhoto", body)
            msg_id = str((data.get("result") or {}).get("message_id") or "")
            return SendResult(success=True, message_id=msg_id)
        except ZaloApiError as e:
            logger.error("[zalo] sendPhoto failed to %s: %s", chat_id, e)
            return SendResult(success=False, error=str(e))
        except Exception as e:
            logger.error("[zalo] sendPhoto error to %s: %s", chat_id, e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a LOCAL image file. Zalo's sendPhoto is URL-only, so the file
        is served over a short-lived tokened route and Zalo is handed the
        public URL. Needs ZALO_PUBLIC_MEDIA_URL; otherwise falls back to the
        base notice (never leaks the host path into chat)."""
        p = str(image_path or "").strip()
        if p.lower().startswith(("http://", "https://")):
            return await self.send_image(chat_id, p, caption, reply_to, metadata)
        if not self._public_media_url or self._media_runner is None:
            logger.warning(
                "[zalo] cannot send local image %s — set ZALO_PUBLIC_MEDIA_URL "
                "(Zalo sendPhoto only accepts a public URL)",
                os.path.basename(p),
            )
            return await super().send_image_file(
                chat_id, image_path, caption, reply_to, metadata, **kwargs
            )
        if not os.path.isfile(p):
            return SendResult(success=False, error=f"file not found: {os.path.basename(p)}")
        try:
            public_url = self._host_local_file(p)
        except Exception as e:
            logger.error("[zalo] host local file failed: %s", e)
            return SendResult(success=False, error=f"host media: {e}")
        return await self.send_image(chat_id, public_url, caption, reply_to, metadata)

    def _host_local_file(self, path: str) -> str:
        """Register a local file for short-lived public serving; return its URL."""
        now = time.time()
        # opportunistic cleanup
        for tok in [k for k, (_, exp, _) in self._hosted_media.items() if exp < now]:
            self._hosted_media.pop(tok, None)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        token = secrets.token_urlsafe(18)
        self._hosted_media[token] = (path, now + _MEDIA_TTL_SEC, ctype)
        return f"{self._public_media_url}/z/{token}"

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": str(chat_id), "type": "dm", "chat_id": str(chat_id)}

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def format_message(self, content: str) -> str:
        return _strip_markdown(content or "")

    # ------------------------------------------------------------------
    # Inbound: long-polling
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        backoff = 1.0
        while self._running or self._poll_task is None:
            if not self._running and self._poll_task is not None:
                # connect() sets _running right after spawning us; tolerate the race
                await asyncio.sleep(0.05)
                if not self._running:
                    return
            try:
                data = await self._call(
                    "getUpdates",
                    {"timeout": str(_POLL_TIMEOUT_SEC)},
                    timeout_sec=_POLL_TIMEOUT_SEC + 10,
                )
                backoff = 1.0
                update = data.get("result")
                if update:
                    await self._process_update(update)
            except asyncio.CancelledError:
                raise
            except ZaloApiError as e:
                if e.is_polling_timeout:
                    continue  # normal: no messages in the window
                logger.warning("[zalo] polling error: %s", e)
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)
            except Exception as e:
                logger.warning("[zalo] polling exception: %s", e)
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)

    # ------------------------------------------------------------------
    # Inbound: webhook
    # ------------------------------------------------------------------

    async def _start_webhook(self) -> bool:
        from aiohttp import web

        if not self._webhook_secret:
            self._webhook_secret = secrets.token_urlsafe(24)
            logger.info("[zalo] generated webhook secret (set ZALO_WEBHOOK_SECRET to pin it)")

        app = web.Application(client_max_size=_WEBHOOK_MAX_BODY_BYTES)
        app.router.add_post("/webhooks/zalo", self._handle_webhook)
        app.router.add_get("/health", lambda _r: web.Response(text="ok"))
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._webhook_host, self._webhook_port)
        await site.start()

        try:
            await self._call(
                "setWebhook",
                {"url": self._webhook_url, "secret_token": self._webhook_secret},
                timeout_sec=15.0,
            )
        except ZaloApiError as e:
            msg = f"[zalo] setWebhook failed: {e}"
            logger.error(msg)
            self._set_fatal_error("zalo_setwebhook_failed", msg, retryable=False)
            return False
        logger.info(
            "[zalo] webhook server on %s:%d -> %s",
            self._webhook_host, self._webhook_port, self._webhook_url,
        )
        return True

    async def _handle_webhook(self, request) -> "aiohttp.web.Response":
        from aiohttp import web

        header_secret = request.headers.get("X-Bot-Api-Secret-Token", "")
        if not secrets.compare_digest(header_secret, self._webhook_secret):
            logger.warning("[zalo] webhook rejected: bad secret token")
            return web.Response(status=403, text="forbidden")
        try:
            raw = await request.read()
            if len(raw) > _WEBHOOK_MAX_BODY_BYTES:
                return web.Response(status=413, text="too large")
            import json as _json
            payload = _json.loads(raw.decode("utf-8"))
        except Exception as e:
            logger.warning("[zalo] webhook parse error: %s", e)
            return web.Response(status=400, text="bad request")

        update = payload
        if isinstance(payload, dict) and payload.get("ok") is True and payload.get("result"):
            update = payload["result"]
        if not isinstance(update, dict) or not update.get("event_name"):
            return web.Response(status=400, text="bad request")

        task = asyncio.create_task(self._process_update(update))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return web.Response(text="ok")

    # ------------------------------------------------------------------
    # Update -> MessageEvent
    # ------------------------------------------------------------------

    async def _download_image(self, url: str) -> Optional[Tuple[str, str]]:
        """Download an inbound Zalo image URL into the local media cache.

        Returns (local_path, content_type) or None. Uses the gateway's
        SSRF-safe image cache helper so the agent's vision tools can read it.
        """
        try:
            from gateway.platforms.base import cache_image_from_url

            lower = url.split("?", 1)[0].lower()
            ext = ".png" if lower.endswith(".png") else (
                ".webp" if lower.endswith(".webp") else (
                    ".gif" if lower.endswith(".gif") else ".jpg"
                )
            )
            path = await cache_image_from_url(url, ext=ext)
            ctype = mimetypes.guess_type(path)[0] or "image/jpeg"
            return path, ctype
        except Exception as e:
            logger.warning("[zalo] inbound image download failed: %s", e)
            return None

    async def _process_update(self, update: dict) -> None:
        try:
            event_name = str(update.get("event_name") or "")
            message = update.get("message") or {}
            if not message:
                return

            chat = message.get("chat") or {}
            sender = message.get("from") or {}
            chat_id = str(chat.get("id") or "")
            if not chat_id:
                return
            zalo_chat_type = str(chat.get("type") or "private").lower()
            chat_type = "group" if zalo_chat_type in ("group", "supergroup") else "dm"
            user_id = str(sender.get("id") or chat_id)
            user_name = str(
                sender.get("display_name") or sender.get("name") or user_id
            )
            chat_name = str(chat.get("name") or chat.get("title") or user_name)
            message_id = str(message.get("message_id") or "")

            # Ignore our own echoes.
            if self._bot_id and user_id == self._bot_id:
                return

            text = message.get("text")
            msg_type = MessageType.TEXT
            media_urls: list = []
            media_types: list = []

            if event_name == "message.image.received" or message.get("photo_url"):
                photo_url = str(
                    message.get("photo_url") or message.get("photo") or ""
                ).strip()
                caption = str(message.get("caption") or "").strip()
                if photo_url:
                    local = await self._download_image(photo_url)
                    if local:
                        media_urls.append(local[0])
                        media_types.append(local[1])
                        msg_type = MessageType.PHOTO
                text = text or caption or "[image]"
            elif not text:
                if event_name == "message.sticker.received":
                    text = "[sticker]"
                else:
                    return

            logger.info(
                "[zalo] inbound %s chat=%s user=%s: %s%s",
                event_name or "message", chat_id, user_id, str(text)[:80],
                f" (+{len(media_urls)} media)" if media_urls else "",
            )

            source = self.build_source(
                chat_id=chat_id,
                chat_name=chat_name,
                chat_type=chat_type,
                user_id=user_id,
                user_name=user_name,
                message_id=message_id or None,
            )
            event = MessageEvent(
                text=str(text),
                message_type=msg_type,
                source=source,
                user_id=user_id,
                user_name=user_name,
                raw_message=update,
                message_id=message_id or None,
                media_urls=media_urls,
                media_types=media_types,
            )
            await self.handle_message(event)
        except Exception as e:
            logger.error("[zalo] failed to process update: %s", e, exc_info=True)


# ──────────────────────────────────────────────────────────────────────────
# Plugin glue
# ──────────────────────────────────────────────────────────────────────────


def _build_adapter(config: PlatformConfig) -> ZaloAdapter:
    return ZaloAdapter(config)


def _env_enablement() -> Optional[dict]:
    """Auto-enable the platform on gateway load when ZALO_BOT_TOKEN is set."""
    if not _token():
        return None
    out: dict = {"enabled": True}
    home = (os.getenv("ZALO_HOME_CHANNEL", "") or "").strip()
    if home:
        out["home_channel"] = {"chat_id": home}
    return out


def _is_connected(config) -> bool:
    try:
        import hermes_cli.gateway as gateway_mod
        return bool((gateway_mod.get_env_value("ZALO_BOT_TOKEN") or "").strip())
    except Exception:
        return bool(_token())


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Out-of-process delivery (cron jobs / send_message tool)."""
    try:
        import aiohttp
    except ImportError:
        return {"error": "aiohttp not installed. Run: pip install aiohttp"}

    token = _token() or (getattr(pconfig, "token", "") or "")
    if not token:
        return {"error": "Zalo not configured (ZALO_BOT_TOKEN required)"}
    api_base = _api_base()
    text = _strip_markdown(message or "")[:MAX_ZALO_TEXT]

    try:
        from gateway.platforms.base import proxy_kwargs_for_aiohttp, resolve_proxy_url
        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(resolve_proxy_url())
    except Exception:
        _sess_kw, _req_kw = {}, {}

    url = f"{api_base}/bot{token}/sendMessage"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), **_sess_kw
        ) as session:
            async with session.post(
                url, json={"chat_id": str(chat_id), "text": text}, **_req_kw
            ) as resp:
                data = await resp.json(content_type=None)
                if not data.get("ok"):
                    return {"error": f"Zalo API error: {data.get('description', data)}"}
                return {
                    "success": True,
                    "platform": "zalo",
                    "chat_id": str(chat_id),
                    "message_id": str((data.get("result") or {}).get("message_id") or ""),
                }
    except Exception as e:
        return {"error": f"Zalo send failed: {e}"}


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="zalo",
        label="Zalo",
        adapter_factory=_build_adapter,
        check_fn=check_zalo_requirements,
        is_connected=_is_connected,
        required_env=["ZALO_BOT_TOKEN"],
        install_hint="pip install aiohttp",
        env_enablement_fn=_env_enablement,
        allowed_users_env="ZALO_ALLOWED_USERS",
        allow_all_env="ZALO_ALLOW_ALL_USERS",
        cron_deliver_env_var="ZALO_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=MAX_ZALO_TEXT,
        emoji="💬",
        allow_update_command=True,
        platform_hint=(
            "You are on Zalo (a Vietnamese messaging app) via the Zalo Bot API. "
            "Plain text only — Zalo does not render Markdown, so never use "
            "**bold**, headings, tables, or backtick code fences; write natural "
            "prose and simple lists. Messages are capped at ~2000 characters and "
            "are split automatically if longer. Images can be sent as public "
            "HTTPS URLs."
        ),
    )
