# Recipes

Each recipe says what goes in, the call or calls, and what to check. Arguments are
`generate_image`'s; paths are absolute (pi also takes relative paths and `"last"` for the last
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
frame, so it can repaint colours the instruction didn't mention. For light, colour and grade,
add `"strength": 0.3` (0–1): the edit is blended back over the input, untouched pixels return
exactly. Don't use strength when the edit moves things: it ghosts.

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

The pose reference plus up to three more references (klein's limit is 4):

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
