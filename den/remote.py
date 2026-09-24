"""A den on another machine, used from one that keeps none of den's files (ADR 0007).

Everything den keeps stays with the broker: config, state, the tasks, the model, the workflows,
the pose library and the logs. A client here reads only where that broker is (`[remotes.<name>]`)
and sends everything else there. No path of either machine crosses, only bytes: a delegation's
files go as their name and contents, an input image as its name and bytes, and each generated
image comes back as bytes and is saved here the way a local den saves it.
"""

import base64
from pathlib import Path

from den import clip, core, image, speech
from den.core import DenError

_checked = set()


def client(caller):
    """The remote broker's client, once it is known to serve remote clients."""
    broker = core.broker(core.load_config(), caller)
    if broker.base_url not in _checked:
        # An older broker treats a path it doesn't know as an LLM request and would load a model
        # for GET /client; its /status lacks llm_model, so ask that first, once per process.
        if "llm_model" not in broker.status():
            raise DenError(
                f"the den on {core.remote_name()} predates remote clients; update it there and restart its broker"
            )
        _checked.add(broker.base_url)
    return broker


def info(caller):
    """What that den offers: {mode, llm: {model, unavailable}, tasks, image: the image spec}."""
    return client(caller).client_info()


def delegate(caller, task, instructions, text=None, files=()):
    """Run a task there; files are read here and sent under their name. {answer, stats, id}."""
    read = [{"path": Path(f["path"]).name, "content": f["content"]} for f in core.read_files(files)]
    return client(caller).delegate(task, instructions, text, read)


def _as_input(value):
    """A request's input image value with a file here turned into its name and bytes; a saved
    pose (pose:NAME) is the remote's own and stays as it is."""
    if image.saved_pose(value):
        return value
    kind, path = image.parse_reference(value)
    return {**image.file_object(path), **({"type": kind} if kind else {})}


def generate_image(caller, request):
    """Progress lines like BrokerClient.generate_image. The input images are files here, sent as
    bytes; the result's images come back as bytes and are saved here as a local den would, so
    its paths and copies are this machine's."""
    request = dict(request)
    out = request.pop("out", None)
    if request.get("image"):
        request["image"] = image.file_object(request["image"])
    if request.get("references"):
        request["references"] = [_as_input(r) for r in request["references"]]
    if (request.get("control") or {}).get("image"):
        request["control"] = {**request["control"], "image": _as_input(request["control"]["image"])}
    for msg in client(caller).generate_image(**request, bytes=True):
        if "result" in msg:
            msg = {**msg, "result": _save(msg["result"], request.get("prompt") or "", out)}
        yield msg


def _save(result, prompt, out):
    paths, copies = image.save([base64.b64decode(i) for i in result.pop("images")], prompt, out)
    maps = [image.save_map(paths[0], m["label"], base64.b64decode(m["base64"])) for m in result.pop("maps", [])]
    return {
        **result,
        "paths": [str(p) for p in paths],
        "copies": [str(p) for p in copies],
        **({"maps": [str(p) for p in maps]} if maps else {}),
    }


# Clips started from here: where each is to be copied, and where it was saved once fetched, so a
# second look at a finished clip doesn't save it again. Keyed by broker and clip id.
_clips = {}


def spoken_here(spec):
    """A voice request with its files read here (ADR 0010): an .srt path becomes the script, and a
    recording's path goes as bytes. A voice name is from the library there."""
    spec = dict(spec)
    srt = spec.get("srt")
    if srt and "\n" not in srt and srt.strip().lower().endswith(".srt"):
        spec["srt"] = Path(srt.strip()).expanduser().read_text(errors="replace")
    voice = spec.get("voice")
    if voice and Path(voice).expanduser().is_file():
        spec["voice"] = image.file_object(voice)
    if spec.get("audio") and not image.is_file_object(spec["audio"]):
        spec["audio"] = image.file_object(spec["audio"])
    return spec


def transcribe(caller, recording, language=None):
    """Progress lines of a transcription there, the recording here sent as bytes; the SRT is
    saved here next to the recording."""
    request = {"audio": image.file_object(recording), **({"language": language} if language else {})}
    for msg in client(caller).transcribe(**request):
        if "result" in msg:
            here = Path(recording).expanduser().with_suffix(".srt")
            here.write_text(msg["result"]["srt"])
            msg = {**msg, "result": {**msg["result"], "path": str(here)}}
        yield msg


def speak(caller, request):
    """Progress lines of a voice job there (ADR 0010), like BrokerClient.speak: the script and a
    recording are read here and sent as content; the track comes back as bytes and is saved here."""
    request = spoken_here(request)
    out = request.pop("out", None)
    for msg in client(caller).speak(**request, bytes=True):
        if "result" in msg:
            result = dict(msg["result"])
            spoken = request.get("text") or " ".join(request.get("lines") or []) or request.get("srt") or "voice"
            path, copies = speech.save(base64.b64decode(result.pop("audio")), spoken[:200], out)
            if result.get("srt"):
                path.with_suffix(".srt").write_text(result["srt"])
            msg = {**msg, "result": {**result, "path": str(path), "copies": [str(p) for p in copies]}}
        yield msg


def voices(caller):
    """The voice library there, its languages, and why speech can't run (or None)."""
    return client(caller).voices()


def add_voice(caller, name, recording, replace=False):
    """Keep a recording here as a voice there, sent as bytes."""
    return client(caller).add_voice(name, image.file_object(recording), replace)


def design_voice(caller, request):
    """Progress lines of designing a voice there; it's kept in that den's library."""
    return client(caller).design_voice(**request)


def remove_voice(caller, name):
    return client(caller).remove_voice(name)


def generate_clip(caller, request):
    """Start a clip there (a detached request, ADR 0009): keyframes are files here, sent as
    bytes. {id, estimate_s, summary}; get_clip fetches it once done."""
    request = dict(request)
    out = request.pop("out", None)
    request["keyframes"] = [
        {**keyframe, "image": image.file_object(keyframe["image"])} for keyframe in request.get("keyframes") or []
    ]
    if request.get("voiceover"):
        request["voiceover"] = spoken_here(request["voiceover"])
    broker = client(caller)
    started = broker.generate_clip(**request)
    _clips[(broker.base_url, started["id"])] = {"out": out}
    return started


def get_clip(caller, clip_id):
    """A clip there, as get_clip locally: once it's done its files come as bytes and are saved
    here, as a local den saves them, so the result's paths are this machine's."""
    broker = client(caller)
    view = broker.get_clip(clip_id)
    if view["state"] != "done":
        return view
    kept = _clips.setdefault((broker.base_url, int(clip_id)), {})
    if "result" not in kept:
        result = broker.get_clip(clip_id, with_bytes=True)["result"]
        path, sheet, copies = clip.save(
            base64.b64decode(result.pop("clip")),
            result.pop("suffix", ".mp4"),
            base64.b64decode(result["sheet"]) if result.get("sheet") else None,
            view.get("prompt") or "",
            kept.get("out"),
        )
        # The voice-over's own track stays there; the clip here carries it.
        result.pop("voiceover_track", None)
        kept["result"] = {**result, "path": str(path), "sheet": str(sheet) if sheet else None, "copies": [str(p) for p in copies]}
    return {**view, "result": kept["result"]}


def save_pose(caller, request):
    """Progress lines of drawing and saving a pose there, from a photo here sent as bytes.

    With draw_only=True in the request the pose is only drawn there and comes back as bytes,
    nothing is saved, and a second call with a name does the saving.
    """
    request = dict(request)
    photo = request.pop("image")
    return client(caller).save_pose(**request, image=image.file_object(photo), origin=Path(photo).name, bytes=True)
