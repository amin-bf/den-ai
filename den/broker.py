"""`den serve`: the GPU broker. Everything that uses the GPU goes through it.

LLM requests (Ollama's native /api/… and OpenAI-style /v1/…) are passed through to Ollama,
streamed as they arrive. The broker counts the requests in progress and owns mode switches:
a switch that turns the LLM off refuses new LLM work, waits for the running requests (or
cancels them with `now`), unloads the models and only then saves the new mode. It re-reads
config.toml and state.json on every request; only the listen address needs a restart.

Own endpoints: GET /status, POST /mode {"mode", "now"} (NDJSON progress lines).
"""

import http.client
import itertools
import json
import os
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from den import core
from den.core import DenError, Ollama

# Requests that don't put work on the GPU: always allowed, whatever the mode, and not tracked.
CONTROL_PATHS = {
    "/", "/api/version", "/api/tags", "/api/ps", "/api/show", "/api/pull", "/api/push",
    "/api/delete", "/api/copy", "/api/create", "/v1/models",
}
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer",
    "upgrade", "content-length", "host",
}
SET_BY_BROKER = {"date", "server"}
UPSTREAM_TIMEOUT_S = 900  # per read: covers loading a model and reading a 32k prompt
# Native endpoints that load a model and accept keep_alive in the body.
KEEP_ALIVE_PATHS = {"/api/generate", "/api/chat", "/api/embed", "/api/embeddings"}
PROGRESS_INTERVAL_S = 5
UNLOAD_TIMEOUT_S = 60


def _is_unload(body):
    """A bare `keep_alive: 0` generate/chat: it frees memory, so it counts as control."""
    return (
        isinstance(body, dict)
        and str(body.get("keep_alive")) in ("0", "0.0", "0s", "0m")
        and not body.get("prompt")
        and not body.get("messages")
    )


class Broker:
    """In-flight requests and the pending mode switch, shared by all handler threads."""

    def __init__(self):
        self.cond = threading.Condition()
        self.inflight = {}  # id -> request info (and its upstream connection)
        self.pending_mode = None
        self.ids = itertools.count(1)
        self.started = time.time()

    def admit(self, info):
        """Register LLM work, or raise DenError when the mode doesn't allow it right now."""
        config = core.load_config()
        state = core.load_state(config)
        with self.cond:
            if not core.llm_on(state):
                raise DenError(f"the local LLM is off (mode: {state['mode']}); turn it on with: den mode llm")
            if self.pending_mode is not None and "llm" not in core.MODES[self.pending_mode]:
                raise DenError(
                    f"the local LLM is being turned off (switching to mode {self.pending_mode}); "
                    "no new LLM requests until: den mode llm"
                )
            info["id"] = next(self.ids)
            info["started"] = time.time()
            self.inflight[info["id"]] = info
        return info["id"]

    def finish(self, req_id):
        with self.cond:
            self.inflight.pop(req_id, None)
            self.cond.notify_all()

    def cancel(self, req_id, reason):
        with self.cond:
            info = self.inflight.get(req_id)
        if not info:
            return
        info["cancelled"] = reason
        conn = info.get("upstream")
        if conn is not None and conn.sock is not None:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)  # Ollama stops generating once the client is gone
            except OSError:
                pass

    def snapshot(self):
        now = time.time()
        with self.cond:
            return [
                {
                    "id": i["id"],
                    "side": "llm",
                    "caller": i["caller"],
                    "request": f"{i['method']} {i['path']}",
                    "model": i["model"],
                    "seconds": round(now - i["started"], 1),
                }
                for i in sorted(self.inflight.values(), key=lambda i: i["id"])
            ]

    def status(self):
        config = core.load_config()
        state = core.load_state(config)
        ollama = Ollama(config["llm"]["base_url"])
        try:
            upstream = {"url": ollama.base_url, "version": ollama.version(), "loaded": ollama.loaded()}
        except DenError as e:
            upstream = {"url": ollama.base_url, "error": str(e)}
        return {
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self.started),
            "mode": state["mode"],
            "pending_mode": self.pending_mode,
            "inflight": self.snapshot(),
            "ollama": upstream,
        }

    def switch_mode(self, mode, now, emit, caller_gone):
        """Run a mode switch, reporting progress through emit(dict). Raises DenError.

        caller_gone() is polled while waiting: a caller that hangs up (Ctrl+C) drops the switch.
        """
        config = core.load_config()
        state = core.load_state(config)
        if mode not in core.MODES:
            raise DenError(f"unknown mode {mode!r}; use one of: {', '.join(core.MODES)}")
        if "image" in core.MODES[mode] and not core.image_available(config):
            raise DenError("image generation is not set up yet (no [image.profiles] in config.toml)")
        with self.cond:
            if self.pending_mode is not None:
                raise DenError(f"a switch to mode {self.pending_mode} is already waiting")
            self.pending_mode = mode
        try:
            unloaded = []
            if "llm" not in core.MODES[mode]:
                self._drain(now, emit, caller_gone)
                unloaded = self._unload(Ollama(config["llm"]["base_url"]), emit)
            state = core.load_state(config)
            state["mode"] = mode
            core.save_state(state)
            emit({"mode": mode, "unloaded": unloaded})
        finally:
            with self.cond:
                self.pending_mode = None

    def _drain(self, now, emit, caller_gone):
        """Wait until no LLM request is running; with now, cancel them first."""
        if now:
            for r in self.snapshot():
                self.cancel(r["id"], "cancelled by: den mode --now")
        last, last_emit = None, 0.0
        while True:
            running = self.snapshot()
            if not running:
                return
            if caller_gone():
                raise ConnectionResetError("the caller hung up")
            ids = [r["id"] for r in running]
            if ids != last or time.time() - last_emit >= PROGRESS_INTERVAL_S:
                emit({"cancelling" if now else "waiting": running})
                last, last_emit = ids, time.time()
            with self.cond:
                self.cond.wait(timeout=1)

    def _unload(self, ollama, emit):
        """Unload every loaded model and wait until Ollama no longer lists them.

        Ollama answers the unload at once but frees the memory only when the model's last
        request ends, so /api/ps is the signal that the GPU is free.
        """
        try:
            names = [m["name"] for m in ollama.loaded()]
        except DenError:
            return []  # Ollama not running: nothing holds GPU memory
        if not names:
            return []
        emit({"unloading": names})
        for name in names:
            ollama.unload(name)
        deadline = time.time() + UNLOAD_TIMEOUT_S
        while ollama.loaded():
            if time.time() > deadline:
                raise DenError(f"ollama still lists loaded models {UNLOAD_TIMEOUT_S}s after the unload")
            time.sleep(0.5)
        return names


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "den-broker"
    broker: Broker  # set on the class by serve()

    def log_message(self, fmt, *args):  # one line per request is written by _log instead
        pass

    def _log(self, text):
        print(f"{time.strftime('%H:%M:%S')} {text}", file=sys.stderr, flush=True)

    # --- plumbing ---

    def _read_body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            parts = []
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return b"".join(parts)
                parts.append(self.rfile.read(size))
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send_json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _error_body(self, message):
        # OpenAI-style clients (pi) read error.message; Ollama's native clients read error.
        if self.path.startswith("/v1/"):
            return {"error": {"message": f"den: {message}", "type": "den_error"}}
        return {"error": f"den: {message}"}

    def _caller_gone(self):
        readable, _, _ = select.select([self.connection], [], [], 0)
        if not readable:
            return False
        try:
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def _chunk(self, data):
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")

    # --- dispatch ---

    def _dispatch(self):
        try:
            path = urlsplit(self.path).path
            if path == "/status" and self.command == "GET":
                self._send_json(200, self.broker.status())
            elif path == "/mode" and self.command == "POST":
                self._mode(json.loads(self._read_body() or b"{}"))
            else:
                self._proxy(path)
        except DenError as e:
            self._send_json(503, self._error_body(str(e)))
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except (OSError, http.client.HTTPException) as e:  # e.g. Ollama stalled or died mid-stream
            self._log(f"{self.command} {self.path} failed: {e!r}")
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = _dispatch

    def _mode(self, body):
        started = False

        def emit(obj):
            nonlocal started
            if not started:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                started = True
            self._chunk(json.dumps(obj).encode() + b"\n")

        mode = body.get("mode")
        self._log(f"mode switch to {mode}{' --now' if body.get('now') else ''} requested")
        try:
            self.broker.switch_mode(mode, bool(body.get("now")), emit, self._caller_gone)
        except DenError as e:
            if not started:
                raise
            emit({"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            self._log(f"mode switch to {mode} dropped: the caller hung up")
            self.close_connection = True
            return
        self._log(f"mode switch to {mode} done")
        self.wfile.write(b"0\r\n\r\n")

    def _proxy(self, path):
        body = self._read_body()
        try:
            parsed = json.loads(body) if body else None
        except json.JSONDecodeError:
            parsed = None
        control = path in CONTROL_PATHS or path.startswith("/api/blobs/") or _is_unload(parsed)
        info = {
            "caller": self.headers.get("X-Den-Caller") or self.headers.get("User-Agent", "?").split(" ")[0],
            "method": self.command,
            "path": path,
            "model": parsed.get("model") if isinstance(parsed, dict) else None,
        }
        req_id = None if control else self.broker.admit(info)
        try:
            config = core.load_config()
            keep_alive = config["llm"].get("keep_alive")
            if req_id is not None and keep_alive and path in KEEP_ALIVE_PATHS and isinstance(parsed, dict) and "keep_alive" not in parsed:
                parsed["keep_alive"] = keep_alive  # callers like pi never send one: Ollama's default is 5 m
                body = json.dumps(parsed).encode()
            self._forward(body, info)
            if req_id is not None and keep_alive and path.startswith("/v1/") and info.get("status") == 200:
                self._refresh_keep_alive(config, info, keep_alive)
        finally:
            if req_id is not None:
                self.broker.finish(req_id)
                took = time.time() - info["started"]
                outcome = info.get("status", "no response") if not info.get("cancelled") else info["cancelled"]
                self._log(f"#{req_id} {info['caller']} {self.command} {path} {info['model']} -> {outcome} in {took:.1f}s")

    def _refresh_keep_alive(self, config, info, keep_alive):
        """Give a /v1 request's model the configured keep_alive: Ollama ignores it in /v1 bodies.

        It runs while the request still counts as in flight, so a mode switch can't unload the
        model in between and have this load it again. It's skipped when another request wants a
        different model, since loading this one back would force an extra swap.
        """
        model = info["model"]
        if not model or any(r["model"] not in (None, model) for r in self.broker.snapshot() if r["id"] != info["id"]):
            return
        try:
            Ollama(config["llm"]["base_url"]).keep_loaded(model, keep_alive)
        except DenError as e:
            self._log(f"#{info['id']} keep_alive refresh for {model} failed: {e}")

    def _forward(self, body, info):
        config = core.load_config()
        upstream = urlsplit(config["llm"]["base_url"])
        conn = http.client.HTTPConnection(upstream.hostname, upstream.port or 80, timeout=UPSTREAM_TIMEOUT_S)
        info["upstream"] = conn
        if info.get("cancelled"):
            raise DenError(info["cancelled"])
        headers = {
            k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP and k.lower() != "x-den-caller"
        }
        if body or self.command in ("POST", "PUT"):
            headers["Content-Length"] = str(len(body))
        try:
            try:
                conn.request(self.command, self.path, body=body or None, headers=headers)
                resp = conn.getresponse()
                info["status"] = resp.status
            except OSError as e:
                if info.get("cancelled"):
                    raise DenError(info["cancelled"]) from e
                raise DenError(
                    f"ollama is not reachable at {config['llm']['base_url']} ({e}); "
                    "start it with: sudo systemctl enable --now ollama"
                ) from e

            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_BY_HOP | SET_BY_BROKER:
                    self.send_header(k, v)
            if self.command == "HEAD":
                self.send_header("Content-Length", resp.getheader("Content-Length", "0"))
                self.end_headers()
                return
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            content_type = resp.getheader("Content-Type", "")
            try:
                while chunk := resp.read1(65536):
                    self._chunk(chunk)
            except (OSError, http.client.HTTPException):
                if not info.get("cancelled"):
                    raise
            if info.get("cancelled"):
                self._chunk(self._stream_error(content_type, info["cancelled"]))
                self.close_connection = True
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            info["cancelled"] = "caller hung up"  # closing upstream below makes Ollama stop
            raise
        finally:
            conn.close()

    def _stream_error(self, content_type, message):
        """An error in the stream's own format, so the client shows why it was cut off."""
        if "event-stream" in content_type:
            return b"data: " + json.dumps(self._error_body(message)).encode() + b"\n\n"
        return json.dumps(self._error_body(message)).encode() + b"\n"


def serve():
    config = core.load_config()
    url = urlsplit(core.broker_url(config))
    Handler.broker = Broker()
    server = ThreadingHTTPServer((url.hostname, url.port), Handler)
    server.daemon_threads = True
    print(f"den broker on {url.hostname}:{url.port} -> ollama {config['llm']['base_url']}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
