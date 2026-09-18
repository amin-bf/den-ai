---
name: den-image
description: Make and refine images with den's local image tools (generate_image, save_pose, list_poses) — single images, edits, and multi-step work such as a recurring character in chosen poses, outfits and places, built in stages the user approves. Use when asked to create or edit an image, iterate on one, keep a person consistent across images, or put someone into a pose taken from a photo.
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
  list.
- **`release_resources`** (Claude): hand the machine back when the image work is done.

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
4. **Place.** The location, empty. Iterate until approved.
5. **Combine** in one call on a workflow that takes references: pose, face, outfit, place, in
   that order (recipes.md, "Recurring character").
6. **Refine** from the user's feedback: same seed, one change at a time.

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
| Carry a person, garment or place into a new image, pose free | **references** | 1 |
| A person in a given pose or framing | **pose reference** (`pose:` photo or saved pose) | 1 |
| Recurring character, parts not made yet | **stages with the user** (above) | 4–5 + 1 |
| Recurring character, parts approved | **four references** | 1 |
| The same person at another angle or expression, as a portrait | **edit the canonical portrait** | 1 each |
| A pose you'll use again | `save_pose` once, then `pose:NAME` | 1 + uses |

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
