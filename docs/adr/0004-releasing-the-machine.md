---
status: accepted (broker, CLI, Claude's tool and the pi extension built)
---

# Releasing the machine, and not taking it back while it's busy

A local model doesn't only hold the GPU. The main LLM is an MoE split between the GPU and RAM:
it keeps several GB of RAM and generates on the CPU as well as the card, and ComfyUI keeps GB of
RAM of its own (ADR 0003). So den competes with whatever else the machine is doing — a test
suite, a build, a benchmark — and those are exactly the jobs an agent starts while den is
loaded.

Two things follow: someone must be able to ask for the machine back, and den must not take it
again while that work runs.

## Decisions

- **`den unload` is the release, and it refuses requests while it runs.** It already drained the
  running requests and unloaded both sides; what was missing is that new requests only *waited*,
  so a request arriving mid-release loaded a side again right behind it. A release is an explicit
  "I need this machine now", so requests are refused while it runs — including ones already
  queued, which would otherwise be the first to reload.
- **The refusal ends with the release, unlike `den mode off`.** The mode stays on, the tools stay
  listed, and the next request loads its side again. `den mode off` remains the kill switch that
  keeps refusing until `den mode on`. Nothing new is needed for "off until I say so".
- **A side that isn't loaded isn't loaded while the machine is busy** (`[limits]` in
  `config.toml`): the 1-minute load average per CPU above `max_load_per_cpu`, or available RAM
  (`MemAvailable`) below `min_free_ram_gb`, and the request is refused with the numbers and the
  limit in the message. Defaults: 0.8 per CPU and 4 GB.
- **Only *loading* is gated; a loaded side keeps serving.** Serving from a model that's already in
  memory costs what it always costs, and cutting a conversation off mid-way to protect the
  machine helps nobody. It also means den can't gate itself out: while the LLM generates, the
  load is high but its side is loaded, so the limits never see its own work.
- **The 1-minute load average, not instantaneous CPU use.** It ignores a short spike (a compile,
  a page load) and catches the sustained load that matters. No hysteresis beyond that: the
  average is already smoothed.
- **Refused at call time, not by hiding the tools.** Availability drives the MCP tool list, and
  the server watches it every couple of seconds; folding load into availability would make
  `local_llm` and `generate_image` flicker in and out of Claude's tool list as the load crosses
  the limit. The tools stay listed and the call fails with the reason, which also tells the
  caller something it can act on.
- **Claude releases through a tool of its own, `release_resources`.** It's listed whatever the
  mode, model or workflows are — it frees the machine instead of using it — and it answers with
  what it freed plus the current load and available RAM, so an agent about to start a long test
  run can hand the resources back first and see that it worked. Calling it when nothing is loaded
  is cheap and harmless, so it needs no separate status tool to check first.
- **`den status` and pi's footer show the same numbers,** with the reason loading is held off
  when it is, so a refused call is never a mystery.

## Consequences

- Under sustained load from other work, the local LLM and image generation are simply
  unavailable until it settles — deliberately. Raise `[limits]` (or set either to 0) when the
  machine should keep serving under load.
- A release plus the limits work together: Claude releases before its heavy job, and the limits
  keep pi or a cron job from loading a model back into the middle of it.
