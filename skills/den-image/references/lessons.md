# Lessons, with the tests behind them

Measured on a 12 GB card with den's klein 9B realism workflow unless noted. Each lesson says what
to do, what happens otherwise, and how it was found.

## Poses

- **A skeleton doesn't show which way the body faces.** From behind and from the front give
  nearly the same map. A pose saved from a figure seen from behind came out facing the camera
  under a prompt that didn't say otherwise. Say "seen from behind", "her back to us".
- **Nor which limb is in front.** In a side view the two shoulders nearly coincide on the map, and
  the model grew both arms from the near shoulder. Saying which arm is in front and that each
  hangs from its own shoulder fixed it in three of four seeds (it tended to swap which arm). A
  negative prompt ("extra arms, both arms from one shoulder") changed nothing and doubled the
  time.
- **Nor the curve of the spine, nor a limb hidden behind the body.** The skeleton draws the spine
  as a straight neck-to-hip line, so an arched back barely changes it; an arm behind the torso
  comes out as a stump or is missing. Say both in the prompt ("her lower back arched", "both
  forearms on the counter").
- **Keep a stand-in full-body, even if the final image will be cropped.** A stand-in bent over a
  counter, seen from above and cut at the thighs, gave a skeleton of head and shoulders only; the
  same pose with head to feet in frame gave a complete one. Crop the final image through its
  prompt and size instead.
- **Describe what's physically possible.** Asking for a chest flat on a counter *and* the head
  held up looking back got neither, over several seeds; letting the cheek rest on the forearms,
  turned to the camera, got both at once.
- **Nor a limb pointing toward the camera.** A shin lying on a counter, seen from behind, points
  at the lens: on the map it's a short line going down from the knee, the same as a foot hanging.
  Every image made from it hung the foot, whatever the prompt said. Shoot the stand-in from an
  angle where the limb lies across the frame.
- **A mirrored stand-in gives the other side.** Flipping the photo and saving it again gives a
  correct skeleton for the mirrored pose; the detector reassigns left and right.
- **Canny copies clothing.** A canny guide from a photo in leggings turned prompted tailored
  trousers skin-tight. A pose guide or pose reference carries no clothing.
- **One person per saved pose.** Without a body detector den finds one figure; a photo with no
  one in it is refused.

## References

- **Order is numbering.** The first reference is "image 1". A prompt that names the images by
  number gets each one used for its part.
- **Four is klein's limit** (two when editing, since the input image counts).
- **A pose reference works without a ControlNet or a LoRA.** klein follows a skeleton passed as a
  reference when the prompt names it. A pose LoRA trained for this on klein base changed the
  result by about 1 % on the distilled model and was dropped.
- **A place reference gives the actual place** (its tiles, plants, skyline) when the prompt says
  "on the terrace from image 4"; without a reference you get a plausible place in its style.

## Places

- **The place must be seen from the pose's camera.** A place image with its counter diagonal in
  the foreground cut the body in two along the counter's edge; the same pose with a counter
  straight across the frame came out whole. A whole room seen from three metres away, under a
  skeleton filling the frame, made the figure a giant.
- **Editing the pose's stand-in into the place fixes both at once**: remove the person, dress the
  set. It keeps the camera, the furniture's height and the scale exactly.
- **Or fit the pose to an approved place**: edit the stand-in to zoom out until the figure fills
  the frame as a person would fill that room, and save it as a new pose. A model working from the
  skill found this on its own when the place was approved first.
- **A fine-tuned workflow's own tendencies can override a reference.** An outfit reference was
  ignored in one framing and kept in another, on the same workflow and seed. When a reference is
  ignored, try the framing or a workflow without the fine-tune before rewording.

## Faces

- **References carry a face's features only when it's large and frontal.** A seated figure seen
  from above, face to the camera, came out with the reference's freckles, eyes and brows. In
  profile, small in a wide shot, or laughing, it came out with the right hair and colouring but
  generic features.
- **A follow-up "give her the face from the reference" edit fails on hard angles.** It turned a
  profile toward the camera (the body stayed in profile) and erased a laugh. Telling it to keep
  the angle made it leave the face alone instead. Swapping the reference for an angle-matched one
  changed almost nothing: the edit redraws a canonical portrait face, whatever reference it has.
- **Editing the person's own portrait keeps them.** Profile, three-quarter and laughing edits of a
  frontal portrait all kept the freckles, eyes, brows and lips. In an edit the person is the
  input and only what's named changes.
- **So today:** for a recurring face at hard angles, expect the right look, not the exact
  features. A character LoRA would be the fix; den doesn't have one yet.
- **Expressions go in last, by editing the finished image.** Fear, pain, a scream and crying
  edited into a portrait all kept her features. The same expressions asked for in a combine
  prompt, or passed as an expression portrait for reference, don't keep them.

## Prompt wording

- **Colour words land on the nearest noun.** "Eyes red and wet" coloured the eyes themselves;
  "the eyes still their clear grey-blue, only the eyelids slightly pink" gave crying eyes.
- **Name the action.** "She kneels on the counter with one leg" got the pose in one round after
  two rounds of describing the knee and shin failed; the first wording that named angles made the
  leg stretch out straight instead.
- **An edit adds; it doesn't move a part.** "Move her raised leg onto the counter; keep her
  standing leg and pose" drew a third leg on the counter and kept the old one.
- **But an edit reframes the whole shot well.** "Move the camera back so she fills half the frame"
  shrank a stand-in cleanly, pose intact, with even the raised knee clearer than before: the whole
  frame changes together, so nothing has to be kept twice.
- **"Both hands around a coffee cup" gave her two cups**, one in each hand; "one cup on the
  table, and both of her hands are wrapped around that single cup" with the same seed gave one
  (klein9b-realism, 1344x768). Count the objects a pair of hands holds.

## Settings

- **Lower an edit's `strength` for light, colour and grade, never for replacing something.**
  Strength blends the input back over the edit. A relight or recolour keeps every shape, so a
  blend at 0.3–0.5 only restores what the edit repainted by accident. An outfit change at lower
  strength bled the old clothing through the new one; the same holds for anything the edit
  replaces or moves, which comes out as a double image. Leave strength at 1 there.

- **On distilled workflows (cfg 1) a negative prompt raises cfg to 2**: about twice the time, and
  cfg 2 without a real negative oversaturates. Reword first; add a negative only for something
  rewording didn't remove, and reuse the seed so the change shows.
- **Stay near a workflow's default size** in total pixels; these models degrade well beyond it.
- **Midjourney flags (`--ar 9:16`) do nothing**; use `size`. Weights like `(word:1.3)` only work on
  workflows whose description says so.

## Clips

Measured at 1280x704 with den's Wan 2.2 5B clip workflow (3-second clips) unless noted.

- **A keyframe comes through exactly; make the look there.** A still made with klein 4B at the
  clip's size was the clip's first frame unchanged, and the scene moved on from it sensibly
  (waves breaking and spraying). A clip from text alone gave a coherent scene too, but in a
  look nobody had approved, at three minutes a try.
- **Camera instructions are weak.** "The camera stays still" still gave a slow drift to the
  right, and a lamp "beam sweeping across the sky" didn't appear. Put the motion you need most
  first, and don't count on a static camera.
- **Wan 5B takes about a minute per second of clip** (186 s and 196 s for 3 s at 20 steps); LTX 13B
  about a sixth of that (39 s for 3 s, 69 s for 4 s with three keyframes). A shorter duration
  is the cheap way to try a motion before the full length.
- **Keyframes made by editing one still give a clip that changes, not one that morphs.** Dusk,
  then twilight and night made by klein edits of the dusk still (same rocks, same framing), at
  0, 50 % and the end of a 4-second clip: LTX 13B and Wan Fun Inpaint both passed through all
  three, with the light changing gradually and no visible seam. The waves kept moving
  throughout, though no keyframe showed them move.
- **A head turn works as an edit of the still.** "She turns her head to look straight at the
  camera and gives a small, warm smile. Keep everything else exactly the same: …" kept the
  hands, cup, window, light and framing, and changed only the head and expression. The face
  read slightly younger from the front. As a start and end keyframe, the clip passed through
  both, lowering her eyes on the way.
- **Speech takes longer than a tight script allows.** "Every morning I walk down to the harbour"
  given 1.6 s took 2.4 s, and "The sea is calm and silver" given 1.4 s took 2.1 s: each line ran
  past its time and pushed the next. About 2.5 words a second fits. den lengthened the clip from
  4 s to 5 s to hold the voice, and said so.
- **ltx23-distilled took 130 s for a 4-second clip** at 960x544 with two keyframes and sound,
  its model partly in RAM. Its sound at cfg 1 averaged −39 dB, a quiet but present track; a
  cfg-3.5 LTX-2.3 fine-tune made the same prompt's track louder (−21 dB), in line with the model
  card's "higher cfg, more audio".

## The machine

- **Each image swaps the chat model out.** The GPU holds either the LLM or the image model: batch
  images, and in pi write your reply first and call the tool last (the turn ends after it).
- **Release when done** (Claude: `release_resources`): the image model holds about 19 GB of RAM
  that builds and tests on the same machine need, until its idle timeout.
