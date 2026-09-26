---
status: accepted (built and run on the GPU: Wan 2.2 5B, and LTX-Video 13B for keyframes; no sound)
---

# Clip generation: video workflows on the image side, as detached requests

den makes images; it should make short **clips** too. ComfyUI already runs behind the broker
as the image side (ADR image-generation) and ships the video nodes (Wan, LTX), so a clip is a workflow
like any other: a graph file, a mapping in `config.toml`, availability from its model files.
What differs is how long one takes and what it hands back. An image takes 2–50 s and streams
its progress to a caller that stays connected; a clip takes minutes, longer than an MCP tool
call, a remote client's SSH channel or a phone's screen should be trusted to stay open.

## Decisions

- **Clips run on the image side.** No third side: ComfyUI makes both, and a swap, a batch cap,
  the idle timeout and a release treat a clip request like an image request. `/status` keeps
  the side name `image`, which pi's footer and the Android client read.
- **A clip is a detached request.** `POST /clip` checks the request (workflow, keyframes,
  settings) as `/image` does, so a bad one fails at once, and then answers with an id instead of
  streaming. The broker runs it on its own thread: it waits for the image side, counts against
  the batch cap, is in flight while it runs and is cancelled by a release with `now`, exactly as
  a streamed request would be. The caller asks for it by id (progress while it waits or runs,
  the result when done) and may cancel it.
- **Only clips are detached.** Images stay streamed: they are short, and pi, the Android client
  and Claude's tool are built around the stream. Detaching them too would change all three for
  nothing.
- **Detached requests live in the broker's memory only.** A broker restart stops ComfyUI and
  loses a running clip; its id then answers "not known here — the broker may have restarted".
  A finished clip is already a file and a log entry, so no queue file is kept. A result is
  held for a day after it finishes, long enough for a client elsewhere to fetch it as bytes
  (ADR remote-brokers), and then dropped; the file stays.
- **A running clip is let finish.** The batch cap only limits starting requests, so an LLM
  request that arrives during a clip waits for it, minutes if need be; cutting a clip off
  wastes more than the wait. The same holds for a release without `now`. Both say in their
  waiting line that a clip is running and about how long it has left, so the wait is never
  silent.
- **Keyframes, not a start frame.** A clip may be given images it must show at given moments
  (`keyframes: [{image, at}]`, `at` in seconds, as a percentage of the clip, `"50%"`, or
  `"end"`); the first defaults to 0, where the clip starts. Each workflow states how many
  keyframes it takes and where, and a request beyond that is refused, as references beyond a
  workflow's `max` are. A clip without a `size` takes its start keyframe's shape at the
  workflow's own number of pixels (den reads the size from the file's header): the models crop
  a keyframe to the clip's shape, so a portrait photo in a 1280x704 clip lost its top and bottom. This is what makes a
  clip plannable: the keyframes are made and approved as stills first (klein with references
  keeps one person consistent), and only then does anything spend minutes on motion.
- **What a clip workflow adds to the mapping** (`[clip.workflows.<name>]`, apart from the image
  workflows so their tool stays as it is): `duration` in seconds, with recommended and allowed
  ranges, which den turns into the model's frame count (Wan wants a multiple of 4, plus one);
  `fps`, read from the graph rather than set, since a model moves at the rate it was trained
  at; the keyframe inputs; and the node whose frames the contact sheet takes. A request's
  `duration` is the clip's length, and a result's `seconds` is how long it took to make, as for
  images. `SaveVideo` reports its file under ComfyUI's `images` output with `animated` set, so
  the result is collected as an image is; den saves it to
  `~/Videos/den/YYYY-MM-DD/<time>-<slug>.mp4` (`DEN_CLIPS` overrides), the sheet beside it, and
  logs it to `~/.local/state/den/clips.jsonl`.
- **LoRAs as for images.** A clip workflow names its `family` and `model` node like an image
  workflow, and `[image.loras]` entries of that family are offered to it, chained after that
  node. The base video models were trained on filtered data, and a LoRA is the cheap way to
  add what they lack; which one, and what it's for, is a machine's own choice and belongs in
  `config.local.toml`.
- **"Time left" is an estimate.** den is stdlib-only and has no websocket client for ComfyUI's
  step progress, so the time a clip has left comes from earlier clips of the same workflow and
  size in the log (seconds per frame and step); the first of a kind has none.
- **A client checks before its first clip.** The broker passes any path it doesn't know to the
  LLM, so an older broker would load a model for `POST /clip`. `/status` always carries
  `clips`, and a client refuses to go on without it.
- **Claude sees a contact sheet, not the clip.** Its model can't watch a video, so every clip
  also gets four frames, first to last, picked and tiled 2x2 in the graph (`ImageFromBatch`,
  `ImageStitch`, `ImageScale`, built-in nodes), saved beside it as `<clip>-sheet.png`; a request
  with `preview` gets it back as one small JPEG, the way an image's small copy is (ADR releasing-the-machine).
  den itself never decodes video: it stays stdlib-only.
- **Separate tools, shared code.** `generate_clip` and `get_clip` (asks for a clip by id, waits
  up to a minute by default, and cancels) in the MCP server, `den clip` in the CLI (watches by
  default, and Ctrl+C stops watching, not the clip; `--detach`, `--id`, `--cancel`, `--list`).
  Their options differ enough from `generate_image` that one tool would describe both badly;
  `den/clip.py` reuses `den/image.py`'s graph helpers, settings, uploads and saving.

## Models

- **First: Wan 2.2 TI2V-5B.** Small enough for a 12 GB card, text-to-video and image-to-video in
  one model, 720p at 24 fps. It takes one keyframe, at 0. It exists to prove the detached
  pipeline end to end, and did. Measured on a 12 GB card with 30 GB of RAM, fp16 model, fp8
  text encoder, 1280x704, 20 steps:

  | Clip | Time to make | GPU memory | ComfyUI RAM |
  |---|---|---|---|
  | 3 s from text (73 frames) | 186 s, first load included | 9.7 GB | 13.3 GB |
  | 3 s from a keyframe | 196 s | 9.6 GB | 13.3 GB |

  So about a minute per second of clip. The keyframe was kept exactly as the first frame and
  the scene moved sensibly from it; the camera still drifted although the prompt asked it to
  stay still. **RAM is the limit, not the GPU:** with other work running, free RAM fell to
  0.9 GB during the second clip. The busy gate (`[limits]`) only checks before a side loads,
  so a long clip isn't protected from what starts after it; releasing the image side right
  after a batch of clips gives the 13 GB back.

- **fp8 doesn't save RAM here.** Loading the same 5B model as fp8 (`weight_dtype`) gave the
  same clip for the same seed in the same time, and ComfyUI still held 14 GB: it reads the
  fp16 file and keeps it. fp16 stays.
- **Several keyframes: LTX-Video 0.9.8 13B distilled (fp8), now the default.** The planned pair
  didn't fit: LTX-2.3 22B is 39 GB of weights with its text encoder, and Wan 2.2 14B two 14 GB
  experts plus a 7 GB text encoder, against 30 GB of RAM that other work shares. Two lighter
  models ran the same test instead, three keyframes (dusk, twilight, night of one scene, made
  as stills by editing one image) over 4 seconds at 1280x704:

  | Workflow | How | Time | GPU | ComfyUI RAM, lowest free RAM |
  |---|---|---|---|---|
  | LTX-Video 13B distilled fp8, 8 steps | `LTXVAddGuide` per keyframe, one pass | 69 s | 10.6 GB | 14 GB, 1.6 GB |
  | Wan 2.2 Fun Inpaint 5B, 20 steps | two segments (start→middle, middle→end), joined in the graph | 234 s | 11.9 GB | 18.6 GB, 0.7 GB |

  Both passed through every keyframe, and neither join nor guide showed on the contact sheet.
  LTX was sharper at full size (rock texture, colour) and changed gradually, and a text-only
  clip took 39 s against Wan 5B's 186 s. Its keyframes may sit anywhere (`at = "any"`: den
  writes the frame index into the guide node and takes an unused guide out of the graph);
  Wan 5B stays for clips from text or one start frame. The segments support (`segments` in a
  duration, a slot feeding the end of one segment and the start of the next) is kept in the
  mapping for a model that only takes first and last frames.

## Sound

Not made. Wan and LTX-Video 0.9.8 make video only, so the clips are silent. In ComfyUI's core
only LTX-2 makes sound with the picture, and that is the model too large for this machine;
adding it afterwards takes a video-to-audio model, which core ComfyUI doesn't have and den
takes no custom nodes for. Revisit with a machine that fits LTX-2, or a core audio node.
