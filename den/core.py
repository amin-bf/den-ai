"""Shared core of the local AI toolchain: config, runtime state, and backend clients.

config.toml is hand-edited (LLM settings, tasks, endpoints); state.json is written by the
broker on mode switches and by the `den` CLI (active model, task toggles). The broker,
CLI and MCP server read them fresh on every use, so a switch takes effect without restarts.
Clients reach the LLM only through the broker (`den serve`, see broker.py), which runs it on
llama-server; Ollama is the model store.
"""

import fcntl
import json
import os
import re
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("DEN_CONFIG", ROOT / "config.toml"))
# Private additions merged over config.toml (git-ignored), e.g. workflow notes.
LOCAL_CONFIG_PATH = Path(os.environ.get("DEN_CONFIG_LOCAL", CONFIG_PATH.with_name("config.local.toml")))
STATE_PATH = Path(os.environ.get("DEN_STATE", ROOT / "state.json"))
# Delegation log: one JSON object per line, outside git because it names files and projects.
_STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
LOG_PATH = Path(os.environ.get("DEN_LOG", _STATE_HOME / "den/delegations.jsonl"))
VERDICTS = ("ok", "partly", "wrong", "unchecked")

# The mode is a kill switch, not a router: `off` keeps every caller off the GPU. Whether a
# request's side can run is decided by what's installed and selected, not by the mode.
MODES = ("on", "off")
# state.json written when a mode still picked a side.
LEGACY_MODES = {"llm": "on", "image": "on", "both": "on"}
OFF_MESSAGE = "den is off; turn it on with: den mode on"

# Rough bytes-per-token used to refuse inputs that would overflow num_ctx: Ollama
# truncates an oversized prompt silently, which yields a confident answer about
# half the input.
BYTES_PER_TOKEN = 3
OUTPUT_RESERVE_TOKENS = 2048


class DenError(Exception):
    pass


def _merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config():
    """config.toml, with config.local.toml (when it exists) merged over it table by table."""
    config = {}
    for path in (CONFIG_PATH, LOCAL_CONFIG_PATH):
        if path is LOCAL_CONFIG_PATH and not path.exists():
            break
        try:
            with open(path, "rb") as f:
                _merge(config, tomllib.load(f))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise DenError(f"cannot read {path}: {e}") from e
    return config


def load_state(config):
    state = {
        "mode": config.get("default_mode", "on"),
        "llm_model": None,
        "tasks": {},
    }
    try:
        state.update(json.loads(STATE_PATH.read_text()))
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as e:
        raise DenError(f"cannot read {STATE_PATH}: {e}") from e
    state["mode"] = LEGACY_MODES.get(state["mode"], state["mode"])
    if state["mode"] not in MODES:
        raise DenError(f"unknown mode {state['mode']!r} in {STATE_PATH}")
    return state


def save_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(STATE_PATH)


def is_on(state):
    return state["mode"] != "off"


def llm_unavailable(config, state):
    """Why an LLM request can't run now, or None.

    Only what den itself knows: Ollama being down or a model missing is reported by the
    request that goes out, not guessed at here.
    """
    if not is_on(state):
        return OFF_MESSAGE
    if not state["llm_model"]:
        return "no LLM model is selected; pick one with: den model"
    return None


def all_tasks(config, state):
    """{name: (task_config, enabled)} — state.json toggles override config defaults."""
    return {
        name: (task, state["tasks"].get(name, task.get("enabled", True)))
        for name, task in config.get("tasks", {}).items()
    }


def enabled_tasks(config, state):
    return {name: task for name, (task, on) in all_tasks(config, state).items() if on}


def llm_model(state):
    """The active Ollama model name. It lives only in state.json, set with `den model`."""
    if not state["llm_model"]:
        raise DenError("no LLM model selected; pick one with: den model")
    return state["llm_model"]


def listen_url(config):
    """Where this machine's broker listens (`den serve`)."""
    return config.get("broker", {}).get("base_url", "http://127.0.0.1:11435")


# A client can use another machine's broker instead of this one's: a remote, reached through
# an SSH tunnel so the broker itself stays on 127.0.0.1 there (ADR 0007). Remotes are named in
# [remotes.<name>] base_url — in config.local.toml, since they belong to this machine — and
# DEN_BROKER picks one; `den --on NAME` sets it.
REMOTE_ENV = "DEN_BROKER"


def remote_name():
    """The remote this process's requests go to, or None for this machine's broker."""
    return os.environ.get(REMOTE_ENV) or None


def broker_url(config):
    """Where a client sends its requests: this machine's broker, or the remote DEN_BROKER names."""
    name = remote_name()
    if not name:
        return listen_url(config)
    url = config.get("remotes", {}).get(name, {}).get("base_url")
    if not url:
        raise DenError(f"{REMOTE_ENV}={name}, but config.local.toml has no [remotes.{name}] base_url")
    return url


# A local model doesn't only hold the GPU: an MoE split across GPU and RAM keeps several GB of
# RAM and generates on the CPU too. So den refuses to *load* a side while the machine is
# already busy with other work; a side that is loaded keeps serving ([limits], ADR 0004).
DEFAULT_MAX_LOAD_PER_CPU = 0.8
DEFAULT_MIN_FREE_RAM_GB = 4.0
DEFAULT_BUSY_WAIT = "60s"


def cpus():
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:  # not Linux
        return os.cpu_count() or 1


def free_ram_gb():
    """RAM a new model could take without swapping, in GB; None when it can't be read.

    How to ask differs per system, so platform.py answers it. It is imported here rather
    than at the top because platform.py needs this module's DenError.
    """
    from den import platform

    return platform.free_ram_gb()


def pressure():
    """What the machine is doing: 1-minute load average per CPU, and RAM still available.

    The 1-minute average, not the current CPU use: it ignores a short spike but catches a test
    suite or a build that has been running for a while.
    """
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = None
    count = cpus()
    return {
        "load": load,
        "cpus": count,
        "load_per_cpu": round(load / count, 2) if load is not None else None,
        "free_ram_gb": round(ram, 1) if (ram := free_ram_gb()) is not None else None,
    }


def limits(config):
    """(max load per cpu, min free RAM in GB) from [limits]; 0 or missing turns a limit off."""
    section = config.get("limits", {})
    return (
        float(section.get("max_load_per_cpu", DEFAULT_MAX_LOAD_PER_CPU) or 0),
        float(section.get("min_free_ram_gb", DEFAULT_MIN_FREE_RAM_GB) or 0),
    )


def busy_wait_s(config, section=None):
    """How long a request waits for a busy machine to settle before it's refused ([limits]).

    Busy is usually a build or a test run, which ends; refusing at once turns a wait into an
    error the caller has to notice and repeat. 0 refuses immediately, as den did before.
    """
    section = config.get("limits", {}) if section is None else section
    return duration_s(section.get("busy_wait", DEFAULT_BUSY_WAIT))


def too_busy(config, now=None):
    """Why the machine is too busy for den to load a model, or None ([limits] in config.toml)."""
    max_load, min_ram = limits(config)
    now = now or pressure()
    reasons = []
    if max_load and now["load_per_cpu"] is not None and now["load_per_cpu"] > max_load:
        reasons.append(
            f"the load average is {now['load']:.1f} on {now['cpus']} cpus "
            f"({now['load_per_cpu']:.2f} per cpu, limit {max_load})"
        )
    if min_ram and now["free_ram_gb"] is not None and now["free_ram_gb"] < min_ram:
        reasons.append(f"only {now['free_ram_gb']:.1f} GB RAM is available (limit {min_ram} GB)")
    return " and ".join(reasons) or None


def duration_s(value):
    """Seconds from a number or a duration like "90s", "30m" or "1h"."""
    if isinstance(value, (int, float)):
        return float(value)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(value))
    if not match:
        raise DenError(f"cannot read duration {value!r}; use e.g. 90s, 30m or 1h")
    return float(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]


def model_tag(model):
    return model if ":" in model else f"{model}:latest"


def _error_message(detail):
    """The message inside an Ollama, OpenAI-style or broker error body, else the raw body."""
    try:
        error = json.loads(detail)["error"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return detail
    return error.get("message", detail) if isinstance(error, dict) else str(error)


class Ollama:
    """Ollama's native API, for the model store: what's downloaded and where its files are. It no
    longer runs models (llama-server does, ADR 0005). The broker uses it upstream; clients use
    BrokerClient, which reaches the same catalogue through the broker."""

    def __init__(self, base_url, caller=None):
        self.base_url = base_url.rstrip("/")
        self.caller = caller

    def _unreachable(self, e):
        from den import platform

        return DenError(
            f"ollama is not reachable at {self.base_url} ({e}); start it with: {platform.ollama_start_hint()}"
        )

    def _open(self, method, path, body, timeout):
        headers = {"Content-Type": "application/json"}
        if self.caller:
            headers["X-Den-Caller"] = self.caller
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            message = _error_message(e.read().decode(errors="replace").strip())
            if message.startswith("den: "):  # the broker's own refusal: already explains itself
                raise DenError(message.removeprefix("den: ")) from e
            raise DenError(f"ollama {path}: HTTP {e.code}: {message}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise self._unreachable(getattr(e, "reason", e)) from e

    def _request(self, method, path, body=None, timeout=10):
        with self._open(method, path, body, timeout) as resp:
            try:
                return json.loads(resp.read() or b"{}")
            except (TimeoutError, ConnectionError) as e:  # connected, but the answer never came
                raise DenError(f"{self.base_url}{path}: no complete answer ({e!r})") from e

    def version(self):
        return self._request("GET", "/api/version")["version"]

    def models(self):
        """Downloaded chat models, sorted by name: no cloud models, no embedding-only models."""
        return sorted(
            (
                m
                for m in self._request("GET", "/api/tags").get("models", [])
                if not m.get("remote_host") and "completion" in m.get("capabilities", ["completion"])
            ),
            key=lambda m: m["name"],
        )

    def installed(self):
        return {model_tag(m["name"]) for m in self._request("GET", "/api/tags").get("models", [])}

    def loaded(self):
        return self._request("GET", "/api/ps").get("models", [])

    def show(self, model):
        """Ollama's record of a model: its modelfile (with the GGUF paths) and capabilities."""
        return self._request("POST", "/api/show", {"model": model}, timeout=30)

    def unload(self, model):
        self._request("POST", "/api/generate", {"model": model, "keep_alive": 0}, timeout=60)

class BrokerClient(Ollama):
    """The broker: the LLM's OpenAI-style API, Ollama's catalogue passed through, and the broker's
    own endpoints."""

    def chat(self, model, settings, system, user):
        """One non-streamed chat completion on llama-server; returns the OpenAI-style answer."""
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
        }
        if "think" in settings:  # the Qwen templates switch thinking with this flag
            body["chat_template_kwargs"] = {"enable_thinking": bool(settings["think"])}
        return self._request("POST", "/v1/chat/completions", body, timeout=settings.get("timeout", 900))

    def _unreachable(self, e):
        from den import platform

        return DenError(
            f"den broker not running at {self.base_url} ({e}); start it with: {platform.start_hint('den')}"
        )

    def status(self):
        return self._request("GET", "/status")

    def _stream(self, path, body):
        """Yield the broker's NDJSON progress lines until it's done; errors raise DenError."""
        with self._open("POST", path, body, timeout=None) as resp:
            for line in resp:
                msg = json.loads(line)
                if "error" in msg:
                    raise DenError(msg["error"])
                yield msg

    def switch_mode(self, mode, now=False):
        return self._stream("/mode", {"mode": mode, "now": now})

    def unload_sides(self, sides=(), now=False):
        """Unload without changing the mode; the next request loads its side again."""
        return self._stream("/unload", {"sides": list(sides), "now": now})

    def generate_image(self, **request):
        """Progress lines of one image request; the last one carries "result"."""
        return self._stream("/image", request)

    def _clips(self):
        """Raise unless this broker makes clips: an older one would take /clip for an LLM
        request and load a model (ADR 0009). Asked once per client."""
        if not getattr(self, "_clips_checked", False):
            if "clips" not in self.status():
                raise DenError(f"the broker at {self.base_url} predates clips; update den there and restart its broker")
            self._clips_checked = True

    def generate_clip(self, **request):
        """Start a clip, a detached request: {id, estimate_s, summary} at once."""
        self._clips()
        return self._request("POST", "/clip", request, timeout=300)

    def get_clip(self, clip_id, with_bytes=False):
        """A clip's state, and its result once done (with the files as bytes if asked)."""
        self._clips()
        return self._request("GET", f"/clip?id={int(clip_id)}" + ("&bytes=1" if with_bytes else ""), timeout=300)

    def clips(self):
        self._clips()
        return self._request("GET", "/clip")["clips"]

    def cancel_clip(self, clip_id):
        self._clips()
        return self._request("POST", "/clip/cancel", {"id": int(clip_id)})

    def speak(self, **request):
        """Progress lines of a voice job (ADR 0010); the last one carries "result"."""
        return self._stream("/voice", request)

    def transcribe(self, **request):
        """Progress lines of a transcription (Whisper); the last one carries the timed SRT."""
        return self._stream("/transcribe", request)

    def live_start(self, **request):
        """Progress lines of taking the machine for a live conversation; the last carries the session."""
        return self._stream("/live/start", request)

    def live_talk(self, say, wait_s=120, voice=None, language=None):
        """Speak say, then wait for the user's next utterance (ADR 0011); voice and language switch
        from this turn on."""
        body = {"say": say, "wait_s": wait_s, **({"voice": voice} if voice else {}), **({"language": language} if language else {})}
        return self._request("POST", "/live/talk", body, timeout=wait_s + 600)

    def standby_start(self, wake_word):
        """Progress lines of starting standby (ADR 0012); the last carries its state."""
        return self._stream("/standby/start", {"wake_word": wake_word})

    def standby_stop(self):
        return self._request("POST", "/standby/stop", {}, timeout=60)

    def standby(self):
        return self._request("GET", "/standby")

    def standby_wait(self, after=0, timeout_s=60):
        """Standby's state once it changed since seq after (the wake word heard, paused, back, off)."""
        return self._request("POST", "/standby/wait", {"after": after, "timeout_s": timeout_s}, timeout=timeout_s + 30)

    def live_stop(self):
        return self._request("POST", "/live/stop", {}, timeout=60)

    def live_keep(self, session, name):
        return self._request("POST", "/live/keep", {"session": session, "name": name}, timeout=30)

    def live_drop(self, session):
        return self._request("POST", "/live/drop", {"session": session}, timeout=30)

    def conversations(self):
        return self._request("GET", "/conversations", timeout=30)["conversations"]

    def conversation(self, ref):
        return self._request("GET", f"/conversations?{urllib.parse.urlencode({'id': ref})}", timeout=30)

    def delete_conversation(self, ref):
        return self._request("POST", "/conversations", {"id": ref, "remove": True}, timeout=30)

    def save_summary(self, ref, summary):
        return self._request("POST", "/conversations", {"id": ref, "summary": summary}, timeout=30)

    def voices(self):
        """The voice library there, the languages, and why speech can't run (or None)."""
        return self._request("GET", "/voices", timeout=30)

    def add_voice(self, name, recording, replace=False, description=None):
        """Keep a recording as a voice: its path here, or {name, base64} from elsewhere."""
        body = {"name": name, "recording": recording, "replace": replace, **({"description": description} if description else {})}
        return self._request("POST", "/voices", body, timeout=60)

    def design_voice(self, **request):
        """Progress lines of designing a voice from a description; the last one carries the result."""
        return self._stream("/voices/design", request)

    def voice(self, name, with_bytes=False):
        """One voice's details, and its sample with with_bytes."""
        query = urllib.parse.urlencode({"name": name, **({"bytes": 1} if with_bytes else {})})
        return self._request("GET", f"/voices?{query}", timeout=60)

    def save_draft(self, draft, name, replace=False):
        """Keep a designed draft voice in the library under name."""
        return self._request("POST", "/voices", {"draft": draft, "name": name, "replace": replace}, timeout=30)

    def rename_voice(self, name, new):
        return self._request("POST", "/voices", {"name": name, "rename_to": new}, timeout=30)

    def describe_voice(self, name, description):
        return self._request("POST", "/voices", {"name": name, "description": description}, timeout=30)

    def remove_voice(self, name):
        return self._request("POST", "/voices", {"name": name, "remove": True}, timeout=30)

    def save_pose(self, **request):
        """Progress lines of drawing and saving a pose; the last one carries "result".

        With draw_only=True the broker only draws the map and hands it back as bytes, saving
        nothing, so a caller can show it before a second call keeps it.
        """
        return self._stream("/pose", request)

    # --- what a client on another machine uses (ADR 0007) ---

    def client_info(self):
        """The mode, the model, the enabled tasks and the image spec of the broker's machine."""
        return self._request("GET", "/client", timeout=30)

    def delegate(self, task, instructions, text=None, files=(), timeout=900):
        """Run a task there, logged there; files are [{"path", "content"}] read here."""
        body = {"task": task, "instructions": instructions, "text": text, "files": list(files)}
        # The answer comes once the model is done, which can take minutes; allow for the load.
        return self._request("POST", "/delegate", body, timeout=timeout + 120)

    def feedback(self, call_id, verdict, note=""):
        return self._request("POST", "/feedback", {"id": call_id, "verdict": verdict, "note": note})

    def skills(self):
        """Which skills the broker's den has: name, description and their references."""
        return self._request("GET", "/skills", timeout=30)["skills"]

    def skill(self, name, reference=None):
        """One skill's text, or one of its references, to load into a conversation."""
        query = {"name": name, **({"reference": reference} if reference else {})}
        return self._request("GET", f"/skill?{urllib.parse.urlencode(query)}", timeout=30)

    def poses(self, name=None):
        """The pose library there, without its files: the table of contents, or one pose with
        small copies of its images."""
        query = f"?{urllib.parse.urlencode({'name': name})}" if name else ""
        return self._request("GET", f"/poses{query}", timeout=30)


def broker(config, caller):
    return BrokerClient(broker_url(config), caller)


def read_files(files):
    """[{"path", "content"}] of the files a delegation takes, read where the caller runs.

    A remote client reads them itself and sends the contents, since the paths are its own
    (ADR 0007); the broker's machine never sees the files, only what they say.
    """
    read = []
    for f in files:
        path = Path(f).expanduser()
        try:
            content = path.read_bytes().decode("utf-8", errors="replace")
        except OSError as e:
            raise DenError(f"cannot read {path}: {e}") from e
        read.append({"path": str(path), "content": content})
    return read


def build_prompt(instructions, text=None, files=()):
    """files: [{"path", "content"}], as read_files returns them."""
    parts = [instructions.strip()]
    if text:
        parts.append(f"<input>\n{text}\n</input>")
    for f in files:
        parts.append(f'<file path="{f["path"]}">\n{f["content"]}\n</file>')
    return "\n\n".join(parts)


def run_task(config, state, task, instructions, text=None, files=(), caller="cli", contents=None):
    """Run one delegated task on the active LLM model. Returns (answer, stats).

    files are paths read here; contents are files a remote client already read, as
    [{"path", "content"}].
    """
    unavailable = llm_unavailable(config, state)
    if unavailable:
        raise DenError(unavailable)
    tasks = enabled_tasks(config, state)
    if task not in tasks:
        raise DenError(f"task {task!r} is not enabled; enabled: {', '.join(tasks) or 'none'}")
    model = llm_model(state)
    llm = config["llm"]

    prompt = build_prompt(instructions, text, read_files(files) + list(contents or ()))
    num_ctx = llm.get("num_ctx", 8192)
    est_tokens = (len(prompt.encode()) + len(tasks[task]["system"].encode())) // BYTES_PER_TOKEN
    if est_tokens > num_ctx - OUTPUT_RESERVE_TOKENS:
        raise DenError(
            f"input is ~{est_tokens} tokens but num_ctx is {num_ctx}; "
            "split the input or raise num_ctx in config.toml"
        )

    began = time.time()
    resp = broker(config, caller).chat(model, llm, tasks[task]["system"], prompt)
    usage = resp.get("usage") or {}
    stats = {
        "task": task,
        "input_bytes": len(prompt.encode()),
        "model": model,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "seconds": round(time.time() - began, 1),
    }
    return resp["choices"][0]["message"].get("content") or "", stats


def _locked_log(update):
    """Run update(entries) -> new entry or None under an exclusive lock, appending its result."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        entry = update([json.loads(line) for line in f if line.strip()])
        if entry is not None:
            f.write(json.dumps(entry) + "\n")
        return entry


def read_log():
    try:
        with open(LOG_PATH) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def log_delegation(stats):
    """Record a completed delegation; returns its id for later feedback."""

    def add(entries):
        next_id = 1 + max((e["id"] for e in entries if e["type"] == "call"), default=0)
        return {"type": "call", "id": next_id, "ts": int(time.time()), **stats}

    return _locked_log(add)["id"]


def record_feedback(call_id, verdict, note=""):
    try:
        call_id = int(call_id)
    except (TypeError, ValueError):
        raise DenError(f"delegation id must be a number, got {call_id!r}") from None
    if verdict not in VERDICTS:
        raise DenError(f"unknown verdict {verdict!r}; use one of: {', '.join(VERDICTS)}")

    def add(entries):
        if not any(e["type"] == "call" and e["id"] == call_id for e in entries):
            raise DenError(f"no delegation with id {call_id} in {LOG_PATH}")
        return {"type": "feedback", "id": call_id, "ts": int(time.time()), "verdict": verdict, "note": note}

    _locked_log(add)


SUMMARY_INSTRUCTIONS = (
    "Summarize this spoken conversation between the user and Claude: what it was about, what was "
    "decided or agreed, open questions, and anything the user asked to be done. Short bullet points; "
    "keep names, numbers and decisions exact; nothing that isn't in the transcript."
)


def summarize_conversation(config, ref, caller="cli"):
    """A conversation's summary by den's own LLM (the summarize task), saved beside it. Returns it."""
    client = broker(config, caller)
    text = client.conversation(ref)["transcript"]
    if not text.strip():
        raise DenError(f"conversation {ref} has nothing in it to summarize")
    answer, stats = run_task(config, load_state(config), "summarize", SUMMARY_INSTRUCTIONS, text=text, caller=caller)
    client.save_summary(ref, answer)
    return answer, stats
