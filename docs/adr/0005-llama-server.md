---
status: accepted (broker, delegation and CLI built; pi unchanged)
---

# The LLM runs on llama-server, and a swap keeps the conversation's cache

Every image swap unloads the LLM (ADR 0002), and Ollama throws the model's cache away when it
unloads. So after every image, the next chat turn loaded the model again and re-read the whole
conversation before it could answer: on the 27B dense model pi moved to, 14 s of loading and
26 s of re-reading 12.5k tokens, growing with the conversation up to a minute at 32k. With pi
making an image every few turns, that was most of the waiting.

Ollama has no way to keep that cache across an unload: no endpoint saves or restores it, and the
server settings offer nothing for it. llama.cpp's own server, which Ollama is built on, does:
`POST /slots/{id}?action=save` and `?action=restore` with `--slot-save-path` write a slot's cache
to a file and read it back.

## Decisions

- **llama-server replaces Ollama for running models.** The broker starts `llama-server` on the
  requested model as its own child process, passes pi's `/v1` requests to it, and stops it for a
  swap, a release, `den mode off`, a model change and `[llm] keep_alive` of idle time. den's own
  delegation moved from Ollama's native `/api/chat` to `/v1/chat/completions`; Ollama's native
  run endpoints now answer with an error pointing to `/v1`. Replacing rather than running both
  keeps one way for a model to hold the GPU.
- **Ollama stays as the model store.** `den model` still downloads with `ollama pull`, its
  catalogue and download endpoints still pass through the broker, and `/api/show` says which GGUF
  file a model is — the broker starts llama-server on that file, so nothing is downloaded or
  copied twice. Ollama remains a system service that only a human starts; when it's down, a model
  can't be looked up and the request says so. A model Ollama itself still holds (a client that
  went around den) is unloaded before a swap and when the broker starts.
- **The cache is saved before the server stops and restored after it starts.** Measured on the
  27B model with a 10.8k-token conversation: saving took 0.2 s (an 827 MiB file), starting the
  server again 4.5–6.6 s, restoring 0.1–0.5 s. In pi's flow — thinking on, a turn ending in a
  tool call, then the tool's result and the next message — the turn after a swap read 40 new
  tokens with 11,300 from the cache, the same as a turn with no swap at all. Before, that turn
  re-read everything.
- **One slot, one saved file per model, only the latest.** `--parallel 1`, so the saved slot is
  always the conversation that ran last; each model's file is overwritten by its next save. The
  files are a computed form of the conversations, so they live in `~/.cache/den/slots/` with mode
  700 (`DEN_SLOTS` overrides it).
- **A UNIX socket instead of a port.** llama-server listens on `$XDG_RUNTIME_DIR/den/llm.sock`
  (mode 700 folder; `DEN_LLM_SOCKET` overrides it), so nothing else on the machine can reach it,
  not even another local program. That's tighter than the API key first planned, and than Ollama,
  whose port is open to every local user.
- **The broker stops its own server on shutdown.** `den.service` uses `KillMode=mixed`, so
  `systemctl restart den` signals only the broker, which saves the cache and stops llama-server;
  a signal to the whole group would kill the server before the save.
- **The server fits itself to the machine.** `--fit on` places the model across GPU and RAM, as
  Ollama did. A model whose GGUF carries its own multi-token-prediction head
  (`*.nextn_predict_layers`) starts with `--spec-type draft-mtp`, which kept Ollama's speed-up:
  8.1 tokens/s against Ollama's 6.4–7.4 on the same model. Extra arguments go in
  `[llm] server_args`.

## Consequences

- **The restore only helps a conversation that continues exactly.** The model pi uses is a hybrid
  one: some layers keep a running state instead of a key/value cache, so the server can reuse a
  cache only when the new prompt extends the saved sequence exactly, or roll back to a checkpoint
  it made in memory. Checkpoints aren't saved with the slot. A chat template that re-renders the
  previous answer differently from how it was generated (thinking switched off, in a test) broke
  the match and the turn re-read everything. pi's flow matched.
- **A delegation between two pi turns replaces pi's cached conversation**, since there's one
  slot; pi's next turn then re-reads once. Two slots would fix it at the cost of the second
  slot's memory.
- **pi's Qwen models need `thinkingFormat: "qwen-chat-template"`.** Ollama read pi's
  `reasoning_effort`; llama-server doesn't, so thinking stayed on whatever pi's level said. With
  it, pi sends `chat_template_kwargs.enable_thinking` and `preserve_thinking`, thinking switches
  off and on with the level, and the previous answers re-render exactly as generated: with
  thinking off, the turn after a swap read 135 new tokens out of 20k. Changing the thinking level
  mid-conversation still re-renders every earlier answer, so that one turn re-reads everything
  (40 s at 20k tokens).
- **The level itself only reaches the model as a token budget.** That format sends thinking as a
  bare on/off, and the Qwen template takes no effort level (`supports_reasoning_effort` is
  false in the server's template capabilities), so low, medium and high were one setting: a turn
  thought 500–2000 tokens whichever was picked, minutes at a few tokens a second. llama-server
  does take a per-request budget, so pi's models get
  `thinkingTokenBudgetField: "thinking_budget_tokens"` and the level becomes the matching entry
  of `settings.json`'s `thinkingBudgets`. Measured: with a 48-token budget the thought ended at
  48 tokens and the answer followed cleanly, where the same request sent with `reasoning_effort`
  instead ran past a 400-token cap without answering at all. pi's default budgets (2048 / 8192 /
  16384, capped at `maxTokens - 1024`) are far more than a turn here writes, so they are set by
  hand.
- **A model's vision projector is loaded when Ollama has one** (`--mmproj`, `[llm] vision`), so a
  caller can send images — pi's `read` tool, for one. It costs about 1 GB. A model can list the
  vision capability and ship without the projector file, so den (and `setup.sh`, for pi's model
  list) go by the file. Turning image input on for a model changed how pi sent an earlier message
  of the conversation, and that turn re-read everything once.
- **64k context with an 8-bit cache.** On the 27B hybrid model only every fourth layer keeps a
  key/value cache (64 KiB per token at f16), so context is cheap: measured with 22.6k tokens in
  context, 32k read 477 and wrote 4.4 tokens/s in 11.9 GiB of RAM; 64k with `q8_0` keys and values
  read 462 and wrote 4.3 in 12.5 GiB; 128k with `q8_0` read 435 and wrote 3.3 in 15.5 GiB. 64k
  doubles pi's room before it compacts (a full summary written at a few tokens per second) for
  almost nothing; 128k would cost a quarter of the writing speed on every turn. `--fit` put the
  extra cache in RAM, not on the GPU.
- **One model at a time.** Ollama could hold several; llama-server holds the one it was started
  with, and a request for another model saves the first one's cache and restarts the server.
- **llama.cpp reads a GGUF more strictly than Ollama, so a model that ran before may not load.**
  Ollama has its own loader; llama.cpp validates the metadata and refuses the file when something
  is off. One MoE download stopped working this way: its
  `<arch>.rope.dimension_sections` holds three values where llama.cpp wants four, and the server
  exits during loading with that key named. den reports it as the load failing and points at
  `~/.local/state/den/llama-server.log`, which carries the reason. There is no way around it from
  den's side, since `--override-kv` takes only int, float, bool and str, not arrays; another
  upload of the same model, converted by a tool that writes the fourth value, loads unchanged.
  A model's header can be read from Ollama's registry with a ranged GET before downloading it,
  which is worth doing when a model is large.
- **llama-server must be installed** (`llama-cpp` with a CUDA backend, e.g. `pacman -S llama-cpp
  ggml-cuda`), and `setup.sh` checks for it. Anything that spoke Ollama's native run API to the
  broker has to use `/v1`.
