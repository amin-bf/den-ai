---
status: accepted (built and run on the GPU: a voice request naming a second workflow, with a
strong-emotion vector, cloned from the same voice library the default workflow already uses)
---

# Speech workflows: a second speech model, chosen per request

ADR voice-overs settled on one speech model for every voice request. Its two-slider emotion
control (exaggeration, cfg_weight) stays flat on a strongly emotional line however far it's
pushed, because it's tuned for expressive delivery of any text, not a discrete emotion. A model
built around a named emotion vector is a genuinely different tool for that job, but it speaks far
fewer languages and needs an older Python and newer torch than either existing speech venv — a
plain replacement would trade breadth for depth den doesn't have to choose between.

## Decisions

- **A second speech workflow, not a second side or a replacement.** Speech already runs on the
  image side (ADR voice-overs); a request now also names which workflow speaks, the same way an
  image request names which ComfyUI workflow draws. den is a broker, not a model provider
  (AGENTS.md, "Design rules"): the default workflow stays default, and this one is picked only
  when a request asks for it by name.
- **Its own venv, never made by a plain `./setup.sh`.** It pins an older Python ceiling and a
  newer torch/CUDA than either the default speech venv or the voice designer's, so none of the
  three can share one — the same reasoning ADR voice-overs already used to justify two venvs
  now justifies three. `--with-emotion` installs it; a repo clone that never asks for it never
  builds that venv, downloads that model, or depends on its license.
- **Timbre and emotion are separate inputs.** The workflow clones a voice from the same short
  recording the default one does — den's voice library needs no change — and takes emotion as an
  8-number vector (happy, angry, sad, afraid, disgusted, melancholic, surprised, calm, each
  0–1) plus an intensity (`emo_alpha`, 0–1) layered on top of that clone. A request can set the
  one or two dimensions a line calls for and leave the rest at 0.
- **Only speaking is a second workflow.** Converting a recording and transcription stay the
  default workflow's job regardless of which workflow speaks a line: `den/speech.py`'s `Server`
  is parameterised by workflow for `/health` and `/speak` alone.
- **One workflow's language list is not another's.** The default workflow's 23 languages live in
  `den/speech.py`; a further workflow's own, usually much shorter list comes from
  `config.toml`'s `[speech.workflows.<name>]`, and a request naming a language its chosen
  workflow doesn't speak fails loudly rather than falling back silently.
- **One speech job at a time, whichever workflow.** The broker's existing speech lock (den has
  seen two voice jobs race the same process before this workflow existed) now covers every
  workflow, not just the default one: two speech models loaded together is untested, and the
  GPU may not hold both regardless.
- **The tool changes only when a second workflow exists.** `generate_voice`'s schema gains
  `workflow`, `emotion` and `emo_alpha` only once a further workflow is actually installed, so a
  caller without one sees the same tool as before.

## Consequences

- A voice request naming a workflow that isn't installed, or a language its workflow doesn't
  speak, fails with what's missing, the same as an unavailable image workflow does.
- Loading this workflow's model takes longer the first time (its files download on first use)
  and about 15–60 s to load into memory afterward, plus a few seconds per line; roughly 6–7 GB
  of VRAM alongside the freed image side, measured on a 12 GB card.
- A recurring voice or a clip's voiceover can ask for this workflow the same way it asks for a
  language or a setting today; nothing about the voice library, SRT timing or clip mixing changes.
