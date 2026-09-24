---
status: accepted (built and run on the GPU: Chatterbox Multilingual V3 through the broker, an SRT voice-over mixed over an LTX-2.3 clip's own sound)
---

# Voice-overs: a speech model on the image side, in a venv of its own

Clips can have sound now (the LTX-2.3 workflows make it from the prompt), but nobody chooses
what is said or who says it. A voice-over should come from a script: an SRT file, each line
spoken at its time, in a voice cloned from a short recording the user makes. The same track
should go over a clip made with any workflow, silent or not, and stand on its own too.

## Decisions

- **Chatterbox Multilingual V3 is the speech model.** MIT-licensed, 0.5B, 23 languages, and it
  clones a voice from a 5–15 s recording; an exaggeration setting controls how expressive it
  is. Qwen3-TTS (Apache, 10 languages) and Kokoro (English only, no cloning) were the other
  candidates. Its output carries Resemble's inaudible Perth watermark.
- **It runs in a venv of its own, not in ComfyUI.** Every speech package pins what ComfyUI has
  moved past: `chatterbox-tts` pins torch 2.6.0 and transformers 5.2.0, `qwen-tts` pins
  transformers 4.57.3, `kokoro` won't install on Python 3.13. Installed into ComfyUI's venv, any
  of them would downgrade what the image and clip models need. `setup.sh` makes the venv
  (Python 3.12, torch 2.6.0 with CUDA 12.6, Chatterbox pinned by commit, since V3 isn't in a
  release yet); the model, about 3.2 GB, downloads on the first start.
- **No third side.** Speech belongs to the image side: a voice request is admitted, batched and
  released like an image or a clip. Making it a side of its own would have meant reworking the
  broker's two-way swap for a model that loads in seconds. What the image side does differently
  for speech is inside the request: it asks ComfyUI to free its models (`POST /free`; they and
  the speech model don't fit on a 12 GB GPU together), starts `speech/server.py` on a private
  UNIX socket as it starts llama-server, speaks each line, and stops the server when the request
  ends, whatever happened. Nothing of speech stays loaded between requests.
- **den's own code stays stdlib.** `speech/server.py` runs in the speech venv; `den/speech.py`
  reads SRT, keeps the voice library and assembles the track with `wave` and `array`. ComfyUI's
  built-in audio nodes (`LoadAudio`, `AudioAdjustVolume`, `AudioMerge`, `TrimAudioDuration`) do
  the mixing, so den needs no ffmpeg.
- **A script's times are kept, not trusted.** Each line starts at its cue. A line that runs long
  pushes the next one later rather than overlapping it, and every overrun becomes a note in the
  result. How long speech takes is only known once it's spoken, so for a clip the voice is
  made first: a clip with an SRT voice-over and no duration lasts to the script's end plus half
  a second, and if the spoken track is longer, den builds the clip's graph again, longer (within
  the workflow's range), before ComfyUI runs it.
- **A voice-over goes into the clip's own graph.** On a workflow with sound, the clip's audio is
  turned down 8 dB and the voice merged over it; on a silent workflow, or with sound off, the
  voice is the sound track, trimmed to the clip. One ComfyUI run, no second pass over the video.
- **Voices are recordings in a library outside the repo,** `~/.local/share/den/voices/`
  (`DEN_VOICES`), kept under a name (`den voice --add`, `POST /voices`, the phone). A request
  names a voice, passes a recording's path, or, from another machine, sends it as bytes.
  Tracks go to `~/Music/den/` (`DEN_SPEECH_OUT`) and are logged to `speech.jsonl`.

## Consequences

- `POST /voice` streams like `/image` and ends with the track; `POST /clip` takes a `voiceover`
  ({srt or text, voice, language}); `/client` carries the voices and languages where speech runs,
  so a client offers voice-overs only there.
- A clip with a voice-over takes the speech time on top (about 10 s for the model and a second
  or two per line): measured 125–134 s for a 4–5 s LTX-2.3 clip with two lines.
- A voice request waits behind a clip like any image-side request, and frees ComfyUI's models,
  so the next image or clip loads its model again.
- `den voice` and the voice tool don't reach a den on another machine yet; a clip's voice-over
  does (its script and recording go as content).
