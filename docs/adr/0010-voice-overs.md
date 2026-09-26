---
status: accepted (built and run on the GPU: an SRT voice-over mixed over a clip's own sound)
---

# Voice-overs: a speech model on the image side, in a venv of its own

Clips can have sound now, but nobody chooses what is said or who says it. A voice-over should
come from a script: an SRT file, each line spoken at its time, in a voice cloned from a short
recording the user makes. The same track should go over a clip made with any workflow, silent or
not, and stand on its own too.

## Decisions

- **The speech model clones a voice from a short recording.** A 5–15 s sample is enough; an
  exaggeration setting controls how expressive the result is. Its output carries an inaudible
  watermark. Which model this is, and why it was chosen over the alternatives weighed, is a
  workflow choice, not part of den's own code — den is a broker, not a model provider (AGENTS.md,
  "Design rules"), and a workflow's model is named only in config, never here.
- **It runs in a venv of its own, not in ComfyUI.** Speech packages pin a torch and transformers
  that ComfyUI's venv has already moved past; installed there, any of them would downgrade what
  the image and clip models need. `setup.sh` makes the venv; the model downloads on the first
  start.
- **No third side.** Speech belongs to the image side: a voice request is admitted, batched and
  released like an image or a clip. Making it a side of its own would have meant reworking the
  broker's two-way swap for a model that loads in seconds. What the image side does differently
  for speech is inside the request: it asks ComfyUI to free its models (`POST /free`; they and
  the speech model don't fit on the same GPU together), starts the speech server on a private
  UNIX socket as it starts llama-server, speaks each line, and stops the server when the request
  ends, whatever happened. Nothing of speech stays loaded between requests.
- **den's own code stays stdlib.** The speech server runs in the speech venv; `den/speech.py`
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
- **Lip-sync puts the voice into the model instead of over the clip.** On a workflow that makes
  sound and picture together (its `lip_sync` key names where the audio latent goes), a
  voice-over with `sync` is padded to the clip's length, encoded by the audio VAE and kept fixed
  with a zero noise mask, as ComfyUI's image-and-audio-to-video template does; the model draws
  the picture to it, lips included. The saved clip carries the clean voice, not its trip through
  the VAE. A voice-over without `sync` stays a narrator over the picture.
- **A voice from a description is designed once, then cloned.** The speech model takes no
  description of a voice, only a recording. A separate voice-design model speaks a fixed sample
  text in a voice made from a description, and den keeps that sample in the voice library, so
  everything after is the one speaking path: the speech model's timing, its languages, its
  lip-sync. It pins its own, different transformers version, so it gets a venv of its own
  (`setup.sh`, `DESIGN_DIR`) and runs as a one-shot process per design (`speech/design.py`), an
  image-side request like speech, not a server. A design is a draft (`draft:ID`, outside the
  library, the last 20 kept) until someone has heard it and saves it under a name, as a pose is
  drawn before it's kept: a voice nobody listened to shouldn't end up in the library.
- **A transcription model writes down a recording.** `POST /transcribe` runs a transcription
  model in the same speech server, loaded only when something is transcribed; the transformers
  the speech model pins already carries it. A recording, the user's own narration, can also be a
  voice-over as it is (`audio`), over a clip or lip-synced into it: the speech server converts it
  to den's WAV, so it's measured, padded and mixed like a spoken track.
- **Lines without times are timed by den.** `lines` are spoken in turn, 0.4 s apart, and every
  voice job returns the SRT at the times its lines were actually spoken, saved next to the
  track: subtitles for free, and a script to reuse with those times.
- **Voices are recordings in a library outside the repo,** `~/.local/share/den/voices/`
  (`DEN_VOICES`), kept under a name (`den voice --add`, `POST /voices`, the phone). A request
  names a voice, passes a recording's path, or, from another machine, sends it as bytes.
  Tracks go to `~/Music/den/` (`DEN_SPEECH_OUT`) and are logged to `speech.jsonl`.

## Consequences

- `POST /voice` streams like `/image` and ends with the track; `POST /clip` takes a `voiceover`
  ({srt or text, voice, language}); `/client` carries the voices and languages where speech runs,
  so a client offers voice-overs only there.
- A clip with a voice-over takes the speech time on top (about 10 s for the model and a second
  or two per line): measured over two minutes for a short clip with two lines.
- A voice request waits behind a clip like any image-side request, and frees ComfyUI's models,
  so the next image or clip loads its model again.
- `den voice` and the voice tool don't reach a den on another machine yet; a clip's voice-over
  does (its script and recording go as content).
