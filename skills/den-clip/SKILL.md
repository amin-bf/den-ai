---
name: den-clip
description: Make short video clips with den's local tools (generate_clip, get_clip) — from text, from an approved still, through keyframes, with sound, with a voice-over, or lip-synced so a person on screen speaks the lines — and judge them on their contact sheet. Use when asked to make, animate or narrate a clip or a short video, to make someone in it talk or say something, or to turn an image into one. The stills come from den-image; scripts and voices from den-voice.
---

# den-clip: short clips with den

den makes silent or sounding clips on this machine's GPU (ComfyUI behind den's broker). The tool
description lists every clip workflow with its options, defaults and ranges; read them there.
This skill is the part it can't carry: how to get a clip that looks and moves as asked, and the
traps that were measured. Clips build on images: the look is made as stills with the den-image
skill first. A voice-over's script and voice are the den-voice skill's.

## The tools

- **`generate_clip`** / **`get_clip`**: a short video clip, silent or with sound. It takes a minute or more
  (`ltxv-13b` about 15 s of work per second of clip, Wan 5B about a minute, larger models
  longer), so `generate_clip` answers with an id and `get_clip` waits for it and shows a
  contact sheet: four frames, first to last, in a 2x2 grid. Extras by workflow: `keyframes`,
  `negative`, `loras`, `size`, `duration`, `sound`, `voiceover`.
- **`generate_image`**: the stills a clip starts from and passes through (den-image).
- **`release_resources`** (Claude): hand the machine back after a batch of clips.

## The look as stills first, then the motion

A clip costs minutes and can't be judged until it's done, so don't find the look in a clip.
Make the first frame as an image (`generate_image`, with den-image's recipes), get it approved,
then pass it as the clip's first keyframe and let the clip's prompt be only about what moves:
the subject's action, the water, the light, the camera. Keyframes come through exactly.

For a clip that has to go somewhere (day into night, a door that opens, a person who turns),
make the later moments as stills too and pass them as keyframes at the moments they belong
(`at`: seconds, `"50%"`, `"end"`): the clip passes through each. Make them by **editing the
first still**, one change at a time, so the place and framing stay the same; separately
generated stills make the clip morph from one scene into another.

For anatomy (extra limbs, a hand that melts), fix the start keyframe and say in the prompt what
each limb does; a negative is the last resort. A clip's negative is added to the workflow's own
list, and unlike an image it doesn't raise cfg for you: on a workflow at cfg 1 it does nothing
unless you also pass a higher `cfg`, which doubles the time.

A workflow whose options list `sound: made with the picture` gives its clips a sound track from
the same prompt, unless you pass `sound: false`: end the prompt with the sounds (the room's
ambience, what the action sounds like, a line of dialogue in quotes for the person on screen to
speak). Without that you get ambience at most; on a silent workflow the words only take
attention from the motion.

A LoRA works only on the model it was trained for, so the clip workflows offer their own, never
the image workflows'. A LoRA that made the still doesn't reach the clip: the look reaches it
through the keyframe. A clip LoRA is for what the video model does itself, such as a kind of
motion or a style of footage.

Judge the result on the contact sheet: whether the motion went where the prompt asked, and
whether it passed through the keyframes. Release the machine after a batch of clips: a clip
model holds 14 GB of RAM or more.

A voice-over is spoken first and mixed into the clip: pass `voiceover` ({srt or text, voice,
language}) to `generate_clip`. With an SRT and no duration the clip lasts to the script's end,
and longer if the voice runs long; the result lists every line that ran past its time. How to
write the script and pick the voice: den-voice.

A voice-over is a narrator: the picture doesn't know it, and no lips move. For a person on screen
who **speaks the lines**, pass `sync: true` in the voiceover, on a workflow whose options say
lip-sync: the voice goes into the model and the picture is made to it. Say in the prompt who
speaks and to whom ("he says to the camera"); keep the rest of the sound to ambience. The lips
read best from a close-up start frame with the face frontal and the mouth large; the clip takes
that frame's shape (a portrait close-up gives a portrait clip).

So there are two ways to make someone on screen talk, both lip-synced:

- **A line in quotes in the prompt**, on a workflow that makes sound: the model makes the voice
  and the lips together, so they match by construction. Quick, one call, but the voice is one
  the model invents from the prompt (describe it: "a raspy old man's voice", "a child's high voice"),
  it picks the timing, and at cfg 1 the words come out slurred: pass `cfg` 3 (see lessons).
- **A voice-over with `sync`**: a chosen or cloned voice, the script's words and timing, the
  picture made to it. For a known voice, a given script, or several lines at set moments.

Don't do both in one clip: with `sync` the clip's sound is the voice-over alone, and a quoted
line in the prompt only confuses who says what.

## The working loop

1. **Pick the workflow from the tool description**: its style, what it takes (keyframes, sound,
   lip-sync) and any note at the end of its description. Then **make the look as stills first**
   (den-image) and get them approved.
2. **Try the motion short**: 2–3 seconds, the workflow's defaults. Note the seed.
3. **Judge the contact sheet**: four frames, first to last. *Claude:* look at it and say what you
   see. *pi:* you don't see it; ask the user. You can't hear a clip's sound: for speech,
   `transcribe_audio` on the clip's mp4 tells you the words; for the rest, ask how it sounds.
4. **Change one thing, reuse the seed**, then make the full length.
5. **Finish.** *Claude:* `release_resources`. *pi:* nothing to do.

The GPU is shared: a clip waits in the broker's queue behind other clients' work (pi's chat, a
phone's image) and they wait behind it. Never release the machine, cancel another client's
request or restart the broker to go first; say the clip is queued. Checks that need the GPU
(`transcribe_audio`) are best run while the image side is still loaded, right after the clip.

## Which recipe for which goal

| Goal | Recipe | Calls |
|---|---|---|
| A short clip from a description | **clip from text** (`generate_clip`, then `get_clip`) | 2 |
| A clip in a look the user approved | **clip from an approved still** (first keyframe) | 1 image + 2 |
| A clip that changes (light, a door, a turn) | **keyframes through it**, each an edit of the first still | 1 + edits + 2 |
| A clip of a person moving (a turn, a smile) | **a person between two stills**: start, an edit for the end | 2 images + 2 |
| A clip with sound | **clip with sound**: a workflow that makes it, the sounds at the end of the prompt | 2 |
| A clip with a narrator | **clip with a voice-over**: an SRT script in a voice from the library | 2 |
| A person who says the lines | **lip-synced clip**: a voice-over with `sync`, the speaker named in the prompt | 2 (+ stills) |
| A character with another voice saying the user's lines, in their timing | **a character who says your lines**: design the voice, record, transcribe, lip-sync | 4 |
| A talking character with nobody recording or listening | **with no human in the loop**: design, lip-sync your lines, check them through Whisper | 5 |
| A recurring character speaking in a voice of their own | **a recurring character speaks**: two drafts, the user picks, their portrait, lip-sync, check | 7–8 |

Step-by-step calls for each: [references/recipes.md](references/recipes.md). Why each holds, with
the tests behind it: [references/lessons.md](references/lessons.md).
