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
