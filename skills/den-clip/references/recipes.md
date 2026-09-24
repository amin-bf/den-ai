# Clip recipes

Arguments are `generate_clip`'s; paths are absolute (pi also takes relative paths and `"last"`
for the last image of the session). Workflow names are examples: pick from the tool description.
The stills these recipes start from are made with the den-image skill.

## Clip from text

`generate_clip`, then `get_clip` with the id it returns. Silent unless the workflow makes sound
(next recipe but one).

```json
{"prompt": "A red fox trots through fresh snow in a birch forest at dawn, its breath visible in the cold air. Soft golden light between the trunks, snow falling lightly. The camera tracks slowly alongside the fox at its height.",
 "duration": 3}
```

One paragraph: the subject and what it does, the setting and light, the camera. The default
(`ltxv-13b`) wants it long and detailed. Judge on the contact sheet `get_clip` returns: four frames,
first to last. Try a new motion at 2–3 seconds before the full length.

## Clip from an approved still

Make the look with `generate_image` first and get it approved; then:

```json
{"prompt": "Waves roll in and break against the dark rocks below the lighthouse, white spray rising and falling back. Clouds drift to the right.",
 "duration": 3,
 "keyframes": [{"image": "/path/approved-still.png"}]}
```

The still is the first frame, exactly. The prompt describes only what moves; restating the
look adds nothing. Without a `size`, the clip takes the still's shape (a portrait still gives a
portrait clip); a `size` of another shape crops the still to fit.

## Clip that goes somewhere: keyframes through it

For a change the clip must show (day into night, a door opening, a person turning), make each
moment a still, **by editing the first one**, then pass all of them:

1. The start: `generate_image`, approved.
2. Each later moment: an edit of the one before, one change at a time, with the same seed:
   ```json
   {"workflow": "flux2-klein-4b", "image": "/path/dusk.png", "seed": 168216973,
    "prompt": "Make it deep blue twilight: the orange glow gone from the sky, a few first stars, the lamp shining brighter. Keep the lighthouse, the rocks, the waves and the framing exactly as they are."}
   ```
3. The clip, with each still at its moment:
   ```json
   {"prompt": "Night falls over a lighthouse on a rocky headland. Waves keep rolling in and breaking against the rocks. The sky darkens from dusk to deep blue to black, stars appear, and the lamp grows brighter until its beam shines out across the sea.",
    "duration": 4,
    "keyframes": [{"image": "/path/dusk.png"},
                  {"image": "/path/twilight.png", "at": "50%"},
                  {"image": "/path/night.png", "at": "end"}]}
   ```

`at` is seconds, a percentage or `"end"`; the tool description says how many keyframes each
workflow takes (`ltxv-13b` and `ltx23-distilled`: a start and two more anywhere; Wan 5B: the
start only). Stills generated
separately instead of edited differ in every detail, and the clip morphs between them. Check on
the contact sheet that it passed through each keyframe.

## A person who moves between two stills

The same recipe for a person: a turn of the head, a smile, looking up. Tested on a woman at a
café window turning from the window to the camera.

1. The start, on a workflow that edits (`klein9b-realism`). Spell out the hands and what they
   hold: "both hands wrapped around one cup" (lessons.md: it once gave her two cups).
2. The end: an edit of the start with the same seed, naming the movement and everything that
   stays:
   ```json
   {"workflow": "klein9b-realism", "image": "/path/start.png", "seed": 1711959363,
    "prompt": "She turns her head to look straight at the camera and gives a small, warm smile. Keep everything else exactly the same: her hair, sweater, both hands around the one cup on the table, the window, the café, the framing and the warm late-afternoon light."}
   ```
3. The clip, start at 0 and end at `"end"`, the prompt telling the movement in order and what
   stays still:
   ```json
   {"workflow": "ltx23-distilled", "duration": 4,
    "prompt": "A woman sits at a small wooden table by a café window in warm late-afternoon light, both hands wrapped around one white coffee cup. She gazes out of the window for a moment, blinks, then slowly turns her head toward the camera, and a small warm smile spreads across her face as her eyes meet the lens. Her hands stay on the cup. The camera is static, a medium shot at eye level.",
    "keyframes": [{"image": "/path/start.png"}, {"image": "/path/end.png", "at": "end"}]}
   ```

Something the prompt adds between the keyframes (a car passing outside) comes through too.

## Clip with sound

On a workflow whose options list `sound: made with the picture`, the clip gets a sound track
from the same prompt. End the prompt with what it should sound like:

```json
{"workflow": "ltx23-distilled", "duration": 4,
 "prompt": "… The camera is static, a medium shot at eye level. Sound: the quiet murmur of a café, cups clinking on saucers, the hiss of an espresso machine in the background, and the muffled hum of a car passing outside."}
```

- Ambience and the sounds of the action come from naming them. A line of dialogue goes in
  quotes, for the person on screen to say.
- `"sound": false` leaves the track out and skips decoding it; a silent workflow refuses
  `"sound": true` and names the ones that make sound.
- You can't hear the result. The summary says "with sound" only when the saved file has a sound
  track; whether it sounds right is for the user to judge, so ask.
- A higher `cfg` gives more motion and more sound, and costs about twice the time per step.

## Clip with a voice-over

A narrator's voice over the clip, from a script (how to write one, and the voices: den-voice).
Write it as SRT, each line at the moment it should be heard, about 2.5 words a second:

```
1
00:00:00,300 --> 00:00:02,800
Every morning I walk down to the harbour.

2
00:00:03,000 --> 00:00:05,000
The sea is calm and silver.
```

```json
{"workflow": "ltx23-distilled",
 "prompt": "A man in a dark wool coat walks slowly along an old stone harbour wall at sunrise … Sound: gentle waves lapping against the stone, distant gulls.",
 "voiceover": {"srt": "/path/script.srt", "voice": "narrator", "language": "en"}}
```

- Leave `duration` out: the clip lasts to the script's end, and longer if the voice runs long.
- On a workflow with sound, keep the prompt's sounds to ambience: the voice goes over them,
  and a line of dialogue in the prompt would talk over the narrator. On a silent workflow the
  voice is the whole sound track.
- The voice names are in the tool's description (`den voice --add` keeps new ones). Without a
  voice it's the model's own.
- The result lists every line that ran past its time as a note; if the notes say so, spread the
  script's times and make it again with the same seed.

## Lip-synced clip: a person who speaks

The keeper says his lines himself instead of a narrator over him. Start from a still where the
speaker's face is visible and large enough to see the mouth, then:

```json
{"workflow": "ltx23-distilled",
 "prompt": "An old lighthouse keeper stands in the lamp room at night, the great lamp blazing beside him. He turns to the camera and says his lines quietly, his beard moving as he speaks. The camera is static, a medium close-up. Sound: wind against the glass.",
 "keyframes": [{"image": "/path/keeper.png"}],
 "voiceover": {"lines": ["For forty years, I kept this light burning.", "Tonight, someone else will keep it."],
               "voice": "narrator", "sync": true}}
```

- `sync` works only on workflows whose options say lip-sync (the ones that make sound with the
  picture); others refuse with the ones that can.
- The voice is made first and fixed in the model; the clip's sound is the voice itself, so
  ambience from the prompt doesn't come through.
- `lines` let den time the script from the speech; the result carries the SRT at those times.
