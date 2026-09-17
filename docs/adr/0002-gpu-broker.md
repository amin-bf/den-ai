---
status: accepted (LLM side built; the image side follows in ADR 0003)
---

# Put every GPU user behind one broker

The 12 GB card holds one heavy model at a time: the LLM and every image model each fill it.
In step 1 of image generation, pi loaded the LLM while ComfyUI was running Chroma, and the
card ran out of memory twice and then segfaulted. The clients (the `den` CLI, Claude's MCP
server, pi) can't coordinate with each other, so **`den serve`, a stdlib HTTP server at
`127.0.0.1:11435` run as a systemd user service, sits in front of the GPU**. Every client talks
to it; nothing else talks to Ollama (11434) or, later, ComfyUI (8188) directly.

## Decisions

- **Pass-through, not a new API.** The broker passes Ollama's native `/api/…` and OpenAI-style
  `/v1/…` requests through as they are, streaming included, so pi only changes its `baseUrl`.
  Its own endpoints are `GET /status` and `POST /mode`.
- **In-flight tracking instead of a busy flag.** The broker knows each running request (caller,
  endpoint, model, age). Callers identify themselves with `X-Den-Caller` (`cli`, `claude`);
  pi shows up by its user agent. Requests that don't use the GPU (`/api/tags`, `/api/ps`,
  `/api/show`, pulls, a bare `keep_alive: 0` unload, …) aren't tracked and work in every mode.
- **The broker owns mode switches.** `den mode` asks the broker, which streams its progress.
  A switch that turns the LLM off refuses new LLM requests at once, waits for the running ones,
  unloads every model and only then saves the mode. It never returns early.
  - `--now` cancels the running requests instead. The broker closes their upstream connection,
    Ollama stops generating, and the client gets an error in its own stream format.
  - Ctrl+C on `den mode` drops the switch (checked every second), and new requests are
    accepted again.
- **Broker down means failure, with no fallback.** Clients print
  `den broker not running …: systemctl --user start den`, even in `llm` mode. A silent
  direct-to-Ollama path would bring back the OOM this exists to prevent.
- **Mode is enforced at the broker.** In `off` (and later `image`), LLM requests from any client,
  pi included, get `the local LLM is off … den mode llm`.
- **The broker applies `[llm] keep_alive` to every caller.** Pi never sends one, so Ollama
  would unload its model after its 5-minute default. Native requests without `keep_alive` get
  the configured value added. Ollama ignores `keep_alive` in `/v1` bodies (tested), so after a
  successful `/v1` request the broker sends a bare `{"model", "keep_alive"}` load call, which
  takes ~15 ms on a loaded model. It sends it while the request still counts as in flight, so a
  mode switch can't unload in between and have the call reload the model. It skips the call
  when another request wants a different model.
- **Same no-restart rule.** The broker reads `config.toml` and `state.json` on every request;
  only its own listen address (`[broker] base_url`) needs a restart.

## Evidence: Ollama and `keep_alive: 0` (2026-09-17, Ollama 0.34.0)

The swap relies on how Ollama treats an unload that arrives while a request is running:

- **Ollama doesn't cancel the request.** The unload call returns at once
  (`done_reason: "unload"`), the running generation finishes normally, and the model leaves
  memory right after it. So the unload response doesn't mean the GPU is free: the broker waits
  until `/api/ps` is empty.
- **Closing the connection does cancel.** When the caller hangs up, the next request is answered
  in 0.18 s, against 6.7 s behind a generation that is still running (Ollama runs one request at
  a time and queues the rest).

## Queue caps (decided now, built with the image side)

When both sides are wanted (`both` mode), requests for the loaded side run first, then one swap
happens. The loaded side stops taking new requests after **120 s or 4 requests**, whichever comes
first, counted from when the other side started waiting. A running request always finishes: caps,
mode switches and the idle timeout never swap in the middle of one. Step-1 timings: 4 fast images
take 8–30 s and 4 Chroma images 150–200 s, so Chroma batches hit the time cap.

## Consequences

- **Clients that bypass the broker aren't covered:** `ollama run`, `ollama launch claude`,
  direct `curl` to 11434, and the ComfyUI web UI used on its own. Downloading (`ollama pull`, which
  `den model` also runs) doesn't use the GPU and goes straight to Ollama.
- **Pi fails loudly when the LLM is off,** where before it quietly loaded the model.
- Code changes to the broker need `systemctl --user restart den`, and code changes to the
  MCP server need `/mcp` in running Claude Code sessions.
