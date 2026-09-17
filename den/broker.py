"""`den serve`: the GPU broker. Everything that uses the GPU goes through it.

Two sides share the GPU, one loaded at a time: the LLM (Ollama) and image generation
(ComfyUI, a systemd user service the broker starts and stops). LLM requests (Ollama's native
/api/… and OpenAI-style /v1/…) are passed through to Ollama, streamed as they arrive. Image
requests (POST /image) fill in a workflow, run it on ComfyUI and save the result.

A request for the side that isn't loaded waits for a swap: the loaded side finishes its running
requests, then the broker unloads it and loads the other. While the other side waits, the
loaded side starts new requests only until a cap (time or count) is reached, so batches go
together without starving either side. ComfyUI stays up after an image until the next LLM
request, a `switch_back`, or `[image] keep_alive` of idle time.

A request runs when its side can: an LLM model is selected, a workflow can run, and — for a side
that isn't loaded yet — the machine isn't already busy with other work ([limits], ADR 0004).
Otherwise the broker says why. The mode is only the kill switch `den mode off`, which refuses
new requests, waits for the running ones (or cancels them with `now`) and unloads both sides.
POST /unload releases the sides alone, leaving the mode on: it refuses new requests while it
runs, because whoever asked for the machine back is about to use it, and afterwards the next
request loads its side again. The broker re-reads config.toml and state.json on every request;
only the listen address needs a restart.

Own endpoints: GET /status, POST /mode {"mode", "now"}, POST /unload {"sides", "now"} and
POST /image (all but /status stream NDJSON progress lines).
"""

import http.client
import itertools
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from den import core, image
from den.core import DenError, Ollama
from den.image import ComfyUI

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
WAIT_REPORT_INTERVAL_S = 30
UNLOAD_TIMEOUT_S = 60
COMFYUI_START_TIMEOUT_S = 180
COMFYUI_STOP_TIMEOUT_S = 30
IDLE_CHECK_S = 10
SIDES = ("llm", "image")
DEFAULT_BATCH_SECONDS = 120
DEFAULT_BATCH_REQUESTS = 4


def log(text):
    print(f"{time.strftime('%H:%M:%S')} {text}", file=sys.stderr, flush=True)


def _is_unload(body):
    """A bare `keep_alive: 0` generate/chat: it frees memory, so it counts as control."""
    return (
        isinstance(body, dict)
        and str(body.get("keep_alive")) in ("0", "0.0", "0s", "0m")
        and not body.get("prompt")
        and not body.get("messages")
    )


def _unavailable(side, config, state):
    """Why side can't run now, or None: what's installed and selected decides, not a mode."""
    if side == "llm":
        return core.llm_unavailable(config, state)
    return image.unavailable(config, state)


def _no_emit(msg):
    pass


def _systemctl(action, service):
    try:
        result = subprocess.run(
            ["systemctl", "--user", action, service], capture_output=True, text=True, timeout=COMFYUI_STOP_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DenError(f"systemctl --user {action} {service} failed: {e}") from e
    if result.returncode != 0:
        raise DenError(f"systemctl --user {action} {service} failed: {result.stderr.strip()}")


class Broker:
    """Which side holds the GPU, and the requests running or waiting, shared by all handler threads."""

    def __init__(self):
        self.cond = threading.Condition()
        self.inflight = {}  # id -> request info (and how to cancel it)
        self.waiting = {}  # id -> request info, waiting for its side to be loaded or for its turn
        self.starts = {side: [] for side in SIDES}  # recent start times, for the batch count cap
        self.loaded = None  # the side holding the GPU, as far as the broker knows
        self.loaded_since = time.time()
        self.swapping = None  # "llm"/"image" while swapping to that side; "mode", "unload" or "idle" while unloading
        self.pending_mode = None
        self.releasing = ()  # the sides a release (POST /unload) is freeing: refused meanwhile
        self.last_image = time.time()
        self.ids = itertools.count(1)
        self.started = time.time()

    # --- admission and the swap ---

    def detect_loaded(self):
        """The side already on the GPU when the broker starts."""
        config = core.load_config()
        if image.settings(config) and ComfyUI(image.settings(config)["base_url"]).up():
            self.loaded = "image"
        else:
            try:
                self.loaded = "llm" if Ollama(config["llm"]["base_url"]).loaded() else None
            except DenError:
                self.loaded = None
        return self.loaded

    def _check_available(self, side, config, state):
        unavailable = _unavailable(side, config, state)
        if unavailable:
            raise DenError(unavailable)
        if self.pending_mode == "off":
            raise DenError("den is being turned off; no new requests until: den mode on")
        if side in self.releasing:
            # Someone asked for the CPU, RAM and GPU back and is about to use them: waiting here
            # would load the side again right behind the release, which is what they asked to avoid.
            raise DenError(
                f"den is releasing the CPU, RAM and GPU the {side} side holds (den unload); "
                "retry once it's done"
            )
        # Serving from a loaded side is cheap; loading one on a machine that is already busy is
        # not, and it's the caller's own test run or build that's usually busy (ADR 0004).
        if self.loaded != side and (busy := core.too_busy(config)):
            raise DenError(
                f"the machine is busy: {busy}. The {side} side isn't loaded and den won't load it "
                "now — retry when the machine settles, or raise the limits in [limits] of config.toml"
            )

    def _batch_open(self, side, config, now):
        """Whether the loaded side may start another request while the other side waits.

        The caps count from when the other side started waiting, or from when this side was
        loaded if that's later: requests that waited through a swap always get their turn.
        """
        others = [w["queued"] for w in self.waiting.values() if w["side"] != side]
        if not others:
            return True
        since = max(min(others), self.loaded_since)
        broker_config = config.get("broker", {})
        max_s = core.duration_s(broker_config.get("batch_seconds", DEFAULT_BATCH_SECONDS))
        max_n = int(broker_config.get("batch_requests", DEFAULT_BATCH_REQUESTS))
        started = sum(1 for t in self.starts[side] if t >= since)
        return now - since < max_s and started < max_n

    def _can_start(self, side, config, now):
        return self.swapping is None and self.loaded == side and self._batch_open(side, config, now)

    def _can_swap(self, side, config, now):
        """Swap to side once the loaded side has nothing running and nothing it may still start."""
        if self.swapping is not None or self.loaded == side:
            return False
        if any(i["side"] != side for i in self.inflight.values()):
            return False
        return not any(
            w["side"] == self.loaded and self._can_start(self.loaded, config, now) for w in self.waiting.values()
        )

    def _wait_reason(self, side, config, now):
        if self.swapping in SIDES:
            return {"reason": f"swapping to the {self.swapping} side"}
        if self.swapping == "mode":
            return {"reason": "den is being turned off"}
        if self.swapping == "unload":
            return {"reason": "the loaded side is being unloaded; this request loads it again"}
        if self.swapping == "idle":
            return {"reason": "the idle image side is being stopped"}
        running = [r for r in self.snapshot() if r["side"] != side]
        if self.loaded == side:
            return {"reason": f"the {_other(side)} side is waiting and the {side} side reached its batch cap"}
        if running:
            return {"reason": f"the {self.loaded} side has running requests; the swap follows", "running": running}
        return {"reason": f"the {self.loaded} side still has requests to start before the swap"}

    def admit(self, info, emit=_no_emit, caller_gone=lambda: False):
        """Start a request for info["side"] once that side is loaded; returns its id.

        Waits (reporting through emit) while the other side holds the GPU, and runs the swap
        itself when it's due. Raises DenError when the side can't run, and
        ConnectionResetError when the caller hangs up while waiting.
        """
        side = info["side"]
        info["id"] = next(self.ids)
        last_reason, last_report = None, 0.0
        try:
            while True:
                config = core.load_config()
                state = core.load_state(config)
                with self.cond:
                    self._check_available(side, config, state)
                    now = time.time()
                    if self._can_start(side, config, now):
                        self.waiting.pop(info["id"], None)
                        info["started"] = now
                        self.inflight[info["id"]] = info
                        self.starts[side] = [t for t in self.starts[side] if now - t < 3600][-100:] + [now]
                        if last_reason is not None:
                            log(f"#{info['id']} {info['caller']} {side} starts after {now - info['queued']:.1f}s")
                        return info["id"]
                    if info["id"] not in self.waiting:
                        info["queued"] = now
                        self.waiting[info["id"]] = info
                    swap = self._can_swap(side, config, now)
                    if swap:
                        self.swapping = side
                    else:
                        reason = self._wait_reason(side, config, now)
                if swap:
                    self._swap(side, config, emit)
                    continue
                # Report a change of reason or of the requests it waits on, not their growing ages.
                key = (reason["reason"], [r["id"] for r in reason.get("running", [])])
                if key != last_reason or now - last_report >= WAIT_REPORT_INTERVAL_S:
                    if key[0] != (last_reason or ("",))[0]:
                        log(f"#{info['id']} {info['caller']} {side} waits: {reason['reason']}")
                    emit({"waiting": reason})
                    last_reason, last_report = key, now
                if caller_gone():
                    log(f"#{info['id']} {info['caller']} {side} gave up after waiting {now - info['queued']:.1f}s")
                    raise ConnectionResetError("the caller hung up while waiting")
                with self.cond:
                    self.cond.wait(timeout=1)
        finally:
            with self.cond:
                if self.waiting.pop(info["id"], None) is not None:
                    self.cond.notify_all()

    def _swap(self, side, config, emit):
        """Unload the other side and load this one; self.swapping is already set by the caller."""
        began = time.time()
        log(f"swap to the {side} side (from {self.loaded or 'nothing'})")
        try:
            if side == "image":
                self._unload(Ollama(config["llm"]["base_url"]), emit)
                self._start_comfyui(config, emit)
            else:
                self._stop_comfyui(config, emit)
        except DenError as e:
            log(f"swap to the {side} side failed: {e}")
            with self.cond:
                self.loaded, self.swapping = None, None
                self.cond.notify_all()
            raise
        with self.cond:
            self.loaded, self.loaded_since, self.swapping = side, time.time(), None
            if side == "image":
                self.last_image = time.time()
            self.cond.notify_all()
        log(f"swap to the {side} side done in {time.time() - began:.1f}s")

    def finish(self, req_id):
        with self.cond:
            info = self.inflight.pop(req_id, None)
            if info and info["side"] == "image":
                self.last_image = time.time()
            self.cond.notify_all()

    def cancel(self, req_id, reason):
        with self.cond:
            info = self.inflight.get(req_id)
        if not info:
            return
        info["cancelled"] = reason
        if info.get("cancel"):  # an image request: ComfyUI drops or interrupts the prompt
            try:
                info["cancel"]()
            except DenError as e:
                log(f"#{req_id} cancel failed: {e}")
            return
        conn = info.get("upstream")
        if conn is not None and conn.sock is not None:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)  # Ollama stops generating once the client is gone
            except OSError:
                pass

    def switch_back(self, config, emit):
        """After an image request that asked for it: stop ComfyUI if no image work is left.

        The LLM isn't preloaded; it loads on its next request as usual.
        """
        state = core.load_state(config)
        with self.cond:
            if (
                self.loaded != "image"
                or self.swapping is not None
                or _unavailable("llm", config, state)
                or any(i["side"] == "image" for i in [*self.inflight.values(), *self.waiting.values()])
            ):
                return False
            self.swapping = "llm"
        self._swap("llm", config, emit)
        return True

    def stop_if_idle(self):
        """Stop ComfyUI after [image] keep_alive without image requests (checked every few seconds)."""
        config = core.load_config()
        keep_alive = core.duration_s(image.settings(config).get("keep_alive", "30m"))
        with self.cond:
            if (
                self.loaded != "image"
                or self.swapping is not None
                or time.time() - self.last_image < keep_alive
                or any(i["side"] == "image" for i in [*self.inflight.values(), *self.waiting.values()])
            ):
                return
            self.swapping = "idle"
        try:
            try:
                if ComfyUI(image.settings(config)["base_url"]).busy():  # its web UI is in use
                    with self.cond:
                        self.last_image = time.time()
                    return
            except DenError:
                pass  # not answering: stopping the service is still right
            log(f"the image side was idle for {keep_alive:.0f}s; stopping ComfyUI")
            self._stop_comfyui(config, _no_emit)
            with self.cond:
                self.loaded = None
        finally:
            with self.cond:
                self.swapping = None
                self.cond.notify_all()

    def idle_loop(self):
        while True:
            time.sleep(IDLE_CHECK_S)
            try:
                self.stop_if_idle()
            except Exception as e:  # keep checking: one failed stop mustn't end the timeout
                log(f"idle check failed: {e!r}")

    # --- the sides' processes ---

    def _start_comfyui(self, config, emit):
        settings = image.settings(config)
        comfy = ComfyUI(settings["base_url"])
        if comfy.up():
            return
        service = settings.get("service", "comfyui")
        emit({"starting": service})
        _systemctl("start", service)
        deadline = time.time() + COMFYUI_START_TIMEOUT_S
        while not comfy.up():
            if time.time() > deadline:
                raise DenError(
                    f"ComfyUI didn't answer at {comfy.base_url} {COMFYUI_START_TIMEOUT_S}s after starting {service}; "
                    f"see: journalctl --user -u {service}"
                )
            try:
                _systemctl("is-active", service)
            except DenError as e:
                raise DenError(f"{service} stopped while starting; see: journalctl --user -u {service}") from e
            time.sleep(1)

    def _stop_comfyui(self, config, emit):
        settings = image.settings(config)
        if not settings:
            return
        comfy = ComfyUI(settings["base_url"])
        if not comfy.up():
            return
        service = settings.get("service", "comfyui")
        emit({"stopping": service})
        _systemctl("stop", service)
        deadline = time.time() + COMFYUI_STOP_TIMEOUT_S
        while comfy.up():
            if time.time() > deadline:
                raise DenError(
                    f"ComfyUI still answers at {comfy.base_url} after stopping {service}; "
                    "was it started by hand? Stop it, then retry"
                )
            time.sleep(0.5)

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

    # --- status and mode switches ---

    def snapshot(self, requests=None):
        now = time.time()
        with self.cond:
            requests = list((self.inflight if requests is None else requests).values())
            return [
                {
                    "id": i["id"],
                    "side": i["side"],
                    "caller": i["caller"],
                    "request": f"{i['method']} {i['path']}",
                    "model": i["model"],
                    "seconds": round(now - i.get("started", i.get("queued", now)), 1),
                }
                for i in sorted(requests, key=lambda i: i["id"])
            ]

    def status(self):
        config = core.load_config()
        state = core.load_state(config)
        ollama = Ollama(config["llm"]["base_url"])
        try:
            upstream = {"url": ollama.base_url, "version": ollama.version(), "loaded": ollama.loaded()}
        except DenError as e:
            upstream = {"url": ollama.base_url, "error": str(e)}
        pressure = core.pressure()
        result = {
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self.started),
            "mode": state["mode"],
            "pending_mode": self.pending_mode,
            "releasing": list(self.releasing) or None,
            "pressure": pressure,
            # Set while a side that isn't loaded would have to wait for the machine to settle.
            "too_busy": core.too_busy(config, pressure),
            "unavailable": {side: _unavailable(side, config, state) for side in SIDES},
            "loaded": self.loaded,
            "loaded_s": round(time.time() - self.loaded_since),
            "swapping": self.swapping,
            "inflight": self.snapshot(),
            "waiting": self.snapshot(self.waiting),
            "ollama": upstream,
        }
        settings = image.settings(config)
        if settings:
            result["comfyui"] = {
                "url": settings["base_url"],
                "up": ComfyUI(settings["base_url"]).up(),
                "idle_s": round(time.time() - self.last_image) if self.loaded == "image" else None,
            }
        return result

    def switch_mode(self, mode, now, emit, caller_gone):
        """Run a mode switch, reporting progress through emit(dict). Raises DenError.

        caller_gone() is polled while waiting: a caller that hangs up (Ctrl+C) drops the switch.
        """
        config = core.load_config()
        if mode not in core.MODES:
            raise DenError(f"unknown mode {mode!r}; use one of: {', '.join(core.MODES)}")
        with self.cond:
            if self.pending_mode is not None:
                raise DenError(f"a switch to mode {self.pending_mode} is already waiting")
            self.pending_mode = mode
            self.cond.notify_all()  # waiting requests for a side being turned off give up
        try:
            off = list(SIDES) if mode == "off" else []
            self._drain(off, now, emit, caller_gone)
            unloaded = []
            try:
                if "llm" in off:
                    unloaded += self._unload(Ollama(config["llm"]["base_url"]), emit)
                if "image" in off and image.settings(config) and ComfyUI(image.settings(config)["base_url"]).up():
                    self._stop_comfyui(config, emit)
                    unloaded.append("comfyui")
                with self.cond:
                    if self.loaded in off:
                        self.loaded = None
            finally:
                with self.cond:
                    self.swapping = None
                    self.cond.notify_all()
            state = core.load_state(config)
            state["mode"] = mode
            core.save_state(state)
            emit({"mode": mode, "unloaded": unloaded})
        finally:
            with self.cond:
                self.pending_mode = None
                self.cond.notify_all()

    def unload_sides(self, sides, now, emit, caller_gone):
        """Release the sides — GPU, and the RAM and CPU their models hold — without changing the mode.

        Progress is reported through emit(dict). A release is an explicit "I need this machine
        now", so new requests are refused while it runs; the mode stays on, so once it's done the
        next request loads its side again. That's the difference from switching to mode off, which
        keeps refusing until `den mode on`.
        """
        config = core.load_config()
        unknown = [side for side in sides if side not in SIDES]
        if unknown:
            raise DenError(f"unknown side {unknown[0]!r}; use one of: {', '.join(SIDES)}")
        with self.cond:
            if self.releasing:
                raise DenError(f"a release of the {' and '.join(self.releasing)} side is already running")
            self.releasing = tuple(sides)
            self.cond.notify_all()  # queued requests give up instead of loading behind the release
        try:
            self._drain(sides, now, emit, caller_gone, command="unload")
            unloaded = []
            try:
                if "llm" in sides:
                    unloaded += self._unload(Ollama(config["llm"]["base_url"]), emit)
                if "image" in sides and image.settings(config) and ComfyUI(image.settings(config)["base_url"]).up():
                    self._stop_comfyui(config, emit)
                    unloaded.append("comfyui")
                with self.cond:
                    if self.loaded in sides:
                        self.loaded = None
            finally:
                with self.cond:
                    self.swapping = None
                    self.cond.notify_all()
        finally:
            with self.cond:
                self.releasing = ()
                self.cond.notify_all()
        emit({"unloaded": unloaded})

    def _drain(self, sides, now, emit, caller_gone, command="mode"):
        """Wait until no request for the sides runs and no swap is going on, then hold off swaps.

        With now, cancel the running requests first. command is the `den` command doing it:
        both refuse new requests meanwhile, "mode" until it's turned back on (pending_mode),
        "unload" only until the release is done (releasing).
        """
        if now:
            for r in self.snapshot():
                if r["side"] in sides:
                    self.cancel(r["id"], f"cancelled by: den {command} --now")
        last, last_emit = None, 0.0
        while True:
            with self.cond:
                running = [r for r in self.snapshot() if r["side"] in sides]
                if not running and self.swapping is None:
                    self.swapping = "mode" if command == "mode" else "unload"
                    return
            if caller_gone():
                raise ConnectionResetError("the caller hung up")
            ids = [r["id"] for r in running]
            if running and (ids != last or time.time() - last_emit >= PROGRESS_INTERVAL_S):
                emit({"cancelling" if now else "waiting": running})
                last, last_emit = ids, time.time()
            with self.cond:
                self.cond.wait(timeout=1)


def _other(side):
    return "image" if side == "llm" else "llm"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "den-broker"
    broker: Broker  # set on the class by serve()

    def log_message(self, fmt, *args):  # one line per request is written by log() instead
        pass

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

    def _caller(self):
        return self.headers.get("X-Den-Caller") or self.headers.get("User-Agent", "?").split(" ")[0]

    def _ndjson(self):
        """An emit(dict) that starts a chunked NDJSON response on first use; started() tells if it did."""
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

        return emit, lambda: started

    # --- dispatch ---

    def _dispatch(self):
        try:
            path = urlsplit(self.path).path
            if path == "/status" and self.command == "GET":
                self._send_json(200, self.broker.status())
            elif path == "/mode" and self.command == "POST":
                self._mode(json.loads(self._read_body() or b"{}"))
            elif path == "/unload" and self.command == "POST":
                self._unload(json.loads(self._read_body() or b"{}"))
            elif path == "/image" and self.command == "POST":
                self._image(json.loads(self._read_body() or b"{}"))
            else:
                self._proxy(path)
        except DenError as e:
            self._send_json(503, self._error_body(str(e)))
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except (OSError, http.client.HTTPException) as e:  # e.g. Ollama stalled or died mid-stream
            log(f"{self.command} {self.path} failed: {e!r}")
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = _dispatch

    def _mode(self, body):
        emit, started = self._ndjson()
        mode = body.get("mode")
        log(f"mode switch to {mode}{' --now' if body.get('now') else ''} requested")
        try:
            self.broker.switch_mode(mode, bool(body.get("now")), emit, self._caller_gone)
        except DenError as e:
            if not started():
                raise
            emit({"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            log(f"mode switch to {mode} dropped: the caller hung up")
            self.close_connection = True
            return
        log(f"mode switch to {mode} done")
        self.wfile.write(b"0\r\n\r\n")

    def _unload(self, body):
        """POST /unload {"sides": ["llm", "image"], "now": false}: release the sides' GPU, RAM and
        CPU, refusing new requests while it runs, and keep the mode on."""
        emit, started = self._ndjson()
        sides = body.get("sides") or list(SIDES)
        log(f"unload of the {' and '.join(sides)} side requested{' --now' if body.get('now') else ''}")
        try:
            self.broker.unload_sides(sides, bool(body.get("now")), emit, self._caller_gone)
        except DenError as e:
            if not started():
                raise
            emit({"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            log("unload dropped: the caller hung up")
            self.close_connection = True
            return
        log("unload done")
        self.wfile.write(b"0\r\n\r\n")

    def _image(self, body):
        """POST /image {prompt, workflow?, negative?, seed?, size?, image?, out?, switch_back?,
        steps?, cfg?, sampler?, scheduler?, loras? [{name, strength?}], references? [path],
        control? {image, type, strength?, start?, end?}, upscale? {name, factor?}, preview?}.

        Streams progress lines (waiting, unloading, starting, generating, stopping) and ends
        with {"result": {...}} or {"error": "..."}. Paths must be absolute.
        """
        emit, started = self._ndjson()
        try:
            self._generate(body, emit)
        except DenError as e:
            if not started():
                raise
            emit({"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return
        self.wfile.write(b"0\r\n\r\n")

    def _generate(self, body, emit):
        config = core.load_config()
        settings = image.settings(config)
        if not settings:
            raise DenError("image generation is not configured ([image] in config.toml)")
        name = body.get("workflow") or settings.get("default_workflow")
        if not name:
            raise DenError("no workflow given and no [image] default_workflow in config.toml")
        references = body.get("references") or []
        control_image = (body.get("control") or {}).get("image")
        paths = [("image", body.get("image")), ("out", body.get("out")), ("control image", control_image)]
        for key, path in paths + [("references", r) for r in references]:
            if path and not Path(path).is_absolute():
                raise DenError(f"{key} must be an absolute path, got {path!r}")
        if body.get("control") and not control_image:
            raise DenError("control needs a guide image")
        for path in filter(None, [body.get("image"), control_image, *references]):
            if not Path(path).is_file():
                raise DenError(f"input image not found: {path}")
        prompt = body.get("prompt") or ""
        options = {key: body.get(key) for key in (*image.SETTINGS, "loras", "references", "control", "upscale")}
        graph, params, uploads = image.build(
            config, name, prompt, body.get("negative"), body.get("seed"), body.get("size"),
            edit=bool(body.get("image")), options=options,
        )

        info = {"side": "image", "caller": self._caller(), "method": "POST", "path": "/image", "model": name}
        req_id = self.broker.admit(info, emit, self._caller_gone)
        comfy = ComfyUI(settings["base_url"])
        try:
            if body.get("image"):
                image.fill_image(config, name, graph, comfy.upload(body["image"]))
            image.fill_uploads(graph, uploads, [comfy.upload(path) for _, path in uploads])
            emit({"generating": {**params, "summary": image.summary_parts(params)}})
            began = time.time()
            prompt_id = comfy.submit(graph)
            info["cancel"] = lambda: comfy.cancel(prompt_id)

            def check():
                if info.get("cancelled"):
                    raise DenError(info["cancelled"])
                if self._caller_gone():
                    comfy.cancel(prompt_id)
                    raise ConnectionResetError("the caller hung up while generating")

            outputs = comfy.wait(prompt_id, check)
            paths, copies = image.save([comfy.view(o) for o in outputs], prompt, body.get("out"))
            seconds = round(time.time() - began, 1)
            # A small copy for a caller whose model can look at the image; the saved file is
            # always the full-size PNG (ADR 0003, ADR 0004).
            shown = image.preview(comfy, outputs[0]) if body.get("preview") else None
        except (DenError, ConnectionResetError, BrokenPipeError) as e:
            log(f"#{req_id} {info['caller']} POST /image {name} -> failed: {e}")
            raise
        finally:
            self.broker.finish(req_id)
        result = {
            **params,
            "summary": image.summary_parts(params),
            "paths": [str(p) for p in paths],
            "copies": [str(p) for p in copies],
            "seconds": seconds,
            "waited_s": round(info["started"] - info.get("queued", info["started"]), 1),
            **({"preview": shown} if shown else {}),
        }
        image.log(
            {
                "caller": info["caller"],
                **params,
                "prompt": prompt,
                "negative": body.get("negative"),
                "image": body.get("image"),
                "references": references or None,
                "control_image": control_image,
                "paths": result["paths"],
                "copies": result["copies"],
                "seconds": seconds,
                "waited_s": result["waited_s"],
            }
        )
        log(f"#{req_id} {info['caller']} POST /image {name} seed {params['seed']} -> {paths[0]} in {seconds}s")
        if body.get("switch_back"):
            result["switched_back"] = self.broker.switch_back(config, emit)
        emit({"result": result})

    def _proxy(self, path):
        body = self._read_body()
        try:
            parsed = json.loads(body) if body else None
        except json.JSONDecodeError:
            parsed = None
        control = path in CONTROL_PATHS or path.startswith("/api/blobs/") or _is_unload(parsed)
        info = {
            "side": "llm",
            "caller": self._caller(),
            "method": self.command,
            "path": path,
            "model": parsed.get("model") if isinstance(parsed, dict) else None,
        }
        req_id = None if control else self.broker.admit(info, caller_gone=self._caller_gone)
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
                log(f"#{req_id} {info['caller']} {self.command} {path} {info['model']} -> {outcome} in {took:.1f}s")

    def _refresh_keep_alive(self, config, info, keep_alive):
        """Give a /v1 request's model the configured keep_alive: Ollama ignores it in /v1 bodies.

        It runs while the request still counts as in flight, so a mode switch can't unload the
        model in between and have this load it again. It's skipped when another request wants a
        different model, since loading this one back would force an extra swap.
        """
        model = info["model"]
        others = [r for r in self.broker.snapshot() if r["id"] != info["id"]]
        if not model or any(r["side"] != "llm" or r["model"] not in (None, model) for r in others):
            return
        with self.broker.cond:
            if any(w["side"] != "llm" for w in self.broker.waiting.values()):
                return  # the image side waits: the swap would unload the model right away
        try:
            Ollama(config["llm"]["base_url"]).keep_loaded(model, keep_alive)
        except DenError as e:
            log(f"#{info['id']} keep_alive refresh for {model} failed: {e}")

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
                # Also in the journal: nobody is watching this caller's error, and someone has
                # to start Ollama by hand (den runs as your user; Ollama is a system service).
                log(f"OLLAMA DOWN at {config['llm']['base_url']} ({e}): start it with: sudo systemctl start ollama")
                raise DenError(
                    f"ollama is not reachable at {config['llm']['base_url']} ({e}); "
                    "start it with: sudo systemctl start ollama"
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
    broker = Broker()
    Handler.broker = broker
    server = ThreadingHTTPServer((url.hostname, url.port), Handler)
    server.daemon_threads = True
    loaded = broker.detect_loaded()
    threading.Thread(target=broker.idle_loop, daemon=True).start()
    comfy = image.settings(config).get("base_url", "not configured")
    log(f"den broker on {url.hostname}:{url.port} -> ollama {config['llm']['base_url']}, comfyui {comfy}; loaded: {loaded or 'nothing'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
