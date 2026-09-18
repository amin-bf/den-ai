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

## Settings

- **On distilled workflows (cfg 1) a negative prompt raises cfg to 2**: about twice the time, and
  cfg 2 without a real negative oversaturates. Reword first; add a negative only for something
  rewording didn't remove, and reuse the seed so the change shows.
- **Stay near a workflow's default size** in total pixels; these models degrade well beyond it.
- **Midjourney flags (`--ar 9:16`) do nothing**; use `size`. Weights like `(word:1.3)` only work on
  workflows whose description says so.

## The machine

- **Each image swaps the chat model out.** The GPU holds either the LLM or the image model: batch
  images, and in pi write your reply first and call the tool last (the turn ends after it).
- **Release when done** (Claude: `release_resources`): the image model holds about 19 GB of RAM
  that builds and tests on the same machine need, until its idle timeout.
