#!/usr/bin/env python3
"""Muse bridge: OpenAI-compatible endpoint + worker pull API.

Part of Muse-on-OpenCode (https://github.com/FaiRefriandi/Muse-on-OpenCode).
Based on bridge.py v5.1 from Kutuyyy/Muse-on-9Router-Hermes-Agent-ai,
extended with tool calling, progress notes, reasoning, and token estimates.

How it works:
  OpenCode (laptop) -> tunnel -> 9Router (VM, :20128) -> this bridge (VM, :8765)
  This bridge queues each chat request as a JSON file and long-polls for an
  answer. A Muse worker agent in the VM pulls jobs and answers them.

Endpoints:
  POST /v1/chat/completions   OpenAI-compatible chat (key role=user)
  GET  /v1/models             lists "muse" and "muse-spark" (key role=user)
  GET  /health                open, no auth
  GET  /muse/pending?limit=5  lease up to `limit` jobs (key role=worker)
  POST /muse/answer           {"id","content"} or {"id","tool_calls":[...],
                              optional "reasoning"} (key role=worker)
  POST /muse/progress         {"id","content", optional "kind":"reasoning"}
                              interim note forwarded to streaming clients
                              (key role=worker)
  POST /muse/release          {"id"} release a lease early (key role=worker)

Keys (stored in keys.json next to the queue dir, printed only at creation):
  python bridge.py keygen --role worker --label muse-vm
  python bridge.py keygen --role user   --label 9router
  python bridge.py keylist
  python bridge.py keydel <prefix|label>

Queue layout (under BRIDGE_QUEUE):
  pending/<id>.json      unleased jobs
  processing/<id>.json   leased jobs (contain "_lease":{"by","until"})
  done/<id>.json         answered jobs
  progress/<id>.jsonl    interim progress/reasoning notes

Run:
  BRIDGE_QUEUE=/path/to/queue python bridge.py serve
"""
import argparse
import hmac
import json
import os
import secrets
import shutil
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

QUEUE = os.environ.get("BRIDGE_QUEUE", "/home/ubuntu/muse-bridge/queue").rstrip("/")
PENDING = f"{QUEUE}/pending"
DONE = f"{QUEUE}/done"
PROCESSING = f"{QUEUE}/processing"
PROGRESS = f"{QUEUE}/progress"
KEYS_FILE = os.environ.get("BRIDGE_KEYS",
                           os.path.join(os.path.dirname(QUEUE), "keys.json"))
MAX_PENDING = 10
WAIT_SECS = 600        # how long /v1/chat/completions waits for an answer
LEASE_SECS = int(os.environ.get("BRIDGE_LEASE_SECS", "600"))  # worker lease
KEEPALIVE_SECS = 15
MAX_BODY = 32 * 1024 * 1024  # 32 MB per request body
TOKEN = os.environ.get("BRIDGE_TOKEN", "").strip()  # legacy user key on /v1/*
DONE_ORPHAN_SECS = 600
PROCESSING_STALE_SECS = 900  # fallback when a job has no/invalid lease info

LOCK = threading.Lock()
RECENT_DONE = {}  # job id -> timestamp of completion (for duplicate-answer detection)
RECENT_DONE_TTL = 3600


# ---------------------------------------------------------------- keys ---
def _load_keys():
    try:
        with open(KEYS_FILE) as f:
            return json.load(f).get("keys", [])
    except Exception:
        return []


def _save_keys(keys):
    os.makedirs(os.path.dirname(KEYS_FILE) or ".", exist_ok=True)
    tmp = KEYS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"keys": keys}, f, indent=2)
    os.replace(tmp, KEYS_FILE)
    try:
        os.chmod(KEYS_FILE, 0o600)
    except Exception:
        pass


def cmd_keygen(role, label):
    if role not in ("user", "worker"):
        print("role must be 'user' or 'worker'", file=sys.stderr)
        return 2
    key = "Muse_kutkey_" + secrets.token_urlsafe(18)
    keys = _load_keys()
    keys.append({"key": key, "role": role, "label": label,
                 "created": datetime.now(timezone.utc).isoformat()})
    _save_keys(keys)
    print(key)  # full key shown ONLY here
    print(f"role={role} label={label} saved to {KEYS_FILE} "
          f"(full key is shown only this once)", file=sys.stderr)
    return 0


def cmd_keylist():
    keys = _load_keys()
    if not keys:
        print("(no keys yet — run: python bridge.py keygen --role worker --label muse-vm)")
        return 0
    print(f"{'LABEL':<20}{'ROLE':<8}{'CREATED':<33}PREFIX")
    for k in keys:
        print(f"{k.get('label',''):<20}{k.get('role',''):<8}"
              f"{k.get('created',''):<33}{k.get('key','')[:14]}...")
    return 0


def cmd_keydel(ident):
    keys = _load_keys()
    keep = [k for k in keys
            if not (k.get("key", "").startswith(ident) or k.get("label") == ident)]
    removed = len(keys) - len(keep)
    if removed:
        _save_keys(keep)
    print(f"removed {removed} key(s)")
    return 0


# --------------------------------------------------------------- queue ---
def _job_path(d, jid):
    # jid is hex from uuid4; guard against path traversal anyway
    safe = "".join(c for c in jid if c.isalnum())[:64]
    return os.path.join(d, safe + ".json")


def _claim_jobs(limit, worker_label):
    """Atomically move up to `limit` pending jobs to processing/ with a lease."""
    claimed, now = [], time.time()
    with LOCK:
        try:
            files = sorted(os.listdir(PENDING))
        except Exception:
            files = []
        for fn in files:
            if len(claimed) >= limit or not fn.endswith(".json"):
                continue
            src, dst = os.path.join(PENDING, fn), os.path.join(PROCESSING, fn)
            try:
                os.rename(src, dst)  # atomic claim within one filesystem
            except FileNotFoundError:
                continue
            try:
                with open(dst) as f:
                    job = json.load(f)
            except Exception:
                job = {"id": fn[:-5]}
            job["_lease"] = {"by": worker_label, "until": now + LEASE_SECS}
            tmp = dst + ".tmp"
            with open(tmp, "w") as f:
                json.dump(job, f)
            os.replace(tmp, dst)
            claimed.append({"id": job.get("id", fn[:-5]),
                            "received_at": job.get("received_at"),
                            "request": job.get("request", {})})
        try:
            pending_n = sum(1 for f in os.listdir(PENDING) if f.endswith(".json"))
        except Exception:
            pending_n = 0
    return claimed, pending_n


def _normalize_tool_calls(raw):
    """Validate/normalize worker-supplied tool_calls to OpenAI format.

    Returns a list (possibly empty) or None if the shape is invalid.
    Each call: {"id", "type": "function", "function": {"name", "arguments"}}
    where arguments is always a JSON string.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw[:8]:
        if not isinstance(item, dict):
            return None
        fn = item.get("function") or {}
        if not isinstance(fn, dict):
            return None
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        args = fn.get("arguments", "{}")
        if isinstance(args, (dict, list)):
            args = json.dumps(args)
        elif not isinstance(args, str):
            args = json.dumps(args)
        cid = item.get("id")
        if not isinstance(cid, str) or not cid.strip():
            cid = "call_" + uuid.uuid4().hex[:16]
        out.append({"id": cid, "type": "function",
                    "function": {"name": name.strip(), "arguments": args}})
    return out


def _answer_job(jid, content, tool_calls=None, reasoning=None):
    """Returns 'ok' | 'duplicate' | None (unknown id)."""
    with LOCK:
        p = _job_path(PROCESSING, jid)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
            first = True
        elif os.path.exists(_job_path(DONE, jid)):
            first = False  # answered but not yet picked up by the /v1 waiter
        elif jid in RECENT_DONE:
            return "duplicate"  # answered AND already delivered to the client
        else:
            return None
        dp = _job_path(DONE, jid)
        tmp = dp + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"content": content or "",
                       "tool_calls": tool_calls or None,
                       "reasoning": reasoning or None,
                       "answered_at": time.time()}, f)
        os.replace(tmp, dp)
        RECENT_DONE[jid] = time.time()
    return "ok" if first else "duplicate"


def _release_job(jid):
    """Move a leased job back to pending/. Returns True if it was leased."""
    with LOCK:
        src = _job_path(PROCESSING, jid)
        if not os.path.exists(src):
            return False
        try:
            with open(src) as f:
                job = json.load(f)
        except Exception:
            job = {"id": jid}
        job.pop("_lease", None)
        dst = _job_path(PENDING, jid)
        tmp = dst + ".tmp"
        with open(tmp, "w") as f:
            json.dump(job, f)
        os.replace(tmp, dst)
        try:
            os.remove(src)
        except Exception:
            pass
    return True


def _sweep_once():
    """Delete orphan done/ files; return expired/stale processing/ jobs to pending/."""
    now = time.time()
    try:
        for fn in os.listdir(DONE):
            p = os.path.join(DONE, fn)
            try:
                if now - os.path.getmtime(p) > DONE_ORPHAN_SECS:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass
    try:
        for fn in os.listdir(PROCESSING):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(PROCESSING, fn)
            expired = False
            try:
                with open(p) as f:
                    job = json.load(f)
                until = (job.get("_lease") or {}).get("until")
                if isinstance(until, (int, float)) and now > until:
                    expired = True
                elif now - os.path.getmtime(p) > PROCESSING_STALE_SECS:
                    expired = True  # no/invalid lease info: stale fallback
            except Exception:
                expired = True
            if expired:
                _release_job(fn[:-5])
    except Exception:
        pass
    # prune duplicate-answer memory
    try:
        cutoff = now - RECENT_DONE_TTL
        for jid in [j for j, ts in RECENT_DONE.items() if ts < cutoff]:
            del RECENT_DONE[jid]
    except Exception:
        pass


def _sweeper():
    while True:
        time.sleep(60)
        _sweep_once()


# ------------------------------------------------------------- handler ---
def _is_dashboard_probe(req):
    """Detect 9Router dashboard's 'Test Connection' probe (15s client timeout)."""
    try:
        if not isinstance(req, dict) or req.get("stream"):
            return False
        if req.get("max_tokens") != 1024:
            return False
        msgs = req.get("messages") or []
        if not msgs:
            return False
        last = msgs[-1]
        return (isinstance(last, dict) and last.get("role") == "user"
                and str(last.get("content", "")).strip().lower() == "hi")
    except Exception:
        return False


def _probe_completion():
    cid = "chatcmpl-" + uuid.uuid4().hex[:12]
    return {"id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": "muse",
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content":
                                     "Halo! Muse online — bridge 9Router aktif dan siap."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


class H(BaseHTTPRequestHandler):
    timeout = 120

    def log_message(self, *a):
        pass

    def _send(self, code, obj, ctype="application/json"):
        try:
            body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def _role(self):
        """'user' | 'worker' | None from the Bearer key (or legacy BRIDGE_TOKEN)."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[7:].strip()
        if TOKEN and hmac.compare_digest(token, TOKEN):
            return "user"  # legacy single token counts as a user key
        for k in _load_keys():
            if k.get("key") and hmac.compare_digest(token, k["key"]):
                return k.get("role")
        return None

    def _label(self):
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        for k in _load_keys():
            if k.get("key") and hmac.compare_digest(token, k["key"]):
                return k.get("label", "?")
        return "token"

    def _require(self, role):
        if self._role() != role:
            self._send(401, {"error": {"message":
                f"unauthorized: need bearer key with role={role}",
                "type": "auth_error", "code": "invalid_api_key"}})
            return False
        return True

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            self._send(413, {"error": {"message":
                f"request body too large (>{MAX_BODY} bytes)"}})
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}") if length else {}
        except Exception:
            self._send(400, {"error": {"message": "bad json"}})
            return None

    # -- GET -----------------------------------------------------------
    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path.rstrip("/") == "/v1/models":
                if not self._require("user"):
                    return
                self._send(200, {"object": "list", "data": [
                    {"id": "muse", "object": "model", "created": 0,
                     "owned_by": "muse"},
                    {"id": "muse-spark", "object": "model", "created": 0,
                     "owned_by": "muse"}]})
            elif path == "/health":
                self._send(200, {"ok": True})
            elif path.rstrip("/") == "/muse/pending":
                if not self._require("worker"):
                    return
                try:
                    limit = int(parse_qs(urlparse(self.path).query)
                                    .get("limit", ["3"])[0])
                except Exception:
                    limit = 3
                limit = max(1, min(10, limit))
                jobs, pending_n = _claim_jobs(limit, self._label())
                self._send(200, {"jobs": jobs, "count": len(jobs),
                                 "pending": pending_n})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            try:
                self._send(500, {"error": {"message": str(e)[:200]}})
            except Exception:
                pass

    # -- POST ----------------------------------------------------------
    def _cleanup(self, rid: str):
        for d in (PENDING, DONE):
            try:
                os.remove(_job_path(d, rid))
            except Exception:
                pass
        try:
            os.remove(_job_path(PROGRESS, rid)[:-5] + ".jsonl")
        except Exception:
            pass

    def _wait_answer(self, rid: str, keepalive_cb=None, progress_cb=None):
        """Wait for the worker answer; returns {"content", "tool_calls"} or None.

        While waiting, forwards worker progress notes (progress/<id>.jsonl,
        appended via POST /muse/progress) to progress_cb(text) so streaming
        clients see interim output instead of silence.
        """
        deadline = time.time() + WAIT_SECS
        answer, last_ping = None, time.time()
        progress_seen = 0
        while time.time() < deadline:
            if progress_cb:
                try:
                    pp = _job_path(PROGRESS, rid)[:-5] + ".jsonl"
                    if os.path.exists(pp):
                        with open(pp) as f:
                            lines = f.readlines()
                        for line in lines[progress_seen:]:
                            progress_seen += 1
                            try:
                                entry = json.loads(line)
                                text = entry.get("content", "")
                                kind = entry.get("kind", "content")
                            except Exception:
                                text, kind = "", "content"
                            if text and not progress_cb(text, kind):
                                return None
                except Exception:
                    pass
            dp = _job_path(DONE, rid)
            if os.path.exists(dp):
                try:
                    with open(dp) as f:
                        data = json.load(f)
                    answer = {"content": data.get("content", "") or "",
                              "tool_calls": data.get("tool_calls") or None,
                              "reasoning": data.get("reasoning") or None}
                except Exception:
                    answer = {"content": "", "tool_calls": None,
                              "reasoning": None}
                try:
                    os.remove(dp)
                except Exception:
                    pass
                break
            if keepalive_cb and time.time() - last_ping >= KEEPALIVE_SECS:
                if not keepalive_cb():
                    break
                last_ping = time.time()
            time.sleep(1)
        return answer

    def do_POST(self):
        try:
            path = urlparse(self.path).path.rstrip("/")
            if path == "/v1/chat/completions":
                if not self._require("user"):
                    return
                req = self._read_json()
                if req is None:
                    return
                if _is_dashboard_probe(req):
                    return self._send(200, _probe_completion())
                try:
                    npend = sum(1 for f in os.listdir(PENDING)
                                if f.endswith(".json"))
                except Exception:
                    npend = 0
                if npend >= MAX_PENDING:
                    return self._send(429, {"error": {"message":
                        "Muse bridge busy, try again in a bit"}})
                rid = uuid.uuid4().hex
                with open(_job_path(PENDING, rid), "w") as f:
                    json.dump({"id": rid, "received_at": time.time(),
                               "request": req}, f)
                if req.get("stream"):
                    return self._handle_stream(req, rid)
                answer = self._wait_answer(rid)
                self._cleanup(rid)
                if answer is None:
                    return self._send(504, {"error": {"message":
                        "Muse did not answer in time"}})
                self._send(200, _completion(answer, req))
            elif path == "/muse/answer":
                if not self._require("worker"):
                    return
                body = self._read_json()
                if body is None:
                    return
                jid, content = body.get("id", ""), body.get("content", "")
                reasoning = body.get("reasoning", "")
                tool_calls = _normalize_tool_calls(body.get("tool_calls"))
                if (not jid or not isinstance(content, str)
                        or not isinstance(reasoning, str)
                        or tool_calls is None
                        or (not content and not tool_calls)):
                    return self._send(400, {"error": {"message":
                        'need {"id": "...", "content": "..."} or '
                        '{"id": "...", "tool_calls": [{"id": "call_1", '
                        '"type": "function", "function": {"name": "...", '
                        '"arguments": "{...}"}}]}; optional "reasoning": "..."'}})
                res = _answer_job(jid, content, tool_calls or None,
                                  reasoning or None)
                if res is None:
                    return self._send(404, {"error": {"message":
                        "unknown job id (not leased?)"}})
                self._send(200, {"ok": True, "id": jid,
                                 "duplicate": res == "duplicate"})
            elif path == "/muse/progress":
                if not self._require("worker"):
                    return
                body = self._read_json()
                if body is None:
                    return
                jid, content = body.get("id", ""), body.get("content", "")
                kind = body.get("kind", "content")
                if kind not in ("content", "reasoning"):
                    kind = "content"
                if not jid or not isinstance(content, str) or not content:
                    return self._send(400, {"error": {"message":
                        'need {"id": "...", "content": "..."}'}})
                if not (os.path.exists(_job_path(PROCESSING, jid))
                        or os.path.exists(_job_path(PENDING, jid))):
                    return self._send(404, {"error": {"message":
                        "unknown job id (not leased?)"}})
                os.makedirs(PROGRESS, exist_ok=True)
                with LOCK:
                    with open(_job_path(PROGRESS, jid)[:-5] + ".jsonl", "a") as f:
                        f.write(json.dumps({"content": content[:4000],
                                            "kind": kind,
                                            "at": time.time()}) + "\n")
                self._send(200, {"ok": True, "id": jid})
            elif path == "/muse/release":
                if not self._require("worker"):
                    return
                body = self._read_json()
                if body is None:
                    return
                if _release_job(body.get("id", "")):
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"error": {"message":
                        "unknown job id (not leased?)"}})
            else:
                self._send(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        except Exception as e:
            try:
                self._send(500, {"error": {"message": str(e)[:200]}})
            except Exception:
                pass

    def _handle_stream(self, req, rid: str):
        self.close_connection = True  # SSE has no Content-Length; EOF ends it
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self._cleanup(rid)
            return
        if not self._sse_write(": connected\n\n"):
            self._cleanup(rid)
            return
        cid = "chatcmpl-" + uuid.uuid4().hex[:12]
        created = int(time.time())

        def _progress(text, kind="content"):
            field = "reasoning_content" if kind == "reasoning" else "content"
            chunk = {"id": cid, "object": "chat.completion.chunk",
                     "created": created, "model": "muse",
                     "choices": [{"index": 0,
                     "delta": {"role": "assistant", field: text},
                     "finish_reason": None}]}
            return self._sse_write("data: " + json.dumps(chunk) + "\n\n")

        answer = self._wait_answer(
            rid,
            keepalive_cb=lambda: self._sse_write(": ping\n\n"),
            progress_cb=_progress)
        self._cleanup(rid)
        if answer is None:
            err = {"error": {"message": "Muse did not answer in time",
                             "type": "timeout"}}
            self._sse_write("data: " + json.dumps(err) + "\n\ndata: [DONE]\n\n")
            return
        content = answer.get("content", "") if isinstance(answer, dict) else answer
        tool_calls = answer.get("tool_calls") if isinstance(answer, dict) else None
        reasoning = answer.get("reasoning") if isinstance(answer, dict) else None
        if reasoning:
            chunk_r = {"id": cid, "object": "chat.completion.chunk",
                       "created": created, "model": "muse",
                       "choices": [{"index": 0,
                       "delta": {"role": "assistant",
                                 "reasoning_content": reasoning},
                       "finish_reason": None}]}
            self._sse_write("data: " + json.dumps(chunk_r) + "\n\n")
        if content:
            chunk1 = {"id": cid, "object": "chat.completion.chunk",
                      "created": created, "model": "muse",
                      "choices": [{"index": 0,
                      "delta": {"role": "assistant", "content": content},
                      "finish_reason": None}]}
            self._sse_write("data: " + json.dumps(chunk1) + "\n\n")
        if tool_calls:
            delta_calls = []
            for i, tc in enumerate(tool_calls):
                delta_calls.append({
                    "index": i, "id": tc.get("id"), "type": "function",
                    "function": {"name": tc["function"]["name"],
                                 "arguments": tc["function"]["arguments"]}})
            chunk_tc = {"id": cid, "object": "chat.completion.chunk",
                        "created": created, "model": "muse",
                        "choices": [{"index": 0,
                        "delta": {"role": "assistant",
                                  "tool_calls": delta_calls},
                        "finish_reason": None}]}
            self._sse_write("data: " + json.dumps(chunk_tc) + "\n\n")
        chunk2 = {"id": cid, "object": "chat.completion.chunk", "created": created,
                  "model": "muse", "choices": [{"index": 0, "delta": {},
                  "finish_reason": "tool_calls" if tool_calls else "stop"}],
                  "usage": _estimate_usage(req, content, tool_calls, reasoning)}
        self._sse_write("data: " + json.dumps(chunk2) + "\n\ndata: [DONE]\n\n")

    def _sse_write(self, payload: str) -> bool:
        try:
            self.wfile.write(payload.encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return False


def _estimate_usage(req, content, tool_calls, reasoning):
    """Rough token estimate (~4 chars/token) so clients can show context use."""
    chars = 0
    try:
        for m in (req or {}).get("messages") or []:
            c = m.get("content") if isinstance(m, dict) else None
            if isinstance(c, str):
                chars += len(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict):
                        chars += len(str(part.get("text", "")))
            if isinstance(m, dict) and m.get("tool_calls"):
                chars += len(json.dumps(m["tool_calls"]))
    except Exception:
        chars = 0
    out = len(content or "") + len(reasoning or "")
    if tool_calls:
        out += len(json.dumps(tool_calls))
    p, c = max(1, chars // 4), max(1, out // 4)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _completion(answer, req=None):
    cid = "chatcmpl-" + uuid.uuid4().hex[:12]
    if isinstance(answer, dict):
        content = answer.get("content", "") or ""
        tool_calls = answer.get("tool_calls") or None
        reasoning = answer.get("reasoning") or None
    else:
        content, tool_calls, reasoning = answer, None, None
    message = {"role": "assistant", "content": content if content else None}
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": "muse",
            "choices": [{"index": 0,
                         "message": message,
                         "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": _estimate_usage(req, content, tool_calls, reasoning)}


# ------------------------------------------------------------------ ---
def cmd_serve():
    import subprocess
    for d in (PENDING, DONE, PROCESSING, PROGRESS):
        os.makedirs(d, exist_ok=True)
    # crash recovery: anything left in processing/ goes back to pending/
    try:
        for fn in os.listdir(PROCESSING):
            if fn.endswith(".json"):
                _release_job(fn[:-5])
    except Exception:
        pass

    threading.Thread(target=_sweeper, daemon=True).start()

    def listen_addrs():
        addrs = ["127.0.0.1"]
        try:
            out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                                 text=True, timeout=10).stdout
            for ip in out.split():
                ip = ip.strip()
                if ip and ip not in addrs:
                    addrs.append(ip)
        except Exception:
            pass
        return addrs

    ok = 0
    for addr in listen_addrs():
        try:
            srv = ThreadingHTTPServer((addr, 8765), H)
        except OSError as e:
            print(f"  [!] tidak bisa listen di {addr}:8765 ({e})", flush=True)
            continue
        srv.daemon_threads = True
        srv.allow_reuse_address = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"muse-bridge v5 listening on {addr}:8765", flush=True)
        ok += 1
    if not ok:
        print("FATAL: tidak ada listen address yang berhasil", flush=True)
        return 2
    n = len(_load_keys())
    print(f"keys: {n} in {KEYS_FILE} | "
          f"legacy BRIDGE_TOKEN: {'on (/v1/*)' if TOKEN else 'off'} | "
          f"lease={LEASE_SECS}s wait={WAIT_SECS}s", flush=True)
    threading.Event().wait()


def main():
    ap = argparse.ArgumentParser(prog="bridge.py",
                                 description="Muse bridge v5 for 9Router")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the HTTP bridge (default)")
    g = sub.add_parser("keygen", help="create an API key (printed once)")
    g.add_argument("--role", required=True, choices=["user", "worker"])
    g.add_argument("--label", required=True)
    sub.add_parser("keylist", help="list keys (prefixes only)")
    d = sub.add_parser("keydel", help="revoke a key by prefix or label")
    d.add_argument("ident")
    args = ap.parse_args()
    if args.cmd == "keygen":
        return cmd_keygen(args.role, args.label)
    if args.cmd == "keylist":
        return cmd_keylist()
    if args.cmd == "keydel":
        return cmd_keydel(args.ident)
    return cmd_serve() or 0  # default: serve (v4-compatible: `python bridge.py`)


if __name__ == "__main__":
    sys.exit(main())
