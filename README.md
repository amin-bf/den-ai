# den

HQ for a local AI toolchain. Claude Code stays the main coding agent and hands selected
cheap, bulk or private tasks to a local LLM served by Ollama, through the `local_llm` MCP
tool. [pi](#pi) is the local chat and coding agent on the same model. Images come from
ComfyUI, through `den image`, Claude's `generate_image` MCP tool or pi's extension.

Everything that uses the GPU (the `den` CLI, Claude's MCP server, pi) goes through the
**broker**, `den serve` on `127.0.0.1:11435`. It passes LLM requests on to Ollama, runs image
requests on ComfyUI, swaps the GPU between the two and owns mode switches, so two models never
fight over the 12 GB card ([ADR 0002](docs/adr/0002-gpu-broker.md),
[ADR 0003](docs/adr/0003-image-generation.md)).

See `AGENTS.md` for the layout and design rules, `CONTEXT.md` for the vocabulary (broker,
side, mode, swap, batch cap, workflow, …) and `docs/adr/` for decisions.

## Status

| Part | State |
|---|---|
| Delegation to the local LLM (`local_llm` MCP tool, tasks, verdict log) | Done, in a trial period ([ADR 0001](docs/adr/0001-delegation-policy.md)) |
| GPU broker, LLM side (pass-through, mode switches that wait, `keep_alive` for every caller) | Done ([ADR 0002](docs/adr/0002-gpu-broker.md)) |
| pi through the broker | Done |
| ComfyUI install (`setup.sh`) and model tests | Done ([Image generation](#image-generation)) |
| Image side of the broker: swaps, batching, idle timeout, `den image` | Done ([ADR 0003](docs/adr/0003-image-generation.md)) |
| Claude's `generate_image` MCP tool | Done ([Image generation](#image-generation)) |
| pi image extension (`generate_image` tool, `/imagine`, inline images, footer status) | Done ([pi](#images-in-pi)) |

## Setup

### Requirements

- Linux with systemd (den runs as a user service) and Python 3.11+ (stdlib only, no pip).
- An NVIDIA GPU with a recent driver. It was built on a 12 GB card; the default model needs
  ~23 GB split across GPU and RAM, so plan for 32 GB of RAM or pick a smaller model.
- [Ollama](https://ollama.com/download), running as a system service.
- Optional: [Claude Code](https://claude.com/claude-code) for delegation, Node.js with npm for
  pi, and `git` plus [uv](https://docs.astral.sh/uv/) for ComfyUI.

### Install

```sh
git clone https://github.com/amin-bf/den-ai.git && cd den-ai
./setup.sh                          # or: ./setup.sh --no-pi --no-comfyui
den model qwen3.6:35b-a3b           # download (~23 GB) and activate the LLM
den status
```

`setup.sh` is safe to re-run. It never uses sudo and never overwrites existing config; what it
can't do, it lists as TODO at the end. It:

- links `den` into `~/.local/bin`, links and starts the broker unit `den.service`, and registers
  the `den` MCP server with Claude Code;
- installs pi if it's missing, links the den extension into `~/.pi/agent/extensions/`, and writes
  `~/.pi/agent/models.json` pointing at the broker (only when that file doesn't exist yet);
- clones ComfyUI into `~/ComfyUI`, creates its venv with PyTorch, and writes `comfyui.service`
  without enabling it. Override with `COMFYUI_DIR`, `COMFYUI_REF`, `COMFYUI_PY` and `TORCH_INDEX`
  (the default is the CUDA 13.0 wheel index; pick the one for your driver from
  [pytorch.org](https://pytorch.org/get-started/locally/)).

Two manual steps:

- **Ollama context length:** run `sudo systemctl edit ollama`, add
  `[Service]` / `Environment="OLLAMA_CONTEXT_LENGTH=32768"`, then `sudo systemctl restart ollama`.
  It must equal `num_ctx` in `config.toml`. Without it the OpenAI-style endpoints pi uses load
  models at 4096 context, and pi and delegations reload the model instead of sharing it.
- **Claude's delegation rule:** add the "when to delegate" rule from
  [ADR 0001](docs/adr/0001-delegation-policy.md) to `~/.claude/CLAUDE.md`. Without it Claude
  sees the tool but has no policy for when to use it.

### After changing code

Reconnect the MCP server in running Claude Code sessions with `/mcp`, and restart the broker
with `systemctl --user restart den`. Run `/reload` in running pi sessions after changing the
extension. Config and state changes need none of these.

Point every client at the broker, not at Ollama (see [pi](#pi)). Clients that go to Ollama
directly (`ollama run`, `ollama launch claude`, `curl localhost:11434`) bypass the broker, and
a mode switch can't wait for them.

## How delegation works

Claude Code sees two MCP tools once an LLM model is selected (`den model`). Both disappear
while den is off (`den mode off`).

| Tool | Purpose |
|---|---|
| `local_llm` | Run an enabled task (`summarize`, `extract`, `classify`, `draft`) on the local model. Files passed via `files` are read locally and never enter Claude's context. The answer ends with `[local: model, tokens in / out, seconds, id N]` |
| `local_llm_feedback` | Claude's verdict on answer N: `ok`, `partly`, `wrong` or `unchecked`, plus a note |

**When Claude delegates** (rule in `~/.claude/CLAUDE.md`):
- **Big inputs:** summarize/extract over ~20 KB or several files, classify over ~30 items, draft from sources over ~10 KB.
- **Private data, always:** `.env` files, credentials, DB dumps with personal data, exported mail or chats, or anything you mark private. Claude never opens it.
- **Otherwise** it does the work itself. If the LLM is off it says so, and for private data it asks.

**How far Claude trusts it** (trial period, until the log says otherwise):

| Task | Trust |
|---|---|
| summarize | Content is reliable; specific facts and conditional rules (only / unless / when) get spot-checked. For rules or procedures, no length cap and each condition as its own bullet, since it drops conditions to fit a limit |
| extract | What it returns is reliable; completeness ("find every X"), counts and totals are not |
| classify | Needs numbered input and the expected count; output checked by script |
| draft | Raw material; Claude always rewrites it |

**Speed** (`qwen3.6:35b-a3b`, warm): reads input at ~720 tokens/s and writes at ~65
tokens/s. Small jobs take 3–10 s, and ~20K tokens of input about 30 s. The first request
after loading takes about twice as long, plus ~15 s to load the model. The model stays
loaded for 30 minutes after its last request from any caller, pi included (`keep_alive` in
`config.toml`, applied by the broker); `den unload` empties the GPU
as soon as running requests finish (`--now` cancels them), without taking the LLM off: the
next delegation loads it again. `den mode off` unloads it too, but it also refuses new
requests and takes the tools away from every Claude session, so it's the kill switch, not the
way to free memory.

## pi

[pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) is a terminal coding agent
that runs on the local model. Start it with plain `pi`. Its config lives in `~/.pi/agent/`,
outside this repo:

- **`models.json`:** the `ollama` provider with `baseUrl` `http://127.0.0.1:11435/v1` (the
  broker, never 11434) and each model with `contextWindow: 32768`, `maxTokens: 8192` and
  `reasoning: true`. Without `contextWindow`, pi assumes 128k.
- **`settings.json`:** `defaultModel` is `qwen3.6:35b-a3b`, the same model Claude delegates to.
  No packages are installed. The Ollama web-search package was removed: it needs an Ollama
  cloud sign-in and sends queries to the cloud.
- **Switching models:** `/model` (or Ctrl+L) opens a picker, and Ctrl+P cycles. Only one model
  fits in memory, so a switch unloads the other. It took 14–46 s in tests, since the new model
  also rereads the whole conversation. When pi and Claude's delegations use different models,
  each one swaps the other out, so keep both on the same model and switch pi only when needed.
- **Off:** pi works unless den is off. In `den mode off` it gets "den is off; turn it on with:
  den mode on".
- **Checking the traffic:** `journalctl --user -u den -f` shows one line per request with the
  caller (`pi`, `claude`, `cli`), model, status and duration.

### Images in pi

`integrations/pi/den.ts` is a pi extension, linked into `~/.pi/agent/extensions/` by `setup.sh`.
It talks only to the broker and the `den` CLI.

- **`generate_image` tool:** the model calls it on its own whenever a workflow can run and den
  isn't off. Its description and parameters come from `den image --json`,
  the same text as Claude's tool, plus how the swap affects pi: the tool unloads the chat model,
  so the model should write its reply first and call the tool last. The turn ends when the images
  are done; the chat model reloads on the next message instead of straight away. Paths may be
  relative to pi's working directory, and `image: "last"` edits the session's last image. pi
  never sets `switch_back`: its next request swaps back anyway.
- **`/imagine [hint]`:** the chat model writes a workflow and prompt from the conversation and the
  hint, and an editable box shows them: Enter generates, Shift+Enter adds a line, Tab picks another
  workflow, Ctrl+R asks the chat model to rewrite, Esc cancels. When the image model is still
  loaded, the box opens with the last prompt and shows the hint as a note, so nothing swaps until
  you press Ctrl+R. `--yes` skips the box, `--workflow NAME` forces a workflow, and
  `--image [PATH]` edits a file, or the session's last image when no path follows. The result
  goes into the conversation as text, so the chat model knows what was drawn.
- **Results** show inline with the path as a `file://` link. The model gets text only: paths,
  workflow, seed and settings. Inside tmux pi turns its own images off, so the extension draws them
  with kitty's Unicode placeholders; that needs kitty and `set -g allow-passthrough on`. Links need
  `set -as terminal-features ',xterm-kitty:hyperlinks'` and a tmux client attached after that line
  was loaded. `PI_IMAGE_PROTOCOL=none` turns the images off.
- **Pose library:** `list_poses` and `save_pose` tools next to `generate_image` (see
  [The pose library](#the-pose-library)). `save_pose` shows the skeleton and, like the image tool,
  ends the turn. `/imagine` completes saved pose names after `--reference pose:` and `--control pose:`.
- **Footer:** while pi works or `/imagine` runs, the footer says when a request waits for the other
  side or the GPU is swapping (`den: pi llm waits for claude image to finish`).

## Image generation

ComfyUI lives in `~/ComfyUI` (installed by `setup.sh`) with a user unit `comfyui.service` on
`127.0.0.1:8188`. It isn't enabled: the broker starts it when an image is requested and stops it
when the LLM needs the GPU.

```sh
den status                          # what can run, and what holds the GPU
den image                           # list workflows (* = default) and whether they can run
den image "A red fox reading a book under a lamp" --seed 7
den image "masterpiece, best quality, fox, forest" -w wai-illustrious -n "bad quality" --size 832x1216
den image "…" -o ~/project/assets/fox.png --switch-back
den image "Make the fox's fur blue, keep the rest" -w flux2-klein-4b --image ~/Pictures/den/…/fox.png
```

- **Results** go to `~/Pictures/den/YYYY-MM-DD/<time>-<slug>.png` (`DEN_IMAGES` overrides), plus
  a copy at `-o` when given. Every request is logged to `~/.local/state/den/images.jsonl` with
  prompt, seed, workflow, times and caller.
- **Swaps:** the first image after LLM work unloads the LLM and starts ComfyUI (a few seconds plus
  the model load). ComfyUI then stays up for the next images. The next LLM request, pi's
  included, stops it first; so does `--switch-back`, or 30 minutes without images
  (`[image] keep_alive`).
- **Batching:** when both sides want the GPU, the loaded one finishes what's running and may start
  new requests for up to 120 s or 4 requests (`[broker] batch_seconds`, `batch_requests`), then
  the other side gets its turn. The CLI prints what it waits for; `den status` shows the queue.
- **Nothing to switch on:** images work whenever a workflow can run, LLM requests whenever a
  model is selected, and the broker swaps between them. `den unload` empties the GPU without
  taking anything away; `den mode off` refuses both sides until `den mode on`.
- **Claude** gets the `generate_image` MCP tool while at least one workflow can run. It picks the workflow from their descriptions, which the
  tool lists, and takes the same options as `den image` (`workflow`, `negative`, `size`, `seed`,
  `image`, `out`, `switch_back`, plus the [settings and extras](#settings-and-extras)). The result is text only: the saved paths, workflow, seed and
  time. Claude doesn't see the image. The tool list refreshes when a mode switch or a model
  download changes the available workflows. A call blocks through queue waits and swaps and
  sends MCP progress messages meanwhile, which keep Claude Code's idle timeout (30 minutes
  without a response or progress) from firing.
- **pi** gets the same tool and a `/imagine` command from its extension (see
  [Images in pi](#images-in-pi)).
- **The web UI** at <http://127.0.0.1:8188> works whenever ComfyUI is up (e.g. after a
  `den image`), and the idle timeout leaves it running while its queue is busy. Don't start
  ComfyUI by hand while the LLM is loaded: the broker can't see it.

### Workflows

| Workflow | Good for | Prompt |
|---|---|---|
| `z-image-turbo` (default) | fast general images, legible text | a few short concrete sentences |
| `flux2-klein-4b` | the fastest drafts and simple assets; **edits** an input image (`--image`) | a plain description, or an edit instruction |
| `chroma1-hd` | slow, highest-quality photorealism (cfg 6, 35 steps) | a long detailed caption, plus a negative prompt |
| `klein9b-realism` | fast photorealistic people and scenes; **edits** | a detailed description, with the pose spelled out |
| `klein9b-anime` | anime and illustration (klein 9B + AniEdit LoRA); **edits**, e.g. photo to anime | a description of scene and style |
| `wai-illustrious` | anime and illustration, with a hires-fix pass | Danbooru tags, positive and negative |
| `realvisxl` | photorealistic people and portraits, natural skin (SDXL) | a photo description with camera terms, plus a negative |
| `juggernaut-xl` | versatile cinematic photorealism: people, landscapes, products (SDXL) | natural language with photographic terms |

Each is `workflows/<name>.json` (a graph in API format) plus `[image.workflows.<name>]` in
`config.toml`, which says where the prompt, negative prompt, seed and size go. To add one, build
it in the web UI, export it with Workflow → Export (API), save it under `workflows/` and add
its mapping. If the model can edit, add the editing graph as `workflows/<name>-edit.json` with
its mapping under `[image.workflows.<name>.edit]` (including the LoadImage input); requests with
an input image use it. Only the klein workflows edit; the others generate from text only.
A workflow can run once the model files its graphs name are in `~/ComfyUI/models`
(`COMFYUI_DIR` overrides the location).

### Settings and extras

Each request may override a workflow's settings, add LoRAs and bring reference images. All are
optional; the defaults come from the graph. `den image` lists what each workflow offers, and
Claude's tool description carries the same list. A value outside the allowed range is refused,
never clamped. The recommended ranges are starting points, not tested limits.

| Workflow | steps (default · recommended · allowed) | cfg | sampler (recommended) | scheduler (recommended) | Extras |
|---|---|---|---|---|---|
| `z-image-turbo` | 8 · 8–10 · 4–20 | 1 · 1–1.5 · 1–4 | res_multistep, euler | simple, beta | control |
| `flux2-klein-4b` | 4 · 4–6 · 2–12 | 1 · 1–2.5 · 1–5 | euler | fixed | references |
| `klein9b-realism` | 4 · 4–6 · 2–12 | 1 (edits 2) · 1–2.5 · 1–5 | euler | fixed | references; LoRA `aniedit` |
| `klein9b-anime` | 4 · 4–6 · 2–12 | 1 (edits 2) · 1–2.5 · 1–5 | euler | fixed | references |
| `chroma1-hd` | 35 · 26–40 · 10–60 | 6 · 4–6 · 1–10 | euler, res_multistep | beta, simple | |
| `wai-illustrious` | 28 · 24–30 · 10–50 | 6 · 5–7 · 1–12 | euler_ancestral, dpmpp_2m | normal, karras | control |
| `realvisxl` | 30 · 25–40 · 10–60 | 5 · 4–6 · 1–12 | dpmpp_2m, dpmpp_sde, dpmpp_2m_sde | karras | control |
| `juggernaut-xl` | 35 · 30–40 · 10–60 | 4.5 · 3–6 · 1–12 | dpmpp_2m, dpmpp_2m_sde | karras | control |

- **Available samplers and schedulers:** any name ComfyUI knows; others are refused: 45 samplers such as `euler`, `euler_ancestral`,
  `dpmpp_2m`, `dpmpp_2m_sde`, `dpmpp_3m_sde`, `res_multistep`, `uni_pc`, `lcm`, and the
  schedulers `simple`, `sgm_uniform`, `karras`, `exponential`, `ddim_uniform`, `beta`, `normal`,
  `linear_quadratic`, `kl_optimal`. "DPM++ 2M Karras" is sampler `dpmpp_2m` with scheduler
  `karras`.
- **Negative prompts on klein:** klein is distilled for cfg 1, where a negative is skipped
  entirely. Giving one wires it in and raises cfg to 2 (about twice the compute; an explicit
  `--cfg` wins). In a test with a hands-heavy prompt it removed the oversaturation cfg 2 alone
  causes, but didn't look clearly better than no negative, so reword the prompt first.
- **LoRAs** are `[image.loras.<name>]` entries: a file in `models/loras`, the model family it
  fits, a description and a strength range. A workflow with that `family` offers it unless its
  graph already loads it.
- **Reference images** (klein) are chained into the conditioning as extra reference latents,
  klein's own way to carry a person, style or object into a new image: up to 4 (klein's own
  limit), or 2 when editing. Klein needs no IP-Adapter for this.
- **A pose reference** is a photo passed as `pose:PATH`: den draws the person's pose as a skeleton
  and passes the skeleton as that reference, and the prompt says which image it is ("apply the pose
  from image 1 to the person from image 2"). The model numbers the references in the order given.
  This is klein's pose control: it carries pose and framing but none of the photo's face or
  clothing, and with a person, a garment and a place as the other references it puts all four in
  one image.
- **Control (ControlNet)** locks composition, pose or outlines to a guide image.
  The SDXL workflows (`wai-illustrious`, `realvisxl`, `juggernaut-xl`) use the SDXL union
  ControlNet (xinsir promax, `models/controlnet`) with the
  types `canny`, `lineart`, `scribble`, `pose`, `depth`, `normal`, `segment` and `tile`.
  `z-image-turbo` uses the Z-Image Fun ControlNet Union 2.1 lite (`models/model_patches`) with
  `canny`, `hed`, `depth`, `pose` and `mlsd`. For `canny` and `pose`, pass a photo and den draws
  the map (ComfyUI's built-in Canny and SDPose nodes; pose needs the preprocessor model below).
  Every other type needs a ready-made map (a depth image, a line drawing). Strength defaults to
  0.7–0.75. Klein and Chroma have no ControlNet among ComfyUI's built-in nodes; for klein, pass
  the pose as a reference instead.
- **Saving the maps:** `save_maps` (`--save-maps`) also saves each map den draws, a pose skeleton
  or canny edges, next to the image as `<image>-ref1-pose.png` or `<image>-control-canny.png`, to
  see what guided it. They're listed in the result, not copied with `out`.
- **Upscalers** run the finished image through an upscale model (`models/upscale_models`), with
  any workflow: `ultrasharp` (4x-UltraSharp, photos and general), `realesrgan` (Real-ESRGAN
  x4plus, photos, smoother) and `realesrgan-anime` (anime and illustration). They're native 4x;
  the factor (default 2, `[image] upscale_factor`) scales the result down after them. They're
  `[image.upscalers.<name>]` entries.
- **Prompt syntax:** ComfyUI just encodes the prompt as text. Midjourney flags (`--ar 9:16`,
  `--v 2`) set nothing; use `--size` and the settings. Weights like `(word:1.3)` are parsed, but
  only the SDXL workflows (`wai-illustrious`, `realvisxl`, `juggernaut-xl`) respond to them
  reliably.
- **Not included, since there are no custom nodes:** IP-Adapter (ComfyUI has no built-in node for
  it; klein's references do that job) and preprocessors for depth, line or segment maps.

### The pose library

den keeps **saved poses**: a pose skeleton drawn once from a photo and kept under a name, so later
images take that pose without the photo. Use one as `pose:NAME`, as a klein reference or as the
guide image of a `pose` ControlNet (`--control pose:NAME --control-type pose`).

- **Saving** draws the photo's pose on the GPU for a few seconds, through the broker:
  `den pose save PHOTO NAME -d "one line on the pose and framing"`, or the `save_pose` tool for
  Claude and pi. Names are lower-case words joined by hyphens (up to 80 characters); a taken name
  needs `--replace`. One person per pose, and a photo where no one is found is refused.
- **Finding them:** `den pose list`, or the `list_poses` tool, which gives a model the table of
  contents (name, description, aspect) to keep for the conversation. The image tool only says the
  library exists, so saving a pose never changes that tool's text (see ADR 0003).
- **Curating** is yours: `den pose mv OLD NEW` and `den pose rm NAME`.
- **Files:** `~/.local/share/den/poses/` (`DEN_POSES` overrides it) holds per pose the skeleton
  (`NAME.png`), a copy of the photo (`NAME.source.<ext>`) and its description (`NAME.json`).
- **Three things to get right in the prompt:** give the image the pose's aspect (the list shows it),
  say which way the body faces, since a skeleton doesn't show front from back, and in a side view
  say which arm is in front: where the shoulders overlap, the model may grow both arms from one
  shoulder. Describing the depth fixed that in three of four seeds in a test, not every time.

### Image models

ComfyUI doesn't come with models. See the
[ComfyUI README](https://github.com/comfyanonymous/ComfyUI#readme) and the
[ComfyUI docs](https://docs.comfy.org/) for which folder each file goes in under
`~/ComfyUI/models/`. The quickest way: open a template in the web UI (Workflow → Browse
Templates); it lists the files it's missing, with download links.

One model isn't a matter of taste and `setup.sh` fetches it for you: the preprocessor that turns
an ordinary photo into a guide map, so a request can steer an image by `pose` without you making
a skeleton somewhere else. `[image.preprocessors]` in `config.toml` says which file each guide
type needs and where it comes from, and the type is offered as drawn from a photo once the file
is there. Skip it with `./setup.sh --no-preprocessors`.

Models tested on a 12 GB card (warm, ~1024² per image):

| Model | Time | Prompt style |
|---|---|---|
| FLUX.2 klein 4B (fp8) | 2 s | Short descriptions; also does editing |
| Z-Image-Turbo (int8) | 4.5 s | Short sentences; renders text well |
| WAI-Illustrious SDXL | 10 s (29 s with hires fix) | Tags, anime and illustration |
| Chroma1-HD (fp8) | 40–50 s | Long, detailed captions; realistic |
| RealVisXL V5.0 (SDXL, fp16) | 8 s (30 steps, 896x1152) | Photo descriptions; portraits, natural skin |
| Juggernaut XI (SDXL) | 11 s (35 steps, 832x1216) | Photographic terms; cinematic realism |

Each one fills the card, so only one model runs at a time.

## Skills

`skills/` holds den's own agent skills, in the [Agent Skills](https://agentskills.io) format that
Claude Code and pi both read. `setup.sh` links each one into `~/.claude/skills/` and
`~/.agents/skills/`, so they're available in every project, like den's tools.

| Skill | What it teaches |
|---|---|
| `den-image` | Using den's image tools well: the working loop (defaults, one change per step, same seed), a recurring character built in stages the user approves (face, outfit, pose, place, then one combined call), which recipe fits which goal (edit, references, pose reference, the pose library, a recurring character in one call), and the traps found in tests (a skeleton shows neither facing nor which limb is in front, faces at hard angles, negative prompts on distilled models) |

A skill carries strategy; the tool descriptions carry the options, which change as models are
added. An agent reads a skill's `SKILL.md` when a task matches, and its `references/` only when
it needs the detail. In pi, `/skill:den-image` loads it by hand.

## `den` cheatsheet

Changes take effect immediately. The broker, the CLI and the MCP server re-read
`config.toml` and `state.json` every time, so nothing needs a restart. Every command except
`task`, `log` and `mode` without an argument needs the broker; when it's down they say
`den broker not running … systemctl --user start den`.

### Status

| Command | What it does |
|---|---|
| `den status` | Mode, broker (up? which side holds the GPU? a switch waiting? requests running or waiting), active model (pulled? loaded? how much is on the GPU), Ollama version, ComfyUI (up? idle for how long?) and runnable workflows, tasks |
| `den serve` | Run the broker in the foreground (the systemd unit does this) |
| `den --help` / `den <cmd> --help` | Help |

### On, off and who gets the GPU

| Command | What it does |
|---|---|
| `den mode` | Show whether den is on or off |
| `den mode off` | The kill switch. Refuses new requests, waits for the running ones (and shows them), unloads the LLM, stops ComfyUI and takes the tools away from Claude. Ctrl+C drops the switch |
| `den mode on` | Serve again (the default). A request runs when its side can: an LLM model is selected, or a workflow can run; otherwise the caller is told which |
| `den mode off --now` | Same as `off`, but cancels the running requests instead of waiting; their callers get an error |
| `den unload` | Empty the GPU without changing the mode: waits for running requests, unloads the LLM and stops ComfyUI. Nothing is refused, and the next request loads its side again |
| `den unload llm` / `den unload image` | Unload one side only |
| `den unload --now` | Same, but cancels the running requests instead of waiting |

### Models

The model list is whatever Ollama has downloaded (cloud and embedding-only models left
out); no model names live in `config.toml`. The active model is stored in `state.json`.
Settings shared by every model (`num_ctx`, `think`, `keep_alive`) are under `[llm]` in
`config.toml`. `think` is off: in a test it was ~3.5× slower and added small errors,
without keeping more conditions in summaries (see the ADR).

| Command | What it does |
|---|---|
| `den model` | List downloaded models (`*` marks the active one) and switch by number; Enter keeps the current one |
| `den model <name>` | Switch to an Ollama model, e.g. `qwen3.6:35b-a3b`. Downloads it first if missing and unloads the old model |
| `den model <name> --no-pull` | Switch, but fail instead of downloading |

### Delegated tasks

Claude can only delegate tasks that are turned on. Add new ones in `config.toml` under
`[tasks.<name>]`.

| Command | What it does |
|---|---|
| `den task` | List tasks with on/off state |
| `den task <name>` | Show whether one task is on |
| `den task <name> on` / `off` | Turn a task on or off (Claude's tool list updates live) |

Current tasks: `summarize`, `extract`, `classify`, `draft`.

### Running a task yourself

`den ask <task> "<instructions>"` takes its input from stdin and/or `--file`
(repeatable). The answer goes to stdout, and the stats (tokens, seconds) go to stderr.
Be exact: the model never asks back. Number list items, and state the expected count and
format. Calls you make with `ask` aren't logged; only Claude's are.

```sh
journalctl -u ollama -n 500 | den ask summarize "What went wrong? 3 bullets"
den ask extract "All URLs as a JSON list" --file notes.md
den ask classify "Label each line: bug, feature or chore" --file todo.txt
git diff --staged | den ask draft "A conventional commit message"
den ask summarize "Compare these" --file a.md --file b.md 2>/dev/null   # hide the stats
```

### Images

| Command | What it does |
|---|---|
| `den image` | List workflows with descriptions; `*` marks the default, and missing model files are shown |
| `den image --json` | The runnable workflows, mode, and the tool description and parameter schema (what pi's extension reads) |
| `den image "<prompt>"` | Generate with the default workflow; prints the saved path, then seed and time on stderr |
| `-w <workflow>` / `-n "<negative>"` | Pick a workflow / give a negative prompt (workflows that take one; on klein it raises cfg to 2) |
| `--seed N` / `--size 832x1216` | Fix the seed (default random) / override the workflow's size |
| `--image <file>` | Edit this image (workflows whose model edits; the size follows the input) |
| `-o <file or dir/>` | Also copy the result there |
| `--switch-back` | Stop ComfyUI afterwards so the LLM can load right away |
| `--steps N` / `--cfg X` | Override the workflow's steps / guidance, within its allowed range |
| `--sampler NAME` / `--scheduler NAME` | Any ComfyUI sampler or scheduler the workflow has a setting for |
| `--lora NAME[:STRENGTH]` | Add a LoRA the workflow offers; repeatable |
| `--reference [pose:]<file>` | A reference image (person, style, object) for klein workflows; `pose:` draws the photo's pose as a skeleton and passes that; `pose:NAME` takes a saved pose; repeatable |
| `--control <file> --control-type T [--control-strength X]` | Guide the image with a ControlNet (z-image-turbo and the SDXL workflows): `canny` or `pose` for a photo, a ready-made `depth`, … map, or `pose:NAME` (a saved pose) with type `pose` |
| `--upscale NAME[:FACTOR]` | Enlarge the result with an upscale model (default factor 2), any workflow |
| `--save-maps` | Also save the maps den draws (pose skeleton, canny edges) beside the image |

Ctrl+C cancels the request, in ComfyUI too.

| Command | What it does |
|---|---|
| `den pose` / `den pose list [--json]` | The saved poses: name, description, aspect and size |
| `den pose save <photo> <name> -d "…" [--replace]` | Draw the photo's pose and save it (GPU, a few seconds) |
| `den pose mv <old> <new>` / `den pose rm <name>` | Rename / delete a saved pose (its skeleton, photo and description) |

### Reviewing delegations

Every `local_llm` call Claude makes is logged with an id and its verdict (see
[How delegation works](#how-delegation-works)). Review after ~20 delegations and adjust the
trust per task in `~/.claude/CLAUDE.md` and the ADR.

| Command | What it does |
|---|---|
| `den log` | Per task: calls, average input tokens and seconds, verdict counts, latest problem notes |
| `den log --notes 30` | Show more problem notes |

### Files and environment

| Item | Purpose |
|---|---|
| `config.toml` | Edit by hand: broker, Ollama and ComfyUI URLs, batch caps, shared LLM settings, image settings and workflow mappings, tasks and their system prompts, defaults |
| `config.local.toml` | Optional, git-ignored: private additions merged over `config.toml`, e.g. a workflow's `note`, which is appended to its description. `DEN_CONFIG_LOCAL=<path>` uses a different one |
| `workflows/` | ComfyUI graphs (API format), one per workflow |
| `integrations/pi/den.ts` | pi extension: `generate_image`, `/imagine`, inline images, footer status. `DEN_BIN=<path>` names the `den` it runs |
| `state.json` | Written by the broker (mode) and the CLI (active model, task overrides). Delete it to reset to the defaults |
| `systemd/den.service` | The broker's user unit. Logs: `journalctl --user -u den -f` |
| `curl -s localhost:11435/status` | The broker's state as JSON: mode, a pending switch, the loaded side, running and waiting requests, loaded models, ComfyUI |
| `DEN_CONFIG=<path>` | Use a different config file |
| `DEN_STATE=<path>` | Use a different state file (e.g. for testing without touching the real one) |
| `~/.local/state/den/delegations.jsonl` | Delegation log (outside git). `DEN_LOG=<path>` uses a different one |
| `~/.local/state/den/images.jsonl` | Image log. `DEN_IMAGE_LOG=<path>` uses a different one |
| `DEN_IMAGES=<dir>` / `COMFYUI_DIR=<dir>` | Where images are saved / where ComfyUI and its models live |
| `~/.local/share/den/poses/` | The pose library (outside git). `DEN_POSES=<dir>` uses a different one |

## Ollama cheatsheet

### Service

Ollama is a **system** service running as its own user, so den can't start it: den runs as you
and never uses sudo. It doesn't try to hide that either — when Ollama is down, every path says
so and names the command:

- callers (Claude's tool, pi, `den ask`) get `ollama is not reachable at … ; start it with:
  sudo systemctl start ollama`, so whoever is at the keyboard is told;
- the broker writes `OLLAMA DOWN at … ` to its journal (`journalctl --user -u den -f`);
- `den status` prints `ollama DOWN: …` **and exits non-zero**, which is the hook for a
  notifier or a shell check;
- pi's footer says `den: ollama is down — sudo systemctl start ollama` while it works.

`Restart=on-failure` in Ollama's unit already covers crashes, so this is mostly about a
deliberate stop.

| Command | What it does |
|---|---|
| `systemctl status ollama` | Is it running? |
| `sudo systemctl start` / `stop` / `restart ollama` | Control the service |
| `sudo systemctl enable` / `disable ollama` | Start at boot or not |
| `journalctl -u ollama -f` | Follow the server log (loading, GPU layers, errors) |
| `ollama -v` | Ollama version |

### Models

| Command | What it does |
|---|---|
| `ollama list` | Downloaded models and their sizes |
| `ollama pull <model>` | Download or update a model (e.g. `qwen3.6:35b-a3b`) |
| `ollama rm <model>` | Delete a model from disk |
| `ollama show <model>` | Architecture, parameters, context length, quantization |
| `ollama show <model> --modelfile` | The full Modelfile (template, default parameters) |
| `ollama cp <src> <dst>` | Copy or rename a model |
| `ollama create <name> -f Modelfile` | Build a custom variant (e.g. a baked-in `num_ctx` or system prompt) |

Models live in `/var/lib/ollama` (owned by the `ollama` user). Browse tags at
<https://ollama.com/library>.

### Running and memory

| Command | What it does |
|---|---|
| `ollama ps` | Loaded models, their size, **GPU/CPU split** and when they unload |
| `ollama stop <model>` | Unload now and free GPU/RAM (`den unload` does this for all models, through the broker) |
| `ollama run <model>` | Interactive chat |
| `ollama run <model> "prompt"` | One-shot answer |
| `ollama run <model> --verbose` | Also print speed (look at `eval rate` for tok/s) |
| `ollama run <model> --think=false` | Turn off thinking for models that support it |
| `ollama run <model> --keepalive 30m` | Keep it loaded for 30 min after use (`0` = unload right away). den uses `keep_alive` from `config.toml` (30m) |
| `nvidia-smi` | GPU memory actually in use |

Inside `ollama run`:

| Command | What it does |
|---|---|
| `/?` | Help |
| `/set parameter num_ctx 32768` | Context window size (larger uses more memory) |
| `/set parameter temperature 0.2` | Less random output |
| `/set system "..."` | Set a system prompt |
| `/set verbose` | Show speed after each answer |
| `/show info` | Model details |
| `/clear` | Clear the conversation |
| `/bye` | Exit (the model stays loaded until keep-alive expires) |

### HTTP API (what the broker passes on)

Use port 11435 (the broker) for anything that runs a model. Only the broker uses 11434.

```sh
curl -s localhost:11435/status                          # broker: mode, running requests, loaded models
curl -s localhost:11434/api/version
curl -s localhost:11434/api/ps                          # loaded models
curl -s localhost:11434/api/generate -d '{"model":"qwen3.6:35b-a3b","prompt":"hi","stream":false}'
curl -s localhost:11434/api/generate -d '{"model":"qwen3.6:35b-a3b","keep_alive":0}'   # unload
```

### Server settings

Server-wide settings are environment variables on the service. Set them with
`sudo systemctl edit ollama`:

```ini
[Service]
Environment="OLLAMA_KEEP_ALIVE=10m"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
```

Then run `sudo systemctl restart ollama`.

| Variable | Effect |
|---|---|
| `OLLAMA_KEEP_ALIVE` | Default time a model stays loaded after use |
| `OLLAMA_MAX_LOADED_MODELS` | How many models can be loaded at once |
| `OLLAMA_FLASH_ATTENTION=1` | Faster attention that uses less memory |
| `OLLAMA_KV_CACHE_TYPE` | `f16` (default), `q8_0` or `q4_0`: smaller context memory at a slight quality cost (needs flash attention) |
| `OLLAMA_CONTEXT_LENGTH` | Default `num_ctx` when a request doesn't set one |
| `OLLAMA_HOST` | Address to listen on (default `127.0.0.1:11434`) |
