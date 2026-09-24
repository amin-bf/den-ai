"""den's speech server: Chatterbox Multilingual on a private UNIX socket (docs/adr/0010-voice-overs.md).

The broker starts it for a voice job and stops it after. It runs in the speech venv that setup.sh
makes, not in den's own Python: Chatterbox pins a torch and transformers of its own.

    GET  /health  {"ready": bool, "error": str|None}; ready once the model is loaded
    POST /speak   {text, language, voice?, exaggeration?, cfg_weight?, temperature?, seed?}
                  -> audio/wav, 16-bit mono at the model's rate; voice is the path of a recording
                  to clone (without one, the model's own voice)
    POST /convert {audio} -> audio/wav, 16-bit mono at the model's rate: a recording in any format
                  as den's own tracks are, so a voice-over can be one
    POST /transcribe {audio, language?} -> {language, segments: [{start, end, text}]}: Whisper
                  large-v3-turbo, loaded on the first call, so a voice job never loads it
"""

import argparse
import io
import json
import os
import socketserver
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler

model = None
load_error = None
lock = threading.Lock()  # one generation at a time: the model keeps one voice's conditioning
voice_state = {"voice": None, "exaggeration": None, "default": None}
WHISPER = "openai/whisper-large-v3-turbo"
whisper = None
device_name = "cpu"


def load(device):
    global model, load_error
    try:
        import torch
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS

        global device_name
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
        device_name = device
        print(f"loading Chatterbox Multilingual V3 on {device}", flush=True)
        m = ChatterboxMultilingualTTS.from_pretrained(device=device, t3_model="v3")
        voice_state["default"] = m.conds
        model = m
        print("ready", flush=True)
    except Exception as e:  # reported through /health, so the broker can say why
        load_error = f"{type(e).__name__}: {e}"
        print(f"load failed: {load_error}", flush=True)


def speak(body):
    import torch

    text = str(body.get("text") or "").strip()
    if not text:
        raise ValueError("the text is empty")
    voice = body.get("voice") or None
    exaggeration = float(body.get("exaggeration", 0.5))
    with lock:
        # Preparing a voice reads and embeds the recording: do it once per voice, not per line.
        if voice != voice_state["voice"] or exaggeration != voice_state["exaggeration"]:
            if voice:
                model.prepare_conditionals(voice, exaggeration=exaggeration)
            else:
                model.conds = voice_state["default"]
            voice_state["voice"], voice_state["exaggeration"] = voice, exaggeration
        if body.get("seed") is not None:
            torch.manual_seed(int(body["seed"]))
        wav = model.generate(
            text,
            language_id=str(body.get("language") or "en"),
            exaggeration=exaggeration,
            cfg_weight=float(body.get("cfg_weight", 0.5)),
            temperature=float(body.get("temperature", 0.8)),
        )
    samples = wav.squeeze().clamp(-1, 1).mul(32767).to(torch.int16).cpu().numpy().tobytes()
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(model.sr)
        w.writeframes(samples)
    return out.getvalue()


def convert(body):
    """A recording as a 16-bit mono WAV at the speech model's rate."""
    import librosa
    import numpy

    path = str(body.get("audio") or "")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no recording at {path}")
    rate = model.sr if model is not None else 24_000
    samples, _ = librosa.load(path, sr=rate, mono=True)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((numpy.clip(samples, -1, 1) * 32767).astype(numpy.int16).tobytes())
    return out.getvalue()


def transcribe(body):
    """Timed segments of a recording's speech."""
    global whisper
    import librosa
    import torch
    from transformers import pipeline

    path = str(body.get("audio") or "")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no recording at {path}")
    with lock:
        if whisper is None:
            print(f"loading {WHISPER} on {device_name}", flush=True)
            whisper = pipeline(
                "automatic-speech-recognition", model=WHISPER, device=device_name,
                dtype=torch.float16 if device_name != "cpu" else torch.float32,
            )
        samples, _ = librosa.load(path, sr=16_000, mono=True)
        kwargs = {"language": body["language"]} if body.get("language") else {}
        result = whisper(
            {"raw": samples, "sampling_rate": 16_000}, return_timestamps=True, chunk_length_s=30,
            generate_kwargs={"task": "transcribe", **kwargs},
        )
    segments = []
    for chunk in result.get("chunks") or []:
        start, end = chunk.get("timestamp") or (None, None)
        text = str(chunk.get("text") or "").strip()
        if text and start is not None:
            segments.append({"start": round(float(start), 3), "end": round(float(end if end is not None else start + 2), 3), "text": text})
    return {"segments": segments, "text": str(result.get("text") or "").strip(), "duration": round(len(samples) / 16_000, 2)}


class Handler(BaseHTTPRequestHandler):
    def address_string(self):  # a UNIX socket has no client address
        return "den"

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ready": model is not None, "error": load_error})
        else:
            self._json(404, {"error": f"no {self.path}"})

    def do_POST(self):
        if self.path == "/transcribe":
            try:
                self._json(200, transcribe(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")))
            except (ValueError, TypeError, json.JSONDecodeError, FileNotFoundError) as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return
        if self.path not in ("/speak", "/convert"):
            self._json(404, {"error": f"no {self.path}"})
            return
        if model is None:
            self._json(503, {"error": load_error or "the model is still loading"})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            data = convert(body) if self.path == "/convert" else speak(body)
        except (ValueError, TypeError, json.JSONDecodeError, FileNotFoundError) as e:
            self._json(400, {"error": str(e)})
            return
        except Exception as e:
            self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socket", required=True)
    parser.add_argument("--device", default="auto", help="cuda, mps or cpu (default: the best there is)")
    args = parser.parse_args()
    if os.path.exists(args.socket):
        os.unlink(args.socket)
    server = Server(args.socket, Handler)
    os.chmod(args.socket, 0o600)
    threading.Thread(target=load, args=(args.device,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if os.path.exists(args.socket):
            os.unlink(args.socket)
    sys.exit(0)


if __name__ == "__main__":
    main()
