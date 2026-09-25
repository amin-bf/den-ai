"""`den serve`: the GPU broker. Everything that uses the GPU goes through it.

Two sides share the GPU, one loaded at a time: the LLM (llama-server, a child process the broker
starts on the requested model) and image generation (ComfyUI, a systemd user service the broker
starts and stops). LLM requests (OpenAI-style /v1/…) are passed through to llama-server over its
private UNIX socket, streamed as they arrive; stopping it saves the conversation's cache, and
starting it restores it (den/llm.py, ADR 0005). Ollama is only the model store: its catalogue
and download endpoints are passed through, but it runs no models. Image
requests (POST /image) fill in a workflow, run it on ComfyUI and save the result; POST /pose
draws a photo's pose on ComfyUI and keeps it in the pose library, or, with "draw_only", only
draws it and hands it back, so a client can show it before a second call saves it.

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

Own endpoints: GET /status, POST /mode {"mode", "now"}, POST /unload {"sides", "now"},
POST /image and POST /pose (these four stream NDJSON progress lines). For a client on another
machine, which has none of den's files and names no path (ADR 0007): GET /client (mode, model,
enabled tasks, image spec), POST /delegate and POST /feedback (tasks run and logged here),
GET /poses, and GET /skills and GET /skill (den's own skills, for a client whose user loads one
into a conversation). /image and /pose take input images as bytes too, and with "bytes" answer
with the images instead of paths; a /pose with "draw_only" always answers that way with the map
it drew, since it saves no file to name.

Clips take minutes, so they are detached requests (ADR 0009): POST /clip checks the request and
answers with an id at once, the broker makes the clip on a thread of its own — waiting, swapping
and counting like any image request — and GET /clip?id=N asks for it, POST /clip/cancel {id}
drops it. The broker keeps them in memory only, a finished one for a day.

A live conversation (ADR 0011) takes the machine: POST /live/start (streams), POST /live/talk
{say, wait_s}, POST /live/stop. Standby (ADR 0012) listens for a wake word without it: POST
/standby/start {wake_word} (streams), POST /standby/wait {after, timeout_s}, POST /standby/stop,
GET /standby; its state lives in memory only.
"""

import http.client
import itertools
import json
import re
import os
import select
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from den import clip, core, image, llm, platform, poses, skills, speech
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
# Ollama's native endpoints that run a model: den runs models through llama-server now.
OLLAMA_RUN_PATHS = {"/api/generate", "/api/chat", "/api/embed", "/api/embeddings"}
# llama-server's own read-only endpoints. They describe the server that is running, so den
# answers them from it and never loads a model for them: a GET of /props used to be treated
# like any other request and swapped the GPU to the selected model.
LLM_INFO_PATHS = {"/props", "/health", "/slots", "/metrics"}
PROGRESS_INTERVAL_S = 5
WAIT_REPORT_INTERVAL_S = 30
UNLOAD_TIMEOUT_S = 60
COMFYUI_START_TIMEOUT_S = 180
COMFYUI_STOP_TIMEOUT_S = 30
IDLE_CHECK_S = 10
LIVE_IDLE_S = 600  # a live session nobody talks in for this long ends by itself (ADR 0011)
WAKE_KEEP_S = 60  # what was said after the wake word is kept this long for a conversation to start (ADR 0012)
STANDBY_WAIT_MAX_S = 600
SIDES = ("llm", "image")
DEFAULT_BATCH_SECONDS = 120
DEFAULT_BATCH_REQUESTS = 4
DETACHED_KEEP_S = 24 * 3600  # how long a finished clip's result is held for its caller


def log(text):
    print(f"{time.strftime('%H:%M:%S')} {text}", file=sys.stderr, flush=True)


def _unavailable(side, config, state):
    """Why side can't run now, or None: what's installed and selected decides, not a mode."""
    if side == "llm":
        return core.llm_unavailable(config, state)
    # The image side makes images and clips: it can run when either can.
    why = image.unavailable(config, state)
    return why if why and clip.unavailable(config, state) else None


def _time_left(seconds):
    if seconds is None:
        return "time left unknown"
    return "under a minute left" if seconds < 60 else f"about {round(seconds / 60)} min left"


def _no_emit(msg):
    pass


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
        self.last_llm = time.time()
        self.llm = llm.Server()
        self.llm_lock = threading.Lock()  # one model at a time: held while a request runs on it
        self.ids = itertools.count(1)
        self.started = time.time()
        self.detached = {}  # id -> record of a detached request (a clip), running or finished
        self.speech = speech.Server()  # started for a voice job, stopped after it (ADR 0010)
        self.speech_lock = threading.Lock()  # one voice job on it at a time: two starts would race the same process
        self.live = None  # the live conversation, while there is one: den does nothing else (ADR 0011)
        self.live_engine = speech.LiveEngine()
        self.engine_lock = threading.Lock()  # one start, stop or change of the live engine at a time
        # Standby (ADR 0012): off, starting, listening, woke, or live (paused for a conversation,
        # back after it). Every change counts up seq, which a client waits on.
        self.standby = {"state": "off", "seq": 0}
        self.standby_gen = 0  # which standby engine a watcher belongs to

    # --- admission and the swap ---

    def detect_loaded(self):
        """The side already on the GPU when the broker starts: only ComfyUI can be, since the
        broker's own llama-server ends with it. A model Ollama still holds from before is unloaded."""
        config = core.load_config()
        try:
            self._unload_ollama(config, _no_emit)
        except DenError as e:
            log(f"couldn't clear models Ollama had loaded: {e}")
        if image.settings(config) and ComfyUI(image.settings(config)["base_url"]).up():
            self.loaded = "image"
        else:
            self.loaded = None
        return self.loaded

    def _check_available(self, side, config, state):
        if self.live is not None:
            # Live mode has the machine to itself: nothing else loads until it ends.
            raise DenError("den is in a live conversation; it takes requests again when that ends (den live off)")
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
        clips = [r for r in running if r["request"] == "POST /clip"]
        if clips:
            # A clip is let finish however long it takes (ADR 0009), so say how long that is.
            left = _time_left(clips[0].get("left_s"))
            return {"reason": f"the image side is making a clip ({left}); the swap follows", "running": running}
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
        info["id"] = info.get("id") or next(self.ids)  # a detached request has its id already
        last_reason, last_report = None, 0.0
        busy_since = None
        try:
            while True:
                config = core.load_config()
                state = core.load_state(config)
                with self.cond:
                    self._check_available(side, config, state)
                    now = time.time()
                    # Loading onto a machine busy with work that isn't den's own (ADR 0004). A
                    # loaded side is den's own and a swap frees it, so only look while den holds
                    # nothing. Busy is usually a build or a test run and ends, so wait it out the
                    # way everything else here waits — but bounded, because the caller may be the
                    # one that made the machine busy, and then no wait would ever help.
                    busy = core.too_busy(config) if self.loaded is None else None
                    if busy:
                        busy_since = now if busy_since is None else busy_since
                        if now - busy_since >= core.busy_wait_s(config):
                            waited = f" after waiting {now - busy_since:.0f}s" if busy_since < now else ""
                            raise DenError(
                                f"the machine is busy: {busy}. den has nothing loaded and won't "
                                f"load the {side} side{waited} — retry when the machine settles, "
                                "or raise the limits in [limits] of config.toml"
                            )
                    if not busy and self._can_start(side, config, now):
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
                    swap = not busy and self._can_swap(side, config, now)
                    if swap:
                        self.swapping = side
                    else:
                        reason = {"reason": f"the machine is busy: {busy}"} if busy else self._wait_reason(side, config, now)
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
                with self.llm_lock:
                    self._stop_llm(emit)
                self._unload_ollama(config, emit)
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
            elif info:
                self.last_llm = time.time()
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
                conn.sock.shutdown(socket.SHUT_RDWR)  # the server stops generating once the client is gone
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

    def stop_llm_if_idle(self):
        """Stop llama-server after [llm] keep_alive without LLM requests, saving its cache."""
        config = core.load_config()
        keep_alive = core.duration_s(config["llm"].get("keep_alive", "30m"))
        with self.cond:
            if (
                self.loaded != "llm"
                or self.swapping is not None
                or time.time() - self.last_llm < keep_alive
                or any(i["side"] == "llm" for i in [*self.inflight.values(), *self.waiting.values()])
            ):
                return
            self.swapping = "idle"
        try:
            log(f"the llm side was idle for {keep_alive:.0f}s; stopping llama-server")
            with self.llm_lock:
                self._stop_llm(_no_emit)
            with self.cond:
                self.loaded = None
        finally:
            with self.cond:
                self.swapping = None
                self.cond.notify_all()

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

    # --- live conversation (ADR 0011) ---

    def live_start(self, voice, language, exaggeration, emit, caller_gone):
        """Take the machine for a live conversation: finish what runs, free both sides, load the
        live engine. Every other request is refused from the first moment. Returns the session id."""
        voice_file = speech.voice_path(voice)
        with self.cond:
            if self.live is not None:
                raise DenError("a live conversation is already on (den live off ends it)")
            if self.releasing or self.pending_mode:
                raise DenError("den is releasing or switching mode; retry once it's done")
            woke = self.standby["state"] == "woke"
            self.live = {"state": "starting", "voice": voice, "language": language, "last": time.time(), "talking": False,
                         "woke": woke}
            if self.standby["state"] != "off":
                # Standby pauses for the conversation and comes back after it; the engine keeps
                # recording meanwhile, so what the user says while the machine is freed isn't lost.
                self._standby_set("live")
            self.cond.notify_all()  # queued requests give up: den is going live
        try:
            self._drain(SIDES, False, emit, caller_gone, command="unload")
            config = core.load_config()
            with self.llm_lock:
                self._stop_llm(emit)
            self._unload_ollama(config, emit)
            self._stop_comfyui(config, emit)
            with self.cond:
                self.loaded, self.swapping = None, None
                self.cond.notify_all()
            with self.engine_lock:
                self.live_engine.start(voice_file, language, exaggeration, emit)
        except BaseException:
            with self.engine_lock:
                self.live_engine.stop()
            with self.cond:
                self.live = None
                self.swapping = None
                self.cond.notify_all()
            threading.Thread(target=self._standby_resume, daemon=True).start()
            raise
        session = speech.new_session()
        with self.cond:
            self.live.update(state="on", session=session, since=time.time(), last=time.time())
        speech.record_turn(session, {"who": "den", "event": "start", "voice": voice, "language": language,
                                     **({"woke": True} if woke else {})})
        log(f"live conversation {session} on (voice {voice or 'default'}, {language}{', after the wake word' if woke else ''})")
        return session

    def live_talk(self, say, wait_s, voice=None, language=None):
        voice_file = speech.voice_path(voice) if voice else None
        if language and language not in speech.LANGUAGES:
            raise DenError(f"unknown language {language!r}; one of: {', '.join(speech.LANGUAGES)}")
        with self.cond:
            if not self.live or self.live.get("state") != "on":
                raise DenError("no live conversation is on (live_start begins one)")
            if self.live["talking"]:
                raise DenError("a talk is already running in this conversation")
            self.live["talking"] = True
            session = self.live["session"]
            switched = {k: v for k, v in (("voice", voice), ("language", language)) if v and v != self.live.get(k)}
            self.live.update(switched)
        if switched:
            speech.record_turn(session, {"who": "den", "event": "switch", **switched})
        began = round(time.time(), 1)  # when Claude's line starts, not when the user's answer ends
        try:
            answer = self.live_engine.talk(say, wait_s, voice_file, language)
        finally:
            with self.cond:
                if self.live:
                    self.live["talking"] = False
                    self.live["last"] = time.time()
        if say:
            speech.record_turn(session, {
                "t": began, "who": "claude", "spoken": answer.get("spoken") or [], "unspoken": answer.get("unspoken") or [],
                "interrupted": answer.get("interrupted", False), "said_first": answer.get("said_first", False),
            })
        if answer.get("heard"):
            speech.record_turn(session, {"who": "user", "text": answer["heard"]})
        return {**answer, "session": session}

    def live_stop(self, reason="asked"):
        with self.cond:
            live, self.live = self.live, None
            self.cond.notify_all()
        with self.engine_lock:
            self.live_engine.stop()
        # Standby comes back in the background: the transcript needn't wait for its detector.
        threading.Thread(target=self._standby_resume, daemon=True).start()
        if not live or not live.get("session"):
            raise DenError("no live conversation is on")
        speech.record_turn(live["session"], {"who": "den", "event": "stop", "reason": reason})
        log(f"live conversation {live['session']} off ({reason})")
        return {"session": live["session"], "transcript": speech.transcript(live["session"]),
                "minutes": round((time.time() - live.get("since", time.time())) / 60, 1)}

    def stop_live_if_idle(self):
        with self.cond:
            live = self.live
            idle = live and live.get("state") == "on" and not live["talking"] and time.time() - live["last"] > LIVE_IDLE_S
        if idle:
            self.live_stop(reason=f"nobody talked for {LIVE_IDLE_S // 60} minutes")

    # --- standby (ADR 0012) ---

    def _standby_set(self, state, **extra):
        """Change standby's state and count it, so a client waiting on it learns. Takes self.cond
        (re-entrant: callers may hold it)."""
        with self.cond:
            keep = {k: self.standby[k] for k in ("wake_word", "since") if k in self.standby}
            self.standby = {**keep, "state": state, "seq": self.standby["seq"] + 1, "changed": time.time(), **extra}
            self.cond.notify_all()
        log(f"standby: {state}" + (f" ({extra['reason']})" if extra.get("reason") else ""))

    def standby_view(self):
        with self.cond:
            return {k: v for k, v in self.standby.items() if k != "changed"}

    def standby_start(self, wake_word, emit):
        """Listen for the wake word: the microphone, the VAD and a small detector on the CPU, and
        no side, so images, clips and the LLM keep working. Again while on: the wake word is
        replaced and what was said since it was heard is forgotten."""
        wake_word = " ".join(str(wake_word or "").split())
        if not re.search(r"\w", wake_word) or len(wake_word) > 60:
            raise DenError("a wake word is a short phrase, such as \"hey Elli\"")
        config = core.load_config()
        if core.load_state(config)["mode"] == "off" or self.pending_mode == "off":
            raise DenError("den is off; standby needs: den mode on")
        why = speech.unavailable()
        if why:
            raise DenError(why)
        with self.cond:
            if self.live is not None:
                # It begins when the conversation ends, as standby paused for it would.
                self.standby["wake_word"] = wake_word
                self._standby_set("live", since=time.time())
                return self.standby_view()
        with self.engine_lock:
            if self.live_engine.phase() == "standby":
                self.live_engine.rearm(wake_word)
                with self.cond:
                    self.standby["wake_word"] = wake_word
                self._standby_set("listening")
                return self.standby_view()
            with self.cond:
                self.standby["wake_word"] = wake_word
            self._standby_set("starting", since=time.time())
            try:
                self.live_engine.start_standby(wake_word, emit)
            except BaseException as e:
                with self.cond:
                    if self.standby["state"] == "starting":
                        self._standby_set("off", reason=str(e) if isinstance(e, DenError) else "standby didn't start")
                raise
            self._standby_watch()
        return self.standby_view()

    def _standby_watch(self):
        """Listening from now on, and a watcher that turns the engine's wake word into a state.
        Only from starting: a conversation or a stop that came while the detector loaded wins."""
        with self.cond:
            if self.standby["state"] != "starting":
                return
            self.standby_gen += 1
            gen = self.standby_gen
            self._standby_set("listening")
        threading.Thread(target=self._standby_watcher, args=(gen,), daemon=True).start()

    def _standby_watcher(self, gen):
        seen = 0
        while True:
            with self.cond:
                current = self.standby_gen == gen and self.standby["state"] in ("listening", "woke")
                woke_at = self.standby.get("woke_at") if self.standby["state"] == "woke" else None
            if not current:
                return
            if woke_at:
                # Heard, and waiting for a conversation to start. Past WAKE_KEEP_S what was said
                # is forgotten and the wake word listened for again.
                if time.time() - woke_at < WAKE_KEEP_S:
                    with self.cond:
                        self.cond.wait(1)
                    continue
                with self.engine_lock:
                    with self.cond:
                        still = self.standby_gen == gen and self.standby["state"] == "woke" and self.live is None
                    if still:
                        try:
                            self.live_engine.rearm()
                        except (DenError, OSError) as e:
                            self._standby_set("off", reason=f"standby stopped: {e}")
                            return
                        self._standby_set("listening", reason=f"no conversation started within {WAKE_KEEP_S}s")
                continue
            try:
                wakes = self.live_engine.wait_wake(seen, 5)
            except (OSError, json.JSONDecodeError):
                with self.cond:
                    current = self.standby_gen == gen and self.standby["state"] in ("listening", "woke")
                if current and not self.live_engine.running():
                    self._standby_set("off", reason=f"the wake word detector exited; see {speech.LIVE_LOG}")
                    return
                time.sleep(1)
                continue
            if wakes > seen:
                seen = wakes
                with self.cond:
                    if self.standby_gen == gen and self.standby["state"] == "listening":
                        self._standby_set("woke", woke_at=time.time())

    def _standby_resume(self):
        """Standby back after a conversation it paused for, with a fresh detector."""
        with self.cond:
            if self.standby["state"] != "live" or self.live is not None:
                return
            wake_word = self.standby.get("wake_word")
        with self.engine_lock:
            with self.cond:
                if self.standby["state"] != "live" or self.live is not None:
                    return
            self._standby_set("starting")
            try:
                self.live_engine.start_standby(wake_word, _no_emit)
            except (DenError, OSError) as e:
                with self.cond:
                    if self.standby["state"] == "starting":
                        self._standby_set("off", reason=f"standby didn't come back: {e}")
                return
            self._standby_watch()

    def standby_stop(self, reason="asked"):
        """Stop listening for the wake word. A conversation that is on goes on; standby just won't
        come back after it."""
        with self.cond:
            was = self.standby["state"]
            if was != "off":
                self._standby_set("off", reason=reason)
        if was in ("starting", "listening", "woke"):
            with self.engine_lock:
                if self.live is None and self.live_engine.phase() in ("standby", None):
                    self.live_engine.stop()
        return self.standby_view()

    def standby_wait(self, after, timeout_s):
        """Standby's state once it has changed since seq `after`, or at the timeout: a client
        learns this way that the wake word was heard, that a conversation paused it, that it's
        back, or that it ended (and why)."""
        deadline = time.time() + max(0.0, min(float(timeout_s), STANDBY_WAIT_MAX_S))
        with self.cond:
            while self.standby["seq"] <= after and time.time() < deadline:
                self.cond.wait(deadline - time.time())
            return self.standby_view()

    def idle_loop(self):
        while True:
            time.sleep(IDLE_CHECK_S)
            try:
                self.stop_live_if_idle()
                self.stop_if_idle()
                self.stop_llm_if_idle()
            except Exception as e:  # keep checking: one failed stop mustn't end the timeout
                log(f"idle check failed: {e!r}")

    def drop_dead_llm(self):
        """Stop llama-server when its compute backend has died, so the next request starts one.

        A backend that failed unrecoverably takes the server with it without stopping it: it
        keeps listening and answers everything with an error, so neither the broker nor a
        caller can tell it apart from a bad request. Stopping it here is enough — the next
        request loads the model again. The cache isn't saved; it belongs to the context that
        failed.
        """
        if not self.llm.running() or not self.llm.backend_dead():
            return
        log(f"llama-server's compute backend failed; stopping it, see {llm.LOG_PATH}")
        self.llm.stop(_no_emit, save=False)

    # --- the sides' processes ---

    def _start_comfyui(self, config, emit):
        settings = image.settings(config)
        comfy = ComfyUI(settings["base_url"])
        if comfy.up():
            return
        service = settings.get("service", "comfyui")
        emit({"starting": service})
        platform.start_service(service)
        deadline = time.time() + COMFYUI_START_TIMEOUT_S
        while not comfy.up():
            if time.time() > deadline:
                raise DenError(
                    f"ComfyUI didn't answer at {comfy.base_url} {COMFYUI_START_TIMEOUT_S}s after starting {service}; "
                    f"{platform.logs_hint(service)}"
                )
            try:
                platform.check_running(service)
            except DenError as e:
                raise DenError(f"{service} stopped while starting; {platform.logs_hint(service)}") from e
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
        platform.stop_service(service)
        deadline = time.time() + COMFYUI_STOP_TIMEOUT_S
        while comfy.up():
            if time.time() > deadline:
                raise DenError(
                    f"ComfyUI still answers at {comfy.base_url} after stopping {service}; "
                    "was it started by hand? Stop it, then retry"
                )
            time.sleep(0.5)

    def _stop_llm(self, emit):
        """Save llama-server's cache and stop it; returns the model it had, as a list (empty if none)."""
        model = self.llm.stop(emit)
        return [model] if model else []

    def ensure_llm(self, config, model):
        """Have llama-server running with model: start it, or save and restart it with another model.
        Called with llm_lock held."""
        if self.llm.running() and self.llm.model == model:
            return
        if self.llm.running():
            log(f"switching llama-server from {self.llm.model} to {model}")
            self.llm.stop(_no_emit)
        took = self.llm.start(config, model, _no_emit)
        log(f"llama-server ready with {model} in {took:.1f}s")

    def _unload_ollama(self, config, emit):
        """Unload anything Ollama itself has loaded (a client that went to it directly), and wait
        until it no longer lists it. den runs no models there, so this is usually a no-op.

        Ollama answers the unload at once but frees the memory only when the model's last
        request ends, so /api/ps is the signal that the GPU is free.
        """
        ollama = Ollama(config["llm"]["base_url"])
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
                    **({"left_s": _left(i, now)} if "estimate" in i else {}),
                }
                for i in sorted(requests, key=lambda i: i["id"])
            ]

    # --- detached requests: clips (ADR 0009) ---

    def detach(self, record):
        with self.cond:
            self._prune()
            self.detached[record["id"]] = record

    def detached_record(self, req_id):
        with self.cond:
            self._prune()
            return self.detached.get(req_id)

    def detached_views(self):
        with self.cond:
            self._prune()
            return [clip_view(r) for r in sorted(self.detached.values(), key=lambda r: r["id"])]

    def _prune(self):
        now = time.time()
        for req_id in [i for i, r in self.detached.items() if r.get("finished") and now - r["finished"] > DETACHED_KEEP_S]:
            del self.detached[req_id]

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
            "llm_model": state["llm_model"],
            "num_ctx": config["llm"].get("num_ctx", 8192),
            "pending_mode": self.pending_mode,
            "releasing": list(self.releasing) or None,
            "live": {k: v for k, v in self.live.items() if k in ("state", "session", "voice", "language", "since", "woke")} if self.live else None,
            "standby": self.standby_view(),
            "pressure": pressure,
            # Set while a side that isn't loaded would have to wait for the machine to settle.
            "too_busy": core.too_busy(config, pressure),
            "unavailable": {side: _unavailable(side, config, state) for side in SIDES},
            "loaded": self.loaded,
            "loaded_s": round(time.time() - self.loaded_since),
            "swapping": self.swapping,
            "inflight": self.snapshot(),
            "waiting": self.snapshot(self.waiting),
            # Always there, even empty: a client checks for it before its first clip, since an
            # older broker would take POST /clip for an LLM request (ADR 0009).
            "clips": self.detached_views(),
            "llm": {
                "server": "llama-server",
                "model": self.llm.model if self.llm.running() else None,
                "pid": self.llm.proc.pid if self.llm.running() else None,
                "idle_s": round(time.time() - self.last_llm) if self.loaded == "llm" else None,
            },
            "ollama": upstream,  # the model store
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
        if mode == "off":
            self.standby_stop(reason="den was turned off")
        try:
            off = list(SIDES) if mode == "off" else []
            self._drain(off, now, emit, caller_gone)
            unloaded = []
            try:
                if "llm" in off:
                    with self.llm_lock:
                        unloaded += self._stop_llm(emit)
                    unloaded += self._unload_ollama(config, emit)
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
                    with self.llm_lock:
                        unloaded += self._stop_llm(emit)
                    unloaded += self._unload_ollama(config, emit)
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


def _left(info, now):
    """Seconds a running clip has left by its estimate, or None without one or before it runs."""
    if info.get("estimate") is None or "started" not in info:
        return None
    return max(0, round(info["estimate"] - (now - info["started"])))


def clip_view(record, with_bytes=False):
    """What a caller sees of a detached clip: its state, where it is, and the result once done.
    With with_bytes the clip and its contact sheet come as bytes instead of paths (ADR 0007)."""
    now = time.time()
    info = record["info"]
    view = {
        "id": record["id"],
        "state": record["state"],
        "caller": info["caller"],
        "workflow": record["params"]["workflow"],
        "prompt": record["prompt"],
        "summary": clip.summary_parts(record["params"]),
        "estimate_s": info.get("estimate"),
        "age_s": round(now - record["created"]),
    }
    if record["state"] == "waiting" and record.get("progress"):
        view["progress"] = record["progress"]
    if record["state"] == "running":
        view["running_s"] = round(now - info["started"])
        view["left_s"] = _left(info, now)
    if record.get("error"):
        view["error"] = record["error"]
    result = record.get("result")
    if result:
        result = dict(result)
        if with_bytes:
            result["clip"] = image.as_bytes(result["path"])
            result["suffix"] = Path(result["path"]).suffix  # the container, for saving it there
            if result.get("sheet"):
                result["sheet"] = image.as_bytes(result["sheet"])
            for key in ("path", "copies"):
                result.pop(key, None)
        view["result"] = result
    return view


def voice_request(spec, folder):
    """A voice job's script, language, voice and settings from a request: {srt? | lines? | text?,
    voice?, language?, exaggeration?, cfg_weight?, temperature?, seed?, sync?}. With `audio` (a
    recording's path, or bytes) the recording is the track and a script only names its lines. A
    voice or a recording sent as bytes is written into folder."""
    why = speech.unavailable()
    if why:
        raise DenError(why)
    if not isinstance(spec, dict):
        raise DenError("a voice-over is {srt or text, voice?, language?, ...}")
    audio = spec.get("audio")
    if image.is_file_object(audio):
        audio = image._write_input(folder, audio)
    elif audio and not (Path(audio).is_absolute() and Path(audio).is_file()):
        raise DenError(f"no recording at {audio} (an absolute path)")
    if audio and not any(spec.get(k) for k in ("srt", "text", "lines")):
        cues = [{"start": 0.0, "end": None, "text": "(recording)"}]
    else:
        cues = speech.script(spec.get("srt"), spec.get("text"), spec.get("lines"))
    language = str(spec.get("language") or "en").lower()
    if language not in speech.LANGUAGES:
        raise DenError(f"unknown language {language!r}; one of: {', '.join(speech.LANGUAGES)}")
    voice = spec.get("voice")
    if image.is_file_object(voice):
        voice_file, voice = image._write_input(folder, voice), voice.get("name") or "recording"
    else:
        voice_file = speech.voice_path(voice)
    options = {k: spec[k] for k in ("exaggeration", "cfg_weight", "temperature", "seed") if spec.get(k) is not None}
    return {
        "cues": cues, "language": language, "voice": voice, "voice_file": voice_file, "options": options,
        "sync": bool(spec.get("sync")), "audio": audio,
    }


def _free_comfy_for_speech(comfy):
    """Unload ComfyUI's models, then give the driver a moment to actually reclaim their VRAM
    before the speech model tries to load into it (llm.Server._await_vram has the same race,
    on the LLM side after a swap)."""
    if comfy and comfy.up():
        before = platform.free_vram_mb()
        comfy.free()
        if before is not None:
            deadline = time.time() + llm.VRAM_SETTLE_TIMEOUT_S
            while time.time() < deadline:
                after = platform.free_vram_mb()
                if after is None or after >= before + llm.VRAM_SETTLE_MIN_RISE_MB:
                    return
                time.sleep(llm.VRAM_SETTLE_POLL_S)


def voice_track(broker, config, request, emit, check):
    """(track WAV, seconds, notes) of a voice request: its lines spoken, or its recording as it is."""
    if not request.get("audio"):
        return speech.assemble(request["cues"], speak_lines(broker, config, request, emit, check))
    settings = image.settings(config)
    comfy = ComfyUI(settings["base_url"]) if settings else None
    # One voice job on the speech server at a time: two starts would race the same process
    # (den has seen a second job's start() see the first's process and call itself ready,
    # then speak into a model that hadn't loaded yet, or into one the first job just stopped).
    with broker.speech_lock:
        _free_comfy_for_speech(comfy)
        try:
            broker.speech.start(emit)
            check()
            track = broker.speech.convert(request["audio"])
        finally:
            broker.speech.stop()
    samples, rate = speech._pcm(track)
    return track, round(len(samples) / rate, 2), []


def transcribe(broker, config, recording, language, emit, check):
    """Whisper's timed segments of a recording, as cues."""
    settings = image.settings(config)
    comfy = ComfyUI(settings["base_url"]) if settings else None
    with broker.speech_lock:
        _free_comfy_for_speech(comfy)
        try:
            broker.speech.start(emit)
            check()
            emit({"transcribing": Path(recording).name})
            return broker.speech.transcribe(recording, language)
        finally:
            broker.speech.stop()


def speak_lines(broker, config, request, emit, check):
    """Speak each line of a voice request on the speech server, which starts once ComfyUI has
    freed its models (they don't fit on the GPU together) and stops after. Returns the WAVs.
    One job runs on the speech server at a time (broker.speech_lock): a second /voice call
    queues here rather than racing the first's start/speak/stop."""
    settings = image.settings(config)
    comfy = ComfyUI(settings["base_url"]) if settings else None
    with broker.speech_lock:
        _free_comfy_for_speech(comfy)
        cues, lines = request["cues"], []
        try:
            broker.speech.start(emit)
            for i, cue in enumerate(cues, 1):
                check()
                emit({"speaking": {"line": i, "of": len(cues), "text": cue["text"][:60]}})
                lines.append(broker.speech.speak(cue["text"], request["language"], request["voice_file"], request["options"]))
        finally:
            broker.speech.stop()
        return lines


def run_clip(broker, record, graph, uploads, sheet, body, folder):
    """Make a detached clip: wait for the image side like any request, run the graph, save the
    clip and its contact sheet. Runs on its own thread; what happens goes into the record."""
    info = record["info"]
    req_id = record["id"]
    prompt = record["prompt"]

    def emit(msg):
        record["progress"] = msg

    admitted = False
    try:
        try:
            broker.admit(info, emit, lambda: bool(info.get("cancelled")))
        except ConnectionResetError:
            raise DenError(info.get("cancelled") or "cancelled") from None
        admitted = True
        if info.get("cancelled"):
            raise DenError(info["cancelled"])
        record["state"] = "running"
        config = core.load_config()
        comfy = ComfyUI(image.settings(config)["base_url"])
        voiced = record.get("voiceover")
        if voiced:
            # The voice first: the speech model and the clip's don't share the GPU.
            def cancelled():
                if info.get("cancelled"):
                    raise DenError(info["cancelled"])

            track, length, notes = voice_track(broker, config, voiced, emit, cancelled)
            track_path, _ = speech.save(track, " ".join(c["text"] for c in voiced["cues"]))
            track_path.with_suffix(".srt").write_text(speech.to_srt(voiced["cues"]))
            wf = clip.check_workflows(config)[record["params"]["workflow"]][0]
            planned = record["params"]["duration"]
            longest = (wf["duration"].get("allowed") or [None, planned])[1]
            if length > planned + 0.1 and longest > planned:
                # The voice came out longer than the script's times: make the clip long enough to hold it.
                graph, params, uploads, sheet = clip.build(config, duration=min(length + 0.3, longest), **voiced["build"])
                kept = {k: record["params"][k] for k in ("voiceover", "lip_sync") if k in record["params"]}
                record["params"] = {**params, **kept}
                notes.append(f"the clip was lengthened to {record['params']['duration']:g}s to hold the voice-over")
            if length > record["params"]["duration"] + 0.1:
                notes.append(f"the voice-over runs {length:g}s; the clip ends at {record['params']['duration']:g}s")
            if voiced["sync"]:
                # The encoded voice must be as long as the clip's audio: pad it with silence.
                synced = Path(folder) / f"{track_path.stem}-synced.wav"
                synced.write_bytes(speech.pad(track, record["params"]["duration"] + 0.1))
                clip.add_lipsync(graph, wf, comfy.upload(synced, subfolder=""), record["params"]["duration"])
            else:
                clip.add_voiceover(graph, wf, comfy.upload(track_path, subfolder=""), record["params"]["duration"])
            voiced.update(track=str(track_path), notes=notes)
        image.fill_uploads(graph, uploads, [comfy.upload(path) for _, path in uploads])
        began = time.time()
        prompt_id = comfy.submit(graph)
        info["cancel"] = lambda: comfy.cancel(prompt_id)

        def check():
            if info.get("cancelled"):
                raise DenError(info["cancelled"])

        outputs = comfy.wait(prompt_id, check)
        made = [o for o in outputs if o["node"] != sheet and o.get("type") == "output"]
        if not made:
            raise DenError("comfyui finished without a clip")
        drawn = next((o for o in outputs if o["node"] == sheet), None)
        path, sheet_path, copies = clip.save(
            comfy.view(made[0]),
            Path(made[0]["filename"]).suffix or ".mp4",
            comfy.view(drawn) if drawn else None,
            prompt,
            body.get("out"),
        )
        seconds = round(time.time() - began, 1)
        # A small copy of the contact sheet for a caller whose model can look at it (ADR 0004).
        shown = image.preview(comfy, drawn) if body.get("preview") and drawn else None
        params = record["params"]
        waited = round(info["started"] - info.get("queued", info["started"]), 1)
        # Whether the saved file has a sound track, as opposed to whether one was asked for.
        if params.get("sound") and not clip.has_sound(path):
            params = {**params, "sound": False}
        record["result"] = {
            **params,
            "summary": clip.summary_parts(params),
            **(
                {"voiceover_track": voiced["track"], "srt": speech.to_srt(voiced["cues"]), "notes": voiced["notes"]}
                if voiced
                else {}
            ),
            "path": str(path),
            "sheet": str(sheet_path) if sheet_path else None,
            "copies": [str(p) for p in copies],
            "seconds": seconds,
            "waited_s": waited,
            **({"preview": shown} if shown else {}),
        }
        clip.log(
            {
                "caller": info["caller"],
                **params,
                "prompt": prompt,
                "negative": body.get("negative"),
                "keyframe_images": [k["image"] for k in body.get("keyframes") or []] or None,
                "path": str(path),
                "sheet": str(sheet_path) if sheet_path else None,
                "copies": [str(p) for p in copies],
                "seconds": seconds,
                "waited_s": waited,
            }
        )
        record["state"] = "done"
        log(f"#{req_id} {info['caller']} POST /clip {params['workflow']} seed {params['seed']} -> {path} in {seconds}s")
    except DenError as e:
        record["state"] = "cancelled" if info.get("cancelled") else "failed"
        record["error"] = str(e)
        log(f"#{req_id} {info['caller']} POST /clip -> {record['state']}: {e}")
    except Exception as e:  # a thread of its own: nothing above it would say what went wrong
        record["state"], record["error"] = "failed", f"internal error: {e!r}"
        log(f"#{req_id} {info['caller']} POST /clip -> internal error: {e!r}")
    finally:
        if admitted:
            broker.finish(req_id)
        record["finished"] = time.time()
        shutil.rmtree(folder, ignore_errors=True)


def _other(side):
    return "image" if side == "llm" else "llm"


def _keypoints(texts):
    """The joints PreviewAny returned as text, parsed; None when there are none to keep."""
    for text in texts or []:
        try:
            return json.loads(text)
        except (TypeError, json.JSONDecodeError):
            log(f"pose keypoints came back unreadable: {str(text)[:80]!r}")
    return None


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
            elif path == "/voice" and self.command == "POST":
                self._stream_request(self._with_voice(self._voice), json.loads(self._read_body() or b"{}"))
            elif path == "/transcribe" and self.command == "POST":
                self._stream_request(self._transcribe, json.loads(self._read_body() or b"{}"))
            elif path == "/live/start" and self.command == "POST":
                self._stream_request(self._live_start, json.loads(self._read_body() or b"{}"))
            elif path == "/live/talk" and self.command == "POST":
                body = json.loads(self._read_body() or b"{}")
                self._send_json(200, self.broker.live_talk(
                    str(body.get("say") or ""), float(body.get("wait_s") or 120), body.get("voice"), body.get("language")
                ))
            elif path == "/live/stop" and self.command == "POST":
                self._send_json(200, self.broker.live_stop())
            elif path == "/standby/start" and self.command == "POST":
                self._stream_request(self._standby_start, json.loads(self._read_body() or b"{}"))
            elif path == "/standby/stop" and self.command == "POST":
                self._send_json(200, self.broker.standby_stop())
            elif path == "/standby/wait" and self.command == "POST":
                body = json.loads(self._read_body() or b"{}")
                self._send_json(200, self.broker.standby_wait(int(body.get("after") or 0), float(body.get("timeout_s", 60))))
            elif path == "/standby" and self.command == "GET":
                self._send_json(200, self.broker.standby_view())
            elif path == "/conversations" and self.command == "GET":
                ref = parse_qs(urlsplit(self.path).query).get("id", [None])[0]
                self._send_json(200, speech.read_conversation(ref) if ref else {"conversations": speech.conversations()})
            elif path == "/conversations" and self.command == "POST":
                # Deleting one is the user's decision; a summary is saved beside the conversation.
                body = json.loads(self._read_body() or b"{}")
                if body.get("remove"):
                    speech.delete_conversation(body.get("id"))
                    log(f"{self._caller()} POST /conversations -> deleted {body.get('id')}")
                    self._send_json(200, {"deleted": body.get("id")})
                elif body.get("summary"):
                    speech.save_summary(body.get("id"), str(body["summary"]))
                    self._send_json(200, {"summarized": body.get("id")})
                else:
                    raise DenError("POST /conversations takes {id, remove: true} or {id, summary}")
            elif path == "/live/keep" and self.command == "POST":
                body = json.loads(self._read_body() or b"{}")
                kept = speech.keep_session(str(body.get("session") or ""), str(body.get("name") or ""))
                self._send_json(200, {"kept": str(kept)})
            elif path == "/live/drop" and self.command == "POST":
                body = json.loads(self._read_body() or b"{}")
                speech.drop_session(str(body.get("session") or ""))
                self._send_json(200, {"dropped": body.get("session")})
            elif path == "/live" and self.command == "GET":
                session = parse_qs(urlsplit(self.path).query).get("session", [None])[0]
                self._send_json(200, {"session": session, "transcript": speech.transcript(session)} if session else {"live": self.broker.status()["live"]})
            elif path == "/voices/design" and self.command == "POST":
                self._stream_request(self._voice_design, json.loads(self._read_body() or b"{}"))
            elif path == "/voices" and self.command == "GET":
                query = parse_qs(urlsplit(self.path).query)
                name = query.get("name", [None])[0]
                self._send_json(200, self._voice_one(name, bool(query.get("bytes"))) if name else self._voices())
            elif path == "/voices" and self.command == "POST":
                self._send_json(200, self._voice_add(json.loads(self._read_body() or b"{}")))
            elif path == "/pose" and self.command == "POST":
                self._pose(json.loads(self._read_body() or b"{}"))
            elif path == "/clip" and self.command == "POST":
                self._send_json(200, self._clip(json.loads(self._read_body() or b"{}")))
            elif path == "/clip" and self.command == "GET":
                self._clip_get(parse_qs(urlsplit(self.path).query))
            elif path == "/clip/cancel" and self.command == "POST":
                self._clip_cancel(json.loads(self._read_body() or b"{}"))
            elif path == "/client" and self.command == "GET":
                self._send_json(200, self._client_info())
            elif path == "/delegate" and self.command == "POST":
                self._send_json(200, self._delegate(json.loads(self._read_body() or b"{}")))
            elif path == "/feedback" and self.command == "POST":
                body = json.loads(self._read_body() or b"{}")
                core.record_feedback(body.get("id"), body.get("verdict"), body.get("note") or "")
                self._send_json(200, {"recorded": body.get("id")})
            elif path == "/skills" and self.command == "GET":
                self._send_json(200, {"skills": skills.listing()})
            elif path == "/skill" and self.command == "GET":
                query = parse_qs(urlsplit(self.path).query)
                self._send_json(200, skills.get(query.get("name", [""])[0], query.get("reference", [None])[0]))
            elif path == "/poses" and self.command == "POST":
                # A person deleting a pose from a client (the phone); no tool deletes one.
                body = json.loads(self._read_body() or b"{}")
                if not body.get("remove"):
                    raise DenError("POST /poses takes {name, remove: true}")
                poses.remove(str(body.get("name") or ""))
                log(f"{self._caller()} POST /poses -> removed {body.get('name')}")
                self._send_json(200, {"removed": body.get("name"), "names": poses.names()})
            elif path == "/poses" and self.command == "GET":
                # For a client on another machine: names, descriptions and small copies, no files.
                name = parse_qs(urlsplit(self.path).query).get("name", [None])[0]
                self._send_json(
                    200, poses.show(name, files=False) if name else {"toc": poses.toc(files=False), "names": poses.names()}
                )
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

    # --- for a client on another machine (ADR 0007) ---

    def _client_info(self):
        """What a client that has none of den's files needs to offer this den as its own."""
        config = core.load_config()
        state = core.load_state(config)
        unavailable = core.llm_unavailable(config, state)
        tasks = core.enabled_tasks(config, state) if unavailable is None else {}
        return {
            "mode": state["mode"],
            "llm": {"model": state["llm_model"], "unavailable": unavailable},
            "tasks": {name: {"description": task["description"]} for name, task in tasks.items()},
            "image": {**image.client_spec(config, state), "listing": image.listing(config, folders=False)},
            "clip": {**clip.client_spec(config, state), "listing": clip.listing(config, folders=False)},
            # Present only where speech runs: a client offers voice-overs when it's there (ADR 0010).
            **({"voice": {**speech.request_spec(), **self._voices()}} if speech.unavailable() is None else {}),
        }

    def _delegate(self, body):
        """POST /delegate {task, instructions, text, files: [{path, content}]}: run the task
        with this machine's tasks and model, and log it here. The files were read by the client."""
        config = core.load_config()
        state = core.load_state(config)
        answer, stats = core.run_task(
            config, state, body.get("task"), body.get("instructions") or "", body.get("text"),
            caller=self._caller(), contents=body.get("files") or (),
        )
        return {"answer": answer, "stats": stats, "id": core.log_delegation(stats)}

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
        steps?, cfg?, sampler?, scheduler?, loras? [{name, strength?}], references? [path or
        type:path], control? {image, type, strength?, start?, end?}, upscale? {name, factor?},
        save_maps?, preview?}.

        Streams progress lines (waiting, unloading, starting, generating, stopping) and ends
        with {"result": {...}} or {"error": "..."}. Paths must be absolute.
        """
        self._stream_request(self._with_inputs(self._generate), body)

    # --- voice-overs (ADR 0010) ---

    def _with_voice(self, run):
        """run(body, emit) with a voice recording sent as bytes written to a private folder."""

        def wrapped(body, emit):
            folder = tempfile.mkdtemp(prefix="den-voice-")
            try:
                if image.is_file_object(body.get("voice")):
                    body = {**body, "voice": image._write_input(folder, body["voice"])}
                run(body, emit)
            finally:
                shutil.rmtree(folder, ignore_errors=True)

        return wrapped

    def _transcribe(self, body, emit):
        """POST /transcribe {audio: path or {name, base64}, language?, out?}: the speech of a
        recording as a timed SRT (Whisper), saved next to where voice tracks go. Streams progress
        and ends with {"result": {srt, text, segments, duration, path}}."""
        why = speech.unavailable()
        if why:
            raise DenError(why)
        folder = tempfile.mkdtemp(prefix="den-voice-")
        try:
            recording = body.get("audio")
            if image.is_file_object(recording):
                recording = image._write_input(folder, recording)
            if not recording or not Path(recording).is_absolute() or not Path(recording).is_file():
                raise DenError(f"no recording at {recording} (an absolute path, or bytes)")
            language = body.get("language")
            config = core.load_config()
            info = {"side": "image", "caller": self._caller(), "method": "POST", "path": "/transcribe", "model": "whisper"}
            req_id = self.broker.admit(info, emit, self._caller_gone)
            began = time.time()

            def check():
                if info.get("cancelled"):
                    raise DenError(info["cancelled"])
                if self._caller_gone():
                    raise ConnectionResetError("the caller hung up while transcribing")

            try:
                heard = transcribe(self.broker, config, recording, language, emit, check)
            finally:
                self.broker.finish(req_id)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        cues = heard["segments"]
        if not cues:
            raise DenError("no speech heard in the recording")
        srt = speech.to_srt(cues)
        path = speech.OUTPUT_DIR / time.strftime("%Y-%m-%d") / f"{time.strftime('%H%M%S')}-{speech.slug(heard['text'])}.srt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(srt)
        seconds = round(time.time() - began, 1)
        log(f"#{req_id} {info['caller']} POST /transcribe {len(cues)} segment(s) -> {path} in {seconds}s")
        emit({"result": {"srt": srt, "text": heard["text"], "segments": cues, "duration": heard["duration"], "path": str(path), "seconds": seconds}})

    def _live_start(self, body, emit):
        """POST /live/start {voice?, language?, exaggeration?}: take the machine for a live
        conversation (ADR 0011). Streams progress; ends with {"result": {session}}."""
        language = str(body.get("language") or "en").lower()
        if language not in speech.LANGUAGES:
            raise DenError(f"unknown language {language!r}; one of: {', '.join(speech.LANGUAGES)}")
        session = self.broker.live_start(body.get("voice"), language, float(body.get("exaggeration") or 0.5), emit, self._caller_gone)
        emit({"result": {"session": session}})

    def _standby_start(self, body, emit):
        """POST /standby/start {wake_word}: listen for the wake word (ADR 0012). Streams progress;
        ends with {"result": standby}. Again while on, it replaces the wake word."""
        emit({"result": self.broker.standby_start(body.get("wake_word"), emit)})

    def _voice_design(self, body, emit):
        """POST /voices/design {description, name?, language?, seed?, replace?, bytes?}: make a voice
        from a description (Qwen3-TTS VoiceDesign speaks a sample in it). Without a name it's a draft,
        outside the library, to listen to and try (voice draft:ID) before POST /voices {draft, name}
        keeps it; with a name it's kept at once. Streams progress and ends with {"result": {draft or
        voice, path, duration, seconds}}, and the sample as bytes with bytes."""
        name = str(body.get("name") or "")
        description = str(body.get("description") or "").strip()
        if name and not speech._NAME.fullmatch(name):
            raise DenError(f"a voice name is lower case letters, digits and dashes, got {name!r}")
        if not description:
            raise DenError("describe the voice, e.g. \"an old man with a deep, raspy, slow voice\"")
        if name and name in speech.voices() and not body.get("replace"):
            raise DenError(f"voice {name!r} exists; replace it with replace")
        why = speech.design_unavailable()
        if why:
            raise DenError(why)
        language = str(body.get("language") or "en").lower()
        if language not in speech.DESIGN_LANGUAGES:
            raise DenError(f"the voice designer speaks {', '.join(speech.DESIGN_LANGUAGES)}; got {language!r}")
        config = core.load_config()
        info = {"side": "image", "caller": self._caller(), "method": "POST", "path": "/voices/design", "model": "voice-design"}
        req_id = self.broker.admit(info, emit, self._caller_gone)
        began = time.time()

        def check():
            if info.get("cancelled"):
                raise DenError(info["cancelled"])
            if self._caller_gone():
                raise ConnectionResetError("the caller hung up while designing")

        try:
            settings = image.settings(config)
            comfy = ComfyUI(settings["base_url"]) if settings else None
            if comfy and comfy.up():
                comfy.free()  # the designer and ComfyUI's models don't share the GPU
            emit({"designing": name})
            data = speech.design(description, language, body.get("seed"), check)
        finally:
            self.broker.finish(req_id)
        samples, rate = speech._pcm(data)
        seconds = round(time.time() - began, 1)
        result = {"description": description, "duration": round(len(samples) / rate, 2), "seconds": seconds}
        if name:
            path = speech.keep_voice(name, data, bool(body.get("replace")), description, language, body.get("seed"))
            result |= {"voice": name, "voices": sorted(speech.voices())}
        else:
            draft = speech.keep_draft(data, description, language, body.get("seed"))
            path = speech.draft_path(draft)
            result |= {"draft": draft, "path": str(path)}
        log(f"#{req_id} {info['caller']} POST /voices/design -> {path.name} in {seconds}s")
        if body.get("bytes"):
            result["sample"] = speech.as_bytes(path)  # to listen to it there
        emit({"result": result})

    def _voices(self):
        return {
            "voices": sorted(speech.voices()),
            # What each voice is, for choosing one: the list_voices tool shows this, not the tool text.
            "toc": speech.toc(),
            "languages": speech.LANGUAGES,
            "unavailable": speech.unavailable(),
            # Voices from a description, where the designer is installed (ADR 0010).
            "design_languages": list(speech.DESIGN_LANGUAGES) if speech.design_unavailable() is None else [],
        }

    def _voice_one(self, name, with_bytes):
        """GET /voices?name=N: one voice's details; with bytes, its sample too (to listen to it)."""
        info = speech.voice_info(name)
        path = info.pop("path")
        if with_bytes:
            info["sample"] = {"name": Path(path).name, "base64": speech.as_bytes(path)}
        return info

    def _voice_add(self, body):
        """POST /voices {name, recording: path or {name, base64}, description?, replace?}: keep a
        voice. {name, remove: true} removes one, {name, rename_to} renames it, and {name,
        description} alone changes what it says about itself."""
        name = str(body.get("name") or "")
        if body.get("draft"):
            # A designed voice someone listened to and liked: into the library, with how it was made.
            path = speech.save_draft(str(body["draft"]), name, bool(body.get("replace")))
            log(f"{self._caller()} POST /voices -> kept draft {body['draft']} as {path.name}")
            return {"voice": name, "voices": sorted(speech.voices())}
        if body.get("remove"):
            speech.remove_voice(name)
            log(f"{self._caller()} POST /voices -> removed {name}")
            return {"removed": name, "voices": sorted(speech.voices())}
        if body.get("rename_to"):
            speech.rename_voice(name, str(body["rename_to"]))
            log(f"{self._caller()} POST /voices -> renamed {name} to {body['rename_to']}")
            return {"voice": body["rename_to"], "voices": sorted(speech.voices())}
        if body.get("description") and not body.get("recording"):
            speech.describe_voice(name, str(body["description"]))
            return {"voice": name, "voices": sorted(speech.voices())}
        folder = tempfile.mkdtemp(prefix="den-voice-")
        try:
            recording = body.get("recording")
            if image.is_file_object(recording):
                recording = image._write_input(folder, recording)
            path = speech.add_voice(name, recording or "", bool(body.get("replace")), body.get("description"))
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        log(f"{self._caller()} POST /voices -> {path.name}")
        return {"voice": path.stem, "voices": sorted(speech.voices())}

    def _voice(self, body, emit):
        """POST /voice {srt? | text?, voice?, language?, exaggeration?, cfg_weight?, temperature?,
        seed?, out?, bytes?}: speak each line of an SRT script (or one text) in a voice from the
        library, a recording's path, or one sent as bytes, and put the lines on one track at their
        times. Streams progress lines and ends with {"result": {...}}."""
        request = voice_request(body, None)
        cues, language, voice, options = request["cues"], request["language"], request["voice"], request["options"]
        config = core.load_config()
        info = {"side": "image", "caller": self._caller(), "method": "POST", "path": "/voice", "model": "chatterbox"}
        req_id = self.broker.admit(info, emit, self._caller_gone)
        began = time.time()

        def check():
            if info.get("cancelled"):
                raise DenError(info["cancelled"])
            if self._caller_gone():
                raise ConnectionResetError("the caller hung up while speaking")

        try:
            data, duration, notes = voice_track(self.broker, config, request, emit, check)
        except (DenError, ConnectionResetError, BrokenPipeError) as e:
            log(f"#{req_id} {info['caller']} POST /voice -> failed: {e}")
            raise
        finally:
            self.broker.finish(req_id)
        spoken = " ".join(c["text"] for c in cues)
        path, copies = speech.save(data, spoken, body.get("out"))
        # The script at the times the lines were spoken: subtitles, and a script to reuse.
        srt = speech.to_srt(cues)
        path.with_suffix(".srt").write_text(srt)
        seconds = round(time.time() - began, 1)
        summary = [
            f"voice {voice or 'default'}", speech.LANGUAGES[language], f"{len(cues)} line(s)", f"{duration:g}s",
            *[f"{k} {v}" for k, v in options.items()],
        ]
        result = {
            "summary": summary, "path": str(path), "copies": [str(p) for p in copies], "duration": duration,
            "lines": len(cues), "notes": notes, "voice": voice, "language": language, "seconds": seconds,
            "srt": srt,
            "waited_s": round(info["started"] - info.get("queued", info["started"]), 1),
        }
        speech.log({"caller": info["caller"], **{k: v for k, v in result.items() if k != "summary"}, "text": spoken[:500], **options})
        log(f"#{req_id} {info['caller']} POST /voice {len(cues)} line(s) -> {path} in {seconds}s")
        if body.get("bytes"):
            result["audio"] = speech.as_bytes(path)
            for key in ("path", "copies"):
                result.pop(key)
        emit({"result": result})

    def _stream_request(self, run, body):
        emit, started = self._ndjson()
        try:
            run(body, emit)
        except DenError as e:
            if not started():
                raise
            emit({"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return
        self.wfile.write(b"0\r\n\r\n")

    def _checker(self, info, comfy, prompt_id):
        """What ComfyUI's wait calls between polls: stop on a cancel or a caller that hung up."""

        def check():
            if info.get("cancelled"):
                raise DenError(info["cancelled"])
            if self._caller_gone():
                comfy.cancel(prompt_id)
                raise ConnectionResetError("the caller hung up while generating")

        return check

    def _with_inputs(self, run):
        """run(body, emit) with the input images a request sent as bytes written to a private
        folder for its length, and removed after, whatever happens."""

        def wrapped(body, emit):
            folder = tempfile.mkdtemp(prefix="den-inputs-")
            try:
                run(image.inputs_from_bytes(body, folder), emit)
            finally:
                shutil.rmtree(folder, ignore_errors=True)

        return wrapped

    def _generate(self, body, emit):
        config = core.load_config()
        settings = image.settings(config)
        if not settings:
            raise DenError("image generation is not configured ([image] in config.toml)")
        name = body.get("workflow") or settings.get("default_workflow")
        if not name:
            raise DenError("no workflow given and no [image] default_workflow in config.toml")
        references = body.get("references") or []
        # A saved pose (pose:NAME) reads its map; an unknown name fails here, before any wait.
        reference_paths = [image.input_path(r) for r in references]
        control = body.get("control") or {}
        control_image = image.input_path(control["image"]) if control.get("image") else None
        paths = [("image", body.get("image")), ("out", body.get("out")), ("control image", control_image)]
        for key, path in paths + [("references", r) for r in reference_paths]:
            if path and not Path(path).is_absolute():
                raise DenError(f"{key} must be an absolute path, got {path!r}")
        if body.get("control") and not control_image:
            raise DenError("control needs a guide image")
        for path in filter(None, [body.get("image"), control_image, *reference_paths]):
            if not Path(path).is_file():
                raise DenError(f"input image not found: {path}")
        prompt = body.get("prompt") or ""
        extras = ("loras", "references", "control", "upscale", "strength", "save_maps")
        options = {key: body.get(key) for key in (*image.SETTINGS, *extras)}
        graph, params, uploads, map_nodes = image.build(
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
            outputs, drawn = image.split_outputs(comfy.wait(prompt_id, self._checker(info, comfy, prompt_id)), map_nodes)
            paths, copies = image.save([comfy.view(o) for o in outputs], prompt, body.get("out"))
            maps = [image.save_map(paths[0], label, comfy.view(img)) for label, img in drawn]
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
            **({"maps": [str(p) for p in maps]} if maps else {}),
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
                "control_image": control.get("image"),
                "paths": result["paths"],
                "copies": result["copies"],
                "maps": result.get("maps"),
                "seconds": seconds,
                "waited_s": result["waited_s"],
            }
        )
        log(f"#{req_id} {info['caller']} POST /image {name} seed {params['seed']} -> {paths[0]} in {seconds}s")
        if body.get("bytes"):
            # The images themselves instead of where they are here (ADR 0007).
            result["images"] = [image.as_bytes(p) for p in paths]
            result["maps"] = [{"label": label, "base64": image.as_bytes(m)} for (label, _), m in zip(drawn, maps)]
            for key in ("paths", "copies"):
                result.pop(key)
        if body.get("switch_back"):
            result["switched_back"] = self.broker.switch_back(config, emit)
        emit({"result": result})

    def _clip(self, body):
        """POST /clip {prompt, workflow?, negative?, seed?, size?, duration?, sound?, keyframes? [{image,
        at?}], steps?, cfg?, sampler?, scheduler?, loras?, out?, preview?}: check the request, then make
        the clip as a detached request. Answers {id, estimate_s, summary} at once; GET /clip?id=N
        tells how it goes. A keyframe's image is an absolute path, or bytes from another machine.
        """
        config = core.load_config()
        name = body.get("workflow") or clip.settings(config).get("default_workflow")
        if not name:
            raise DenError("no workflow given and no [clip] default_workflow in config.toml")
        if body.get("out") and not Path(body["out"]).is_absolute():
            raise DenError(f"out must be an absolute path, got {body['out']!r}")
        # Keyframes sent as bytes live in a private folder until the clip is made.
        folder = tempfile.mkdtemp(prefix="den-inputs-")
        try:
            keyframes = []
            for i, keyframe in enumerate(body.get("keyframes") or []):
                if not isinstance(keyframe, dict):
                    raise DenError(f"keyframe {i + 1} must be {{image, at?}}")
                path = keyframe.get("image")
                if image.is_file_object(path):
                    path = image._write_input(folder, path)
                elif path and not Path(path).is_absolute():
                    raise DenError(f"keyframe {i + 1} must be an absolute path, got {path!r}")
                if path and not Path(path).is_file():
                    raise DenError(f"keyframe image not found: {path}")
                keyframes.append({**keyframe, "image": path})
            voiced = voice_request(body["voiceover"], folder) if body.get("voiceover") else None
            duration = body.get("duration")
            flows = clip.check_workflows(config)
            if voiced and voiced["sync"] and name in flows:
                wf = flows[name][0]
                if "lip_sync" not in wf:
                    makes = [n for n, (w, problem) in flows.items() if "lip_sync" in w and problem is None]
                    raise DenError(
                        f"workflow {name} can't lip-sync"
                        + (f"; these can: {', '.join(makes)}" if makes else "; no clip workflow here can")
                    )
                if body.get("sound") is False:
                    raise DenError("lip-sync makes the clip's sound from the voice: it needs sound on")
            if voiced and duration is None and name in flows:
                # Long enough for the script, within what the workflow allows.
                length = clip.voiceover_length(voiced["cues"])
                allowed = flows[name][0]["duration"].get("allowed")
                duration = min(length, allowed[1]) if length and allowed else length
            build = {
                "name": name, "prompt": body.get("prompt") or "", "negative": body.get("negative"),
                "seed": body.get("seed"), "size": body.get("size"), "keyframes": keyframes,
                "options": {key: body.get(key) for key in (*image.SETTINGS, "loras", "sound")},
            }
            graph, params, uploads, sheet = clip.build(config, duration=duration, **build)
            if voiced:
                params["voiceover"] = "recording" if voiced["audio"] else voiced["voice"] or "default"
                if voiced["sync"]:
                    params["lip_sync"] = True
                # Kept to build the graph again if the spoken voice turns out longer than planned.
                voiced["build"] = {**build, "seed": params["seed"]}
            # Refuse now what admit would refuse later, rather than hand out an id that fails.
            self.broker._check_available("image", config, core.load_state(config))
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        caller = self._caller()
        info = {
            "side": "image", "caller": caller, "method": "POST", "path": "/clip", "model": name,
            "id": next(self.broker.ids), "estimate": clip.estimate(params),
        }
        record = {
            "id": info["id"], "state": "waiting", "info": info, "params": params,
            "prompt": body.get("prompt"), "created": time.time(), "voiceover": voiced,
        }
        self.broker.detach(record)
        body = {**body, "keyframes": keyframes}
        threading.Thread(target=run_clip, args=(self.broker, record, graph, uploads, sheet, body, folder), daemon=True).start()
        log(f"#{info['id']} {caller} POST /clip {name} detached ({_time_left(info['estimate'])})")
        return {"id": info["id"], "estimate_s": info["estimate"], "summary": clip.summary_parts(params)}

    def _clip_get(self, query):
        """GET /clip?id=N[&bytes=1]: one clip; without an id, every clip the broker holds."""
        if "id" not in query:
            self._send_json(200, {"clips": self.broker.detached_views()})
            return
        record = self._clip_record(query["id"][0])
        if record:
            self._send_json(200, clip_view(record, with_bytes=query.get("bytes", ["0"])[0] not in ("0", "")))

    def _clip_cancel(self, body):
        """POST /clip/cancel {id}: drop a clip that waits, interrupt one that runs."""
        record = self._clip_record(body.get("id"))
        if not record:
            return
        if record["state"] in ("waiting", "running"):
            reason = f"cancelled by {self._caller()}"
            record["info"]["cancelled"] = reason  # a waiting one sees it in admit
            self.broker.cancel(record["id"], reason)  # a running one: ComfyUI interrupts it
            log(f"#{record['id']} {reason}")
        self._send_json(200, {"id": record["id"], "state": record["state"]})

    def _clip_record(self, value):
        """The record of a clip id, or None after answering 404 for one the broker doesn't hold."""
        try:
            record = self.broker.detached_record(int(value))
        except (TypeError, ValueError):
            record = None
        if record is None:
            self._send_json(
                404,
                self._error_body(
                    f"clip {value} is not known here: the broker may have restarted, or it finished "
                    "over a day ago (a finished clip's file stays where it was saved)"
                ),
            )
        return record

    def _pose(self, body):
        """POST /pose {image, name, description, replace?, draw_only?, preview?}: draw the photo's
        pose and keep it in the pose library, with its joints as JSON. Streams progress lines like
        /image and ends with {"result": {name, description, width, height, aspect, map, source,
        toc, seconds, waited_s, preview?}} or {"error": "..."}.

        With draw_only the map comes back and nothing is saved — no library entry, no files — so a
        client can show what was found before it is kept; the same request without it, a second
        drawing later, is what saves. The result is then {name?, width, height, aspect, images:
        [the map as bytes], seconds, waited_s, preview?}: there is no file to name, so the map
        always comes back the way a "bytes" request gets its images (ADR 0007). A name is
        optional there, and one that is given is checked as a save would check it.
        """
        self._stream_request(self._with_inputs(self._save_pose), body)

    def _save_pose(self, body, emit):
        config = core.load_config()
        settings = image.settings(config)
        if not settings:
            raise DenError("image generation is not configured ([image] in config.toml)")
        name, description, photo = body.get("name"), body.get("description"), body.get("image")
        replace = bool(body.get("replace"))
        draw_only = bool(body.get("draw_only"))
        # Refuse a bad name, a taken one or a missing photo before waiting for the GPU. A
        # draw_only request saves nothing, so it needs no name; a name it does carry is checked
        # here, so the save that follows doesn't fail on it after a second drawing.
        if not draw_only:
            poses.check_new(name, description, replace)
        elif name is not None:
            poses.check_name(name)
            poses.check_taken(name, replace)
        if not photo or not Path(photo).is_absolute():
            raise DenError(f"image must be an absolute path, got {photo!r}")
        if not Path(photo).is_file():
            raise DenError(f"input image not found: {photo}")
        graph, load, joints = image.pose_graph(config)

        info = {"side": "image", "caller": self._caller(), "method": "POST", "path": "/pose", "model": "pose"}
        req_id = self.broker.admit(info, emit, self._caller_gone)
        comfy = ComfyUI(settings["base_url"])
        try:
            image.fill_uploads(graph, [(load, photo)], [comfy.upload(photo)])
            emit({"drawing": {"name": name or "the photo"}})
            began = time.time()
            prompt_id = comfy.submit(graph)
            info["cancel"] = lambda: comfy.cancel(prompt_id)
            drawn = comfy.wait(prompt_id, self._checker(info, comfy, prompt_id))
            if not drawn:
                raise DenError("comfyui finished without a pose map")
            data = comfy.view(drawn[0])
            if poses.is_blank(data):
                raise DenError("no person found in the photo: the pose map is blank, so nothing was saved")
            if draw_only:
                width, height = poses.png_size(data)
                entry = {**({"name": name} if name else {}), "width": width, "height": height}
            else:
                keypoints = _keypoints(comfy.texts(prompt_id).get(joints))
                entry = poses.save(name, description, photo, data, replace, keypoints, body.get("origin"))
            seconds = round(time.time() - began, 1)
            shown = image.preview(comfy, drawn[0]) if body.get("preview") else None
        except (DenError, ConnectionResetError, BrokenPipeError) as e:
            log(f"#{req_id} {info['caller']} POST /pose {name or '-'} -> failed: {e}")
            raise
        finally:
            self.broker.finish(req_id)
        where = "drawn only, nothing saved" if draw_only else entry["map"]
        log(f"#{req_id} {info['caller']} POST /pose {name or '-'} -> {where} in {seconds}s")
        if body.get("bytes") and not draw_only:
            # Where the pose's files are here means nothing to a client on another machine.
            entry = {k: v for k, v in entry.items() if k not in ("map", "source", "keypoints", "from")}
        result = {
            **entry,
            "aspect": poses.aspect(entry["width"], entry["height"]),
            # Nothing was saved, so the map itself is the answer, and the library is unchanged.
            **({"images": [image.encoded(data)]} if draw_only else {"toc": poses.toc(files=not body.get("bytes"))}),
            "seconds": seconds,
            "waited_s": round(info["started"] - info.get("queued", info["started"]), 1),
            **({"preview": shown} if shown else {}),
        }
        emit({"result": result})

    def _proxy(self, path):
        body = self._read_body()
        try:
            parsed = json.loads(body) if body else None
        except json.JSONDecodeError:
            parsed = None
        if path in OLLAMA_RUN_PATHS:
            raise DenError(
                "den runs models through llama-server, which speaks the OpenAI-style API: "
                "send this as /v1/chat/completions (Ollama is only the model store)"
            )
        if self.command == "GET" and path in LLM_INFO_PATHS:
            # Read what the running server already has, and never load one for it. No llm_lock:
            # llama-server answers these while it generates, and holding the lock would queue a
            # health check behind a whole turn. If it stops meanwhile, the read says so.
            if not self.broker.llm.running():
                raise DenError(
                    f"llama-server holds no model, so it has nothing to report at {path}; "
                    "it starts on the first request that needs it"
                )
            self._forward(body, {"caller": self._caller()}, self.broker.llm.request_conn(), self._llm_down)
            return
        if path in CONTROL_PATHS or path.startswith("/api/"):
            # Ollama as the model store: its catalogue and downloads use no GPU and aren't tracked.
            config = core.load_config()
            upstream = urlsplit(config["llm"]["base_url"])
            conn = http.client.HTTPConnection(upstream.hostname, upstream.port or 80, timeout=UPSTREAM_TIMEOUT_S)
            self._forward(body, {"caller": self._caller()}, conn, self._ollama_down)
            return
        info = {
            "side": "llm",
            "caller": self._caller(),
            "method": self.command,
            "path": path,
            "model": parsed.get("model") if isinstance(parsed, dict) else None,
        }
        req_id = self.broker.admit(info, caller_gone=self._caller_gone)
        try:
            config = core.load_config()
            model = info["model"] or core.load_state(config)["llm_model"]
            if not model:
                raise DenError("the request names no model and none is selected; pick one with: den model")
            with self.broker.llm_lock:
                self.broker.ensure_llm(config, model)
                conn = self.broker.llm.request_conn()
                self._forward(body, info, conn, self._llm_down)
                self.broker.drop_dead_llm()
        finally:
            self.broker.finish(req_id)
            took = time.time() - info["started"]
            outcome = info.get("status", "no response") if not info.get("cancelled") else info["cancelled"]
            log(f"#{req_id} {info['caller']} {self.command} {path} {info['model']} -> {outcome} in {took:.1f}s")

    def _ollama_down(self, e):
        # Also in the journal: nobody is watching this caller's error, and someone has to start
        # Ollama by hand (den runs as your user; Ollama is a system service).
        config = core.load_config()
        log(f"OLLAMA DOWN at {config['llm']['base_url']} ({e}): start it with: {platform.ollama_start_hint()}")
        return DenError(
            f"ollama is not reachable at {config['llm']['base_url']} ({e}); "
            f"start it with: {platform.ollama_start_hint()}"
        )

    def _llm_down(self, e):
        log(f"llama-server stopped answering ({e}); see {llm.LOG_PATH}")
        return DenError(f"llama-server stopped answering ({e}); see {llm.LOG_PATH}")

    def _forward(self, body, info, conn, down):
        """Pass the request to conn's server and stream its answer back. down(e) is the error to
        raise when that server can't be reached."""
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
                raise down(e) from e

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
            info["cancelled"] = "caller hung up"  # closing upstream below makes the server stop
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
    url = urlsplit(core.listen_url(config))
    broker = Broker()
    Handler.broker = broker
    server = ThreadingHTTPServer((url.hostname, url.port), Handler)

    def terminate(signum, frame):  # a stop or restart from the service manager: leave through the finally below
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    server.daemon_threads = True
    loaded = broker.detect_loaded()
    threading.Thread(target=broker.idle_loop, daemon=True).start()
    comfy = image.settings(config).get("base_url", "not configured")
    log(
        f"den broker on {url.hostname}:{url.port} -> llama-server at {llm.SOCKET} (models from ollama "
        f"{config['llm']['base_url']}), comfyui {comfy}; loaded: {loaded or 'nothing'}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        with broker.llm_lock:
            broker._stop_llm(_no_emit)  # save the cache: a restart shouldn't cost the conversation
