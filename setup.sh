#!/usr/bin/env bash
# Set up den on this machine. Safe to re-run: it installs what's missing and never overwrites
# existing config. It doesn't use sudo; system-level steps (Ollama, GPU drivers) are printed
# for you to run.
#
#   ./setup.sh                     den, pi and ComfyUI
#   ./setup.sh --no-pi             skip pi
#   ./setup.sh --no-comfyui        skip ComfyUI
#   ./setup.sh --no-preprocessors  skip downloading guide-map models (pose, …)
#
# Environment overrides:
#   COMFYUI_DIR    where ComfyUI lives               (default: ~/ComfyUI)
#   COMFYUI_REF    ComfyUI tag or branch to clone    (default: v0.36.0)
#   COMFYUI_PY     Python version for its venv       (default: 3.13)
#   TORCH_INDEX    PyTorch wheel index for your GPU  (default: CUDA 13.0)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
COMFYUI_DIR="${COMFYUI_DIR:-$HOME/ComfyUI}"
COMFYUI_REF="${COMFYUI_REF:-v0.36.0}"
COMFYUI_PY="${COMFYUI_PY:-3.13}"
TORCH_INDEX="${TORCH_INDEX:-https://download.pytorch.org/whl/cu130}"
BROKER_URL="http://127.0.0.1:11435"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

with_pi=1
with_comfyui=1
with_preprocessors=1
for arg in "$@"; do
  case "$arg" in
    --no-pi) with_pi=0 ;;
    --no-comfyui) with_comfyui=0 ;;
    --no-preprocessors) with_preprocessors=0 ;;
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
todos=0

step "Requirements"
have python3 || die "python3 is missing"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "python3 must be 3.11 or newer (tomllib)"
ok "python3 $(python3 -c 'import platform; print(platform.python_version())')"
have systemctl || die "systemd is required (den runs as a user service)"
ok "systemd"
if have ollama; then
  ok "ollama $(ollama -v 2>/dev/null | awk '{print $NF}')"
else
  todo "install Ollama (https://ollama.com/download or your distro's package), then: sudo systemctl enable --now ollama"
fi
if have ollama && ! systemctl show ollama -p Environment 2>/dev/null | grep -q 'OLLAMA_CONTEXT_LENGTH=32768'; then
  todo "set OLLAMA_CONTEXT_LENGTH=32768 on the Ollama service (sudo systemctl edit ollama, add
          [Service] / Environment=\"OLLAMA_CONTEXT_LENGTH=32768\", then sudo systemctl restart ollama)"
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

if [ "$(readlink -f "$UNIT_DIR/den.service" 2>/dev/null)" != "$REPO/systemd/den.service" ]; then
  systemctl --user link "$REPO/systemd/den.service" >/dev/null
  did "linked den.service"
fi
systemctl --user enable --now den >/dev/null 2>&1
sleep 1
if python3 -c "import urllib.request; urllib.request.urlopen('$BROKER_URL/status', timeout=5)" 2>/dev/null; then
  ok "broker running at $BROKER_URL"
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
    ollama list 2>/dev/null | awk 'NR > 1 && $1 !~ /cloud/ {print $1}' | python3 -c '
import json, sys
url, path = sys.argv[1], sys.argv[2]
models = [
    {"id": name, "input": ["text"], "reasoning": True, "contextWindow": 32768, "maxTokens": 8192}
    for name in sys.stdin.read().split()
]
provider = {"api": "openai-completions", "apiKey": "ollama", "baseUrl": url + "/v1", "models": models}
open(path, "w").write(json.dumps({"providers": {"ollama": provider}}, indent=2) + "\n")
' "$BROKER_URL" "$models"
    did "wrote $models (Ollama's downloaded models, through the broker)"
    [ -s "$models" ] && grep -q '"id"' "$models" || todo "no Ollama models yet: after 'den model <name>', delete $models and re-run"
  fi
fi

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
    uv pip install --quiet --python "$COMFYUI_DIR/.venv/bin/python" torch torchvision torchaudio --index-url "$TORCH_INDEX"
    uv pip install --quiet --python "$COMFYUI_DIR/.venv/bin/python" -r "$COMFYUI_DIR/requirements.txt"
    did "created the venv (Python $COMFYUI_PY, PyTorch from $TORCH_INDEX) and installed requirements"
  fi

  unit="$UNIT_DIR/comfyui.service"
  if [ -f "$unit" ]; then
    ok "$unit"
  else
    cat >"$unit" <<EOF
[Unit]
Description=ComfyUI (localhost)

[Service]
WorkingDirectory=$COMFYUI_DIR
ExecStart=$COMFYUI_DIR/.venv/bin/python main.py --listen 127.0.0.1 --port 8188
Restart=no

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    did "wrote comfyui.service (not enabled: the broker starts and stops it)"
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
    done < <(python3 - "$REPO/config.toml" <<'PY'
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

step "Summary"
if [ "$todos" = 0 ]; then
  echo "  All set. Try: den status"
else
  echo "  $todos item(s) marked TODO above need you."
fi
