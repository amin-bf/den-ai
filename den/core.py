"""Shared core of the local AI toolchain: config, runtime state, and backend clients.

config.toml is hand-edited (LLM settings, tasks, endpoints); state.json is written by the
broker on mode switches and by the `den` CLI (active model, task toggles). The broker,
CLI and MCP server read them fresh on every use, so a switch takes effect without restarts.
Clients reach Ollama only through the broker (`den serve`, see broker.py).
"""

import fcntl
import json
import os
import re
import time
import tomllib
import urllib.error
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


def broker_url(config):
    return config.get("broker", {}).get("base_url", "http://127.0.0.1:11435")


# A local model doesn't only hold the GPU: an MoE split across GPU and RAM keeps several GB of
# RAM and generates on the CPU too. So den refuses to *load* a side while the machine is
# already busy with other work; a side that is loaded keeps serving ([limits], ADR 0004).
DEFAULT_MAX_LOAD_PER_CPU = 0.8
DEFAULT_MIN_FREE_RAM_GB = 4.0
MEMINFO_PATH = Path("/proc/meminfo")


def cpus():
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:  # not Linux
        return os.cpu_count() or 1


def free_ram_gb():
    """MemAvailable in GB — what a new model could take without swapping; None if unreadable."""
    try:
        for line in MEMINFO_PATH.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024 / 1e9
    except (OSError, IndexError, ValueError):
        return None
    return None


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
    """Ollama's native API. The broker uses it upstream; clients use BrokerClient."""

    def __init__(self, base_url, caller=None):
        self.base_url = base_url.rstrip("/")
        self.caller = caller

    def _unreachable(self, e):
        return DenError(
            f"ollama is not reachable at {self.base_url} ({e}); start it with: sudo systemctl start ollama"
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

    def keep_loaded(self, model, keep_alive):
        """Set how long a loaded model stays in memory (loads it if it isn't loaded)."""
        self._request("POST", "/api/generate", {"model": model, "keep_alive": keep_alive}, timeout=60)

    def unload(self, model):
        self._request("POST", "/api/generate", {"model": model, "keep_alive": 0}, timeout=60)

    def chat(self, model, settings, system, user, keep_alive):
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "keep_alive": keep_alive,
            "options": {"num_ctx": settings.get("num_ctx", 8192)},
        }
        if "think" in settings:
            body["think"] = settings["think"]
        return self._request("POST", "/api/chat", body, timeout=settings.get("timeout", 900))


class BrokerClient(Ollama):
    """Ollama's API as passed through by the broker, plus the broker's own endpoints."""

    def _unreachable(self, e):
        return DenError(
            f"den broker not running at {self.base_url} ({e}); start it with: systemctl --user start den"
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


def broker(config, caller):
    return BrokerClient(broker_url(config), caller)


def build_prompt(instructions, text=None, files=()):
    parts = [instructions.strip()]
    if text:
        parts.append(f"<input>\n{text}\n</input>")
    for f in files:
        path = Path(f).expanduser()
        try:
            content = path.read_bytes().decode("utf-8", errors="replace")
        except OSError as e:
            raise DenError(f"cannot read {path}: {e}") from e
        parts.append(f'<file path="{path}">\n{content}\n</file>')
    return "\n\n".join(parts)


def run_task(config, state, task, instructions, text=None, files=(), caller="cli"):
    """Run one delegated task on the active LLM model. Returns (answer, stats)."""
    unavailable = llm_unavailable(config, state)
    if unavailable:
        raise DenError(unavailable)
    tasks = enabled_tasks(config, state)
    if task not in tasks:
        raise DenError(f"task {task!r} is not enabled; enabled: {', '.join(tasks) or 'none'}")
    model = llm_model(state)
    llm = config["llm"]

    prompt = build_prompt(instructions, text, files)
    num_ctx = llm.get("num_ctx", 8192)
    est_tokens = (len(prompt.encode()) + len(tasks[task]["system"].encode())) // BYTES_PER_TOKEN
    if est_tokens > num_ctx - OUTPUT_RESERVE_TOKENS:
        raise DenError(
            f"input is ~{est_tokens} tokens but num_ctx is {num_ctx}; "
            "split the input or raise num_ctx in config.toml"
        )

    resp = broker(config, caller).chat(model, llm, tasks[task]["system"], prompt, llm.get("keep_alive", "10m"))
    stats = {
        "task": task,
        "input_bytes": len(prompt.encode()),
        "model": model,
        "input_tokens": resp.get("prompt_eval_count"),
        "output_tokens": resp.get("eval_count"),
        "seconds": round(resp.get("total_duration", 0) / 1e9, 1),
    }
    return resp["message"]["content"], stats


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
