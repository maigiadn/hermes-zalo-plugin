#!/usr/bin/env node
// Hermes <-> personal Zalo bridge (zca-js, unofficial Zalo Web API).
//
//   node bridge.mjs login [--qr <png>]   QR login; saves credentials, then exits
//   node bridge.mjs whoami               verify saved credentials, print own id/name
//   node bridge.mjs serve                long-running bridge for the Python adapter
//
// serve protocol (JSON lines):
//   stdin  <- {"id":N,"op":"send"|"send_files"|"typing"|"user_info"|"group_info"|"ping", ...}
//   stdout -> {"id":N,"ok":true,...} | {"id":N,"ok":false,"error":"..."}
//   stdout -> {"event":"ready"|"message"|"status"|"fatal", ...}
// stdout is reserved for the protocol: every log line (ours and zca-js's) goes to stderr.
//
// Exit codes: 0 normal, 2 no credentials, 3 login rejected (re-login needed),
// 4 listener kicked / duplicate connection (Zalo Web opened elsewhere), 5 listener closed.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";
import util from "node:util";
import { Zalo, ThreadType, LoginQRCallbackEventType, CloseReason } from "zca-js";

console.log = console.info = console.debug = (...a) => process.stderr.write(util.format(...a) + "\n");
const log = (...a) => process.stderr.write(`[zalouser-bridge] ${util.format(...a)}\n`);

const HERMES_HOME = process.env.HERMES_HOME || path.join(os.homedir(), ".hermes");
const STATE_DIR = process.env.ZALOUSER_STATE_DIR || path.join(HERMES_HOME, "zalouser");
const CREDS_PATH = process.env.ZALOUSER_CREDENTIALS || path.join(STATE_DIR, "credentials.json");

// ── image metadata without sharp (zca-js v2 needs width/height for photos) ──

function imageSize(buf) {
  // PNG
  if (buf.length > 24 && buf.readUInt32BE(0) === 0x89504e47) {
    return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
  }
  // GIF
  if (buf.length > 10 && buf.toString("ascii", 0, 3) === "GIF") {
    return { width: buf.readUInt16LE(6), height: buf.readUInt16LE(8) };
  }
  // WEBP
  if (buf.length > 30 && buf.toString("ascii", 0, 4) === "RIFF" && buf.toString("ascii", 8, 12) === "WEBP") {
    const chunk = buf.toString("ascii", 12, 16);
    if (chunk === "VP8 ") return { width: buf.readUInt16LE(26) & 0x3fff, height: buf.readUInt16LE(28) & 0x3fff };
    if (chunk === "VP8L") {
      const b = buf.readUInt32LE(21);
      return { width: (b & 0x3fff) + 1, height: ((b >> 14) & 0x3fff) + 1 };
    }
    if (chunk === "VP8X") return { width: buf.readUIntLE(24, 3) + 1, height: buf.readUIntLE(27, 3) + 1 };
  }
  // JPEG: walk segments to the first SOFn
  if (buf.length > 4 && buf[0] === 0xff && buf[1] === 0xd8) {
    let i = 2;
    while (i + 9 < buf.length) {
      if (buf[i] !== 0xff) { i++; continue; }
      const marker = buf[i + 1];
      if (marker >= 0xc0 && marker <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(marker)) {
        return { height: buf.readUInt16BE(i + 5), width: buf.readUInt16BE(i + 7) };
      }
      i += 2 + buf.readUInt16BE(i + 2);
    }
  }
  return { width: undefined, height: undefined };
}

async function imageMetadataGetter(filePath) {
  const data = await fs.promises.readFile(filePath);
  return { ...imageSize(data), size: data.length };
}

function newZalo() {
  return new Zalo({ selfListen: false, checkUpdate: false, logging: false, imageMetadataGetter });
}

function loadCredentials() {
  if (!fs.existsSync(CREDS_PATH)) return null;
  return JSON.parse(fs.readFileSync(CREDS_PATH, "utf8"));
}

function saveCredentials(creds) {
  fs.mkdirSync(STATE_DIR, { recursive: true, mode: 0o700 });
  const tmp = `${CREDS_PATH}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(creds, null, 2), { mode: 0o600 });
  fs.renameSync(tmp, CREDS_PATH);
}

async function ownProfile(api) {
  const selfId = String(api.getOwnId());
  let name = "";
  try {
    const info = await api.fetchAccountInfo();
    name = info?.profile?.displayName || info?.profile?.zaloName || "";
  } catch (e) {
    log("fetchAccountInfo failed:", e?.message || e);
  }
  return { selfId, name };
}

// ── login ───────────────────────────────────────────────────────────────────

async function cmdLogin(argv) {
  const qi = argv.indexOf("--qr");
  const qrPath = qi >= 0 && argv[qi + 1] ? path.resolve(argv[qi + 1]) : path.join(STATE_DIR, "qr.png");
  fs.mkdirSync(path.dirname(qrPath), { recursive: true, mode: 0o700 });
  let expiredCount = 0;
  let gotCreds = false;

  const api = await newZalo().loginQR({ qrPath }, async (ev) => {
    switch (ev.type) {
      case LoginQRCallbackEventType.QRCodeGenerated:
        await ev.actions.saveToFile(qrPath);
        fs.chmodSync(qrPath, 0o600);
        log(`QR saved: ${qrPath}`);
        log("Open it and scan with the Zalo app of the account Hermes should use (Zalo > QR scanner).");
        break;
      case LoginQRCallbackEventType.QRCodeExpired:
        if (++expiredCount > 5) {
          log("QR expired too many times, giving up.");
          ev.actions.abort();
        } else {
          log(`QR expired, generating a new one (${expiredCount}/5)...`);
          ev.actions.retry();
        }
        break;
      case LoginQRCallbackEventType.QRCodeScanned:
        log(`Scanned by "${ev.data?.display_name}". Confirm the login on the phone.`);
        break;
      case LoginQRCallbackEventType.QRCodeDeclined:
        log("Login declined on the phone.");
        ev.actions.abort();
        break;
      case LoginQRCallbackEventType.GotLoginInfo:
        saveCredentials({ imei: ev.data.imei, cookie: ev.data.cookie, userAgent: ev.data.userAgent });
        gotCreds = true;
        log(`Credentials saved: ${CREDS_PATH}`);
        break;
    }
  });
  try { fs.unlinkSync(qrPath); } catch {}
  if (!api || !gotCreds) {
    log("Login did not complete.");
    process.exit(1);
  }
  const me = await ownProfile(api);
  log(`Logged in as "${me.name}" (uid ${me.selfId}).`);
  process.exit(0);
}

async function loginWithSaved() {
  const creds = loadCredentials();
  if (!creds) {
    log(`No credentials at ${CREDS_PATH}. Run: node bridge.mjs login`);
    process.exit(2);
  }
  try {
    return await newZalo().login(creds);
  } catch (e) {
    return { error: String(e?.message || e) };
  }
}

async function cmdWhoami() {
  const api = await loginWithSaved();
  if (api.error) {
    log("Login with saved credentials failed:", api.error);
    process.exit(3);
  }
  process.stdout.write(JSON.stringify(await ownProfile(api)) + "\n");
  process.exit(0);
}

// ── serve ───────────────────────────────────────────────────────────────────

const emit = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");

async function cmdServe() {
  const api = await loginWithSaved();
  if (api.error) {
    emit({ event: "fatal", code: "login_failed", error: api.error });
    process.exit(3);
  }
  const me = await ownProfile(api);
  const listener = api.listener;
  const recent = new Map(); // msgId -> raw message data, for quoting replies
  let ready = false;

  const threadTypeOf = (t) => (t === "group" ? ThreadType.Group : ThreadType.User);

  listener.on("connected", () => {
    if (!ready) {
      ready = true;
      emit({ event: "ready", selfId: me.selfId, name: me.name });
    } else {
      emit({ event: "status", state: "connected" });
    }
  });
  listener.on("disconnected", (code, reason) => emit({ event: "status", state: "disconnected", code, reason }));
  listener.on("closed", (code, reason) => {
    emit({ event: "status", state: "closed", code, reason });
    const kicked = code === CloseReason.DuplicateConnection || code === CloseReason.KickConnection;
    process.exit(kicked ? 4 : 5);
  });
  listener.on("error", (e) => log("listener error:", e?.message || e));

  listener.on("message", (m) => {
    const d = m.data || {};
    const c = d.content;
    const isGroup = m.type === ThreadType.Group;
    recent.set(String(d.msgId), d);
    if (recent.size > 300) recent.delete(recent.keys().next().value);
    emit({
      event: "message",
      threadType: isGroup ? "group" : "user",
      threadId: String(m.threadId),
      isSelf: !!m.isSelf,
      msgId: String(d.msgId || ""),
      cliMsgId: String(d.cliMsgId || ""),
      msgType: String(d.msgType || ""),
      uidFrom: String(d.uidFrom || ""),
      dName: d.dName || "",
      ts: String(d.ts || ""),
      text: typeof c === "string" ? c : "",
      attach: c && typeof c === "object"
        ? { title: c.title, description: c.description, href: c.href, thumb: c.thumb, params: c.params }
        : null,
      mentions: (d.mentions || []).map((x) => ({ uid: String(x.uid), pos: x.pos, len: x.len })),
      quote: d.quote
        ? {
            ownerId: String(d.quote.ownerId),
            msg: d.quote.msg || "",
            globalMsgId: String(d.quote.globalMsgId || ""),
            cliMsgId: String(d.quote.cliMsgId || ""),
            fromD: d.quote.fromD || "",
          }
        : null,
    });
  });

  const ops = {
    ping: async () => ({ selfId: me.selfId }),
    send: async (r) => {
      const content = { msg: String(r.text || "") };
      const q = r.quoteMsgId ? recent.get(String(r.quoteMsgId)) : null;
      if (q) content.quote = q;
      const res = await api.sendMessage(content, String(r.threadId), threadTypeOf(r.threadType));
      return { msgId: String(res?.message?.msgId ?? "") };
    },
    send_files: async (r) => {
      const paths = (r.paths || []).map(String);
      const res = await api.sendMessage(
        { msg: String(r.text || ""), attachments: paths },
        String(r.threadId),
        threadTypeOf(r.threadType),
      );
      const ids = [res?.message?.msgId, ...(res?.attachment || []).map((a) => a?.msgId)].filter((x) => x != null);
      return { msgId: String(ids[ids.length - 1] ?? "") };
    },
    typing: async (r) => {
      await api.sendTypingEvent(String(r.threadId), threadTypeOf(r.threadType));
      return {};
    },
    user_info: async (r) => {
      const res = await api.getUserInfo(String(r.uid));
      const p = res?.changed_profiles?.[String(r.uid)] || Object.values(res?.changed_profiles || {})[0] || {};
      return { name: p.displayName || p.zaloName || "" };
    },
    group_info: async (r) => {
      const res = await api.getGroupInfo(String(r.groupId));
      const g = res?.gridInfoMap?.[String(r.groupId)] || {};
      return { name: g.name || "" };
    },
  };

  const rl = readline.createInterface({ input: process.stdin });
  rl.on("line", async (line) => {
    let req;
    try { req = JSON.parse(line); } catch { return; }
    const fn = ops[req.op];
    if (!fn) return emit({ id: req.id, ok: false, error: `unknown op ${req.op}` });
    try {
      emit({ id: req.id, ok: true, ...(await fn(req)) });
    } catch (e) {
      emit({ id: req.id, ok: false, error: String(e?.message || e) });
    }
  });
  rl.on("close", () => {
    // Parent went away (gateway stop/restart): leave cleanly.
    try { listener.stop(); } catch {}
    process.exit(0);
  });
  for (const sig of ["SIGTERM", "SIGINT"]) {
    process.on(sig, () => {
      try { listener.stop(); } catch {}
      process.exit(0);
    });
  }

  listener.start({ retryOnClose: true });
}

// ── main ────────────────────────────────────────────────────────────────────

const [cmd, ...rest] = process.argv.slice(2);
const commands = { login: cmdLogin, whoami: cmdWhoami, serve: cmdServe };
if (!commands[cmd]) {
  log("usage: node bridge.mjs login [--qr <png>] | whoami | serve");
  process.exit(64);
}
commands[cmd](rest).catch((e) => {
  log("fatal:", e?.stack || e);
  if (cmd === "serve") emit({ event: "fatal", code: "bridge_crash", error: String(e?.message || e) });
  process.exit(1);
});
