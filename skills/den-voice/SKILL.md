---
name: den-voice
description: Speak a text or an SRT script in a voice cloned from a recording, with den's local speech model (generate_voice), for a voice-over track or narration on a clip — keeping voices, writing scripts whose lines fit their times, languages and expressiveness. Use when asked for a voice-over, narration, a spoken line or a voice from a recording, or to say something in someone's voice. Putting the voice on a clip is den-clip's; the clip passes the same script as its voiceover.
---

# den-voice: voice-overs with den

den speaks with Chatterbox Multilingual (23 languages) on this machine's GPU, in a voice cloned
from a short recording, and puts each line of a script at its time on one track. The tool
description lists the voices and languages; this skill is how to get speech that fits.

## The tools

- **`generate_voice`**: a text, or an SRT script with each line at its time, spoken in a voice
  and saved as one WAV track. Settings: `exaggeration`, `cfg_weight`, `seed`.
- **`generate_clip`** with `voiceover` (den-clip): the same script and voice, spoken first and
  mixed into a clip.
- **Voices** are recordings kept under a name, listed in the tool description. The user adds them
  (`den voice --add NAME RECORDING`, or the phone's Clip pane); a request may also pass a
  recording's path. Without a voice it's the model's own.

## Recording a voice

What to tell a user who wants their own voice, or a narrator's:

- About **10 seconds** of continuous, natural speech (5–15 s works); not a list of words.
- A **quiet room** without music, TV or echo, the phone close to the mouth. Soft furnishings
  help.
- Spoken **in the tone the voice should have**, calm or lively: the tone is copied along with
  the voice.
- In the **language used most**. Other languages come out in the same voice, with the
  recording's accent.
- Any common audio format; a phone's voice recorder is fine. A second recording in another
  mood is a second voice.

## Writing a script

- **One idea per line**, a line per SRT cue, at the moment it should be heard.
- **About 2.5 words a second.** A line given less time runs past its end and pushes the next one
  later; the result lists each as a note. Leave a short pause between cues.
- **Write for the ear**: short sentences, numbers and abbreviations spelled as said ("twenty
  twenty-six", "doctor").
- **Say the language** (`language`); a text in one language with another's code comes out wrong.

## Settings

- **`exaggeration`** 0.25–2 (default 0.5): higher is more expressive, even theatrical; about 0.3
  for calm narration.
- **`cfg_weight`** 0–1 (default 0.5): lower is slower and calmer, a good pair with higher
  exaggeration; for a fast-talking recording, lower it.
- **`seed`**: the same seed and text give the same take; change one thing at a time.

## The working loop

1. **One short line first** in the chosen voice and language, to hear the voice before a whole
   script.
2. **You can't hear the result.** Say what was made (the track, its length, the notes) and ask
   the user how it sounds: the voice, the pace, the tone.
3. **Fix the script's times from the notes** before anything else; then one setting at a time,
   with the same seed.
4. For a clip, pass the finished script to `generate_clip` as `voiceover` (den-clip).

## Which recipe for which goal

| Goal | Recipe | Calls |
|---|---|---|
| A spoken line or paragraph | **a text** | 1 |
| Narration with timing | **an SRT script** | 1 |
| The user's own voice | **a voice from a recording**, then either of the above | 1 + 1 |
| A clip with a narrator | den-clip, **clip with a voice-over** | 2 |

Step-by-step calls for each: [references/recipes.md](references/recipes.md). Why each holds, with
the tests behind it: [references/lessons.md](references/lessons.md).
