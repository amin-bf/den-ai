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
DESIGN_DIR = Path(os.environ.get("DESIGN_DIR") or _DATA_HOME / "den/voice-design")
DRAFTS_KEPT = 20  # designed voices waiting to be listened to; older ones are dropped
DESIGNER = core.ROOT / "speech/design.py"
DESIGN_LOG = core._STATE_HOME / "den/voice-design.log"
DESIGN_TIMEOUT_S = 1800  # the first run downloads the model (about 4.5 GB)
# The languages the voice designer speaks, and a sample of about ten seconds with varied sounds in
# each: the sample is what Chatterbox clones, so it only needs to show the voice.
DESIGN_LANGUAGES = {
    "en": ("English", "Every morning I walk down to the harbour, buy a coffee from the little stand by the boats, "
           "and watch the fishermen come in. Some days the sea is calm and silver; other days the wind throws spray over the wall."),
    "de": ("German", "Jeden Morgen gehe ich hinunter zum Hafen, hole mir einen Kaffee am kleinen Stand bei den Booten "
           "und sehe den Fischern zu. Manchmal ist das Meer ruhig und silbern, manchmal wirft der Wind die Gischt über die Mauer."),
    "fr": ("French", "Chaque matin, je descends au port, j'achète un café au petit stand près des bateaux et je regarde "
           "rentrer les pêcheurs. Certains jours la mer est calme et argentée, d'autres le vent jette l'écume par-dessus le mur."),
    "es": ("Spanish", "Cada mañana bajo al puerto, compro un café en el puesto junto a los barcos y miro llegar a los "
           "pescadores. Algunos días el mar está tranquilo y plateado; otros, el viento lanza la espuma sobre el muro."),
    "it": ("Italian", "Ogni mattina scendo al porto, prendo un caffè al chiosco vicino alle barche e guardo rientrare i "
           "pescatori. Certi giorni il mare è calmo e argenteo, altri il vento getta gli spruzzi oltre il muro."),
    "pt": ("Portuguese", "Todas as manhãs desço ao porto, compro um café na banca junto aos barcos e vejo os pescadores "
           "chegar. Há dias em que o mar está calmo e prateado; noutros, o vento atira a espuma por cima do muro."),
    "ru": ("Russian", "Каждое утро я спускаюсь в гавань, беру кофе у маленького ларька возле лодок и смотрю, как "
           "возвращаются рыбаки. Иногда море спокойное и серебристое, а иногда ветер перебрасывает брызги через стену."),
    "ja": ("Japanese", "毎朝、港まで歩いて行き、船のそばの小さな店でコーヒーを買って、漁師たちが帰ってくるのを眺めます。"
           "海が穏やかで銀色に光る日もあれば、風がしぶきを堤防の上まで吹き上げる日もあります。"),
    "ko": ("Korean", "매일 아침 나는 항구로 내려가 배 옆의 작은 가게에서 커피를 사고 어부들이 돌아오는 것을 바라본다. "
           "어떤 날은 바다가 잔잔하고 은빛이며, 어떤 날은 바람이 물보라를 방파제 너머로 날린다."),
    "zh": ("Chinese", "每天早上，我走到港口，在船边的小摊买一杯咖啡，看着渔民们归来。有时大海平静而泛着银光，有时海风把浪花吹过堤岸。"),
}


def design_unavailable():
    """Why voices can't be designed here, or None."""
    if not (DESIGN_DIR / ".venv/bin/python").exists():
        return f"the voice designer isn't installed ({DESIGN_DIR}/.venv is missing); run ./setup.sh"
    return None


def design(description, language="en", seed=None, check=lambda: None):
    """A voice's sample from a description: WAV bytes, spoken by the voice designer (Qwen3-TTS
    VoiceDesign) in a process of its own. check() is called while it runs, to cancel it."""
    why = design_unavailable()
    if why:
        raise DenError(why)
    if language not in DESIGN_LANGUAGES:
        raise DenError(f"the voice designer speaks {', '.join(DESIGN_LANGUAGES)}; got {language!r}")
    name, sample = DESIGN_LANGUAGES[language]
    out = SOCKET.parent / f"design-{os.getpid()}-{time.time_ns()}.wav"
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    DESIGN_LOG.parent.mkdir(parents=True, exist_ok=True)
    args = [str(DESIGN_DIR / ".venv/bin/python"), str(DESIGNER), "--description", description,
            "--text", sample, "--language", name, "--out", str(out)]
    if seed is not None:
        args += ["--seed", str(int(seed))]
    with open(DESIGN_LOG, "w") as log:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + DESIGN_TIMEOUT_S
        try:
            while proc.poll() is None:
                check()
                if time.time() > deadline:
                    raise DenError(f"the voice designer took longer than {DESIGN_TIMEOUT_S}s; see {DESIGN_LOG}")
                time.sleep(1)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(10)
    if proc.returncode != 0 or not out.is_file():
        raise DenError(f"the voice designer failed; see {DESIGN_LOG}")
    data = out.read_bytes()
    out.unlink()
    return data


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


# --- live conversation (ADR 0011) ---

LIVE_ENGINE = core.ROOT / "speech/live.py"
LIVE_SOCKET = SOCKET.parent / "live.sock"
LIVE_LOG = core._STATE_HOME / "den/live-engine.log"
LIVE_DIR = core._STATE_HOME / "den/live"  # every session's transcript, until it's kept or dropped
CONVERSATIONS_DIR = Path(os.environ.get("DEN_CONVERSATIONS") or _DATA_HOME / "den/conversations")


class LiveEngine:
    """The live engine process (speech/live.py): microphone, voice and models, while live."""

    def __init__(self):
        self.proc = None
        self.log = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _call(self, method, path, body=None, timeout=10):
        conn = UnixHTTPConnection(LIVE_SOCKET, timeout=timeout)
        try:
            data = None if body is None else json.dumps(body).encode()
            conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")
        finally:
            conn.close()

    def start(self, voice_file, language, exaggeration, emit):
        why = unavailable()
        if why:
            raise DenError(why)
        LIVE_SOCKET.parent.mkdir(parents=True, exist_ok=True)
        LIVE_SOCKET.unlink(missing_ok=True)
        LIVE_LOG.parent.mkdir(parents=True, exist_ok=True)
        emit({"starting": "the live engine (microphone, Whisper, voice)"})
        args = [str(PYTHON), str(LIVE_ENGINE), "--socket", str(LIVE_SOCKET), "--language", language,
                "--exaggeration", str(exaggeration)]
        if voice_file:
            args += ["--voice", str(voice_file)]
        self.log = open(LIVE_LOG, "w")
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + START_TIMEOUT_S
        while True:
            if not self.running():
                self.stop()
                raise DenError(f"the live engine exited while loading; see {LIVE_LOG}")
            try:
                status, health = self._call("GET", "/health", timeout=5)
                if health.get("error"):
                    self.stop()
                    raise DenError(f"the live engine didn't load: {health['error']}; see {LIVE_LOG}")
                if health.get("ready"):
                    return
            except (OSError, json.JSONDecodeError):
                pass
            if time.time() > deadline:
                self.stop()
                raise DenError(f"the live engine wasn't ready after {START_TIMEOUT_S}s; see {LIVE_LOG}")
            time.sleep(1)

    def talk(self, say, wait_s, voice_file=None, language=None):
        body = {"say": say, "wait_s": wait_s, **({"voice": str(voice_file)} if voice_file else {}),
                **({"language": language} if language else {})}
        try:
            status, answer = self._call("POST", "/talk", body, timeout=wait_s + 300)
        except OSError as e:
            raise DenError(f"the live engine didn't answer: {e}; see {LIVE_LOG}") from e
        if status != 200:
            raise DenError(f"live: {answer.get('error')}")
        return answer

    def stop(self):
        if self.running():
            try:
                self._call("POST", "/stop", {}, timeout=10)
                self.proc.wait(15)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.send_signal(signal.SIGTERM)
                try:
                    self.proc.wait(15)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        self.proc = None
        if self.log:
            self.log.close()
            self.log = None
        LIVE_SOCKET.unlink(missing_ok=True)


def new_session():
    """A new session's id; its transcript is written turn by turn, so a crash loses nothing."""
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    return time.strftime("%Y%m%d-%H%M%S")  # every session is kept: it's text, and small


def record_turn(session, turn):
    with open(LIVE_DIR / f"{session}.jsonl", "a") as f:
        f.write(json.dumps({"t": round(time.time(), 1), **turn}, ensure_ascii=False) + "\n")


def session_turns(session):
    path = LIVE_DIR / f"{session}.jsonl"
    if not re.fullmatch(r"[0-9-]+", str(session)) or not path.is_file():
        raise DenError(f"no live session {session!r}")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def transcript(session):
    """A session as plain text: who said what, marking where the user cut Claude off."""
    lines = []
    for turn in session_turns(session):
        clock = time.strftime("%H:%M:%S", time.localtime(turn["t"]))
        if turn["who"] == "claude":
            said = " ".join(turn.get("spoken") or [])
            cut = f" [interrupted; not said: {' '.join(turn['unspoken'])}]" if turn.get("interrupted") else ""
            if said or cut:
                lines.append(f"[{clock}] Claude: {said}{cut}")
        elif turn["who"] == "user":
            lines.append(f"[{clock}] User: {turn['text']}")
        elif turn.get("event") == "switch":
            changed = ", ".join(f"{k} {turn[k]}" for k in ("voice", "language") if turn.get(k))
            lines.append(f"[{clock}] (switched to {changed})")
        # den's own entries (start, stop) mark the session, not the conversation.
    return "\n".join(lines)


def _conversation(ref):
    """(kind, path) of a conversation: a session not decided on yet (its id), or a kept one (its name)."""
    ref = str(ref or "")
    if re.fullmatch(r"[0-9]{8}-[0-9]{6}", ref) and (LIVE_DIR / f"{ref}.jsonl").is_file():
        return "session", LIVE_DIR / f"{ref}.jsonl"
    if _NAME.fullmatch(ref) and (CONVERSATIONS_DIR / f"{ref}.md").is_file():
        return "kept", CONVERSATIONS_DIR / f"{ref}.md"
    raise DenError(f"no conversation {ref!r}: a session id (20260924-212720) or a kept conversation's name")


def _summary_path(path):
    return path.with_name(path.stem + ".summary.md")


def conversations():
    """Every conversation, newest first: [{id, kind, when, turns, opening, summary}]. Sessions not
    decided on and kept conversations alike; a summary where one was made."""
    rows = []
    for path in LIVE_DIR.glob("*.jsonl") if LIVE_DIR.is_dir() else []:
        turns = [t for t in session_turns(path.stem) if t["who"] in ("user", "claude")]
        opening = next((t["text"] for t in turns if t["who"] == "user"), "")
        rows.append({"id": path.stem, "kind": "session", "when": path.stat().st_mtime, "turns": len(turns), "opening": opening[:120]})
    for path in CONVERSATIONS_DIR.glob("*.md") if CONVERSATIONS_DIR.is_dir() else []:
        if path.name.endswith(".summary.md"):
            continue
        text = path.read_text()
        opening = next((line.split("User: ", 1)[1] for line in text.splitlines() if "User: " in line), "")
        rows.append({"id": path.stem, "kind": "kept", "when": path.stat().st_mtime,
                     "turns": text.count("] Claude:") + text.count("] User:"), "opening": opening[:120]})
    for row in rows:
        path = _conversation(row["id"])[1]
        summary = _summary_path(path)
        row["summary"] = summary.read_text().strip()[:300] if summary.is_file() else None
        row["when"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["when"]))
    return sorted(rows, key=lambda r: r["when"], reverse=True)


def read_conversation(ref):
    """A conversation's transcript as text, with its summary first where there is one."""
    kind, path = _conversation(ref)
    text = transcript(path.stem) if kind == "session" else path.read_text()
    summary = _summary_path(path)
    return {"id": path.stem, "kind": kind, "transcript": text,
            "summary": summary.read_text().strip() if summary.is_file() else None}


def save_summary(ref, text):
    _, path = _conversation(ref)
    _summary_path(path).write_text(text.strip() + "\n")


def delete_conversation(ref):
    _, path = _conversation(ref)
    path.unlink()
    _summary_path(path).unlink(missing_ok=True)


def keep_session(session, name):
    """Keep a session under name: its transcript moves to the kept conversations, with its summary."""
    if not _NAME.fullmatch(name):
        raise DenError(f"a conversation's name is lower case letters, digits and dashes, got {name!r}")
    kind, path = _conversation(session)
    if kind != "session":
        raise DenError(f"{session!r} is kept already")
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    target = CONVERSATIONS_DIR / f"{name}.md"
    if target.exists():
        raise DenError(f"a conversation {name!r} exists")
    target.write_text(f"# {name}\n\nSession {session}\n\n{transcript(session)}\n")
    if _summary_path(path).is_file():
        _summary_path(path).rename(_summary_path(target))
    path.unlink()
    return target


def drop_session(session):
    delete_conversation(session)


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
            "description": "A voice from the library by name (list_voices shows them, with what each sounds "
            "like), draft:ID for a designed voice not saved yet, or the absolute path of a recording to clone. "
            "Default: the model's own voice.",
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


def _drafts_dir():
    return VOICES_DIR / ".drafts"


def keep_draft(data, description, language=None, seed=None):
    """Keep a designed sample as a draft, outside the library until it's saved; returns its id."""
    folder = _drafts_dir()
    folder.mkdir(parents=True, exist_ok=True)
    draft = time.strftime("%Y%m%d-%H%M%S")
    n = 0
    while (folder / f"{draft}{f'-{n}' if n else ''}.wav").exists():
        n += 1
    draft = f"{draft}{f'-{n}' if n else ''}"
    (folder / f"{draft}.wav").write_bytes(data)
    (folder / f"{draft}.json").write_text(json.dumps(
        {"description": description, "source": "designed", "language": language, "seed": seed,
         "created": time.strftime("%Y-%m-%d")}, ensure_ascii=False, indent=2) + "\n")
    for old in sorted(folder.glob("*.wav"))[:-DRAFTS_KEPT]:
        old.unlink()
        old.with_suffix(".json").unlink(missing_ok=True)
    return draft


def draft_path(draft):
    path = _drafts_dir() / f"{draft}.wav"
    if not re.fullmatch(r"[0-9-]+", str(draft)) or not path.is_file():
        raise DenError(f"no draft voice {draft!r}; design_voice makes one (den keeps the last {DRAFTS_KEPT})")
    return path


def save_draft(draft, name, replace=False):
    """Move a draft into the library under name, with how it was made; returns its path there."""
    path = draft_path(draft)
    try:
        meta = json.loads(path.with_suffix(".json").read_text())
    except (OSError, json.JSONDecodeError):
        meta = {}
    target = keep_voice(name, path.read_bytes(), replace, meta.get("description"), meta.get("language"), meta.get("seed"))
    path.unlink()
    path.with_suffix(".json").unlink(missing_ok=True)
    return target


def voice_path(voice):
    """A voice's recording: a name from the library, draft:ID for a designed voice not saved yet,
    or an absolute path. None for the model's own."""
    if not voice:
        return None
    if str(voice).startswith("draft:"):
        return str(draft_path(str(voice)[len("draft:"):]))
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


def _meta_path(name):
    return VOICES_DIR / f"{name}.json"


def _write_meta(name, meta):
    _meta_path(name).write_text(json.dumps({k: v for k, v in meta.items() if v is not None}, ensure_ascii=False, indent=2) + "\n")


def voice_info(name):
    """One voice's details: {name, description, source, language, seed, created, seconds, path}.
    A voice kept before details were (a bare recording) has only its name, path and length."""
    path = voices().get(name)
    if not path:
        raise DenError(f"no voice {name!r}; voices: {', '.join(voices()) or 'none yet'}")
    try:
        meta = json.loads(_meta_path(name).read_text())
    except (OSError, json.JSONDecodeError):
        meta = {}
    info = {"name": name, **meta, "path": str(path)}
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path)) as w:
                info["seconds"] = round(w.getnframes() / w.getframerate(), 1)
        except (wave.Error, OSError):
            pass
    return info


def toc():
    """[{name, description, source}] of every voice, for choosing one."""
    rows = []
    for name in voices():
        info = voice_info(name)
        rows.append({k: info.get(k) for k in ("name", "description", "source", "language")})
    return rows


def _check_new(name, replace):
    if not _NAME.fullmatch(name):
        raise DenError(f"a voice name is lower case letters, digits and dashes, got {name!r}")
    existing = voices().get(name)
    if existing and not replace:
        raise DenError(f"voice {name!r} exists; replace it with --replace")
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    if existing:
        existing.unlink()
        _meta_path(name).unlink(missing_ok=True)


def add_voice(name, recording, replace=False, description=None):
    """Keep a recording in the library under name, with what it is; returns its path there."""
    source = Path(recording).expanduser()
    if not source.is_file():
        raise DenError(f"no recording at {source}")
    if source.suffix.lower() not in AUDIO_SUFFIXES:
        raise DenError(f"not an audio file den knows ({', '.join(AUDIO_SUFFIXES)}): {source.name}")
    _check_new(name, replace)
    target = VOICES_DIR / f"{name}{source.suffix.lower()}"
    shutil.copyfile(source, target)
    _write_meta(name, {"description": description or None, "source": "recorded", "created": time.strftime("%Y-%m-%d")})
    return target


def keep_voice(name, data, replace=False, description=None, language=None, seed=None):
    """Keep a designed sample (WAV bytes) under name, with how it was made; returns its path."""
    _check_new(name, replace)
    target = VOICES_DIR / f"{name}.wav"
    target.write_bytes(data)
    _write_meta(name, {"description": description, "source": "designed", "language": language, "seed": seed,
                       "created": time.strftime("%Y-%m-%d")})
    return target


def describe_voice(name, description):
    """Set or change what a voice is, e.g. for a recording kept without a description."""
    info = voice_info(name)
    meta = {k: info.get(k) for k in ("source", "language", "seed", "created")}
    _write_meta(name, {**meta, "description": description})


def rename_voice(old, new):
    path = voices().get(old)
    if not path:
        raise DenError(f"no voice {old!r}")
    if not _NAME.fullmatch(new):
        raise DenError(f"a voice name is lower case letters, digits and dashes, got {new!r}")
    if new in voices():
        raise DenError(f"voice {new!r} exists")
    path.rename(path.with_name(f"{new}{path.suffix}"))
    if _meta_path(old).exists():
        _meta_path(old).rename(_meta_path(new))


def remove_voice(name):
    path = voices().get(name)
    if not path:
        raise DenError(f"no voice {name!r}")
    path.unlink()
    _meta_path(name).unlink(missing_ok=True)


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
