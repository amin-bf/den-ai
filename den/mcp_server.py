"""MCP stdio server exposing the local toolchain to Claude Code or any other MCP client.

Stdlib only (newline-delimited JSON-RPC 2.0). The tool list follows config.toml and
state.json: when a mode, model or task switch changes it, the server sends
notifications/tools/list_changed so clients refresh without a restart.
"""

import json
import sys
import threading
import time

from den import core
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


def list_tools():
    config = core.load_config()
    state = core.load_state(config)
    tools = []
    if core.llm_on(state):
        tasks = core.enabled_tasks(config, state)
        if tasks and state["llm_model"]:
            tools.append(local_llm_tool(state["llm_model"], tasks))
            tools.append(feedback_tool())
    return tools


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
        # Local generation takes minutes; run it off the read loop so pings still get answered.
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
