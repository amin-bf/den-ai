"""Clips: short videos from video workflows, made on the image side (ADR 0009).

A clip workflow is a ComfyUI graph, `workflows/<name>.json`, plus its `[clip.workflows.<name>]`
entry in config.toml, like an image workflow: a description, the node inputs that receive the
prompt, seed and size, and what a clip adds — its duration in seconds, its frame rate, and the
keyframes it takes (images the clip must show at given moments). Availability follows the model
files, as for images. The broker runs a clip as a detached request: it answers with an id and
makes the clip on its own, since one takes minutes.

Every clip also gets a contact sheet: a few of its frames tiled into one image inside the graph,
saved beside it, so a caller that can't watch video (Claude's model) can still see what it made.
den never decodes video itself.
"""

import fcntl
import json
import os
import random
import re
import statistics
import time
from pathlib import Path

from den import core, image, speech
from den.core import DenError

CLIP_DIR = Path(os.environ.get("DEN_CLIPS") or Path.home() / "Videos/den")
LOG_PATH = Path(os.environ.get("DEN_CLIP_LOG", core._STATE_HOME / "den/clips.jsonl"))
SHEET_FRAMES = 4  # tiled 2x2
SHEET_WIDTH = 1024
# Node ids den adds to a clip graph; exported graphs number theirs far lower.
KEYFRAME_NODE = 940
SHEET_NODE = 960
VOICE_NODE = 980
VOICE_UNDER_DB = -8  # the clip's own sound, under a voice-over


def settings(config):
    return config.get("clip", {})


def workflows(config):
    return settings(config).get("workflows", {})


def check_workflows(config, folders=True):
    """{name: (workflow config, problem or None)}, as image.check_workflows does for images."""
    installed = image.installed_models()
    result = {}
    for name, wf in workflows(config).items():
        try:
            graph = image.load_graph(name)
        except DenError as e:
            result[name] = (wf, str(e))
            continue
        missing = [f for f in image.model_files(graph) if not any(p.endswith("/" + f) for p in installed)]
        where = f" in {image.COMFYUI_DIR / 'models'}" if folders else ""
        result[name] = (wf, f"missing model files{where}: {', '.join(missing)}" if missing else None)
    return result


def available(config):
    return {name: wf for name, (wf, problem) in check_workflows(config).items() if problem is None}


def unavailable(config, state):
    """Why a clip request can't run now, or None."""
    if not core.is_on(state):
        return core.OFF_MESSAGE
    if not available(config):
        return "no clip workflow can run (none configured, or model files missing); see: den clip"
    return None


def _graph_value(graph, ref):
    node, _, field = ref.partition(".")
    return graph.get(node, {}).get("inputs", {}).get(field)


def _fps(wf, graph):
    fps = _graph_value(graph, wf["fps"]) if "fps" in wf else None
    if not fps:
        raise DenError("clip workflow mapping needs fps = \"node.input\" (the frame rate)")
    return float(fps)


def frame_count(duration, fps, step):
    """Frames for a clip of about duration seconds: a multiple of the model's step, plus the
    first one."""
    return max(1, round(duration * fps / step)) * step + 1


def _segments(wf):
    """How many chained segments a clip is made of: each renders from one keyframe to the next,
    and neighbours share the frame between them."""
    return int(wf["duration"].get("segments", 1))


def _default_duration(wf, graph, fps):
    frames = _graph_value(graph, image._refs(wf["duration"]["input"])[0])
    return round(_segments(wf) * (frames - 1) / fps, 2)


def keyframe_index(at, fps, frames):
    """The frame a keyframe's `at` names: seconds as a number, "NN%" of the clip, or "end"."""
    last = frames - 1
    if at == "end":
        return last
    if isinstance(at, str) and at.endswith("%"):
        try:
            share = float(at[:-1]) / 100
        except ValueError:
            raise DenError(f"keyframe at {at!r}: a percentage looks like 50%") from None
        if not 0 <= share <= 1:
            raise DenError(f"keyframe at {at}: must be between 0% and 100%")
        return round(share * last)
    try:
        seconds = float(at)
    except (TypeError, ValueError):
        raise DenError(f"keyframe at {at!r}: give seconds, a percentage (50%) or end") from None
    index = round(seconds * fps)
    if not 0 <= index <= last:
        raise DenError(f"keyframe at {seconds:g}s is outside the clip (0–{last / fps:g}s)")
    return index


def fit_size(path, pixels, step):
    """(width, height) with the image's aspect and about `pixels` pixels, both a multiple of
    step; None when the file's size can't be read."""
    found = image.file_size(path) if path else None
    if not found or not all(found):
        return None
    aspect = found[0] / found[1]
    width = max(step, round((pixels * aspect) ** 0.5 / step) * step)
    height = max(step, round((pixels / aspect) ** 0.5 / step) * step)
    return width, height


def _slot_word(at):
    return {0: "the start", "end": "the end", "any": "any moment"}.get(at, f"{at} of the way")


def _slot_words(slots):
    return ", ".join(_slot_word(s.get("at", 0)) for s in slots)


def _wire(graph, ref, value):
    """Set an input a graph may leave out (a keyframe image is optional): only the node must exist."""
    node, _, field = ref.partition(".")
    if node not in graph:
        raise DenError(f"workflow mapping {ref!r} doesn't match a node in the graph")
    graph[node]["inputs"][field] = value


def _bypass(graph, node, passthrough):
    """Take an unused guide node out of the graph: whatever read its outputs reads, in order, the
    inputs it would have passed on (e.g. positive, negative, latent)."""
    inputs = graph.pop(node)["inputs"]
    for spec in graph.values():
        for key, value in spec["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and value[0] == node:
                spec["inputs"][key] = inputs[passthrough[value[1]]]


def _add_keyframes(name, wf, graph, keyframes, fps, frames):
    """Wire each keyframe's image into the workflow's keyframe input for that moment; returns the
    (LoadImage node, path) uploads and what went where.

    A slot is {input = "node.input" or a list (one keyframe can end one segment and start the
    next), at = 0 | "end" | "NN%" | "any", frame = "node.input" that receives the frame index
    (optional), passthrough = [input names] for a guide node taken out when no keyframe fills it}.
    """
    slots = [dict(slot, at=slot.get("at", 0)) for slot in wf.get("keyframes", [])]
    if len(keyframes) > len(slots):
        takes = f"{len(slots)} keyframe(s), at {_slot_words(slots)}" if slots else "no keyframes"
        raise DenError(f"workflow {name} takes {takes}; got {len(keyframes)}")
    fixed = {keyframe_index(slot["at"], fps, frames): slot for slot in slots if slot["at"] != "any"}
    anywhere = [slot for slot in slots if slot["at"] == "any"]
    uploads, placed, used = [], [], set()
    for i, keyframe in enumerate(keyframes):
        path = keyframe.get("image")
        if not path:
            raise DenError(f"keyframe {i + 1} has no image")
        # The first keyframe starts the clip unless it says otherwise; later ones must say when.
        at = keyframe.get("at", 0 if i == 0 else None)
        if at is None:
            raise DenError(f"keyframe {i + 1} needs an `at`: seconds, a percentage (50%) or end")
        index = keyframe_index(at, fps, frames)
        slot = fixed.pop(index, None) or (anywhere.pop(0) if anywhere else None)
        if slot is None:
            raise DenError(
                f"workflow {name} can't place a keyframe at frame {index} ({at}); "
                f"it takes keyframes at {_slot_words(slots)} only"
            )
        node = str(KEYFRAME_NODE + i)
        image._load_image(graph, node)
        for ref in image._refs(slot["input"]):
            _wire(graph, ref, [node, 0])
        for ref in image._refs(slot.get("frame")):
            _wire(graph, ref, index)
        used.add(id(slot))
        uploads.append((node, path))
        placed.append({"at": at, "frame": index})
    for slot in slots:
        if id(slot) not in used and slot.get("passthrough"):
            _bypass(graph, image._refs(slot["input"])[0].partition(".")[0], slot["passthrough"])
    return uploads, placed


def _add_sheet(graph, frames_node, frames):
    """Pick SHEET_FRAMES frames evenly, tile them 2x2 and scale the tile; returns its node."""
    count = min(SHEET_FRAMES, frames)
    picks = sorted({round(i * (frames - 1) / max(1, count - 1)) for i in range(count)})
    nodes = []
    for i, index in enumerate(picks):
        node = str(SHEET_NODE + i)
        graph[node] = {"class_type": "ImageFromBatch", "inputs": {"image": [frames_node, 0], "batch_index": index, "length": 1}}
        nodes.append(node)

    def stitch(node, a, b, direction):
        graph[node] = {
            "class_type": "ImageStitch",
            "inputs": {"image1": [a, 0], "image2": [b, 0], "direction": direction, "match_image_size": True, "spacing_width": 0, "spacing_color": "white"},
        }
        return node

    if len(nodes) == 4:
        top = stitch(str(SHEET_NODE + 4), nodes[0], nodes[1], "right")
        bottom = stitch(str(SHEET_NODE + 5), nodes[2], nodes[3], "right")
        tiled = stitch(str(SHEET_NODE + 6), top, bottom, "down")
    else:
        tiled = nodes[0]
        for i, node in enumerate(nodes[1:]):
            tiled = stitch(str(SHEET_NODE + 4 + i), tiled, node, "right")
    scaled, shown = str(SHEET_NODE + 7), str(SHEET_NODE + 8)
    graph[scaled] = {
        "class_type": "ImageScale",
        "inputs": {"image": [tiled, 0], "upscale_method": "lanczos", "width": SHEET_WIDTH, "height": 0, "crop": "disabled"},
    }
    graph[shown] = {"class_type": "PreviewImage", "inputs": {"images": [scaled, 0]}}
    return shown


def build(config, name, prompt, negative=None, seed=None, size=None, duration=None, keyframes=(), options=None):
    """(graph, parameters used, uploads, sheet node). Raises DenError on bad input.

    keyframes is [{image: path, at?}]; options holds the settings (steps, cfg, sampler,
    scheduler), loras ([{name, strength}], [image.loras] entries of the workflow's family) and
    sound (a bool, for a workflow that makes sound: false leaves the sound track out).
    uploads lists (LoadImage node, path) for the keyframes, which the broker uploads
    and fills in (image.fill_uploads).
    """
    options = options or {}
    flows = check_workflows(config)
    if name not in flows:
        raise DenError(f"unknown clip workflow {name!r}; configured: {', '.join(flows) or 'none'}")
    wf, problem = flows[name]
    if problem:
        raise DenError(f"workflow {name} can't run: {problem}")
    if not prompt or not prompt.strip():
        raise DenError("the prompt is empty")
    graph = image.load_graph(name)
    image._set(graph, wf["prompt"], prompt)
    if negative is not None and negative.strip():
        if "negative" not in wf:
            raise DenError(f"workflow {name} takes no negative prompt")
        # Added to the graph's own negative, which a caller rarely means to throw away.
        builtin = _graph_value(graph, wf["negative"]) or ""
        image._set(graph, wf["negative"], ", ".join(filter(None, (builtin.strip(), negative.strip()))))
    seed = random.randrange(2**32) if seed is None else int(seed)
    for ref in image._refs(wf.get("seed")):
        image._set(graph, ref, seed)
    size_nodes = image._refs(wf.get("size"))
    sized_by = None
    if size is None and size_nodes and keyframes:
        # A keyframe the clip's shape doesn't match is cropped to fit, so without a size the clip
        # takes the first keyframe's shape, at the workflow's own number of pixels.
        inputs = graph[size_nodes[0]]["inputs"]
        first = next((i for i, k in enumerate(keyframes) if k.get("at", 0) in (0, 0.0, "0")), 0)
        fitted = fit_size(keyframes[first].get("image"), inputs["width"] * inputs["height"], int(wf.get("size_step", 32)))
        if fitted:
            size, sized_by = f"{fitted[0]}x{fitted[1]}", f"keyframe {first + 1}"
    if size is not None:
        if not size_nodes:
            raise DenError(f"workflow {name} has a fixed size")
        width, height = image.parse_size(size)
        for node in size_nodes:
            image._set(graph, f"{node}.width", width)
            image._set(graph, f"{node}.height", height)
    else:
        inputs = graph[size_nodes[0]]["inputs"] if size_nodes else {}
        width, height = inputs.get("width"), inputs.get("height")

    sound = _set_sound(flows, name, wf, graph, options.get("sound"))

    fps = _fps(wf, graph)
    length = wf["duration"]
    if duration is None:
        duration = _default_duration(wf, graph, fps)
    else:
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            raise DenError(f"duration must be a number of seconds, got {duration!r}") from None
        image._check_range("duration", duration, length)
    segments = _segments(wf)
    per_segment = frame_count(duration / segments, fps, int(length.get("frame_step", 1)))
    for ref in image._refs(length["input"]):
        image._set(graph, ref, per_segment)
    frames = segments * (per_segment - 1) + 1

    # "duration" is the clip's length; "seconds" in a result is how long it took to make, as
    # for images.
    params = {
        "workflow": name,
        "seed": seed,
        "width": width,
        "height": height,
        "duration": round((frames - 1) / fps, 2),
        "frames": frames,
        "fps": fps,
        **({"size_from": sized_by} if sized_by else {}),
        **({"sound": sound} if sound is not None else {}),
    }
    params |= image.apply_settings(name, wf.get("settings", {}), graph, options)
    if options.get("loras"):
        params["loras"] = image._add_loras(config, name, wf, graph, options["loras"])
    uploads, placed = _add_keyframes(name, wf, graph, list(keyframes or []), fps, frames)
    if placed:
        params["keyframes"] = placed
    sheet = _add_sheet(graph, str(wf["frames"]), frames) if "frames" in wf else None
    return graph, params, uploads, sheet


def _set_sound(flows, name, wf, graph, wanted):
    """Whether the clip gets its sound track: None for a workflow that makes none. `sound` in a
    workflow names the input that receives the decoded audio (e.g. CreateVideo's audio); sound
    off takes that input out, and the decoder with it, so ComfyUI doesn't decode it."""
    if wanted not in (None, True, False):
        raise DenError(f"sound must be true or false, got {wanted!r}")
    if "sound" not in wf:
        if wanted:
            makes = [n for n, (w, problem) in flows.items() if "sound" in w and problem is None]
            raise DenError(
                f"workflow {name} makes silent clips; "
                + (f"for sound use {', '.join(makes)}" if makes else "no clip workflow here makes sound")
            )
        return None
    node, _, field = wf["sound"].partition(".")
    if field not in graph.get(node, {}).get("inputs", {}):
        raise DenError(f"workflow mapping {wf['sound']!r} doesn't match a node input in the graph")
    if wanted is False:
        source = graph[node]["inputs"].pop(field)
        # The decoder feeds only the sound track, so it goes too.
        if isinstance(source, list) and not any(
            value == source for spec in graph.values() for value in spec["inputs"].values()
        ):
            graph.pop(source[0], None)
        return False
    return True


def add_voiceover(graph, wf, track, duration):
    """Put a voice-over track (an uploaded file's name) into the clip: over the clip's own sound,
    turned down, where it has some; as its sound track where it has none. Either way the track
    ends with the clip."""
    loaded = str(VOICE_NODE)
    graph[loaded] = {"class_type": "LoadAudio", "inputs": {"audio": track}}
    node, _, field = (wf.get("sound") or "").partition(".")
    own = graph.get(node, {}).get("inputs", {}).get(field)
    if isinstance(own, list):
        under, mixed = str(VOICE_NODE + 1), str(VOICE_NODE + 2)
        graph[under] = {"class_type": "AudioAdjustVolume", "inputs": {"audio": own, "volume": VOICE_UNDER_DB}}
        # AudioMerge fits the second track to the first's length: the voice ends with the clip.
        graph[mixed] = {"class_type": "AudioMerge", "inputs": {"audio1": [under, 0], "audio2": [loaded, 0], "merge_method": "add"}}
        graph[node]["inputs"][field] = [mixed, 0]
        return
    videos = [n for n, spec in graph.items() if spec.get("class_type") == "CreateVideo"]
    if not videos:
        raise DenError("this workflow's graph has no CreateVideo node to put a voice-over on")
    trimmed = str(VOICE_NODE + 3)
    graph[trimmed] = {"class_type": "TrimAudioDuration", "inputs": {"audio": [loaded, 0], "start_index": 0.0, "duration": float(duration)}}
    for video in videos:
        graph[video]["inputs"]["audio"] = [trimmed, 0]


def voiceover_length(cues):
    """How long a clip must be to hold an SRT voice-over: to its last line's end, and a breath."""
    ends = [c["end"] for c in cues if c.get("end") is not None]
    return round(max(ends) + 0.5, 2) if ends else None


def has_sound(path):
    """Whether an MP4 or MOV file holds a sound track: its handler box says soun."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    return re.search(rb"hdlr.{8}soun", data, re.DOTALL) is not None


def summary_parts(params):
    """What a clip was made with: workflow, seed, size, length, settings, keyframes."""
    parts = [params["workflow"], f"seed {params['seed']}"]
    if params.get("width"):
        shape = f" (the shape of {params['size_from']})" if params.get("size_from") else ""
        parts.append(f"{params['width']}x{params['height']}{shape}")
    parts.append(f"{params['duration']:g}s ({params['frames']} frames at {params['fps']:g} fps)")
    parts += [f"{key} {params[key]}" for key in image.SETTINGS if key in params]
    parts += [f"lora {lora['name']} {lora['strength']}" for lora in params.get("loras", [])]
    if params.get("keyframes"):
        parts.append(f"{len(params['keyframes'])} keyframe(s)")
    if "sound" in params:
        parts.append("with sound" if params["sound"] else "sound off")
    if params.get("voiceover"):
        parts.append(f"voice-over {params['voiceover']}")
    return parts


def _ranges(rec, allowed):
    """ " (recommended a–b, allowed c–d)" with whichever of the two a setting has."""
    parts = [f"recommended {rec[0]}–{rec[1]}" if rec else None, f"allowed {allowed[0]}–{allowed[1]}" if allowed else None]
    shown = [p for p in parts if p]
    return f" ({', '.join(shown)})" if shown else ""


def describe_options(config, name, wf):
    """One line per option a clip workflow offers, with its default and range."""
    graph = image.load_graph(name)
    fps = _fps(wf, graph)
    lines = []
    size_nodes = image._refs(wf.get("size"))
    if size_nodes:
        inputs = graph.get(size_nodes[0], {}).get("inputs", {})
        lines.append(f"size: default {inputs.get('width')}x{inputs.get('height')}, any WIDTHxHEIGHT")
    else:
        lines.append("size: fixed by the workflow")
    length = wf["duration"]
    rec, allowed = length.get("recommended"), length.get("allowed")
    lines.append(
        f"duration: default {_default_duration(wf, graph, fps):g} seconds at {fps:g} fps"
        + _ranges(rec, allowed)
        + "; time to make grows with it"
    )
    specs = wf.get("settings", {})
    lines.append(
        "sound: made with the picture, from the prompt (end it with the sounds); sound false leaves it out"
        if "sound" in wf
        else "sound: none, the clip is silent"
    )
    if "negative" in wf:
        # A sampler at cfg 1 skips the negative pass, so a negative only works with a higher cfg.
        cfg = _graph_value(graph, specs["cfg"]["input"]) if "cfg" in specs else None
        lines.append(
            "negative prompt: taken, added to the workflow's own"
            + ("; no effect at the default cfg 1, only with a higher cfg" if cfg == 1 else "")
        )
    else:
        lines.append("negative prompt: not taken by this workflow")
    for key in image.SETTINGS:
        if key not in specs:
            continue
        spec = specs[key]
        default = _graph_value(graph, image._refs(spec["input"])[0])
        rec = spec.get("recommended")
        if key in ("sampler", "scheduler"):
            lines.append(f"{key}: default {default}" + (f" (recommended {', '.join(rec)})" if rec else ""))
        else:
            allowed = spec.get("allowed")
            lines.append(f"{key}: default {default}" + _ranges(rec, allowed))
    for lora_name, lora in image.available_loras(config, wf, graph).items():
        strength = lora.get("strength", {})
        ranges = _ranges(strength.get("recommended"), strength.get("allowed"))  # " (…)" or ""
        extra = f", {ranges[2:-1]}" if ranges else ""
        lines.append(f"lora {lora_name}: {image.describe(lora)} (strength default {strength.get('default', 1.0)}{extra})")
    slots = wf.get("keyframes", [])
    lines.append(
        f"keyframes: up to {len(slots)}, at {_slot_words(slots)} (an image the clip must show there)"
        if slots
        else "keyframes: none (text to video only)"
    )
    return lines


def listing(config, folders=True):
    """`den clip` without a prompt: every clip workflow with its options or why it can't run."""
    default = settings(config).get("default_workflow")
    flows = check_workflows(config, folders)
    lines = [] if flows else ["no clip workflows configured ([clip.workflows.<name>] in config.toml)"]
    for name, (wf, problem) in flows.items():
        marker = "*" if name == default else " "
        lines.append(f"{marker} {name:<16} {image.describe(wf)}")
        if problem:
            lines.append(f"  {'':<16} CAN'T RUN: {problem}")
            continue
        lines += [f"  {'':<16} {line}" for line in describe_options(config, name, wf)]
    return "\n".join([*lines, "", image.PROMPT_SYNTAX])


def request_spec(config, flows, default):
    """The clip tool's description and JSON schema, built from the workflows that can run."""
    lines = [
        "The den-clip skill has the recipes (the look as stills first, keyframes, sound, a voice-over).",
        "Clip workflows (pick by style; each has its own prompt format):",
    ]
    for name, wf in flows.items():
        marker = " (default)" if name == default else ""
        lines.append(f"- {name}{marker}: {image.describe(wf)}")
        lines += [f"    {line}" for line in describe_options(config, name, wf)]
    lora_names = set()
    for name, wf in flows.items():
        lora_names.update(image.available_loras(config, wf, image.load_graph(name)))
    settings_schema = {
        "steps": {"type": "integer"},
        "cfg": {"type": "number"},
        "sampler": {"type": "string"},
        "scheduler": {"type": "string"},
    }
    return {
        "description": "\n".join([*lines, "", image.PROMPT_SYNTAX]),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "What happens in the clip: subject, action, camera, light."},
                "workflow": {"type": "string", "enum": list(flows), "description": f"Default: {default}."},
                "negative": {"type": "string", "description": "What to avoid, added to the workflow's own negative."},
                "seed": {"type": "integer", "description": "Reuse one to change a single thing between clips."},
                "size": {"type": "string", "description": "WIDTHxHEIGHT, e.g. 1280x704."},
                "duration": {"type": "number", "description": "Length of the clip in seconds."},
                **(
                    {
                        "voiceover": {
                            "type": "object",
                            "description": "A voice-over, spoken before the clip is made and mixed over its "
                            "sound (or as its sound track on a silent workflow). With an SRT and no duration, "
                            "the clip lasts to the script's end, and longer if the voice runs long.",
                            "properties": speech.spoken_properties(),
                        }
                    }
                    if speech.unavailable() is None
                    else {}
                ),
                "sound": {
                    "type": "boolean",
                    "description": "Whether the clip gets a sound track, on a workflow whose options say it makes sound "
                    "(default true there); asking a silent workflow for sound is refused. Describe the sounds in the prompt.",
                },
                "keyframes": {
                    "type": "array",
                    "description": "Images the clip must show at given moments; the first starts it.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "image": {"type": "string", "description": "Absolute path of the image."},
                            "at": {"description": "Seconds, a percentage like \"50%\", or \"end\". Default for the first: 0."},
                        },
                        "required": ["image"],
                    },
                },
                **settings_schema,
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
                "out": {"type": "string", "description": "Also copy the clip here (a file, or a folder ending in /)."},
            },
            "required": ["prompt"],
        },
    }


def client_spec(config, state):
    """What a client needs to offer clips here, like image.client_spec."""
    default = settings(config).get("default_workflow")
    flows = available(config)
    spec = request_spec(config, flows, default) if flows else {"description": None, "parameters": None}
    why = unavailable(config, state)
    return {
        "clip_on": why is None,
        "unavailable": why,
        "default": default,
        "workflows": list(flows),
        # How many keyframes each takes, for a client that offers a keyframe per row.
        "keyframes": {name: len(wf.get("keyframes", [])) for name, wf in flows.items()},
        # Which make sound, for a client that offers sound only where there is some.
        "sound": {name: "sound" in wf for name, wf in flows.items()},
        # The LoRAs each takes, for a client that offers them as a list.
        "loras": {
            name: {
                lora_name: {"description": image.describe(lora), "strength": lora.get("strength", {}).get("default", 1.0)}
                for lora_name, lora in image.available_loras(config, wf, image.load_graph(name)).items()
            }
            for name, wf in flows.items()
        },
        **spec,
    }


def save(data, suffix, sheet, prompt, out=None):
    """Write the clip to CLIP_DIR/YYYY-MM-DD/<time>-<slug><suffix>, its contact sheet beside it as
    <clip>-sheet.png; copy the clip to out if given. Returns (clip, sheet or None, copies)."""
    day = CLIP_DIR / time.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    stem = f"{time.strftime('%H%M%S')}-{image.slug(prompt)}"
    n, path = 0, None
    while path is None or path.exists():
        path = day / f"{stem}{f'-{n + 1}' if n else ''}{suffix}"
        n += 1
    path.write_bytes(data)
    sheet_path = None
    if sheet is not None:
        sheet_path = path.with_name(f"{path.stem}-sheet.png")
        sheet_path.write_bytes(sheet)
    return path, sheet_path, image.copy_out([path], out)


def log(entry):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps({"ts": int(time.time()), **entry}) + "\n")


def estimate(params):
    """Seconds a clip with these parameters will take to make, from earlier clips of the same
    workflow and size, or None the first time. Time grows with frames times steps."""
    work = params["frames"] * (params.get("steps") or 1)
    rates = []
    try:
        with open(LOG_PATH) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (e.get("workflow"), e.get("width"), e.get("height")) != (params["workflow"], params["width"], params["height"]):
                    continue
                if e.get("seconds") and e.get("frames"):
                    rates.append(e["seconds"] / (e["frames"] * (e.get("steps") or 1)))
    except OSError:
        return None
    return round(statistics.median(rates[-10:]) * work) if rates else None
