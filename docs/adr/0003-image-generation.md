---
status: accepted (broker, CLI, Claude's tool and the pi extension built)
---

# Image generation: ComfyUI behind the broker, workflows as files

Image generation is the second **side** of the GPU broker (ADR gpu-broker). Every image model
tested fills the 12 GB card, and ComfyUI keeps 9–20 GB of RAM besides, so it can't share the
machine with the LLM. The broker runs ComfyUI as a systemd user service, starts it on a swap to
images and stops it on a swap back. Clients ask the broker (`POST /image`); none of them talks
to ComfyUI.

## Decisions

- **ComfyUI as the backend.** Its web UI is where models get tried and workflows exported, and
  its graph API covers text-to-image, editing and multi-pass workflows with one interface. It
  runs from its own venv (`setup.sh`), outside the stdlib-only den code.
- **A swap stops the service; it doesn't ask ComfyUI to free memory.** `POST /free` only acts once
  ComfyUI's queue is idle, and the process keeps its RAM. Stopping takes about a second and frees
  the GPU at once; starting takes ~4 s plus the first model load (5–40 s).
- **Workflows are files plus a mapping.** `workflows/<name>.json` is a graph in API format, and
  `[image.workflows.<name>]` names the node inputs that receive the prompt, negative prompt,
  seed (sometimes several nodes) and size, with a description that says what the
  workflow is good at and how to write its prompt. Prompt style matters more than the model:
  each model wants its own (short sentences, long captions, tags).
- **Editing is a variant of a workflow, not a workflow of its own.** A workflow whose model can
  edit also has `workflows/<name>-edit.json` and `[image.workflows.<name>.edit]`; a request with
  an input image runs that graph instead. Callers keep picking by style, and an input image
  given to a model that can't edit fails loudly. A single graph can't do both: the editing
  graph encodes the input as a reference latent and takes its size from it. Of the four models
  only FLUX.2 klein edits; img2img by partial denoising would work with any of them but
  doesn't follow instructions, so it isn't offered as editing.
- **No model names in config: availability comes from the files.** A workflow can run when every
  model file its graph names is under `$COMFYUI_DIR/models`. Whoever calls picks the workflow
  per request from the descriptions; `[image] default_workflow` covers requests that name none.
- **The broker writes the files.** It saves each result to
  `~/Pictures/den/YYYY-MM-DD/<time>-<slug>.png` (`DEN_IMAGES` overrides), copies it to `out` when
  given, and appends prompt, seed, workflow, times and caller to
  `~/.local/state/den/images.jsonl`. Results carry paths, never image data, so Claude's and pi's
  models never receive the image. Paths in a request must be absolute. (One exception since:
  a caller may ask for a small re-encoded copy with `preview`, which Claude's MCP tool does so
  its model can judge what it made — [ADR releasing-the-machine](0004-releasing-the-machine.md). pi's model
  still gets text only.)
- **Lazy switch-back.** After an image ComfyUI stays up, since images tend to come in series.
  It stops when an LLM request needs the GPU, when a request sets `switch_back` and no other
  image work is left, or after `[image] keep_alive` (30 m) without image requests. `switch_back`
  only stops ComfyUI; the LLM loads on its next request, as usual.
- **The idle timeout checks ComfyUI's own queue** before stopping it, so a session in its web UI
  keeps it up.

## Batching (refines ADR gpu-broker)

The caps (`[broker] batch_seconds = 120`, `batch_requests = 4`) apply to both sides the same way:
while the other side waits, the loaded side starts new requests only until one cap is reached.
They count from when the other side started waiting **or from when the loaded side was loaded,
whichever is later**. Without the second clause, requests that waited through a long batch would
find their own cap already expired after the swap and hand the GPU straight back, starting
nothing.

## Models (tested on a 12 GB card, warm, ~1024²)

| Workflow | Style | s/image |
|---|---|---|
| `flux2-klein-4b` (fp8) | general, quick drafts; edits | 2 |
| `z-image-turbo` (int8 convrot, fp8 text encoder; bf16 took 8.5 s, same images) | general, legible text | 4.5 |
| `wai-illustrious` (SDXL, hires fix) | anime and illustration, tag prompts | 29 |
| `chroma1-hd` (fp8, cfg 6, 35 steps) | photorealistic, long captions, negative prompt | ~50 |
| `realvisxl` (SDXL fp16, 30 steps, 896x1152) | photorealistic people, natural skin | 8 |
| `juggernaut-xl` (SDXL, 35 steps, 832x1216) | cinematic photorealism | 11 |

## Request settings, LoRAs and references

The caller's model can tune a request instead of only picking a workflow: `steps`, `cfg`,
`sampler`, `scheduler`, `loras`, `references` and, on an edit, `strength` are optional request
fields, mapped per workflow in `config.toml` like the prompt and seed.

- **Ranges are enforced.** Each numeric setting has a `recommended` range (shown to the model)
  and an `allowed` one (refused outside, never clamped). Advice written for another model family,
  e.g. SDXL's cfg 5 on a distilled klein, would otherwise ruin the image quietly. Samplers and
  schedulers accept any ComfyUI name, with a recommended few per workflow.
- **A negative prompt on a guidance-distilled model changes the graph.** At cfg 1 ComfyUI never
  runs the negative pass, so klein's graphs carry an unwired negative encoder, and a
  `with_negative` mapping wires it in and raises cfg to 2 only when a negative is given. Requests
  without one produce pixel-identical images to before. An explicit cfg wins.
- **The raised cfg belongs with the negative, and a graph must not ship it alone.** Two klein9b
  edit graphs were exported at cfg 2 while every sibling shipped 1, so `with_negative` had nothing
  left to raise and every edit ran with the guidance up and the negative branch still zeroed. On
  an edit that branch is `ConditioningZeroOut` chained onto the input image's own reference
  latent, so the sampler extrapolates away from the input image itself: colour posterises, skin
  goes plastic, and it compounds over a chain of edits at twice the time. With a real negative at
  the same cfg 2 the images improve instead. Keep a distilled graph at cfg 1 and let
  `with_negative` raise it.
- **An edit's `strength` blends it back over its input.** A klein edit re-renders the whole frame
  conditioned on the input rather than changing part of it, so nothing is preserved by
  construction and a small instruction repaints colours it was never about — one that only asked
  to warm the light rotated a saturated garment 46 degrees in hue and turned black trousers
  brown. The model has no notion of magnitude, so gentler wording doesn't help. `strength`
  inserts an `ImageBlend` between the decode and the save, over the scaled input the result takes
  its size from, so untouched pixels come back exactly and the rest scales down; at 0.3 the same
  edit held the hue within 8 degrees. It ghosts when an edit moves something, so it is described
  as being for light, colour and grade.
- **Extras are graph insertions, not new workflows,** built only from ComfyUI's built-in nodes
  (no custom nodes): LoRAs as `LoraLoaderModelOnly` nodes after the workflow's model node
  (matched by model family), klein reference images as chained `ReferenceLatent` nodes,
  ControlNet as `ControlNetApplyAdvanced` on the prompt conditioning (SDXL union) or the
  `ZImageFunControlnet` model patch (Z-Image), a blend against the input for an edit's
  `strength`, and upscale models before `SaveImage`. IP-Adapter is still custom-node only and
  left out; klein's references cover what it would do.
- **A guide image may be an ordinary photo, for the types den can preprocess.** Canny has always
  been derived with ComfyUI's built-in `Canny` node; pose now is too, since ComfyUI ships
  `SDPoseKeypointExtractor` and `SDPoseDrawKeypoints` as built-ins — den inserts them between the
  guide image and the ControlNet, and `[image.preprocessors]` names the model file each needs.
  Availability follows the file, as everywhere else: a type is offered as drawn from a photo once
  its model is downloaded, and otherwise still takes a ready-made map. This matters because canny
  imports the outline of whatever the guide is wearing — a source in leggings turned a prompted
  blazer and tailored trousers into skin-tight ones, and only a hand-tuned control window got the
  clothes back, while pose at its default strength kept the pose and left the clothing free.
- **On klein, a pose is a reference, not a guide.** No ControlNet for FLUX.2 klein loads in core
  ComfyUI: its Flux ControlNet class is built for FLUX.1's block layout, the model-patch loader has
  no FLUX.2 branch, and the FLUX.2 control models published so far target FLUX.2 dev's wider
  hidden size or need another runtime. klein doesn't need one: given the skeleton as a reference
  image and a prompt that names it ("apply the pose from image 1 to the person from image 2"), it
  follows the pose. So a reference may be typed, `pose:PATH`, and den draws the skeleton from the
  photo with the same SDPose nodes and passes it in the caller's order, since the prompt refers to
  the images by number. Tested on the 9B realism workflow: a skeleton plus a person, a garment
  and a place as four references put all four in one image in 26 s, where the SDXL-ControlNet
  route took three calls and 41 s and only prompted the place; a pose-reference LoRA trained for
  this changed the result by about 1 % on den's distilled model and was left out. The face is
  the weak part with four references, and a klein identity edit afterwards restores it. The
  reference limit on the klein generation workflows went from 3 to 4, klein's own maximum.
- **The maps den draws can be kept.** A skeleton or canny edges only exist inside the graph, so a
  caller never saw what guided an image, and a canny map that copies the guide's clothing is
  obvious once seen. `save_maps` adds a `PreviewImage` after each map, and the broker saves those
  beside the result as `<image>-<label>.png` and lists them apart from `paths`: the image a caller
  shows or gets a copy of stays the result. Off by default.
- **When to use them is the caller's call**, from feedback on an earlier image. The tool
  description says to start from the defaults, reword before adding a negative, and reuse the seed.
- **Every setting a request ran with comes back in the result,** not only the ones the caller
  passed. A setting that came from the graph is the one nobody chose, so it is the one worth
  seeing; the cfg above hid for exactly that reason.

## Callers: Claude's tool and pi's extension

- **One description for both.** `image.request_spec` builds the workflow list, settings, prompt
  syntax note and parameter schema. The MCP server adds `switch_back`; pi's extension reads the
  same spec from `den image --json` (stdlib Python stays the only place it's written) and adds how
  swaps affect pi. The MCP server rebuilds it for every tool list and pi before every turn, so
  mode switches and model downloads show up.
- **pi's tool ends the turn.** After an image, pi would normally send the result back to the
  chat model, which swaps ComfyUI out again right away and makes the model reread the
  conversation (up to ~45 s at 32k). The tool returns `terminate: true` instead, and its
  description asks the model to write its reply first and call the tool last. The next user
  message reloads the model; errors still go back to the model.
- **`/imagine` doesn't swap to draft when the image model is loaded.** The box opens with the
  last prompt and shows the hint as a note, and the chat model is asked only on Ctrl+R. With the
  LLM loaded, the chat model writes a workflow and prompt as JSON from the conversation and hint.
- **Images show only to the user.** pi draws them in the terminal, never as image content that
  could reach the model. pi turns its own inline images off inside tmux, so under tmux in kitty
  the extension uses kitty's Unicode placeholders: tmux moves them like text, and the graphics
  command (kitty reads the file itself) reaches kitty through tmux's passthrough.

## The pose library

A pose worth using again is kept: `save_pose` (and `den pose save`) draws a photo's skeleton once
and saves it under a name, and a request takes it as `pose:NAME`, as a klein reference or a pose
ControlNet's guide image, without the photo or the drawing.

- **A pose can be confirmed before it is kept.** `POST /pose` with `draw_only` draws the
  skeleton, hands it back as bytes and saves nothing, so a client shows it beside the photo
  first and a second call — a second drawing, since no map is cached — saves it under a name.
  The preprocessor pass is short, and a skeleton with a limb missing is better thrown away than
  curated later.

- **Outside the repo, in den's data folder** (`~/.local/share/den/poses/`, `DEN_POSES`): the
  skeleton, a byte-for-byte copy of the photo, the joints as OpenPose JSON, and a JSON line of
  description and size. The joints come back through ComfyUI's core `PreviewAny` node, which
  JSON-encodes the detector's keypoints, so no custom node is needed. The photo
  is kept so a human curating the library sees what each pose came from, and so a pose can be
  redrawn with a better preprocessor later; it's often a real person's photo, which is why none
  of this is versioned.
- **The library is a separate tool, not a list in the image tool.** Listing the saved poses in
  `generate_image`'s description would keep them in front of the model with no call, but every
  save would change that tool's text, and pi re-registers a changed tool and rereads the
  conversation (15–45 s on the local model). So `list_poses` returns the table of contents (name,
  description, aspect) for a conversation to keep, `save_pose` returns it updated, and the image
  tool carries one fixed sentence that the library exists. Neither pose tool's text names a pose.
  The list says where each pose's files are, and `list_poses` with a name shows one pose's
  skeleton and photo: a model following the skill needs the stand-in photo's path to edit a
  place from it, and the first model that tried had only the name.
- **A model adds, a human curates.** `save_pose` refuses a taken name unless `replace` is set;
  renaming and deleting are only `den pose mv` and `den pose rm`.
- **A saved pose is one figure, and never a blank one.** Without a body detector, which core
  ComfyUI lacks, `SDPoseKeypointExtractor` finds one person; with no one in the photo it draws
  an empty canvas, which den detects from the PNG's bytes and refuses rather than saving a pose
  that would guide nothing.
- **A name is lower-case words joined by hyphens, up to 80 characters**, so `pose:NAME` never
  reads as `pose:PATH`, which still means "draw this photo".
- **A skeleton doesn't show which way the body faces, nor which limb is in front.** A pose saved
  from a figure seen from behind came out facing the camera under a prompt that didn't say
  otherwise. In a side view the two shoulders nearly coincide on the map, and the model grew both
  arms from the near one. Saying in the prompt which arm is in front gave each arm its own
  shoulder in three of four seeds (though it tended to swap which arm), while a negative prompt
  changed nothing and doubled the time (cfg 2). So the image tool asks for the facing and, in a
  side view, the limb depth in the prompt, along with the pose's aspect as the image size.

## Consequences

- **A mode switch, an unload or a swap waits for running images**, which can take a minute with
  Chroma. `den mode off --now` and `den unload --now` interrupt them in ComfyUI.
- **ComfyUI started by hand is outside the broker.** A swap that finds ComfyUI still answering
  after stopping the unit fails loudly rather than loading the LLM beside it. Its web UI is safe
  to use while the image side is loaded.
- **Pass-through LLM callers can't see why they wait.** Their request simply takes longer
  while images finish and ComfyUI stops. pi's extension polls `/status` while pi works and says
  so in its footer; other clients have `den status` and the broker's journal.
