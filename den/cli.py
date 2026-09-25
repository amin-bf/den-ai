"""`den` — switch modes, models and delegated tasks; run a task from the shell."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from den import clip, core, image, platform, poses, remote, speech
from den.core import DenError


# With a remote, den's files are there: these act on this machine's broker or files, so they
# are run on that machine instead (ADR 0007).
THERE_ONLY = {
    "serve": "den serve runs the broker of the machine it's on",
    "model": "the model is picked on the machine that runs it",
    "log": "the delegation log is kept where the tasks run",
}


def _load():
    config = core.load_config()
    return config, core.load_state(config)


def _gb(n):
    return f"{n / 1e9:.1f} GB"


def _describe(r):
    model = f" {r['model']}" if r["model"] else ""
    left = f", {_time_left(r['left_s'])}" if "left_s" in r else ""
    return f"#{r['id']} {r['caller']} {r['request']}{model} ({r['seconds']:.0f}s{left})"


def _time_left(seconds):
    if seconds is None:
        return "time left unknown"
    return "under a minute left" if seconds < 60 else f"about {round(seconds / 60)} min left"


def cmd_status(args):
    if core.remote_name():
        return _status_remote(core.remote_name())
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
        server = status.get("llm") or {}
        if server.get("model"):
            where = f"llama-server has {server['model']} loaded (pid {server['pid']})"
        else:
            where = "not loaded"
        if state["llm_model"]:
            print(f"llm      {model}  ({'pulled' if pulled else 'NOT PULLED'}; {where})")
        else:
            print(f"llm      {model}  ({where})")
        # Ollama is only the model store; anything it has loaded came from a client that went
        # around den, and the broker unloads it before a swap.
        stray = [m["name"] for m in upstream["loaded"]]
        print(f"ollama   {version} at {upstream['url']} (model store)" + (f"; has loaded outside den: {', '.join(stray)}" if stray else ""))

    comfy = status.get("comfyui")
    runnable = image.available(config)
    if comfy is None:
        print("image    not configured")
    else:
        where = "up" if comfy["up"] else "stopped"
        if comfy["idle_s"] is not None:
            where += f", idle {comfy['idle_s'] // 60} min"
        print(f"image    comfyui {where} at {comfy['url']}; workflows: {', '.join(runnable) or 'none can run (den image)'}")
    if clip.workflows(config):
        active = [c for c in status.get("clips", []) if c["state"] in ("waiting", "running")]
        busy = f"; {len(active)} waiting or running (den clip --list)" if active else ""
        print(f"clip     workflows: {', '.join(clip.available(config)) or 'none can run (den clip)'}{busy}")
        why = speech.unavailable()
        voices = ", ".join(speech.voices()) or "none yet (den voice --add)"
        print(f"speech   {f'not installed: {why}' if why else f'voices: {voices}'}")
    if status.get("live"):
        print(f"live     {status['live']['state']}: den takes nothing else until: den live off")
    if (status.get("standby") or {}).get("state", "off") != "off":
        print(_standby_line(status["standby"]).replace("standby: ", "standby  ", 1))
    _print_pressure(config, status)
    _print_tasks(config, state)
    # Non-zero while Ollama is down, so a shell check or a notifier can watch this.
    return 1 if down else 0


def _status_remote(name):
    """`den --on NAME status`: the den there, from its broker alone."""
    client = remote.client("cli")
    status, offered = client.status(), client.client_info()
    llm = offered["llm"]
    if status["mode"] == "on":
        sides = {"llm": llm["unavailable"], "image": offered["image"]["unavailable"]}
        ready = ", ".join(f"{side}: {'ready' if why is None else 'not ready'}" for side, why in sides.items())
        print(f"mode     on  ({ready})")
    else:
        print(f"mode     off  ({core.OFF_MESSAGE})")
    print(f"broker   up at {client.base_url} (the den on {name}), pid {status['pid']}; GPU side: {status['loaded'] or 'nothing'}")
    for r in status["inflight"]:
        print(f"         running {_describe(r)}")
    for r in status["waiting"]:
        print(f"         waiting {_describe(r)}")
    server = status.get("llm") or {}
    loaded = f"llama-server has {server['model']} loaded" if server.get("model") else "not loaded"
    print(f"llm      {llm['model'] or '(none selected there: den model)'}  ({loaded})")
    flows = offered["image"]["workflows"]
    print(f"image    workflows: {', '.join(flows) or 'none can run there'}")
    clips = offered.get("clip")  # an older broker offers none
    if clips:
        active = [c for c in status.get("clips", []) if c["state"] in ("waiting", "running")]
        busy = f"; {len(active)} waiting or running (den clip --list)" if active else ""
        print(f"clip     workflows: {', '.join(clips['workflows']) or 'none can run there'}{busy}")
    voice = offered.get("voice")  # only where speech runs there
    if voice:
        print(f"speech   voices: {', '.join(voice['voices']) or 'none yet there (den voice --add)'}")
    now = status["pressure"]
    load = f"load {now['load']:.1f} on {now['cpus']} cpus ({now['load_per_cpu']:.2f}/cpu)" if now["load"] is not None else "load unknown"
    ram = f"{now['free_ram_gb']:.1f} GB RAM free" if now["free_ram_gb"] is not None else "free RAM unknown"
    print(f"machine  {load}, {ram}")
    if status.get("too_busy"):
        print(f"         too busy to load a side that isn't loaded: {status['too_busy']}")
    print(f"tasks    {', '.join(offered['tasks']) or 'none enabled there'}")


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
        print(remote.client("cli").status()["mode"] if core.remote_name() else state["mode"])
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
        print(f"starting {msg['starting']} ...", flush=True)
    elif "stopping" in msg:
        print(f"stopping {msg['stopping']} ...", flush=True)
    else:
        return False
    return True


def _print_image_step(msg):
    """Print an image request's waiting, swap or generating line; False for other messages."""
    if "waiting" in msg:
        print(f"waiting: {msg['waiting']['reason']}", flush=True)
        for r in msg["waiting"].get("running", []):
            print(f"  {_describe(r)}", flush=True)
    elif _print_swap_step(msg):
        pass
    elif "generating" in msg:
        g = msg["generating"]
        what = "editing" if g.get("edit") else "generating"
        workflow, *rest = g["summary"]
        print(f"{what} with {workflow} ({', '.join(rest)}) ...", flush=True)
    else:
        return False
    return True


def cmd_live(args):
    """den live on|off|status: the live conversation's switch (ADR 0011); Claude starts one itself."""
    config, _ = _load()
    client = core.broker(config, "cli")
    if args.action == "on":
        for msg in client.live_start(**{k: v for k, v in {"voice": args.voice, "language": args.language}.items() if v}):
            if _print_image_step(msg):
                pass
            elif "result" in msg:
                print(f"live conversation {msg['result']['session']} on; den takes nothing else until: den live off")
    elif args.action == "off":
        answer = client.live_stop()
        print(answer["transcript"] or "(nothing was said)")
        print(f"[session {answer['session']} · {answer['minutes']} min]", file=sys.stderr)
    elif args.action == "list":
        for r in client.conversations():
            print(f"{r['id']:<22} {r['kind']:<8} {r['when']}  {r['turns']:>3} turns  {r.get('summary') or r['opening']}"[:200])
    elif args.action == "show":
        c = client.conversation(args.id)
        print((f"Summary:\n{c['summary']}\n\n" if c.get("summary") else "") + c["transcript"])
    elif args.action == "summary":
        answer, stats = core.summarize_conversation(config, args.id)
        print(answer)
        print(f"[{stats['model']}, {stats['seconds']}s; saved beside {args.id}]", file=sys.stderr)
    elif args.action == "rm":
        client.delete_conversation(args.id)
        print(f"deleted {args.id}")
    elif args.action == "keep":
        print(client.live_keep(args.id, args.name)["kept"])
    else:
        live = client.status().get("live")
        print(f"live: {live['state']} (session {live.get('session')}, voice {live.get('voice') or 'default'})" if live else "live: off")


def cmd_standby(args):
    """den standby on PHRASE | off | wait | status: listen for a wake word (ADR 0012)."""
    config, _ = _load()
    client = core.broker(config, "cli")
    if args.action == "on":
        if not args.wake_word:
            raise DenError('den standby on takes the wake word, e.g. den standby on "hey Elli"')
        for msg in client.standby_start(args.wake_word):
            if _print_image_step(msg):
                pass
            elif "result" in msg:
                print(f"standby: listening for \"{msg['result'].get('wake_word')}\"; den's other tools keep working")
    elif args.action == "off":
        client.standby_stop()
        print("standby: off")
    elif args.action == "wait":
        # Until the next change: the wake word heard, a conversation pausing it, standby back or off.
        seq = client.standby()["seq"]
        while True:
            standby = client.standby_wait(after=seq, timeout_s=600)
            if standby["seq"] != seq:
                break
        print(_standby_line(standby))
    else:
        print(_standby_line(client.standby()))


def _standby_line(standby):
    wake = f" for \"{standby['wake_word']}\"" if standby.get("wake_word") and standby["state"] != "off" else ""
    reason = f" ({standby['reason']})" if standby.get("reason") else ""
    return f"standby: {standby['state']}{wake}{reason}"


def broker_client(where, client):
    """The broker a voice command talks to: this machine's, or the remote's (ADR 0007)."""
    return remote.client("cli") if where else client


def cmd_voice(args):
    config, _ = _load()
    # On another machine's den (ADR 0007): the files here go as bytes and the track comes back here.
    where = core.remote_name()
    client = None if where else core.broker(config, "cli")
    if args.add:
        name, recording = args.add
        recording = str(Path(recording).expanduser().resolve())
        answer = (
            remote.add_voice("cli", name, recording, args.replace, args.description) if where
            else client.add_voice(name, recording, args.replace, args.description)
        )
        print(f"voice {answer['voice']} kept; voices: {', '.join(answer['voices'])}")
        return
    if args.save:
        draft, name = args.save
        answer = broker_client(where, client).save_draft(draft, name, args.replace)
        print(f"voice {answer['voice']} kept; voices: {', '.join(answer['voices'])}")
        return
    if args.design:
        request = {"description": args.design, "name": args.keep_as, "replace": args.replace or None,
                   "language": args.language, "seed": args.seed}
        request = {k: v for k, v in request.items() if v is not None}
        for msg in remote.design_voice("cli", request) if where else client.design_voice(**request):
            if _print_image_step(msg):
                pass
            elif "designing" in msg:
                print(f"designing the voice {msg['designing']} (the first time downloads the designer, about 4.5 GB) ...", flush=True)
            elif "result" in msg:
                r = msg["result"]
                if r.get("voice"):
                    print(f"voice {r['voice']} kept ({r['duration']:g}s sample, {r['seconds']}s); voices: {', '.join(r['voices'])}")
                else:
                    print(r["path"])
                    print(
                        f"draft {r['draft']} ({r['duration']:g}s sample, {r['seconds']}s): listen to it, try it with "
                        f"-v draft:{r['draft']}, and keep it with: den voice --save {r['draft']} NAME",
                        file=sys.stderr,
                    )
        return
    if args.transcribe:
        recording = str(Path(args.transcribe).expanduser().resolve())
        stream = remote.transcribe("cli", recording, args.language) if where else client.transcribe(
            audio=recording, **({"language": args.language} if args.language else {})
        )
        for msg in stream:
            if _print_image_step(msg):
                pass
            elif "transcribing" in msg:
                print(f"transcribing {msg['transcribing']} ...", flush=True)
            elif "result" in msg:
                r = msg["result"]
                print(r["srt"])
                print(f"[{len(r['segments'])} line(s) · {r['duration']:g}s of audio · {r['seconds']}s] -> {r['path']}", file=sys.stderr)
        return
    broker = remote.client("cli") if where else client
    if args.show:
        info = broker.voice(args.show)
        for key in ("name", "description", "source", "language", "seed", "created", "seconds"):
            if info.get(key) is not None:
                print(f"{key:12} {info[key]}")
        return
    if args.mv:
        answer = broker.rename_voice(*args.mv)
        print(f"voice {args.mv[0]} is now {answer['voice']}; voices: {', '.join(answer['voices'])}")
        return
    if args.describe:
        broker.describe_voice(*args.describe)
        print(f"voice {args.describe[0]} described")
        return
    if args.rm:
        answer = remote.remove_voice("cli", args.rm) if where else client.remove_voice(args.rm)
        print(f"voice {args.rm} removed; voices: {', '.join(answer['voices']) or 'none'}")
        return
    if args.text is None and args.srt is None and args.lines is None:
        listing = remote.voices("cli") if where else client.voices()
        if listing.get("unavailable"):
            print(f"speech can't run: {listing['unavailable']}")
        if not listing["voices"]:
            print("voices:    none yet (den voice --add NAME RECORDING, or --design NAME DESCRIPTION)")
        for row in listing.get("toc") or [{"name": n} for n in listing["voices"]]:
            made = f" [{row['source']}]" if row.get("source") else ""
            print(f"  {row['name']:<14}{made} {row.get('description') or '(no description: den voice --describe NAME TEXT)'}")
        print("languages: " + ", ".join(f"{code} {name}" for code, name in listing["languages"].items()))
        return
    voice = args.voice
    if voice and Path(voice).expanduser().is_file():
        voice = str(Path(voice).expanduser().resolve())  # a recording here, not a name there
    request = {
        "text": args.text,
        "srt": Path(args.srt).expanduser().read_text() if args.srt else None,
        "lines": Path(args.lines).expanduser().read_text().splitlines() if args.lines else None,
        "voice": voice,
        "language": args.language,
        "exaggeration": args.exaggeration,
        "cfg_weight": args.cfg_weight,
        "temperature": args.temperature,
        "seed": args.seed,
        "out": _out_arg(args.out),
    }
    request = {k: v for k, v in request.items() if v is not None}
    try:
        for msg in remote.speak("cli", request) if where else client.speak(**request):
            if _print_image_step(msg):
                pass
            elif "speaking" in msg:
                s = msg["speaking"]
                print(f"line {s['line']}/{s['of']}: {s['text']}", flush=True)
            elif "result" in msg:
                r = msg["result"]
                for path in [r["path"], *r["copies"]]:
                    print(path, flush=True)
                print(f"script at the spoken times: {Path(r['path']).with_suffix('.srt')}", file=sys.stderr)
                for note in r["notes"]:
                    print(f"note: {note}", file=sys.stderr)
                waited = f", waited {r['waited_s']:.0f}s" if r["waited_s"] >= 1 else ""
                print(f"[{' · '.join([*r['summary'], f'{r['seconds']}s{waited}'])}]", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nvoice request cancelled", file=sys.stderr)
        return 130


def _voiceover_arg(args):
    """--voiceover as the request carries it: an SRT file's script, or a line of text."""
    if not args.voiceover:
        return None
    path = Path(args.voiceover).expanduser()
    if path.suffix.lower() == ".srt" and path.is_file():
        spoken = {"srt": path.read_text()}
    elif path.suffix.lower() in speech.AUDIO_SUFFIXES and path.is_file():
        spoken = {"audio": str(path.resolve())}  # a recording is the voice-over itself
    elif path.is_file():
        spoken = {"lines": path.read_text().splitlines()}  # a text file: lines in turn
    else:
        spoken = {"text": args.voiceover}
    extra = {"voice": args.voice, "language": args.language, "sync": args.lip_sync or None}
    return {**spoken, **{k: v for k, v in extra.items() if v}}


def _out_arg(value):
    """--out as an absolute path, keeping a trailing / (a folder, even a new one): Path() drops
    it, so the argument stays a string until here."""
    if not value:
        return None
    return str(Path(value).expanduser().resolve()) + ("/" if value.endswith("/") else "")


def _lora_arg(value):
    name, _, strength = value.partition(":")
    try:
        return {"name": name, **({"strength": float(strength)} if strength else {})}
    except ValueError:
        raise DenError(f"--lora takes NAME or NAME:STRENGTH, got {value!r}") from None


def _reference_arg(value):
    """PATH, TYPE:PATH (e.g. pose:photo.png) with the path made absolute and the type kept, or
    pose:NAME for a saved pose, as it is."""
    if image.saved_pose(value):
        return value
    kind, path = image.parse_reference(value)
    path = str(Path(path).expanduser().resolve())
    return f"{kind}:{path}" if kind else path


def _control_arg(args):
    if not args.control:
        return None
    if not args.control_type:
        raise DenError("--control needs --control-type (den image lists each workflow's types)")
    guide = args.control if image.saved_pose(args.control) else str(Path(args.control).expanduser().resolve())
    control = {"image": guide, "type": args.control_type}
    if args.control_strength is not None:
        control["strength"] = args.control_strength
    if args.control_start is not None:
        control["start"] = args.control_start
    if args.control_end is not None:
        control["end"] = args.control_end
    return control


def _upscale_arg(value):
    if not value:
        return None
    name, _, factor = value.partition(":")
    try:
        return {"name": name, **({"factor": float(factor)} if factor else {})}
    except ValueError:
        raise DenError(f"--upscale takes NAME or NAME:FACTOR, got {value!r}") from None


def cmd_pose(args):
    command = args.pose_command or "list"
    if core.remote_name():
        return _pose_remote(args, command)
    if command == "list":
        entries = poses.entries()
        if args.json:
            print(json.dumps([{**e, "aspect": poses.aspect(e["width"], e["height"])} for e in entries], indent=2))
        elif entries:
            print(poses.toc())
        else:
            print(f"no saved poses yet (in {poses.POSES_DIR}); save one with: den pose save PHOTO NAME -d '…'")
        return
    if command == "show":
        entry = poses.get(args.name)
        print(json.dumps({**entry, "aspect": poses.aspect(entry["width"], entry["height"])}, indent=2) if args.json else poses.details(entry))
        return
    if command == "rm":
        poses.remove(args.name)
        print(f"deleted saved pose {args.name}")
        return
    if command == "mv":
        entry = poses.rename(args.old, args.new, args.replace)
        print(f"renamed {args.old} to {entry['name']}")
        return
    config, _ = _load()
    if command == "refresh":
        # Redraw from the stored photo: fills in keypoints for poses saved before den kept them.
        failed = 0
        for name in args.names or poses.names():
            entry = poses.get(name)
            if not entry.get("source"):
                print(f"{name}: no stored photo, skipped", file=sys.stderr)
                failed += 1
                continue
            request = {"image": entry["source"], "name": name, "description": entry["description"], "replace": True}
            try:
                if _save_pose(config, request):
                    return 130  # cancelled
            except DenError as e:
                print(f"{name}: {e}", file=sys.stderr)
                failed += 1
        return 1 if failed else None
    request = {
        "image": str(args.photo.expanduser().resolve()),
        "name": args.name,
        "description": args.description,
        "replace": args.replace,
    }
    return _save_pose(config, request)


def _pose_remote(args, command):
    """`den --on NAME pose`: the pose library there. Saving draws on its GPU from a photo here,
    sent as bytes; renaming, deleting and redrawing are done there."""
    name = core.remote_name()
    client = remote.client("cli")
    if command == "list":
        found = client.poses()
        print(json.dumps(found["names"]) if args.json else found["toc"] or f"no saved poses on {name} yet")
        return
    if command == "show":
        print(client.poses(args.name)["details"])
        return
    if command == "save":
        request = {
            "image": str(args.photo.expanduser().resolve()),
            "name": args.name,
            "description": args.description,
            "replace": args.replace,
        }
        return _save_pose(core.load_config(), request)
    raise DenError(f"the pose library is {name}'s; run it there: den pose {command} …")


def _save_pose(config, request):
    if core.remote_name():
        stream = remote.save_pose("cli", request)
    else:
        stream = core.broker(config, "cli").save_pose(**request)
    try:
        for msg in stream:
            if "waiting" in msg:
                print(f"waiting: {msg['waiting']['reason']}", flush=True)
            elif _print_swap_step(msg):
                pass
            elif "drawing" in msg:
                print(f"drawing the pose for {msg['drawing']['name']} ...", flush=True)
            elif "result" in msg:
                r = msg["result"]
                print(f"saved pose {r['name']} ({r['aspect']}, {r['width']}x{r['height']}) in {r['seconds']}s")
                if core.remote_name():
                    print(f"in {core.remote_name()}'s pose library")
                else:
                    print(r["map"])
                    print(r["source"])
                    print(r.get("keypoints") or "(no keypoints came back)")
    except KeyboardInterrupt:
        print("\npose request cancelled", file=sys.stderr)
        return 130


def cmd_image(args):
    if core.remote_name() and args.prompt is None:
        return _image_remote(args)
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
        # The pose tools and the saved names (for completions) sit apart from the image tool's
        # description and schema, so a new saved pose doesn't change that tool (ADR 0003).
        extra = {"poses": poses.names(), "pose_tools": image.pose_tool_specs(config)}
        # The clip tool's spec rides along, so pi learns both from one call (ADR 0009).
        extra["clip"] = clip.client_spec(config, state)
        # And the voice tool's, where speech is installed (ADR 0010).
        if speech.unavailable() is None and unavailable is None:
            extra["voice"] = {
                **speech.request_spec(),
                "design_languages": list(speech.DESIGN_LANGUAGES) if speech.design_unavailable() is None else [],
            }
        print(json.dumps({**listing, "workflows": list(flows), "edits": [n for n, wf in flows.items() if "edit" in wf], **spec, **extra}))
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
        "out": _out_arg(args.out),
        "switch_back": args.switch_back,
        "steps": args.steps,
        "cfg": args.cfg,
        "sampler": args.sampler,
        "scheduler": args.scheduler,
        "loras": [_lora_arg(value) for value in args.lora] or None,
        "references": [_reference_arg(value) for value in args.reference] or None,
        "control": _control_arg(args),
        "upscale": _upscale_arg(args.upscale),
        "strength": args.strength,
        "save_maps": args.save_maps or None,
    }
    try:
        request = {k: v for k, v in request.items() if v is not None}
        if core.remote_name():
            stream = remote.generate_image("cli", request)
        else:
            stream = core.broker(config, "cli").generate_image(**request)
        for msg in stream:
            if _print_image_step(msg):
                pass
            elif "result" in msg:
                r = msg["result"]
                for path in r["paths"] + r["copies"] + r.get("maps", []):
                    print(path, flush=True)
                waited = f", waited {r['waited_s']:.0f}s" if r["waited_s"] >= 1 else ""
                back = "; ComfyUI stopped" if r.get("switched_back") else ""
                parts = [*r["summary"], f"{r['seconds']}s{waited}"]
                print(f"[{' · '.join(parts)}{back}]", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nimage request cancelled", file=sys.stderr)
        return 130


def _image_remote(args):
    """`den --on NAME image` without a prompt: the den there lists its workflows, or with --json
    gives its spec. A prompt runs like a local one (cmd_image), sent there with its files as bytes."""
    if args.json:
        info = remote.info("cli")
        offered = info["image"]
        offered.pop("listing", None)
        clips = info.get("clip")  # an older broker offers none
        if clips:
            clips.pop("listing", None)
            offered["clip"] = clips
        if info.get("voice") and offered.get("image_on"):  # only where speech runs there (ADR 0010)
            offered["voice"] = info["voice"]
        print(json.dumps({"broker": remote.client("cli").base_url, **offered}))
        return
    print(remote.info("cli")["image"]["listing"])


def _keyframe_arg(value):
    """PATH[@AT]: an image the clip must show, at seconds, a percentage (50%) or end."""
    path, at = value, None
    head, sep, tail = value.rpartition("@")
    if sep and (tail == "end" or tail.endswith("%") or tail.replace(".", "", 1).isdigit()):
        path, at = head, tail if tail == "end" or tail.endswith("%") else float(tail)
    keyframe = {"image": str(Path(path).expanduser().resolve())}
    return keyframe if at is None else {**keyframe, "at": at}


def _clip_line(view):
    """One line for a clip: id, state and where it is."""
    state = view["state"]
    if state == "running":
        state += f" {view['running_s']}s, {_time_left(view.get('left_s'))}"
    elif state == "waiting" and view.get("progress", {}).get("waiting"):
        state += f": {view['progress']['waiting']['reason']}"
    elif view.get("error"):
        state += f": {view['error']}"
    return f"#{view['id']} {view['caller']} {state} — {view['prompt'][:60]}"


def _clip_done(view):
    r = view["result"]
    for path in [r["path"], *r["copies"]]:
        print(path, flush=True)
    if r.get("sheet"):
        print(f"contact sheet: {r['sheet']}", file=sys.stderr)
    if r.get("voiceover_track"):
        print(f"voice-over track: {r['voiceover_track']}", file=sys.stderr)
    for note in r.get("notes") or []:
        print(f"note: {note}", file=sys.stderr)
    waited = f", waited {r['waited_s']:.0f}s" if r["waited_s"] >= 1 else ""
    parts = [*r["summary"], f"{r['seconds']}s{waited}"]
    print(f"[{' · '.join(parts)}]", file=sys.stderr)


def _get_clip(config, clip_id):
    return remote.get_clip("cli", clip_id) if core.remote_name() else core.broker(config, "cli").get_clip(clip_id)


def _watch_clip(config, clip_id):
    """Print a clip's progress until it's done; Ctrl+C stops watching, not the clip."""
    last = None
    try:
        while True:
            view = _get_clip(config, clip_id)
            line = _clip_line(view)
            # Report a change of state or reason, not the seconds ticking.
            key = (view["state"], (view.get("progress") or {}).get("waiting", {}).get("reason"))
            if key != last:
                print(line, file=sys.stderr, flush=True)
                last = key
            if view["state"] == "done":
                _clip_done(view)
                return 0
            if view["state"] in ("failed", "cancelled"):
                return 1
            time.sleep(3)
    except KeyboardInterrupt:
        print(f"\nstopped watching; clip #{clip_id} goes on (den clip --id {clip_id}, or --cancel {clip_id})", file=sys.stderr)
        return 130


def cmd_clip(args):
    config = core.load_config()
    where = core.remote_name()
    broker = None if where else core.broker(config, "cli")
    if args.list:
        views = remote.client("cli").clips() if where else broker.clips()
        for view in views:
            print(_clip_line(view))
        if not views:
            print("no clips held (a finished one is kept for a day, or until the broker restarts)")
        return
    if args.cancel is not None:
        done = (remote.client("cli") if where else broker).cancel_clip(args.cancel)
        print(f"clip #{done['id']}: {done['state'] if done['state'] not in ('waiting', 'running') else 'cancelling'}")
        return
    if args.id is not None:
        if args.wait:
            return _watch_clip(config, args.id)
        view = _get_clip(config, args.id)
        print(_clip_line(view))
        if view["state"] == "done":
            _clip_done(view)
        return
    if args.prompt is None:
        print(remote.info("cli")["clip"]["listing"] if where else clip.listing(config))
        return
    request = {
        "prompt": args.prompt,
        "workflow": args.workflow,
        "negative": args.negative,
        "seed": args.seed,
        "size": args.size,
        "duration": args.duration,
        "sound": args.sound,
        "voiceover": _voiceover_arg(args),
        "keyframes": [_keyframe_arg(value) for value in args.keyframe] or None,
        "steps": args.steps,
        "cfg": args.cfg,
        "sampler": args.sampler,
        "scheduler": args.scheduler,
        "loras": [_lora_arg(value) for value in args.lora] or None,
        "out": _out_arg(args.out),
    }
    request = {k: v for k, v in request.items() if v is not None}
    started = remote.generate_clip("cli", request) if where else broker.generate_clip(**request)
    print(f"clip #{started['id']}: {' · '.join(started['summary'])}; {_time_left(started['estimate_s'])}", file=sys.stderr)
    if args.detach:
        print(started["id"])
        return
    return _watch_clip(config, started["id"])


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

    state["llm_model"] = new_model
    core.save_state(state)
    # Nothing to unload here: llama-server switches to the new model on its next request,
    # saving the old one's cache first.
    print(f"llm model: {new_model}")


def cmd_task(args):
    if core.remote_name():
        if args.state is not None:
            raise DenError(f"the tasks are {core.remote_name()}'s; switch one there: den task {args.name} {args.state}")
        for name, task in remote.info("cli")["tasks"].items():
            print(f"on   {name:<10} {task['description']}")
        return
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
    text = None if sys.stdin.isatty() else sys.stdin.read()
    if core.remote_name():
        # Run and logged there; the files are read here and sent as their contents.
        done = remote.delegate("cli", args.task, args.instructions, text, args.file)
        answer, stats = done["answer"], done["stats"]
    else:
        config, state = _load()
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
    parser.add_argument(
        "--on", metavar="NAME",
        help=f"use the den on another machine, [remotes.NAME], as if it were the only one (sets {core.REMOTE_ENV})",
    )
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

    sub.add_parser(
        "serve", help=f"run the GPU broker (normally started by: {platform.start_hint('den')})"
    ).set_defaults(func=cmd_serve)

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
    p.add_argument("-o", "--out", help="also copy the result here (a file, or a directory ending in /)")
    p.add_argument("--switch-back", action="store_true", help="stop ComfyUI afterwards so the LLM can load")
    p.add_argument("--steps", type=int, help="sampling steps (den image lists each workflow's ranges)")
    p.add_argument("--cfg", type=float, help="guidance scale; overrides the cfg a negative prompt sets")
    p.add_argument("--sampler", help="ComfyUI sampler name, e.g. euler, dpmpp_2m")
    p.add_argument("--scheduler", help="ComfyUI scheduler name, e.g. simple, karras")
    p.add_argument("--lora", action="append", default=[], metavar="NAME[:STRENGTH]", help="add a LoRA the workflow offers; repeatable")
    p.add_argument(
        "--reference", action="append", default=[], metavar="[TYPE:]PATH|pose:NAME",
        help="reference image, for workflows that take them; repeatable. pose:PATH draws the "
        "photo's pose as a skeleton and passes that instead; pose:NAME takes a saved pose",
    )
    p.add_argument("--control", metavar="PATH|pose:NAME", help="ControlNet guide image, or a saved pose, for workflows that list control")
    p.add_argument("--control-type", help="what the guide image is: canny (a photo), pose, depth, … (den image lists them)")
    p.add_argument("--control-strength", type=float, help="how strongly the guide image steers (default: the workflow's)")
    p.add_argument("--control-start", type=float, help="fraction of the sampling where the guide starts acting (default 0)")
    p.add_argument("--control-end", type=float, help="fraction where it stops; end early to fix only the composition (default 1)")
    p.add_argument("--upscale", metavar="NAME[:FACTOR]", help="upscale the result with this model (default factor 2)")
    p.add_argument("--strength", type=float, metavar="0-1", help="with --image: how much of the edit to keep, blended back over the input")
    p.add_argument("--save-maps", action="store_true", help="also save the maps den draws (pose skeleton, canny edges) beside the image")
    p.set_defaults(func=cmd_image)

    p = sub.add_parser(
        "clip",
        help="make a short video clip (a detached request: it runs on after you stop watching), "
        "look at or cancel one, or list clip workflows (no prompt)",
    )
    p.add_argument("prompt", nargs="?")
    p.add_argument("-w", "--workflow", help="clip workflow name (default: [clip] default_workflow)")
    p.add_argument("-n", "--negative", help="negative prompt, for workflows that take one")
    p.add_argument("--seed", type=int, help="default: random")
    p.add_argument("--size", help="WIDTHxHEIGHT, e.g. 1280x704 (default: the workflow's)")
    p.add_argument("-d", "--duration", type=float, help="length in seconds (den clip lists each workflow's range)")
    p.add_argument(
        "--sound", action=argparse.BooleanOptionalAction,
        help="give the clip a sound track, on workflows that make sound (default there: on); describe the sounds in the prompt",
    )
    p.add_argument(
        "--voiceover", metavar="SRT|TEXT",
        help="a voice-over: an SRT script (each line at its time; the clip lasts to its end), a text file of "
        "lines (spoken in turn), a recording (used as it is), or a line of text, "
        "over the clip's own sound or as its sound track",
    )
    p.add_argument("--voice", help="with --voiceover: a voice from the library (den voice), or a recording's path")
    p.add_argument(
        "--lip-sync", action="store_true",
        help="with --voiceover: make the clip to the voice, so a person on screen speaks it (workflows that say lip-sync)",
    )
    p.add_argument("--language", help="with --voiceover: language code (default: en)")
    p.add_argument(
        "--keyframe", action="append", default=[], metavar="PATH[@AT]",
        help="an image the clip must show; AT is seconds, a percentage (50%%) or end, default 0 for "
        "the first; repeatable where the workflow takes several",
    )
    p.add_argument("--steps", type=int, help="sampling steps")
    p.add_argument("--cfg", type=float, help="guidance scale")
    p.add_argument("--sampler", help="ComfyUI sampler name")
    p.add_argument("--scheduler", help="ComfyUI scheduler name")
    p.add_argument("--lora", action="append", default=[], metavar="NAME[:STRENGTH]", help="add a LoRA the workflow offers; repeatable")
    p.add_argument("-o", "--out", help="also copy the clip here (a file, or a directory ending in /)")
    p.add_argument("--detach", action="store_true", help="print the clip's id and return at once")
    p.add_argument("--id", type=int, help="show the clip with this id (its files once it's done)")
    p.add_argument("--wait", action="store_true", help="with --id: watch it until it's done")
    p.add_argument("--cancel", type=int, metavar="ID", help="cancel a clip that waits or runs")
    p.add_argument("--list", action="store_true", help="the clips the broker holds")
    p.set_defaults(func=cmd_clip)

    p = sub.add_parser(
        "voice",
        help="speak a text or an SRT script in a voice cloned from a recording (a voice-over track), "
        "or list and keep voices",
    )
    p.add_argument("text", nargs="?", help="what to say, from the start (or use --srt)")
    p.add_argument("--srt", help="an SRT script: each line spoken at its time, on one track")
    p.add_argument("--lines", help="a text file of lines to speak in turn: den times them and writes the SRT")
    p.add_argument("-v", "--voice", help="a voice from the library, or a recording's path (default: the model's own)")
    p.add_argument("-l", "--language", help="language code, e.g. en, de, fr (default: en; den voice lists them)")
    p.add_argument("--exaggeration", type=float, help="expressiveness, 0.25–2 (default 0.5)")
    p.add_argument("--cfg-weight", type=float, help="pacing and adherence, 0–1 (default 0.5; lower is slower and calmer)")
    p.add_argument("--temperature", type=float, help="variation, default 0.8")
    p.add_argument("--seed", type=int, help="default: random")
    p.add_argument("-o", "--out", help="also copy the track here (a file, or a directory ending in /)")
    p.add_argument("--add", nargs=2, metavar=("NAME", "RECORDING"), help="keep a recording in the voice library")
    p.add_argument("--replace", action="store_true", help="with --add or --design: replace a voice of that name")
    p.add_argument("--description", help="with --add: what the voice is, e.g. \"my own voice, calm\"")
    p.add_argument("--show", metavar="NAME", help="a voice's details: what it is, how it was made")
    p.add_argument("--mv", nargs=2, metavar=("OLD", "NEW"), help="rename a voice")
    p.add_argument("--describe", nargs=2, metavar=("NAME", "TEXT"), help="set what a voice is")
    p.add_argument("--rm", metavar="NAME", help="remove a voice from the library")
    p.add_argument(
        "--design", metavar="DESCRIPTION",
        help='design a voice from a description, e.g. "an old man with a deep, raspy, slow voice": a draft to '
        "listen to and try (-v draft:ID) before --save keeps it (-l for the sample's language, --seed)",
    )
    p.add_argument("--keep-as", metavar="NAME", help="with --design: keep it at once under NAME, no draft")
    p.add_argument("--save", nargs=2, metavar=("DRAFT", "NAME"), help="keep a designed draft voice under NAME")
    p.add_argument(
        "--transcribe", metavar="RECORDING",
        help="write down what a recording says, as a timed SRT (Whisper; -l for its language)",
    )
    p.set_defaults(func=cmd_voice)

    p = sub.add_parser("live", help="a live spoken conversation with Claude: den's microphone and voice, exclusively")
    p.add_argument("action", choices=["on", "off", "status", "list", "show", "summary", "keep", "rm"])
    p.add_argument("id", nargs="?", help="with show, summary, keep, rm: a session id or a kept conversation's name")
    p.add_argument("name", nargs="?", help="with keep: the name to keep it under")
    p.add_argument("-v", "--voice", help="with on: the voice to speak in")
    p.add_argument("-l", "--language", help="with on: the language (default en)")
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("standby", help="listen for a wake word, without taking the machine")
    p.add_argument("action", choices=["on", "off", "wait", "status"])
    p.add_argument("wake_word", nargs="?", help='with on: the phrase, e.g. "hey Elli"')
    p.set_defaults(func=cmd_standby)

    p = sub.add_parser("pose", help="the pose library: list, save a photo's pose, rename or delete saved poses")
    pose_sub = p.add_subparsers(dest="pose_command")
    p.set_defaults(func=cmd_pose, json=False)
    q = pose_sub.add_parser("list", help="the saved poses (default)")
    q.add_argument("--json", action="store_true", help="print them as JSON")
    q = pose_sub.add_parser("save", help="draw the pose in a photo and save it under a name (uses the GPU briefly)")
    q.add_argument("photo", type=Path)
    q.add_argument("name", help="lower-case words joined by hyphens, e.g. look-back-hand-on-hip")
    q.add_argument("-d", "--description", required=True, help="one line: the pose and the framing")
    q.add_argument("--replace", action="store_true", help="overwrite a saved pose of the same name")
    q = pose_sub.add_parser("show", help="one saved pose: description, size and the paths of its files")
    q.add_argument("name")
    q.add_argument("--json", action="store_true", help="print it as JSON")
    q = pose_sub.add_parser("refresh", help="redraw saved poses from their stored photos (uses the GPU briefly)")
    q.add_argument("names", nargs="*", metavar="NAME", help="the poses to redraw (default: all)")
    q = pose_sub.add_parser("rm", help="delete a saved pose (its map, photo, keypoints and description)")
    q.add_argument("name")
    q = pose_sub.add_parser("mv", help="rename a saved pose")
    q.add_argument("old")
    q.add_argument("new")
    q.add_argument("--replace", action="store_true", help="overwrite a saved pose that has the new name")

    p = sub.add_parser("log", help="review delegations by Claude: per-task stats, verdicts and problem notes")
    p.add_argument("--notes", type=int, default=10, help="how many recent problem notes to show")
    p.set_defaults(func=cmd_log)

    args = parser.parse_args(argv)
    if args.on:
        os.environ[core.REMOTE_ENV] = args.on
    try:
        if core.remote_name() and args.command in THERE_ONLY:
            raise DenError(f"{THERE_ONLY[args.command]}; run it on {core.remote_name()}: den {args.command} …")
        return args.func(args) or 0
    except DenError as e:
        print(f"den: {e}", file=sys.stderr)
        return 1
