"""MCP stdio server exposing the local toolchain to Claude Code or any other MCP client.

Stdlib only (newline-delimited JSON-RPC 2.0). The tool list follows config.toml, state.json
and the downloaded image models: when a mode, model or task switch or a download changes it,
the server sends notifications/tools/list_changed so clients refresh without a restart.

With DEN_BROKER set it serves a den on another machine instead (registered by hand as den-<name>),
as if den were only there: the tools, tasks, model, workflows and pose library come from that
broker and the delegations are logged there. Only delegated files are read here, and each
generated image is fetched here as well (ADR 0007).
"""

import json
import sys
import threading
import time

from den import clip, core, image, poses, remote, speech
from den.core import DenError

SERVER_INFO = {"name": f"den-{core.remote_name()}" if core.remote_name() else "den", "version": "0.1.0"}
FALLBACK_PROTOCOL = "2025-06-18"
WATCH_INTERVAL_S = 2
# A remote's tool list is read from its /status, over the network: often enough to follow a
# model switch there, not so often that the watch keeps that machine busy.
REMOTE_WATCH_INTERVAL_S = 15

_stdout_lock = threading.Lock()


def send(msg):
    with _stdout_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def local_llm_tool(model, tasks, remote=None):
    task_lines = "\n".join(f"- {name}: {task['description']}" for name, task in tasks.items())
    where = f"on your machine {remote}, through an encrypted tunnel" if remote else "on this machine"
    return {
        "name": "local_llm",
        "description": (
            f"Delegate a task to the local LLM ({model}) running {where}. It is free and "
            "private but much slower and weaker than you, so delegate ONLY these task types:\n"
            f"{task_lines}\n"
            "Pass file contents via `files` rather than `text` whenever possible: files are read "
            "locally and never enter your context, which is the right choice for private data and "
            "large inputs. Check the result before relying on it; a call can take minutes.\n"
            "How to ask: it never asks back and silently guesses at anything ambiguous, so be "
            "exact. Number list items and state the expected count and the allowed labels or "
            "format; ask for one fact per line and short output. When summarizing rules or "
            "procedures, don't cap the length: ask for every condition (only / unless / when / "
            "if) as its own bullet, since it drops conditions to fit a limit. It is reliable on content but "
            "weak at counting, totals, exact formats and length limits, so check those with a "
            "script. Record your verdict on each answer with local_llm_feedback."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "enum": list(tasks)},
                "instructions": {"type": "string", "description": "Exactly what to produce, and in what format."},
                "text": {"type": "string", "description": "Inline input text."},
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute paths of files to read locally as input.",
                },
            },
            "required": ["task", "instructions"],
        },
    }


def feedback_tool():
    return {
        "name": "local_llm_feedback",
        "description": (
            "Record your verdict on a local_llm answer, using the id from its footer. ok: correct; "
            "partly: usable after fixes; wrong: misleading or unusable; unchecked: not verified "
            "(private data, or low stakes). Add a short note saying what was wrong unless ok."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "verdict": {"type": "string", "enum": list(core.VERDICTS)},
                "note": {"type": "string"},
            },
            "required": ["id", "verdict"],
        },
    }


def release_tool(remote=None):
    if remote:
        return remote_release_tool(remote)
    return {
        "name": "release_resources",
        "description": (
            "Free this machine's CPU, RAM and GPU: unload the local LLM and stop the local image "
            "model. Call it before you start anything heavy here — a long test suite, a build, a "
            "benchmark — because a local model holds several GB of RAM and generates on the CPU "
            "as well as the GPU, so it competes with that work. It waits for local jobs already "
            "running (`now` cancels them instead), refuses new ones while it releases, and "
            "answers with what it freed and what the machine is doing. Afterwards den is still "
            "on: the next local_llm or generate_image call loads its model again, though den "
            "refuses to load one while the machine stays under heavy load. Quick and harmless "
            "when nothing is loaded, so just call it rather than checking first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "now": {
                    "type": "boolean",
                    "description": (
                        "Cancel local jobs that are still running instead of waiting for them. "
                        "Someone else may be waiting on one, so prefer waiting."
                    ),
                }
            },
        },
    }


def remote_release_tool(remote):
    return {
        "name": "release_resources",
        "description": (
            f"Free the CPU, RAM and GPU of {remote}, the machine this server's LLM runs on — not "
            f"this one. Work you run here doesn't compete with it, so don't call this before a "
            f"build or test run here; call it only when {remote} itself needs its resources back. "
            "It waits for jobs already running there (`now` cancels them), refuses new ones while "
            "it releases, and answers with what it freed and what that machine is doing. The "
            "next local_llm call loads the model again."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "now": {
                    "type": "boolean",
                    "description": "Cancel jobs still running there instead of waiting for them.",
                }
            },
        },
    }


def generate_image_tool(spec, remote_name=None):
    """spec: the image spec's description and parameters (image.request_spec, or a remote's)."""
    spec["parameters"]["properties"]["switch_back"] = {
        "type": "boolean",
        "description": "Stop the image model after this image so the LLM can load again.",
    }
    where = (
        f"This runs on {remote_name}, not on the machine you work on, but the files are this "
        f"machine's: the paths you pass (image, references, control, out) are files here, sent "
        f"to {remote_name} as bytes, and the result is saved here. A saved pose (pose:NAME) is "
        f"from {remote_name}'s pose library.\n\n"
        if remote_name
        else ""
    )
    return {
        "name": "generate_image",
        "description": (
            where + spec["description"] + "\n"
            "A small copy of the image comes back with the result, so look at it before you answer: "
            "say what you see, and when it misses the request, change one thing — prompt wording, a "
            "setting, a LoRA, the workflow — and reuse the seed so that change is the only "
            "difference. The saved file is the full-size one.\n"
            "The GPU holds either the local LLM or the image model: when the LLM is loaded or busy, "
            "the call waits for it and swaps, so a call can take minutes. The image model stays "
            "loaded afterwards; set switch_back on the last image of a batch to free the GPU for "
            "the LLM again, and leave it off while more images follow."
        ),
        "inputSchema": spec["parameters"],
    }


def clip_tools(spec, remote_name=None):
    """generate_clip and get_clip; spec is the clip spec (clip.request_spec, or a remote's)."""
    where = (
        f"This runs on {remote_name}; the keyframe paths you pass are files here, sent as bytes, "
        "and the clip is saved here once get_clip sees it done.\n\n"
        if remote_name
        else ""
    )
    return [
        {
            "name": "generate_clip",
            "description": (
                where + "Start a short video clip. It takes minutes, so this returns at once with the "
                "clip's id and an estimate; call get_clip with that id to wait for it and see it. "
                "The clip keeps going if you do other work meanwhile. For a person or a scene that "
                "has to look a certain way, make the start as an image first (generate_image), get "
                "it approved, and pass it as the first keyframe.\n" + spec["description"]
            ),
            "inputSchema": spec["parameters"],
        },
        {
            "name": "get_clip",
            "description": (
                "Wait for a clip started with generate_clip and return it: its path and a contact "
                "sheet (four frames, first to last, in a 2x2 grid) to look at, since you can't "
                "watch the video. Waits up to `wait` seconds (default 60) and then reports how far "
                "it is; call again to keep waiting. Look at the sheet before you answer: say what "
                "you see and whether the motion went where the prompt asked. `cancel` drops it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "The id generate_clip returned."},
                    "wait": {"type": "integer", "description": "Seconds to wait for it, 0–300. Default 60."},
                    "cancel": {"type": "boolean", "description": "Cancel the clip instead of waiting."},
                },
                "required": ["id"],
            },
        },
    ]


def list_tools():
    if core.remote_name():
        return remote_tools(core.remote_name())
    config = core.load_config()
    state = core.load_state(config)
    # Releasing frees the machine instead of using it, so it's listed whatever the mode, the
    # model or the workflows are — and a constant entry keeps the list from flapping.
    tools = [release_tool()]
    if core.llm_unavailable(config, state) is None:
        tasks = core.enabled_tasks(config, state)
        if tasks:
            tools.append(local_llm_tool(state["llm_model"], tasks))
            tools.append(feedback_tool())
    if image.unavailable(config, state) is None:
        # Availability follows the model files, so a finished download adds its workflow.
        flows = image.available(config)
        if flows:
            spec = image.request_spec(config, flows, image.settings(config).get("default_workflow"))
            tools.append(generate_image_tool(spec))
            # The pose tools' text never lists the library, so a save doesn't change the tools.
            for name, spec in image.pose_tool_specs(config).items():
                tools.append({"name": name, "description": spec["description"], "inputSchema": spec["parameters"]})
    if clip.unavailable(config, state) is None:
        tools += clip_tools(clip.request_spec(config, clip.available(config), clip.settings(config).get("default_workflow")))
    # Speech runs on the image side (ADR 0010): listed when that side can run and speech is installed.
    if image.unavailable(config, state) is None and speech.unavailable() is None:
        spec = speech.request_spec()
        tools.append({"name": "generate_voice", "description": spec["description"], "inputSchema": spec["parameters"]})
    return tools


def remote_tools(name):
    """The tools of the den on another machine, all built from what its broker reports. An
    unreachable one leaves the release listed, as a local den that's down would."""
    tools = [release_tool(name)]
    try:
        offered = remote.info("claude")
    except DenError:
        return tools
    if offered["llm"]["unavailable"] is None and offered["tasks"]:
        tools.append(local_llm_tool(offered["llm"]["model"], offered["tasks"], name))
        tools.append(feedback_tool())
    spec = offered["image"]
    if spec["image_on"] and spec["workflows"]:
        tools.append(generate_image_tool(spec, name))
        for tool, pose_spec in spec["pose_tools"].items():
            note = f"The pose library is on {name}; the photo you pass is a file here, sent as bytes. " if tool == "save_pose" else ""
            tools.append({"name": tool, "description": note + pose_spec["description"], "inputSchema": pose_spec["parameters"]})
    clips = offered.get("clip")  # an older broker offers none
    if clips and clips["clip_on"] and clips["workflows"]:
        tools += clip_tools(clips, name)
    return tools


def progress_text(msg):
    """A short line for a broker progress message, or None for the result.

    An image request's `waiting` carries the reason it waits; a release's carries the requests
    it waits for.
    """
    if "waiting" in msg:
        waiting = msg["waiting"]
        if isinstance(waiting, dict):
            return f"waiting: {waiting['reason']}"
        return f"waiting for {len(waiting)} running local job(s) to finish"
    if "cancelling" in msg:
        return f"cancelling {len(msg['cancelling'])} running local job(s)"
    if "unloading" in msg:
        return f"unloading the LLM ({', '.join(msg['unloading'])})"
    if "starting" in msg or "stopping" in msg:
        return f"{'starting' if 'starting' in msg else 'stopping'} {msg.get('starting') or msg.get('stopping')}"
    if "speaking" in msg:
        s = msg["speaking"]
        return f"speaking line {s['line']} of {s['of']}"
    if "drawing" in msg:
        return f"drawing the pose for {msg['drawing']['name']}"
    if "generating" in msg:
        g = msg["generating"]
        return f"{'editing' if g.get('edit') else 'generating'} with {g['workflow']} (seed {g['seed']})"
    return None


def notify(progress_token, step, text):
    """One progress notification. It also keeps the client's idle timeout from firing on long waits."""
    send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {"progressToken": progress_token, "progress": step, "message": text},
        }
    )


def machine_line(now=None):
    """What the machine is doing, for a caller deciding whether it has room for its own work."""
    now = now or core.pressure()
    load = (
        f"load {now['load']:.1f} on {now['cpus']} cpus ({now['load_per_cpu']:.2f} per cpu)"
        if now["load"] is not None
        else "load unknown"
    )
    ram = f"{now['free_ram_gb']:.1f} GB RAM available" if now["free_ram_gb"] is not None else "available RAM unknown"
    return f"[machine: {load}, {ram}]"


def release_resources(args, progress_token):
    """Release both sides, so the CPU, RAM and GPU are free for whatever the caller does next."""
    config = core.load_config()
    freed, step = None, 0
    for msg in core.broker(config, "claude").unload_sides(now=bool(args.get("now"))):
        if "unloaded" in msg:
            freed = msg["unloaded"]
        elif progress_token is not None and (text := progress_text(msg)):
            step += 1
            notify(progress_token, step, text)
    if freed is None:
        raise DenError("the broker ended the release without saying what it freed")
    what = (
        f"released: {', '.join(freed)}; that CPU, RAM and GPU are free now"
        if freed
        else "nothing was loaded: den held no CPU, RAM or GPU"
    )
    # A remote's machine is the one released, so its broker says what that machine is doing.
    now = core.broker(config, "claude").status()["pressure"] if core.remote_name() else None
    return f"{what}\n{machine_line(now)}"


def generate_image(args, progress_token):
    config = core.load_config()
    keys = ("prompt", "workflow", "negative", "seed", "size", "image", "out", "switch_back", *image.SETTINGS, "loras", "references", "control", "upscale", "strength", "save_maps")
    # Ask for a small copy of the image: a text path can't be judged, and the next call's
    # prompt, seed or settings depend on what came out (the saved file stays a full-size PNG).
    request = {k: args[k] for k in keys if args.get(k) is not None} | {"preview": True}
    where = core.remote_name()
    stream = remote.generate_image("claude", request) if where else core.broker(config, "claude").generate_image(**request)
    result, step = None, 0
    for msg in stream:
        if "result" in msg:
            result = msg["result"]
        elif progress_token is not None and (text := progress_text(msg)):
            step += 1
            notify(progress_token, step, text)
    if result is None:
        raise DenError("the broker ended the image request without a result")
    lines = [f"saved: {p}" for p in result["paths"]] + [f"copied to: {p}" for p in result["copies"]]
    lines += [f"map: {p}" for p in result.get("maps", [])]
    waited = f", waited {result['waited_s']:.0f}s" if result["waited_s"] >= 1 else ""
    parts = [*result["summary"], f"{result['seconds']}s{waited}"]
    lines.append(f"[{' · '.join(parts)}]")
    if result.get("switched_back"):
        lines.append("ComfyUI stopped; the GPU is free for the LLM.")
    shown = result.get("preview")
    content = [{"type": "text", "text": "\n".join(lines)}]
    if shown:
        content.append({"type": "image", "data": shown["base64"], "mimeType": shown["mime"]})
    return content


def generate_voice(args, progress_token):
    config = core.load_config()
    keys = ("srt", "text", "voice", "language", "exaggeration", "cfg_weight", "temperature", "seed", "out")
    request = {k: args[k] for k in keys if args.get(k) is not None}
    result, step = None, 0
    for msg in core.broker(config, "claude").speak(**request):
        if "result" in msg:
            result = msg["result"]
        elif progress_token is not None and (text := progress_text(msg)):
            step += 1
            notify(progress_token, step, text)
    if result is None:
        raise DenError("the broker ended the voice request without a result")
    lines = [f"saved: {result['path']}"] + [f"copied to: {p}" for p in result["copies"]]
    lines += [f"note: {note}" for note in result["notes"]]
    waited = f", waited {result['waited_s']:.0f}s" if result["waited_s"] >= 1 else ""
    lines.append(f"[{' · '.join([*result['summary'], f'{result['seconds']}s{waited}'])}]")
    return "\n".join(lines)


CLIP_POLL_S = 3


def generate_clip(args):
    config = core.load_config()
    keys = ("prompt", "workflow", "negative", "seed", "size", "duration", "sound", "voiceover", "keyframes", *image.SETTINGS, "loras", "out")
    # A small copy of the contact sheet comes back with the finished clip, for get_clip to show.
    request = {k: args[k] for k in keys if args.get(k) is not None} | {"preview": True}
    where = core.remote_name()
    started = remote.generate_clip("claude", request) if where else core.broker(config, "claude").generate_clip(**request)
    return (
        f"clip {started['id']} started: {' · '.join(started['summary'])}; "
        f"{_time_left(started['estimate_s'])} by earlier clips.\n"
        f"Call get_clip with id {started['id']} to wait for it."
    )


def _time_left(seconds):
    if seconds is None:
        return "no estimate yet (the first clip of this kind)"
    return "under a minute" if seconds < 60 else f"about {round(seconds / 60)} min"


def get_clip(args, progress_token):
    config = core.load_config()
    where = core.remote_name()
    broker = remote.client("claude") if where else core.broker(config, "claude")
    clip_id = args.get("id")
    if clip_id is None:
        raise DenError("get_clip needs the id generate_clip returned")
    if args.get("cancel"):
        done = broker.cancel_clip(clip_id)
        return f"clip {clip_id}: {'cancelling' if done['state'] in ('waiting', 'running') else done['state']}"
    wait = max(0, min(300, int(args.get("wait", 60))))
    deadline, step, last = time.time() + wait, 0, None
    while True:
        view = remote.get_clip("claude", clip_id) if where else broker.get_clip(clip_id)
        state = view["state"]
        if state == "running":
            text = f"making the clip: {view['running_s']}s in, {_time_left(view.get('left_s'))} left"
        elif state == "waiting":
            text = progress_text(view["progress"]) if view.get("progress") else None
            text = text or "waiting for the image side"
        else:
            text = None
        if text is None or time.time() >= deadline:
            break
        if progress_token is not None and text != last:
            step += 1
            notify(progress_token, step, text)
            last = text
        time.sleep(CLIP_POLL_S)
    if state in ("waiting", "running"):
        return f"clip {clip_id} is still {state}: {text}. Call get_clip again to keep waiting."
    if state != "done":
        return f"clip {clip_id} {state}: {view.get('error', 'no reason given')}"
    r = view["result"]
    lines = [f"saved: {r['path']}"] + [f"copied to: {p}" for p in r["copies"]]
    if r.get("sheet"):
        lines.append(f"contact sheet: {r['sheet']}")
    if r.get("voiceover_track"):
        lines.append(f"voice-over track: {r['voiceover_track']}")
    lines += [f"note: {note}" for note in r.get("notes") or []]
    waited = f", waited {r['waited_s']:.0f}s" if r["waited_s"] >= 1 else ""
    lines.append(f"[{' · '.join([*r['summary'], str(r['seconds']) + 's' + waited])}]")
    content = [{"type": "text", "text": "\n".join(lines)}]
    shown = r.get("preview")
    if shown:
        content.append({"type": "image", "data": shown["base64"], "mimeType": shown["mime"]})
    return content


def save_pose(args, progress_token):
    config = core.load_config()
    request = {k: args[k] for k in ("image", "name", "description", "replace") if args.get(k) is not None}
    # A small copy of the skeleton, so a missing limb shows before the pose is relied on.
    where = core.remote_name()
    if where:
        stream = remote.save_pose("claude", {**request, "preview": True})
    else:
        stream = core.broker(config, "claude").save_pose(**request, preview=True)
    result, step = None, 0
    for msg in stream:
        if "result" in msg:
            result = msg["result"]
        elif progress_token is not None and (text := progress_text(msg)):
            step += 1
            notify(progress_token, step, text)
    if result is None:
        raise DenError("the broker ended the pose request without a result")
    lines = [
        f"saved pose {result['name']} ({result['aspect']}, {result['width']}x{result['height']}), "
        f"use it as pose:{result['name']}",
        # A remote's library is there and names no file; this machine's shows where each one is.
        *(
            [f"in {where}'s pose library"]
            if where
            else [
                f"skeleton: {result['map']}",
                f"photo: {result['source']}",
                f"keypoints: {result.get('keypoints') or '(not recorded)'}",
            ]
        ),
        "",
        "Pose library, updated:",
        result["toc"],
    ]
    content = [{"type": "text", "text": "\n".join(lines)}]
    shown = result.get("preview")
    if shown:
        content.append({"type": "image", "data": shown["base64"], "mimeType": shown["mime"]})
    return content


def list_poses(name=None):
    """The pose library's table of contents, or one pose: its details, then small copies of the
    skeleton and the photo to look at. A remote's library is its broker's."""
    if core.remote_name():
        found = remote.client("claude").poses(name)
    else:
        found = poses.show(name) if name else {"toc": poses.toc()}
    if not name:
        return found["toc"]
    images = [{"type": "image", "data": i["base64"], "mimeType": i["mime"]} for i in found["images"]]
    return [{"type": "text", "text": found["details"]}, *images]


def call_tool(req_id, params):
    name, args = params.get("name"), params.get("arguments") or {}
    try:
        if name == "local_llm":
            task_args = (args.get("task"), args.get("instructions", ""), args.get("text"), args.get("files") or ())
            if core.remote_name():
                # Run and logged there; the files are read here and sent as their contents.
                done = remote.delegate("claude", *task_args)
                answer, stats, call_id = done["answer"], done["stats"], done["id"]
            else:
                config = core.load_config()
                answer, stats = core.run_task(config, core.load_state(config), *task_args, caller="claude")
                call_id = core.log_delegation(stats)
            footer = (
                f"\n\n[{core.remote_name() or 'local'}: {stats['model']}, {stats['input_tokens']} in / "
                f"{stats['output_tokens']} out, {stats['seconds']}s, id {call_id}]"
            )
            text = answer + footer
        elif name == "local_llm_feedback":
            if core.remote_name():
                remote.client("claude").feedback(args.get("id"), args.get("verdict"), args.get("note") or "")
            else:
                core.record_feedback(args.get("id"), args.get("verdict"), args.get("note") or "")
            text = f"recorded {args.get('verdict')} for delegation {args.get('id')}"
        elif name == "release_resources":
            text = release_resources(args, (params.get("_meta") or {}).get("progressToken"))
        elif name == "generate_image":
            text = generate_image(args, (params.get("_meta") or {}).get("progressToken"))
        elif name == "generate_clip":
            text = generate_clip(args)
        elif name == "generate_voice":
            text = generate_voice(args, (params.get("_meta") or {}).get("progressToken"))
        elif name == "get_clip":
            text = get_clip(args, (params.get("_meta") or {}).get("progressToken"))
        elif name == "save_pose":
            text = save_pose(args, (params.get("_meta") or {}).get("progressToken"))
        elif name == "list_poses":
            text = list_poses(args.get("name"))
        else:
            raise DenError(f"unknown tool {name!r}")
        content = text if isinstance(text, list) else [{"type": "text", "text": text}]
        result = {"content": content}
    except DenError as e:
        result = {"content": [{"type": "text", "text": f"den: {e}"}], "isError": True}
    except Exception as e:  # never let one call kill the server
        result = {"content": [{"type": "text", "text": f"den internal error: {e!r}"}], "isError": True}
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def watch_tools():
    def signature():
        try:
            return json.dumps(list_tools(), sort_keys=True)
        except DenError as e:
            return str(e)

    last = signature()
    interval = REMOTE_WATCH_INTERVAL_S if core.remote_name() else WATCH_INTERVAL_S
    while True:
        time.sleep(interval)
        current = signature()
        if current != last:
            last = current
            send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})


def handle(msg):
    method, req_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if req_id is None:
        return  # notification (initialized, cancelled, ...): nothing to answer
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", FALLBACK_PROTOCOL),
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": SERVER_INFO,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        try:
            result = {"tools": list_tools()}
        except DenError as e:
            send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}})
            return
    elif method == "tools/call":
        # Local generation and swaps take minutes; run them off the read loop so pings still get answered.
        threading.Thread(target=call_tool, args=(req_id, params), daemon=True).start()
        return
    else:
        send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"method not found: {method}"}})
        return
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def main():
    threading.Thread(target=watch_tools, daemon=True).start()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        handle(msg)
