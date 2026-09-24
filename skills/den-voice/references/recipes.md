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

Whisper writes numbers as digits ("40 years") and times its lines a little more coarsely than a
script den spoke itself; read the SRT before speaking it again.

## A designed voice

For a character's voice nobody has recorded:

```json
{"name": "grandpa", "description": "An old man in his eighties with a deep, warm, slightly raspy voice, speaking slowly and calmly, like a storyteller by the fire."}
```

- Describe **age, gender, pitch, texture, pace and mood**; the more concrete, the more it holds.
- The sample is spoken in `language` (10 to choose from); Chatterbox then speaks the voice in any
  of its 23, with the sample's accent.
- Try it on one short line with `generate_voice` and ask the user. Not right: the same
  description with another `seed`, or a sharper description, with `replace`.
- The description, language and seed are kept with the voice: `list_voices` shows them, and the
  same description and seed make the same voice again if it's ever lost.
- From then on it's a voice like any recorded one: `voice: "grandpa"` in `generate_voice` or a
  clip's voiceover, with `sync` for a lip-synced character.
