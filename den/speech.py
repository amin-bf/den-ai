"""Speech: voice-overs from a script, in voices cloned from a recording (docs/adr/0010-voice-overs.md).

The speech model (Chatterbox Multilingual) runs in a venv of its own that setup.sh makes, as
speech/server.py on a private UNIX socket. It belongs to the image side: the broker starts it for a
voice job, after asking ComfyUI to free its models, and stops it when the job is done. This file
is den's side of it: the server's process, SRT scripts, the voice library and the voice track.
"""

import array
import base64
import fcntl
import io
import json
import os
import re
import shutil
import signal
import subprocess
import time
import wave
from pathlib import Path

from den import core, platform
from den.core import DenError
from den.llm import UnixHTTPConnection

_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
SPEECH_DIR = Path(os.environ.get("SPEECH_DIR") or _DATA_HOME / "den/speech")
PYTHON = SPEECH_DIR / ".venv/bin/python"
SERVER = core.ROOT / "speech/server.py"
SOCKET = Path(os.environ.get("DEN_SPEECH_SOCKET") or platform.runtime_dir() / "den/speech.sock")
SERVER_LOG = core._STATE_HOME / "den/speech-server.log"
VOICES_DIR = Path(os.environ.get("DEN_VOICES") or _DATA_HOME / "den/voices")
OUTPUT_DIR = Path(os.environ.get("DEN_SPEECH_OUT") or Path.home() / "Music/den")
LOG_PATH = Path(os.environ.get("DEN_SPEECH_LOG", core._STATE_HOME / "den/speech.jsonl"))
# The first start downloads the model (about 3.2 GB); later ones load it in seconds.
START_TIMEOUT_S = 1800
STOP_TIMEOUT_S = 20
LINE_TIMEOUT_S = 300
LINE_GAP_S = 0.4  # between lines spoken in turn, when a script gives no times
AUDIO_SUFFIXES = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac")
LANGUAGES = {
    "ar": "Arabic", "da": "Danish", "de": "German", "el": "Greek", "en": "English", "es": "Spanish",
    "fi": "Finnish", "fr": "French", "he": "Hebrew", "hi": "Hindi", "it": "Italian", "ja": "Japanese",
    "ko": "Korean", "ms": "Malay", "nl": "Dutch", "no": "Norwegian", "pl": "Polish", "pt": "Portuguese",
    "ru": "Russian", "sv": "Swedish", "sw": "Swahili", "tr": "Turkish", "zh": "Chinese",
}
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def unavailable():
    """Why speech can't run here, or None."""
    if not PYTHON.exists():
        return f"speech isn't installed ({PYTHON} is missing); run ./setup.sh"
    if not SERVER.is_file():
        return f"speech server missing: {SERVER}"
    return None


# --- the server process ---


class Server:
    """The speech server, if one is running. Started per voice job; it loads in seconds."""

    def __init__(self):
        self.proc = None
        self.log = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _call(self, method, path, body=None, timeout=10):
        conn = UnixHTTPConnection(SOCKET, timeout=timeout)
        try:
            data = None if body is None else json.dumps(body).encode()
            conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, resp.getheader("Content-Type") or "", resp.read()
        finally:
            conn.close()

    def start(self, emit):
        why = unavailable()
        if why:
            raise DenError(why)
        if self.running():
            return
        SOCKET.parent.mkdir(parents=True, exist_ok=True)
        SOCKET.parent.chmod(0o700)
        SOCKET.unlink(missing_ok=True)
        SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
        emit({"starting": "speech model (Chatterbox)"})
        self.log = open(SERVER_LOG, "w")
        self.proc = subprocess.Popen(
            [str(PYTHON), str(SERVER), "--socket", str(SOCKET)],
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + START_TIMEOUT_S
        while True:
            if not self.running():
                self.stop()
                raise DenError(f"the speech server exited while loading; see {SERVER_LOG}")
            try:
                status, _, raw = self._call("GET", "/health", timeout=5)
                health = json.loads(raw or b"{}")
                if health.get("error"):
                    self.stop()
                    raise DenError(f"the speech model didn't load: {health['error']}; see {SERVER_LOG}")
                if status == 200 and health.get("ready"):
                    return
            except (OSError, json.JSONDecodeError):
                pass
            if time.time() > deadline:
                self.stop()
                raise DenError(f"the speech model wasn't ready after {START_TIMEOUT_S}s; see {SERVER_LOG}")
            time.sleep(1)

    def speak(self, text, language, voice=None, options=None):
        """One line as WAV bytes."""
        body = {"text": text, "language": language, "voice": voice, **(options or {})}
        try:
            status, kind, raw = self._call("POST", "/speak", body, timeout=LINE_TIMEOUT_S)
        except OSError as e:
            raise DenError(f"the speech server didn't answer: {e}; see {SERVER_LOG}") from e
        if status != 200 or not kind.startswith("audio/"):
            try:
                message = json.loads(raw).get("error")
            except (json.JSONDecodeError, AttributeError):
                message = raw[:200].decode(errors="replace")
            raise DenError(f"speech failed: {message}")
        return raw

    def convert(self, recording):
        """A recording in any format as a WAV like the spoken lines."""
        status, kind, raw = self._call("POST", "/convert", {"audio": str(recording)}, timeout=LINE_TIMEOUT_S)
        if status != 200 or not kind.startswith("audio/"):
            raise DenError(f"couldn't read the recording: {_error(raw)}")
        return raw

    def transcribe(self, recording, language=None):
        """{segments: [{start, end, text}], text, duration} of a recording's speech (Whisper)."""
        body = {"audio": str(recording), **({"language": language} if language else {})}
        try:
            # The first call downloads Whisper (about 1.6 GB) and loads it.
            status, _, raw = self._call("POST", "/transcribe", body, timeout=START_TIMEOUT_S)
        except OSError as e:
            raise DenError(f"the speech server didn't answer: {e}; see {SERVER_LOG}") from e
        if status != 200:
            raise DenError(f"transcription failed: {_error(raw)}")
        return json.loads(raw)

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        self.proc = None
        if self.log:
            self.log.close()
            self.log = None
        SOCKET.unlink(missing_ok=True)


def _error(raw):
    try:
        return json.loads(raw).get("error")
    except (json.JSONDecodeError, AttributeError):
        return raw[:200].decode(errors="replace")


# --- scripts ---


_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def _seconds(stamp):
    h, m, s, ms = _TIME.fullmatch(stamp.strip()).groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text):
    """[{start, end, text}] from an SRT script, in time order. Markup like <i> is dropped."""
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip().lstrip("﻿")):
        lines = [line.strip() for line in block.strip().splitlines()]
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            continue
        start, _, end = lines[timing].partition("-->")
        end = end.strip().split()[0] if end.strip() else ""
        try:
            cue = {"start": _seconds(start), "end": _seconds(end)}
        except AttributeError:
            raise DenError(f"not an SRT time: {lines[timing]!r}") from None
        spoken = re.sub(r"<[^>]+>|\{[^}]+\}", "", " ".join(lines[timing + 1 :])).strip()
        if spoken:
            cues.append({**cue, "text": spoken})
    if not cues:
        raise DenError("the script has no timed lines (an SRT file: number, 00:00:01,000 --> 00:00:03,000, text)")
    return sorted(cues, key=lambda c: c["start"])


def script(srt=None, text=None, lines=None):
    """The cues to speak: an SRT script (its text, or the absolute path of an .srt file), lines
    spoken in turn with no times given (den times them and writes the SRT), or one text spoken
    from the start."""
    if sum(bool(x) for x in (srt, text, lines)) != 1:
        raise DenError("give one of: an SRT script, lines to speak in turn, or a text")
    if lines:
        if isinstance(lines, str):
            lines = lines.splitlines()
        spoken = [str(line).strip() for line in lines if str(line).strip()]
        if not spoken:
            raise DenError("the lines are empty")
        return [{"start": None, "end": None, "text": line} for line in spoken]
    if srt and "\n" not in srt and srt.strip().lower().endswith(".srt"):
        path = Path(srt.strip()).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise DenError(f"no SRT file at {srt} (an absolute path)")
        srt = path.read_text(errors="replace")
    return parse_srt(srt) if srt else [{"start": 0.0, "end": None, "text": text.strip()}]


# --- what a tool offers ---


def spoken_properties():
    """The schema of what to say and how: shared by the voice tool and a clip's voice-over."""
    names = sorted(voices())
    return {
        "srt": {
            "type": "string",
            "description": "An SRT script, each line spoken at its time: its text, or the absolute path of an .srt file.",
        },
        "lines": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Or lines to speak in turn, with no times: den times them from the speech and returns the SRT.",
        },
        "text": {"type": "string", "description": "Or one text to speak from the start."},
        "audio": {
            "type": "string",
            "description": "Or a recording's absolute path, used as it is instead of speaking a script "
            "(e.g. the user's own narration); a script alongside only names its lines.",
        },
        "voice": {
            "type": "string",
            "description": "A voice from the library"
            + (f" ({', '.join(names)})" if names else " (none yet: den voice --add NAME RECORDING)")
            + ", or the absolute path of a recording to clone. Default: the model's own voice.",
        },
        "language": {"type": "string", "enum": sorted(LANGUAGES), "description": "Language of the text. Default: en."},
    }


def request_spec():
    """The voice tool's description and JSON schema."""
    return {
        "description": (
            "Speak a text, or an SRT script with each line at its time, in a voice cloned from a "
            "recording (Chatterbox Multilingual, 23 languages), and save it as one WAV track: a "
            "voice-over. For a voice-over on a clip, pass it to generate_clip as voiceover instead. "
            "Give each SRT line time to be said, about 2.5 words a second: a line that runs long "
            "pushes the next one later, and the result lists each one as a note. You can't hear the "
            "result; ask the user how it sounds. The den-voice skill has the recipes (recording a voice, "
            "scripts that fit their times, the settings)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                **spoken_properties(),
                "exaggeration": {"type": "number", "description": "Expressiveness, 0.25–2. Default 0.5; higher is more dramatic."},
                "cfg_weight": {"type": "number", "description": "Pacing, 0–1. Default 0.5; lower is slower and calmer."},
                "seed": {"type": "integer", "description": "Reuse one to change a single thing between takes."},
                "out": {"type": "string", "description": "Also copy the track here (a file, or a folder ending in /)."},
            },
        },
    }


# --- voices ---


def voices():
    """{name: recording path} in the voice library."""
    if not VOICES_DIR.is_dir():
        return {}
    return {p.stem: p for p in sorted(VOICES_DIR.iterdir()) if p.suffix.lower() in AUDIO_SUFFIXES}


def voice_path(voice):
    """A voice's recording: a name from the library, or an absolute path. None for the model's own."""
    if not voice:
        return None
    known = voices()
    if voice in known:
        return str(known[voice])
    path = Path(voice).expanduser()
    if path.is_absolute() and path.is_file():
        return str(path)
    raise DenError(
        f"unknown voice {voice!r}; "
        + (f"voices: {', '.join(known)}" if known else "no voices yet: den voice add NAME RECORDING")
    )


def add_voice(name, recording, replace=False):
    """Keep a recording in the library under name; returns its path there."""
    if not _NAME.fullmatch(name):
        raise DenError(f"a voice name is lower case letters, digits and dashes, got {name!r}")
    source = Path(recording).expanduser()
    if not source.is_file():
        raise DenError(f"no recording at {source}")
    if source.suffix.lower() not in AUDIO_SUFFIXES:
        raise DenError(f"not an audio file den knows ({', '.join(AUDIO_SUFFIXES)}): {source.name}")
    existing = voices().get(name)
    if existing and not replace:
        raise DenError(f"voice {name!r} exists; replace it with --replace")
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    if existing:
        existing.unlink()
    target = VOICES_DIR / f"{name}{source.suffix.lower()}"
    shutil.copyfile(source, target)
    return target


def remove_voice(name):
    path = voices().get(name)
    if not path:
        raise DenError(f"no voice {name!r}")
    path.unlink()


# --- the track ---


def _pcm(data):
    """(samples, rate) of a 16-bit mono WAV."""
    with wave.open(io.BytesIO(data)) as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise DenError("the speech server returned audio that isn't 16-bit mono")
        samples = array.array("h")
        samples.frombytes(w.readframes(w.getnframes()))
        return samples, w.getframerate()


def assemble(cues, lines):
    """One WAV from the spoken lines, each at its cue's start. A line that runs into the next
    pushes it later rather than overlapping it. Returns (wav bytes, seconds, notes)."""
    track, rate, notes = array.array("h"), None, []
    for i, (cue, data) in enumerate(zip(cues, lines), 1):
        samples, line_rate = _pcm(data)
        if rate is None:
            rate = line_rate
        elif line_rate != rate:
            raise DenError("the spoken lines came back at different sample rates")
        if cue["start"] is None:
            # Lines without times follow each other, a breath apart; the SRT records where they fell.
            cue["start"] = round(len(track) / rate + (LINE_GAP_S if track else 0.3), 3)
            cue["end"] = round(cue["start"] + len(samples) / rate, 3)
        at = round(cue["start"] * rate)
        if len(track) > at:
            notes.append(f"line {i} starts {(len(track) - at) / rate:.1f}s late: the line before ran long")
        else:
            track.extend(array.array("h", bytes(2 * (at - len(track)))))
        track.extend(samples)
        if cue.get("end") is not None:
            over = len(track) / rate - cue["end"]
            if over > 0.25:
                notes.append(f"line {i} runs {over:.1f}s past its end ({cue['end']:g}s)")
    last_end = max((c["end"] for c in cues if c.get("end") is not None), default=0)
    if len(track) < round(last_end * rate):
        track.extend(array.array("h", bytes(2 * (round(last_end * rate) - len(track)))))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(track.tobytes())
    return out.getvalue(), round(len(track) / rate, 2), notes


def pad(data, seconds):
    """A WAV with silence added at the end to last `seconds`, or as it is when it's longer."""
    samples, rate = _pcm(data)
    missing = round(seconds * rate) - len(samples)
    if missing > 0:
        samples.extend(array.array("h", bytes(2 * missing)))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return out.getvalue()


def _stamp(seconds):
    ms = round(seconds * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def to_srt(cues):
    """An SRT script of the cues, at their times."""
    return "\n".join(
        f"{i}\n{_stamp(c['start'])} --> {_stamp(c['end'] if c.get('end') is not None else c['start'] + 2)}\n{c['text']}\n"
        for i, c in enumerate(cues, 1)
    )


def slug(text, length=40):
    words = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return words[:length].rstrip("-") or "voice"


def save(data, text, out=None):
    """Write the track to OUTPUT_DIR/YYYY-MM-DD/<time>-<slug>.wav; copy it to out if given."""
    day = OUTPUT_DIR / time.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    stem = f"{time.strftime('%H%M%S')}-{slug(text)}"
    n, path = 0, None
    while path is None or path.exists():
        path = day / f"{stem}{f'-{n + 1}' if n else ''}.wav"
        n += 1
    path.write_bytes(data)
    copies = []
    if out:
        target = Path(out).expanduser()
        if str(out).endswith("/") or target.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            target = target / path.name
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        copies.append(target)
    return path, copies


def as_bytes(path):
    return base64.b64encode(Path(path).read_bytes()).decode()


def log(entry):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps({"ts": int(time.time()), **entry}) + "\n")
