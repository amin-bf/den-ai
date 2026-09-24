---
name: den-image
description: Make and refine images and short video clips with den's local tools (generate_image, generate_clip, get_clip, save_pose, list_poses) — single images, edits, clips from text or from an approved still, and multi-step work such as a recurring character in chosen poses, outfits and places, built in stages the user approves. Use when asked to create or edit an image or a clip, iterate on one, keep a person consistent across images, or put someone into a pose taken from a photo.
---

# den-image: images with den, one call or many

den runs image models on this machine's GPU (ComfyUI behind den's broker). The tool
descriptions list every option each workflow takes, with defaults and allowed ranges; read
them there, since they change as models are added. This skill is the part a tool description
can't carry: how to reach a goal in one call or several, and the traps that were measured.

## The tools

- **`generate_image`**: a new image from a prompt, or an edit of an input image (`image`), on
  a workflow you pick. Extras by workflow: `references`, `control` (a guide image), `loras`,
  `upscale`, `strength` (edits), `save_maps`.
- **`list_poses`** / **`save_pose`**: the pose library. A pose drawn once from a photo and kept
  under a name, used later as `pose:NAME`. Call `list_poses` once per conversation and keep the
  list; `list_poses` with a name shows one pose: its skeleton, its stand-in photo and their
  paths.
- **`generate_clip`** / **`get_clip`**: a short video clip, silent. It takes a minute or more
  (`ltxv-13b` about 15 s of work per second of clip, Wan 5B about a minute, larger models
  longer), so `generate_clip` answers with an id and `get_clip` waits for it and shows a
  contact sheet: four frames, first to last, in a 2x2 grid. Extras by workflow: `keyframes`,
  `negative`, `loras`, `size`, `duration`, `sound`.
- **`generate_voice`**: speech in a voice cloned from a recording, from a text or an SRT script
  (each line at its time), saved as one track. For a clip, pass the same as `voiceover` to
  `generate_clip` instead: it's spoken first and mixed in.
- **`release_resources`** (Claude): hand the machine back when the image work is done.

## Clips: the look as stills first, then the motion

A clip costs minutes and can't be judged until it's done, so don't find the look in a clip.
Make the first frame as an image (`generate_image`, with every recipe below), get it approved,
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

## A person, an outfit, a pose and a place: build it in stages, with the user

When the request is a specific person (a named character, "her", "this person") in a given
outfit, pose or place, **don't write one long prompt and generate once.** A description in words
gives a different person every time. Build each part as its own image, show it, and wait for the
user's approval before the next:

1. **Face.** A portrait of the person: frontal, neutral, large, plain background. Iterate with
   the user until they say it's right. This image is now the person.
2. **Outfit.** The clothing alone (a flat lay or on a plain mannequin). Iterate until approved.
3. **Pose.** One from `list_poses` if it fits; else a photo the user gives; else **create one**:
   generate a stand-in figure in that pose (any person, fitted clothes, plain background, whole
   body in frame), get it approved, then `save_pose` it. Show the skeleton and confirm it's the
   pose they mean (recipes.md, "Create a pose").
4. **Place.** The location, empty, **seen from the pose's camera**: the same angle, height and
   scale, or the body splits along furniture or comes out giant. Best made by editing the pose's
   stand-in photo (its path: `list_poses` with the pose's name): remove the person, turn the set
   into the place (recipes.md, "A place that fits the pose"). Iterate until approved.
5. **Combine** in one call on a workflow that takes references: pose, face, outfit, place, in
   that order (recipes.md, "Recurring character").
6. **Refine** from the user's feedback: same seed, one change at a time. Add an expression
   last, by editing the finished image (recipes.md, "Change the expression").

Rules for the stages:
- **Stop after every stage and ask** whether it's right. Don't start the next stage in the same
  turn, and don't combine anything the user hasn't approved.
- **Keep the approved image paths** and say them back ("face: /path/…"), so later steps and later
  turns use exactly those files.
- **Skip a stage only when the user already has that part**: an approved image from earlier, a
  saved pose, a photo they pass.
- **Use a workflow that takes references** for the combine step. Workflows without them can only
  describe the person in words.

## The working loop

1. **Pick the workflow by the job**, and write the prompt in the style its description asks for
   (sentences, long captions or tags). Prompt style matters more than the model.
2. **Start with the workflow's defaults.** Note the seed in the result.
3. **Judge the result.**
   - *Claude:* a small copy comes back; look at it and say what you see.
   - *pi:* you never see the image, only its path and settings; the user does. Ask what's off
     rather than guessing.
4. **Change one thing, reuse the seed**: prompt wording, one setting, a LoRA, the workflow. Then
   the difference you see is that change and nothing else.
5. **Finish.** *Claude:* call `release_resources` (or set `switch_back` on the last image), so
   the local LLM and other work get the machine back. *pi:* nothing to do.

Every image call takes the GPU from the chat model on this machine: batch the images you need,
and in pi write your reply first and call the tool last.

## Which recipe for which goal

| Goal | Recipe | Calls |
|---|---|---|
| One image from a description | plain prompt | 1 |
| Change one thing in an image that's already right | **edit**: `image` + an instruction | 1 |
| Light, colour or grade only | edit with `strength` 0.3–0.5 | 1 |
| Replace something: clothing, background, an object | edit, `strength` left out (1) | 1 |
| Carry a person, garment or place into a new image, pose free | **references** | 1 |
| A person in a given pose or framing | **pose reference** (`pose:` photo or saved pose) | 1 |
| Recurring character, parts not made yet | **stages with the user** (above) | 4–5 + 1 |
| Recurring character, parts approved | **four references** | 1 |
| The same person at another angle or expression, as a portrait | **edit the canonical portrait** | 1 each |
| A pose you'll use again | `save_pose` once, then `pose:NAME` | 1 + uses |
| A short clip from a description | **clip from text** (`generate_clip`, then `get_clip`) | 2 |
| A clip in a look the user approved | **clip from an approved still** (first keyframe) | 1 image + 2 |
| A clip that changes (light, a door, a turn) | **keyframes through it**, each an edit of the first still | 1 + edits + 2 |
| A clip of a person moving (a turn, a smile) | **a person between two stills**: start, an edit for the end | 2 images + 2 |
| A clip with sound | **clip with sound**: a workflow that makes it, the sounds at the end of the prompt | 2 |
| A clip with a narrator | **clip with a voice-over**: an SRT script in a voice from the library | 2 |

Step-by-step calls for each: [references/recipes.md](references/recipes.md).

## Rules that aren't obvious

Why each holds, with the tests behind it: [references/lessons.md](references/lessons.md).

1. **References are numbered in the order you pass them.** Name them in the prompt: "the woman
   from image 2, wearing the jacket from image 3, on the terrace from image 4".
2. **A pose skeleton carries pose and framing, nothing else.** No face, no clothing, no depth,
   no spine curve: say which way the body faces ("seen from behind"), which arm is in front in a
   side view, an arched back, and where a hidden arm goes. Make the image the pose's aspect (the
   library lists it).
3. **Use `pose`, not `canny`, for people.** Canny edges copy the guide's clothing outline.
4. **A face comes through references only when it's large and frontal.** In profile, small in
   the frame, or with a strong expression, you get the person's colouring but not their
   features. No prompt wording or better reference fixes that today.
5. **Don't repair a face with a follow-up edit on a hard angle.** "Give her the face from the
   reference" turns a profile toward the camera and erases a laugh. It's safe only on a frontal,
   neutral face.
6. **To get a person at a new angle or expression, edit their own portrait.** As the input of an
   edit the person stays themselves; as a reference they don't.
7. **On distilled models (cfg 1), a negative prompt costs double.** It raises cfg to 2, about
   twice the time. Reword the prompt first. Negatives don't fix anatomy.
8. **Keep what you learn about a workflow in its seed.** When a result is close, reuse the seed
   and change one thing; a new seed is a new image.
9. **Put a colour on the exact part it belongs to.** "Eyes red from crying" painted the eyes
   themselves red; "only the eyelids slightly pink, the eyes still grey-blue" didn't. Restate
   the colours that must not change.
10. **An edit adds; it doesn't move a part.** Asked to move a leg, an edit drew a third one. Change
   limbs by regenerating with a different pose, not by editing. Reframing the whole shot is
   different: "move the camera back" rescaled everything cleanly (recipes.md, "Change a pose's
   scale").
11. **Name the action, not the angles.** "She kneels on the counter with one leg" worked where
   "knee up, shin along the edge" and "shin flat, pointing right" didn't.
12. **Lower `strength` only for light, colour or grade.** It blends the input back over the
   edit, which is what keeps a recolour or relight from repainting the rest. On an edit that
   replaces something (other clothing, a new background, an object swapped), the input bleeds
   through: the old outfit shows under the new one. Leave strength out there (it's 1).
