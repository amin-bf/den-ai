#!/usr/bin/env bash
# Set up den on this machine. Safe to re-run: it installs what's missing and never overwrites
# existing config. It doesn't use sudo; system-level steps (Ollama, GPU drivers) are printed
# for you to run.
#
#   ./setup.sh                     den, pi and ComfyUI
#   ./setup.sh --no-pi             skip pi
#   ./setup.sh --no-comfyui        skip ComfyUI
#   ./setup.sh --no-speech         skip the speech model (voice-overs)
#   ./setup.sh --no-preprocessors  skip downloading guide-map models (pose, …)
#
# Environment overrides:
#   COMFYUI_DIR    where ComfyUI lives               (default: ~/ComfyUI)
#   COMFYUI_REF    ComfyUI tag or branch to clone    (default: v0.36.0)
#   COMFYUI_PY     Python version for its venv       (default: 3.13)
#   COMFYUI_GGUF_REF  ComfyUI-GGUF commit to check out (default: 6ea2651e)
#   SPEECH_DIR     where the speech model's venv lives (default: ~/.local/share/den/speech)
#   CHATTERBOX_REF Chatterbox commit to install (default: 5de7a54a)
#   TORCH_INDEX    PyTorch wheel index for your GPU  (default: CUDA 13.0; none on macOS,
#                                                     where the wheels carry Metal)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
COMFYUI_DIR="${COMFYUI_DIR:-$HOME/ComfyUI}"
COMFYUI_REF="${COMFYUI_REF:-v0.36.0}"
COMFYUI_PY="${COMFYUI_PY:-3.13}"
COMFYUI_GGUF_REF="${COMFYUI_GGUF_REF:-6ea2651e}"
SPEECH_DIR="${SPEECH_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/den/speech}"
CHATTERBOX_REF="${CHATTERBOX_REF:-5de7a54aa4e5e2baadb0182dde554908b48b85c2}"
BROKER_URL="http://127.0.0.1:11435"

# macOS runs the same den, with launchd where Linux has systemd and Metal where it has CUDA
# (docs/adr/0006-running-on-macos.md).
case "$(uname -s)" in
  Darwin) MACOS=1 ;;
  *) MACOS=0 ;;
esac
if [ "$MACOS" = 1 ]; then
  # PyPI's own torch wheels carry Metal; there is no separate index to pick.
  TORCH_INDEX="${TORCH_INDEX:-}"
  SPEECH_TORCH_INDEX="${SPEECH_TORCH_INDEX:-}"
  UNIT_DIR="$HOME/Library/LaunchAgents"
else
  TORCH_INDEX="${TORCH_INDEX:-https://download.pytorch.org/whl/cu130}"
  # Chatterbox pins torch 2.6.0, whose newest CUDA wheels are 12.6.
  SPEECH_TORCH_INDEX="${SPEECH_TORCH_INDEX:-https://download.pytorch.org/whl/cu126}"
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
fi
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/den"

with_pi=1
with_comfyui=1
with_preprocessors=1
with_speech=1
for arg in "$@"; do
  case "$arg" in
    --no-pi) with_pi=0 ;;
    --no-comfyui) with_comfyui=0 ;;
    --no-preprocessors) with_preprocessors=0 ;;
    --no-speech) with_speech=0 ;;
    -h | --help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok() { printf '  ok    %s\n' "$*"; }
did() { printf '  done  %s\n' "$*"; }
todo() { printf '  TODO  %s\n' "$*"; todos=$((todos + 1)); }
die() { printf '  FAIL  %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
# launchd expands nothing of its own, so a plist template's @PLACEHOLDERS@ are filled here:
# that keeps absolute paths out of the repo and the agent working wherever den is cloned.
fill_plist() { # template target log
  sed -e "s|@PYTHON@|$PYTHON|g" -e "s|@REPO@|$REPO|g" -e "s|@PATH@|${agent_path:-}|g" \
      -e "s|@COMFYUI_DIR@|$COMFYUI_DIR|g" -e "s|@LOG@|$3|g" "$1" >"$2"
}
todos=0

step "Requirements"
# den needs tomllib, so 3.11 or newer. The `python3` on PATH is the one the shebangs pick,
# but macOS ships an old one ahead of Homebrew's, so look for a newer one by name and use
# that for the service; the PATH itself is the user's to fix.
PYTHON=""
for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
  if have "$candidate" && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PYTHON="$(command -v "$candidate")"
    break
  fi
done
[ -n "$PYTHON" ] || die "python3 must be 3.11 or newer (tomllib)$([ "$MACOS" = 1 ] && echo '; try: brew install python@3.13')"
ok "python3 $("$PYTHON" -c 'import platform; print(platform.python_version())') ($PYTHON)"
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
  # bin/den and bin/den-mcp hand over to a newer interpreter themselves, so this is a note,
  # not something to fix.
  ok "the python3 on your PATH is older; den hands over to $PYTHON itself"
fi

if [ "$MACOS" = 1 ]; then
  have launchctl || die "launchctl is missing (den runs as a launchd agent)"
  ok "launchd"
  # macOS wires about 75% of unified memory to the GPU. Where the model, its cache, a vision
  # projector and a draft context all come out of that one pool, that default is the ceiling
  # a model hits, and it fails as an opaque Metal allocation error. The sysctl doesn't
  # survive a reboot either, so a model that loaded yesterday fails today. Leave the machine
  # ~4 GB and set it at boot; den never uses sudo, so the commands are printed (ADR 0006).
  mkdir -p "$STATE_DIR"
  wired_want=$(( $(sysctl -n hw.memsize) / 1048576 - 4096 ))
  wired_have=$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo 0)
  wired_plist="$STATE_DIR/ai.den.wiredlimit.plist"
  sed "s|@WIRED_MB@|$wired_want|g" "$REPO/launchd/ai.den.wiredlimit.plist" >"$wired_plist"
  if [ "${wired_have:-0}" -ge "$wired_want" ]; then
    ok "GPU wired limit ${wired_have} MB"
  else
    todo "Metal may wire only ~75% of this machine's memory to the GPU, which a large model
          plus its cache can exceed — and the setting is lost on reboot. To raise it for good:
            sudo install -o root -g wheel -m 644 $wired_plist /Library/LaunchDaemons/ai.den.wiredlimit.plist
            sudo launchctl bootstrap system /Library/LaunchDaemons/ai.den.wiredlimit.plist"
  fi
else
  have systemctl || die "systemd is required (den runs as a user service)"
  ok "systemd"
fi

if have ollama; then
  ok "ollama $(ollama -v 2>/dev/null | awk '{print $NF}')"
elif [ "$MACOS" = 1 ]; then
  todo "install Ollama (brew install ollama, or https://ollama.com/download), then: brew services start ollama"
else
  todo "install Ollama (https://ollama.com/download or your distro's package), then: sudo systemctl enable --now ollama"
fi

if have llama-server; then
  ok "llama-server $(llama-server --version 2>&1 | head -1)"
elif [ "$MACOS" = 1 ]; then
  todo "install llama-server, which runs den's LLM: brew install llama.cpp (its wheels carry Metal)"
else
  todo "install llama-server with a CUDA backend (it runs den's LLM), e.g.: sudo pacman -S llama-cpp ggml-cuda"
fi

step "den"
mkdir -p "$HOME/.local/bin" "$UNIT_DIR"
if [ "$(readlink -f "$HOME/.local/bin/den" 2>/dev/null)" = "$REPO/bin/den" ]; then
  ok "~/.local/bin/den"
elif [ -e "$HOME/.local/bin/den" ]; then
  todo "~/.local/bin/den exists and isn't a link to this repo; remove it and re-run"
else
  ln -s "$REPO/bin/den" "$HOME/.local/bin/den"
  did "linked ~/.local/bin/den"
fi
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) todo "add ~/.local/bin to your PATH" ;; esac

mkdir -p "$STATE_DIR"
if [ "$MACOS" = 1 ]; then
  # A launchd agent starts with a bare PATH and the broker has to find llama-server, so the
  # plist carries one built from where this machine keeps things.
  agent_path="$(dirname "$PYTHON")"
  have brew && agent_path="$agent_path:$(brew --prefix)/bin"
  have llama-server && agent_path="$agent_path:$(dirname "$(command -v llama-server)")"
  agent_path="$agent_path:/usr/bin:/bin:/usr/sbin:/sbin"
  fill_plist "$REPO/launchd/ai.den.broker.plist" "$UNIT_DIR/ai.den.broker.plist" "$STATE_DIR/den.log"
  # Boot it out first so a re-run loads the plist as it now reads, not as it was loaded.
  # bootout returns before launchd has finished, and bootstrapping a job still going away
  # fails and leaves nothing loaded, so wait for it to go.
  if launchctl print "gui/$(id -u)/ai.den.broker" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)/ai.den.broker" 2>/dev/null || true
    for _ in $(seq 1 60); do
      launchctl print "gui/$(id -u)/ai.den.broker" >/dev/null 2>&1 || break
      sleep 1
    done
  fi
  launchctl enable "gui/$(id -u)/ai.den.broker" 2>/dev/null || true
  if launchctl bootstrap "gui/$(id -u)" "$UNIT_DIR/ai.den.broker.plist" 2>/dev/null; then
    did "loaded the broker agent ai.den.broker"
  else
    todo "loading ai.den.broker failed; see: $STATE_DIR/den.log"
  fi
else
  if [ "$(readlink -f "$UNIT_DIR/den.service" 2>/dev/null)" != "$REPO/systemd/den.service" ]; then
    systemctl --user link "$REPO/systemd/den.service" >/dev/null
    did "linked den.service"
  fi
  systemctl --user enable --now den >/dev/null 2>&1
fi
sleep 1
if "$PYTHON" -c "import urllib.request; urllib.request.urlopen('$BROKER_URL/status', timeout=5)" 2>/dev/null; then
  ok "broker running at $BROKER_URL"
elif [ "$MACOS" = 1 ]; then
  todo "the broker didn't start; see: $STATE_DIR/den.log"
else
  todo "the broker didn't start; see: journalctl --user -u den -n 50"
fi

if have ollama && ! "$REPO/bin/den" status 2>/dev/null | grep -q '(pulled'; then
  todo "download a model and make it active, e.g.: den model qwen3.6:35b-a3b (~23 GB)"
fi

if have claude; then
  if claude mcp get den >/dev/null 2>&1; then
    ok "Claude Code MCP server 'den'"
  else
    claude mcp add --scope user den -- "$REPO/bin/den-mcp" >/dev/null
    did "registered the MCP server 'den' with Claude Code"
  fi
else
  todo "Claude Code not found; after installing it run: claude mcp add --scope user den -- $REPO/bin/den-mcp"
fi

if [ "$with_pi" = 1 ]; then
  step "pi"
  if have pi; then
    ok "pi $(pi --version 2>/dev/null | head -1)"
  elif have npm; then
    npm install -g @earendil-works/pi-coding-agent >/dev/null
    did "installed pi (npm install -g @earendil-works/pi-coding-agent)"
  else
    todo "install Node.js with npm, then re-run to install pi"
  fi

  pi_dir="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
  extension="$pi_dir/extensions/den.ts"
  if [ "$(readlink -f "$extension" 2>/dev/null)" = "$REPO/integrations/pi/den.ts" ]; then
    ok "pi extension $extension"
  elif [ -e "$extension" ]; then
    todo "$extension exists and isn't a link to this repo; remove it and re-run"
  else
    mkdir -p "$(dirname "$extension")"
    ln -s "$REPO/integrations/pi/den.ts" "$extension"
    did "linked the pi extension (generate_image, /imagine) into $pi_dir/extensions; /reload in running pi sessions"
  fi

  models="$pi_dir/models.json"
  if [ -f "$models" ]; then
    if grep -q "$BROKER_URL/v1" "$models"; then
      ok "$models points at the broker"
    else
      todo "set the ollama provider's baseUrl in $models to $BROKER_URL/v1 (the broker, not Ollama)"
    fi
  elif have ollama; then
    mkdir -p "$(dirname "$models")"
    ollama list 2>/dev/null | awk 'NR > 1 && $1 !~ /cloud/ {print $1}' | "$PYTHON" -c '
import json, sys, urllib.request
url, path = sys.argv[1], sys.argv[2]
def sees(name):
    # Vision needs the capability and a projector file next to the model: a model can list
    # vision and still ship without one, and then an image would fail.
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/show", json.dumps({"model": name}).encode())
        show = json.load(urllib.request.urlopen(req, timeout=10))
    except OSError:
        return False
    froms = [line for line in show.get("modelfile", "").splitlines() if line.startswith("FROM ")]
    return "vision" in show.get("capabilities", []) and len(froms) > 1
def model(name):
    entry = {"id": name, "input": ["text", "image"] if sees(name) else ["text"], "reasoning": True,
             "contextWindow": 65536, "maxTokens": 8192}
    if "qwen" in name.lower():
        # Qwen templates switch thinking with chat_template_kwargs.enable_thinking, which
        # llama-server passes on; without this pi sends reasoning_effort and thinking stays on.
        # The template takes no effort level, so the level itself only reaches the model as a
        # thinking-token budget: the second key names the request field llama-server reads for it.
        entry["compat"] = {"thinkingFormat": "qwen-chat-template",
                           "thinkingTokenBudgetField": "thinking_budget_tokens"}
    return entry
models = [model(name) for name in sys.stdin.read().split()]
provider = {"api": "openai-completions", "apiKey": "ollama", "baseUrl": url + "/v1", "models": models}
open(path, "w").write(json.dumps({"providers": {"ollama": provider}}, indent=2) + "\n")
' "$BROKER_URL" "$models"
    did "wrote $models (Ollama's downloaded models, through the broker)"
    [ -s "$models" ] && grep -q '"id"' "$models" || todo "no Ollama models yet: after 'den model <name>', delete $models and re-run"
  fi
fi

step "skills"
# den's own skills (skills/<name>/SKILL.md), linked where agents find global skills: pi reads
# ~/.agents/skills, Claude Code ~/.claude/skills.
for skill in "$REPO"/skills/*/; do
  skill="${skill%/}"
  name="$(basename "$skill")"
  for dir in "$HOME/.agents/skills" "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"; do
    target="$dir/$name"
    if [ "$(readlink -f "$target" 2>/dev/null)" = "$skill" ]; then
      ok "skill $name in $dir"
    elif [ -e "$target" ] || [ -L "$target" ]; then
      todo "$target exists and isn't a link to this repo; remove it and re-run"
    else
      mkdir -p "$dir"
      ln -s "$skill" "$target"
      did "linked the skill $name into $dir"
    fi
  done
done

if [ "$with_comfyui" = 1 ]; then
  step "ComfyUI"
  if [ -d "$COMFYUI_DIR/.git" ]; then
    ok "$COMFYUI_DIR ($(git -C "$COMFYUI_DIR" describe --tags 2>/dev/null || echo 'unknown version'))"
  else
    have git || die "git is required to install ComfyUI"
    have uv || die "uv is required to install ComfyUI (https://docs.astral.sh/uv/)"
    git -c advice.detachedHead=false clone --quiet --branch "$COMFYUI_REF" https://github.com/comfyanonymous/ComfyUI "$COMFYUI_DIR"
    did "cloned ComfyUI $COMFYUI_REF into $COMFYUI_DIR"
  fi
  if [ -x "$COMFYUI_DIR/.venv/bin/python" ]; then
    ok "venv $("$COMFYUI_DIR/.venv/bin/python" -c 'import platform; print(platform.python_version())')"
  else
    have uv || die "uv is required to install ComfyUI (https://docs.astral.sh/uv/)"
    uv venv --quiet --python "$COMFYUI_PY" "$COMFYUI_DIR/.venv"
    # On macOS the ordinary wheels carry Metal, so there is no index to choose.
    if [ -n "$TORCH_INDEX" ]; then
      uv pip install --quiet --python "$COMFYUI_DIR/.venv/bin/python" torch torchvision torchaudio --index-url "$TORCH_INDEX"
    else
      uv pip install --quiet --python "$COMFYUI_DIR/.venv/bin/python" torch torchvision torchaudio
    fi
    uv pip install --quiet --python "$COMFYUI_DIR/.venv/bin/python" -r "$COMFYUI_DIR/requirements.txt"
    did "created the venv (Python $COMFYUI_PY, PyTorch ${TORCH_INDEX:-with Metal}) and installed requirements"
  fi
  # ComfyUI-GGUF loads .gguf models and text encoders, for workflows too large for the GPU unquantized.
  gguf="$COMFYUI_DIR/custom_nodes/ComfyUI-GGUF"
  if [ -d "$gguf/.git" ]; then
    ok "ComfyUI-GGUF ($(git -C "$gguf" rev-parse --short HEAD))"
  else
    have git || die "git is required to install ComfyUI-GGUF"
    have uv || die "uv is required to install ComfyUI-GGUF (https://docs.astral.sh/uv/)"
    git clone --quiet https://github.com/city96/ComfyUI-GGUF "$gguf"
    git -C "$gguf" -c advice.detachedHead=false checkout --quiet "$COMFYUI_GGUF_REF"
    uv pip install --quiet --python "$COMFYUI_DIR/.venv/bin/python" -r "$gguf/requirements.txt"
    did "installed ComfyUI-GGUF $COMFYUI_GGUF_REF"
  fi

  if [ "$MACOS" = 1 ]; then
    unit="$UNIT_DIR/ai.den.comfyui.plist"
    if [ -f "$unit" ]; then
      ok "$unit"
    else
      fill_plist "$REPO/launchd/ai.den.comfyui.plist" "$unit" "$STATE_DIR/comfyui.log"
      did "wrote ai.den.comfyui.plist (not loaded: the broker loads and boots it out)"
    fi
  else
  unit="$UNIT_DIR/comfyui.service"
  # --reserve-vram keeps room on the GPU for what a quantized (GGUF) model unpacks mid-step; without
  # it a model that fills the GPU runs out of memory on its first step.
  if [ -f "$unit" ]; then
    ok "$unit"
  else
    cat >"$unit" <<EOF
[Unit]
Description=ComfyUI (localhost)

[Service]
WorkingDirectory=$COMFYUI_DIR
ExecStart=$COMFYUI_DIR/.venv/bin/python main.py --listen 127.0.0.1 --port 8188 --reserve-vram 1.5
Restart=no

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    did "wrote comfyui.service (not enabled: the broker starts and stops it)"
  fi
  fi
  if [ "$with_preprocessors" = 1 ]; then
    # A preprocessor lets a request guide an image with an ordinary photo instead of a map it
    # had to make elsewhere. config.toml lists each one with the file it needs and where that
    # comes from, so this covers any added later without a change here.
    while IFS="$(printf '\t')" read -r name file dir url; do
      [ -n "$file" ] || continue
      target="$COMFYUI_DIR/models/$dir/$file"
      if [ -f "$target" ]; then
        ok "$name preprocessor ($file)"
      elif [ -z "$url" ] || ! have curl; then
        todo "download $file into $COMFYUI_DIR/models/$dir, for guiding an image by $name"
      else
        mkdir -p "$COMFYUI_DIR/models/$dir"
        printf '  ...   downloading %s for the %s guide type\n' "$file" "$name"
        curl -fL --progress-bar -o "$target.part" "$url" || die "downloading $file failed"
        mv "$target.part" "$target"
        did "downloaded $file"
      fi
    done < <("$PYTHON" - "$REPO/config.toml" <<'PY'
import sys, tomllib

for name, pre in tomllib.load(open(sys.argv[1], "rb")).get("image", {}).get("preprocessors", {}).items():
    print("\t".join([name, pre.get("file", ""), pre.get("dir", "checkpoints"), pre.get("url", "")]))
PY
    )
  fi
  if ! find "$COMFYUI_DIR/models" -name '*.safetensors' -size +100M 2>/dev/null | grep -q .; then
    todo "download an image model: see 'Image models' in README.md"
  fi
fi

# Speech (voice-overs): Chatterbox in a venv of its own, since it pins a torch and transformers
# that ComfyUI's newer ones can't share. The broker starts speech/server.py in it for a voice
# job and stops it after (docs/adr/0010-voice-overs.md).
if [ "$with_speech" = 1 ]; then
  step "speech"
  py="$SPEECH_DIR/.venv/bin/python"
  if [ -x "$py" ] && "$py" -c "import chatterbox.mtl_tts" 2>/dev/null; then
    ok "Chatterbox in $SPEECH_DIR/.venv"
  else
    have uv || die "uv is required to install the speech model (https://docs.astral.sh/uv/)"
    mkdir -p "$SPEECH_DIR"
    [ -x "$py" ] || uv venv --quiet --python 3.12 "$SPEECH_DIR/.venv"
    if [ -n "$SPEECH_TORCH_INDEX" ]; then
      uv pip install --quiet --python "$py" torch==2.6.0 torchaudio==2.6.0 --index-url "$SPEECH_TORCH_INDEX"
    fi
    uv pip install --quiet --python "$py" "chatterbox-tts @ git+https://github.com/resemble-ai/chatterbox@$CHATTERBOX_REF"
    did "installed Chatterbox ${CHATTERBOX_REF:0:8} in $SPEECH_DIR/.venv (its model, about 3.2 GB, downloads on first use)"
  fi
fi

step "Summary"
if [ "$todos" = 0 ]; then
  echo "  All set. Try: den status"
else
  echo "  $todos item(s) marked TODO above need you."
fi
