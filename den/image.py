"""The image side: workflows, the ComfyUI client, output files and the image log.

A workflow is a ComfyUI graph in API format, `workflows/<name>.json`, plus its
`[image.workflows.<name>]` entry in config.toml: a description for whoever picks it and the
node inputs that receive the prompt, seed and size. A workflow whose model can edit also has
an edit variant, `workflows/<name>-edit.json` with `[image.workflows.<name>.edit]`, used when a
request brings an input image. A workflow is available when every model file its graphs name
is in ComfyUI's models folder. Only the broker talks to ComfyUI; clients ask the broker
(POST /image).
"""

import fcntl
import json
import os
import random
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from den import core
from den.core import DenError

WORKFLOWS_DIR = core.ROOT / "workflows"
COMFYUI_DIR = Path(os.environ.get("COMFYUI_DIR") or Path.home() / "ComfyUI")
OUTPUT_DIR = Path(os.environ.get("DEN_IMAGES") or Path.home() / "Pictures/den")
LOG_PATH = Path(os.environ.get("DEN_IMAGE_LOG", core._STATE_HOME / "den/images.jsonl"))
MODEL_SUFFIXES = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".sft")
POLL_S = 0.5


def settings(config):
    return config.get("image", {})


def workflows(config):
    return settings(config).get("workflows", {})


def load_graph(name):
    path = WORKFLOWS_DIR / f"{name}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise DenError(f"cannot read workflow {path}: {e}") from e


def model_files(graph):
    """The model file names a graph's loader nodes ask for."""
    return sorted(
        {
            value
            for node in graph.values()
            for value in node.get("inputs", {}).values()
            if isinstance(value, str) and value.lower().endswith(MODEL_SUFFIXES)
        }
    )


def installed_models():
    """Paths under ComfyUI's models folder, relative to it (e.g. vae/ae.safetensors)."""
    root = COMFYUI_DIR / "models"
    found = set()
    for dirpath, _, filenames in os.walk(root, followlinks=True):
        rel = Path(dirpath).relative_to(root)
        found.update((rel / f).as_posix() for f in filenames if f.lower().endswith(MODEL_SUFFIXES))
    return found


def variant(name, wf, edit):
    """(graph name, input mapping) of a workflow, or of its edit variant."""
    if not edit:
        return name, wf
    if "edit" not in wf:
        raise DenError(f"workflow {name} can't take an input image (its model doesn't edit)")
    return f"{name}-edit", wf["edit"]


def describe(wf):
    """A workflow's description, with its private note from config.local.toml if any."""
    return " ".join(filter(None, (wf.get("description", ""), wf.get("note", ""))))


def check_workflows(config):
    """{name: (workflow config, problem or None)}: a problem is a missing graph or model file."""
    installed = installed_models()
    result = {}
    for name, wf in workflows(config).items():
        try:
            graphs = [load_graph(variant(name, wf, edit)[0]) for edit in ((False, True) if "edit" in wf else (False,))]
        except DenError as e:
            result[name] = (wf, str(e))
            continue
        # Loaders name files relative to their category folder (vae/, checkpoints/, …).
        wanted = sorted({f for graph in graphs for f in model_files(graph)})
        missing = [f for f in wanted if not any(p.endswith("/" + f) for p in installed)]
        result[name] = (wf, f"missing model files in {COMFYUI_DIR / 'models'}: {', '.join(missing)}" if missing else None)
    return result


def available(config):
    """{name: workflow config} for the workflows that can run on this machine."""
    return {name: wf for name, (wf, problem) in check_workflows(config).items() if problem is None}


def _refs(value):
    return [value] if isinstance(value, str) else list(value or [])


def _set(graph, ref, value):
    node, _, field = ref.partition(".")
    if field not in graph.get(node, {}).get("inputs", {}):
        raise DenError(f"workflow mapping {ref!r} doesn't match a node input in the graph")
    graph[node]["inputs"][field] = value


def parse_size(size):
    match = re.fullmatch(r"(\d+)[x×](\d+)", str(size).strip())
    if not match:
        raise DenError(f"size must look like 1024x1024, got {size!r}")
    return int(match[1]), int(match[2])


def build(config, name, prompt, negative=None, seed=None, size=None, edit=False):
    """The filled-in graph and the parameters used. Raises DenError on bad input.

    With edit, it's the edit variant's graph; the input image is set later (fill_image),
    after it's uploaded to ComfyUI.
    """
    flows = check_workflows(config)
    if name not in flows:
        raise DenError(f"unknown workflow {name!r}; configured: {', '.join(flows) or 'none'}")
    wf, problem = flows[name]
    if problem:
        raise DenError(f"workflow {name} can't run: {problem}")
    if not prompt or not prompt.strip():
        raise DenError("the prompt is empty")
    graph_name, wf = variant(name, wf, edit)
    graph = load_graph(graph_name)
    _set(graph, wf["prompt"], prompt)
    if negative is not None:
        if "negative" not in wf:
            raise DenError(f"workflow {name} takes no negative prompt")
        _set(graph, wf["negative"], negative)
    seed = random.randrange(2**32) if seed is None else int(seed)
    for ref in _refs(wf.get("seed")):
        _set(graph, ref, seed)
    size_nodes = _refs(wf.get("size"))
    if size is not None:
        if not size_nodes:
            raise DenError(
                f"workflow {name} takes its size from the input image" if edit else f"workflow {name} has a fixed size"
            )
        width, height = parse_size(size)
        for node in size_nodes:
            _set(graph, f"{node}.width", width)
            _set(graph, f"{node}.height", height)
    elif size_nodes:
        inputs = graph[size_nodes[0]]["inputs"]
        width, height = inputs["width"], inputs["height"]
    else:
        width = height = None
    params = {"workflow": name, "edit": edit, "seed": seed, "width": width, "height": height}
    return graph, params


def fill_image(config, name, graph, uploaded):
    _set(graph, workflows(config)[name]["edit"]["image"], uploaded)


class ComfyUI:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def _open(self, method, path, data=None, headers=None, timeout=30):
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers or {})
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise DenError(f"comfyui {path}: HTTP {e.code}: {_comfy_error(e.read())}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise DenError(f"comfyui is not reachable at {self.base_url} ({getattr(e, 'reason', e)})") from e

    def _json(self, method, path, body=None, timeout=30):
        data = None if body is None else json.dumps(body).encode()
        with self._open(method, path, data, {"Content-Type": "application/json"}, timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw.strip() else {}

    def up(self):
        try:
            self._json("GET", "/system_stats", timeout=3)
            return True
        except (DenError, json.JSONDecodeError):
            return False

    def busy(self):
        """Whether ComfyUI runs or queues anything, e.g. from its web UI."""
        queue = self._json("GET", "/queue", timeout=5)
        return bool(queue.get("queue_running") or queue.get("queue_pending"))

    def upload(self, path):
        """Upload an input image; returns the name a LoadImage node takes."""
        path = Path(path).expanduser()
        try:
            content = path.read_bytes()
        except OSError as e:
            raise DenError(f"cannot read {path}: {e}") from e
        boundary = uuid.uuid4().hex
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="subfolder"\r\n\r\nden\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{path.name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n".encode()
            + content
            + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        with self._open("POST", "/upload/image", b"".join(parts), headers, timeout=60) as resp:
            info = json.loads(resp.read())
        return f"{info['subfolder']}/{info['name']}" if info.get("subfolder") else info["name"]

    def submit(self, graph):
        return self._json("POST", "/prompt", {"prompt": graph, "client_id": "den"})["prompt_id"]

    def wait(self, prompt_id, check):
        """Poll until the prompt is done; returns its output images. check() may raise to stop."""
        while True:
            check()
            entry = self._json("GET", f"/history/{prompt_id}").get(prompt_id)
            status = (entry or {}).get("status", {})
            if status.get("status_str") == "error":
                raise DenError(f"comfyui failed: {_execution_error(status)}")
            if entry and status.get("completed", True):
                images = [img for out in entry.get("outputs", {}).values() for img in out.get("images", [])]
                saved = [img for img in images if img.get("type") == "output"]
                if not (saved or images):
                    raise DenError("comfyui finished without an output image")
                return saved or images
            time.sleep(POLL_S)

    def view(self, img):
        query = urllib.parse.urlencode({k: img.get(k, "") for k in ("filename", "subfolder", "type")})
        with self._open("GET", f"/view?{query}", timeout=60) as resp:
            return resp.read()

    def cancel(self, prompt_id):
        """Drop the prompt from ComfyUI's queue, or interrupt it when it's running."""
        self._json("POST", "/queue", {"delete": [prompt_id]})
        self._json("POST", "/interrupt", {"prompt_id": prompt_id})


def _comfy_error(raw):
    """A readable message from a /prompt validation error body, else the raw body."""
    text = raw.decode(errors="replace").strip()
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return text
    lines = []
    if isinstance(body.get("error"), dict):
        lines.append(body["error"].get("message", ""))
    for node_id, node in (body.get("node_errors") or {}).items():
        for err in node.get("errors", []):
            lines.append(f"node {node_id} ({node.get('class_type')}): {err.get('message')} {err.get('details', '')}".strip())
    return "; ".join(filter(None, lines)) or text


def _execution_error(status):
    for event, data in status.get("messages", []):
        if event == "execution_error":
            return f"{data.get('node_type')}: {data.get('exception_message', '').strip()}"
    return "unknown error (see: journalctl --user -u comfyui)"


def slug(prompt, length=40):
    words = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")
    return words[:length].rstrip("-") or "image"


def save(images, prompt, out=None):
    """Write the images to OUTPUT_DIR/YYYY-MM-DD/<time>-<slug>.png; copy them to out if given."""
    day = OUTPUT_DIR / time.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    stem = f"{time.strftime('%H%M%S')}-{slug(prompt)}"
    paths = []
    for i, data in enumerate(images):
        n, path = i, None
        while path is None or path.exists():
            path = day / f"{stem}{f'-{n + 1}' if n else ''}.png"
            n += 1
        path.write_bytes(data)
        paths.append(path)
    copies = []
    if out:
        # Check the trailing slash before Path() drops it: out/ is a folder, even a new one.
        into_dir = str(out).endswith("/")
        out = Path(out).expanduser()
        for i, path in enumerate(paths):
            if into_dir or out.is_dir():
                target = out / path.name
            elif len(paths) > 1:
                target = out.with_name(f"{out.stem}-{i + 1}{out.suffix}")
            else:
                target = out
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            except OSError as e:
                raise DenError(f"saved {path} but cannot copy it to {target}: {e}") from e
            copies.append(target)
    return paths, copies


def log(entry):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps({"ts": int(time.time()), **entry}) + "\n")
