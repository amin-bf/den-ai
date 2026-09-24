# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, …) working in this repo.

## What this is

**den** is the HQ for a **local AI toolchain**. Claude stays the main coding agent;
it delegates selected cheap, bulk or private tasks to a local LLM. Images and clips come from
ComfyUI, and voice-overs from a speech model, all on top of the GPU broker.

- **Runtime:** `llama-server` (llama.cpp) runs the LLM, started by the broker on the selected
  model; Ollama is the model store (downloads, and which GGUF file a model is). Models are split
  between GPU and RAM ([ADR 0005](docs/adr/0005-llama-server.md)).
- **Integration:** a stdlib-only MCP server exposes `local_llm` (delegation), `generate_image`,
  `generate_clip`, `generate_voice`, `transcribe_audio` and `release_resources` (hand the machine
  back) to Claude Code; pi's extension and the Android client offer the same through the broker.
- **GPU broker:** `den serve` (user unit `den.service`, `127.0.0.1:11435`) sits in front
  of llama-server, ComfyUI and the speech server. The CLI, the MCP server and pi all go
  through it ([ADR 0002](docs/adr/0002-gpu-broker.md)).

## This repo is public

Everything committed is published at https://github.com/amin-bf/den-ai. Be discreet:

- **No personal or host details:** no hardware specs, OS, home paths, usernames, emails, IPs
  or hostnames beyond `127.0.0.1`. Write `~/…` and describe hardware generically ("a 12 GB GPU").
- **No private material:** nothing from work repos (names, code, PR numbers), exported
  chats or mail, credentials, or delegation logs. Test results describe inputs generically.
- **No content notes about what image models will or won't generate,** in code, config, tool
  descriptions or docs. Describe models by style and prompt format only. Content notes belong
  in the git-ignored `config.local.toml` (a workflow's `note`), never in versioned files.
- **A workflow built for explicit content, or on files only this machine has, stays out of the
  repo entirely:** its graph in `~/.config/den/workflows/` and its entry in `config.local.toml`.
  Model files it downloads get neutral local names, since the graph names them.
- **Nothing machine-specific in code, config or scripts:** no absolute paths. Derive the repo
  location from the script or module (`dirname "${BASH_SOURCE[0]}"`, `Path(__file__)`), and user
  paths from `$HOME` / `XDG_*` (`%h` in systemd units), with an environment override where a
  user might want another location. Anyone who clones the repo must be able to run it as is.
- **Working notes stay in git-ignored places** (`.branch-artifacts/`), which may be as
  detailed as needed. Check `git diff --cached` for anything above before each commit.

## Layout

| Path | Role |
|---|---|
| `config.toml` | Hand-edited: broker address, the LLM side (Ollama as model store, llama-server binary and arguments, keep-alive, context; no model names), batch caps, `[limits]` (the load and free RAM above which no side is loaded), image section (ComfyUI, idle timeout, workflow mappings), clip section (clip workflow mappings), delegated tasks with system prompts. |
| `state.json` | Written by the broker (mode) and the CLI, not versioned: the kill switch (`on`/`off`), active model, task on/off overrides. |
| `den/core.py` | Config and state loading, the Ollama (model store) and broker clients, `run_task` (with a guard against overflowing the context window), the machine's load and free RAM (`pressure`, `too_busy`). |
| `den/llm.py` | The LLM side: a model's GGUF file from Ollama's record, `llama-server` started and stopped on a private UNIX socket, the conversation's cache saved before a stop and restored after a start. |
| `den/broker.py` | `den serve`: streaming pass-through of `/v1` to llama-server (and of Ollama's catalogue and download endpoints), `/image`, `/pose` (which also draws without saving, `draw_only`, for a client that shows the skeleton before it is kept), `/clip` (clips as detached requests: an id at once, `GET /clip?id=` and `/clip/cancel` after, [ADR 0009](docs/adr/0009-clip-generation.md)), swaps between the sides with batch caps, idle timeout, in-flight and waiting requests, the busy gate, `/status`, `/mode` and `/unload` (both wait, `now` cancels, and both refuse meanwhile)., `/voice` and `/voices` (voice-overs on the image side: ComfyUI frees its models, the speech server runs for the request only, [ADR 0010](docs/adr/0010-voice-overs.md)) |
| `den/remote.py` | The den on another machine, used from one that keeps none of den's files: asks its broker for the tools, sends files and input images as bytes, saves the images that come back ([ADR 0007](docs/adr/0007-remote-brokers.md)). |
| `den/image.py` | Workflows (load, fill in, availability from model files), ComfyUI client, output files and the image log. |
| `den/clip.py` | Clip workflows on top of `image.py`'s helpers: duration to frames, keyframes, the contact sheet, clip files and the clip log with its time estimates ([ADR 0009](docs/adr/0009-clip-generation.md)).; a clip's `sound` and its voice-over, put into its graph |
| `den/speech.py` | Speech (ADR 0010): the speech server's process on a private UNIX socket (speaking, converting a recording, Whisper transcription), SRT scripts and lines den times, the voice library in `~/.local/share/den/voices/`, the voice track assembled at the script's times, and the speech log. |
| `speech/design.py` | The voice designer: Qwen3-TTS VoiceDesign speaks a sample in a voice made from a description, run once per voice in its own venv (`~/.local/share/den/voice-design/`); den keeps the sample as a voice. |
| `speech/server.py` | The speech server: Chatterbox Multilingual behind `GET /health` and `POST /speak`, `/convert` for a recording, and Whisper behind `POST /transcribe`. It runs in the speech venv `setup.sh` makes, not in den's Python, so it's the one file here that isn't stdlib. |
| `den/poses.py` | The pose library: saved poses (skeleton, photo copy, keypoints JSON, description) in `~/.local/share/den/poses/`, names, the table of contents, one pose's details, rename and delete. |
| `workflows/` | Example ComfyUI graphs in API format, one per image or clip workflow, on public models under their official file names; their mappings live in `config.toml`. Private workflows live in `~/.config/den/workflows/` (`DEN_WORKFLOWS`) with their entries in `config.local.toml`, and a graph there wins over the repo's of the same name. |
| `den/cli.py` | `den status / mode / unload / model / task / ask / image / clip / voice / pose / log / serve`. |
| `den/platform.py` | What differs between the systems den runs on: starting and stopping a service (systemd units, launchd agents), the runtime folder the LLM's socket goes in, free RAM, and the hints in messages. The only file that asks which system this is ([ADR 0006](docs/adr/0006-running-on-macos.md)). |
| `systemd/den.service` | The broker's user unit (linked with `systemctl --user link`). |
| `launchd/` | The same two services as launchd agents for macOS, as templates `setup.sh` fills in: launchd expands no home directory of its own, and absolute paths don't belong in the repo. |
| `setup.sh` | Idempotent setup: den (PATH link, service, MCP), den's skills (links), pi and ComfyUI (clone, venv, the ComfyUI-GGUF node pinned by commit, unit), the speech venv (Chatterbox pinned by commit) and the voice designer's (qwen-tts pinned). Never overwrites config, no sudo. |
| `CONTEXT.md` | Glossary: the domain's terms and the words to avoid. Use them in code, docs and tool text. |
| `den/mcp_server.py` | MCP stdio server: `local_llm`, `local_llm_feedback`, `generate_image` (with a small copy of the image), `generate_clip` and `get_clip` (with the contact sheet), `generate_voice`, `list_voices`, `design_voice`, `save_voice`, `transcribe_audio`, `list_poses`, `save_pose` and `release_resources` (always listed); sends `tools/list_changed` when config, state or the runnable workflows change. |
| `den/skills.py` | den's own skills as the broker serves them: `GET /skills` lists them, `GET /skill` gives one's text or a reference, so a client elsewhere can load one into a conversation when its user asks ([ADR 0007](docs/adr/0007-remote-brokers.md)). |
| `skills/` | den's own agent skills (Agent Skills format), versioned: `den-image` (images), `den-clip` (clips, which build on its stills) and `den-voice` (voice-overs) teach their tools' recipes and measured traps. `setup.sh` links each into `~/.claude/skills/` and `~/.agents/skills/`. |
| `clients/android/` | Android client (Kotlin, Compose): the den on another machine from a phone, over an SSH channel per request with a key made on the device; Status, Ask, Image and Poses. Its own README ([ADR 0007](docs/adr/0007-remote-brokers.md)). |
| `integrations/pi/den.ts` | pi extension: the `generate_image` tool, `/imagine`, inline images and the broker status in pi's footer. `setup.sh` links it into pi's extensions folder. |
| `docs/adr/` | Decisions and their reasons. Read the relevant one before changing an area. |
| `bin/den`, `bin/den-mcp` | Entry points (they add the repo root to `sys.path`). |
| `.agents/skills/` | Third-party skills for working on this repo, local only (git-ignored); `.claude/skills` is a symlink to it. Not den's own skills, which are in `skills/`. |

## Design rules

- **Python stdlib only.** No pip installs: `tomllib`, `urllib` and `json` are enough.
  `speech/server.py` is the exception: it runs in the speech venv, never in den's own Python.
- **Switch without restarts.** The broker, CLI and MCP server re-read config and state on
  every use. Code changes to the broker need `systemctl --user restart den`
  (`launchctl kickstart -k gui/$UID/ai.den.broker` on macOS).
- **One file knows which system this is.** Starting and stopping services, where a private
  socket may live, how much RAM is free, and every hint that names a command belong in
  `den/platform.py`; nothing else asks. den runs on Linux with systemd and on macOS with
  launchd, and a third system should mean a branch in that one file, not a search through
  the tree. Two launchd habits are already paid for: `bootout` and `bootstrap` both return
  before launchd has finished, so both wait ([ADR 0006](docs/adr/0006-running-on-macos.md)).
- **A remote is the den on another machine, used as if it were the only one.** Its broker
  stays on `127.0.0.1` and an SSH tunnel with a restricted key reaches it; den adds no auth of
  its own. A client reads only `[remotes.<name>]` from its own config; everything else (tasks,
  model, workflows, pose library, logs) comes from that broker. No path crosses either way,
  only bytes: files and input images go as name and bytes, results come back with `"bytes"`.
  A local caller that sends paths gets paths, unchanged
  ([ADR 0007](docs/adr/0007-remote-brokers.md)).
- **Voices are kept like poses, and no tool text lists them.** The voice library is
  `~/.local/share/den/voices/` (`DEN_VOICES`), outside the repo: each voice's sample and a
  `NAME.json` with its description, how it was made (designed or recorded), language, seed and
  date, so a model chooses by what a voice sounds like and a designed one can be made again. A
  model finds voices with `list_voices`; `generate_voice` and a clip's voiceover only say the
  library exists, because a tool whose text changes with every voice makes pi reread its
  conversation. A designed voice is a draft (`draft:ID`) until `save_voice` keeps it, once the
  user has heard it. `den voice --mv`, `--describe` and `--rm` rename, describe and delete, and
  the phone's Voices tab deletes after asking; no tool deletes a voice, as none deletes a pose.
- **Speech runs in a venv of its own, on the image side.** Chatterbox pins a torch and
  transformers that ComfyUI's venv has moved past, so installing it there would break image and
  clip generation; `setup.sh` gives it `~/.local/share/den/speech/` (`SPEECH_DIR`) instead. There
  is no third side: a voice request is an image-side request that asks ComfyUI to free its models,
  starts `speech/server.py` and stops it when the request ends, so nothing of speech stays loaded.
  A clip's voice-over is spoken first and mixed into the clip's own graph; the clip is built
  longer if the spoken track outruns the script's times ([ADR 0010](docs/adr/0010-voice-overs.md)).
- **Everything that uses the GPU goes through the broker.** Clients never call Ollama
  (11434) or llama-server directly; llama-server listens only on a private UNIX socket the broker
  owns. When the broker is down they fail with a start hint and never fall back. Only downloads
  (`ollama pull`) skip it. Clients speak the OpenAI-style `/v1`; Ollama's native run endpoints
  are refused ([ADR 0005](docs/adr/0005-llama-server.md)).
- **A swap keeps the conversation.** Stopping llama-server (a swap, a release, a model change,
  the idle timeout, `systemctl restart den`) saves its one slot's cache to
  `~/.cache/den/slots/` (mode 700); starting it restores that model's file, so the next turn reads
  only what's new. `den.service` uses `KillMode=mixed` so the broker can save before the server
  stops.
- **Availability, not modes.** A request runs when its own side can: an LLM model is selected
  (`den model`), or a workflow's model files are there. An unavailable side answers with what
  is missing, never with silence. `llm`, `image` and `both` are gone; an old `state.json`
  reads them as `on` ([ADR 0002](docs/adr/0002-gpu-broker.md)).
- **A side isn't loaded onto a machine busy with work that isn't den's own.** Above `[limits]`
  `max_load_per_cpu` (1-minute load average) or below `min_free_ram_gb` (`MemAvailable`), a
  request whose side would have to load waits `busy_wait` (60 s) for the machine to settle and is
  then refused with the numbers and the limit — but only while den holds nothing. Busy is usually
  a build or a test run and ends, and everything else in the broker waits rather than failing;
  the bound is there because the caller may be what's keeping the machine busy (`busy_wait = 0`
  refuses at once). A release and `den mode off` still refuse outright. A side that is loaded keeps serving, and a swap to the other side
  goes ahead and frees it first, so den never gates out its own load: keying the gate on the
  requested side instead deadlocked it against its own ComfyUI until the idle timeout. The tools
  stay listed and the call fails, rather than the tool list flapping with the load
  ([ADR 0004](docs/adr/0004-releasing-the-machine.md)).
- **The mode is only a kill switch,** `on | off`, enforced by the broker. `den mode off` goes
  through the broker: it refuses new requests, waits for in-flight ones (`--now` cancels them),
  unloads both sides (llama-server by saving its cache and stopping it; ComfyUI by stopping its
  unit), and only then saves the mode.
- **`den unload` is the release, without a mode switch:** the same draining and unloading, and
  it refuses requests for the sides it frees *while it runs* — a release is an explicit "I need
  this machine now", and a request that only waited would load a side again right behind it.
  The refusal ends with the release, so the next request loads its side again; that is the
  difference from `den mode off`. It frees the RAM and CPU the models hold, not only the GPU.
  Claude asks for the same thing through the `release_resources` tool before it starts
  something heavy ([ADR 0004](docs/adr/0004-releasing-the-machine.md)). Only one side holds the
  GPU at a time, swapped on demand and batched by the caps in `[broker]`
  ([ADR 0003](docs/adr/0003-image-generation.md)).
- **Only a human can start Ollama:** it's a system service, den runs as the user and never uses
  sudo. den needs it only to look a model up and to download one, but without it no model can
  start: the broker logs `OLLAMA DOWN`, callers get the same line with `sudo systemctl start
  ollama`, and `den status` exits non-zero.
- **Workflows, not model names:** a workflow is available when its graph's model files are in
  ComfyUI's models folder. Add one as `<name>.json` (API format) plus `[image.workflows.<name>]`
  (or `[clip.workflows.<name>]`): in `workflows/` and `config.toml` when it's a shareable example
  on public models, in `~/.config/den/workflows/` and `config.local.toml` when it isn't. An
  example a private workflow replaces is hidden with `enabled = false` in `config.local.toml`.
- **The private workflows are the ones in use; the tracked ones are examples.** Every improvement
  to a workflow (a mapping key such as `sound` or `lip_sync`, new graph nodes, a fix, a better
  default) lands in the private workflow that uses that model first, in
  `~/.config/den/workflows/` and `config.local.toml`, and is tested there. The tracked example is
  then brought along, so the repo shows the feature on public files, but it's never the only
  place a feature exists. After a change, compare the private graph and entry with their example:
  the same nodes and mapping keys, differing only in model files, cfg and notes. An example that
  a private workflow replaces stays `enabled = false` locally, so clients see one of the two. A
  model that can edit gets an edit variant too (`<name>-edit.json`,
  `[image.workflows.<name>.edit]`). Descriptions say style and prompt format only. An edit takes
  `strength` too: it blends the result back over its input, because a klein edit re-renders the
  whole frame and repaints colours the instruction never mentioned.
- **A guide image can be an ordinary photo where den has the preprocessor.** `canny` is built
  into ComfyUI; the rest are `[image.preprocessors]` entries naming a model file, and a type is
  offered as drawn from a photo once that file is downloaded — otherwise it still takes a
  ready-made map. Prefer `pose` over `canny` for a person: canny carries the guide's clothing
  outline along with the pose. klein has no ControlNet in core ComfyUI, so there a pose is a
  reference: `pose:PATH` makes den draw the skeleton and pass it in the caller's order, and the
  prompt names it by number. `save_maps` keeps every map den draws beside the result, listed apart
  from `paths` ([ADR 0003](docs/adr/0003-image-generation.md)).
- **Saved poses live outside the repo, and no tool text lists them.** The pose library is in
  `~/.local/share/den/poses/` (`DEN_POSES`), with a copy of each source photo, so nothing of it
  is versioned. A model finds poses with `list_poses`; the image tool only says the library
  exists, because a tool whose text changes on every save makes pi reread its conversation. A
  model saves (a taken name needs `replace`); only people rename or delete: `den pose mv` / `rm`,
  or the phone's Delete (`POST /poses` with `remove`), never a tool ([ADR
  0003](docs/adr/0003-image-generation.md)).
- **An image result is paths, plus a small copy when asked for.** `POST /image` takes
  `preview`, and only Claude's MCP tool sets it: ComfyUI re-encodes the output as a small JPEG
  so that model can look at what it made. The saved file stays the full-size PNG, the CLI and
  pi don't ask for a copy, and an unsupported `/view?preview` falls back to the text result
  ([ADR 0004](docs/adr/0004-releasing-the-machine.md)).
- **A skill teaches strategy, a tool description the options.** Each skill in `skills/` holds
  its recipes and measured traps, one skill per kind of result (image, clip, voice), pointing
  to the others where they meet; the workflows, settings and ranges stay in the tool text,
  built from `config.toml`, so the skill never lists what changes when a model is added. A new
  lesson from a test goes into the skill's `references/lessons.md` with the test behind it.
- **Delegation is opt-in per task.** Only enabled tasks appear in the tool's `task` enum,
  and the tool description tells Claude to delegate nothing else. Add tasks in
  `config.toml`; toggle them with `den task <name> on|off`.
- **Privacy:** the `files` argument is read server-side, so file contents never enter
  Claude's context. The answer still does.
- **Delegation policy** ([ADR 0001](docs/adr/0001-delegation-policy.md)): the *when* rule
  lives in `~/.claude/CLAUDE.md`, the *how* in the `local_llm` tool description. Each MCP
  call is logged to `~/.local/state/den/delegations.jsonl` (`DEN_LOG` overrides it)
  with an id that Claude's verdict refers to.
- **Fail loudly:** backend down, task disabled or oversized input returns a clear error,
  never silent truncation.

## Checks

No test suite yet. Smoke test with `bin/den status`, `bin/den task`, `bin/den model` and
`bin/den unload` (it loads nothing). Test MCP by piping JSON-RPC into `bin/den-mcp`, with
`DEN_STATE` and `DEN_LOG` pointed at scratch files so the real state and delegation
log stay untouched. Test the broker as a second instance: a scratch copy of `config.toml`
with `[broker] base_url` on another port (e.g. 11436), `DEN_CONFIG` pointing at it, and
`bin/den serve` in the background. Ask before tests that load a model (see the shared
GPU note in memory).

On macOS the same commands apply, with launchd underneath: `launchctl print
gui/$UID/ai.den.broker` says whether the broker is loaded and running, its output is in
`~/.local/state/den/den.log` (ComfyUI's in `comfyui.log`), and `./setup.sh` is worth running
twice — the first version of the launchd branch left the broker neither stopped nor started
on the second run. `bin/den` and `bin/den-mcp` re-exec themselves on a 3.11+ interpreter, so
they work even where PATH's `python3` is older.

## Agent skills

`.agents/skills/` is **local only and git-ignored** (third-party, not part of the public repo),
vendored from [mattpocock/skills](https://github.com/mattpocock/skills) @ `959a8e9`:
`grilling`, `domain-modeling`, `grill-with-docs` (user-invoked) and `handoff` (user-invoked).
`.claude/skills` is a symlink to it. Edit them there, not through the symlink. Local change:
`handoff` saves to `.branch-artifacts/` (see below) instead of the OS temp directory. A fresh
clone has none of them; install them from upstream if wanted.

## Git & PR conventions

- **Commit style:** `type(Scope): <gitmoji> <subject>`, with the gitmoji matching the
  conventional-commit type: `feat ✨`, `fix 🐛`, `refactor 🔨`, `docs 📝`, `test ✅`,
  `chore 🔧`. Split work into logical commits by concern, order them so each one works on
  its own, and stage paths explicitly (never `git add -A` on a mixed tree). Try to keep
  the subject line under 72 chars.
- **Commit subjects are lower case throughout**, e.g. `feat(MCP): ✨ add the generate_image
  tool`. Not Title Case, and the first word isn't capitalised either. PR _titles_ are
  Title Case; commit subjects are not.
- **Read the index before committing.** `git add <paths>` only adds to the index; it never
  clears it. A `git rm` or `git mv` from earlier is already staged and the next commit takes
  it silently. Check with `git diff --cached --stat` first. If the index holds more than the
  commit at hand, `git restore --staged <path>` the extras. If it already landed and isn't
  pushed, `git reset --mixed HEAD~1` and re-split.
- **AI-authored commits** end with a `Co-Authored-By` trailer naming the assistant and
  model, e.g. `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
- **Commits are signed where signing is set up.** On the Linux box `commit.gpgsign=true`, so
  a plain `git commit` signs; other machines may have neither the config nor gpg, and there
  commits come out unsigned. Verify with `git log --format='%h %G? %s'` (`G` = good, `N` =
  unsigned; `N` with `error: cannot run gpg` means this machine can't check, not that the
  commit is unsigned). If signing is configured and fails, or a machine can't sign at all,
  **stop and ask**: no `--no-gpg-sign`, no config changes, no "re-sign later".
- **Don't push unless asked.** Once a branch has an open PR, merge `main` into it; never
  rebase or force-push it.
- **PR title:** `Domain / Title Case Description`, e.g. `MCP / Add Image Generation Tool`.
  No emoji markers.
- **Don't create GitHub issues unprompted.**

## Working artifacts

- Per-branch working files (handoffs, diff viewers, reports, probes, agent notes) go under
  the git-ignored **`.branch-artifacts/<branch>/`**, never the repo root, the workspace tree
  or OS temp. Resolve `<branch>` each time from `git rev-parse --abbrev-ref HEAD`, with any
  `/` replaced by `-` (e.g. `image/comfyui` → `.branch-artifacts/image-comfyui/`).
- Organise by type inside that folder: **`handoffs/`** for handoff docs, **`diffs/`** for
  diff-viewer HTML (`NN-slug.html`), and other folders (`reports/`, `probes/`, …) as needed.

## Keeping this file current

- When you learn a durable convention, or correct a mistake recorded here, **propose an
  update to this file, but confirm with the user before editing it.**
