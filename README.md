# den

HQ for a local AI toolchain. Claude Code stays the main coding agent and hands selected
cheap, bulk or private tasks to a local LLM served by llama-server, through the `local_llm` MCP
tool. [pi](#pi) is the local chat and coding agent on the same model. Images come from
ComfyUI, through `den image`, Claude's `generate_image` MCP tool or pi's extension.

Everything that uses the GPU (the `den` CLI, Claude's MCP server, pi) goes through the
**broker**, `den serve` on `127.0.0.1:11435`. It runs LLM requests on llama-server (models come
from Ollama, which is only the model store), runs image requests on ComfyUI, swaps the GPU
between the two and owns mode switches, so two models never fight over the 12 GB card. A swap
saves the conversation's cache and restores it afterwards, so the next turn doesn't re-read the
whole conversation ([ADR gpu-broker](docs/adr/0002-gpu-broker.md),
[ADR image-generation](docs/adr/0003-image-generation.md), [ADR llama-server](docs/adr/0005-llama-server.md)).

See `AGENTS.md` for the layout and design rules, `CONTEXT.md` for the vocabulary (broker,
side, mode, swap, batch cap, workflow, …) and `docs/adr/` for decisions.

## Status

| Part | State |
|---|---|
| Delegation to the local LLM (`local_llm` MCP tool, tasks, verdict log) | Done, in a trial period ([ADR delegation-policy](docs/adr/0001-delegation-policy.md)) |
| GPU broker, LLM side (pass-through, mode switches that wait, `keep_alive` for every caller) | Done ([ADR gpu-broker](docs/adr/0002-gpu-broker.md)) |
| pi through the broker | Done |
| ComfyUI install (`setup.sh`) and model tests | Done ([Image generation](#image-generation)) |
| Image side of the broker: swaps, batching, idle timeout, `den image` | Done ([ADR image-generation](docs/adr/0003-image-generation.md)) |
| Claude's `generate_image` MCP tool | Done ([Image generation](#image-generation)) |
| pi image extension (`generate_image` tool, `/imagine`, inline images, footer status) | Done ([pi](#images-in-pi)) |
| LLM on llama-server, the conversation's cache kept across swaps | Done ([ADR llama-server](docs/adr/0005-llama-server.md)) |

## Setup

### Requirements

- Linux with systemd, or macOS with launchd (den runs as a user service either way), and
  Python 3.11+ (stdlib only, no pip). See [ADR running-on-macos](docs/adr/0006-running-on-macos.md) for
  what differs; `den/platform.py` is the only file that knows.
- A GPU. It was built on a 12 GB NVIDIA card with a recent driver; the default model needs
  ~23 GB split across GPU and RAM, so plan for 32 GB of RAM or pick a smaller model. On
  Apple Silicon the GPU shares that same memory, so the model and everything else on the
  machine come out of one pool.
- [Ollama](https://ollama.com/download), running as a service: the model store. On macOS
  that is your own (`brew install ollama && brew services start ollama`), so no sudo is
  involved.
- `llama-server` from [llama.cpp](https://github.com/ggml-org/llama.cpp), which runs the
  models: with a CUDA backend on Linux (e.g. `pacman -S llama-cpp ggml-cuda`), or
  `brew install llama.cpp` on macOS, whose wheels carry Metal. `[llm] server` in
  `config.toml` names another binary.
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
  [pytorch.org](https://pytorch.org/get-started/locally/). On macOS there is none to pick:
  the ordinary wheels carry Metal).

On macOS it also writes `~/Library/LaunchAgents/ai.den.broker.plist` and
`ai.den.comfyui.plist` from the templates in `launchd/`, and loads the broker's. The system
`python3` there is older than den needs and comes first on PATH, so setup picks a newer one
for the service and tells you to put one ahead of it for the `den` command itself.

Two manual steps:

- **Context length:** nothing to set in Ollama any more: the broker starts llama-server with
  `num_ctx` from `config.toml`, and every caller shares that one server.
- **Claude's delegation rule:** add the "when to delegate" rule from
  [ADR delegation-policy](docs/adr/0001-delegation-policy.md) to `~/.claude/CLAUDE.md`. Without it Claude
  sees the tool but has no policy for when to use it.

### After changing code

Reconnect the MCP server in running Claude Code sessions with `/mcp`, and restart the broker
with `systemctl --user restart den` (`launchctl kickstart -k gui/$UID/ai.den.broker` on
macOS). Run `/reload` in running pi sessions after changing the
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
after loading takes about twice as long, plus ~5 s for llama-server to load the model. After a
swap to images, the conversation's saved cache is restored, so the next turn reads only the new
message instead of the whole conversation again. The model stays
loaded for 30 minutes after its last request from any caller, pi included (`keep_alive` in
`config.toml`: the broker stops llama-server after that, saving the conversation's cache first);
`den unload` empties the GPU
as soon as running requests finish (`--now` cancels them), without taking the LLM off: the
next delegation loads it again. `den mode off` unloads it too, but it also refuses new
requests and takes the tools away from every Claude session, so it's the kill switch, not the
way to free memory.

### Using the den on another machine

A machine can use the den on another machine — a **remote** — as if den existed only there: its
model, tasks, workflows, pose library and logs. The broker there stays on `127.0.0.1`, and an SSH
tunnel carries the requests; nothing but SSH is reachable on the network
([ADR remote-brokers](docs/adr/0007-remote-brokers.md)). No path crosses, only bytes: files you delegate and
input images are read here and sent, and each generated image comes back and is saved here under
`~/Pictures/den`, as a local den would. So you work with your own files; saved poses are
`pose:NAME` from that machine's library.

1. **A key for the tunnel only**, without a passphrase, since the tunnel starts unattended:
   `ssh-keygen -t ed25519 -f ~/.ssh/den-tunnel -N "" -C den-tunnel`.
2. **On the other machine**, allow that key nothing but den's port, in `~/.ssh/authorized_keys`:
   `restrict,port-forwarding,permitopen="127.0.0.1:11435",command="/usr/bin/false" ssh-ed25519 AAAA… den-tunnel`.
   Let it accept keys only (`PasswordAuthentication no` and `KbdInteractiveAuthentication no`,
   e.g. in `/etc/ssh/sshd_config.d/`).
3. **Keep the tunnel up** with a user unit running
   `ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -i ~/.ssh/den-tunnel -L 127.0.0.1:11436:127.0.0.1:11435 <host>`
   with `Restart=always`.
4. **Name it** in `config.local.toml`: `[remotes.mac]` with `base_url = "http://127.0.0.1:11436"`.
From the shell, `den --on mac` runs `status`, `mode`, `unload`, `ask`, `task`, `image` and
`pose list | show | save` there. Picking the model, toggling tasks, renaming or deleting poses and
`den log` happen on that machine itself. It must run a den recent enough to serve remote
clients; `den --on mac status` says when it isn't. This machine's own den keeps working as before.
Claude can have the same as an MCP server beside this machine's, registered by hand when wanted:
`claude mcp add --scope user den-mac -e DEN_BROKER=mac -- ~/…/den-ai/bin/den-mcp`.

## pi

[pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) is a terminal coding agent
that runs on the local model. Start it with plain `pi`. Its config lives in `~/.pi/agent/`,
outside this repo:

- **`models.json`:** the `ollama` provider with `baseUrl` `http://127.0.0.1:11435/v1` (the
  broker, never 11434) and each model with `contextWindow: 65536` (equal to `num_ctx` in
  `config.toml`), `maxTokens: 8192` and `reasoning: true`. Without `contextWindow`, pi assumes
  128k. Models with a vision projector in Ollama get `"input": ["text", "image"]`, so pi's `read`
  tool can show them a picture; `setup.sh` checks for the projector, since a model can list vision
  and ship without one. Qwen models also need two `compat` keys:
  `"thinkingFormat": "qwen-chat-template"` hands thinking on/off to the chat template, and
  without it pi's thinking level has no effect at all (thinking stays on);
  `"thinkingTokenBudgetField": "thinking_budget_tokens"` carries the level itself, since the
  template takes no effort level and llama-server only takes a budget. `setup.sh` writes both
  for new files; add them by hand to an existing one.
- **Thinking level:** off, low, medium and high are the token budgets under `thinkingBudgets`
  in `settings.json`, and the model stops thinking when one runs out. pi's own defaults (`low`
  2048, `medium` 8192, `high` 16384, each capped at `maxTokens - 1024`) are minutes of thinking
  at a few tokens a second, so set your own, e.g.
  `"thinkingBudgets": {"minimal": 128, "low": 256, "medium": 768, "high": 2048}`. Pick the level
  at the start of a session: changing it mid-conversation re-renders every earlier answer, so the
  next turn re-reads the whole conversation once (about 40 s at 20k tokens); after that turns are
  fast again, across image swaps too.
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
- **Clips and voice:** `generate_clip` (and `get_clip`) with keyframes, sound, a voice-over and
  lip-sync, `generate_voice` for a voice-over track, and `transcribe_audio` for a recording's
  timed SRT, all from the broker's own specs, as Claude's tools are. A script or a recording pi
  is given as a path goes to the broker as its content, so they work on a den on another machine
  too. `generate_clip` and `generate_voice` end the turn like the image tool; the transcript
  comes back to the model.
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

Each is a graph in API format plus its entry in the config, which says where the prompt,
negative prompt, seed and size go. A workflow can run once the model files its graphs name are
in `~/ComfyUI/models` (`COMFYUI_DIR` overrides the location); `den image` says what's missing.

**Two places for workflows:**

- **`workflows/` in the repo holds examples**: the workflows above and the [clip
  workflows](#clips), each with its entry in `config.toml`. They use public models under their
  official file names, so a fresh clone runs them once the models are downloaded. Copy one as a
  starting point for your own.
- **Your own go in `~/.config/den/workflows/`** (`DEN_WORKFLOWS` overrides it), with their
  entries in the git-ignored `config.local.toml`. Nothing of them is versioned, which suits a
  workflow built on files only you have, or one you'd rather keep to yourself. A graph there
  wins over the repo's of the same name, so you can also change an example without touching
  the repo.
- **Hide an example** your own workflow replaces with `enabled = false` under its name in
  `config.local.toml` (`[clip.workflows.ltx23-distilled]`, `[image.workflows.<name>]`): no client
  sees it, and a request for it is refused as unknown.

To add one, build it in the web UI, export it with Workflow → Export (API), save it as
`<name>.json` in one of those folders and add its `[image.workflows.<name>]` entry (or
`[clip.workflows.<name>]`). If the model can edit, add the editing graph as `<name>-edit.json`
with its mapping under `[image.workflows.<name>.edit]` (including the LoadImage input); requests
with an input image use it. Only the klein workflows edit; the others generate from text only.
A workflow's description says its style and prompt format; anything else about it (what it's
for, what it will or won't make) goes in a `note` in `config.local.toml`, which is appended to
the description.

**Quantized models:** `setup.sh` installs the [ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF)
custom node, so a graph can load a `.gguf` model or text encoder (`UnetLoaderGGUF`,
`DualCLIPLoaderGGUF`) where the full-size one wouldn't fit beside the GPU. den counts `.gguf`
files like any other model file.

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

### Clips

Short silent video clips come from clip workflows, on the same image side (ComfyUI) as images
([ADR clip-generation](docs/adr/0009-clip-generation.md)).

```sh
den clip                                        # list clip workflows and their options
den clip "A fox trots through fresh snow, the camera tracking beside it" -d 4
den clip "She turns toward the window" --keyframe ~/Pictures/den/…/still.png
den clip "…" -w wan22-5b --size 704x1280 -n "extra limbs" --lora my-lora:0.8
den clip "…" --detach                           # answer with an id at once
den clip --id 12 --wait                         # pick it up later
```

| Workflow | Good for | Prompt |
|---|---|---|
| `ltxv-13b` (default) | fast clips (about 15 s of work per second of clip at 720p); a start keyframe and up to two more anywhere | one long, detailed English paragraph |
| `wan22-5b` | general clips up to 720p at 24 fps, from text or a start image | one detailed paragraph, motion words over style words |
| `ltx23-distilled` | realistic motion and people **with sound**, three keyframes like `ltxv-13b`; slower, its model partly held in RAM (GGUF) | a paragraph of what happens in order, the camera, and the sounds |

- **A clip is a detached request:** it takes minutes, so the broker answers with an id at once
  and the clip goes on without the caller. `den clip` waits unless `--detach`; Claude's
  `generate_clip` returns the id and `get_clip` waits for it; the Android app picks a clip up
  again when it comes back.
- **Results** go to `~/Videos/den/YYYY-MM-DD/<time>-<slug>.mp4` (`DEN_CLIPS` overrides) with a
  contact sheet beside it: four frames, first to last, in a 2x2 grid. Every clip is logged to
  `~/.local/state/den/clips.jsonl`, which also gives the time estimates.
- **Keyframes** are images the clip must show at given moments (`@AT`: seconds, `50%` or
  `end`); the first starts it. A workflow says how many it takes and where. Without a `--size`,
  a clip takes its start keyframe's shape.
- **Sound:** LTX-2.3 makes the audio with the picture, so its clips have a sound track; the
  others are silent. `den clip` lists which make sound; `--no-sound` leaves the track out, and
  asking a silent workflow for sound is refused. The summary says whether the file has one. Describe the sounds in the prompt (ambience, footsteps, a line of dialogue in
  quotes, which a person on screen then speaks); without that you get ambient sound at most.
- **Duration** is in seconds, within the workflow's range; den turns it into the frame count
  the model wants.
- **A negative prompt is added to the workflow's own**, never replacing it. On a workflow that
  samples at cfg 1 (`ltxv-13b`) it has no effect unless you also raise `--cfg`, which doubles the
  time; `den clip` says so per workflow.
- **LoRAs** work as for images: an `[image.loras.<name>]` entry whose `family` matches the clip
  workflow's (`ltxv`, `wan22-5b`) is offered to it once its file is in `~/ComfyUI/models/loras`.
  A LoRA works only on the model it was trained for.
- **A voice-over** (`--voiceover`) is spoken first and mixed into the clip: see below.

### Voice-overs

Speech in a voice cloned from your own recording, from a line of text or an SRT script
([ADR voice-overs](docs/adr/0010-voice-overs.md)). The speech model supports 23 languages, in a venv
of its own that `setup.sh` makes (`--no-speech` skips it); it runs on the image side and only
while a voice request does.

```sh
den voice                                       # voices and languages
den voice --add narrator ~/Recordings/me.m4a    # keep a recording as a voice
den voice "Welcome to the harbour." -v narrator # one line, as a WAV track
den voice --srt script.srt -v narrator -l de    # each line at its time, on one track
den voice --lines lines.txt -v narrator         # lines in turn; den writes the SRT at their times
den voice --transcribe me.m4a                   # what a recording says, as a timed SRT
den voice --design "an old man with a deep, raspy, slow voice"   # a draft voice from a description
den voice "A line to try." -v draft:ID          # try the draft through the speech model
den voice --save ID grandpa                     # keep it once you like it (--keep-as skips the draft)
den voice --show grandpa                        # a voice's details; --mv OLD NEW, --describe NAME TEXT
den clip "…" --voiceover script.srt --voice narrator   # a clip with the voice mixed in
den clip "…he says to the camera…" --voiceover lines.txt --voice narrator --lip-sync
den clip "…he says to the camera…" --voiceover me.m4a --lip-sync   # your own recording, lip-synced
```

- **Recording a voice:** about 10 seconds of natural speech in a quiet room, phone close to
  your mouth, in the tone the narrator should have, in the language you'll use most. Any common
  audio format works.
- **Lip-sync:** with `--lip-sync` (`sync` in a tool's voiceover) the voice goes into the model
  and the picture is made to it, so a person on screen speaks the lines; only on workflows that
  make sound and picture together (LTX-2.3). Without it the voice is a narrator over the clip.
- **Every voice job writes an SRT** beside its track, at the times the lines were spoken:
  subtitles, or a script to reuse.
- **Scripts:** each SRT line is spoken at its start time. Give a line about 2.5 words a second:
  one that runs long pushes the next one later, and the result lists every overrun as a note.
- **On a clip:** with an SRT and no `--duration`, the clip lasts to the script's end, and longer
  if the voice runs long. On a workflow with sound the voice goes over the clip's own sound,
  turned down; on a silent one it's the sound track.
- **Settings:** `--exaggeration` (0.25–2, default 0.5) for how expressive, `--cfg-weight` (0–1,
  default 0.5; lower is slower and calmer), `--seed` to repeat a take.
- **Results** go to `~/Music/den/YYYY-MM-DD/` (`DEN_SPEECH_OUT`), logged to
  `~/.local/state/den/speech.jsonl`.
- **The voice library** is `~/.local/share/den/voices/` (`DEN_VOICES`), outside the repo like
  the pose library: each voice's sample and a `NAME.json` with what it is (description, designed
  or recorded, language, seed, date). `den voice` lists them with their descriptions, and
  Claude's and pi's `list_voices` tool shows the same; no tool text names a voice, so keeping
  one doesn't change the tools. The phone plays a voice's sample (Listen).
- **Claude and pi** get a `generate_voice` tool, and `generate_clip` takes a `voiceover`; the
  phone's Clip pane has a voice-over field (a script, lines, or a text), a voice and language
  picker, a lip-sync switch, the timed script of the last clip (reuse or save it), records a new
  voice with the microphone, and records your narration: its words come back as the script, and
  the recording itself can be the voice-over. `transcribe_audio` is Claude's and pi's tool for it.
  The speech model marks its audio with an inaudible watermark.

### Live conversation

Claude can talk with you by voice at the PC ([ADR live-conversation](docs/adr/0011-live-conversation.md)):
den listens through the microphone (a transcription model) and speaks Claude's replies in a voice from the
library, and you can interrupt it mid-sentence. Ask Claude to talk; it starts live mode itself.

- **It takes the whole machine**: every other request (images, clips, pi, delegation) is refused
  until it ends, and it ends by itself after ten minutes without an exchange.
- **Audio** goes through PipeWire's echo canceller, loaded for the session, so the voice from
  speakers isn't taken for you; headphones work too.
- **Every conversation is kept** (`~/.local/state/den/live/`) until you decide: keep it under a
  name (`~/.local/share/den/conversations/`), delete it, or have den's own LLM summarize it.

```sh
den live on -v narrator # by hand; Claude's live_start does the same
den live off            # ends it and prints the transcript
den live list           # every conversation, with its summary
den live show ID        # one transcript
den live summary ID     # a summary by den's LLM, kept beside it
den live keep ID NAME   # keep it under a name
den live rm ID          # delete it
```

A client that drives the conversation itself can also speak unasked while `talk`
waits for you: `POST /live/talk` with `now: true` ends the waiting talk (`preempted`) once the
voice has finished its sentence, and speaks at once.

**Standby** ([ADR standby](docs/adr/0012-standby.md)) listens between conversations for a wake word,
such as "hey Elli", without taking the machine: only a small transcription model on the CPU hears the start of
each utterance, and forgets it unless it's the wake word. Images, clips and the LLM keep working,
and other programs can still record from the microphone. When the wake word is heard, den tells
the client, which starts the conversation; what you say from the wake word on is kept, so your
first words aren't lost while the machine is freed. A conversation pauses standby and it comes
back after; `den unload` leaves it on, `den mode off` ends it.

```sh
den standby on "hey Elli"  # listen for it (again: another wake word)
den standby wait           # until the next change: woke, live, listening again, off
den standby off
```

Clients use `POST /standby/start {wake_word}`, `POST /standby/wait {after, timeout_s}` (answers
once standby's `seq` has passed `after`), `POST /standby/stop` and `GET /standby`.

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
  library exists, so saving a pose never changes that tool's text (see ADR image-generation).
- **Looking at one:** `den pose show NAME`, or `list_poses` with a name, which shows the skeleton
  and the photo (as images to Claude, inline in pi) with their paths.
- **Curating** is yours: `den pose mv OLD NEW` and `den pose rm NAME`. `den pose refresh [NAME…]`
  redraws poses from their stored photos, e.g. to add keypoints to ones saved before den kept them.
- **Files:** `~/.local/share/den/poses/` (`DEN_POSES` overrides it) holds per pose the skeleton
  (`NAME.png`), a copy of the photo (`NAME.source.<ext>`), the joints as data
  (`NAME.keypoints.json`, OpenPose format) and its description (`NAME.json`).
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
| `den-clip` | Clips: the look as stills first (from `den-image`), keyframes made by editing one still, a person between two stills, sound from the prompt, a voice-over on a clip, and the measured traps (weak camera instructions, negatives at cfg 1, what a clip costs) |
| `den-voice` | Voice-overs: recording a voice, writing an SRT script whose lines fit their times (about 2.5 words a second), languages, expressiveness and pacing, and checking a take you can't hear by asking |

A skill carries strategy; the tool descriptions carry the options, which change as models are
added. An agent reads a skill's `SKILL.md` when a task matches, and its `references/` only when
it needs the detail. In pi, `/skill:den-image` (or `den-clip`, `den-voice`) loads one by hand.

## `den` cheatsheet

Changes take effect immediately. The broker, the CLI and the MCP server re-read
`config.toml` and `state.json` every time, so nothing needs a restart. Every command except
`task`, `log` and `mode` without an argument needs the broker; when it's down they say
`den broker not running …` with the command this system starts it with.

### Status

| Command | What it does |
|---|---|
| `den status` | Mode, broker (up? which side holds the GPU? a switch waiting? requests running or waiting), active model (pulled? loaded in llama-server, with its pid?), Ollama (the model store: version, anything it loaded outside den), ComfyUI (up? idle for how long?) and runnable workflows, tasks |
| `den serve` | Run the broker in the foreground (the systemd unit does this) |
| `den --on NAME <cmd>` | Use the den on another machine, `[remotes.NAME]`, as if it were the only one: `status`, `mode`, `unload`, `ask`, `task`, `image`, `pose list/show/save` |
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
| `den model <name>` | Switch to an Ollama model, e.g. `qwen3.6:35b-a3b`. Downloads it first if missing; llama-server switches to it on the next request |
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
| `den pose show <name> [--json]` | One saved pose: description, size and the paths of its files |
| `den pose refresh [<name>…]` | Redraw saved poses from their stored photos (default: all), adding keypoints (GPU, a few seconds each) |
| `den pose mv <old> <new>` / `den pose rm <name>` | Rename / delete a saved pose (its skeleton, photo, keypoints and description) |

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
| `workflows/` | Example ComfyUI graphs (API format), one per image or clip workflow |
| `~/.config/den/workflows/` | Your own graphs, outside git; one there wins over the repo's of the same name. `DEN_WORKFLOWS=<dir>` uses a different one |
| `integrations/pi/den.ts` | pi extension: `generate_image`, `/imagine`, inline images, footer status. `DEN_BIN=<path>` names the `den` it runs |
| `state.json` | Written by the broker (mode) and the CLI (active model, task overrides). Delete it to reset to the defaults |
| `systemd/den.service` | The broker's user unit. Logs: `journalctl --user -u den -f` |
| `launchd/*.plist` | The same two services for macOS, as templates setup fills in. Logs: `~/.local/state/den/den.log`, `comfyui.log` |
| `curl -s localhost:11435/status` | The broker's state as JSON: mode, a pending switch, the loaded side, running and waiting requests, loaded models, ComfyUI |
| `DEN_CONFIG=<path>` | Use a different config file |
| `DEN_STATE=<path>` | Use a different state file (e.g. for testing without touching the real one) |
| `DEN_BROKER=<name>` | Send this process's requests to the remote `[remotes.<name>]` (what `den --on` sets, and a `den-<name>` MCP server if you register one) |
| `~/.local/state/den/delegations.jsonl` | Delegation log (outside git). `DEN_LOG=<path>` uses a different one |
| `~/.local/state/den/images.jsonl` | Image log. `DEN_IMAGE_LOG=<path>` uses a different one |
| `~/.local/state/den/clips.jsonl` | Clip log, and the source of clip time estimates. `DEN_CLIP_LOG=<path>` uses a different one |
| `~/.local/share/den/voices/`, `~/.local/state/den/speech.jsonl` | The voice library and the speech log. `DEN_VOICES=<dir>`, `DEN_SPEECH_LOG=<path>` use others; `SPEECH_DIR` is where the speech venv lives |
| `DEN_IMAGES=<dir>` / `DEN_CLIPS=<dir>` / `COMFYUI_DIR=<dir>` | Where images / clips are saved, and where ComfyUI and its models live |
| `~/.local/share/den/poses/` | The pose library (outside git). `DEN_POSES=<dir>` uses a different one |
| `~/.cache/den/slots/` | Saved conversation caches, one per model (mode 700). `DEN_SLOTS=<dir>` uses a different one |
| `~/.local/state/den/llama-server.log` | llama-server's log for the current run (loading, GPU fit, timings) |
| `$XDG_RUNTIME_DIR/den/llm.sock` | llama-server's private socket; only the broker talks to it. `DEN_LLM_SOCKET=<path>` overrides it |

## Ollama cheatsheet

den keeps its models in Ollama and downloads with it, but doesn't run them there any more:
llama-server does ([ADR llama-server](docs/adr/0005-llama-server.md)). The commands below still manage
the store, and `ollama run` still works for trying a model outside den, as long as den isn't
using the GPU at the same time.

### Service

Ollama is a service den can't start: on Linux it is a **system** service running as its own
user, and den runs as you and never uses sudo. On macOS it is your own service
(`brew services`), but den still leaves it to you — one thing starts Ollama, and it isn't den.
It doesn't try to hide that either — when Ollama is down, every path says so and names the
command for this system:

- callers (Claude's tool, pi, `den ask`) get `ollama is not reachable at … ; start it with:
  sudo systemctl start ollama` (`brew services start ollama` on macOS), so whoever is at the
  keyboard is told;
- the broker writes `OLLAMA DOWN at … ` to its journal (`journalctl --user -u den -f`, or
  `~/.local/state/den/den.log` on macOS);
- `den status` prints `ollama DOWN: …` **and exits non-zero**, which is the hook for a
  notifier or a shell check;
- pi's footer says `den: ollama is down — sudo systemctl start ollama` while it works.

`Restart=on-failure` in Ollama's unit already covers crashes, so this is mostly about a
deliberate stop.

| Command | What it does | On macOS |
|---|---|---|
| `systemctl status ollama` | Is it running? | `brew services info ollama` |
| `sudo systemctl start` / `stop` / `restart ollama` | Control the service | `brew services start` / `stop` / `restart ollama` |
| `sudo systemctl enable` / `disable ollama` | Start at boot or not | `brew services start` already does; `brew services stop` undoes it |
| `journalctl -u ollama -f` | Follow the server log (loading, GPU layers, errors) | `tail -f $(brew --prefix)/var/log/ollama.log` |
| `ollama -v` | Ollama version | same |

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
| `ollama stop <model>` | Unload a model Ollama itself loaded (e.g. from `ollama run`); den's own model runs on llama-server, which `den unload` stops |
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

Server-wide settings are environment variables on the service. They matter less than they
did, since llama-server runs the models now and Ollama only stores them. Set them with
`sudo systemctl edit ollama` (on macOS, `brew services` reads the plist in
`$(brew --prefix)/opt/ollama`):

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
