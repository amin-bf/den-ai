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
    tools = []
    if core.llm_on(state):
        tasks = core.enabled_tasks(config, state)
        if tasks and state["llm_model"]:
            tools.append(local_llm_tool(state["llm_model"], tasks))
            tools.append(feedback_tool())
    if core.image_on(state):
        # Availability follows the model files, so a finished download adds its workflow.
        flows = image.available(config)
        if flows:
            tools.append(generate_image_tool(config, flows, image.settings(config).get("default_workflow")))
    return tools


def progress_text(msg):
    """A short line for an image request's progress message, or None for the result."""
    if "waiting" in msg:
        return f"waiting: {msg['waiting']['reason']}"
    if "unloading" in msg:
        return f"unloading the LLM ({', '.join(msg['unloading'])})"
    if "starting" in msg or "stopping" in msg:
        return f"{'starting' if 'starting' in msg else 'stopping'} ComfyUI"
    if "generating" in msg:
        g = msg["generating"]
        return f"{'editing' if g.get('edit') else 'generating'} with {g['workflow']} (seed {g['seed']})"
    return None


def generate_image(args, progress_token):
    config = core.load_config()
    keys = ("prompt", "workflow", "negative", "seed", "size", "image", "out", "switch_back", *image.SETTINGS, "loras", "references", "control", "upscale")
    request = {k: args[k] for k in keys if args.get(k) is not None}
    result, step = None, 0
    for msg in core.broker(config, "claude").generate_image(**request):
        if "result" in msg:
            result = msg["result"]
        elif progress_token is not None and (text := progress_text(msg)):
            # Progress also keeps the client's idle timeout from firing during long waits.
            step += 1
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {"progressToken": progress_token, "progress": step, "message": text},
                }
            )
    if result is None:
        raise DenError("the broker ended the image request without a result")
    lines = [f"saved: {p}" for p in result["paths"]] + [f"copied to: {p}" for p in result["copies"]]
    size = f" · {result['width']}x{result['height']}" if result.get("width") else ""
    size += "".join(f" · {k} {result[k]}" for k in image.SETTINGS if k in result)
    size += "".join(f" · lora {l['name']} {l['strength']}" for l in result.get("loras", []))
    size += f" · {result['references']} reference(s)" if result.get("references") else ""
    size += f" · control {result['control']['type']} {result['control']['strength']}" if result.get("control") else ""
    size += f" · upscale {result['upscale']['name']} x{result['upscale']['factor']:g}" if result.get("upscale") else ""
    waited = f", waited {result['waited_s']:.0f}s" if result["waited_s"] >= 1 else ""
    lines.append(f"[{result['workflow']} · seed {result['seed']}{size} · {result['seconds']}s{waited}]")
    if result.get("switched_back"):
        lines.append("ComfyUI stopped; the GPU is free for the LLM.")
    return "\n".join(lines)


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
        elif name == "generate_image":
            text = generate_image(args, (params.get("_meta") or {}).get("progressToken"))
        else:
            raise DenError(f"unknown tool {name!r}")
        result = {"content": [{"type": "text", "text": text}]}
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
