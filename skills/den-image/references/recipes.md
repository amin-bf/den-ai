# Recipes

Each recipe says what goes in, the call or calls, and what to check. Arguments are
`generate_image`'s, or `generate_clip`'s in the clip recipes at the end; paths are absolute (pi also takes relative paths and `"last"` for the last
image of the session). Workflow names are examples: pick from the tool description, which says
which workflows take references, edits, control and LoRAs.

## Plain image

```json
{"prompt": "…in the workflow's prompt style…", "workflow": "z-image-turbo"}
```

Then iterate: same `seed`, one change at a time.

## Edit: change one thing

```json
{"workflow": "klein9b-realism", "image": "/path/result.png",
 "prompt": "Replace the grey studio background with a sunlit kitchen. Keep her, her clothes and the framing as they are."}
```

The prompt is an instruction: say what changes and what stays. An edit re-renders the whole
frame, so it can repaint colours the instruction didn't mention.

`strength` (0–1, default 1) is how much of the edit to keep; the rest is the input, blended back
over it. Which value depends on the kind of edit:

- **Light, colour, grade** (warmer light, a darker dress, a film look): `"strength": 0.3`–`0.5`.
  The shapes don't change, so blending loses nothing, and untouched pixels return exactly.
- **Replacing something** (other clothing, a new background, an object swapped) or moving
  anything: leave strength out. Below 1 the input bleeds into the result: the old outfit shows
  through the new one, the old background through the new. The edit's own repainting of
  colours is the smaller problem; fix it in the prompt ("keep her skin tone and hair colour").

## References: carry a person, garment or place into a new image

On workflows that list `references` (klein: up to 4, 2 with an input image).

```json
{"workflow": "klein9b-realism",
 "references": ["/path/person.png", "/path/jacket.png", "/path/place.png"],
 "prompt": "The woman from image 1, wearing the jacket from image 2, standing on the terrace from image 3. Full body, facing the camera, golden hour, photograph."}
```

A reference guides the new image; it isn't edited. The place comes out as the actual place
only when it's a reference and the prompt says so; otherwise you get its style.

## Pose reference: a person in a given pose

The skeleton goes first, so it's image 1:

```json
{"workflow": "klein9b-realism", "size": "832x1216",
 "references": ["pose:/path/pose-photo.png", "/path/person.png"],
 "prompt": "apply pose from image 1 with reference from image 2. The woman from image 2 stands in the pose of the skeleton in image 1: full body, three-quarter view from behind, looking back over her shoulder, one hand on her hip. …outfit, place, light…"}
```

- `pose:/path/photo.png` makes den draw the skeleton from any photo; `pose:NAME` takes a saved
  one. Add `"save_maps": true` to get the drawn skeleton back and check it.
- Match `size` to the pose photo's aspect, or the framing shifts.
- Describe the pose in words as well: which way the body faces, and in a side view which arm and
  leg are nearer the camera. The skeleton can't say either.

## Recurring character: person + outfit + place + pose, one call

Once the face, outfit and place images are approved and the pose is chosen (SKILL.md, "build it in
stages"): the pose reference plus up to three more references (klein's limit is 4).

```json
{"workflow": "klein9b-realism", "size": "832x1216",
 "references": ["pose:standing-look-back-hand-on-hip", "/path/face.png", "/path/outfit.png", "/path/place.png"],
 "prompt": "apply pose from image 1 with reference from image 2. The woman from image 2 takes the pose of the skeleton in image 1: … She wears the jacket and trousers from image 3. She is on the terrace from image 4. …"}
```

About 26 s on a 12 GB card. Pose, outfit and place come through reliably. The face comes
through well when it's large and frontal in the new image, and loosely otherwise (see
lessons.md, "Faces").

## The same person at another angle or expression

Edit the person's own canonical portrait; don't generate a new image from it as a reference.

```json
{"workflow": "klein9b-realism", "image": "/path/face.png",
 "prompt": "Turn her head to her right so her face is in strict side profile. Keep her exact face and features, her eyes, brows, freckles, skin, hair, top and background."}
```

Profiles, three-quarter views, a laugh: each keeps the person, about 9 s each. Useful as
portraits in their own right. (They don't fix the face inside a larger scene: used there as a
reference, they carry as little as the frontal one.)

## Create a pose when there's no photo

When no saved pose fits and the user has no photo, generate a stand-in figure in the pose, then
save its skeleton. Who the figure is doesn't matter; only its skeleton is kept.

1. Generate the stand-in on a fast workflow, in the aspect the final image will have:

   ```json
   {"workflow": "z-image-turbo", "size": "832x1216",
    "prompt": "Full body photograph of a woman in a fitted grey t-shirt and black leggings, crouching low with one knee on the ground, left arm raised to shield her face, looking up to the right. Whole body in frame, plain light grey studio background, even lighting."}
   ```

   Fitted clothes and a plain background let the pose detector find every limb; loose clothing,
   props and clutter hide joints. One person only. Keep the whole body in frame even when the
   final image will be cropped: a figure cut at the thighs loses its hips and legs, and then its
   torso, in the skeleton. For an unusual pose, spell out where the camera is and what touches
   what ("the camera is behind her; her chest lies flat on the countertop"), and try a workflow
   that follows spatial wording closely (klein) if the fast one keeps misreading it.
2. Show it and ask whether that's the pose. Iterate the prompt (same seed) until it is.
3. `save_pose` it under a name that says the pose (next section), and show the skeleton.
4. Use it as `pose:NAME` in the combine step.

Need the same pose facing the other way? Mirror the stand-in photo left to right and save that:
the pose detector finds the joints again, so left and right come out right.

## A place that fits the pose

The place reference must share the pose's camera, or the combined image breaks: a counter seen
diagonally in the foreground cut the body in two at the waist, and a whole room seen from far
away made the figure a giant. The surest way is to edit the pose's own stand-in photo, whose
path `list_poses` gives for the pose's name (`photo: …`):

```json
{"workflow": "klein9b-realism", "image": "/path/pose-stand-in.png",
 "prompt": "Remove the woman completely, leaving the space where she stood empty. Turn the plain studio into a cozy apartment kitchen at night: the grey counter becomes a kitchen counter with a wooden top and white cabinets, in exactly the same place and at the same size. Behind it … In front of it a rug on a wooden floor. Keep the camera and framing exactly as they are."}
```

The edit keeps the camera, the counter's height and the room's scale. Every pose shot the same
way can then share this place.

## Change a pose's scale

The other way round: when the place is already approved and seen from farther back than the pose,
fit the pose to the place. Edit the pose's stand-in photo to zoom out, then save it as a new pose:

```json
{"workflow": "klein9b-realism", "image": "/path/pose-stand-in.png",
 "prompt": "Move the camera back so the person is far away: the same woman in the same pose, but now she occupies only about half of the frame height, with a large amount of empty plain light-grey background above her head and around her. Keep her pose, clothing, lighting and the plain background exactly the same."}
```

The figure then fills the frame the way a person fills the room in the place image, and the
combined image gets the scale right. Check the new skeleton is still complete: a smaller figure
has fewer pixels for the detector.

## Change the expression in a finished scene

Combine the scene with a neutral face first, get it approved, then edit the expression in:

```json
{"workflow": "klein9b-realism", "image": "/path/scene.png",
 "prompt": "Change her expression to fear: eyes wide open, brows raised and pulled together, lips parted. Keep her face and features, her pose, her clothes and everything else as it is."}
```

In an edit the person is the input, so their features stay while the expression changes. Asking
for the expression in the combine prompt loses the features, and an expression portrait passed
as the face reference barely changes anything (references carry the look, not the expression).
In a full-body shot the face is small, so the expression reads only faintly; frame tighter when it
matters.

## Save a pose for later

`save_pose` with:

```json
{"image": "/path/photo.png", "name": "walking-away-from-behind-head-turned-left",
 "description": "full body, seen from behind walking away, head turned left so the face shows in profile"}
```

Check the skeleton in the result: every limb there, one figure. Name it by the pose, and put the
facing and framing in the description, since that's what it will be picked by. Then use
`pose:NAME` in `references`, or as `control.image` with `"type": "pose"` on a workflow with a
pose ControlNet.

## Guide with a ControlNet (non-klein workflows)

```json
{"workflow": "realvisxl", "prompt": "…",
 "control": {"image": "pose:standing-side-profile", "type": "pose", "strength": 0.7}}
```

`pose` and `canny` can be drawn from an ordinary photo; other types need a ready-made map. For
people prefer `pose`. With `start`/`end` (e.g. end 0.5) the guide fixes the composition early
and leaves the detail free.

## Clip from text

`generate_clip`, then `get_clip` with the id it returns. Clips are silent.

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
workflow takes (`ltxv-13b`: a start and two more anywhere; Wan 5B: the start only). Stills generated
separately instead of edited differ in every detail, and the clip morphs between them. Check on
the contact sheet that it passed through each keyframe.
