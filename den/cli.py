"""`den` — switch modes, models and delegated tasks; run a task from the shell."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from den import core, image
from den.core import DenError


def _load():
    config = core.load_config()
    return config, core.load_state(config)


def _gb(n):
    return f"{n / 1e9:.1f} GB"


def _describe(r):
    model = f" {r['model']}" if r["model"] else ""
    return f"#{r['id']} {r['caller']} {r['request']}{model} ({r['seconds']:.0f}s)"


def cmd_status(args):
    config, state = _load()
    if core.is_on(state):
        # The reason a side isn't ready belongs on its own line below, which already carries it.
        sides = {"llm": core.llm_unavailable(config, state), "image": image.unavailable(config, state)}
        ready = ", ".join(f"{side}: {'ready' if why is None else 'not ready'}" for side, why in sides.items())
        print(f"mode     on  ({ready})")
    else:
        print(f"mode     off  ({core.OFF_MESSAGE})")

    model = state["llm_model"] or "(none selected: den model)"
    client = core.broker(config, "cli")
    try:
        status = client.status()
    except DenError as e:
        print(f"broker   DOWN: {e}")
        print(f"llm      {model}")
        _print_pressure(config, {})
        _print_tasks(config, state)
        return 1

    loaded = status["loaded"] or "nothing"
    print(f"broker   up at {client.base_url}, pid {status['pid']}; GPU side: {loaded}")
    if status["pending_mode"]:
        print(f"         turning den {status['pending_mode']}")
    if status.get("releasing"):
        sides = " and ".join(status["releasing"])
        print(f"         releasing the {sides} side (den unload); its requests are refused meanwhile")
    if status["swapping"]:
        print(f"         swapping ({status['swapping']})")
    for r in status["inflight"]:
        print(f"         running {_describe(r)}")
    for r in status["waiting"]:
        print(f"         waiting {_describe(r)}")
    upstream = status["ollama"]
    down = "error" in upstream
    if down:
        print(f"llm      {model}")
        print(f"ollama   DOWN: {upstream['error']}")
    else:
        version = upstream["version"]
        pulled = core.model_tag(model) in client.installed()
        loaded = {core.model_tag(m["name"]): m for m in upstream["loaded"]}
        m = loaded.get(core.model_tag(model))
        where = f"loaded, {_gb(m['size_vram'])} of {_gb(m['size'])} on GPU" if m else "not loaded"
        if state["llm_model"]:
            print(f"llm      {model}  ({'pulled' if pulled else 'NOT PULLED'}, {where})")
        else:
            print(f"llm      {model}")
        others = [n for n in loaded if n != core.model_tag(model)]
        print(f"ollama   {version} at {upstream['url']}" + (f"; also loaded: {', '.join(others)}" if others else ""))

    comfy = status.get("comfyui")
    runnable = image.available(config)
    if comfy is None:
        print("image    not configured")
    else:
        where = "up" if comfy["up"] else "stopped"
        if comfy["idle_s"] is not None:
            where += f", idle {comfy['idle_s'] // 60} min"
        print(f"image    comfyui {where} at {comfy['url']}; workflows: {', '.join(runnable) or 'none can run (den image)'}")
    _print_pressure(config, status)
    _print_tasks(config, state)
    # Non-zero while Ollama is down, so a shell check or a notifier can watch this.
    return 1 if down else 0


def _print_pressure(config, status):
    """What the machine is doing, and whether that holds off loading a side (`[limits]`)."""
    now = status.get("pressure") or core.pressure()
    max_load, min_ram = core.limits(config)
    load = f"load {now['load']:.1f} on {now['cpus']} cpus ({now['load_per_cpu']:.2f}/cpu)" if now["load"] is not None else "load unknown"
    ram = f"{now['free_ram_gb']:.1f} GB RAM free" if now["free_ram_gb"] is not None else "free RAM unknown"
    limits = ", ".join([f"{max_load}/cpu" if max_load else "no load limit", f"{min_ram} GB" if min_ram else "no RAM limit"])
    print(f"machine  {load}, {ram}  (limits: {limits})")
    busy = core.too_busy(config, now)
    if busy:
        print(f"         too busy to load a side that isn't loaded: {busy}")


def _print_tasks(config, state):
    tasks = core.all_tasks(config, state)
    print("tasks    " + ", ".join(f"{n} {'on' if enabled else 'off'}" for n, (_, enabled) in tasks.items()))


def cmd_mode(args):
    config, state = _load()
    if args.mode is None:
        print(state["mode"])
        return
    # The broker does the switch: turning den off refuses new requests, waits for the running
    # ones and unloads both sides before saving the mode. Ctrl+C drops the switch.
    try:
        for msg in core.broker(config, "cli").switch_mode(args.mode, args.now):
            if "waiting" in msg or "cancelling" in msg:
                running = msg.get("waiting") or msg["cancelling"]
                requests = f"{len(running)} running request{'s' if len(running) > 1 else ''}"
                print(
                    f"cancelling {requests} for the side being turned off:"
                    if "cancelling" in msg
                    else f"waiting for {requests} to finish before turning den off "
                    "(--now cancels them); new ones are refused meanwhile:",
                    flush=True,
                )
                for r in running:
                    print(f"  {_describe(r)}", flush=True)
            elif _print_swap_step(msg):
                pass
            elif "mode" in msg:
                print(f"mode: {msg['mode']}")
    except KeyboardInterrupt:
        print("\nmode switch dropped, unless it was already unloading (check: den status)", file=sys.stderr)
        return 130


def cmd_unload(args):
    config, _ = _load()
    # Unlike `den mode off` this leaves the mode alone: both sides stay available, so the tools
    # stay listed and the next request loads its side again. New requests are only refused while
    # the release runs, since whoever asked for the machine back is about to use it.
    try:
        for msg in core.broker(config, "cli").unload_sides([args.side] if args.side else [], args.now):
            if "waiting" in msg or "cancelling" in msg:
                running = msg.get("waiting") or msg["cancelling"]
                requests = f"{len(running)} running request{'s' if len(running) > 1 else ''}"
                print(
                    f"cancelling {requests} before releasing:"
                    if "cancelling" in msg
                    else f"waiting for {requests} to finish before releasing "
                    "(--now cancels them); new ones are refused meanwhile:",
                    flush=True,
                )
                for r in running:
                    print(f"  {_describe(r)}", flush=True)
            elif _print_swap_step(msg):
                pass
            elif "unloaded" in msg:
                print(f"released: {', '.join(msg['unloaded']) or 'nothing was loaded'}")
    except KeyboardInterrupt:
        print("\nrelease dropped, unless it was already unloading (check: den status)", file=sys.stderr)
        return 130


def _print_swap_step(msg):
    """Print an unloading / starting / stopping progress line; False for other messages."""
    if "unloading" in msg:
        print(f"unloading the LLM ({', '.join(msg['unloading'])}) ...", flush=True)
    elif "starting" in msg:
        print(f"starting {msg['starting']} (ComfyUI) ...", flush=True)
    elif "stopping" in msg:
        print(f"stopping {msg['stopping']} (ComfyUI) ...", flush=True)
    else:
        return False
    return True


def _lora_arg(value):
    name, _, strength = value.partition(":")
    try:
        return {"name": name, **({"strength": float(strength)} if strength else {})}
    except ValueError:
        raise DenError(f"--lora takes NAME or NAME:STRENGTH, got {value!r}") from None


def _control_arg(args):
    if not args.control:
        return None
    if not args.control_type:
        raise DenError("--control needs --control-type (den image lists each workflow's types)")
    control = {"image": str(args.control.expanduser().resolve()), "type": args.control_type}
    if args.control_strength is not None:
        control["strength"] = args.control_strength
    return control


def _upscale_arg(value):
    if not value:
        return None
    name, _, factor = value.partition(":")
    try:
        return {"name": name, **({"factor": float(factor)} if factor else {})}
    except ValueError:
        raise DenError(f"--upscale takes NAME or NAME:FACTOR, got {value!r}") from None


def cmd_image(args):
    config, state = _load()
    if args.json:
        # For callers that build their own tool from it (pi's extension): the workflows that can
        # run now, with the description and parameter schema Claude's MCP tool uses too.
        default = image.settings(config).get("default_workflow")
        flows = image.available(config)
        spec = image.request_spec(config, flows, default) if flows else {"description": None, "parameters": None}
        unavailable = image.unavailable(config, state)
        listing = {
            "broker": core.broker_url(config),
            "mode": state["mode"],
            "image_on": unavailable is None,
            "unavailable": unavailable,
            "default": default,
        }
        print(json.dumps({**listing, "workflows": list(flows), "edits": [n for n, wf in flows.items() if "edit" in wf], **spec}))
        return
    if args.prompt is None:
        default = image.settings(config).get("default_workflow")
        flows = image.check_workflows(config)
        if not flows:
            print("no workflows configured ([image.workflows.<name>] in config.toml)")
        for name, (wf, problem) in flows.items():
            marker = "*" if name == default else " "
            edits = " [edits: takes --image]" if "edit" in wf else ""
            print(f"{marker} {name:<16} {image.describe(wf)}{edits}")
            if problem:
                print(f"  {'':<16} CAN'T RUN: {problem}")
                continue
            for line in image.describe_options(config, name, wf):
                print(f"  {'':<16} {line}")
        ups = image.describe_upscalers(config)
        if ups:
            print("\nupscalers (--upscale NAME[:FACTOR], any workflow):")
            for line in ups:
                print(f"  {line}")
        print(f"\n{image.PROMPT_SYNTAX}")
        return
    request = {
        "prompt": args.prompt,
        "workflow": args.workflow,
        "negative": args.negative,
        "seed": args.seed,
        "size": args.size,
        "image": str(args.image.expanduser().resolve()) if args.image else None,
        "out": str(args.out.expanduser().resolve()) + ("/" if str(args.out).endswith("/") else "") if args.out else None,
        "switch_back": args.switch_back,
        "steps": args.steps,
        "cfg": args.cfg,
        "sampler": args.sampler,
        "scheduler": args.scheduler,
        "loras": [_lora_arg(value) for value in args.lora] or None,
        "references": [str(path.expanduser().resolve()) for path in args.reference] or None,
        "control": _control_arg(args),
        "upscale": _upscale_arg(args.upscale),
    }
    try:
        for msg in core.broker(config, "cli").generate_image(**{k: v for k, v in request.items() if v is not None}):
            if "waiting" in msg:
                print(f"waiting: {msg['waiting']['reason']}", flush=True)
                for r in msg["waiting"].get("running", []):
                    print(f"  {_describe(r)}", flush=True)
            elif _print_swap_step(msg):
                pass
            elif "generating" in msg:
                g = msg["generating"]
                size = f", {g['width']}x{g['height']}" if g["width"] else ""
                what = "editing" if g.get("edit") else "generating"
                extra = "".join(f", {k} {g[k]}" for k in image.SETTINGS if k in g)
                extra += "".join(f", lora {l['name']} {l['strength']}" for l in g.get("loras", []))
                extra += f", {g['references']} reference(s)" if g.get("references") else ""
                extra += f", control {g['control']['type']} {g['control']['strength']}" if g.get("control") else ""
                extra += f", upscale {g['upscale']['name']} x{g['upscale']['factor']:g}" if g.get("upscale") else ""
                print(f"{what} with {g['workflow']} (seed {g['seed']}{size}{extra}) ...", flush=True)
            elif "result" in msg:
                r = msg["result"]
                for path in r["paths"] + r["copies"]:
                    print(path, flush=True)
                waited = f", waited {r['waited_s']:.0f}s" if r["waited_s"] >= 1 else ""
                back = "; ComfyUI stopped" if r.get("switched_back") else ""
                print(f"[{r['workflow']} · seed {r['seed']} · {r['seconds']}s{waited}{back}]", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nimage request cancelled", file=sys.stderr)
        return 130


def _pick_model(state, ollama):
    """List downloaded models; on a terminal, also ask for one by number. Returns its name or None."""
    models = ollama.models()
    if not models:
        raise DenError("Ollama has no models downloaded; get one with: den model <name>")
    active = core.model_tag(state["llm_model"]) if state["llm_model"] else None
    width = max(len(m["name"]) for m in models)
    for i, m in enumerate(models, 1):
        marker = "*" if core.model_tag(m["name"]) == active else " "
        print(f"{i:>2} {marker} {m['name']:<{width}}  {_gb(m['size'])}")
    if not sys.stdin.isatty():
        return None

    try:
        answer = input("switch to [number, Enter keeps current]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not answer:
        return None
    if not answer.isdigit() or not 1 <= int(answer) <= len(models):
        raise DenError(f"pick a number from 1 to {len(models)}")
    return models[int(answer) - 1]["name"]


def cmd_model(args):
    config, state = _load()
    ollama = core.broker(config, "cli")
    new_model = args.model or _pick_model(state, ollama)
    if new_model is None:
        return

    if core.model_tag(new_model) not in ollama.installed():
        if args.no_pull:
            raise DenError(f"{new_model} is not pulled (drop --no-pull to download it)")
        print(f"pulling {new_model} ...")  # straight to Ollama: downloading doesn't use the GPU
        if subprocess.run(["ollama", "pull", new_model]).returncode != 0:
            raise DenError(f"ollama pull {new_model} failed")

    old_model = state["llm_model"]
    state["llm_model"] = new_model
    core.save_state(state)
    if old_model and core.model_tag(old_model) != core.model_tag(new_model):
        try:
            ollama.unload(old_model)
        except DenError:
            pass  # was not loaded
    print(f"llm model: {new_model}")


def cmd_task(args):
    config, state = _load()
    tasks = core.all_tasks(config, state)
    if args.name is None:
        for name, (task, enabled) in tasks.items():
            print(f"{'on ' if enabled else 'off'}  {name:<10} {task['description']}")
        return
    if args.name not in tasks:
        raise DenError(f"unknown task {args.name!r}; configured: {', '.join(tasks)} (add new ones in config.toml)")
    if args.state is None:
        print("on" if tasks[args.name][1] else "off")
        return
    state["tasks"][args.name] = args.state == "on"
    core.save_state(state)
    print(f"task {args.name}: {args.state}")


def cmd_ask(args):
    config, state = _load()
    text = None if sys.stdin.isatty() else sys.stdin.read()
    answer, stats = core.run_task(config, state, args.task, args.instructions, text, args.file)
    print(answer)
    print(
        f"\n[{stats['model']} · {stats['input_tokens']} in / {stats['output_tokens']} out · {stats['seconds']}s]",
        file=sys.stderr,
    )


def cmd_serve(args):
    from den import broker

    broker.serve()


def cmd_log(args):
    entries = core.read_log()
    calls = {e["id"]: e for e in entries if e["type"] == "call"}
    verdicts = {e["id"]: e for e in entries if e["type"] == "feedback"}  # the latest one wins
    if not calls:
        print(f"no delegations logged yet ({core.LOG_PATH})")
        return

    columns = ("calls", "avg in", "avg s", *core.VERDICTS, "none")
    print(f"{'task':<10} " + " ".join(f"{c:>9}" for c in columns))
    for task in sorted({c["task"] for c in calls.values()}):
        ids = [i for i, c in calls.items() if c["task"] == task]
        counts = [sum(1 for i in ids if verdicts.get(i, {}).get("verdict") == v) for v in core.VERDICTS]
        row = (
            len(ids),
            round(sum(calls[i]["input_tokens"] or 0 for i in ids) / len(ids)),
            round(sum(calls[i]["seconds"] for i in ids) / len(ids), 1),
            *counts,
            len(ids) - sum(counts),
        )
        print(f"{task:<10} " + " ".join(f"{v:>9}" for v in row))

    problems = [v for v in verdicts.values() if v["verdict"] in ("partly", "wrong")]
    if problems:
        print("\nlatest problems:")
        for v in sorted(problems, key=lambda v: v["ts"])[-args.notes :]:
            day = time.strftime("%Y-%m-%d", time.localtime(v["ts"]))
            print(f"  #{v['id']} {day} {calls[v['id']]['task']} {v['verdict']}: {v['note'] or '(no note)'}")
    print(f"\nlog: {core.LOG_PATH}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="den", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show mode, active model, GPU use and tasks").set_defaults(func=cmd_status)

    p = sub.add_parser("mode", help="show or switch the kill switch: off keeps every caller off the GPU")
    p.add_argument("mode", nargs="?", choices=list(core.MODES))
    p.add_argument("--now", action="store_true", help="cancel running requests instead of waiting for them")
    p.set_defaults(func=cmd_mode)

    p = sub.add_parser(
        "unload",
        help="release the GPU, RAM and CPU the local models hold, without changing the mode "
        "(requests are refused while it runs; the next one loads its side again)",
    )
    p.add_argument("side", nargs="?", choices=["llm", "image"], help="default: both sides")
    p.add_argument("--now", action="store_true", help="cancel running requests instead of waiting for them")
    p.set_defaults(func=cmd_unload)

    sub.add_parser("serve", help="run the GPU broker (normally started by: systemctl --user start den)").set_defaults(
        func=cmd_serve
    )

    p = sub.add_parser("model", help="pick the LLM from the downloaded models, or name one (pulls it if missing)")
    p.add_argument("model", nargs="?", help="Ollama model name, e.g. qwen3.6:35b-a3b")
    p.add_argument("--no-pull", action="store_true", help="fail instead of downloading a missing model")
    p.set_defaults(func=cmd_model)

    p = sub.add_parser("task", help="list delegated tasks or turn one on/off")
    p.add_argument("name", nargs="?")
    p.add_argument("state", nargs="?", choices=["on", "off"])
    p.set_defaults(func=cmd_task)

    p = sub.add_parser("ask", help="run a task on the local LLM (text on stdin and/or --file)")
    p.add_argument("task")
    p.add_argument("instructions")
    p.add_argument("--file", action="append", default=[], type=Path)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("image", help="generate an image through the broker, or list workflows (no prompt)")
    p.add_argument("prompt", nargs="?")
    p.add_argument("--json", action="store_true", help="print the runnable workflows with a tool description and parameter schema as JSON")
    p.add_argument("-w", "--workflow", help="workflow name (default: [image] default_workflow)")
    p.add_argument("-n", "--negative", help="negative prompt, for workflows that take one (on klein it raises cfg to 2)")
    p.add_argument("--seed", type=int, help="default: random")
    p.add_argument("--size", help="WIDTHxHEIGHT, e.g. 1024x1024 (default: the workflow's)")
    p.add_argument("--image", type=Path, help="input image to edit, for workflows whose model edits")
    p.add_argument("-o", "--out", type=Path, help="also copy the result here (a file, or a directory ending in /)")
    p.add_argument("--switch-back", action="store_true", help="stop ComfyUI afterwards so the LLM can load")
    p.add_argument("--steps", type=int, help="sampling steps (den image lists each workflow's ranges)")
    p.add_argument("--cfg", type=float, help="guidance scale; overrides the cfg a negative prompt sets")
    p.add_argument("--sampler", help="ComfyUI sampler name, e.g. euler, dpmpp_2m")
    p.add_argument("--scheduler", help="ComfyUI scheduler name, e.g. simple, karras")
    p.add_argument("--lora", action="append", default=[], metavar="NAME[:STRENGTH]", help="add a LoRA the workflow offers; repeatable")
    p.add_argument("--reference", action="append", default=[], type=Path, help="reference image, for workflows that take them; repeatable")
    p.add_argument("--control", type=Path, help="ControlNet guide image, for workflows that list control")
    p.add_argument("--control-type", help="what the guide image is: canny (a photo), pose, depth, … (den image lists them)")
    p.add_argument("--control-strength", type=float, help="how strongly the guide image steers (default: the workflow's)")
    p.add_argument("--upscale", metavar="NAME[:FACTOR]", help="upscale the result with this model (default factor 2)")
    p.set_defaults(func=cmd_image)

    p = sub.add_parser("log", help="review delegations by Claude: per-task stats, verdicts and problem notes")
    p.add_argument("--notes", type=int, default=10, help="how many recent problem notes to show")
    p.set_defaults(func=cmd_log)

    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except DenError as e:
        print(f"den: {e}", file=sys.stderr)
        return 1
