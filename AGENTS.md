# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, …) working in this repo.

## What this is

**den** is the HQ for a **local AI toolchain**. Claude stays the main coding agent;
it delegates selected cheap, bulk or private tasks to a local LLM. Image generation
(ComfyUI) is being built on top of the GPU broker.

- **Runtime:** Ollama serves the LLM. Main model: `qwen3.6:35b-a3b` (MoE, ~23 GB,
  split between GPU and RAM).
- **Integration:** a stdlib-only MCP server exposes one tool, `local_llm`, to Claude Code.
- **GPU broker:** `den serve` (user unit `den.service`, `127.0.0.1:11435`) sits in front
  of Ollama. The CLI, the MCP server and pi all go through it
  ([ADR 0002](docs/adr/0002-gpu-broker.md)).

## This repo is public

Everything committed is published at https://github.com/amin-bf/den-ai. Be discreet:

- **No personal or host details:** no hardware specs, OS, home paths, usernames, emails, IPs
  or hostnames beyond `127.0.0.1`. Write `~/…` and describe hardware generically ("a 12 GB GPU").
- **No private material:** nothing from work repos (names, code, PR numbers), exported
  chats or mail, credentials, or delegation logs. Test results describe inputs generically.
- **No content notes about what image models will or won't generate,** in code, config, tool
  descriptions or docs. Describe models by style and prompt format only.
- **Nothing machine-specific in code, config or scripts:** no absolute paths. Derive the repo
  location from the script or module (`dirname "${BASH_SOURCE[0]}"`, `Path(__file__)`), and user
  paths from `$HOME` / `XDG_*` (`%h` in systemd units), with an environment override where a
  user might want another location. Anyone who clones the repo must be able to run it as is.
- **Working notes stay in git-ignored places** (`.branch-artifacts/`), which may be as
  detailed as needed. Check `git diff --cached` for anything above before each commit.

## Layout

| Path | Role |
|---|---|
| `config.toml` | Hand-edited: broker address, Ollama endpoint and shared model settings (no model names), image section (empty), delegated tasks with system prompts. |
| `state.json` | Written by the broker (mode) and the CLI, not versioned: current mode, active model, task on/off overrides. |
| `den/core.py` | Config and state loading, Ollama and broker clients, `run_task` (with a guard against overflowing the context window). |
| `den/broker.py` | `den serve`: streaming pass-through to Ollama (`/api`, `/v1`), in-flight tracking, `/status`, `/mode` (waits, `now` cancels). |
| `den/cli.py` | `den status / mode / model / task / ask / log / serve`. |
| `systemd/den.service` | The broker's user unit (linked with `systemctl --user link`). |
| `setup.sh` | Idempotent setup: den (PATH link, service, MCP), pi and ComfyUI (clone, venv, unit). Never overwrites config, no sudo. |
| `CONTEXT.md` | Glossary: broker, side, mode, swap, caller, task, delegation. |
| `den/mcp_server.py` | MCP stdio server: `local_llm` and `local_llm_feedback`; sends `tools/list_changed` when config or state changes. |
| `docs/adr/` | Decisions and their reasons. Read the relevant one before changing an area. |
| `bin/den`, `bin/den-mcp` | Entry points (they add the repo root to `sys.path`). |
| `.agents/skills/` | Agent-agnostic skills, local only (git-ignored); `.claude/skills` is a symlink to it. |

## Design rules

- **Python stdlib only.** No pip installs: `tomllib`, `urllib` and `json` are enough.
- **Switch without restarts.** The broker, CLI and MCP server re-read config and state on
  every use. Code changes to the broker need `systemctl --user restart den`.
- **Everything that uses the GPU goes through the broker.** Clients never call Ollama
  (11434) directly. When the broker is down they fail with a start hint and never fall back.
  Only downloads (`ollama pull`) skip it.
- **Modes:** `llm | image | both | off`, enforced by the broker. A mode switch goes through
  the broker: it refuses new requests for the side being turned off, waits for in-flight
  ones (`--now` cancels them), unloads via `keep_alive: 0` and waits until `/api/ps` is
  empty, and only then saves the mode. Only one heavy model holds the GPU at a time;
  `both` will mean "both available, one loaded, swapped on demand" (image side: step 3).
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

No test suite yet. Smoke test with `bin/den status`, `bin/den task` and
`bin/den model`. Test MCP by piping JSON-RPC into `bin/den-mcp`, with
`DEN_STATE` and `DEN_LOG` pointed at scratch files so the real state and delegation
log stay untouched. Test the broker as a second instance: a scratch copy of `config.toml`
with `[broker] base_url` on another port (e.g. 11436), `DEN_CONFIG` pointing at it, and
`bin/den serve` in the background. Ask before tests that load a model (see the shared
GPU note in memory).

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
- **Commits are signed** (`commit.gpgsign=true` on this machine), so a plain `git commit`
  signs. Verify with `git log --format='%h %G? %s'` (`G` = good, `N` = unsigned). If signing
  fails, **stop and ask**: no `--no-gpg-sign`, no config changes, no "re-sign later".
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
