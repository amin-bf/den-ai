# den

HQ for a local AI toolchain. Claude Code stays the main coding agent and hands selected
cheap, bulk or private tasks to a local LLM served by Ollama, through the `local_llm` MCP
tool. [pi](#pi) is the local chat and coding agent on the same model. Images come from
ComfyUI, through `den image` for now.

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
| Claude's `generate_image` MCP tool | Next |
| pi image extension (`/imagine`, inline images) | Planned |

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
- installs pi if it's missing and writes `~/.pi/agent/models.json` pointing at the broker (only
  when that file doesn't exist yet);
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
with `systemctl --user restart den`. Config and state changes need neither.

Point every client at the broker, not at Ollama (see [pi](#pi)). Clients that go to Ollama
directly (`ollama run`, `ollama launch claude`, `curl localhost:11434`) bypass the broker, and
a mode switch can't wait for them.

## How delegation works

Claude Code sees two MCP tools while the LLM is on (`den mode llm`). Both disappear in
`mode off`.

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
`config.toml`, applied by the broker); `den mode off` frees the GPU
as soon as running requests finish (`--now` cancels them).

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
- **Mode:** pi works only while the LLM is on. In `den mode off` it gets "the local LLM is off
  … den mode llm".
- **Checking the traffic:** `journalctl --user -u den -f` shows one line per request with the
  caller (`pi`, `claude`, `cli`), model, status and duration.

## Image generation

ComfyUI lives in `~/ComfyUI` (installed by `setup.sh`) with a user unit `comfyui.service` on
`127.0.0.1:8188`. It isn't enabled: the broker starts it when an image is requested and stops it
when the LLM needs the GPU.

```sh
den mode both                       # LLM and images available, swapped on demand
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
- **Modes:** `den mode llm` stops ComfyUI (after running images finish, or at once with `--now`),
  `den mode image` unloads the LLM, and `den mode off` does both.
- **The web UI** at <http://127.0.0.1:8188> works whenever ComfyUI is up (e.g. after a
  `den image`), and the idle timeout leaves it running while its queue is busy. Don't start
  ComfyUI by hand while the LLM is loaded: the broker can't see it.

### Workflows

| Workflow | Good for | Prompt |
|---|---|---|
| `z-image-turbo` (default) | fast general images, legible text | a few short concrete sentences |
| `flux2-klein-4b` | the fastest drafts and simple assets; **edits** an input image (`--image`) | a plain description, or an edit instruction |
| `chroma1-hd` | slow, highest-quality photorealism (cfg 6, 35 steps) | a long detailed caption, plus a negative prompt |
| `wai-illustrious` | anime and illustration, with a hires-fix pass | Danbooru tags, positive and negative |

Each is `workflows/<name>.json` (a graph in API format) plus `[image.workflows.<name>]` in
`config.toml`, which says where the prompt, negative prompt, seed and size go. To add one, build
it in the web UI, export it with Workflow → Export (API), save it under `workflows/` and add
its mapping. If the model can edit, add the editing graph as `workflows/<name>-edit.json` with
its mapping under `[image.workflows.<name>.edit]` (including the LoadImage input); requests with
an input image use it. Of the four, only klein edits; the others generate from text only.
A workflow can run once the model files its graphs name are in `~/ComfyUI/models`
(`COMFYUI_DIR` overrides the location).

### Image models

ComfyUI doesn't come with models. See the
[ComfyUI README](https://github.com/comfyanonymous/ComfyUI#readme) and the
[ComfyUI docs](https://docs.comfy.org/) for which folder each file goes in under
`~/ComfyUI/models/`. The quickest way: open a template in the web UI (Workflow → Browse
Templates); it lists the files it's missing, with download links.

Models tested on a 12 GB card (warm, ~1024² per image):

| Model | Time | Prompt style |
|---|---|---|
| FLUX.2 klein 4B (fp8) | 2 s | Short descriptions; also does editing |
| Z-Image-Turbo | 7.5 s | Short sentences; renders text well |
| WAI-Illustrious SDXL | 10 s (29 s with hires fix) | Tags, anime and illustration |
| Chroma1-HD (fp8) | 40–50 s | Long, detailed captions; realistic |

Each one fills the card, so only one model runs at a time.

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

### Modes: who gets the GPU

| Command | What it does |
|---|---|
| `den mode` | Show the current mode |
| `den mode llm` | LLM on, image off (default). Stops ComfyUI after running images finish |
| `den mode image` | Images on, LLM off. Unloads the LLM after running requests finish |
| `den mode both` | Both on, one loaded at a time, swapped on demand with batching |
| `den mode off` | Everything off. Refuses new requests, waits for the running ones (and shows them), unloads the LLM, stops ComfyUI and removes the tool from Claude. Ctrl+C drops the switch |
| `den mode <mode> --now` | Same, but cancels running requests of the sides turned off instead of waiting; their callers get an error |

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
| `den image "<prompt>"` | Generate with the default workflow; prints the saved path, then seed and time on stderr |
| `-w <workflow>` / `-n "<negative>"` | Pick a workflow / give a negative prompt (workflows that take one) |
| `--seed N` / `--size 832x1216` | Fix the seed (default random) / override the workflow's size |
| `--image <file>` | Edit this image (workflows whose model edits; the size follows the input) |
| `-o <file or dir/>` | Also copy the result there |
| `--switch-back` | Stop ComfyUI afterwards so the LLM can load right away |

Ctrl+C cancels the request, in ComfyUI too.

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
| `state.json` | Written by the broker (mode) and the CLI (active model, task overrides). Delete it to reset to the defaults |
| `systemd/den.service` | The broker's user unit. Logs: `journalctl --user -u den -f` |
| `curl -s localhost:11435/status` | The broker's state as JSON: mode, a pending switch, the loaded side, running and waiting requests, loaded models, ComfyUI |
| `DEN_CONFIG=<path>` | Use a different config file |
| `DEN_STATE=<path>` | Use a different state file (e.g. for testing without touching the real one) |
| `~/.local/state/den/delegations.jsonl` | Delegation log (outside git). `DEN_LOG=<path>` uses a different one |
| `~/.local/state/den/images.jsonl` | Image log. `DEN_IMAGE_LOG=<path>` uses a different one |
| `DEN_IMAGES=<dir>` / `COMFYUI_DIR=<dir>` | Where images are saved / where ComfyUI and its models live |

## Ollama cheatsheet

### Service

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
| `ollama stop <model>` | Unload now and free GPU/RAM (`den mode off` does this for all models) |
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
