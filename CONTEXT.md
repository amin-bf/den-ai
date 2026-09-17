# Local AI toolchain

A coding agent (Claude) that hands selected work to models on this machine, which share
one GPU.

## Language

### GPU sharing

**Broker**:
The one local server every GPU user goes through. It decides what holds the GPU.
_Avoid_: proxy, gateway, daemon

**Side**:
One kind of GPU work, LLM or image. Only one side is loaded at a time.
_Avoid_: backend, engine

**Mode**:
The sides that are available: `llm`, `image`, `both` or `off`.
_Avoid_: profile, state

**Swap**:
Unloading one side and loading the other.
_Avoid_: switch (that word means a mode change)

**Batch cap**:
The limit (time or count) on how many new requests the loaded side may start while the other side waits for a swap.
_Avoid_: quota, timeslice

**Switch back**:
Stopping the image side after a request because no more images follow, so the LLM can load.
_Avoid_: release, free

**In-flight request**:
A request the broker has passed on and that hasn't finished yet. A swap or mode change waits for it.
_Avoid_: busy, job

**Caller**:
Who sent a request: `cli`, `claude` or `pi`.
_Avoid_: client, user

### Delegation

**Task**:
A kind of work Claude may delegate to the LLM (summarize, extract, classify, draft), with its own system prompt.
_Avoid_: job, skill

**Delegation**:
One call where Claude runs a task on the local LLM, logged with an id and Claude's verdict.
_Avoid_: request (that's the HTTP level)

### Image generation

**Workflow**:
A ComfyUI graph saved as a file, with the inputs where a request's prompt, seed and size go. It is what a caller picks instead of a model; one whose model can edit also has an edit variant, used when a request brings an input image.
_Avoid_: profile, pipeline, preset

**Setting**:
An optional per-request value a workflow exposes (steps, cfg, sampler, scheduler), with a default from its graph, a recommended range for callers and an allowed range the broker enforces.
_Avoid_: parameter (too broad), option

**Reference image**:
An image a request brings for the model to draw on (a person, style or object), chained into the conditioning next to the prompt. Unlike an input image, it isn't edited.

**Guide image**:
The image a ControlNet follows for composition, pose or outlines: a photo (edges drawn by den) or a ready-made pose, depth or line map.
_Avoid_: control image, hint
