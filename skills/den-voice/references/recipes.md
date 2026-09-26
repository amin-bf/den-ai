# Voice recipes

Arguments are `generate_voice`'s; paths are absolute. Voice names come from the tool description.

## A text

```json
{"text": "Every morning I walk down to the harbour and watch the boats come in.",
 "voice": "narrator", "language": "en"}
```

One track, spoken from the start. Good for one line, or a paragraph read straight through.

## An SRT script

Each line at its time, on one track as long as the script:

```
1
00:00:00,300 --> 00:00:02,800
Every morning I walk down to the harbour.

2
00:00:03,000 --> 00:00:05,000
The sea is calm and silver.
```

```json
{"srt": "/path/script.srt", "voice": "narrator", "language": "en", "exaggeration": 0.4}
```

`srt` is the path of the file or the script itself. Read the notes: a line that ran past its
time needs more time in the script (about 2.5 words a second), not a faster setting.

## A voice from a recording

The user records about 10 seconds (see SKILL.md, "Recording a voice") and keeps it:
`den voice --add narrator ~/recording.m4a`, or on the phone in the Clip pane. It's then listed
as `narrator`. For a one-off, pass the recording's path as `voice` instead.

Try it on one short line before a whole script, and ask the user whether it sounds like them.

## Lines in turn, timed by den

```json
{"lines": ["For forty years, I kept this light burning.", "Tonight, someone else will keep it."],
 "voice": "narrator"}
```

Each line is spoken after the one before, a breath apart; the track lasts as long as the speech.
The result names an `.srt` next to the track with the times the lines were spoken: use it as
subtitles, or as the script of a clip's voice-over so the clip keeps those times.

## The user's own narration

The user speaks the lines themselves (on the phone: Clip pane, Record narration). Two ways on:

1. **Their recording is the voice-over** as it is: pass it as `audio` in the clip's voiceover,
   with `sync` for a person on screen to speak it. Nothing is spoken again.
2. **Its words, in another voice**: `transcribe_audio` gives the SRT at the times they spoke;
   pass that as the `srt` of `generate_voice` or a voiceover with another voice.

```json
{"audio": "/path/narration.m4a", "language": "en"}
```

The transcription model writes numbers as digits ("40 years") and times its lines a little more
coarsely than a script den spoke itself; read the SRT before speaking it again.

## A strong emotion

```json
{"text": "Please — don't leave. I can't do this. I CAN'T... not without you.",
 "voice": "elli", "exaggeration": 1.4, "cfg_weight": 0.2, "seed": 7}
```

One short line first, to hear whether the emotion landed before a whole script. Not right: a
sharper setting (SKILL.md, "A strong emotion") or a rewrite with shorter, more broken sentences —
never letter-stretching a word for a drawn-out sound, which reads as a stutter instead.

## A named emotion, on a further workflow

Only where `generate_voice`'s tool lists `workflow`. `exaggeration` and `cfg_weight` are the
default workflow's own settings and do nothing here; check the workflow's languages before
asking for one it doesn't speak (far fewer than the default workflow's 23, and it fails loudly
rather than falling back). One short line first, same as any other emotion attempt.

Set only the one or two dimensions a line actually calls for — leaving the rest at 0 reads more
natural than spreading weight across dimensions that don't fit:

**Crying, pleading:**
```json
{"text": "Please — don't leave. I can't do this. I CAN'T... not without you.",
 "voice": "elli", "workflow": "emotion", "emotion": {"sad": 0.8, "afraid": 0.3}, "emo_alpha": 1.0}
```

**Scared, breathless:**
```json
{"text": "They're coming. They're coming, I can hear them— I have to move, I have to move now.",
 "voice": "elli", "workflow": "emotion", "emotion": {"afraid": 1.0}, "emo_alpha": 1.0}
```

**Flirty:**
```json
{"text": "Mmm, well hello there... I was hoping you'd come find me.",
 "voice": "elli", "workflow": "emotion", "emotion": {"happy": 0.6, "surprised": 0.3}, "emo_alpha": 1.0}
```

**Playful:**
```json
{"text": "Ooh, catch me if you can, I bet you can't!",
 "voice": "elli", "workflow": "emotion", "emotion": {"happy": 0.7, "surprised": 0.4}, "emo_alpha": 1.0}
```

**Epic, rallying:**
```json
{"text": "Rise now, for this is the hour we were born for, and nothing will stop us!",
 "voice": "elli", "workflow": "emotion", "emotion": {"happy": 0.3, "angry": 0.6, "surprised": 0.3}, "emo_alpha": 1.0}
```

**Brave, resolute:**
```json
{"text": "I'm not afraid. Whatever happens, I'm walking through that door.",
 "voice": "elli", "workflow": "emotion", "emotion": {"angry": 0.3, "afraid": 0.2, "calm": 0.4}, "emo_alpha": 1.0}
```

**Disgusted:**
```json
{"text": "Ugh, get that away from me, I can't even look at it.",
 "voice": "elli", "workflow": "emotion", "emotion": {"angry": 0.3, "disgusted": 0.9}, "emo_alpha": 1.0}
```

**Melancholic, wistful:**
```json
{"text": "Some days I still expect you to walk through that door.",
 "voice": "elli", "workflow": "emotion", "emotion": {"sad": 0.5, "melancholic": 0.9}, "emo_alpha": 1.0}
```

**Surprised, shocked:**
```json
{"text": "Wait, seriously? I did not see that coming at all!",
 "voice": "elli", "workflow": "emotion", "emotion": {"happy": 0.2, "afraid": 0.1, "surprised": 0.9}, "emo_alpha": 1.0}
```

**Calm, confident:**
```json
{"text": "Take your time. We've got this, there's no rush at all.",
 "voice": "elli", "workflow": "emotion", "emotion": {"calm": 0.9}, "emo_alpha": 1.0}
```

## A line that must fit a time limit

Only on a further workflow (the tool lists `max_seconds` alongside `workflow`), and only for a
single `text` — an SRT script or several `lines` already have their own times, and a global cap
on each of several lines is ambiguous, so it's refused there:

```json
{"text": "Please, don't leave. I can't do this without you.",
 "voice": "elli", "workflow": "emotion", "max_seconds": 3.5}
```

If the first take runs long, den retries once faster (`duration_factor`) before giving up; the
result still says so if it's still over after that — never cut to force a fit. For a real
lip-synced clip, pass this in the clip's `voiceover` instead so the clip is built to hold it,
rather than fighting the speech for a length the clip already decides.

## A designed voice

For a character's voice nobody has recorded:

```json
{"description": "An old man in his eighties with a deep, warm, slightly raspy voice, speaking slowly and calmly, like a storyteller by the fire."}
```

That makes a draft (`draft 20260924-…`). Try it: `generate_voice` with `"voice": "draft:20260924-…"`
on a real line. The user likes it: `save_voice` with `{"draft": "20260924-…", "name": "grandpa"}`.

- Describe **age, gender, pitch, texture, pace and mood**; the more concrete, the more it holds.
- The sample is spoken in `language` (10 to choose from); the speech model then speaks the voice in any
  of its 23, with the sample's accent.
- Not right: the same description with another `seed`, or a sharper description, as a new draft.
  Only a user who asked for it at once gets `name` in `design_voice`, which skips the draft.
- The description, language and seed are kept with the voice: `list_voices` shows them, and the
  same description and seed make the same voice again if it's ever lost.
- From then on it's a voice like any recorded one: `voice: "grandpa"` in `generate_voice` or a
  clip's voiceover, with `sync` for a lip-synced character.
