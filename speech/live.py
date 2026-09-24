"""den's live engine: ears and a voice for a spoken conversation with Claude (docs/adr/0011-live-conversation.md).

The broker starts it when live mode begins and stops it at the end. It runs in the speech venv
and owns the microphone, the speakers and the models meanwhile: Silero VAD listens all the time,
Whisper writes down what the user says, and Chatterbox speaks in a voice from the library. Audio
goes through PipeWire's echo canceller, so den's own voice is never taken for the user.

    GET  /health  {ready, error}
    POST /talk    {say?, wait_s?, voice?, language?} -> from this turn on in voice (a recording's
                  path) and language ("auto": Whisper detects it); speaks `say` sentence by sentence, then waits for the user's
                  next utterance: {heard, interrupted, spoken, unspoken, silence}. The user
                  speaking over the voice stops it at once (interrupted, and how far it got);
                  something said before the call came in is returned without speaking at all.
    POST /stop    ends the engine
"""

import argparse
import json
import os
import queue
import re
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler

MIC_RATE = 16_000
FRAME = 512  # samples per VAD frame: 32 ms at 16 kHz
START_FRAMES = 4  # about 130 ms of speech starts an utterance
END_SILENCE_S = 0.7  # this much silence ends it
PREROLL_FRAMES = 10  # kept from before the start, so the first syllable isn't cut
MIN_UTTERANCE_S = 0.35
SPEECH_P, SILENCE_P = 0.5, 0.35
EC_SOURCE, EC_SINK = "den_live_mic", "den_live_out"

state = {"ready": False, "error": None}
heard = queue.Queue()  # finished utterances, as {text, t_start, t_end}
barge = threading.Event()  # the user started speaking over the voice
user_speaking = threading.Event()  # an utterance is under way: never end the user's turn meanwhile
writing = [0]  # utterances Whisper is still writing down
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


def listen(target):
    """Read the microphone for as long as the engine runs, and cut it into utterances."""
    import numpy
    import torch

    vad = models["vad"]
    args = ["pw-record", "--raw", "--rate", str(MIC_RATE), "--channels", "1", "--format", "s16", "-"]
    if target:
        args[1:1] = ["--target", target]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    preroll, frames, voiced, quiet = [], [], 0, 0
    in_speech, started = False, 0.0
    try:
        while not stopping.is_set():
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
                        with writing_lock:
                            writing[0] += 1
                        threading.Thread(target=transcribe, args=(audio, started, time.time()), daemon=True).start()
                    vad.reset_states()
    finally:
        proc.kill()


def transcribe(audio, t_start, t_end):
    with models["whisper_lock"]:
        result = models["whisper"](
            {"raw": audio, "sampling_rate": MIC_RATE},
            generate_kwargs={"task": "transcribe", **({"language": models["language"]} if models["language"] else {})},
        )
    text = str(result.get("text") or "").strip()
    with writing_lock:
        writing[0] -= 1  # this utterance is written down, or was nothing
    if text:
        heard.put({"text": text, "t_start": t_start, "t_end": t_end})


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


def synthesize(sentence):
    import torch

    with models["tts_lock"]:
        wav = models["tts"].generate(sentence, language_id=models["speak_language"], exaggeration=models["exaggeration"])
    return wav.squeeze().clamp(-1, 1).mul(32767).to(torch.int16).cpu().numpy().tobytes()


def speak(text, target):
    """Say the text sentence by sentence; stop at once when the user starts speaking. Returns the
    sentences fully said and the ones not said."""
    parts = sentences(text)
    audio = queue.Queue(maxsize=2)

    def produce():
        for part in parts:
            if barge.is_set():
                break
            audio.put((part, synthesize(part)))
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
    """Another voice or language from this turn on: the voice's recording is read once (about a
    second); a language applies to speaking and listening, "auto" letting Whisper detect it."""
    if language:
        models["language"] = None if language == "auto" else language
        if language != "auto":
            models["speak_language"] = language
    if voice and voice != models.get("voice"):
        with models["tts_lock"]:
            models["tts"].prepare_conditionals(voice, exaggeration=models["exaggeration"])
        models["voice"] = voice


def talk(body, targets):
    say = str(body.get("say") or "").strip()
    wait_s = float(body.get("wait_s") or 120)
    switch(body.get("voice"), body.get("language"))
    with talk_lock:
        # Something said while Claude was thinking comes first: its reply may no longer fit.
        if say and not heard.empty():
            return {"heard": drain(), "interrupted": False, "spoken": [], "unspoken": sentences(say), "said_first": True}
        barge.clear()
        said, unsaid = speak(say, targets[1]) if say else ([], [])
        interrupted = bool(unsaid) and barge.is_set()
        try:
            first = heard.get(timeout=wait_s)
        except queue.Empty:
            return {"heard": None, "silence": True, "interrupted": False, "spoken": said, "unspoken": unsaid}
        return {"heard": gather(first["text"]), "interrupted": interrupted, "spoken": said, "unspoken": unsaid}


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
    they're speaking or Whisper is still writing down what they said."""
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


def load(args):
    try:
        import torch
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        from silero_vad import load_silero_vad
        from transformers import pipeline

        device = "cuda" if torch.cuda.is_available() else "cpu"
        log(f"loading the live engine on {device}")
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
        models["language"] = None if args.language == "auto" else args.language
        models["speak_language"] = args.language if args.language != "auto" else "en"
        models["exaggeration"] = args.exaggeration
        state["ready"] = True
        log("ready")
    except Exception as e:
        state["error"] = f"{type(e).__name__}: {e}"
        log(f"load failed: {state['error']}")


class Handler(BaseHTTPRequestHandler):
    targets = (None, None)

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
            self._json(200, {"ready": state["ready"], "error": state["error"]})
        else:
            self._json(404, {"error": f"no {self.path}"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/stop":
            stopping.set()
            self._json(200, {"stopped": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        elif self.path == "/talk":
            if not state["ready"]:
                self._json(503, {"error": state["error"] or "the live engine is still loading"})
                return
            try:
                self._json(200, talk(body, self.targets))
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
    args = parser.parse_args()
    if os.path.exists(args.socket):
        os.unlink(args.socket)
    module = load_echo_cancel()
    Handler.targets = (EC_SOURCE, EC_SINK) if module else (None, None)
    server = Server(args.socket, Handler)
    os.chmod(args.socket, 0o600)

    def start():
        load(args)
        if state["ready"]:
            listen(Handler.targets[0])

    threading.Thread(target=start, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        stopping.set()
        server.server_close()
        unload_echo_cancel(module)
        if os.path.exists(args.socket):
            os.unlink(args.socket)
    sys.exit(0)


if __name__ == "__main__":
    main()
