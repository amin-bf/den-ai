"""MCP stdio server exposing the local toolchain to Claude Code or any other MCP client.

Stdlib only (newline-delimited JSON-RPC 2.0). The tool list follows config.toml, state.json
and the downloaded image models: when a mode, model or task switch or a download changes it,
the server sends notifications/tools/list_changed so clients refresh without a restart.
"""

import json
import sys
import threading
import time

from den import core, image
from den.core import DenError

SERVER_INFO = {"name": "den", "version": "0.1.0"}
FALLBACK_PROTOCOL = "2025-06-18"
WATCH_INTERVAL_S = 2

_stdout_lock = threading.Lock()


def send(msg):
    with _stdout_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def local_llm_tool(model, tasks):
    task_lines = "\n".join(f"- {name}: {task['description']}" for name, task in tasks.items())
    return {
        "name": "local_llm",
        "description": (
            f"Delegate a task to the local LLM ({model}) running on this machine. It is free and "
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


def release_tool():
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


def generate_image_tool(config, flows, default):
    spec = image.request_spec(config, flows, default)
    spec["parameters"]["properties"]["switch_back"] = {
        "type": "boolean",
        "description": "Stop the image model after this image so the LLM can load again.",
    }
    return {
        "name": "generate_image",
        "description": (
            spec["description"] + "\n"
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


def list_tools():
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
            tools.append(generate_image_tool(config, flows, image.settings(config).get("default_workflow")))
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
        return f"{'starting' if 'starting' in msg else 'stopping'} ComfyUI"
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
    return f"{what}\n{machine_line()}"


def generate_image(args, progress_token):
    config = core.load_config()
    keys = ("prompt", "workflow", "negative", "seed", "size", "image", "out", "switch_back", *image.SETTINGS, "loras", "references", "control", "upscale")
    # Ask for a small copy of the image: a text path can't be judged, and the next call's
    # prompt, seed or settings depend on what came out (the saved file stays a full-size PNG).
    request = {k: args[k] for k in keys if args.get(k) is not None} | {"preview": True}
    result, step = None, 0
    for msg in core.broker(config, "claude").generate_image(**request):
        if "result" in msg:
            result = msg["result"]
        elif progress_token is not None and (text := progress_text(msg)):
            step += 1
            notify(progress_token, step, text)
    if result is None:
        raise DenError("the broker ended the image request without a result")
    lines = [f"saved: {p}" for p in result["paths"]] + [f"copied to: {p}" for p in result["copies"]]
    size = f" · {result['width']}x{result['height']}" if result.get("width") else ""
    size += "".join(f" · {k} {result[k]}" for k in image.SETTINGS if k in result)
    size += "".join(f" · lora {l['name']} {l['strength']}" for l in result.get("loras", []))
    size += f" · {result['references']} reference(s)" if result.get("references") else ""
    size += f" · control {image.control_summary(result['control'])}" if result.get("control") else ""
    size += f" · upscale {result['upscale']['name']} x{result['upscale']['factor']:g}" if result.get("upscale") else ""
    waited = f", waited {result['waited_s']:.0f}s" if result["waited_s"] >= 1 else ""
    lines.append(f"[{result['workflow']} · seed {result['seed']}{size} · {result['seconds']}s{waited}]")
    if result.get("switched_back"):
        lines.append("ComfyUI stopped; the GPU is free for the LLM.")
    shown = result.get("preview")
    content = [{"type": "text", "text": "\n".join(lines)}]
    if shown:
        content.append({"type": "image", "data": shown["base64"], "mimeType": shown["mime"]})
    return content


def call_tool(req_id, params):
    name, args = params.get("name"), params.get("arguments") or {}
    try:
        if name == "local_llm":
            config = core.load_config()
            state = core.load_state(config)
            answer, stats = core.run_task(
                config, state, args.get("task"), args.get("instructions", ""), args.get("text"), args.get("files") or (),
                caller="claude",
            )
            call_id = core.log_delegation(stats)
            footer = (
                f"\n\n[local: {stats['model']}, {stats['input_tokens']} in / {stats['output_tokens']} out, "
                f"{stats['seconds']}s, id {call_id}]"
            )
            text = answer + footer
        elif name == "local_llm_feedback":
            core.record_feedback(args.get("id"), args.get("verdict"), args.get("note") or "")
            text = f"recorded {args.get('verdict')} for delegation {args.get('id')}"
        elif name == "release_resources":
            text = release_resources(args, (params.get("_meta") or {}).get("progressToken"))
        elif name == "generate_image":
            text = generate_image(args, (params.get("_meta") or {}).get("progressToken"))
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
    while True:
        time.sleep(WATCH_INTERVAL_S)
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
