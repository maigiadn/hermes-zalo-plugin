"""Personal Zalo platform adapter for Hermes Agent (via zca-js).

Logs Hermes into a regular (personal) Zalo account through the unofficial
Zalo Web API implemented by the ``zca-js`` npm package. A small Node process
(``bridge/bridge.mjs serve``) owns the Zalo session and talks to this adapter
over stdin/stdout JSON lines:

    adapter -> bridge   {"id":N,"op":"send","threadId":..,"threadType":"user|group","text":..}
    bridge  -> adapter  {"id":N,"ok":true,"msgId":..}
    bridge  -> adapter  {"event":"ready"|"message"|"status"|"fatal", ...}

Login is a one-off QR scan done outside the gateway:

    cd ~/.hermes/plugins/zalouser/bridge && node bridge.mjs login

which stores cookies/imei/user-agent at ``$HERMES_HOME/zalouser/credentials.json``.

WARNING: zca-js is unofficial; Zalo may lock or restrict an account that is
automated this way. Use a secondary account.

Access policy (a personal account receives messages from everyone):
  * DMs are forwarded ONLY from ZALOUSER_ALLOWED_USERS (or everyone when
    ZALOUSER_ALLOW_ALL_USERS=true). Unknown senders are dropped silently —
    no pairing codes are ever sent to friends/strangers. Their uid is logged
    once so the owner can find IDs to allowlist.
  * Groups are ignored unless listed in ZALOUSER_ALLOWED_GROUPS, and then
    only messages that @mention the account (or reply to it) are forwarded
    (ZALOUSER_GROUP_REQUIRE_MENTION=false to drop that requirement).

Chat IDs: a DM chat id is the peer's Zalo uid; a group chat id is
``g:<groupId>``.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import ipaddress
import json
import logging
import mimetypes
import os
import re
import shutil
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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


logger = logging.getLogger(__name__)

PLATFORM_NAME = "zalouser"
BRIDGE_DIR = Path(__file__).resolve().parent / "bridge"
BRIDGE_SCRIPT = BRIDGE_DIR / "bridge.mjs"
MAX_ZALO_TEXT = 2000
GROUP_PREFIX = "g:"
_READY_TIMEOUT_SEC = 60
_REQUEST_TIMEOUT_SEC = 60
_CHUNK_DELAY_SEC = 0.8  # pause between split chunks — looks less bot-like
_UNKNOWN_LOG_INTERVAL_SEC = 3600
_STDOUT_LIMIT = 8 * 1024 * 1024
_ATTACH_MAX_BYTES = 30 * 1024 * 1024  # per-attachment download cap (group log)
_ATTACH_TIMEOUT_SEC = 60
_ATTACH_TYPES = ("chat.photo", "share.file", "chat.voice", "chat.video")

# Bridge exit codes (see bridge.mjs).
_EXIT_NO_CREDS = 2
_EXIT_LOGIN_REJECTED = 3
_EXIT_KICKED = 4


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")


def _creds_path() -> Path:
    override = (os.getenv("ZALOUSER_CREDENTIALS", "") or "").strip()
    return Path(override) if override else _hermes_home() / "zalouser" / "credentials.json"


def _bridge_env() -> Dict[str, str]:
    try:
        from hermes_constants import with_hermes_node_path
        env = with_hermes_node_path()
    except Exception:
        env = dict(os.environ)
    env["HERMES_HOME"] = str(_hermes_home())
    env["ZALOUSER_CREDENTIALS"] = str(_creds_path())
    return env


def _node_bin() -> Optional[str]:
    override = (os.getenv("ZALOUSER_NODE_BIN", "") or "").strip()
    if override:
        return override if os.path.isfile(override) else None
    return shutil.which("node", path=_bridge_env().get("PATH"))


def _csv_set(name: str) -> Set[str]:
    raw = os.getenv(name, "") or ""
    return {x.strip() for x in raw.split(",") if x.strip()}


def _env_true(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _split_chat_id(chat_id: str) -> Tuple[str, str]:
    """``g:<id>`` -> (id, "group"); anything else -> (uid, "user")."""
    cid = str(chat_id or "").strip()
    if cid.startswith(GROUP_PREFIX):
        return cid[len(GROUP_PREFIX):], "group"
    return cid, "user"


def check_zalouser_requirements() -> bool:
    """Passive probe — node + bridge deps installed and a QR login saved."""
    return (
        bool(_node_bin())
        and (BRIDGE_DIR / "node_modules" / "zca-js").is_dir()
        and _creds_path().is_file()
    )


class BridgeError(Exception):
    pass


class ZaloUserAdapter(BasePlatformAdapter):
    """Personal Zalo (zca-js bridge) <-> Hermes gateway adapter."""

    MAX_MESSAGE_LENGTH = MAX_ZALO_TEXT
    splits_long_messages = True
    # Gate at intake (see module docstring); tells the gateway to ignore — not
    # pair — unknown DMs, since a personal account must never answer strangers.
    enforces_own_access_policy = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(PLATFORM_NAME))
        self._allowed_users: Set[str] = _csv_set("ZALOUSER_ALLOWED_USERS")
        self._allow_all: bool = _env_true("ZALOUSER_ALLOW_ALL_USERS")
        self._allowed_groups: Set[str] = {
            g[len(GROUP_PREFIX):] if g.startswith(GROUP_PREFIX) else g
            for g in _csv_set("ZALOUSER_ALLOWED_GROUPS")
        }
        self._group_require_mention: bool = _env_true("ZALOUSER_GROUP_REQUIRE_MENTION", True)
        self._dm_policy = "open" if self._allow_all else "allowlist"
        self._group_policy = "allowlist" if self._allowed_groups else "disabled"
        # Passive group log: every message in an allowed group (not only @mentions) is appended to
        # <dir>/<groupId>/YYYY-MM-DD.log and its attachments saved under <dir>/<groupId>/files/,
        # without triggering the agent. Opt-in: other members' messages are stored on this machine.
        self._group_log: bool = _env_true("ZALOUSER_GROUP_LOG", False)
        _gdir = (os.getenv("ZALOUSER_GROUP_LOG_DIR", "") or "").strip()
        self._group_log_dir: Path = Path(_gdir) if _gdir else _hermes_home() / "zalouser" / "groups"
        try:
            self._group_log_days: int = max(1, int(os.getenv("ZALOUSER_GROUP_LOG_RETENTION_DAYS", "30") or 30))
        except ValueError:
            self._group_log_days = 30
        self._last_prune: float = 0.0

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._ready: Optional[asyncio.Future] = None
        self._shutting_down = False
        self._self_id = ""
        self._self_name = ""
        self._group_names: Dict[str, str] = {}
        self._unknown_logged: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Access policy
    # ------------------------------------------------------------------

    def _is_dm_allowed(self, user_id: str) -> bool:
        return self._allow_all or str(user_id) in self._allowed_users

    # ------------------------------------------------------------------
    # Bridge process
    # ------------------------------------------------------------------

    async def _request(self, op: str, timeout: float = _REQUEST_TIMEOUT_SEC, **fields) -> dict:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise BridgeError("zalouser bridge is not running")
        self._next_id += 1
        req_id = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            proc.stdin.write((json.dumps({"id": req_id, "op": op, **fields}) + "\n").encode())
            await proc.stdin.drain()
            res = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(req_id, None)
        if not res.get("ok"):
            raise BridgeError(str(res.get("error") or f"{op} failed"))
        return res

    async def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as e:
                logger.warning("[zalouser] oversized bridge line dropped: %s", e)
                continue
            if not line:
                break
            try:
                msg = json.loads(line)
            except Exception:
                logger.debug("[zalouser] non-JSON bridge output: %r", line[:200])
                continue
            if "id" in msg:
                fut = self._pending.get(msg["id"])
                if fut is not None and not fut.done():
                    fut.set_result(msg)
                continue
            self._dispatch_event(msg)
        rc = await proc.wait()
        await self._on_bridge_exit(rc)

    async def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                logger.info("[zalouser] bridge: %s", text[:500])

    def _dispatch_event(self, msg: dict) -> None:
        event = msg.get("event")
        if event == "ready":
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(msg)
        elif event == "message":
            task = asyncio.create_task(self._process_message(msg))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        elif event == "status":
            logger.info(
                "[zalouser] listener %s (code=%s %s)",
                msg.get("state"), msg.get("code", ""), msg.get("reason", ""),
            )
        elif event == "fatal":
            logger.error("[zalouser] bridge fatal: %s %s", msg.get("code"), msg.get("error"))

    async def _on_bridge_exit(self, rc: int) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(BridgeError(f"bridge exited (code {rc})"))
        if self._ready is not None and not self._ready.done():
            self._ready.set_result({"event": "exit", "rc": rc})
        if self._shutting_down or not self._running:
            return
        if rc == _EXIT_LOGIN_REJECTED:
            code, retryable = "zalouser_session_expired", False
            message = (
                "Zalo rejected the saved session — log in again: "
                "cd ~/.hermes/plugins/zalouser/bridge && node bridge.mjs login"
            )
        elif rc == _EXIT_KICKED:
            code, retryable = "zalouser_kicked", True
            message = (
                "Zalo closed the listener (the account was opened on Zalo Web/PC "
                "elsewhere); will reconnect"
            )
        else:
            code, retryable = "zalouser_bridge_exited", True
            message = f"zalouser bridge exited unexpectedly (code {rc})"
        logger.error("[zalouser] %s", message)
        self._set_fatal_error(code, message, retryable=retryable)
        await self._notify_fatal_error()

    async def _stop_bridge(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()  # bridge exits cleanly on stdin EOF
                await asyncio.wait_for(proc.wait(), 5)
            except Exception:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), 5)
                except Exception:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._reader_task = self._stderr_task = None
        self._proc = None

    # ------------------------------------------------------------------
    # Required abstract methods
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        node = _node_bin()
        if not node or not (BRIDGE_DIR / "node_modules" / "zca-js").is_dir():
            msg = (
                "[zalouser] node or bridge deps missing — run: "
                f"cd {BRIDGE_DIR} && npm install"
            )
            logger.error(msg)
            self._set_fatal_error("zalouser_missing_deps", msg, retryable=False)
            return False
        if not _creds_path().is_file():
            msg = (
                f"[zalouser] not logged in ({_creds_path()} missing) — run: "
                f"cd {BRIDGE_DIR} && node bridge.mjs login"
            )
            logger.error(msg)
            self._set_fatal_error("zalouser_not_logged_in", msg, retryable=False)
            return False

        self._shutting_down = False
        self._ready = asyncio.get_running_loop().create_future()
        self._proc = await asyncio.create_subprocess_exec(
            node, str(BRIDGE_SCRIPT), "serve",
            cwd=str(BRIDGE_DIR),
            env=_bridge_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STDOUT_LIMIT,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

        try:
            ready = await asyncio.wait_for(asyncio.shield(self._ready), _READY_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            logger.warning("[zalouser] bridge not ready after %ss (will retry)", _READY_TIMEOUT_SEC)
            self._shutting_down = True
            await self._stop_bridge()
            return False

        if ready.get("event") != "ready":
            rc = ready.get("rc")
            self._shutting_down = True
            await self._stop_bridge()
            if rc in (_EXIT_LOGIN_REJECTED, _EXIT_NO_CREDS):
                msg = (
                    "[zalouser] Zalo rejected the saved session — log in again: "
                    f"cd {BRIDGE_DIR} && node bridge.mjs login"
                )
                logger.error(msg)
                self._set_fatal_error("zalouser_session_expired", msg, retryable=False)
            else:
                logger.warning("[zalouser] bridge exited during startup (code %s, will retry)", rc)
            return False

        self._self_id = str(ready.get("selfId") or "")
        self._self_name = str(ready.get("name") or "")
        self._mark_connected()
        if self._group_log and not getattr(self, "_prune_task", None):
            self._prune_task = asyncio.create_task(self._prune_loop())
        logger.info(
            "[zalouser] connected as %s (uid %s) dm=%s groups=%s",
            self._self_name or "?", self._self_id or "?",
            "all" if self._allow_all else f"{len(self._allowed_users)} allowed",
            f"{len(self._allowed_groups)} allowed" if self._allowed_groups else "off",
        )
        self._wire_plugin_handlers(None)
        return True

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.to_thread(self._prune_group_log, True)
            await asyncio.sleep(6 * 3600)

    async def disconnect(self) -> None:
        self._shutting_down = True
        self._running = False
        task = getattr(self, "_prune_task", None)
        if task:
            task.cancel()
            self._prune_task = None
        await self._stop_bridge()
        logger.info("[zalouser] disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        thread_id, thread_type = _split_chat_id(chat_id)
        chunks = self.truncate_message(self.format_message(content), max_length=MAX_ZALO_TEXT)
        # Quote only in groups — in a DM every reply quoting its prompt is noise.
        quote = reply_to if (reply_to and thread_type == "group") else None
        last = SendResult(success=True)
        for i, chunk in enumerate(chunks):
            if i:
                await asyncio.sleep(_CHUNK_DELAY_SEC)
            fields: Dict[str, Any] = {"threadId": thread_id, "threadType": thread_type, "text": chunk}
            if quote and i == 0:
                fields["quoteMsgId"] = str(quote)
            try:
                res = await self._request("send", **fields)
                last = SendResult(success=True, message_id=str(res.get("msgId") or "") or None)
            except BridgeError as e:
                logger.error("[zalouser] send to %s failed: %s", chat_id, e)
                return SendResult(success=False, error=str(e), retryable=not self._running)
            except asyncio.TimeoutError:
                logger.error("[zalouser] send to %s timed out", chat_id)
                return SendResult(success=False, error="send timed out", retryable=True)
        return last

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        thread_id, thread_type = _split_chat_id(chat_id)
        try:
            await self._request("typing", timeout=8, threadId=thread_id, threadType=thread_type)
        except Exception as e:
            logger.debug("[zalouser] typing failed for %s: %s", chat_id, e)

    async def _send_files(
        self, chat_id: str, paths: List[str], caption: Optional[str] = None
    ) -> SendResult:
        thread_id, thread_type = _split_chat_id(chat_id)
        missing = [p for p in paths if not os.path.isfile(p)]
        if missing:
            return SendResult(success=False, error=f"file not found: {os.path.basename(missing[0])}")
        text = self.format_message(caption or "")[:MAX_ZALO_TEXT]
        try:
            res = await self._request(
                "send_files", timeout=180,
                threadId=thread_id, threadType=thread_type, paths=paths, text=text,
            )
            return SendResult(success=True, message_id=str(res.get("msgId") or "") or None)
        except BridgeError as e:
            logger.error("[zalouser] send_files to %s failed: %s", chat_id, e)
            return SendResult(success=False, error=str(e), retryable=not self._running)
        except asyncio.TimeoutError:
            return SendResult(success=False, error="upload timed out", retryable=True)

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
            return await self.send_image_file(chat_id, url, caption, reply_to, metadata)
        local = await self._download_image(url)
        if not local:
            return await super().send_image(chat_id, url, caption, reply_to, metadata)
        return await self._send_files(chat_id, [local[0]], caption)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_files(chat_id, [str(image_path)], caption)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_files(chat_id, [str(file_path)], caption)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_files(chat_id, [str(video_path)], caption)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        thread_id, thread_type = _split_chat_id(chat_id)
        if thread_type == "group":
            name = await self._group_name(thread_id)
            return {"name": name or str(chat_id), "type": "group", "chat_id": str(chat_id)}
        return {"name": str(chat_id), "type": "dm", "chat_id": str(chat_id)}

    def format_message(self, content: str) -> str:
        return _strip_markdown(content or "")

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def _group_name(self, group_id: str) -> str:
        if group_id in self._group_names:
            return self._group_names[group_id]
        name = ""
        try:
            name = str((await self._request("group_info", timeout=15, groupId=group_id)).get("name") or "")
        except Exception as e:
            logger.debug("[zalouser] group_info %s failed: %s", group_id, e)
        self._group_names[group_id] = name
        return name

    async def _download_image(self, url: str) -> Optional[Tuple[str, str]]:
        try:
            from gateway.platforms.base import cache_image_from_url

            lower = url.split("?", 1)[0].lower()
            ext = next((e for e in (".png", ".webp", ".gif") if lower.endswith(e)), ".jpg")
            path = await cache_image_from_url(url, ext=ext)
            return path, mimetypes.guess_type(path)[0] or "image/jpeg"
        except Exception as e:
            logger.warning("[zalouser] image download failed: %s", e)
            return None

    def _log_unknown(self, kind: str, ident: str, name: str) -> None:
        now = time.time()
        if now - self._unknown_logged.get(ident, 0) < _UNKNOWN_LOG_INTERVAL_SEC:
            return
        self._unknown_logged[ident] = now
        hint = "ZALOUSER_ALLOWED_GROUPS" if kind == "group" else "ZALOUSER_ALLOWED_USERS"
        logger.info("[zalouser] ignored %s from %s (%s) — add to %s to allow", kind, ident, name, hint)

    # ------------------------------------------------------------------
    # Passive group log (opt-in via ZALOUSER_GROUP_LOG)
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_filename(name: str) -> str:
        name = re.sub(r"[^\w.\-]+", "_", os.path.basename(str(name or "")), flags=re.UNICODE).strip("._")
        return name[:80] or "file"

    @staticmethod
    def _fetch_public_https(url: str, dest: Path) -> int:
        """Blocking download (run in a thread): https only, public IPs only, size-capped."""
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("only https attachment URLs are fetched")
        for info in socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM):
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                raise ValueError(f"refusing non-public address {ip}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        total = 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            with urllib.request.urlopen(req, timeout=_ATTACH_TIMEOUT_SEC) as resp, open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _ATTACH_MAX_BYTES:
                        raise ValueError("attachment exceeds size cap")
                    fh.write(chunk)
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                tmp.unlink()
        return total

    async def _save_attachment(self, ev: dict, group_id: str) -> Optional[Path]:
        """Download a photo/file/voice/video of a group message into the group log dir."""
        msg_type = str(ev.get("msgType") or "")
        attach = ev.get("attach") or {}
        if not msg_type.startswith(_ATTACH_TYPES):
            return None
        url = str(attach.get("href") or "")
        if not url:
            return None
        ext = Path(urllib.parse.urlsplit(url).path).suffix
        title = str(attach.get("title") or "")
        if msg_type == "share.file" and title:
            name = self._safe_filename(title)
        else:
            name = self._safe_filename(title or "attachment") + (ext if not title.endswith(ext or "\0") else "")
        if msg_type == "chat.photo" and not Path(name).suffix:
            name += ".jpg"
        dest = self._group_log_dir / group_id / "files" / f"{ev.get('msgId') or int(time.time())}_{name}"
        try:
            await asyncio.to_thread(self._fetch_public_https, url, dest)
            return dest
        except Exception as e:
            logger.warning("[zalouser] attachment download failed (%s %s): %s", msg_type, name, e)
            return None

    def _prune_group_log(self, force: bool = False) -> None:
        """Delete day logs and attachments older than the retention window (at most every 6h)."""
        now = time.time()
        if not force and now - self._last_prune < 6 * 3600:
            return
        self._last_prune = now
        cutoff = now - self._group_log_days * 86400
        removed = 0
        try:
            if not self._group_log_dir.is_dir():
                return
            for gdir in self._group_log_dir.iterdir():
                if not gdir.is_dir():
                    continue
                for f in [*gdir.glob("*.log"), *(gdir / "files").glob("*")]:
                    try:
                        if f.is_file() and f.stat().st_mtime < cutoff:
                            f.unlink()
                            removed += 1
                    except OSError as e:
                        logger.warning("[zalouser] group log prune failed for %s: %s", f, e)
            if removed:
                logger.info("[zalouser] group log prune: removed %d item(s) older than %d days", removed, self._group_log_days)
        except Exception as e:
            logger.warning("[zalouser] group log prune error: %s", e)

    def _append_group_log(self, ev: dict, group_id: str, user_id: str, user_name: str,
                          text: str, saved: Optional[Path], triggered: bool) -> None:
        self._prune_group_log()
        try:
            try:
                when = _dt.datetime.fromtimestamp(int(ev.get("ts") or 0) / 1000)
            except (ValueError, OverflowError, OSError):
                when = _dt.datetime.now()
            if when.year < 2000:
                when = _dt.datetime.now()
            gdir = self._group_log_dir / group_id
            gdir.mkdir(parents=True, exist_ok=True)
            name_file = gdir / "_group_name.txt"
            if not name_file.exists():
                name_file.write_text(self._group_names.get(group_id, "") + "\n", encoding="utf-8")
            quote = (ev.get("quote") or {}).get("msg") or ""
            parts = [f"[{when:%Y-%m-%d %H:%M:%S}] {user_name} ({user_id}){' *to-cindy*' if triggered else ''}: {text}"]
            if quote:
                parts.append(f"  (trả lời tin: {str(quote)[:200]})")
            if saved:
                parts.append(f"  [đính kèm đã lưu: {saved}]")
            with open(gdir / f"{when:%Y-%m-%d}.log", "a", encoding="utf-8") as fh:
                fh.write("\n".join(parts).replace("\r", "") + "\n")
        except Exception as e:
            logger.warning("[zalouser] group log write failed: %s", e)

    def _strip_leading_self_mention(self, text: str, mentions: Any) -> str:
        """"@Bot /new" -> "/new": drop a leading self-mention when a slash command follows,
        so commands typed in a group (where the account must be @mentioned) are recognised."""
        for m in mentions or []:
            try:
                if str(m.get("uid")) != self._self_id or int(m.get("pos") or 0) != 0:
                    continue
                rest = text[int(m.get("len") or 0):].lstrip()
            except (TypeError, ValueError, AttributeError):
                continue
            if rest.startswith("/"):
                return rest
        return text

    async def _process_message(self, ev: dict) -> None:
        try:
            if ev.get("isSelf"):
                return
            user_id = str(ev.get("uidFrom") or "")
            if not user_id or (self._self_id and user_id == self._self_id):
                return
            user_name = str(ev.get("dName") or user_id)
            thread_id = str(ev.get("threadId") or "")
            is_group = ev.get("threadType") == "group"

            if is_group:
                if thread_id not in self._allowed_groups:
                    self._log_unknown("group", thread_id, user_name)
                    return
                triggered = True
                if self._group_require_mention:
                    mentioned = any(m.get("uid") == self._self_id for m in ev.get("mentions") or [])
                    replied = (ev.get("quote") or {}).get("ownerId") == self._self_id
                    triggered = mentioned or replied
                if not triggered and not self._group_log:
                    return
            elif not self._is_dm_allowed(user_id):
                self._log_unknown("DM", user_id, user_name)
                return

            msg_type = str(ev.get("msgType") or "")
            attach = ev.get("attach") or {}
            text = str(ev.get("text") or "")
            kind = MessageType.TEXT
            media_urls: List[str] = []
            media_types: List[str] = []

            saved: Optional[Path] = None
            if is_group and self._group_log:
                saved = await self._save_attachment(ev, thread_id)
                logged = text or str(attach.get("title") or "") or (msg_type if msg_type.startswith(_ATTACH_TYPES) else "")
                if logged or saved:
                    await self._group_name(thread_id)
                    self._append_group_log(ev, thread_id, user_id, user_name, logged, saved, triggered)
                if not triggered:
                    return

            text = self._strip_leading_self_mention(text, ev.get("mentions"))

            if msg_type == "chat.photo" and saved:
                media_urls.append(str(saved))
                media_types.append(mimetypes.guess_type(saved.name)[0] or "image/jpeg")
                kind = MessageType.PHOTO
                text = text or str(attach.get("title") or "").strip() or f"[image saved at {saved}]"
            elif saved and (msg_type in ("chat.voice", "share.file") or msg_type.startswith("chat.video")):
                label = "voice message" if msg_type == "chat.voice" else (
                    "video" if msg_type.startswith("chat.video") else f"file {attach.get('title') or saved.name}")
                text = f"[{label} saved at {saved}]" + (f" {text}" if text else "")
            elif msg_type == "chat.photo":
                url = str(attach.get("href") or attach.get("thumb") or "")
                local = await self._download_image(url) if url else None
                if local:
                    media_urls.append(local[0])
                    media_types.append(local[1])
                    kind = MessageType.PHOTO
                text = text or str(attach.get("title") or "").strip() or "[image]"
            elif msg_type == "chat.sticker":
                text, kind = "[sticker]", MessageType.STICKER
            elif msg_type == "chat.voice":
                text = "[voice message — not supported yet]"
            elif msg_type.startswith("chat.video"):
                text = "[video — not supported yet]"
            elif msg_type == "share.file":
                text = f"[file: {attach.get('title') or 'unnamed'} — not supported yet]"
            elif not text and attach.get("href"):
                text = " ".join(x for x in (str(attach.get("title") or ""), str(attach.get("href"))) if x)
            if not text:
                return

            chat_id = f"{GROUP_PREFIX}{thread_id}" if is_group else user_id
            chat_name = (await self._group_name(thread_id) or chat_id) if is_group else user_name
            message_id = str(ev.get("msgId") or "") or None

            logger.info(
                "[zalouser] inbound %s chat=%s user=%s: %s%s",
                msg_type or "message", chat_id, user_id, text[:80],
                f" (+{len(media_urls)} media)" if media_urls else "",
            )
            source = self.build_source(
                chat_id=chat_id,
                chat_name=chat_name,
                chat_type="group" if is_group else "dm",
                user_id=user_id,
                user_name=user_name,
                message_id=message_id,
            )
            await self.handle_message(MessageEvent(
                text=text,
                message_type=kind,
                source=source,
                user_id=user_id,
                user_name=user_name,
                raw_message=ev,
                message_id=message_id,
                media_urls=media_urls,
                media_types=media_types,
            ))
        except Exception as e:
            logger.error("[zalouser] failed to process message: %s", e, exc_info=True)


# ──────────────────────────────────────────────────────────────────────────
# Plugin glue
# ──────────────────────────────────────────────────────────────────────────


def _build_adapter(config: PlatformConfig) -> ZaloUserAdapter:
    return ZaloUserAdapter(config)


def _env_enablement() -> Optional[dict]:
    """Auto-enable once a QR login has stored credentials."""
    if not _creds_path().is_file() or _env_true("ZALOUSER_DISABLED"):
        return None
    out: dict = {"enabled": True}
    home = (os.getenv("ZALOUSER_HOME_CHANNEL", "") or "").strip()
    if home:
        out["home_channel"] = {"chat_id": home}
    return out


def _is_connected(config) -> bool:
    return _creds_path().is_file()


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Zalo (personal)",
        adapter_factory=_build_adapter,
        check_fn=check_zalouser_requirements,
        is_connected=_is_connected,
        required_env=[],
        install_hint=f"cd {BRIDGE_DIR} && npm install",
        env_enablement_fn=_env_enablement,
        allowed_users_env="ZALOUSER_ALLOWED_USERS",
        allow_all_env="ZALOUSER_ALLOW_ALL_USERS",
        cron_deliver_env_var="ZALOUSER_HOME_CHANNEL",
        max_message_length=MAX_ZALO_TEXT,
        emoji="💬",
        allow_update_command=False,
        platform_hint=(
            "You are on Zalo (a Vietnamese messaging app), replying from a personal "
            "Zalo account. Plain text only — Zalo does not render Markdown, so never "
            "use **bold**, headings, tables, or backtick code fences; write natural "
            "prose and simple lists. Messages are capped at ~2000 characters and are "
            "split automatically if longer. Images and files can be sent."
        ),
    )
