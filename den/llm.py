"""The LLM side: one llama-server process that the broker starts, stops and swaps (ADR llama-server).

Ollama stays as the model store — `den model` / `ollama pull` download into it and its API says
which GGUF file a model is — but it no longer runs models. The broker starts `llama-server` on
that file as its own child process, listening on a UNIX socket in the user's private runtime
folder, so no port is open to anyone else. Before the server stops (a swap to images, a model
change, a release, the idle timeout) the broker saves the conversation's cache to a file, and
restores it when the server comes back: the next request then reads only what's new instead of
the whole conversation again.
"""

import http.client
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

from den import core, platform
from den.core import DenError, Ollama

SOCKET = Path(os.environ.get("DEN_LLM_SOCKET") or platform.runtime_dir() / "den/llm.sock")
_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
SLOT_DIR = Path(os.environ.get("DEN_SLOTS") or _CACHE / "den/slots")
LOG_PATH = core._STATE_HOME / "den/llama-server.log"
START_TIMEOUT_S = 600  # loading a ~17 GB model split across GPU and RAM
STOP_TIMEOUT_S = 30
# The CUDA driver can lag behind the process exit it belongs to: llama-server has been seen
# holding its VRAM for tens of seconds after stop() considered it gone, long enough to starve
# the speech model's load right after a swap. These bound how long stop() waits for the
# driver to catch up before giving the GPU to whatever asked for it next.
VRAM_SETTLE_TIMEOUT_S = 20
VRAM_SETTLE_POLL_S = 0.5
VRAM_SETTLE_MIN_RISE_MB = 256  # noise-sized rises don't count as "freed"
# What ggml prints when a compute backend has failed unrecoverably — a Metal allocation that
# didn't fit, a CUDA OOM. The server stays up and answers every later request with an error,
# so this line is the only way to tell a dead backend from a request that simply failed.
DEAD_BACKEND = "backend is in error state"
LOG_TAIL_BYTES = 1 << 16
SAVE_TIMEOUT_S = 300
REQUEST_TIMEOUT_S = 900  # per read: covers reading a 32k prompt


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over the server's UNIX socket."""

    def __init__(self, path, timeout=REQUEST_TIMEOUT_S):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def gguf_type(path):
    """A GGUF file's general.type ("model", "mmproj", …) and its MTP layer count, from its header."""
    sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
    formats = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
    found = {"type": None, "nextn": 0}
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise DenError(f"{path} isn't a GGUF file")
        f.read(4)  # version
        _, n_kv = struct.unpack("<QQ", f.read(16))

        def text():
            (n,) = struct.unpack("<Q", f.read(8))
            return f.read(n).decode("utf-8", "replace")

        for _ in range(n_kv):
            key = text()
            (kind,) = struct.unpack("<I", f.read(4))
            if kind == 8:
                value = text()
            elif kind == 9:  # an array: skip it
                (item,) = struct.unpack("<I", f.read(4))
                (n,) = struct.unpack("<Q", f.read(8))
                if item == 8:
                    for _ in range(n):
                        text()
                else:
                    f.seek(sizes[item] * n, 1)
                continue
            else:
                (value,) = struct.unpack(formats[kind], f.read(sizes[kind]))
            if key == "general.type":
                found["type"] = value
            elif key.endswith(".nextn_predict_layers"):
                found["nextn"] = int(value)
    return found


# Sampling settings a model's Ollama record may set, and the llama-server flag for each. Ollama
# applied them; llama-server doesn't read them, and its own defaults (temperature 0.8, top_k 40,
# min_p 0.05) are not what the model was published with.
SAMPLING = {
    "temperature": "--temp", "top_k": "--top-k", "top_p": "--top-p", "min_p": "--min-p",
    "presence_penalty": "--presence-penalty", "frequency_penalty": "--frequency-penalty",
    "repeat_penalty": "--repeat-penalty", "repeat_last_n": "--repeat-last-n",
}


def sampling(parameters):
    """The llama-server flags for the sampling settings in Ollama's `parameters` text."""
    args = []
    for line in (parameters or "").splitlines():
        key, _, value = line.strip().partition(" ")
        if key in SAMPLING and value.strip():
            args += [SAMPLING[key], value.strip()]
    return args


def resolve(config, model):
    """{model, nextn, mmproj, sampling}: the GGUF files an Ollama model lives in, from Ollama's own
    record of it — the model itself, and its vision projector when it has one (else None) — and
    the sampling settings that record gives it, as llama-server flags."""
    show = Ollama(config["llm"]["base_url"]).show(model)
    paths = [line[5:].strip() for line in show.get("modelfile", "").splitlines() if line.startswith("FROM ")]
    found = {"model": None, "nextn": 0, "mmproj": None, "sampling": sampling(show.get("parameters"))}
    for path in paths:
        if not Path(path).is_file():
            continue
        info = gguf_type(path)
        if info["type"] == "mmproj":
            found["mmproj"] = found["mmproj"] or path
        elif info["type"] in (None, "model") and not found["model"]:
            found["model"], found["nextn"] = path, info["nextn"]
    if not found["model"]:
        raise DenError(f"ollama lists no readable GGUF model file for {model} ({', '.join(paths) or 'none'})")
    return found


def slot_file(model):
    """The file name a model's saved cache goes in (llama-server takes a bare name)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model) + ".bin"


class Server:
    """The llama-server process, if one is running, and the model it has."""

    def __init__(self):
        self.proc = None
        self.model = None
        self.log = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _conn(self, timeout=REQUEST_TIMEOUT_S):
        return UnixHTTPConnection(SOCKET, timeout=timeout)

    def _call(self, method, path, body=None, timeout=30):
        conn = self._conn(timeout)
        try:
            data = None if body is None else json.dumps(body).encode()
            conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            try:
                answer = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                answer = {"raw": raw.decode(errors="replace")}
            return resp.status, answer
        finally:
            conn.close()

    def args(self, config, model, files):
        llm = config["llm"]
        args = [
            llm.get("server", "llama-server"), "-m", files["model"], "--alias", model,
            "--host", str(SOCKET), "--no-webui", "--parallel", "1", "--jinja",
            "--ctx-size", str(llm.get("num_ctx", 32768)), "--fit", "on",
            "--slot-save-path", str(SLOT_DIR),
        ]
        if files["nextn"]:  # the model carries its own multi-token-prediction head
            args += ["--spec-type", "draft-mtp"]
        if files["mmproj"] and llm.get("vision", True):  # it can read images: load its projector
            args += ["--mmproj", files["mmproj"]]
        # The model's own sampling defaults; server_args come after, so they win, and a caller's
        # request still overrides both.
        args += files.get("sampling", [])
        return args + [str(a) for a in llm.get("server_args", [])]

    def start(self, config, model, emit):
        """Start the server with model and restore its last saved cache. Returns the seconds taken."""
        began = time.time()
        files = resolve(config, model)
        SOCKET.parent.mkdir(parents=True, exist_ok=True)
        SOCKET.parent.chmod(0o700)
        SLOT_DIR.mkdir(parents=True, exist_ok=True)
        SLOT_DIR.chmod(0o700)  # the saved caches are a computed form of the conversations
        SOCKET.unlink(missing_ok=True)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        emit({"starting": f"llama-server ({model})"})
        self.log = open(LOG_PATH, "w")
        try:
            self.proc = subprocess.Popen(
                self.args(config, model, files), stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT
            )
        except OSError as e:
            raise DenError(
                f"cannot start llama-server ({e}); install it ({platform.llama_install_hint()}) "
                "or set [llm] server in config.toml"
            ) from e
        self.model = model
        deadline = time.time() + START_TIMEOUT_S
        while True:
            if not self.running():
                self.proc = self.model = None
                raise DenError(f"llama-server exited while loading {model}; see {LOG_PATH}")
            try:
                status, _ = self._call("GET", "/health", timeout=5)
                if status == 200:
                    break
            except OSError:
                pass
            if time.time() > deadline:
                self.stop(_no_emit, save=False)
                raise DenError(f"llama-server didn't get {model} ready in {START_TIMEOUT_S}s; see {LOG_PATH}")
            time.sleep(0.5)
        self._restore(model)
        return time.time() - began

    def _restore(self, model):
        name = slot_file(model)
        if not (SLOT_DIR / name).is_file():
            return
        try:
            status, answer = self._call("POST", "/slots/0?action=restore", {"filename": name}, timeout=SAVE_TIMEOUT_S)
        except OSError as e:
            core_log(f"cache restore for {model} failed: {e}")
            return
        if status == 200:
            ms = (answer.get("timings") or {}).get("restore_ms", 0)
            core_log(f"restored {answer.get('n_restored')} cached tokens for {model} in {ms / 1000:.1f}s")
        else:
            core_log(f"cache restore for {model} refused (HTTP {status}): {answer}")

    def _save(self):
        try:
            status, answer = self._call("POST", "/slots/0?action=save", {"filename": slot_file(self.model)}, timeout=SAVE_TIMEOUT_S)
        except OSError as e:
            core_log(f"cache save for {self.model} failed: {e}")
            return
        if status == 200:
            ms = (answer.get("timings") or {}).get("save_ms", 0)
            core_log(f"saved {answer.get('n_saved')} cached tokens for {self.model} in {ms / 1000:.1f}s")
        else:
            core_log(f"cache save for {self.model} refused (HTTP {status}): {answer}")

    def stop(self, emit, save=True):
        """Save the cache (unless save=False) and stop the server. Returns the model it had, or None."""
        if not self.running():
            self.proc = self.model = None
            return None
        model = self.model
        emit({"unloading": [model]})
        if save:
            self._save()
        before = platform.free_vram_mb()
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(10)
        self.proc = self.model = None
        if self.log:
            self.log.close()
            self.log = None
        SOCKET.unlink(missing_ok=True)
        self._await_vram(before)
        return model

    def _await_vram(self, before):
        """Wait a little past the process exiting for the driver to actually reclaim its VRAM
        (a CUDA context can outlive the process that opened it), so the next thing that loads
        onto the GPU doesn't race a reclaim that's still in flight. Best-effort: gives up
        silently if VRAM can't be read here, or if it doesn't rise within the deadline —
        callers should keep treating a load failure afterward as a real one, not retry forever.
        """
        if before is None:
            return
        deadline = time.time() + VRAM_SETTLE_TIMEOUT_S
        while time.time() < deadline:
            after = platform.free_vram_mb()
            if after is None or after >= before + VRAM_SETTLE_MIN_RISE_MB:
                return
            time.sleep(VRAM_SETTLE_POLL_S)

    def backend_dead(self):
        """Whether this server's compute backend has failed unrecoverably.

        There is nothing to ask it: `/health` answers 200 from a server whose backend is
        gone, and so does every endpoint that doesn't compute. The log is where it says so,
        and it says so again on each attempt, so the tail is enough.
        """
        try:
            with open(LOG_PATH, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - LOG_TAIL_BYTES))
                return DEAD_BACKEND in f.read().decode("utf-8", "replace")
        except OSError:
            return False

    def request_conn(self):
        """A connection to the server for the broker to pass a request through."""
        return self._conn()


def _no_emit(msg):
    pass


def core_log(text):
    print(f"{time.strftime('%H:%M:%S')} {text}", file=sys.stderr, flush=True)
