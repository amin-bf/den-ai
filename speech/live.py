"""den's live engine: ears and a voice for a spoken conversation with Claude (docs/adr/0011-live-conversation.md).

The broker starts it when a live conversation begins and stops it at the end. It runs in the speech
venv and owns the speakers and the models meanwhile: a voice-activity model listens all the time,
a transcription model writes down what the user says, and the speech model speaks in a voice from
the library. Audio goes through PipeWire's echo canceller, so den's own voice is never taken for
the user.

Started with --wake-word it begins in standby (docs/adr/0012-standby.md): only the voice-activity
model and a small transcription model on the CPU, which hears the start of each utterance for the
wake word and forgets it. What
is said from the wake word on is kept as audio until the conversation starts in this same process
(POST /live), so the first words aren't lost; nothing said before it reaches another model.

    GET  /health  {ready, error, phase, wakes}: phase is standby, loading or live
    POST /wait    {after, timeout_s} -> {wakes}, once the wake word has been heard more than
                  `after` times, or at the timeout
    POST /wake    {wake_word?} -> in standby: forget what was said since the wake word, and listen
                  for it (or a new one) again
    POST /live    {voice?, language, exaggeration} -> from standby, start the conversation: load
                  its models; /health says when it's ready
    POST /talk    {say?, wait_s?, voice?, language?, exaggeration?, cfg_weight?, now?} -> from this
                  turn on in voice (a recording's path), speaking language (what `say` is in;
                  listening always detects each utterance's own); speaks `say` sentence by
                  sentence, then waits for the user's next utterance: {heard, interrupted, spoken,
                  unspoken, silence}. exaggeration/cfg_weight color this turn's delivery only,
                  falling back to the session's own (set at /live) when left out; changing
                  exaggeration is cheap; it patches the voice's own conditioning in place rather
                  than re-reading it, so it's fine to change turn by turn. The user speaking over
                  the voice stops it at once (interrupted, and how far it got); something said
                  before the call came in is returned without speaking at all. With now, a talk
                  that is running ends first (preempted): its voice finishes the sentence it's on,
                  and its wait ends unless the user has begun to answer.
    POST /stop    ends the engine
"""

import argparse
import difflib
import json
import os
import queue
import re
import socketserver
import subprocess
import sys
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler

MIC_RATE = 16_000
FRAME = 512  # samples per VAD frame: 32 ms at 16 kHz
START_FRAMES = 4  # about 130 ms of speech starts an utterance
END_SILENCE_S = 0.7  # this much silence ends it
PREROLL_FRAMES = 10  # kept from before the start, so the first syllable isn't cut
MIN_UTTERANCE_S = 0.35
SPEECH_P, SILENCE_P = 0.5, 0.35
EC_SOURCE, EC_SINK = "den_live_mic", "den_live_out"
WAKE_MODEL = "openai/whisper-base"  # small enough for the CPU, good enough at names
WAKE_WINDOW_S = 2.0  # the wake word counts only at the start of an utterance
WAKE_MATCH = 0.9  # how alike, by sound, the start of an utterance and the wake word must be
WAKE_THREADS = 2  # the detector's share of the CPU: standby leaves the machine to others
EARLY_MAX_S = 120  # at most this much speech is kept between the wake word and the conversation
FILLERS = {"oh", "um", "uh", "hm", "hmm", "ok", "okay", "so", "well"}

state = {"ready": False, "error": None, "phase": "live", "wakes": 0}  # phase: standby -> loading -> live
utterances = queue.Queue()  # (audio, t_start, t_end, counted, began_in) in the order they were said
heard = queue.Queue()  # finished utterances, as {text, t_start, t_end}
wake_heard = threading.Condition()  # notified when the wake word is heard
early = []  # (audio, t_start, t_end, wake) since the wake word, until the conversation's transcription model is loaded
targets = [None, None]  # the echo canceller's source and sink, once it's loaded
recorder = {"target": None, "switch": False}  # listen() moves to the echo canceller's source when asked
preempt = threading.Event()  # a talk with now is waiting for the one before it to end
barge = threading.Event()  # the user started speaking over the voice
user_speaking = threading.Event()  # an utterance is under way: never end the user's turn meanwhile
writing = [0]  # utterances the transcription model is still writing down
writing_lock = threading.Lock()
speaking = threading.Event()  # den's voice is playing
stopping = threading.Event()
models = {}
talk_lock = threading.Lock()


def log(text):
    print(text, flush=True)


# --- echo cancellation ---


def load_echo_cancel():
    """PipeWire's echo canceller for the session: a source that hears the user without den's own
    voice, and a sink den plays to. Returns the module id, or None when it couldn't be loaded."""
    try:
        # An engine that was killed leaves its module behind, which a new one would clash with.
        listed = subprocess.run(["pactl", "list", "short", "modules"], capture_output=True, text=True, timeout=10)
        for line in listed.stdout.splitlines():
            if f"source_name={EC_SOURCE}" in line:
                unload_echo_cancel(line.split()[0])
        out = subprocess.run(
            ["pactl", "load-module", "module-echo-cancel", "aec_method=webrtc",
             f"source_name={EC_SOURCE}", f"sink_name={EC_SINK}",
             "source_properties=device.description=den-live-mic", "sink_properties=device.description=den-live-out"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
        log(f"echo cancellation not loaded: {out.stderr.strip()}")
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"echo cancellation not loaded: {e}")
    return None


def unload_echo_cancel(module):
    if module:
        subprocess.run(["pactl", "unload-module", module], capture_output=True, timeout=10)


# --- ears ---


def record(target):
    """A recorder on the microphone, or on target. Recording shares the microphone: other
    programs can go on recording from it."""
    args = ["pw-record", "--raw", "--rate", str(MIC_RATE), "--channels", "1", "--format", "s16", "-"]
    if target:
        args[1:1] = ["--target", target]
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def listen():
    """Read the microphone for as long as the engine runs, and cut it into utterances. Standby
    records the microphone as it is; the conversation moves to the echo canceller's source."""
    import numpy
    import torch

    vad = models["vad"]
    proc = record(recorder["target"])
    preroll, frames, voiced, quiet = [], [], 0, 0
    in_speech, started, began_in = False, 0.0, None
    try:
        while not stopping.is_set():
            if recorder["switch"]:
                # The new recorder starts before the old one ends, so an utterance runs on through it.
                recorder["switch"] = False
                old, proc = proc, record(recorder["target"])
                old.kill()
            raw = proc.stdout.read(FRAME * 2)
            if len(raw) < FRAME * 2:
                break
            samples = numpy.frombuffer(raw, dtype=numpy.int16).astype(numpy.float32) / 32768
            p = vad(torch.from_numpy(samples), MIC_RATE).item()
            if not in_speech:
                preroll = (preroll + [samples])[-PREROLL_FRAMES:]
                voiced = voiced + 1 if p > SPEECH_P else 0
                if voiced >= START_FRAMES:
                    in_speech, started, frames, quiet = True, time.time(), list(preroll), 0
                    began_in = state["phase"]  # an utterance begun in standby is the detector's alone
                    user_speaking.set()
                    if speaking.is_set():
                        barge.set()  # the user talks over the voice: it stops
            else:
                frames.append(samples)
                quiet = quiet + 1 if p < SILENCE_P else 0
                if quiet * FRAME / MIC_RATE >= END_SILENCE_S:
                    in_speech, voiced = False, 0
                    user_speaking.clear()
                    audio = numpy.concatenate(frames)
                    if len(audio) / MIC_RATE >= MIN_UTTERANCE_S:
                        counted = state["phase"] == "live"  # a talk waits while it's written down
                        if counted:
                            with writing_lock:
                                writing[0] += 1
                        utterances.put((audio, started, time.time(), counted, began_in))
                    vad.reset_states()
    finally:
        proc.kill()


def handle_utterances():
    """Every utterance, in the order it was said. One begun in standby is heard by the detector
    only, and forgotten unless it starts with the wake word; from the wake word on, or once the
    conversation is starting, it's kept as audio until the transcription model is loaded to write it down."""
    while not stopping.is_set():
        item = utterances.get()
        if item is None:
            # The conversation's transcription model is loaded: what was kept is written down first, in order.
            for audio, t_start, t_end, wake in early:
                write_down(audio, t_start, t_end, strip_wake=wake)
            early.clear()
            state.update(phase="live", ready=True)
            log("ready")
            continue
        audio, t_start, t_end, counted, began_in = item
        try:
            wake = False
            if began_in == "standby" and not models.get("woke"):
                if not is_wake(detect(audio)):
                    continue  # forgotten
                wake = models["woke"] = True
                with wake_heard:
                    state["wakes"] += 1
                    wake_heard.notify_all()
                log("wake word heard")  # never what was said: standby keeps nothing it hears
            if state["phase"] == "live":
                write_down(audio, t_start, t_end, strip_wake=wake)
            elif sum(len(a) for a, *_ in early) / MIC_RATE < EARLY_MAX_S:
                early.append((audio, t_start, t_end, wake))
        except Exception as e:  # one utterance lost, not the ears
            log(f"an utterance failed: {type(e).__name__}: {e}")
        finally:
            if counted:
                with writing_lock:
                    writing[0] -= 1  # this utterance is written down, or was nothing


def write_down(audio, t_start, t_end, strip_wake=False):
    with models["whisper_lock"]:
        result = models["whisper"](
            {"raw": audio, "sampling_rate": MIC_RATE},
            generate_kwargs={"task": "transcribe", **({"language": models["language"]} if models["language"] else {})},
        )
    text = str(result.get("text") or "").strip()
    if strip_wake:
        text = after_wake(text)
    if text:
        heard.put({"text": text, "t_start": t_start, "t_end": t_end})


# --- standby: the wake word ---


def words(text):
    """Lower case letters and digits, word by word: what the wake word is compared by."""
    text = unicodedata.normalize("NFKD", text.lower())
    return re.findall(r"[a-z0-9]+", "".join(c for c in text if not unicodedata.combining(c)))


def sounds(letters):
    """Letters as they sound, roughly: the transcription model spells a name as it likes ("Elli", "Ellie", "Ely")."""
    letters = re.sub(r"(ay|ey|ei|ai)", "ei", re.sub(r"(ie|ee|ea|y)", "i", letters))
    return re.sub(r"(.)\1+", r"\1", letters)


def wake_span(text):
    """How many of text's first words are the wake word, or 0. The words are compared joined and
    by sound, since the transcription model spells names freely and splits or joins words; one filler before it
    ("oh, hey Elli") is allowed. A near name ("hey Olli", "hey Emily") doesn't count: a false wake
    takes the machine, a missed one is only said again."""
    said, wake = words(text), sounds("".join(words(models["wake_word"])))
    if not wake:
        return 0
    skip = 1 if said[:1] and said[0] in FILLERS else 0
    best, span = 0.0, 0
    for n in range(1, min(len(said) - skip, len(words(models["wake_word"])) + 1) + 1):
        ratio = difflib.SequenceMatcher(None, sounds("".join(said[skip : skip + n])), wake).ratio()
        if ratio > best:
            best, span = ratio, skip + n
    return span if best >= WAKE_MATCH else 0


def is_wake(text):
    return wake_span(text) > 0


def after_wake(text):
    """What was said after the wake word, as the transcription model wrote it; "" when nothing was."""
    span = wake_span(text)
    rest = text
    for _ in range(span):
        rest = re.sub(r"^[^\w]*[\w'’-]+", "", rest, count=1)
    return rest.lstrip(" ,.!?…-–—").strip()


def detect(audio):
    """What the detector hears at the start of an utterance. It goes no further than the caller's
    comparison: nothing heard in standby is written down, logged or passed on."""
    if "detector" not in models:
        return ""  # the conversation has begun: what was begun before it stays unheard
    head = audio[: int(WAKE_WINDOW_S * MIC_RATE)]
    result = models["detector"]({"raw": head, "sampling_rate": MIC_RATE},
                                generate_kwargs={"task": "transcribe", "max_new_tokens": 12})
    return str(result.get("text") or "")


# --- voice ---


def sentences(text):
    """The reply cut into sentences, so the voice starts after the first; very short ones merge."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text.strip()) if p.strip()]
    merged = []
    for part in parts:
        if merged and len(merged[-1].split()) < 4:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged


def synthesize(sentence, exaggeration=None, cfg_weight=None):
    import torch

    with models["tts_lock"]:
        wav = models["tts"].generate(
            sentence, language_id=models["speak_language"],
            exaggeration=exaggeration if exaggeration is not None else models["exaggeration"],
            cfg_weight=cfg_weight if cfg_weight is not None else 0.5,
        )
    return wav.squeeze().clamp(-1, 1).mul(32767).to(torch.int16).cpu().numpy().tobytes()


def speak(text, target, exaggeration=None, cfg_weight=None):
    """Say the text sentence by sentence; stop at once when the user starts speaking. Returns the
    sentences fully said and the ones not said. exaggeration/cfg_weight color this turn only —
    changing exaggeration is cheap (it patches the voice's own conditioning in place), so a turn
    can be calmer or more dramatic than the session's default without re-reading the voice."""
    parts = sentences(text)
    audio = queue.Queue(maxsize=2)

    def produce():
        for part in parts:
            if barge.is_set():
                break
            audio.put((part, synthesize(part, exaggeration, cfg_weight)))
        audio.put(None)

    threading.Thread(target=produce, daemon=True).start()
    rate = models["tts"].sr
    # --raw: without it pw-play reads through libsndfile, which wants a header, and fails at once.
    args = ["pw-play", "--raw", "--rate", str(rate), "--channels", "1", "--format", "s16", "-"]
    if target:
        args[1:1] = ["--target", target]
    player = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    said, chunk = [], rate // 10 * 2  # 100 ms at a time, so a stop is quick
    speaking.set()
    try:
        while True:
            item = audio.get()
            if item is None or barge.is_set():
                break
            part, pcm = item
            began = time.time()
            for i in range(0, len(pcm), chunk):
                if barge.is_set():
                    break
                player.stdin.write(pcm[i : i + chunk])
                player.stdin.flush()
                # Keep pace with playback: write no further ahead than the player's buffer.
                ahead = (i + chunk) / 2 / rate - (time.time() - began)
                if ahead > 0.2:
                    time.sleep(ahead - 0.2)
            if barge.is_set():
                break
            said.append(part)
            if preempt.is_set():
                break  # a line that can't wait is next: this sentence is finished, the rest dropped
    except BrokenPipeError:
        # The player died: say why, instead of passing the reply off as unspoken.
        error = (player.stderr.read() or b"").decode(errors="replace").strip()
        log(f"playback failed: {error}")
        raise RuntimeError(f"playback failed: {error or 'the player exited'}")
    finally:
        speaking.clear()
        if barge.is_set():
            player.kill()  # drop what the player still holds
        else:
            try:
                player.stdin.close()
                player.wait(30)
            except (OSError, subprocess.TimeoutExpired):
                player.kill()
    return said, parts[len(said):]


# --- the conversation ---


def switch(voice=None, language=None):
    """Another voice or speaking language from this turn on: the voice's recording is read once
    (about a second). Listening needs no language: the transcription model detects each utterance's own, so the
    user can answer in English what was said in German, and the other way round."""
    if language:
        models["speak_language"] = language
    if voice and voice != models.get("voice"):
        with models["tts_lock"]:
            models["tts"].prepare_conditionals(voice, exaggeration=models["exaggeration"])
        models["voice"] = voice


def talk(body):
    say = str(body.get("say") or "").strip()
    wait_s = float(body.get("wait_s") or 120)
    switch(body.get("voice"), body.get("language"))
    exaggeration = float(body["exaggeration"]) if body.get("exaggeration") is not None else None
    cfg_weight = float(body["cfg_weight"]) if body.get("cfg_weight") is not None else None
    if body.get("now"):
        preempt.set()  # the talk that is running ends, so this one can speak
    with talk_lock:
        preempt.clear()
        # Something said while Claude was thinking comes first: its reply may no longer fit.
        if say and not heard.empty():
            return {"heard": drain(), "interrupted": False, "spoken": [], "unspoken": sentences(say), "said_first": True}
        barge.clear()
        said, unsaid = speak(say, targets[1], exaggeration, cfg_weight) if say else ([], [])
        interrupted = bool(unsaid) and barge.is_set()
        cut = {"preempted": True} if unsaid and not interrupted else {}  # stopped for a line that can't wait
        deadline = time.time() + wait_s
        while True:
            try:
                first = heard.get(timeout=0.1)
                break
            except queue.Empty:
                pass
            # A line that can't wait ends the wait, unless the user has begun to answer: the
            # answer belongs to what was asked, and the line is spoken after it.
            if preempt.is_set() and not user_speaking.is_set() and writing[0] == 0:
                return {"heard": None, "preempted": True, "interrupted": False, "spoken": said, "unspoken": unsaid}
            if time.time() > deadline:
                return {"heard": None, "silence": True, "interrupted": False, "spoken": said, "unspoken": unsaid, **cut}
        return {"heard": gather(first["text"]), "interrupted": interrupted, "spoken": said, "unspoken": unsaid, **cut}


UNFINISHED = re.compile(r"(,|\b(and|but|so|or|because|maybe|like|that|the|a|to|of|um|uh|if|when|then))$", re.I)


def after_pause(text):
    """How long a pause after these words may still be the middle of the user's thought."""
    words = text.split()
    if not re.search(r"[.!?…]$", text) or UNFINISHED.search(text.rstrip(".!?… ")):
        return 2.5  # it doesn't sound finished
    if len(words) < 3:
        return 1.8  # a short answer is often the start of more ("No. I don't think so…")
    return 0.6


def gather(text):
    """The user's turn: this utterance and what follows it closely. The turn never ends while
    they're speaking or the transcription model is still writing down what they said."""
    texts = [text]
    while True:
        deadline = time.time() + after_pause(" ".join(texts))
        while time.time() < deadline or user_speaking.is_set() or writing[0] > 0:
            try:
                texts.append(heard.get(timeout=0.1)["text"])
                break
            except queue.Empty:
                continue
        else:
            return " ".join(texts)


def drain():
    return gather(heard.get()["text"])


def load_standby(args):
    """The VAD and the detector, on the CPU only: standby leaves the GPU and most cores to others."""
    try:
        import torch
        from silero_vad import load_silero_vad
        from transformers import pipeline

        models["threads"] = torch.get_num_threads()
        torch.set_num_threads(WAKE_THREADS)
        log("loading the wake word detector on the cpu")
        models["vad"] = load_silero_vad()
        models["detector"] = pipeline("automatic-speech-recognition", model=WAKE_MODEL, device="cpu", dtype=torch.float32)
        models["wake_word"] = args.wake_word
        state.update(phase="standby", ready=True)
        log("standby")
    except Exception as e:
        state["error"] = f"{type(e).__name__}: {e}"
        log(f"load failed: {state['error']}")


def go_live(args):
    """From standby to the conversation in this process, so the microphone never stops: the echo
    canceller, then the transcription model and the voice. Ready once what was said since the wake word (or since
    this call) is written down."""
    import torch

    state.update(phase="loading", ready=False)  # from here on every utterance is the conversation's
    torch.set_num_threads(models.get("threads") or torch.get_num_threads())
    models["ec_module"] = load_echo_cancel()
    if models["ec_module"]:
        targets[:] = [EC_SOURCE, EC_SINK]
        recorder.update(target=EC_SOURCE, switch=True)
    load(args, vad=False, ready=False)
    if not state["error"]:
        models.pop("detector", None)
        utterances.put(None)  # handle_utterances writes down what was kept, then is ready


def load(args, vad=True, ready=True):
    try:
        import torch
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        from silero_vad import load_silero_vad
        from transformers import pipeline

        device = "cuda" if torch.cuda.is_available() else "cpu"
        log(f"loading the live engine on {device}")
        if vad:
            models["vad"] = load_silero_vad()
        models["whisper"] = pipeline(
            "automatic-speech-recognition", model="openai/whisper-large-v3-turbo", device=device,
            dtype=torch.float16 if device != "cpu" else torch.float32,
        )
        models["whisper_lock"] = threading.Lock()
        tts = ChatterboxMultilingualTTS.from_pretrained(device=device, t3_model="v3")
        if args.voice:
            tts.prepare_conditionals(args.voice, exaggeration=args.exaggeration)
        models["tts"] = tts
        models["tts_lock"] = threading.Lock()
        models["voice"] = args.voice
        models["language"] = None  # listening: every utterance's own language, detected
        models["speak_language"] = args.language
        models["exaggeration"] = args.exaggeration
        if ready:
            state["ready"] = True
            log("ready")
    except Exception as e:
        state["error"] = f"{type(e).__name__}: {e}"
        log(f"load failed: {state['error']}")


class Handler(BaseHTTPRequestHandler):
    def address_string(self):
        return "den"

    def log_message(self, fmt, *args):
        log(fmt % args)

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {k: state[k] for k in ("ready", "error", "phase", "wakes")})
        else:
            self._json(404, {"error": f"no {self.path}"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/stop":
            stopping.set()
            self._json(200, {"stopped": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        elif self.path == "/wait":
            after, deadline = int(body.get("after") or 0), time.time() + float(body.get("timeout_s") or 30)
            with wake_heard:
                while state["wakes"] <= after and not stopping.is_set() and time.time() < deadline:
                    wake_heard.wait(min(1.0, max(0.0, deadline - time.time())))
            self._json(200, {"wakes": state["wakes"]})
        elif self.path == "/wake":
            if state["phase"] != "standby":
                self._json(409, {"error": f"not in standby ({state['phase']})"})
                return
            if body.get("wake_word"):
                models["wake_word"] = str(body["wake_word"])
            models["woke"] = False
            early.clear()  # what was said since the wake word is forgotten, unheard by any other model
            self._json(200, {"wakes": state["wakes"]})
        elif self.path == "/live":
            if state["phase"] != "standby" or not state["ready"]:
                self._json(409, {"error": f"can't start the conversation from {state['phase']}"})
                return
            args = argparse.Namespace(voice=body.get("voice"), language=body.get("language") or "en",
                                      exaggeration=float(body.get("exaggeration") or 0.5))
            # Before answering: from here on what is begun is the conversation's, and the broker's
            # next /health mustn't see standby ready.
            state.update(phase="loading", ready=False)
            threading.Thread(target=go_live, args=(args,), daemon=True).start()
            self._json(200, {"loading": True})
        elif self.path == "/talk":
            if not state["ready"] or state["phase"] != "live":
                self._json(503, {"error": state["error"] or "the live engine is still loading"})
                return
            try:
                self._json(200, talk(body))
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {e}"})
        else:
            self._json(404, {"error": f"no {self.path}"})


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socket", required=True)
    parser.add_argument("--voice", help="the recording of the voice to speak in")
    parser.add_argument("--language", default="en")
    parser.add_argument("--exaggeration", type=float, default=0.5)
    parser.add_argument("--wake-word", help="start in standby, listening only for this phrase")
    args = parser.parse_args()
    if os.path.exists(args.socket):
        os.unlink(args.socket)
    if not args.wake_word:
        # Standby goes without it: den doesn't speak there, and it adds a microphone to the system.
        models["ec_module"] = load_echo_cancel()
        if models["ec_module"]:
            targets[:] = [EC_SOURCE, EC_SINK]
            recorder["target"] = EC_SOURCE
    server = Server(args.socket, Handler)
    os.chmod(args.socket, 0o600)

    def start():
        load_standby(args) if args.wake_word else load(args)
        if state["ready"]:
            threading.Thread(target=handle_utterances, daemon=True).start()
            listen()

    threading.Thread(target=start, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        stopping.set()
        with wake_heard:
            wake_heard.notify_all()
        server.server_close()
        unload_echo_cancel(models.get("ec_module"))
        if os.path.exists(args.socket):
            os.unlink(args.socket)
    sys.exit(0)


if __name__ == "__main__":
    main()
