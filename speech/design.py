"""den's voice designer: a voice's sample from a description (docs/adr/voice-overs.md).

The voice-design model speaks a sample text in a voice made from a description ("an old man with a
deep, raspy voice"); den keeps the sample in its voice library, and the speech model clones it from then
on. It runs once per voice, as a job the broker starts and waits for, in a venv of its own that
setup.sh makes: the voice-design model's package pins a transformers version the speech model's own
package can't share.

    python design.py --description TEXT --text TEXT --language English --out SAMPLE.wav [--seed N]
"""

import argparse
import sys
import wave

MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--description", required=True, help="what the voice sounds like")
    parser.add_argument("--text", required=True, help="what the sample says")
    parser.add_argument("--language", default="English", help="the text's language, by name")
    parser.add_argument("--out", required=True, help="the WAV to write")
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()

    import numpy
    import torch
    from qwen_tts import Qwen3TTSModel

    if args.seed is not None:
        torch.manual_seed(args.seed)
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    print(f"loading {MODEL} on {device}", flush=True)
    model = Qwen3TTSModel.from_pretrained(
        MODEL, device_map=device, dtype=torch.bfloat16 if device != "cpu" else torch.float32
    )
    print("designing", flush=True)
    wavs, rate = model.generate_voice_design(text=args.text, language=args.language, instruct=args.description)
    samples = numpy.clip(numpy.asarray(wavs[0], dtype=numpy.float32), -1, 1)
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((samples * 32767).astype(numpy.int16).tobytes())
    print(f"done: {len(samples) / rate:.1f}s", flush=True)


if __name__ == "__main__":
    sys.exit(main())
