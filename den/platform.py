"""What differs between the systems den runs on: services, the runtime folder, free RAM.

den is otherwise the same everywhere — the broker, the swaps, the delegation and the whole
image side are plain Python over HTTP. Three things are not portable: how a background
service is started and stopped (systemd user units, or launchd agents), where a private
socket may live, and how to ask what RAM is still free. They are gathered here so the rest
of den keeps one code path, and so a third system means adding a branch in one file
([ADR running-on-macos](../docs/adr/0006-running-on-macos.md)).

The hints in error messages come from here too: telling someone to run `systemctl` on a
machine that has no systemd is worse than saying nothing.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from den import core
from den.core import DenError

MACOS = sys.platform == "darwin"

# launchd agents are named by a reverse-DNS label; config.toml and systemd name a service
# plainly ("comfyui"), so the label is built from it. The broker is `den.service` there,
# which would read as ai.den.den here, so it is named for what it is.
LABEL_PREFIX = "ai.den"
LABEL_NAMES = {"den": "broker"}
SERVICE_TIMEOUT_S = 30


def label(service):
    """The launchd label for a service named as config.toml and systemd name it."""
    return f"{LABEL_PREFIX}.{LABEL_NAMES.get(service, service)}"


def agent_dir():
    return Path.home() / "Library/LaunchAgents"


def agent_plist(service):
    return agent_dir() / f"{label(service)}.plist"


def runtime_dir():
    """A folder only this user can reach, for the LLM's socket.

    Linux has one per session ($XDG_RUNTIME_DIR, /run/user/<uid>). macOS has no equivalent,
    but $TMPDIR there is already per-user and mode 700, and is short enough for a socket
    path: a UNIX socket path is capped at ~104 bytes, well under which $TMPDIR/den/llm.sock
    stays.
    """
    if runtime := os.environ.get("XDG_RUNTIME_DIR"):
        return Path(runtime)
    if MACOS:
        if tmp := os.environ.get("TMPDIR"):
            return Path(tmp)
        return Path("/tmp")
    return Path(f"/run/user/{os.getuid()}")


# --- free RAM -------------------------------------------------------------------------

MEMINFO_PATH = Path("/proc/meminfo")
# vm_stat prints counts of pages; these are the ones a new model could use without pushing
# anything out to swap — free, plus what the system would reclaim before it swapped.
_MACOS_FREE_PAGES = ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")


def _linux_free_ram_gb():
    for line in MEMINFO_PATH.read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024 / 1e9
    return None


def _macos_free_ram_gb():
    """The same idea as MemAvailable, from vm_stat: free and reclaimable pages.

    There is no single number for it on macOS. Free pages alone read far too low, because
    the system keeps using memory it would hand back at once; adding the inactive,
    speculative and purgeable pages gives what a model could take before anything swaps.
    """
    out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10).stdout
    if not (size := re.search(r"page size of (\d+) bytes", out)):
        return None
    pages = 0
    found = False
    for line in out.splitlines():
        name, _, count = line.partition(":")
        if name.strip() in _MACOS_FREE_PAGES and count.strip():
            pages += int(count.strip().rstrip("."))
            found = True
    return pages * int(size.group(1)) / 1e9 if found else None


def free_ram_gb():
    """RAM a new model could take without swapping, in GB; None when it can't be read.

    On a machine whose GPU shares this memory the number matters more, not less: the model
    is not off in a card's own memory, it is in this.
    """
    try:
        return _macos_free_ram_gb() if MACOS else _linux_free_ram_gb()
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


# --- free VRAM -------------------------------------------------------------------------


def free_vram_mb():
    """Free memory on the first discrete GPU, in MB, via nvidia-smi; None when there is none
    to ask (macOS has no separate VRAM to poll — its GPU shares free_ram_gb's memory — and a
    machine without nvidia-smi answers the same way).
    """
    if MACOS:
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return int(out.stdout.splitlines()[0].strip())
    except (IndexError, ValueError):
        return None


# --- services -------------------------------------------------------------------------


def _run(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=SERVICE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DenError(f"{' '.join(argv)} failed: {e}") from e


def _systemctl(action, service):
    result = _run(["systemctl", "--user", action, service])
    if result.returncode != 0:
        raise DenError(f"systemctl --user {action} {service} failed: {result.stderr.strip()}")


def _domain():
    return f"gui/{os.getuid()}"


def _launchctl_loaded(service):
    """Whether launchd has the agent loaded at all — running or not."""
    return _run(["launchctl", "print", f"{_domain()}/{label(service)}"]).returncode == 0


def _launchctl_running(service):
    """Whether launchd has the agent loaded and its process hasn't died: running, or still
    on its way there.

    `launchctl print` prints a job's state; a job that exited stays loaded, so the state is
    what separates a running ComfyUI from one that died on startup. But right after
    `bootstrap` the state isn't `running` yet either (`spawn scheduled`, for one), and taking
    that for a death failed every cold start while ComfyUI came up fine behind it. So only
    `not running` after an exit counts as stopped. When the output carries no state, being
    loaded is all den can tell.
    """
    result = _run(["launchctl", "print", f"{_domain()}/{label(service)}"])
    if result.returncode != 0:
        return False
    state = re.search(r"^\s*state\s*=\s*(.+?)\s*$", result.stdout, re.MULTILINE)
    exited = re.search(r"^\s*last exit code\s*=\s*(.+?)\s*$", result.stdout, re.MULTILINE)
    if state and state.group(1) == "not running":
        return not exited or exited.group(1) == "(never exited)"
    return True


def _launchctl_start(service):
    plist = agent_plist(service)
    if not plist.is_file():
        raise DenError(f"no launchd agent for {service} at {plist}; run setup.sh")
    # An agent already loaded is started again with kickstart; one that isn't is loaded,
    # which starts it (the plists den writes have RunAtLoad).
    if _launchctl_loaded(service):
        result = _run(["launchctl", "kickstart", f"{_domain()}/{label(service)}"])
    else:
        result = _run(["launchctl", "bootstrap", _domain(), str(plist)])
    if result.returncode != 0:
        raise DenError(f"starting {service} failed: {(result.stderr or result.stdout).strip()}")
    # bootstrap returns before the job shows up, and a caller that asks whether it is running
    # in that window would be told no and give up on a service that is only just starting.
    deadline = time.time() + SERVICE_TIMEOUT_S
    while not _launchctl_loaded(service):
        if time.time() > deadline:
            raise DenError(f"{service} didn't load {SERVICE_TIMEOUT_S}s after starting it")
        time.sleep(0.5)


def _launchctl_stop(service):
    result = _run(["launchctl", "bootout", f"{_domain()}/{label(service)}"])
    # Booting out something already gone is what was wanted, not a failure.
    if result.returncode != 0 and "No such process" not in (result.stderr or ""):
        raise DenError(f"stopping {service} failed: {(result.stderr or result.stdout).strip()}")
    # bootout returns before launchd has finished tearing the job down, and bootstrapping
    # one that is still going fails. systemd's restart is one step; here the wait is ours.
    deadline = time.time() + SERVICE_TIMEOUT_S
    while _launchctl_loaded(service):
        if time.time() > deadline:
            raise DenError(f"{service} was still loaded {SERVICE_TIMEOUT_S}s after booting it out")
        time.sleep(0.5)


def start_service(service):
    _launchctl_start(service) if MACOS else _systemctl("start", service)


def stop_service(service):
    _launchctl_stop(service) if MACOS else _systemctl("stop", service)


def check_running(service):
    """Raise DenError when the service isn't running — how a start loop notices it died."""
    if MACOS:
        if not _launchctl_running(service):
            raise DenError(f"{service} is not running")
    else:
        _systemctl("is-active", service)


# --- what to tell someone -------------------------------------------------------------


def service_log(service):
    """The file a launchd agent writes its output to.

    systemd collects a unit's output itself, so this is only for launchd, whose agents write
    where their plist says and nowhere else. It sits with den's other state, next to
    llama-server's log.
    """
    return core._STATE_HOME / f"den/{service}.log"


def logs_hint(service):
    """Where this system keeps a service's output."""
    if MACOS:
        return f"see: {service_log(service)}"
    return f"see: journalctl --user -u {service}"


def start_hint(service):
    if MACOS:
        return f"launchctl kickstart {_domain()}/{label(service)}"
    return f"systemctl --user start {service}"


def restart_hint(service):
    if MACOS:
        return f"launchctl kickstart -k {_domain()}/{label(service)}"
    return f"systemctl --user restart {service}"


def llama_install_hint():
    """How this system installs llama-server. The binary is the same either way; what differs
    is which backend it was built against — Metal where the GPU shares the machine's memory,
    CUDA where it doesn't."""
    return "brew install llama.cpp" if MACOS else "e.g. pacman -S llama-cpp ggml-cuda"


def ollama_start_hint():
    """Ollama is the one service den never starts itself; it belongs to whoever runs the machine.

    On macOS it is the user's own (Homebrew's services, or the app), so no sudo is involved.
    """
    return "brew services start ollama" if MACOS else "sudo systemctl start ollama"
