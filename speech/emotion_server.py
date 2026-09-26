"""den's second speech workflow: a strong-emotion speech model on a private UNIX socket
(docs/adr/voice-overs.md). Opt-in: setup.sh only installs it when asked, in its own venv, since
it pins a torch and transformers neither the default speech venv nor ComfyUI's can share.

The broker starts it for a voice job that names this workflow, and stops it after, exactly as
the default workflow's server (speech/server.py). Only speaking is offered here: a recording's
conversion and transcription stay the default workflow's job regardless of which workflow speaks.

    GET  /health  {"ready": bool, "error": str|None}; ready once the model is loaded
    POST /speak   {text, language, voice?, emo_vector?, emo_alpha?, duration_factor?, seed?}
                  -> audio/wav, 16-bit mono; voice is the path of a recording to clone (without
                  one, the model's own voice); emo_vector is 8 numbers 0-1, in this order:
                  happy, angry, sad, afraid, disgusted, melancholic, surprised, calm;
                  duration_factor is speaking rate, 0.5-2.0 (default 1.0), lower is slower/longer
"""

import argparse
import json
import os
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler

model = None
load_error = None
lock = threading.Lock()  # one generation at a time
device_name = "cpu"
LANGUAGES = {"en": "EN", "zh": "ZH", "ja": "JA", "es": "ES", "ar": "AR"}


def load(device, checkpoints):
    global model, load_error
    try:
        import torch

        global device_name
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        device_name = device
        print(f"loading the speech workflow's model on {device}", flush=True)
        # The module sets HF_HUB_CACHE to "./checkpoints/hf_cache" on import — a plain string
        # relative to whatever the process's cwd is right then, not to model_dir. huggingface_hub
        # freezes it into a constant during that same import, so setting the env var afterward has
        # no effect at all; chdir before the import is the only thing that actually lands it in
        # the right place. Getting this wrong doesn't error — it silently re-downloads several GB
        # of auxiliary models (w2v-bert-2.0, BigVGAN, ...) whenever cwd isn't checkpoints' parent.
        os.chdir(os.path.dirname(checkpoints) or ".")
        from indextts.infer_v2_5 import IndexTTS2

        m = IndexTTS2(
            cfg_path=os.path.join(checkpoints, "config.yaml"), model_dir=checkpoints,
            use_bf16=(device == "cuda"), device=None if device == "auto" else device,
        )
        model = m
        print("ready", flush=True)
    except Exception as e:  # reported through /health, so the broker can say why
        load_error = f"{type(e).__name__}: {e}"
        print(f"load failed: {load_error}", flush=True)


def speak(body):
    text = str(body.get("text") or "").strip()
    if not text:
        raise ValueError("the text is empty")
    language = LANGUAGES.get(str(body.get("language") or "en").lower())
    if language is None:
        raise ValueError(f"this workflow speaks {', '.join(LANGUAGES)}; got {body.get('language')!r}")
    voice = body.get("voice") or None
    if not voice:
        raise ValueError("this workflow clones a voice; it has none of its own")
    vector = body.get("emo_vector")
    if vector is not None and (not isinstance(vector, list) or len(vector) != 8):
        raise ValueError("emo_vector is 8 numbers 0-1")
    duration_factor = float(body.get("duration_factor", 1.0))
    if not (0.5 <= duration_factor <= 2.0):
        raise ValueError("duration_factor is 0.5-2.0")
    out = f"/tmp/den-emotion-speech-{os.getpid()}-{threading.get_ident()}.wav"
    with lock:
        if body.get("seed") is not None:
            import torch

            torch.manual_seed(int(body["seed"]))
        model.infer(
            spk_audio_prompt=voice,
            text=text,
            lang=language,
            output_path=out,
            emo_vector=vector,
            emo_alpha=float(body.get("emo_alpha", 1.0)),
            duration_factor=duration_factor,
            verbose=False,
        )
    try:
        return open(out, "rb").read()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


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
        if self.path != "/speak":
            self._json(404, {"error": f"no {self.path}"})
            return
        if model is None:
            self._json(503, {"error": load_error or "the model is still loading"})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            data = speak(body)
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
    parser.add_argument("--device", default="auto", help="cuda or cpu (default: the best there is)")
    parser.add_argument("--checkpoints", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkpoints"),
                         help="the model's downloaded files (default: a checkpoints/ folder beside this script)")
    args = parser.parse_args()
    if os.path.exists(args.socket):
        os.unlink(args.socket)
    server = Server(args.socket, Handler)
    os.chmod(args.socket, 0o600)
    threading.Thread(target=load, args=(args.device, args.checkpoints), daemon=True).start()
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
