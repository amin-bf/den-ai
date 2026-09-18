"""The image side: workflows, the ComfyUI client, output files and the image log.

A workflow is a ComfyUI graph in API format, `workflows/<name>.json`, plus its
`[image.workflows.<name>]` entry in config.toml: a description for whoever picks it and the
node inputs that receive the prompt, seed and size. A workflow whose model can edit also has
an edit variant, `workflows/<name>-edit.json` with `[image.workflows.<name>.edit]`, used when a
request brings an input image. A workflow is available when every model file its graphs name
is in ComfyUI's models folder. Only the broker talks to ComfyUI; clients ask the broker
(POST /image).
"""

import base64
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

# What ComfyUI accepts (comfy/samplers.py, v0.36). Each workflow recommends a few of them.
SAMPLERS = [
    "euler", "euler_cfg_pp", "euler_ancestral", "euler_ancestral_cfg_pp", "heun", "heunpp2", "exp_heun_2_x0",
    "exp_heun_2_x0_sde", "dpm_2", "dpm_2_ancestral", "lms", "dpm_fast", "dpm_adaptive", "dpmpp_2s_ancestral",
    "dpmpp_2s_ancestral_cfg_pp", "dpmpp_sde", "dpmpp_sde_gpu", "dpmpp_2m", "dpmpp_2m_cfg_pp", "dpmpp_2m_sde",
    "dpmpp_2m_sde_gpu", "dpmpp_2m_sde_heun", "dpmpp_2m_sde_heun_gpu", "dpmpp_3m_sde", "dpmpp_3m_sde_gpu", "ddpm",
    "lcm", "ipndm", "ipndm_v", "deis", "cfgpp_ud10_ab", "res_multistep", "res_multistep_cfg_pp",
    "res_multistep_ancestral", "res_multistep_ancestral_cfg_pp", "gradient_estimation", "gradient_estimation_cfg_pp",
    "er_sde", "seeds_2", "seeds_3", "sa_solver", "sa_solver_pece", "ddim", "uni_pc", "uni_pc_bh2",
]
SCHEDULERS = ["simple", "sgm_uniform", "karras", "exponential", "ddim_uniform", "beta", "normal", "linear_quadratic", "kl_optimal"]
SETTINGS = {"steps": int, "cfg": float, "sampler": SAMPLERS, "scheduler": SCHEDULERS}
# Guide image types per control kind; for SDXL union ControlNets, the union type each one maps to.
CONTROL_TYPES = {
    "controlnet": {
        "canny": "canny/lineart/anime_lineart/mlsd",
        "lineart": "canny/lineart/anime_lineart/mlsd",
        "scribble": "hed/pidi/scribble/ted",
        "pose": "openpose",
        "depth": "depth",
        "normal": "normal",
        "segment": "segment",
        "tile": "tile",
    },
    "zimage-fun": {"canny": None, "hed": None, "depth": None, "pose": None, "mlsd": None},
}

# For whoever writes prompts (the MCP tool, pi): what the text encoders do with prompt syntax.
PROMPT_SYNTAX = (
    "Prompts reach the model's text encoder as plain text: ComfyUI just encodes it as text. "
    "Midjourney-style flags such as --ar 9:16, --v 2 or --stylize set nothing and only add noise "
    "to the prompt; use size and the settings instead. ComfyUI parses weights like (word:1.3), "
    "but only workflows whose description says so respond to them reliably."
)


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


def unavailable(config, state):
    """Why an image request can't run now, or None."""
    if not core.is_on(state):
        return core.OFF_MESSAGE
    if not settings(config) or not available(config):
        return "no image workflow can run (none configured, or model files missing); see: den image"
    return None


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


def loras(config):
    return settings(config).get("loras", {})


def available_loras(config, wf, graph=None):
    """{name: lora config} a workflow can add: its model family, downloaded, not already in the graph."""
    family = wf.get("family")
    if not family or "model" not in wf:
        return {}
    installed = installed_models()
    present = set(model_files(graph)) if graph is not None else set()
    return {
        name: lora
        for name, lora in loras(config).items()
        if lora.get("family") == family
        and lora["file"] not in present
        and any(p.endswith("/" + lora["file"]) for p in installed)
    }


def _check_range(what, value, spec):
    low, high = spec.get("allowed", [None, None])
    if low is not None and value < low or high is not None and value > high:
        raise DenError(f"{what} {value} is outside the allowed range {low}–{high}")


def _add_loras(config, name, wf, graph, chosen):
    """Chain LoraLoaderModelOnly nodes after the workflow's model node."""
    offered = available_loras(config, wf, graph)
    source = [wf["model"], 0]
    consumers = [
        (node, key) for node, spec in graph.items() for key, value in spec["inputs"].items() if value == source
    ]
    used = []
    for i, item in enumerate(chosen):
        lora_name = item.get("name")
        if lora_name not in offered:
            raise DenError(
                f"LoRA {lora_name!r} isn't available for workflow {name}; available: {', '.join(offered) or 'none'}"
            )
        spec = offered[lora_name]
        strength = float(item.get("strength", spec.get("strength", {}).get("default", 1.0)))
        _check_range(f"LoRA {lora_name} strength", strength, spec.get("strength", {}))
        node = str(900 + i)
        graph[node] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": source, "lora_name": spec["file"], "strength_model": strength},
        }
        source = [node, 0]
        used.append({"name": lora_name, "strength": strength})
    for node, key in consumers:
        graph[node]["inputs"][key] = source
    return used


def _load_image(graph, node):
    graph[node] = {"class_type": "LoadImage", "inputs": {"image": ""}}
    return node


def _add_references(name, wf, graph, paths):
    """Chain one ReferenceLatent per reference image into the guider's positive and negative."""
    ref = wf.get("references")
    if not ref:
        raise DenError(f"workflow {name} takes no reference images")
    count = len(paths)
    if count > ref.get("max", 1):
        raise DenError(f"at most {ref.get('max', 1)} reference images, got {count}")
    pos_node, _, pos_key = ref["positive"].partition(".")
    neg_node, _, neg_key = ref["negative"].partition(".")
    loads = []
    for i in range(count):
        load, scale, encode, pos, neg = (str(950 + 5 * i + k) for k in range(5))
        _load_image(graph, load)
        graph[scale] = {
            "class_type": "ImageScaleToTotalPixels",
            "inputs": {"image": [load, 0], "upscale_method": "lanczos", "megapixels": ref.get("megapixels", 1), "resolution_steps": 1},
        }
        graph[encode] = {"class_type": "VAEEncode", "inputs": {"pixels": [scale, 0], "vae": [ref["vae"], 0]}}
        graph[pos] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": graph[pos_node]["inputs"][pos_key], "latent": [encode, 0]}}
        graph[neg] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": graph[neg_node]["inputs"][neg_key], "latent": [encode, 0]}}
        graph[pos_node]["inputs"][pos_key] = [pos, 0]
        graph[neg_node]["inputs"][neg_key] = [neg, 0]
        loads.append((load, paths[i]))
    return loads


def controls(config, wf):
    """A workflow's control (ControlNet) mapping when its model file is downloaded, else None."""
    control = wf.get("control")
    if control and any(p.endswith("/" + control["file"]) for p in installed_models()):
        return control
    return None


def _rewire(graph, old, new, skip=()):
    for node, spec in graph.items():
        if node in skip:
            continue
        for key, value in spec["inputs"].items():
            if value == old:
                spec["inputs"][key] = new


def _add_control(config, name, wf, graph, request):
    """Guide the image with a ControlNet: a guide image, its type and a strength."""
    control = controls(config, wf)
    if not control:
        raise DenError(f"workflow {name} takes no guide image (no ControlNet configured or downloaded)")
    kind = control["kind"]
    kind_types = CONTROL_TYPES[kind]
    allowed_types = control.get("types", list(kind_types))
    kind_type = request.get("type")
    if kind_type not in allowed_types:
        raise DenError(f"control type must be one of {', '.join(allowed_types)}, got {kind_type!r}")
    spec = control.get("strength", {})
    strength = float(request.get("strength", spec.get("default", 1.0)))
    _check_range("control strength", strength, spec)
    start, end = float(request.get("start", 0.0)), float(request.get("end", 1.0))
    if not 0 <= start < end <= 1:
        raise DenError(f"control start and end must be 0 ≤ start < end ≤ 1, got {start} and {end}")
    if kind != "controlnet" and (start, end) != (0.0, 1.0):
        raise DenError(
            f"workflow {name}'s control takes no start or end: it patches the model, which acts "
            "through the whole sampling"
        )
    load = _load_image(graph, "980")
    hint = [load, 0]
    if kind_type == "canny":  # a photo: den draws the edges
        graph["981"] = {"class_type": "Canny", "inputs": {"image": hint, "low_threshold": 0.4, "high_threshold": 0.8}}
        hint = ["981", 0]
    elif kind_type in preprocessors(config):  # a photo: den finds the pose and draws the map
        pre = preprocessors(config)[kind_type]
        graph["985"] = {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": pre["file"]}}
        graph["986"] = {
            "class_type": "SDPoseKeypointExtractor",
            "inputs": {"model": ["985", 0], "vae": ["985", 2], "image": hint, "batch_size": 16},
        }
        graph["987"] = {
            "class_type": "SDPoseDrawKeypoints",
            "inputs": {
                "keypoints": ["986", 0], "draw_body": True, "draw_hands": True, "draw_face": True,
                "draw_feet": True, "draw_head": True, "stick_width": 4, "face_point_size": 2,
                "score_threshold": 0.5,
            },
        }
        hint = ["987", 0]
    if kind == "controlnet":
        graph["982"] = {"class_type": "ControlNetLoader", "inputs": {"control_net_name": control["file"]}}
        graph["983"] = {"class_type": "SetUnionControlNetType", "inputs": {"control_net": ["982", 0], "type": kind_types[kind_type]}}
        pos, neg = [control["positive"], 0], [control["negative"], 0]
        graph["984"] = {
            "class_type": "ControlNetApplyAdvanced",
            "inputs": {
                "positive": pos, "negative": neg, "control_net": ["983", 0], "image": hint, "strength": strength,
                "start_percent": start, "end_percent": end,
            },
        }
        _rewire(graph, pos, ["984", 0], skip={"984"})
        _rewire(graph, neg, ["984", 1], skip={"984"})
    else:  # zimage-fun: a model patch between the model and the sampler
        model = [control["model"], 0]
        graph["982"] = {"class_type": "ModelPatchLoader", "inputs": {"name": control["file"]}}
        graph["984"] = {
            "class_type": "ZImageFunControlnet",
            "inputs": {"model": model, "model_patch": ["982", 0], "vae": [control["vae"], 0], "strength": strength, "image": hint},
        }
        _rewire(graph, model, ["984", 0], skip={"984"})
    used = {"type": kind_type, "strength": strength}
    if (start, end) != (0.0, 1.0):  # only when asked for, so the usual summary line stays short
        used.update(start=start, end=end)
    return used, [(load, request.get("image"))]


def preprocessors(config):
    """{type: preprocessor config} whose model file is downloaded — the guide types den can
    draw itself from a photo, on top of the built-in canny."""
    installed = installed_models()
    return {
        kind: pre
        for kind, pre in settings(config).get("preprocessors", {}).items()
        if any(p.endswith("/" + pre["file"]) for p in installed)
    }


def upscalers(config):
    """{name: upscaler config} whose model file is downloaded."""
    installed = installed_models()
    return {
        name: up
        for name, up in settings(config).get("upscalers", {}).items()
        if any(p.endswith("/" + up["file"]) for p in installed)
    }


def _add_blend(name, wf, graph, strength, edit):
    """Blend the edit back over the image it started from: strength is how much of it to keep.

    A klein edit re-renders the whole frame conditioned on the input rather than changing part
    of it, so nothing is preserved by construction — an instruction meant to be small still
    repaints colours it was never about. Blending restores the untouched pixels exactly and
    scales the change down, which is what "a little warmer" asks for. It ghosts when the edit
    moves something, so it suits light, colour and grade.
    """
    if not edit:
        raise DenError("strength applies to an edit: pass image, or leave strength out")
    source = wf.get("blend")
    if not source:
        raise DenError(f"workflow {name} takes no strength")
    try:
        strength = float(strength)
    except (TypeError, ValueError):
        raise DenError(f"strength must be a number, got {strength!r}") from None
    if not 0 < strength <= 1:
        raise DenError(f"strength must be above 0 and at most 1, got {strength:g}")
    # The blend goes in before any upscale, which rewires the same SaveImage inputs after it.
    if strength < 1:
        for i, spec in enumerate([s for s in graph.values() if s["class_type"] == "SaveImage"]):
            node = str(970 + i)
            graph[node] = {
                "class_type": "ImageBlend",
                "inputs": {
                    "image1": [source, 0],  # the input, already scaled to the result's size
                    "image2": spec["inputs"]["images"],
                    "blend_factor": strength,
                    "blend_mode": "normal",
                },
            }
            spec["inputs"]["images"] = [node, 0]
    return strength


def _add_upscale(config, graph, request):
    """Run the decoded image through an upscale model before it's saved; factor scales the result."""
    offered = upscalers(config)
    up_name = request.get("name")
    if up_name not in offered:
        raise DenError(f"unknown upscaler {up_name!r}; available: {', '.join(offered) or 'none'}")
    up = offered[up_name]
    native = up.get("scale", 4)
    factor = float(request.get("factor", settings(config).get("upscale_factor", 2)))
    if not 1 <= factor <= native:
        raise DenError(f"upscale factor must be between 1 and {native}, got {factor}")
    graph["990"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": up["file"]}}
    for i, (node, spec) in enumerate([(n, s) for n, s in graph.items() if s["class_type"] == "SaveImage"]):
        up_node, scale_node = str(991 + 2 * i), str(992 + 2 * i)
        graph[up_node] = {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["990", 0], "image": spec["inputs"]["images"]}}
        out = [up_node, 0]
        if factor != native:
            graph[scale_node] = {"class_type": "ImageScaleBy", "inputs": {"image": out, "upscale_method": "lanczos", "scale_by": factor / native}}
            out = [scale_node, 0]
        spec["inputs"]["images"] = out
    return {"name": up_name, "factor": factor}


def build(config, name, prompt, negative=None, seed=None, size=None, edit=False, options=None):
    """(graph, parameters used, uploads). Raises DenError on bad input.

    options holds the optional settings (steps, cfg, sampler, scheduler) and extras: loras
    ([{name, strength}]), references ([path]), control ({image, type, strength, start, end})
    and upscale ({name, factor}). With edit, it's the edit variant's graph. uploads lists
    (LoadImage node, path) pairs for the reference and control images; the broker uploads them
    and sets the names (fill_uploads), as it does for the input image (fill_image).
    """
    options = options or {}
    flows = check_workflows(config)
    if name not in flows:
        raise DenError(f"unknown workflow {name!r}; configured: {', '.join(flows) or 'none'}")
    base, problem = flows[name]
    if problem:
        raise DenError(f"workflow {name} can't run: {problem}")
    if not prompt or not prompt.strip():
        raise DenError("the prompt is empty")
    graph_name, wf = variant(name, base, edit)
    graph = load_graph(graph_name)
    _set(graph, wf["prompt"], prompt)
    if negative is not None and negative.strip():
        if "negative" not in wf:
            raise DenError(f"workflow {name} takes no negative prompt")
        _set(graph, wf["negative"], negative)
        # Guidance-distilled models ignore a negative at cfg 1: rewire and raise cfg only when one is given.
        for ref, value in wf.get("with_negative", {}).items():
            _set(graph, ref, value)
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

    # Settings come after with_negative, so an explicit cfg wins over its cfg.
    specs = wf.get("settings", base.get("settings", {}))
    for key, kind in SETTINGS.items():
        if options.get(key) is None:
            continue
        if key not in specs:
            raise DenError(f"workflow {name} has no {key} setting; it has: {', '.join(specs) or 'none'}")
        value = options[key]
        if isinstance(kind, list):
            if value not in kind:
                raise DenError(f"unknown {key} {value!r}; ComfyUI has: {', '.join(kind)}")
        else:
            try:
                value = kind(value)
            except (TypeError, ValueError):
                raise DenError(f"{key} must be a number, got {value!r}") from None
            _check_range(key, value, specs[key])
        for ref in _refs(specs[key]["input"]):
            _set(graph, ref, value)
    # Report every setting the workflow offers, read back from the graph: the caller's value where
    # it gave one, the graph's default otherwise, including a cfg that with_negative raised. A
    # setting that came from the graph is the one most worth seeing, since nobody chose it.
    for key in specs:
        node, _, field = _refs(specs[key]["input"])[0].partition(".")
        if field in graph.get(node, {}).get("inputs", {}):
            params[key] = graph[node]["inputs"][field]
    if options.get("loras"):
        params["loras"] = _add_loras(config, name, base, graph, options["loras"])
    uploads = []
    if options.get("references"):
        uploads += _add_references(name, wf if "references" in wf else base, graph, options["references"])
        params["references"] = len(options["references"])
    if options.get("control"):
        params["control"], control_uploads = _add_control(config, name, base, graph, options["control"])
        uploads += control_uploads
    if options.get("strength") is not None:
        params["strength"] = _add_blend(name, wf, graph, options["strength"], edit)
    if options.get("upscale"):
        params["upscale"] = _add_upscale(config, graph, options["upscale"])
    return graph, params, uploads


def control_summary(control):
    """A control's type and strength, plus its window when a request narrowed it."""
    window = f" {control['start']:g}-{control['end']:g}" if "start" in control else ""
    return f"{control['type']} {control['strength']}{window}"


def summary_parts(params):
    """What an image was made with, in order: workflow, seed, size, settings and extras.

    Every caller's summary line is built from this one list — the CLI's, the MCP server's and
    pi's, which reads it off the wire — so a new setting or extra shows up everywhere at once
    instead of in whichever renderer was remembered. The time isn't here: a `generating` message
    doesn't have it yet, so each caller appends its own.
    """
    parts = [params["workflow"], f"seed {params['seed']}"]
    if params.get("width"):
        parts.append(f"{params['width']}x{params['height']}")
    parts += [f"{key} {params[key]}" for key in SETTINGS if key in params]
    parts += [f"lora {lora['name']} {lora['strength']}" for lora in params.get("loras", [])]
    if params.get("strength") is not None:
        parts.append(f"strength {params['strength']:g}")
    if params.get("references"):
        parts.append(f"{params['references']} reference(s)")
    if params.get("control"):
        parts.append(f"control {control_summary(params['control'])}")
    if params.get("upscale"):
        parts.append(f"upscale {params['upscale']['name']} x{params['upscale']['factor']:g}")
    return parts


def describe_options(config, name, wf):
    """One line per option a workflow offers — size, negative prompt, settings, LoRAs, references,
    control — with its default and range. Everything a caller may pass is named here."""
    specs = wf.get("settings", {})
    graph = load_graph(name)
    edit = wf.get("edit")
    lines = []
    size_nodes = _refs(wf.get("size"))
    if size_nodes:
        inputs = graph.get(size_nodes[0], {}).get("inputs", {})
        default = f"{inputs.get('width')}x{inputs.get('height')}"
        from_image = edit is not None and not _refs(edit.get("size"))
        lines.append(
            f"size: default {default}, any WIDTHxHEIGHT"
            + (" (with an input image the size comes from the image instead)" if from_image else "")
        )
    else:
        lines.append("size: fixed by the workflow, not per request")
    if "negative" in wf:
        cfg = next((ref for ref in wf.get("with_negative", {}) if ref.endswith(".cfg")), None)
        lines.append("negative prompt: taken" + (f", and giving one sets cfg to {wf['with_negative'][cfg]}" if cfg else ""))
    else:
        lines.append("negative prompt: not taken by this workflow")
    for key in SETTINGS:
        if key not in specs:
            continue
        spec = specs[key]
        node, _, field = _refs(spec["input"])[0].partition(".")
        default = graph[node]["inputs"][field]
        if "edit" in wf:
            edit_default = load_graph(f"{name}-edit").get(node, {}).get("inputs", {}).get(field, default)
            if edit_default != default:
                default = f"{default} (edits: {edit_default})"
        recommended = spec.get("recommended")
        if key in ("sampler", "scheduler"):
            rec = f"recommended {', '.join(recommended)}; " if recommended else ""
            lines.append(f"{key}: default {default} ({rec}available: any ComfyUI {key})")
        else:
            rec = f"recommended {recommended[0]}–{recommended[1]}, " if recommended else ""
            allowed = spec.get("allowed")
            lines.append(f"{key}: default {default} ({rec}allowed {allowed[0]}–{allowed[1]})" if allowed else f"{key}: default {default}")
    for lora_name, lora in available_loras(config, wf, graph).items():
        strength = lora.get("strength", {})
        rec, allowed = strength.get("recommended"), strength.get("allowed")
        rng = "".join(
            [f", recommended {rec[0]}–{rec[1]}" if rec else "", f", allowed {allowed[0]}–{allowed[1]}" if allowed else ""]
        )
        desc = describe(lora)
        lines.append(f"lora {lora_name}: {desc} (strength default {strength.get('default', 1.0)}{rng})")
    if (edit or {}).get("blend"):
        lines.append(
            "strength: 0–1 with an input image, default 1 (how much of the edit to keep, blended "
            "back over the input; for light, colour and grade — it ghosts if the edit moves things)"
        )
    refs, edit_refs = wf.get("references"), (edit or {}).get("references")
    if refs or edit_refs:
        counts = [f"up to {refs.get('max', 1)}" if refs else "none"]
        if edit_refs and (not refs or edit_refs.get("max", 1) != refs.get("max", 1)):
            counts.append(f"up to {edit_refs.get('max', 1)} with an input image")
        elif not edit_refs and edit is not None:
            counts.append("none with an input image")
        lines.append(f"references: {', '.join(counts)} (a person, style or object to carry over)")
    control = controls(config, wf)
    if control:
        strength = control.get("strength", {})
        rec, allowed = strength.get("recommended"), strength.get("allowed")
        offered = control.get("types", CONTROL_TYPES[control["kind"]])
        # canny is built into ComfyUI; the rest den can only draw where its preprocessor is downloaded.
        drawn = [kind for kind in offered if kind == "canny" or kind in preprocessors(config)]
        ready = [kind for kind in offered if kind not in drawn]
        how = f"den draws {', '.join(drawn)} from an ordinary photo" if drawn else ""
        how += ("; " if how and ready else "") + (f"{', '.join(ready)} take a ready-made map" if ready else "")
        lines.append(
            f"control: guide image types {', '.join(offered)}"
            f" ({how});"
            f" strength default {strength.get('default', 1.0)}"
            + (f", recommended {rec[0]}–{rec[1]}" if rec else "")
            + (f", allowed {allowed[0]}–{allowed[1]}" if allowed else "")
            # start/end are a ControlNet's own inputs; the model-patch kind has no equivalent.
            + ("; start/end limit it to part of the sampling (default 0–1)" if control["kind"] == "controlnet" else "")
        )
    return lines


def fill_image(config, name, graph, uploaded):
    _set(graph, workflows(config)[name]["edit"]["image"], uploaded)


def fill_uploads(graph, uploads, names):
    for (node, _), uploaded in zip(uploads, names):
        graph[node]["inputs"]["image"] = uploaded


def describe_upscalers(config):
    """One line per downloaded upscaler, for whoever picks one."""
    factor = settings(config).get("upscale_factor", 2)
    return [
        f"{name}: {describe(up)} (factor default {factor}, allowed 1–{up.get('scale', 4)})"
        for name, up in upscalers(config).items()
    ]


def request_spec(config, flows, default):
    """{description, parameters}: what a model needs to call POST /image, shared by Claude's MCP
    tool and pi's extension (`den image --json`). Each caller adds how swaps affect it."""
    lines, lora_names, control_types = [], set(), set()
    for name, wf in flows.items():
        marks = " [default]" * (name == default) + " [edits]" * ("edit" in wf)
        lines.append(f"- {name}{marks}: {describe(wf)}")
        lines.extend(f"    {line}" for line in describe_options(config, name, wf))
        lora_names.update(available_loras(config, wf, load_graph(name)))
        control = controls(config, wf)
        if control:
            control_types.update(control.get("types", CONTROL_TYPES[control["kind"]]))
    has_references = any("references" in wf or "references" in (wf.get("edit") or {}) for wf in flows.values())
    has_strength = any((wf.get("edit") or {}).get("blend") for wf in flows.values())
    workflow_lines = "\n".join(lines)
    ups = describe_upscalers(config)
    description = (
        "Generate an image on this machine's GPU with a local image model (ComfyUI), or edit an "
        "input image. Pick the workflow that fits the request and write the prompt in the style "
        "its description asks for; prompt style matters more than the choice of model. "
        "Workflows, each with every option it offers (default, recommended, allowed); an option "
        "a workflow doesn't list is refused for it:\n"
        f"{workflow_lines}\n"
        + ("Upscalers, for any workflow:\n" + "".join(f"    {line}\n" for line in ups) if ups else "")
        + f"{PROMPT_SYNTAX}\n"
        "Only prompt is required. Size, negative prompt, settings, LoRAs, references, control and "
        "upscale are optional: start with the workflow's defaults, and change them when feedback "
        "on an earlier image calls for it, reusing that image's seed so the change is the only "
        "difference. A value outside a workflow's allowed range is refused, with the range.\n"
        "On a workflow whose cfg defaults to 1 the model is guidance-distilled: raise cfg only "
        "together with a negative prompt. Above 1 with no negative the sampler pushes away from "
        "an empty prompt — on an edit, away from the input image itself — which overcooks colour "
        "and contrast, and costs about twice the time.\n"
        "A workflow marked [edits] also edits an input image: pass `image` and make the prompt the "
        "instruction. Combine what a workflow lists — an edit with references, control with a "
        "LoRA, any of them with an upscale — in one call. An edit re-renders the whole frame, so "
        "it can repaint colours the instruction was not about; `strength` on a workflow that "
        "lists it scales the change back down.\n"
        "The image is saved in a dated folder on this machine; the result gives its path, seed, "
        "workflow and every setting used. Pass `out` to also copy it somewhere, e.g. into a "
        "project."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The image prompt, or the edit instruction with `image`."},
            "workflow": {"type": "string", "enum": list(flows), "description": f"Default: {default}."},
            "negative": {
                "type": "string",
                "description": (
                    "What to avoid; not every workflow takes one. Leave it out on a first try: fix "
                    "problems by rewording the prompt first. Add one when feedback on an earlier "
                    "image names something unwanted that rewording didn't remove, and reuse that "
                    "image's seed so the change shows."
                ),
            },
            "size": {
                "type": "string",
                "description": (
                    "WIDTHxHEIGHT, e.g. 1024x1024; omit for the workflow's default, listed above. "
                    "Stay near the default's total pixels — these models degrade well beyond it. "
                    "An edit takes its size from the input image, so leave it out there."
                ),
            },
            "seed": {
                "type": "integer",
                "description": (
                    "Omit for a random one. Pass the seed of an earlier image to change one thing "
                    "about it (prompt, a setting, a LoRA) and see only that change."
                ),
            },
            "image": {
                "type": "string",
                "description": (
                    "Absolute path of an input image to edit, for workflows marked [edits]: the "
                    "prompt becomes the instruction, e.g. \"make the sky red, keep the rest\"."
                ),
            },
            "out": {
                "type": "string",
                "description": "Absolute path of a file, or of a directory ending in /, to copy the image to.",
            },
            "steps": {"type": "integer", "description": "Sampling steps, within the workflow's range."},
            "cfg": {
                "type": "number",
                "description": "Guidance scale, within the workflow's range; overrides the cfg a negative sets.",
            },
            "sampler": {"type": "string", "enum": SAMPLERS, "description": "Prefer the workflow's recommended ones."},
            "scheduler": {
                "type": "string",
                "enum": SCHEDULERS,
                "description": "Prefer the workflow's recommended ones; not every workflow has one.",
            },
            **(
                {
                    "loras": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "enum": sorted(lora_names)},
                                "strength": {"type": "number"},
                            },
                            "required": ["name"],
                        },
                        "description": "LoRAs the workflow lists, with an optional strength.",
                    }
                }
                if lora_names
                else {}
            ),
            **(
                {
                    "references": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Absolute paths of reference images — a person, style or object to carry "
                            "over into the new image — up to the count the workflow lists. They "
                            "guide the image; they are not edited. Works together with `image`."
                        ),
                    }
                }
                if has_references
                else {}
            ),
            **(
                {
                    "strength": {
                        "type": "number",
                        "description": (
                            "How much of the edit to keep, 0–1 (default 1), on a workflow that lists "
                            "it. An edit re-renders the whole frame, so a small instruction can still "
                            "repaint colours it was not about; blending it back over the input at, "
                            "say, 0.3 restores the untouched pixels exactly and scales the change "
                            "down. For light, colour and grade — it ghosts if the edit moves things."
                        ),
                    }
                }
                if has_strength
                else {}
            ),
            **(
                {
                    "control": {
                        "type": "object",
                        "properties": {
                            "image": {"type": "string", "description": "Absolute path of the guide image."},
                            "type": {
                                "type": "string",
                                "enum": sorted(control_types),
                                "description": "What the guide image is, from the types the workflow lists: canny for "
                                "a photo (den draws its edges), or a ready-made pose, depth, … map.",
                            },
                            "strength": {
                                "type": "number",
                                "description": "How strongly it steers; the workflow's default and range are listed above.",
                            },
                            "start": {
                                "type": "number",
                                "description": "Fraction of the sampling where the guide starts acting; default 0.",
                            },
                            "end": {
                                "type": "number",
                                "description": (
                                    "Fraction where it stops; default 1. End around 0.5 to fix the composition "
                                    "early and leave the detail free."
                                ),
                            },
                        },
                        "required": ["image", "type"],
                        "description": (
                            "Lock composition, pose or outlines to a guide image, for workflows that list "
                            "control. start and end only apply to workflows whose control line names them."
                        ),
                    }
                }
                if control_types
                else {}
            ),
            **(
                {
                    "upscale": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "enum": sorted(upscalers(config))},
                            "factor": {
                                "type": "number",
                                "description": "How much larger; each upscaler's default and range are listed above.",
                            },
                        },
                        "required": ["name"],
                        "description": (
                            "Enlarge the finished image with an upscale model, with any workflow. It "
                            "adds no detail the image doesn't have: generate at a good size first."
                        ),
                    }
                }
                if upscalers(config)
                else {}
            ),
        },
        "required": ["prompt"],
    }
    return {"description": description, "parameters": parameters}


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

    def view(self, img, preview=None):
        """The image's bytes; with preview ("jpeg;70") ComfyUI re-encodes it on the way out."""
        query = {k: img.get(k, "") for k in ("filename", "subfolder", "type")}
        if preview:
            query["preview"] = preview
        with self._open("GET", f"/view?{urllib.parse.urlencode(query)}", timeout=60) as resp:
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


# A copy of the result for a caller whose model can look at images (Claude's MCP tool). The
# file that is saved stays a full-size PNG; this is only what goes back over the wire.
PREVIEW_FORMAT = "jpeg;70"
PREVIEW_MAX_BYTES = 4 << 20


def preview(comfy, img):
    """{mime, base64} of a small re-encoded copy of an output image, or None.

    A caller that asks for it still gets the path and the settings, so an old ComfyUI without
    /view?preview, or an image too big to hand over, must not fail the request.
    """
    try:
        data = comfy.view(img, preview=PREVIEW_FORMAT)
    except DenError:
        return None
    if not data or len(data) > PREVIEW_MAX_BYTES:
        return None
    return {"mime": "image/jpeg", "base64": base64.b64encode(data).decode()}


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
