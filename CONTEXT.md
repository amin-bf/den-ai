# Local AI toolchain

A coding agent (Claude) that hands selected work to models on this machine, which share
one GPU.

## Language

### GPU sharing

**Broker**:
The one local server every GPU user goes through. It decides what holds the GPU.
_Avoid_: proxy, gateway, daemon

**Side**:
One kind of GPU work: the LLM, or image (ComfyUI, which makes images and clips; speech belongs
here too, there is no third side). Only one side is loaded at a time.
_Avoid_: backend (see Workflow: a side runs one), engine

**Mode**:
The kill switch, `on` or `off`: whether any caller may use the GPU at all. It never picks a side — a request runs when its own side can.
_Avoid_: profile, state

**Swap**:
Unloading one side and loading the other.
_Avoid_: switch (that word means a mode change)

**Model store**:
Where the LLM models are kept and downloaded: Ollama. It no longer runs them; the broker runs the selected one on llama-server.
_Avoid_: backend (for Ollama)

**Saved cache**:
The conversation's computed state, written to a file when the LLM side stops and read back when it starts, so the next turn reads only what's new. One per model, the latest only.
_Avoid_: slot (that's llama-server's word for where a conversation runs), checkpoint

**Batch cap**:
The limit (time or count) on how many new requests the loaded side may start while the other side waits for a swap.
_Avoid_: quota, timeslice

**Switch back**:
Stopping the image side after a request because no more images follow, so the LLM can load.
_Avoid_: release, free

**Available**:
Whether a side can run a request now: an LLM model is selected, or a workflow's model files are there. The broker checks it per request and answers an unavailable one with the reason.
_Avoid_: enabled, on

**Release**:
Giving the machine back because someone asked for it: the sides take no new requests, finish what is running and unload — the GPU, and the RAM and CPU their models hold. The mode stays on, so the next request loads its side again.
_Avoid_: free, eviction, standby (that's waiting for the wake word)

**Unload**:
Emptying memory of a loaded side's model. It is the last step of a swap, of a release and of turning den off.
_Avoid_: kill, purge, drop

**Pressure**:
What the machine is doing besides den: how loaded its CPUs are and how much RAM is still available. Enough of it stops a side that isn't loaded from loading, while a loaded side keeps serving.
_Avoid_: load (that is only one of its two numbers), utilisation

**In-flight request**:
A request the broker has passed on and that hasn't finished yet. A swap or mode change waits for it.
_Avoid_: busy, job

**Detached request**:
A request the broker keeps running after answering its caller with an id, which the caller asks about later. It waits, counts and runs like any other request; only clips are detached.
_Avoid_: job, render, task (that's delegation)

**Caller**:
Who sent a request: `cli`, `claude` or `pi`.
_Avoid_: client, user

**Remote**:
The den on another machine, named in `[remotes.<name>]` and reached through an SSH tunnel. A client picks one per process and uses it as if den existed only there. No path crosses: files and input images go as bytes, and generated images come back as bytes.
_Avoid_: server, host (that's the SSH side), node

### Live conversation

**Live conversation**:
A spoken conversation between a client (such as Claude) and the user through den's microphone and
voice. It has the machine to itself: no side is loaded while it lasts.
_Avoid_: live mode (the mode is the kill switch), call, chat

**Standby**:
The quiet state between live conversations, in which den listens only for the wake word, with a
small detector and no side loaded. Other programs can still record from the microphone.
_Avoid_: standby mode, idle, sleep, release (that gives the machine back)

**Wake word**:
The phrase that ends standby, given by the client (e.g. "hey Ellie"). It counts only at the start
of an utterance; nothing said before it reaches any model but the detector.
_Avoid_: hotword, trigger, agent name (the client's; den only knows the phrase)

### Delegation

**Task**:
A kind of work Claude may delegate to the LLM (summarize, extract, classify, draft), with its own system prompt.
_Avoid_: job, skill

**Delegation**:
One call where Claude runs a task on the local LLM, logged with an id and Claude's verdict.
_Avoid_: request (that's the HTTP level)

### Image generation

**Clip**:
A short generated video: what a video workflow makes, as an image workflow makes an image.
_Avoid_: video (the medium), animation, movie

**Keyframe**:
An image a clip must show at a given moment. The first one is where the clip starts; a workflow says how many it takes.
_Avoid_: start frame, init image, guide frame (a guide image is a ControlNet's), step (a setting)

**Workflow**:
What a caller picks instead of a model, on any side. On the image side, a ComfyUI graph saved as
a file, with the inputs where a request's prompt, seed and size go; one whose model can edit also
has an edit variant, used when a request brings an input image. On the speech side, the process
that speaks: a caller names the workflow its request wants, den runs whichever model that
workflow is. den is a broker, not a model provider, so a side having only one workflow today
doesn't make that workflow the side.
_Avoid_: backend (a side is the only thing den swaps as a whole; a workflow is what a side runs),
profile, pipeline, preset, engine

**Setting**:
An optional per-request value a workflow exposes (steps, cfg, sampler, scheduler), with a default from its graph, a recommended range for callers and an allowed range the broker enforces.
_Avoid_: parameter (too broad), option

**Reference image**:
An image a request brings for the model to draw on (a person, style or object), chained into the conditioning next to the prompt. Unlike an input image, it isn't edited.

**Guide image**:
The image a ControlNet follows for composition, pose or outlines: a photo (den draws its edges or pose) or a ready-made pose, depth or line map.
_Avoid_: control image, hint

**Preprocessor**:
A model den runs on a photo to draw a map from it, such as a person's pose as a skeleton. A map type is offered as drawn from a photo once its preprocessor's file is downloaded; canny needs none.

**Map**:
What a preprocessor (or ComfyUI's Canny node) draws from a photo: a pose skeleton, canny edges. It guides the image and is saved beside it only when a request asks (`save_maps`).
_Avoid_: hint, control image

**Pose reference**:
A reference image passed as `pose:PATH`: den draws the photo's pose as a map and passes the map as that reference, so a klein model takes the pose and framing without the photo's face or clothing. The prompt names it by its number in the order given.
_Avoid_: pose ControlNet (klein has none)

**Saved pose**:
A pose map den keeps under a name, with the photo it was drawn from, so later requests can use it by name as a reference or a guide image without the photo.
_Avoid_: preset, template, pose (alone: that is the pose in a photo)

**Pose library**:
The saved poses den keeps. A model adds to it; a human renames and deletes.
