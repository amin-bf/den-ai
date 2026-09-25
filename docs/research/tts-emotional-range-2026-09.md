# Local TTS models with strong emotional range — a look beyond Chatterbox Multilingual

*Research note, not an ADR. `docs/adr/` is reserved for decisions already made; this file
starts a `docs/research/` location for open investigations that may or may not turn into one.*

## Context

den-ai currently speaks through **Chatterbox Multilingual** (resemble-ai/chatterbox, MIT,
~0.5B params) for both voice cloning and voice design, covering the 23 languages in
`den/speech.py`'s `LANGUAGES` dict, on a machine with roughly 12 GB of VRAM shared with
ComfyUI. Its only emotion controls are two continuous sliders (`exaggeration`, `cfg_weight`)
plus a handful of paralinguistic tags — there is no dedicated "cry" or "scream" mode. This note
asks whether a newer local, GPU-runnable model does meaningfully better at strong emotional
delivery (real crying, screaming, vocal breaks, not just "expressive" marketing copy), while
still fitting a 12 GB card, staying cleanly licensed, and being safe to reference from a
**public** repo.

## Comparison table

| Model | Maintained? | License (code / weights) | VRAM | Languages | Emotional range evidence | Fits den's constraints? |
|---|---|---|---|---|---|---|
| Chatterbox Multilingual (current) | Yes — active HF repo | MIT / MIT | Not stated in README/model card (den runs it today on ~12 GB shared with ComfyUI) | 23+ (den uses exactly the 23 in scope) | `exaggeration`/`cfg_weight` sliders only; no discrete emotion tags documented on the multilingual model card | Yes (in production) |
| Chatterbox Turbo | Yes — new HF repo, active | MIT / MIT | Not stated; described only as using "less compute and VRAM" than prior Chatterbox models | English only | `[cough]`, `[laugh]`, `[chuckle]` tags documented as native to Turbo | English-only, so at best a bolt-on, not a replacement |
| Chatterbox Nano | Yes — sibling of Turbo | MIT / MIT | Not stated; smallest of the three (110M) | English only | Same tag set as Turbo (inherited, per model card) | English-only bolt-on only |
| Breeze TTS 2 (BreezeBlue/RESONIA) | Yes — released 2026-08-25, active | Apache-2.0 (code) / **BreezeBlue Research and Non-Commercial License** (weights & self-hosted outputs) | ~7.7 GiB eager (12 GB GPU) / ~14.4 GiB fast path (24 GB GPU) | **2**: English and Chinese (Mandarin) only, per its own model card | Voice Direction docs mention laughter, coughing, throat-clearing, sighing; **no documented crying/screaming benchmark or demo found** | VRAM fits (eager mode); language count (2) does not meet den's 23-language need; non-commercial weights license is a legal question mark for a public repo (see below) |
| IndexTTS-2 / IndexTTS-2.5 | Yes — IndexTTS-2.5 released 2026-08-10 | **bilibili Model Use License Agreement** (proprietary, code+weights) | Not stated for IndexTTS-2 in its README; IndexTTS-2.5 reported (third-party, unverified) around 6 GB | 2 stated in IndexTTS-2 HF tags (en/zh); IndexTTS-2.5 site states 5: Chinese, English, Japanese, Spanish, Arabic | **Explicit "Cry" demo category** on the project's own GitHub Pages site (Section 2.1), plus an 8-dimension emotion vector (happy/angry/sad/afraid/disgusted/melancholic/surprised/calm) — the most specific documented emotion evidence of anything checked | Fits VRAM loosely; language count well short of 23; license is proprietary with revenue/MAU thresholds requiring separate licensing — a bad fit for a public MIT-adjacent repo |
| Fish Audio S2 / OpenAudio (fish-speech) | Yes — active (740 commits) | **Fish Audio Research License** (custom, non-commercial by default) for both code repo and weights | Not documented in README | "Over 80 languages" across 3 tiers per README | 15,000+ tags including `[screaming]`, `[shouting]`, `[angry]`, `[sad]`; **no explicit crying tag found** | Widest language coverage of anything checked, and a real `[screaming]` tag — but the license is non-commercial/custom and explicitly bars using outputs to improve "foundational generative AI models," a broad restriction to carry into a public repo |
| Higgs Audio v2 (boson-ai) | Yes — v2 current at time of check; repo also references a "v3" README not independently confirmed | Apache-2.0 (inference code) / **Boson Higgs Audio 2 Community License** (weights — free under 100,000 MAU, else requires an expanded license from Boson AI) | Not documented | "100+ languages" claimed in README; no itemized list found | High win-rate numbers vs. competitors on an emotion benchmark (75.7%) per README, but no crying/screaming-specific demo found | VRAM undocumented; weights license is commercial-friendly at small scale but not a clean permissive license; **the "v3" reference could not be verified against a primary source and should be treated as unconfirmed** |
| Bark (suno-ai) | **No — appears abandoned.** No GitHub releases at all; last substantive README activity dates to 2023-05-01 | MIT / MIT | ~12 GB full model; ~2–8 GB with smaller model flags, per README | 13 languages listed in README | README explicitly states the model "can also produce nonverbal communications like laughing, sighing and crying," plus `[sighs]`, `[gasps]` tags — but the README itself warns output is unpredictable ("could be anything from perfect speech to multiple people arguing") | Cleanest license of the alternatives, but effectively unmaintained and non-deterministic; not a serious candidate for production voice-over work |

## Per-model detail

### 1. Chatterbox Turbo / Nano (resemble-ai/chatterbox)

- **Language scope:** the HuggingFace model card and GitHub README both describe Turbo and
  Nano as **English-only**, distinct from Chatterbox Multilingual's 23+-language support.
  (Sources: https://huggingface.co/ResembleAI/chatterbox-turbo ,
  https://github.com/resemble-ai/chatterbox)
- **Paralinguistic tags:** the README/model card explicitly names `[cough]`, `[laugh]`, and
  `[chuckle]` as "native to the Turbo model" (and present in Nano); no exhaustive tag list
  beyond these three was found in either primary source. This is narrower, not broader, than
  what would be needed for crying/screaming.
- **VRAM:** neither the GitHub README nor the HuggingFace model cards for Turbo, Nano, or
  Multilingual state exact VRAM figures. The Turbo card only claims it "delivers high-quality
  speech with less compute and VRAM than our previous models" — a relative, unquantified claim.
- **License:** GitHub `LICENSE` file and the HuggingFace model card both state **MIT** for
  Turbo, Nano, and Multilingual alike — code and weights carry the same license, no divergence
  found. (https://github.com/resemble-ai/chatterbox/blob/master/LICENSE ,
  https://huggingface.co/ResembleAI/chatterbox-turbo , https://huggingface.co/ResembleAI/chatterbox)
- **Architectural fit for den:** Turbo is smaller (350M vs 500M params, per both model cards)
  and English-only. Given den's "one speech server process at a time" design (`speech/server.py`,
  started per job on a private UNIX socket, stopped after), running Turbo *instead of*
  Multilingual for English-language jobs and Multilingual for everything else is technically
  possible — den already treats the speech server as a single process it starts and stops per
  job, so swapping which checkpoint that process loads based on requested language is a
  config/routing change, not an architecture change. The cost is complexity (two model configs,
  a language-based routing rule, twice the download/disk footprint) for a gain that, per the
  primary sources, is model size/speed only — Turbo's documented tag set is a subset of what
  Multilingual already exposes via `exaggeration`/`cfg_weight`, and no primary source claims
  Turbo has *better* emotional range, only that it's faster/smaller for English. This does not
  address the "stronger emotional range" goal at all.

### 2. Breeze TTS 2 (BreezeBlue / RESONIA)

- **Voice Direction (technical):** per the model's own HuggingFace card, Voice Direction
  "clones a voice from reference audio while steering tone, emotion, pace, and delivery." It
  takes reference audio, a reference-text transcript, target text, and a natural-language
  instruction string describing the desired delivery; the instruction strength is controlled via
  a `--cfg-scale` parameter (documented example: `--cfg-scale 4`). This is instruction-conditioned
  generation layered on top of voice cloning, not a discrete emotion label system.
  (https://huggingface.co/BreezeBlue/Breeze-TTS-2)
- **Voice Design (technical):** creates a voice "from a natural-language description without
  reference audio," also via an `--instruction` parameter and the same `--cfg-scale` mechanism.
  The card notes the instruction's language should match the target text's language.
- **VRAM:** the model card states **~7.7 GiB for eager inference** (12 GB GPU recommended) and
  **~14.4 GiB for the fast path** (`--fast-all` flag, 24 GB GPU recommended). Confirmed
  consistently across two separate fetches of the same HuggingFace card.
- **License — the key question for den:** the code (inference scripts, tokenizer) is
  **Apache-2.0**. The **model weights, checkpoints, adapters, derivative models, and
  self-hosted outputs** are separately governed by the **"BreezeBlue Research and
  Non-Commercial License,"** and the card is explicit that Apache-2.0 covering the code "does
  not grant rights to use the model commercially." Commercial use requires a paid subscription
  through breezeblue.ai's hosted platform, or written authorization from RESONIA, INC. The card
  does not phrase the restriction as "commercial products/services only" — it restricts the
  weights and **any self-hosted output** to research/non-commercial use, full stop. A personal,
  non-commercial, publicly-visible repo like den-ai plausibly qualifies as "non-commercial use"
  under this license's own terms (den is not sold, has no revenue), but this is not the same as
  a permissive license — den would be relying on the non-commercial carve-out applying to it,
  not on an unrestricted grant, and the license terms should be read in full (not just this
  summary) before depending on that reading for anything beyond personal use.
- **Language support:** the model's own HuggingFace card states **"Bilingual Support —
  Generates natural English and Chinese speech with a single model."** This is 2 languages,
  confirmed by a second, independent fetch. A "50 languages" figure that surfaces in
  third-party blog coverage refers to BreezeBlue's *hosted service*, not the open-weight model;
  the GitHub README and HF tags list only English and Chinese. This alone rules Breeze TTS 2 out
  as a Chatterbox Multilingual replacement for den, which needs 23 languages.
- **Emotional-range evidence:** the model card documents vocal-event support (laughter,
  coughing, throat-clearing, sighing) but I found **no benchmark, demo page, or documented
  example specifically of crying or screaming** for Breeze TTS 2 in the primary sources
  checked. The "steers tone/emotion" framing is closer to marketing copy than a specific,
  demonstrated capability for the strong-emotion use case den is asking about.

### 3. Other actively-maintained candidates

**IndexTTS-2 / IndexTTS-2.5** (index-tts/index-tts on GitHub; IndexTeam on HuggingFace)
- Maintained: yes — IndexTTS-2.5 released 2026-08-10 per the GitHub repo's own news section;
  this is the most recently updated model among all candidates checked.
- License: **bilibili Model Use License Agreement**, a proprietary license covering "model
  weights and final code" together (not split like Breeze/Higgs). It carries scale thresholds
  (organizations over 100M MAU or >1B RMB annual revenue must seek a separate license), bars
  high-risk uses (medical, autonomous driving, military, critical infrastructure), and restricts
  using the model to improve other AI models except non-commercial ones or IndexTTS derivatives.
  (https://github.com/index-tts/index-tts/blob/main/LICENSE)
- VRAM: not stated for IndexTTS-2 in its own README; a ~6 GB figure for IndexTTS-2.5 appeared
  only in a third-party/aggregator search snippet and **could not be confirmed against the
  project's own GitHub or HuggingFace pages** — treat as unverified.
- Languages: the IndexTTS-2 HuggingFace card's tags indicate English/Chinese; the IndexTTS-2.5
  project page (index-tts.github.io) states support for Chinese, English, Japanese, Spanish and
  Arabic (5 languages) — well short of den's 23.
- Emotional range: this is the standout finding of the whole search. The project's own GitHub
  Pages technical report (index-tts.github.io/index-tts2.github.io) documents an **explicit
  "Cry" category** as one of its demonstrated emotion controls (Section 2.1, with paired
  synthesis samples), and a separate 8-dimension emotion-vector system (happy, angry, sad,
  afraid, disgusted, melancholic, surprised, calm) with an `emo_alpha` intensity control
  (0.0–1.4 per dimension). This is the most specific, first-party documented crying capability
  found in this research — more specific than anything in Chatterbox's own materials. No
  screaming-specific demo was found.
- Verdict: the emotional-range documentation is genuinely the best of the field, but the
  license (proprietary, code+weights bundled, scale-gated) and thin language coverage make it a
  poor structural fit for a public, MIT-adjacent, 23-language tool like den.

**Fish Audio S2 / OpenAudio S1 (fishaudio/fish-speech)**
- Maintained: yes, actively (740 commits on the GitHub repo at time of check).
- License: **Fish Audio Research License**, a custom non-commercial-by-default license
  covering both the repo and the weights (unlike Breeze/Higgs, which split code vs. weights).
  It explicitly requires a separate written commercial license for any "Commercial Purpose"
  and separately bars using the materials "to create or improve any foundational generative AI
  model." It also requires attribution ("Built with Fish Audio") on distributed materials.
  (https://github.com/fishaudio/fish-speech/blob/main/LICENSE)
- VRAM: not documented in the README content retrieved.
- Languages: README claims "over 80 languages" across three support tiers — the broadest
  claimed coverage of any candidate, comfortably exceeding den's 23-language list, though I did
  not independently verify all 80 are production-quality rather than best-effort.
- Emotional range: the README documents "15,000+ unique tags" including `[whisper]`,
  `[excited]`, `[angry]`, `[laughing]`, `[singing]`, `[sad]`, **`[screaming]`**, `[shouting]`,
  `[surprised]`. This is the only candidate with an explicit, named `[screaming]` tag in its own
  documentation. No explicit `[crying]` tag was found in the material retrieved.
- Verdict: best documented screaming support and by far the widest language list, but the
  custom non-commercial research license is more restrictive in spirit than Breeze's (it also
  reaches into "don't use this to improve other generative models," a term that has nothing to
  do with den's use case but is broad enough to warrant caution before referencing it in a
  public repo).

**Higgs Audio v2 (boson-ai/higgs-audio)**
- Maintained: the GitHub repo content retrieved referenced a "v3" release directing users away
  from the repo entirely; I could **not independently confirm a "Higgs Audio v3" release
  against a primary source** in this session (my searches surfaced only v2 artifacts:
  `bosonai/higgs-audio-v2-generation-3B-base` on HuggingFace, the "Boson Higgs Audio 2 Community
  License"). Treat any "v3" claim as unverified; v2 itself is a real, actively referenced
  release.
- License: **inference code is Apache-2.0**; **model weights are under the "Boson Higgs Audio 2
  Community License,"** a Llama-3-license-derived, non-permissive-but-commercial-capable
  license: free to use up to 100,000 monthly active users, beyond which an expanded license
  must be requested from Boson AI. This is commercial-friendly at small/personal scale but is
  not a clean open license, and differs from MIT in kind (usage-scale gated, not just
  attribution-gated).
- VRAM: not documented in the material retrieved.
- Languages: README claims "100+ languages" for conversational TTS, with no itemized list found
  in the content retrieved — this figure could not be cross-checked against a language table.
- Emotional range: README cites a benchmark win-rate of 75.7% on what's described as an
  "emotion" evaluation category versus competing systems, but **no crying- or screaming-specific
  demo or benchmark was found**; this is a comparative score, not a documented capability list.
- Verdict: interesting on paper (wide language claim, commercially usable at small scale) but
  the emotional-range evidence found is a benchmark percentage, not a specific documented
  capability — weaker evidence than either Fish Audio's `[screaming]` tag or IndexTTS-2's "Cry"
  demo.

**Bark (suno-ai/bark)**
- Maintained: **no.** The GitHub Releases page shows no releases at all
  ("There aren't any releases here"), and the most recent substantive README activity found
  dates to 2023-05-01. This is the only candidate confirmed abandoned.
- License: **MIT**, matching Chatterbox — the cleanest license of any alternative checked.
- VRAM: README states ~12 GB for the full model, with smaller/optimized configurations usable
  in roughly 2–8 GB via documented environment flags.
- Languages: 13 languages listed in the README (English, German, Spanish, French, Hindi,
  Italian, Japanese, Korean, Polish, Portuguese, Russian, Turkish, simplified Chinese) — short
  of den's 23.
- Emotional range: the README explicitly states Bark "can also produce nonverbal
  communications like laughing, sighing and crying," alongside tags like `[sighs]`, `[gasps]`,
  `[clears throat]`. This is a genuine, first-party documented crying claim — but the same
  README warns the model is non-deterministic and "could be anything from perfect speech to
  multiple people arguing," which is a serious reliability caveat for production voice-over use,
  compounded by the lack of any ongoing maintenance.

## Recommendation for den-ai

**Keep Chatterbox Multilingual as the primary/default model. Do not switch wholesale to any
of the alternatives evaluated, and be cautious about adding any of them given den-ai's public,
personal, MIT-adjacent posture.** Reasoning:

- **None of the alternatives meet den's 23-language requirement while also having a clean
  license.** Breeze TTS 2 (2 languages) and IndexTTS-2/2.5 (2–5 languages) fall far short.
  Fish Audio's 80+ languages and Higgs Audio's 100+ languages claims would cover it, but both
  carry non-permissive licenses (Fish: custom research/non-commercial with an anti-"improve
  other generative models" clause; Higgs: MAU-gated commercial license) that are a worse fit for
  a public MIT-licensed toolchain than staying on Chatterbox's plain MIT.
- **The single best-documented "strong emotion" evidence found — IndexTTS-2's explicit "Cry"
  demo category — sits behind the least permissive license of the group** (proprietary,
  code+weights bundled, scale-gated, third-party training restrictions), and its language count
  is far short of what den needs. It would only make sense as a narrow, opt-in addition for
  specific emotional beats in English/Chinese content, never as a Multilingual replacement, and
  even then the license terms deserve a careful read (not just this summary) before any code
  references it publicly.
- **Fish Audio's `[screaming]` tag is the most specific documented screaming support found**,
  and its language breadth is the widest of anything checked — but its license explicitly
  requires a separate commercial agreement for anything beyond research/personal use, and reaches
  further than Breeze's restriction (it also touches "don't use this to improve other generative
  models," irrelevant to den's use case but broad enough to warrant caution in a public repo that
  documents its own tool choices for others to read).
- **Chatterbox Turbo/Nano don't solve the emotional-range question at all** — they're
  faster/smaller English-only siblings with the *same* two-slider emotion control as
  Multilingual, plus three narrow tags (`[cough]`, `[laugh]`, `[chuckle]`). Routing English jobs
  to Turbo is architecturally easy given den's "start the speech server per job" design, but it
  would add real complexity (two checkpoints, a routing rule, more disk) for a speed/size win
  only — not a stronger-emotion win. Not worth doing on emotional-range grounds; it would only
  make sense if den separately wanted faster English-only jobs.
- **Bark's crying claim is real but the project is abandoned** (no releases, no meaningful
  activity since May 2023) and self-described as non-deterministic ("could be anything from
  perfect speech to multiple people arguing"). Not viable for production voice-over work
  regardless of license cleanliness.
- **Breeze TTS 2's Voice Direction is a genuinely different mechanism worth knowing about**
  (natural-language instruction steering, not discrete emotion labels) and its VRAM footprint
  (~7.7 GiB eager) fits den's 12 GB card comfortably — but with only 2 languages and a
  non-commercial weights license, it isn't a Multilingual replacement, and there's no
  crying/screaming-specific demo backing up its "steers emotion" claim, unlike Fish Audio or
  IndexTTS-2.

**Honest bottom line:** nothing checked is unambiguously better-documented for crying/screaming
*and* cleanly licensed *and* multilingual enough to replace Chatterbox Multilingual in den today.
The two models with the strongest first-party emotional-range evidence (IndexTTS-2's "Cry" demo,
Fish Audio's `[screaming]` tag) both carry licenses that are worse fits for a public,
non-commercial personal repo than Chatterbox's plain MIT, and both under-deliver on language
count relative to Fish Audio's breadth vs. IndexTTS's narrowness. If stronger emotional beats
are needed for specific voice-over moments, the lowest-risk experiment would be trying Fish
Audio's `[screaming]`/`[angry]`/`[sad]` tags for isolated English clips under its research
license (den is non-commercial personal use, which the license's research/personal exemption is
likely intended to cover — but read the license itself, not this summary, before deciding), while
leaving Chatterbox Multilingual as the default for everything else. This would be an addition,
not a replacement, and should be treated as an experiment with an unclear licensing footing
rather than a settled plan.

## Unverified / could not confirm against a primary source

- **Exact VRAM figures for Chatterbox (any variant)** — neither the GitHub README nor any
  HuggingFace model card states a number; only relative claims ("less VRAM than our previous
  models") were found.
- **A "Higgs Audio v3" release** — referenced in the GitHub repo's own README content, but no
  independent HuggingFace or announcement page for a v3 could be found in this session; the only
  confirmed-real artifacts are v2 (`bosonai/higgs-audio-v2-generation-3B-base`, Boson Higgs Audio
  2 Community License).
- **IndexTTS-2.5's ~6 GB VRAM figure** — surfaced only via a search-engine snippet aggregating
  third-party pages, not confirmed on the project's own GitHub or HuggingFace pages.
- **Higgs Audio's "100+ languages" and Fish Audio's "over 80 languages"** — both are the
  project's own README claims, but neither README (as retrieved) included an itemized language
  list to check the claim against, so the count is taken on the source's word, not verified item
  by item.
- **Whether den's specific non-commercial personal/public use would satisfy Breeze TTS 2's or
  Fish Audio's non-commercial license terms** — this note quotes the licenses' own language, but
  a definitive legal reading of "does personal, unpaid, public-repo use count as non-commercial"
  under either license was not attempted here and would need a full read of the license text
  (not just the summarized clauses above) or legal advice before relying on it.

## Sources

All accessed 2026-09-25.

- Chatterbox GitHub repo — https://github.com/resemble-ai/chatterbox
- Chatterbox GitHub LICENSE (MIT) — https://github.com/resemble-ai/chatterbox/blob/master/LICENSE
- Chatterbox-Turbo HuggingFace model card — https://huggingface.co/ResembleAI/chatterbox-turbo
- Chatterbox (Multilingual) HuggingFace model card — https://huggingface.co/ResembleAI/chatterbox
- Breeze TTS 2 HuggingFace model card — https://huggingface.co/BreezeBlue/Breeze-TTS-2
- Breeze TTS 2 GitHub repo — https://github.com/breezeblue-ai/breeze-tts
- IndexTTS-2 GitHub repo — https://github.com/index-tts/index-tts
- IndexTTS-2 GitHub LICENSE (bilibili Model Use License Agreement) — https://github.com/index-tts/index-tts/blob/main/LICENSE
- IndexTTS-2 project technical page (emotion demo, "Cry" category) — https://index-tts.github.io/index-tts2.github.io/
- IndexTTS-2.5 project technical page — https://index-tts.github.io/index-tts2-5.github.io/
- IndexTTS-2 HuggingFace model card — https://huggingface.co/IndexTeam/IndexTTS-2
- IndexTTS-2.5 HuggingFace model card — https://huggingface.co/IndexTeam/IndexTTS-2.5
- Fish Speech GitHub repo — https://github.com/fishaudio/fish-speech
- Fish Speech GitHub LICENSE (Fish Audio Research License) — https://github.com/fishaudio/fish-speech/blob/main/LICENSE
- Higgs Audio GitHub repo — https://github.com/boson-ai/higgs-audio
- Higgs Audio GitHub LICENSE (code, Apache-2.0) — https://github.com/boson-ai/higgs-audio/blob/main/LICENSE
- Higgs Audio v2 HuggingFace model card — https://huggingface.co/bosonai/higgs-audio-v2-generation-3B-base
- Higgs Audio v2 model weights license — https://huggingface.co/bosonai/higgs-audio-v2-generation-3B-base/blob/main/LICENSE
- Bark GitHub repo — https://github.com/suno-ai/bark
- Bark GitHub releases (confirms no releases / inactivity) — https://github.com/suno-ai/bark/releases
