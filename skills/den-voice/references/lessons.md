# Voice lessons, with the tests behind them

Measured with the speech model through den's broker on a 12 GB card.

- **Speech takes longer than a tight script allows.** "Every morning I walk down to the harbour"
  given 1.6 s took 2.4 s, and "The sea is calm and silver" given 1.4 s took 2.1 s: each line ran
  past its time and pushed the next. About 2.5 words a second fits. On a clip, den lengthened it
  from 4 s to 5 s to hold the voice, and said so.
- **A voice job costs about 20 s at the least.** Two short lines took 21 s and one line in a
  cloned voice 18 s, most of it starting the speech model; each further line adds a second or
  two. Batch a script's lines into one request rather than one request per line.
- **Lines in turn come out a breath apart, and the SRT says where.** "For forty years, I kept
  this light burning." and "Tonight, someone else will keep it." as `lines` landed at
  0.3–2.38 s and 2.78–4.66 s; the SRT beside the track holds exactly those times.
- **The transcription model writes numbers as digits and times a little coarsely.** The narration
  above came back as "For 40 years, I kept this light burning." at 0.0–2.76 s (said at 0.4 s) and
  "Tonight someone else will keep it." at 4.32–5.5 s; 36 s including its first download. Fix the
  words and the times before speaking a transcript again.
- **Cloning works from any clean recording.** A 6.5-second track of the model's own voice kept
  as a voice gave a line in that voice, in one request with no other setup.
- **A designed voice survives cloning.** Three voices designed from descriptions (an old man in
  his eighties, deep and raspy; an old woman, soft and gentle; a cheerful eight-year-old girl),
  then cloned by the speech model on one line: median pitch 130 → 122 Hz, 172 → 172 Hz and 526 →
  444 Hz, still clearly apart. Designing took 59 s the first time (the download), then 15–18 s;
  each sample came out 12–18 s long.
- **The transcription model reads a finished clip's mp4 directly.** The lip-synced keeper clip,
  passed as it was, came back "Tonight, someone else will keep the light." word for word: a way
  to check a clip's speech without anyone listening.
- **Two contrasting drafts make the choice easy.** For a 24-year-old woman described only by her
  looks: "warm, smooth, slightly low … unhurried" came out around 200 Hz and "bright, clear,
  lively … a smile in her tone" around 370 Hz, both kept by the clone (203 and 370 Hz) and both
  word for word through the transcription model. The user picked the low one at once; the high one read young.
- **Moderate settings on a calm voice stay flat, whatever the mood asked for.** Elli (calm,
  unhurried by design) at exaggeration 0.3–1.0 and cfg_weight 0.3–0.6 gave scared, epic, brave,
  crying, flirty and playful takes the user heard as barely different from each other, and no
  crying at all. Pushing to exaggeration 1.2–2.0 and cfg_weight 0.1–0.25, with the text rewritten
  short and broken ("I can't. I can't do this without you. Please, I can't."), read as far more
  upset on the same voice and seed.
- **Letter-stretching a word reads as a stutter, not a drawn-out sound.** "I caaaaaan't...
  without youuuuuuuuu..." came back as "you, you, you, you, you", confirmed against
  [a primary source on the speech model's own emotion-control guidance](https://deapi.ai/blog/chatterbox-tts-guide-how-to-control-emotion-and-22-languages-with-text-alone):
  the model has no inline tag parser and reads emotion only from real punctuation and
  capitalization — `...` for hesitation, `—` for a dramatic pause, `?!` for its strongest
  inflection, and CAPS on at most 1–3 words (a fully capitalized sentence distorts instead of
  emphasizing). `[happy]`-style tags and SSML do nothing either.
- **A voice's own reference sample sets a ceiling on how far settings can push it.** Elli's
  sample is calm and soft; even at the most pushed settings tried, an emotion needing real
  vocal break (sobbing, screaming) stayed short of the raw upset a more dramatic reference
  recording, or a different TTS model built for it, might reach — untested here.
- **A named-emotion workflow changes pacing on identical text, unprompted.** The same line
  ("Please — don't leave. I can't do this. I CAN'T... not without you.") through the emotion
  workflow, same voice and text, came back 6.27 s at sad=0.8, 4.91 s at afraid=0.8, 4.5 s at
  happy=0.8+surprised=0.3, and 4.91 s with no vector at all: the mood alone slowed or sped the
  delivery, something pushing the default workflow's settings never did on the same text. Timbre
  and emotion are separate inputs there (a voice sample plus an 8-number vector), not one
  setting doing both jobs.
- **The emotion workflow costs about 6.5 GB of VRAM and 15–60 s to load**, measured against a
  clean `nvidia-smi` baseline on the same 12 GB card the default workflow shares with ComfyUI:
  well within what's left once ComfyUI frees its models, the same as the default workflow's own
  footprint. First load downloads its checkpoints (about 5.2 GB) and pulls in a couple of small
  auxiliary models on top; later loads are the fast end of that range.
