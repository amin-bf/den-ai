---
status: accepted (both sides measured; most image workflows still lack their models)
---

# den runs on macOS too, behind one platform layer

den was built on a Linux box with a discrete 12 GB card, and said so in the places where a
program meets the system it runs on: `systemctl --user` started and stopped ComfyUI,
`/proc/meminfo` said how much RAM was free, the LLM's socket went under `/run/user`, and
every hint named `systemctl`, `journalctl` or `pacman`. None of that exists on macOS.

Nothing else was in the way. The broker, the swaps, the delegation, the pose library and the
whole image side are plain Python over HTTP, and [ADR 0005](0005-llama-server.md) had already
moved the LLM onto llama-server, whose Metal build is as well supported on Apple Silicon as
its CUDA build is on Linux. Measured on a 32 GB Apple Silicon machine: the server was ready in
1.6 s, answered over its UNIX socket, and saved and restored its conversation cache across a
restart — with no change to `llm.py` beyond where the socket goes.

## Decisions

- **One module knows which system this is.** `den/platform.py` answers three questions —
  how a service is started and stopped, where a private socket may live, and how much RAM is
  free — and nothing else in den asks. A third system means a branch in one file. The hints
  in error messages come from there too: telling someone to run `systemctl` on a machine with
  no systemd is worse than saying nothing.
- **launchd agents, written from templates.** `launchd/*.plist` are the counterparts of
  `systemd/den.service` and the `comfyui.service` setup writes. launchd expands no home
  directory of its own, so `setup.sh` fills in the placeholders; committing absolute paths
  would tie the repo to one machine. `KeepAlive`/`SuccessfulExit` is `Restart=on-failure`,
  `ThrottleInterval` is `RestartSec`, and `ExitTimeOut` is `TimeoutStopSec` — launchd signals
  the job's own process and only kills the group after that timeout, which is what
  `KillMode=mixed` asks systemd for, and what the broker needs to save the cache.
- **A launchd agent carries its own PATH.** An agent starts with a bare one and the broker
  has to find `llama-server`, so the plist is written with a PATH built from where this
  machine keeps things.
- **Starting and stopping wait.** `launchctl bootout` returns before launchd has torn the job
  down, and `bootstrap` returns before it appears; both were found the hard way, the first by
  a second setup run that left the broker neither stopped nor started, the second by a start
  loop that asked whether ComfyUI was running and was told no while it was loading. systemd
  does this in one step; here the waiting is den's.
- **Free RAM is `vm_stat`, added up.** macOS has no single `MemAvailable`. Free pages alone
  read far too low, so den adds the inactive, speculative and purgeable pages: what a model
  could take before anything swaps. This matters more here, not less — the GPU shares this
  memory, so a model is not off in a card of its own, and `[limits] min_free_ram_gb` is the
  gate that keeps it from being taken. Before this the number was unreadable, and that limit
  silently never fired.
- **The socket goes in `$TMPDIR`.** macOS has no `$XDG_RUNTIME_DIR`, but its `$TMPDIR` is
  already per-user and mode 700, and short enough: a UNIX socket path is capped near 104
  bytes and this one comes to about 60.
- **Homebrew is where the parts come from,** and Ollama is the user's own service there
  (`brew services start ollama`), so the rule that only a human starts it holds without sudo
  being involved at all.
- **PyTorch needs no index on macOS.** Its ordinary wheels carry Metal, where the Linux
  install picks a CUDA one.

## Consequences

- **The Linux box is unaffected.** Every hint and path the systemd branch produces is what
  was there before, checked string by string against the previous commit. The only changes
  visible there are in `setup.sh`: it now looks for a newer interpreter by name instead of
  failing when `python3` is old, and one word of a message moved.
- **macOS ships a Python too old for den, ahead of any newer one.** The system `python3` is
  3.9 and comes before Homebrew's on PATH, so `#!/usr/bin/env python3` in `bin/den` finds the
  wrong one. Setup picks a good interpreter for the service, which is why the broker runs
  regardless, and says that the PATH is the user's to fix for the `den` command itself.
- **fp8 buys nothing on Metal, so the quantised files are the wrong ones to keep here.**
  Measured: `torch.float8_e4m3fn` and `float8_e5m2` cannot be allocated on an MPS device at
  all (torch 2.14, "Undefined type Float8_e4m3fn"), and ComfyUI answers
  `supports_fp8_compute: False` and picks `float16` for the UNET. So an fp8 checkpoint is
  dequantised on load: a 9.4 GB file occupies about 19 GB, the same as the full-precision
  original, at lower quality. Where a workflow's model exists in bf16 or fp16, that is the
  file this machine wants; the fp8 and int8 files are a trade for VRAM that Metal doesn't
  make. The same reasoning runs the other way for the LLM, where llama.cpp quantises for
  itself and a *higher* quant is what the extra memory buys.
- **Images work, and the step count is what costs.** Measured with an SDXL workflow at
  896x1152 and 30 steps: 105.7 s cold (7 s of it starting ComfyUI) and 91.8 s warm, GPU
  pinned at 100% and 27 W, 18.6 GiB held, no swap. That is about 3 s a step, against the 8 s
  the same workflow takes in total on the 12 GB card — an order of magnitude, and nothing to
  tune: it is what this GPU does. So the distilled few-step models are not a convenience
  here but the point, since they are the difference between iterating every 15 seconds and
  every 90. A workflow's step count now predicts its cost better than its size does, and a
  35-step one is a deliberate wait rather than a default.
- **The image side's remaining workflows are untested here.** ComfyUI installs with Metal
  and the broker starts and stops it through launchd in about five seconds and one second.
  What no measurement covers yet is the workflows themselves: the timings in the README are
  from a 12 GB CUDA card, and on unified memory a workflow's text encoder and its UNET
  compete for one pool rather than taking turns against a separate system RAM. They will
  need their own pass.
- **Metal is not a choice between backends.** On this hardware the GPU is reached through
  Metal or not at all; CUDA needs another card, and MLX — the one real alternative for the
  LLM — has no equivalent of llama-server's slot save and restore, which is the whole point
  of [ADR 0005](0005-llama-server.md). The wired-memory cap applies to any of them, and is
  raised with `sudo sysctl iogpu.wired_limit_mb=…`, which den never does itself.
- **One pool, so what llama-server adds on top of the weights is what decides the model.**
  A 25 GB model that fits a 32 GB machine on paper failed to load: Metal answered
  `command buffer … failed with status 5` and swap went from 0 to 2.4 GB. The weights were
  not the problem. den had also asked for a 64k KV cache, a vision projector (~1 GB) and,
  because the GGUF carries an MTP head, `--spec-type draft-mtp` — which builds a second
  draft context against the same model. With `num_ctx` at 16384, `vision = false` and
  `--spec-type none` appended to `server_args` (last wins), the same model loaded and ran at
  **7.65 tok/s generating and 48.6 tok/s reading, GPU pinned at 100% and the CPU idle at
  5%** — nothing spilled. It holds 30.2 of 32 GiB, so there is no room to buy any extra back.
  The Linux box runs this same model at Q4 and ~8 tok/s; this machine runs it at Q6 for the
  same speed.
- **A machine with a card has more memory for a model, not less.** The Linux box pairs a
  12 GB card with 32 GB of system RAM, and `--fit` spreads a model across both: a 44 GB
  pool. Unified memory places a model better — all of it is GPU-reachable — but there is
  one 32 GB pool for the model, macOS and everything else. So the Mac's ceiling is lower,
  and the choice it offers is a higher quant of a smaller model rather than a bigger one.
- **The GPU's share of memory is a setting, and it forgets.** macOS wires about 75% of
  unified memory to the GPU — ~24 GB of 32 — which is below what a 20 GB model, a 64k cache,
  a projector and a draft context need together. `sysctl iogpu.wired_limit_mb` raises it, but
  not across a reboot, so a model that loaded yesterday fails today as a Metal allocation
  error with nothing pointing at the restart. `launchd/ai.den.wiredlimit.plist` sets it at
  every boot; it is a system daemon and needs root, so setup fills it in and prints the two
  commands rather than running them, as it does for Ollama. Measured on a 32 GB Apple Silicon machine:
  at 28 GB the 20 GB model runs with its full context, projector and draft context, holding
  29.4 of 32 GiB with no swap growth, at 7.47 tok/s generating and 46.4 reading, with the
  MTP draft accepted 42% of the time. The 25 GB model reaches the same speed but only by
  giving up all three.
- **A dead Metal backend is not noticed.** After the allocation failure llama-server stayed
  up and answered every later request with `Compute error`, its log saying the backend
  "is in error state from a previous command buffer failure - recreate the backend to
  recover". den has no check for that: the server is running, so the broker keeps sending to
  it. Recovering means restarting the server, which only `den unload` or a model change
  currently does.
- **`~/Library/LaunchAgents` is not `~/.config/systemd/user`.** An agent written there stays
  after a `git clean`, as a systemd unit does; removing den means booting the agents out and
  deleting the plists.
