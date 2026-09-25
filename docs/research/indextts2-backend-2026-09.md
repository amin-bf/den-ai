# Adding IndexTTS-2 as a second, optional speech backend

*Scoping note, not an ADR. This asks whether and how IndexTTS-2 (or 2.5) could sit alongside
Chatterbox Multilingual as a selectable backend — not replace it. No code changes here; this is
the plan to test before committing to an ADR. Builds on
`docs/research/tts-emotional-range-2026-09.md`, which found IndexTTS-2's "Cry" demo and 8-dim
emotion vector to be the best-documented strong-emotion evidence of anything checked, gated
behind a proprietary license that's fine for this personal, non-commercial repo but structurally
different from Chatterbox's plain MIT.*

## 1. What primary sources actually show

Verified 25.09.2026 against the GitHub repo (`index-tts/index-tts`), its `pyproject.toml`, and
the `IndexTeam/IndexTTS-2` HuggingFace repo's file listing — not just the earlier research note's
secondary claims.

### Inference API

Not a one-line `model.generate(text, ...)` call like Chatterbox's. It's a class with a richer,
still-synchronous interface:

```python
from indextts.infer_v2_5 import IndexTTS2
tts = IndexTTS2(cfg_path="checkpoints/config.yaml", model_dir="checkpoints", use_bf16=True)
tts.infer(
    spk_audio_prompt="voice.wav",       # timbre: the voice to clone (like Chatterbox's `voice`)
    text="Hello world", lang="EN",
    output_path="out.wav",
    emo_audio_prompt="emo_sad.wav",     # OPTIONAL: a separate reference clip for emotion only
    emo_vector=[0,0,0.8,0,0,0,0,0],     # OR an 8-dim vector: happy/angry/sad/afraid/disgusted/
                                         #   melancholic/surprised/calm
    emo_alpha=0.9,                      # intensity, 0.0–1.0 (default 1.0) — narrower than the
                                         #   0.0–1.4 the research note cited from the tech page
    use_emo_text=True,                  # OR: derive emotion from the text itself (needs
                                         #   use_qwen_emo=True at init — the 1.2 GB Qwen model)
    duration_factor=1.2,                # 0.5–2.0 speaking-rate control, no Chatterbox equivalent
)
```

Confirms research-note point 4 precisely: **timbre and emotion are two separate reference
inputs.** `spk_audio_prompt` is exactly what den's voice library already stores (a short clone
sample) — reusable as-is. `emo_audio_prompt` is a *different* clip whose only job is to carry an
emotional tone (e.g., a sample of someone crying), independent of whose voice it is. Chatterbox
has nothing like this: it has no emotion-reference-audio concept at all, only the
`exaggeration`/`cfg_weight` sliders. So a "cry in this person's voice" call needs **two** audio
inputs den doesn't currently have a home for: the person's voice sample (have it) and an
emotional-delivery sample (don't have it) — or the numeric `emo_vector`, which needs no second
clip, or `use_emo_text`, which needs none either. The vector and text-based routes are the ones
that reuse den's library unchanged; the audio-reference route would need a second per-request
file or a small "emotion samples" library of its own, out of scope for a first cut.

The 8-dim vector confirms the research note's dimensions exactly (happy, angry, sad, afraid,
disgusted, melancholic, surprised, calm). `emo_alpha`'s documented range is 0.0–1.0 (default
1.0), not 0.0–1.4 as the tech page's UI slider showed — the UI likely allows overdrive past 1.0;
the Python default is 1.0. Treat 1.0 as the sane ceiling for a den parameter until tested.

### Dependencies — a real, sharp conflict

`pyproject.toml`: **`requires-python = ">=3.10,<3.12"`**, `torch==2.8.*` (CUDA 12.8 wheels on
Linux), `torchaudio==2.8.*`, `transformers==4.52.1`.

Compare against `setup.sh`'s speech venv: **Python 3.12**, torch 2.6.0 (CUDA 12.6), and
Chatterbox pins transformers 5.2.0 (the design venv separately pins 4.57.3 against Chatterbox's
own transformers, per ADR 0010). IndexTTS-2 needs an *older* transformers (4.52.1) than either
existing speech venv, an *older* Python ceiling (`<3.12`, so not even the version den currently
uses) and a *newer* torch/CUDA (2.8/12.8 vs 2.6/12.6) than Chatterbox. None of the three
version axes line up. This isn't a "might conflict" like the original research note's cautious
wording — it's a Python major-minor ceiling incompatible with the existing speech venv's
interpreter, on top of the same kind of torch/transformers pin clash that already justified
giving Chatterbox and the voice designer separate venvs in ADR 0010. **A third venv is not
optional, it's required**, exactly as ADR 0010's own reasoning ("every speech package pins what
[the other venv] has moved past") predicts for a third package.

### VRAM — sharper than "unknown," still not a first-party number

No explicit VRAM figure in the GitHub README, HuggingFace card, or technical report for either
IndexTTS-2 or 2.5. But the checkpoint sizes are now confirmed from HuggingFace's own file
listing, which is a reasonable proxy per the task's instructions:

| File | Size |
|---|---|
| `gpt.pth` | 3.48 GB |
| `s2mel.pth` | 1.2 GB |
| `qwen0.6bemo4-merge/model.safetensors` (text-emotion model, only needed for `use_emo_text`) | 1.19 GB |
| small config/tokenizer files | ~15 MB |
| **Total download** | **~5.9 GB** |

Loaded weights at fp32 would roughly match file size (~4.7 GB for the core GPT+s2mel pair,
without the emotion-text model); at bf16 (the flag `IndexTTS2(..., use_bf16=True)` supports)
roughly half that for the parts that support it, plus activation and KV-cache overhead during
generation. A rough floor is **"a few GB, likely under 6 GB just for weights in bf16,
meaningfully more with the optional Qwen emotion-text model loaded and in fp32."** This is
still not a confirmed peak-VRAM number — no first-party source gives one — but it's now backed
by real file sizes rather than an unverified third-party search snippet. **Treat actual runtime
VRAM as unknown until measured**, especially whether it fits alongside den's existing 12 GB-card
assumption *at the same time as* ComfyUI being freed (same pattern as Chatterbox today) or
*instead of* Chatterbox in a from-scratch load — the two are very different asks since one venv
process running by itself is far cheaper than trying to run two speech backends concurrently
(which den's design doesn't do and shouldn't start doing).

### Voice cloning

Confirmed: zero-shot from a single reference clip (`spk_audio_prompt`), same shape as Chatterbox
(`model.prepare_conditionals(recording, ...)`). **Den's existing voice library
(`~/.local/share/den/voices/`, a sample + `NAME.json` per voice) can be reused unchanged as the
source of `spk_audio_prompt`** — no format conversion beyond what `speech/server.py`'s existing
`/convert` already does (librosa resample to a WAV). No evidence IndexTTS-2 requires anything
different in the clip's length or format from what Chatterbox already accepts.

### Model download and access

Public, no login wall: `hf download IndexTeam/IndexTTS-2.5 --local-dir=checkpoints` (or the
ModelScope mirror) — an ordinary anonymous HuggingFace download via `huggingface-hub`, not `git
lfs` by hand, not gated. ~5.9 GB total (see above), in the same range as Chatterbox's ~3.2 GB
and the designer's ~4.5 GB.

### License

Unchanged from the earlier research note: `bilibili Model Use License Agreement`
(`LICENSE.txt`/`LICENSE_ZH.txt` in the repo), proprietary, with a >100M-MAU / >1B-RMB-revenue
commercial threshold, high-risk-use bars, and a restriction on using it to improve other AI
models. Amin has said license doesn't matter here (personal, non-commercial, and this would be
an optional, non-default backend) — noted for completeness, not a blocker. Per AGENTS.md's
public-repo discretion rules, the plan below keeps this backend's *setup* opt-in and its own
venv, so a clone that skips it never touches bilibili's terms at all.

## 2. Design, matched to den's existing patterns

### Where it lives: a third venv, `speech2/`

Following ADR 0010's own precedent (Chatterbox's venv, the voice designer's separate venv, both
justified purely by pin conflicts) and AGENTS.md's stdlib-only rule with `speech/server.py` as
the one named exception: a new **`speech2/server.py`** (or `speech/indextts_server.py` — naming
below) runs in its own venv, `INDEXTTS_DIR` (default `~/.local/share/den/indextts/`), Python
3.11 (the newest IndexTTS-2 accepts), torch 2.8.0/CUDA 12.8, transformers 4.52.1. It is *not*
folded into the existing `speech/` venv — confirmed above that the Python ceiling alone rules
that out, before even reaching the torch/transformers mismatch.

**Naming:** the repo already has one file per backend-process (`speech/server.py`,
`speech/design.py`, `speech/live.py`) all under `speech/`, so a second backend server as
`speech/indextts_server.py` (same directory, its own venv referenced by absolute interpreter
path, same as `design.py` and `server.py` already do) keeps one directory instead of
introducing `speech2/` as a sibling module — `speech/` is already "the speech-side processes,"
not "the Chatterbox process." This avoids implying there's a second, parallel `den/speech2.py`
broker-side module too, which there shouldn't be (see below).

**HTTP interface**, mirroring `speech/server.py`'s shape so `den/speech.py`'s `Server` class
needs minimal branching:

```
GET  /health   {"ready": bool, "error": str|None}
POST /speak    {text, language, voice?, emo_vector?, emo_audio?, emo_alpha?, use_emo_text?,
                duration_factor?, seed?} -> audio/wav
```

No `/convert` or `/transcribe` needed on the IndexTTS-2 server — those stay Chatterbox/Whisper's
job regardless of which backend speaks. A voice job that wants a recording converted or
transcribed keeps using the existing speech server; only `/speak` gets a backend choice.
`language` maps to IndexTTS-2's `lang` (its language set is much smaller — see open risks).

### `den/speech.py`: two `Server`-like classes, one call site

The existing `Server` class stays exactly as it is for Chatterbox. Add a second, structurally
identical class (or, better, one `Server` class parameterised by which backend's venv/script/
socket/log paths it uses — the `start`/`_call`/`stop` logic is backend-agnostic HTTP-over-
UNIX-socket plumbing that doesn't care which model is on the other end). Concretely:

- Factor `PYTHON`, `SERVER`, `SOCKET`, `SERVER_LOG` into a small `_BACKENDS` table (or a
  `Backend` namedtuple) keyed by name (`"chatterbox"`, `"indextts"`), each with its own
  venv/script/socket/log paths (`INDEXTTS_DIR`, a new `DEN_INDEXTTS_SOCKET`, etc., following the
  existing `SPEECH_DIR`/`DEN_SPEECH_SOCKET` env-override pattern).
  `Server.__init__(self, backend="chatterbox")` picks the row; `start`/`speak`/`stop` are
  unchanged code, just reading from `self._paths` instead of module globals.
  This is smaller than two parallel classes and keeps one code path to test.
- `unavailable()` becomes `unavailable(backend="chatterbox")`, checking that backend's venv.
- `speak(text, language, voice, options)` stays the shared surface; `options` already flows
  through as a dict (`{k: spec[k] for k in (...) if spec.get(k) is not None}` in
  `broker.py:_voice_request`, roughly), so IndexTTS-2-only keys (`emo_vector`, `emo_alpha`,
  `emo_audio`, `use_emo_text`, `duration_factor`) pass through the same way Chatterbox's
  `exaggeration`/`cfg_weight` do today — no new plumbing needed there, only new allowed keys.

### `den/broker.py`: one new field, same call sites

`broker.speech` is currently a single `Server()` instance held for the process's life
(`self.speech = speech.Server()` in `Broker.__init__`), started/stopped per job. The minimal
change:

- `Broker.__init__` keeps one `speech.Server()` but the request now carries which backend to
  use; `voice_track`/`transcribe`/`speak_lines` pass `backend=request.get("backend", "chatterbox")`
  into `broker.speech.start(emit, backend=...)` (or the class is re-instantiated per job with
  the right backend — jobs are already serialized one-at-a-time under `broker.speech_lock`, so
  there's no concurrency hazard in picking the backend fresh each time; a job already fully
  starts and stops the process, it doesn't keep one running between jobs).
- **Simplest correct shape:** don't keep a long-lived `Server` instance field at all changes —
  keep `broker.speech` as today for Chatterbox (default, unconditional availability check), and
  construct `speech.Server(backend)` locally inside `voice_track`/`speak_lines`/`transcribe`
  when `request.get("backend")` names something other than the default. This avoids restructuring
  `Broker.__init__` and matches "availability, not modes": a backend that isn't installed simply
  isn't offered, same as an image workflow whose files aren't there.
- `_free_comfy_for_speech` is backend-agnostic already (it just frees ComfyUI's VRAM before
  *any* speech server loads) — no change needed there.
- `speech_lock` already serializes "one voice job on the speech side at a time," and that
  invariant should extend to "one job on the speech side at a time, whichever backend it uses"
  — two backends must never run concurrently, both because of the VRAM-freeing dance with
  ComfyUI and because nothing has verified two speech models fit together. **Do not run
  Chatterbox and IndexTTS-2 concurrently even during a swap** — the lock already prevents this
  by construction as long as both backends go through the same `speech_lock`.
- `_voice_request` (the function around line 860-885 building the request dict from a client's
  spec) gains one more passthrough key: `backend` (default `"chatterbox"`), validated against
  whichever backends `unavailable()` reports as installed, and the IndexTTS-2-only option keys
  added to the existing `options = {k: spec[k] for k in (...) if spec.get(k) is not None}` line
  guarded by `if backend == "indextts"`.

### `config.toml`: a `[speech.backends.*]` block, following the workflow pattern

There is no `[speech]` table in `config.toml` today — Chatterbox's paths are all environment
variables and constants in `den/speech.py`, not config. Introducing backend *selection* as
config (not just env vars) fits the "workflows, not model names" precedent
(`[image.workflows.<name>]`) better than adding more env vars:

```toml
[speech]
default_backend = "chatterbox"   # what a request gets when it names none

[speech.backends.chatterbox]
enabled = true                    # true once the venv exists; setup.sh could also just rely on
                                   # unavailable() and skip this key entirely for the default

[speech.backends.indextts]
enabled = true
languages = ["en", "zh"]          # far short of Chatterbox's 23 — see open risks
```

Availability still ultimately comes from `unavailable(backend)` checking the venv exists
(the "availability, not modes" rule), so `enabled` here is closer to a default's presence than a
kill switch — arguably `config.toml` doesn't even need an `enabled` flag if a missing venv is
already sufficient, mirroring how image workflows are "available" purely by their model files
being present, no separate `enabled=true` required unless hiding a replaced example
(`enabled = false` is only used for an example a private workflow has superseded). Simplest:
skip `enabled` entirely and let `unavailable("indextts")` do all the work, keeping only
`default_backend` and `languages` (needed because IndexTTS-2's language set is much narrower
than Chatterbox's `LANGUAGES` dict and the tool schema needs to know which languages a given
backend can be asked for).

### Voice library and MCP tool surface

**No schema change to the voice library itself.** A voice's `NAME.json` (recording, description,
how it was made) doesn't need an emotion field — emotion is a per-*call* parameter (like
`exaggeration` today), not a property of the stored voice, since the same cloned voice can be
asked to sound happy or sad on different calls. This matches ADR 0010's own framing of
`exaggeration` as a call-time knob, not a voice attribute.

**`generate_voice`'s MCP schema** gains, guarded by backend availability (only listed when
`indextts` is installed, following "delegation is opt-in per task" / tools reflecting real
availability):

- `backend`: `"chatterbox" | "indextts"` (enum), default `chatterbox`.
- `emotion`: an object exposing a practical subset of the 8 dimensions rather than all 8 raw
  floats — Claude picking 8 independent 0–1 sliders per call is worse UX than a smaller, named
  set. Concretely, expose the 8 names as an object with optional 0.0–1.0 fields
  (`{"happy":0.0,"angry":0.0,"sad":0.8,"afraid":0.0,"disgusted":0.0,"melancholic":0.0,
  "surprised":0.0,"calm":0.0}`), all optional and defaulting to 0, translated straight into
  `emo_vector`'s fixed 8-element order server-side — this is a direct mapping, no lossy
  simplification, and lets Claude set just the one or two dimensions a line calls for.
- `emo_alpha`: number, 0.0–1.0, default 1.0 (matching the Python default, not the tech page's
  0–1.4 UI range, until testing shows overdriving past 1.0 is worth exposing).
- Do **not** expose `emo_audio_prompt` (a second reference clip) or `use_emo_text` in the first
  cut — the former needs a second per-call file or a new library concept ("emotion samples")
  that doesn't exist yet, and the latter pulls in the extra 1.2 GB Qwen emotion-text model for a
  feature (`emo_vector` already does the job more controllably). Both are natural follow-ups,
  not blockers for a first working integration.
- `exaggeration`/`cfg_weight` stay Chatterbox-only params, simply ignored/rejected when
  `backend="indextts"` (mirroring how a workflow's mapping keys differ per model already).

## 3. Rough size of the change

New files:
- `speech/indextts_server.py` (~150-200 lines — closely mirrors `speech/server.py`'s structure:
  load, `/health`, `/speak`, HTTP handler boilerplate; smaller than the original since no
  `/convert`/`/transcribe` needed).
- `setup.sh` additions (~30-40 lines — a new `--no-indextts` flag, `INDEXTTS_DIR`, a step
  parallel to the existing speech/design steps, `uv venv --python 3.11`, the pinned
  `torch==2.8.*`/`transformers==4.52.1` install, `hf download` for the checkpoints).

Modified files:
- `den/speech.py`: refactor `Server`'s hardcoded paths into a per-backend table (~40-60 lines
  changed/added), new `unavailable(backend)` signature (small).
- `den/broker.py`: `_voice_request` gains `backend` + emotion-key passthrough (~10-15 lines);
  `voice_track`/`speak_lines`/`transcribe` construct the right `Server` (~10-20 lines, mostly
  unwinding the single hardcoded `broker.speech` into a locally-picked instance).
- `den/mcp_server.py`: `voice_tool`'s schema gains `backend`, `emotion`, `emo_alpha` (~20-30
  lines, mostly schema JSON), conditioned on `offered["voice"]` reporting the backend as
  available (small addition to whatever `/client` already reports about voice).
- `config.toml`: a `[speech]` table (~10 lines).
- `docs/adr/0010-voice-overs.md`: either an amendment noting a second backend, or (more likely,
  given ADR conventions here of one decision per file) a new **ADR 0012** documenting the
  decision to add IndexTTS-2 as an optional second backend, referencing 0010 rather than editing
  it — 0010's own text is about *why Chatterbox* was chosen and *why one speech process at a
  time*; a second backend extends rather than reverses either point, which is exactly what a new
  ADR referencing an old one is for.
- `skills/den-voice/`: a short addition noting the backend option and its narrower language/
  emotion trade-off, once it's real enough to recommend using.

**Total: 2 new files, roughly 5-6 modified files, on the order of 300-400 net-new lines
including the new server file** (the new server file is the bulk of it; the broker/speech.py/
mcp_server.py changes are individually small because the existing "one job, one server, start/
speak/stop" shape already generalizes to a second backend almost for free — the design
deliberately avoids introducing a second parallel code path in `den/speech.py` or
`den/broker.py`, reusing the same `Server` shape and the same `speech_lock`).

Reused as-is, unchanged: the voice library and its files, `speech.script`/`speech.assemble`/
`speech.to_srt` (SRT and track handling doesn't care which model produced the WAV), the
`speech_lock` serialization, `_free_comfy_for_speech`, the clip-voiceover and lip-sync code
paths in `broker.py` (they consume a WAV, not a backend), `den/speech.py`'s `LANGUAGES` dict for
Chatterbox (a second, smaller dict or a `languages` list per backend in config covers
IndexTTS-2's narrower set).

## 4. Open risks and unknowns — test before committing to an ADR

1. **Actual VRAM alongside den's 12 GB-card assumption is still unmeasured.** The ~5.9 GB
   download is a real number now; peak VRAM during generation (activations, KV-cache, bf16 vs
   fp32) is not confirmed by any first-party source. Needs a real load-and-generate test on the
   actual machine, watching `nvidia-smi`, with ComfyUI freed exactly as `_free_comfy_for_speech`
   already does for Chatterbox.
2. **Python 3.11 vs den's existing venvs' 3.12** means `uv venv --python 3.11` must actually
   resolve on this machine (a 3.11 interpreter needs to be installable via uv, not assumed
   present) — a `setup.sh` prerequisite to verify, not just assume.
3. **Does `emo_alpha`/`emo_vector` actually sound natural, or still "robotic" at extremes?**
   The tech page's own demo page is a curated showcase; nothing here has heard an *arbitrary*
   line said with an arbitrary vector. This needs ear-testing on real den content (a narration
   line, not the project's chosen demo sentence) before deciding the feature is worth the venv's
   disk and setup cost.
4. **Language coverage is a real regression for any request that doesn't specify a backend.**
   IndexTTS-2's confirmed languages (English, Chinese; IndexTTS-2.5 adds Japanese, Spanish,
   Arabic per the earlier research note) are a small fraction of Chatterbox's 23. `backend`
   must never silently become the default for a language it doesn't support — the config's
   `languages` list per backend, validated in `_voice_request` the same way `LANGUAGES` already
   is, prevents this, but it needs an explicit test: a German line requested with
   `backend="indextts"` should fail loudly (matching AGENTS.md's "fail loudly" design rule), not
   silently fall back to English or Chatterbox.
5. **Does `use_bf16=True` actually reduce VRAM meaningfully on this card**, and is bf16 even
   supported by the GPU in question? Needs a same-machine check, not an assumption from the
   RTX 4090 number the GitHub page quotes for inference speed (a different card).
6. **Whether 2.5's checkpoint differs enough from 2's to matter** — the repo's own naming
   (`infer_v2_5`) and the separate HuggingFace repo (`IndexTeam/IndexTTS-2.5`) suggest 2.5 is
   the one to target directly rather than 2, since 2.5 is what the inference class in the
   examples above actually is; this plan targets 2.5 throughout, and that should be made
   explicit in the eventual ADR title/text rather than hedging between "IndexTTS-2(.5)."
7. **CUDA 12.8 toolkit availability on the host** — `setup.sh`'s existing `SPEECH_TORCH_INDEX`
   pattern assumes a `cu126` wheel index for Chatterbox; a `cu128` index for IndexTTS-2.5's
   torch 2.8 build needs the same treatment (a separate `INDEXTTS_TORCH_INDEX` env var, matching
   the existing per-package override pattern), and needs confirming the installed NVIDIA driver
   actually supports CUDA 12.8 (newer than 12.6) before assuming the wheel will run, not just
   install.

None of these are blockers to writing the code, but all six should be answered by a real,
on-machine test (venv build, model download, one generation, one `nvidia-smi` reading, one
foreign-language rejection) before wiring `generate_voice`'s MCP schema to it — the same
discipline ADR 0010 itself followed (each of its decisions is annotated as "built and run on the
GPU," not speculative).

## 5. Recommendation

Worth prototyping as a standalone script first (not yet through `den/broker.py`): a one-off
`speech/indextts_server.py`-shaped script run directly, hand-fed a line and a voice sample, to
answer risks 1, 3, 5 and 7 above before touching `den/speech.py` or `den/broker.py` at all. If
that prototype's VRAM and quality hold up, the broker/config/MCP wiring described in §2 is
small and low-risk to add, because it deliberately reuses `speech.Server`'s existing shape
rather than inventing a second one.
