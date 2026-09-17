---
status: accepted (broker, CLI and Claude's tool built; the pi extension follows)
---

# Image generation: ComfyUI behind the broker, workflows as files

Image generation is the second **side** of the GPU broker (ADR 0002). Every image model
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
  models never receive the image. Paths in a request must be absolute.
- **Lazy switch-back.** After an image ComfyUI stays up, since images tend to come in series.
  It stops when an LLM request needs the GPU, when a request sets `switch_back` and no other
  image work is left, or after `[image] keep_alive` (30 m) without image requests. `switch_back`
  only stops ComfyUI; the LLM loads on its next request, as usual.
- **The idle timeout checks ComfyUI's own queue** before stopping it, so a session in its web UI
  keeps it up.

## Batching (refines ADR 0002)

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
`sampler`, `scheduler`, `loras` and `references` are optional request fields, mapped per workflow
in `config.toml` like the prompt and seed.

- **Ranges are enforced.** Each numeric setting has a `recommended` range (shown to the model)
  and an `allowed` one (refused outside, never clamped). Advice written for another model family,
  e.g. SDXL's cfg 5 on a distilled klein, would otherwise ruin the image quietly. Samplers and
  schedulers accept any ComfyUI name, with a recommended few per workflow.
- **A negative prompt on a guidance-distilled model changes the graph.** At cfg 1 ComfyUI never
  runs the negative pass, so klein's graphs carry an unwired negative encoder, and a
  `with_negative` mapping wires it in and raises cfg to 2 only when a negative is given. Requests
  without one produce pixel-identical images to before. An explicit cfg wins.
- **Extras are graph insertions, not new workflows,** built only from ComfyUI's built-in nodes
  (no custom nodes): LoRAs as `LoraLoaderModelOnly` nodes after the workflow's model node
  (matched by model family), klein reference images as chained `ReferenceLatent` nodes,
  ControlNet as `ControlNetApplyAdvanced` on the prompt conditioning (SDXL union) or the
  `ZImageFunControlnet` model patch (Z-Image), with the built-in Canny node when the guide is a
  photo, and upscale models before `SaveImage`. IP-Adapter and pose or depth preprocessors exist
  only as custom nodes, so they're left out; klein's references cover what IP-Adapter would do.
- **When to use them is the caller's call**, from feedback on an earlier image. The tool
  description says to start from the defaults, reword before adding a negative, and reuse the seed.

## Consequences

- **A mode switch or swap waits for running images**, which can take a minute with Chroma.
  `den mode … --now` interrupts them in ComfyUI.
- **ComfyUI started by hand is outside the broker.** A swap that finds ComfyUI still answering
  after stopping the unit fails loudly rather than loading the LLM beside it. Its web UI is safe
  to use while the image side is loaded.
- **Pass-through LLM callers (pi) can't see why they wait.** Their request simply takes longer
  while images finish and ComfyUI stops; `den status` and the broker's journal show the queue.
