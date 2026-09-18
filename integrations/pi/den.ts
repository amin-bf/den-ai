/**
 * den for pi: image generation through den's GPU broker.
 *
 * - `generate_image`: a tool the model calls on its own, listed while a workflow can run and
 *   den isn't off (`den mode off`). Its description and parameters come
 *   from `den image --json`, the same text Claude's MCP tool uses.
 * - `/imagine [hint] [--flag value …]`: the chat model writes a prompt from the conversation and
 *   the hint, an editable box shows it, and Enter generates. Ctrl+N edits the negative prompt in
 *   the same box. When the image model is still loaded, the box opens with the last prompt
 *   instead (Ctrl+R asks the chat model, which swaps). The flags are den's own options, read
 *   from the request spec, so one den gains later needs no change here.
 * - Results show inline (kitty graphics) with the path as a file:// link. The model gets text
 *   only: path, workflow, seed and settings.
 * - The footer explains broker waits and swaps while pi works.
 *
 * Only talks to the broker (`den serve`) and the `den` CLI. Link this file into
 * ~/.pi/agent/extensions/ (setup.sh does it); `/reload` picks up changes.
 */

import { closeSync, openSync, readFileSync, readSync } from "node:fs";
import http from "node:http";
import { homedir } from "node:os";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import type { ExtensionAPI, ExtensionCommandContext, ExtensionContext, Theme } from "@earendil-works/pi-coding-agent";
import { DynamicBorder, getSelectListTheme } from "@earendil-works/pi-coding-agent";
import {
  type Component,
  Container,
  Editor,
  getCapabilities,
  getCellDimensions,
  hyperlink,
  Image,
  matchesKey,
  Text,
} from "@earendil-works/pi-tui";

const TOOL = "generate_image";
const MESSAGE_TYPE = "den-image";
const CALLER = "pi";
const DEN = process.env.DEN_BIN || "den";
const STATUS_POLL_MS = 1500;
const MAX_IMAGE_COLS = 60;
const MAX_IMAGE_ROWS = 24;
// How much of the conversation /imagine shows the chat model (characters, newest kept).
const CONVERSATION_CHARS = 24000;

type Spec = {
  broker: string;
  mode: string;
  image_on: boolean;
  unavailable: string | null;
  default: string | null;
  workflows: string[];
  edits: string[];
  description: string | null;
  parameters: { type: "object"; properties: Record<string, any>; required: string[] } | null;
};

type ImageResult = {
  workflow: string;
  seed: number;
  width: number | null;
  height: number | null;
  /** What it was made with — workflow, seed, size, settings, extras — built by den for every caller. */
  summary?: string[];
  paths: string[];
  copies: string[];
  seconds: number;
  waited_s: number;
  [key: string]: unknown;
};

type Generation = { result: ImageResult; prompt: string; negative?: string };

type Draft = {
  workflow: string;
  prompt: string;
  negative?: string;
  size?: string;
  image?: string;
  /** Every other request option, by den's own name: references, seed, cfg, loras, control, strength… */
  options?: Record<string, any>;
};

type BrokerStatus = {
  loaded: string | null;
  swapping: string | null;
  pending_mode: string | null;
  releasing?: string[] | null;
  too_busy?: string | null;
  ollama?: { error?: string };
  inflight: { id: number; side: string; caller: string; model: string | null }[];
  waiting: { id: number; side: string; caller: string; model: string | null }[];
};

// --- den and the broker ---

async function loadSpec(pi: ExtensionAPI): Promise<Spec> {
  const run = await pi.exec(DEN, ["image", "--json"], { timeout: 15000 });
  if (run.code !== 0) {
    throw new Error(`${DEN} image --json failed: ${(run.stderr || run.stdout).trim() || `exit ${run.code}`}`);
  }
  return JSON.parse(run.stdout);
}

function brokerDown(broker: string, err: Error): Error {
  return new Error(`den broker not running at ${broker} (${err.message}); start it with: systemctl --user start den`);
}

function getStatus(broker: string): Promise<BrokerStatus> {
  return new Promise((done, fail) => {
    const req = http.get(`${broker}/status`, { headers: { "X-Den-Caller": CALLER }, timeout: 3000 }, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => (body += chunk));
      res.on("end", () => {
        try {
          done(JSON.parse(body));
        } catch (e) {
          fail(e as Error);
        }
      });
    });
    req.on("timeout", () => req.destroy(new Error("timed out")));
    req.on("error", (e) => fail(brokerDown(broker, e)));
  });
}

/**
 * POST /image and hand each progress line to onLine; resolves with the result line.
 * Uses node:http rather than fetch, whose body timeout would cut long generations off.
 * Aborting closes the connection, and the broker cancels the request.
 */
function requestImage(
  broker: string,
  request: Record<string, unknown>,
  signal: AbortSignal | undefined,
  onLine: (msg: Record<string, any>) => void,
): Promise<ImageResult> {
  return new Promise((done, fail) => {
    if (signal?.aborted) return fail(new Error("cancelled"));
    const body = JSON.stringify(request);
    let settled = false;
    const finish = (err: Error | null, result?: ImageResult) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener("abort", onAbort);
      err ? fail(err) : done(result!);
    };
    const req = http.request(
      `${broker}/image`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body), "X-Den-Caller": CALLER },
      },
      (res) => {
        let buffer = "";
        res.setEncoding("utf8");
        res.on("data", (chunk: string) => {
          buffer += chunk;
          if (res.statusCode !== 200) return;
          let newline: number;
          while ((newline = buffer.indexOf("\n")) >= 0) {
            const line = buffer.slice(0, newline).trim();
            buffer = buffer.slice(newline + 1);
            if (!line) continue;
            const msg = JSON.parse(line);
            if (msg.error) return finish(new Error(`den: ${msg.error}`));
            if (msg.result) return finish(null, msg.result);
            onLine(msg);
          }
        });
        res.on("end", () => {
          if (res.statusCode !== 200) {
            let message = buffer.trim();
            try {
              const error = JSON.parse(message).error;
              message = typeof error === "string" ? error : (error?.message ?? message);
            } catch {}
            return finish(new Error(message.startsWith("den:") ? message : `den broker: HTTP ${res.statusCode}: ${message}`));
          }
          finish(new Error("the broker ended the image request without a result"));
        });
        res.on("error", (e) => finish(e));
      },
    );
    const onAbort = () => {
      req.destroy();
      finish(new Error("cancelled"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
    req.on("error", (e) => finish(signal?.aborted ? new Error("cancelled") : brokerDown(broker, e)));
    req.end(body);
  });
}

function progressText(msg: Record<string, any>): string | null {
  if (msg.waiting) {
    const running = (msg.waiting.running ?? []).map((r: any) => `${r.caller} ${r.side}`).join(", ");
    return `waiting: ${msg.waiting.reason}${running ? ` (running: ${running})` : ""}`;
  }
  if (msg.unloading) return `unloading the LLM (${msg.unloading.join(", ")})`;
  if (msg.starting) return "starting ComfyUI";
  if (msg.stopping) return "stopping ComfyUI";
  if (msg.generating) {
    const g = msg.generating;
    const [workflow, ...rest] = g.summary as string[];
    return `${g.edit ? "editing" : "generating"} with ${workflow} (${rest.join(", ")})`;
  }
  return null;
}

/** One line of what was used: den's summary, plus the time only this side knows how to phrase.
 *  A generation saved before den sent a summary falls back to what its result still carries. */
function summary(r: ImageResult): string {
  const made = r.summary ?? [r.workflow, `seed ${r.seed}`, ...(r.width ? [`${r.width}x${r.height}`] : [])];
  return [...made, `${r.seconds}s${r.waited_s >= 1 ? `, waited ${Math.round(r.waited_s)}s` : ""}`].join(" · ");
}

function resultText(g: Generation): string {
  const lines = [...g.result.paths.map((p) => `saved: ${p}`), ...g.result.copies.map((p) => `copied to: ${p}`)];
  lines.push(`[${summary(g.result)}]`);
  return lines.join("\n");
}

function statusText(s: BrokerStatus): string | null {
  // Nobody else is watching the broker's journal: say it here so it gets started.
  if (s.ollama?.error) return `den: ollama is down — sudo systemctl start ollama`;
  if (s.pending_mode) return `den: turning den ${s.pending_mode}`;
  if (s.releasing?.length) return `den: releasing the ${s.releasing.join(" and ")} side (den unload)`;
  if (s.swapping === "llm" || s.swapping === "image") return `den: swapping the GPU to the ${s.swapping} side`;
  if (s.swapping === "idle") return "den: stopping the idle image side";
  if (s.swapping === "mode") return "den: unloading to turn den off";
  if (s.swapping === "unload") return "den: unloading the GPU (den unload)";
  const w = s.waiting[0];
  // Only worth saying while nothing waits on it: it's the reason the next request would be refused.
  if (!w) return s.too_busy && !s.loaded ? `den: too busy to load a model — ${s.too_busy}` : null;
  const others = s.inflight.filter((r) => r.side !== w.side).map((r) => `${r.caller} ${r.side}`);
  const more = s.waiting.length > 1 ? ` (+${s.waiting.length - 1})` : "";
  return others.length
    ? `den: ${w.caller} ${w.side} waits for ${others.join(", ")} to finish${more}`
    : `den: ${w.caller} ${w.side} waits for a swap${more}`;
}

// --- paths and the session ---

/** A path argument as the broker wants it: absolute, with ~ and a stray leading @ handled. */
function absolutePath(value: string, cwd: string): string {
  let path = value.trim().replace(/^@/, "");
  if (path === "~" || path.startsWith("~/")) path = homedir() + path.slice(1);
  const resolved = resolve(cwd, path);
  return path.endsWith("/") && !resolved.endsWith("/") ? `${resolved}/` : resolved;
}

/** Image generations on the current branch, oldest first: tool results and /imagine messages. */
function generations(ctx: ExtensionContext): Generation[] {
  const found: Generation[] = [];
  for (const entry of ctx.sessionManager.getBranch() as any[]) {
    const details =
      entry.type === "message" && entry.message?.role === "toolResult" && entry.message.toolName === TOOL && !entry.message.isError
        ? entry.message.details
        : entry.type === "custom_message" && entry.customType === MESSAGE_TYPE
          ? entry.details
          : undefined;
    if (details?.result?.paths?.length) found.push(details);
  }
  return found;
}

function lastImage(ctx: ExtensionContext): string | undefined {
  return generations(ctx).at(-1)?.result.paths[0];
}

function toBrokerRequest(params: Record<string, any>, ctx: ExtensionContext): Record<string, unknown> {
  const request: Record<string, any> = { ...params };
  if (typeof request.image === "string") {
    if (request.image.trim() === "last") {
      const last = lastImage(ctx);
      if (!last) throw new Error('image "last": no image was generated in this session yet');
      request.image = last;
    } else {
      request.image = absolutePath(request.image, ctx.cwd);
    }
  }
  if (typeof request.out === "string") request.out = absolutePath(request.out, ctx.cwd);
  if (Array.isArray(request.references)) request.references = request.references.map((p: string) => absolutePath(p, ctx.cwd));
  if (request.control?.image) request.control = { ...request.control, image: absolutePath(request.control.image, ctx.cwd) };
  delete request.switch_back; // pi's next step is always an LLM request, which swaps back anyway
  return request;
}

// --- inline images ---

// Row/column diacritics for kitty's Unicode placeholders (rowcolumn-diacritics.txt, first 64).
const DIACRITICS = [
  0x0305, 0x030d, 0x030e, 0x0310, 0x0312, 0x033d, 0x033e, 0x033f, 0x0346, 0x034a, 0x034b, 0x034c, 0x0350, 0x0351,
  0x0352, 0x0357, 0x035b, 0x0363, 0x0364, 0x0365, 0x0366, 0x0367, 0x0368, 0x0369, 0x036a, 0x036b, 0x036c, 0x036d,
  0x036e, 0x036f, 0x0483, 0x0484, 0x0485, 0x0486, 0x0487, 0x0592, 0x0593, 0x0594, 0x0595, 0x0597, 0x0598, 0x0599,
  0x059c, 0x059d, 0x059e, 0x059f, 0x05a0, 0x05a1, 0x05a8, 0x05a9, 0x05ab, 0x05ac, 0x05af, 0x05c4, 0x0610, 0x0611,
  0x0612, 0x0613, 0x0614, 0x0615, 0x0616, 0x0617, 0x0657, 0x0658,
].map((c) => String.fromCodePoint(c));

/**
 * How to draw images here. pi draws them itself where it can, but turns them off inside tmux.
 * Under tmux in kitty, kitty's Unicode placeholders work: tmux moves them like text, and the
 * graphics command reaches kitty through tmux's passthrough (`allow-passthrough on`).
 */
function imageMode(): "tmux-kitty" | "pi" | null {
  const env = process.env;
  if (["none", "0"].includes((env.PI_IMAGE_PROTOCOL ?? "").toLowerCase())) return null;
  if (env.TMUX) {
    const kitty = !!env.KITTY_WINDOW_ID || env.TERM === "xterm-kitty" || env.TERM_PROGRAM === "kitty";
    return kitty && !env.SSH_CONNECTION ? "tmux-kitty" : null;
  }
  return getCapabilities().images ? "pi" : null;
}

function pngSize(path: string): { width: number; height: number } | null {
  const header = Buffer.alloc(24);
  try {
    const fd = openSync(path, "r");
    try {
      readSync(fd, header, 0, 24, 0);
    } finally {
      closeSync(fd);
    }
  } catch {
    return null;
  }
  if (header.readUInt32BE(0) !== 0x89504e47) return null;
  return { width: header.readUInt32BE(16), height: header.readUInt32BE(20) };
}

function fileLink(path: string): string {
  const shown = path.startsWith(`${homedir()}/`) ? `~${path.slice(homedir().length)}` : path;
  return getCapabilities().hyperlinks || process.env.TMUX ? hyperlink(shown, pathToFileURL(path).href) : shown;
}

/** An image file drawn inline, or nothing where the terminal can't. */
class ImageView implements Component {
  private id = 1 + Math.floor(Math.random() * 0xfffffe); // 24 bits: the id is the placeholder's color
  private lines?: string[];
  private width?: number;
  private placed?: string;
  private piImage?: Image;

  constructor(private path: string) {}

  invalidate(): void {
    this.lines = undefined;
    this.piImage?.invalidate();
  }

  render(width: number): string[] {
    if (this.lines && this.width === width) return this.lines;
    this.width = width;
    const mode = imageMode();
    const size = pngSize(this.path);
    if (!mode || !size) {
      this.lines = [];
    } else if (mode === "pi") {
      try {
        this.piImage ??= new Image(readFileSync(this.path).toString("base64"), "image/png", { fallbackColor: (s) => s }, {
          maxWidthCells: MAX_IMAGE_COLS,
          maxHeightCells: MAX_IMAGE_ROWS,
          filename: this.path,
        });
        this.lines = this.piImage.render(width);
      } catch {
        this.lines = [];
      }
    } else {
      this.lines = this.placeholders(width, size);
    }
    return this.lines;
  }

  private placeholders(width: number, size: { width: number; height: number }): string[] {
    const cell = getCellDimensions();
    const aspect = (size.height / size.width) * (cell.widthPx / cell.heightPx); // rows per column
    let cols = Math.max(1, Math.min(width - 2, MAX_IMAGE_COLS));
    let rows = Math.max(1, Math.round(cols * aspect));
    if (rows > MAX_IMAGE_ROWS) {
      rows = MAX_IMAGE_ROWS;
      cols = Math.max(1, Math.min(cols, Math.round(rows / aspect)));
    }
    // Transmit (kitty reads the file itself) and make a virtual placement of cols x rows.
    const geometry = `${cols}x${rows}`;
    if (this.placed !== geometry) {
      const payload = Buffer.from(this.path).toString("base64");
      const command = `\x1b_Ga=T,U=1,f=100,t=f,i=${this.id},c=${cols},r=${rows},q=2;${payload}\x1b\\`;
      process.stdout.write(`\x1bPtmux;${command.replaceAll("\x1b", "\x1b\x1b")}\x1b\\`);
      this.placed = geometry;
    }
    const color = `\x1b[38;2;${(this.id >> 16) & 255};${(this.id >> 8) & 255};${this.id & 255}m`;
    const lines: string[] = [];
    for (let row = 0; row < rows; row++) {
      // The first cell names row and column; the rest continue the row.
      const first = `\u{10EEEE}${DIACRITICS[row]}${DIACRITICS[0]}`;
      lines.push(` ${color}${first}${`\u{10EEEE}${DIACRITICS[row]}`.repeat(cols - 1)}\x1b[39m`);
    }
    return lines;
  }
}

/** A finished generation: its summary, the image and a link per file. */
function generationView(g: Generation, theme: Theme, views: Map<string, ImageView>, showImage = true): Component {
  const box = new Container();
  box.addChild(new Text(theme.fg("muted", summary(g.result)), 0, 0));
  for (const path of [...g.result.paths, ...g.result.copies]) {
    if (showImage && g.result.paths.includes(path)) {
      let view = views.get(path);
      if (!view) {
        view = new ImageView(path);
        views.set(path, view);
      }
      box.addChild(view);
    }
    box.addChild(new Text(theme.fg("accent", fileLink(path)), 0, 0));
  }
  return box;
}

export default function (pi: ExtensionAPI) {
  let spec: Spec | null = null;
  let registered = ""; // the description and schema last registered, to skip identical updates
  let hiddenByUs = false;
  const views = new Map<string, ImageView>(); // for /imagine messages, which have no row state

  // --- the tool follows den's mode and the downloaded models ---

  async function refreshTool(): Promise<Spec | null> {
    try {
      spec = await loadSpec(pi);
    } catch {
      spec = null; // den missing or failing: no tool; /imagine reports the error
    }
    const active = pi.getActiveTools();
    if (!spec?.image_on || !spec.workflows.length || !spec.description || !spec.parameters) {
      if (active.includes(TOOL)) {
        pi.setActiveTools(active.filter((name) => name !== TOOL));
        hiddenByUs = true;
      }
      return spec;
    }
    // Only a real change re-registers: a new tool list makes the model reread the conversation.
    const key = JSON.stringify([spec.description, spec.parameters]);
    if (key !== registered) {
      pi.registerTool(toolDefinition(spec));
      registered = key;
    }
    if (hiddenByUs && !pi.getActiveTools().includes(TOOL)) {
      pi.setActiveTools([...pi.getActiveTools(), TOOL]);
    }
    hiddenByUs = false;
    return spec;
  }

  function toolDefinition(s: Spec) {
    const properties = { ...s.parameters!.properties };
    properties.image = {
      ...properties.image,
      description: `${properties.image.description} "last" is the last image generated in this session.`,
    };
    return {
      name: TOOL,
      label: "Generate image",
      description:
        `${s.description}\n` +
        "Paths may be relative to the working directory. The user sees the image inline in the terminal; " +
        "you only get its path, seed and settings back.\n" +
        "The GPU holds either you (the chat model) or the image model, so this call unloads you. Write your " +
        "text reply first and call generate_image last; several images belong in one message. Your turn ends " +
        "when the images are done, so the user can look at them; you reload on their next message, which " +
        "takes up to a minute.",
      promptSnippet: "Generate or edit an image with a local image model (ComfyUI)",
      parameters: { ...s.parameters!, properties },

      async execute(_id: string, params: Record<string, any>, signal: AbortSignal | undefined, onUpdate: any, ctx: ExtensionContext) {
        const broker = (spec ?? s).broker;
        const request = toBrokerRequest(params, ctx);
        const result = await requestImage(broker, request, signal, (msg) => {
          const text = progressText(msg);
          if (text) onUpdate?.({ content: [{ type: "text", text }], details: { progress: text } });
        });
        const generation: Generation = { result, prompt: String(request.prompt), negative: request.negative as string };
        return {
          content: [{ type: "text", text: `${resultText(generation)}\nThe user sees the image.` }],
          details: generation,
          // The model wrote its reply before calling: end the turn instead of reloading it now.
          terminate: true,
        };
      },

      renderCall(args: Record<string, any>, theme: Theme) {
        const what = args.image ? "edit" : "image";
        const prompt = String(args.prompt ?? "").replace(/\s+/g, " ");
        return new Text(
          `${theme.fg("toolTitle", theme.bold(`${TOOL} `))}${theme.fg("muted", `${args.workflow ?? "default"} ${what}: `)}${theme.fg("dim", prompt)}`,
          0,
          0,
        );
      },

      renderResult(result: any, options: { isPartial: boolean }, theme: Theme, context: any) {
        const text = result.content?.find((c: any) => c.type === "text")?.text ?? "";
        if (options.isPartial) return new Text(theme.fg("muted", text || "sending to the broker"), 0, 0);
        if (context.isError || !result.details?.result) return new Text(theme.fg("error", text), 0, 0);
        return generationView(result.details, theme, (context.state.views ??= new Map()), context.showImages);
      },
    };
  }

  pi.registerMessageRenderer<Generation>(MESSAGE_TYPE, (message, _options, theme) => {
    if (!message.details?.result) return undefined;
    const box = new Container();
    box.addChild(new Text(theme.fg("toolTitle", theme.bold("/imagine ")) + theme.fg("dim", message.details.prompt.replace(/\s+/g, " ")), 0, 0));
    box.addChild(generationView(message.details, theme, views));
    return box;
  });

  pi.on("session_start", async () => {
    views.clear();
    await refreshTool();
  });
  pi.on("before_agent_start", async () => {
    await refreshTool();
  });

  // --- footer status while something waits or swaps ---

  let statusCtx: ExtensionContext | undefined;
  let agentRunning = false;
  let commands = 0;
  let timer: ReturnType<typeof setInterval> | undefined;
  let polling = false;

  async function poll() {
    if (polling || !statusCtx?.hasUI || !spec) return;
    polling = true;
    try {
      const text = statusText(await getStatus(spec.broker));
      statusCtx.ui.setStatus("den", text ? statusCtx.ui.theme.fg("warning", text) : undefined);
    } catch {
      statusCtx.ui.setStatus("den", undefined);
    } finally {
      polling = false;
    }
  }

  function watchStatus(ctx: ExtensionContext) {
    statusCtx = ctx;
    const busy = agentRunning || commands > 0;
    if (busy && !timer) {
      void poll();
      timer = setInterval(poll, STATUS_POLL_MS);
    } else if (!busy && timer) {
      clearInterval(timer);
      timer = undefined;
      if (ctx.hasUI) ctx.ui.setStatus("den", undefined);
    }
  }

  pi.on("agent_start", async (_event, ctx) => {
    agentRunning = true;
    watchStatus(ctx);
  });
  pi.on("agent_settled", async (_event, ctx) => {
    agentRunning = false;
    watchStatus(ctx);
  });
  pi.on("session_shutdown", async () => {
    if (timer) clearInterval(timer);
    timer = undefined;
  });

  // --- /imagine ---

  pi.registerCommand("imagine", {
    description:
      "Generate an image from the conversation: /imagine [hint] [--yes] [--workflow NAME] " +
      "[--image [PATH]] [--negative TEXT] [--reference PATH] [--seed N] [--size WxH] [--lora NAME[:STRENGTH]] " +
      "[--strength 0-1] [--control PATH --control-type TYPE] [--upscale NAME[:FACTOR]] — quote values with spaces",
    getArgumentCompletions(prefix: string) {
      const words = prefix.split(/\s+/);
      const current = words.at(-1) ?? "";
      if (words.at(-2) === "--workflow" || words.at(-2) === "-w") {
        const items = (spec?.workflows ?? []).filter((w) => w.startsWith(current));
        return items.length ? items.map((w) => ({ value: [...words.slice(0, -1), w].join(" "), label: w })) : null;
      }
      if (!current.startsWith("-")) return null;
      const flags = ["--yes", "--workflow", "--image"].filter((f) => f.startsWith(current));
      return flags.length ? flags.map((f) => ({ value: [...words.slice(0, -1), f].join(" "), label: f })) : null;
    },
    handler: async (args, ctx) => {
      commands++;
      watchStatus(ctx);
      try {
        await imagine(args, ctx);
      } catch (e) {
        const message = (e as Error).message;
        if (message !== "cancelled") notify(ctx, message, "error");
      } finally {
        commands--;
        watchStatus(ctx);
      }
    },
  });

  function notify(ctx: ExtensionContext, text: string, level: "info" | "warning" | "error") {
    if (ctx.hasUI) ctx.ui.notify(text, level);
    else console.error(text);
  }

  async function imagine(args: string, ctx: ExtensionCommandContext) {
    await ctx.waitForIdle();
    const s = await refreshTool();
    if (!s) throw new Error(`den isn't available: ${DEN} image --json failed (is den on PATH?)`);
    if (!s.image_on) throw new Error(s.unavailable || "image generation isn't available (see: den image)");
    if (!s.workflows.length) throw new Error("no image workflow can run (see: den image)");
    // The flags come from den's spec, so it has to be in hand before the arguments can be read.
    const options = parseImagineArgs(args, ctx, s);
    if (!options.yes && ctx.mode !== "tui") throw new Error("/imagine needs the interactive UI; pass --yes to skip the prompt box");
    if (options.workflow && !s.workflows.includes(options.workflow)) {
      throw new Error(`workflow ${options.workflow} can't run; available: ${s.workflows.join(", ")}`);
    }
    if (options.image && options.workflow && !s.edits.includes(options.workflow)) {
      throw new Error(`workflow ${options.workflow} doesn't edit; these do: ${s.edits.join(", ") || "none"}`);
    }

    // The image model still loaded: start from the last prompt, so drafting doesn't swap.
    const status = await getStatus(s.broker);
    const last = generations(ctx).at(-1);
    let draft: Draft | null;
    let note = "";
    if (status.loaded === "image" && !status.swapping && last && !(options.yes && options.hint)) {
      const lastFlow = options.image && !s.edits.includes(last.result.workflow) ? (s.edits[0] ?? last.result.workflow) : last.result.workflow;
      draft = {
        workflow: options.workflow ?? lastFlow,
        prompt: last.prompt,
        negative: last.negative,
        image: options.image,
      };
      note = options.hint;
    } else {
      draft = await withLoader(ctx, `${ctx.model?.id ?? "the chat model"} is writing the prompt`, (signal) =>
        writePrompt(ctx, s, options, undefined, signal),
      );
    }
    if (!draft) return;
    // Flags win over whatever the draft came with, and carry the rest of the request.
    draft = {
      ...draft,
      negative: options.negative ?? draft.negative,
      size: options.size ?? draft.size,
      options: options.extras,
    };
    if (!options.yes) {
      draft = await promptBox(ctx, s, draft, note, options);
      if (!draft) return;
    }

    const request = toBrokerRequest(
      {
        prompt: draft.prompt,
        workflow: draft.workflow,
        negative: draft.negative,
        size: draft.size,
        image: draft.image,
        ...(draft.options ?? {}),
      },
      ctx,
    );
    for (const key of Object.keys(request)) if (request[key] === undefined || request[key] === "") delete request[key];
    const result = await generate(ctx, s, request);
    if (!result) return;
    const generation: Generation = { result, prompt: draft.prompt, negative: draft.negative };
    pi.sendMessage<Generation>({
      customType: MESSAGE_TYPE,
      content: `The user generated an image with /imagine (prompt: ${JSON.stringify(draft.prompt)}).\n${resultText(generation)}`,
      display: true,
      details: generation,
    });
  }

  /** Split on whitespace but keep quoted runs together, so a multi-word --negative survives. */
  function tokenize(args: string): string[] {
    const words: string[] = [];
    const pattern = /"([^"]*)"|'([^']*)'|(\S+)/g;
    let match: RegExpExecArray | null;
    while ((match = pattern.exec(args))) words.push(match[1] ?? match[2] ?? match[3]);
    return words;
  }

  const SHORT_FLAGS: Record<string, string> = { y: "yes", w: "workflow", i: "image", n: "negative", s: "size", r: "reference" };

  type ImagineOptions = {
    yes: boolean;
    workflow?: string;
    image?: string;
    negative?: string;
    size?: string;
    hint: string;
    /** Everything else, keyed by den's own option names, passed to the broker untouched. */
    extras: Record<string, any>;
  };

  /** Every option den offers, as a flag, so one den gains later needs no change here. */
  function imagineFlags(properties: Record<string, any>): string[] {
    // The repeatable ones read better singular, and that is the spelling the parser documents.
    const singular: Record<string, string> = { references: "reference", loras: "lora" };
    const own = Object.keys(properties).filter((name) => name !== "prompt" && name !== "switch_back" && name !== "control");
    const control = ["--control", "--control-type", "--control-strength", "--control-start", "--control-end"];
    return ["--yes", ...own.map((name) => `--${singular[name] ?? name}`), ...("control" in properties ? control : [])].sort();
  }

  /**
   * `/imagine [hint] [--flag value …]`. Flags are den's own, in the same compact form as
   * `den image`: --negative, --reference (repeatable), --lora NAME[:STRENGTH], --upscale
   * NAME[:FACTOR], --control PATH with --control-type, and any scalar option the spec lists.
   * Quoted values keep their spaces. The rest of the words are the hint for the chat model.
   */
  function parseImagineArgs(args: string, ctx: ExtensionContext, s: Spec): ImagineOptions {
    const properties = s.parameters?.properties ?? {};
    const options: ImagineOptions = { yes: false, hint: "", extras: {} };
    const hint: string[] = [];
    const words = tokenize(args);
    const control: Record<string, any> = {};
    const value = (name: string, raw: string | undefined) => {
      if (raw === undefined) throw new Error(`--${name} needs a value`);
      return raw;
    };
    const number = (name: string, raw: string | undefined) => {
      const parsed = Number(value(name, raw));
      if (Number.isNaN(parsed)) throw new Error(`--${name} takes a number, got ${JSON.stringify(raw)}`);
      return parsed;
    };
    // NAME or NAME:AMOUNT, as --lora and --upscale take it.
    const named = (flag: string, raw: string, key: string) => {
      const at = raw.lastIndexOf(":");
      if (at < 0) return { name: raw };
      const amount = Number(raw.slice(at + 1));
      if (Number.isNaN(amount)) throw new Error(`--${flag} takes NAME or NAME:${key.toUpperCase()}, got ${JSON.stringify(raw)}`);
      return { name: raw.slice(0, at), [key]: amount };
    };

    for (let i = 0; i < words.length; i++) {
      const word = words[i];
      if (!word.startsWith("-") || word === "-") {
        hint.push(word);
        continue;
      }
      const long = word.startsWith("--");
      const flag = word.replace(/^--?/, "");
      const name = long ? flag : (SHORT_FLAGS[flag] ?? flag);
      if (name === "yes") {
        options.yes = true;
        continue;
      }
      if (name === "workflow" || name === "negative" || name === "size") {
        options[name] = value(name, words[++i]);
        continue;
      }
      if (name === "image") {
        // A path if the next word looks like one; otherwise the last image of this session.
        const next = words[i + 1];
        if (next && (next === "last" || /[/.~]/.test(next))) {
          i++;
          options.image = next === "last" ? "last" : absolutePath(next, ctx.cwd);
        } else {
          options.image = "last";
        }
        continue;
      }
      if (name === "reference" || name === "references") {
        const raw = value("reference", words[++i]);
        const path = raw === "last" ? lastImage(ctx) : absolutePath(raw, ctx.cwd);
        if (!path) throw new Error("--reference last: no image was generated in this session yet");
        (options.extras.references ??= []).push(path);
        continue;
      }
      if (name === "lora" || name === "loras") {
        (options.extras.loras ??= []).push(named("lora", value(name, words[++i]), "strength"));
        continue;
      }
      if (name === "upscale") {
        options.extras.upscale = named("upscale", value(name, words[++i]), "factor");
        continue;
      }
      if (name === "control") {
        control.image = absolutePath(value(name, words[++i]), ctx.cwd);
        continue;
      }
      if (name === "control-type") {
        control.type = value(name, words[++i]);
        continue;
      }
      if (name === "control-strength" || name === "control-start" || name === "control-end") {
        control[name.slice("control-".length)] = number(name, words[++i]);
        continue;
      }
      const schema = properties[name];
      if (!schema) throw new Error(`unknown option --${name}; /imagine takes: ${imagineFlags(properties).join(" ")}`);
      if (schema.type === "number" || schema.type === "integer") options.extras[name] = number(name, words[++i]);
      else if (schema.type === "array") (options.extras[name] ??= []).push(value(name, words[++i]));
      else options.extras[name] = value(name, words[++i]);
    }

    if (control.image || control.type) {
      if (!control.image) throw new Error("--control-type needs --control with the guide image");
      if (!control.type) throw new Error("--control needs --control-type (den image lists each workflow's types)");
      options.extras.control = control;
    }
    options.hint = hint.join(" ");
    if (options.image === "last") {
      options.image = lastImage(ctx);
      if (!options.image) throw new Error("--image: no image was generated in this session yet; give a path");
    }
    return options;
  }

  /** Ask the chat model for a workflow and prompt, from the conversation, the hint and any current draft. */
  async function writePrompt(
    ctx: ExtensionContext,
    s: Spec,
    options: { workflow?: string; image?: string; hint: string },
    current: Draft | undefined,
    signal: AbortSignal,
  ): Promise<Draft> {
    if (!ctx.model) throw new Error("no chat model selected");
    const system = [
      "You write one request for a local image generator, based on a conversation between a user and an assistant.",
      "Pick the workflow that fits and write the prompt in the style its description asks for.",
      "",
      s.description,
      "",
      'Answer with only a JSON object: {"workflow": "<name>", "prompt": "<prompt>"}, optionally with',
      '"negative" (only for workflows that take one) and "size" ("WIDTHxHEIGHT"). No other text.',
    ].join("\n");
    const task = [
      `<conversation>\n${conversationText(ctx)}\n</conversation>`,
      options.hint ? `The user's request for the image: ${options.hint}` : "Draw what the conversation is about right now.",
    ];
    if (options.workflow) task.push(`Use the workflow ${options.workflow}.`);
    if (options.image) task.push(`This edits the image ${options.image}: write an edit instruction, for a workflow marked [edits].`);
    if (current) task.push(`Rewrite this draft to follow the request: ${JSON.stringify(current)}`);
    const response = await ctx.modelRegistry.complete(
      ctx.model,
      { systemPrompt: system, messages: [{ role: "user", content: [{ type: "text", text: task.join("\n\n") }], timestamp: Date.now() }] },
      { signal },
    );
    if (response.stopReason === "aborted") throw new Error("cancelled");
    if (response.stopReason === "error") throw new Error(`the chat model failed: ${response.errorMessage ?? "unknown error"}`);
    const text = response.content
      .filter((c): c is { type: "text"; text: string } => c.type === "text")
      .map((c) => c.text)
      .join("\n")
      .replace(/<think>[\s\S]*?<\/think>/g, "")
      .trim();
    // An edit needs a workflow that edits, whatever the model picked.
    const allowed = options.image ? s.edits : s.workflows;
    const fallback = options.workflow ?? (current && allowed.includes(current.workflow) ? current.workflow : undefined) ?? (allowed.includes(s.default ?? "") ? s.default! : allowed[0]);
    let parsed: Partial<Draft> = {};
    try {
      parsed = JSON.parse(text.slice(text.indexOf("{"), text.lastIndexOf("}") + 1));
    } catch {
      parsed = { prompt: text }; // not JSON: take the text as the prompt
    }
    const workflow = options.workflow ?? (parsed.workflow && allowed.includes(parsed.workflow) ? parsed.workflow : fallback);
    if (!workflow) throw new Error("no workflow that edits can run (see: den image)");
    const prompt = String(parsed.prompt ?? "").trim();
    if (!prompt) throw new Error("the chat model wrote no prompt");
    return {
      workflow,
      prompt,
      negative: parsed.negative ? String(parsed.negative) : undefined,
      // Edits keep the input image's size.
      size: !options.image && parsed.size && /^\d+x\d+$/.test(String(parsed.size)) ? String(parsed.size) : undefined,
      image: options.image,
    };
  }

  function conversationText(ctx: ExtensionContext): string {
    const sections: string[] = [];
    for (const entry of ctx.sessionManager.getBranch() as any[]) {
      const message = entry.type === "message" ? entry.message : undefined;
      const role = message?.role === "user" ? "User" : message?.role === "assistant" ? "Assistant" : undefined;
      let text = "";
      if (role) {
        const content = message.content;
        text = typeof content === "string" ? content : content.filter((c: any) => c.type === "text").map((c: any) => c.text).join("\n");
      } else if (entry.type === "custom_message" && entry.customType === MESSAGE_TYPE) {
        text = String(entry.content);
      }
      if (text.trim()) sections.push(`${role ?? "Note"}: ${text.trim()}`);
    }
    return sections.join("\n\n").slice(-CONVERSATION_CHARS);
  }

  /** Run work(signal, progress) behind a bordered loader; Esc cancels (returns null). */
  async function withLoader<T>(
    ctx: ExtensionContext,
    title: string,
    work: (signal: AbortSignal, progress: (text: string) => void) => Promise<T>,
  ): Promise<T | null> {
    const controller = new AbortController();
    if (ctx.mode !== "tui") return work(controller.signal, (text) => console.error(text));
    const outcome = await ctx.ui.custom<{ value?: T; error?: Error }>((tui, theme, _kb, done) => {
      const text = new Text(theme.fg("muted", `${title} …`), 1, 0);
      const progress = (line: string) => {
        text.setText(theme.fg("muted", `${line} …`));
        tui.requestRender();
      };
      const box = new Container();
      box.addChild(new DynamicBorder((s: string) => theme.fg("accent", s)));
      box.addChild(text);
      box.addChild(new Text(theme.fg("dim", "Esc cancels"), 1, 0));
      box.addChild(new DynamicBorder((s: string) => theme.fg("accent", s)));
      work(controller.signal, progress)
        .then((value) => done({ value }))
        .catch((error) => done({ error }));
      return {
        render: (width: number) => box.render(width),
        invalidate: () => box.invalidate(),
        handleInput: (data: string) => {
          if (matchesKey(data, "escape")) controller.abort();
        },
      };
    });
    if (controller.signal.aborted) return null;
    if (outcome.error) throw outcome.error;
    return outcome.value as T;
  }

  /** The editable prompt box: Enter generates, Tab picks the workflow, Ctrl+R rewrites, Esc cancels. */
  /** One option as the box shows it: a count for reference paths, the name for a lora or upscale. */
  function optionText(key: string, value: any): string {
    if (Array.isArray(value)) {
      if (key === "references") return `${value.length} reference(s)`;
      return value.map((v: any) => (v && typeof v === "object" ? `${key} ${v.name}${v.strength !== undefined ? ` ${v.strength}` : ""}` : `${key} ${v}`)).join(" · ");
    }
    if (value && typeof value === "object") return `${key} ${value.name ?? value.type ?? ""}`.trim();
    return `${key} ${value}`;
  }

  async function promptBox(
    ctx: ExtensionCommandContext,
    s: Spec,
    draft: Draft,
    note: string,
    options: { workflow?: string; image?: string; hint: string },
  ): Promise<Draft | null> {
    return ctx.ui.custom<Draft | null>((tui, theme, _kb, done) => {
      let current = { ...draft };
      let rewriting: AbortController | undefined;
      let problem = "";
      // One editor serves both texts: Ctrl+N swaps which one it holds, so the box stays one line tall.
      let field: "prompt" | "negative" = "prompt";
      const editor = new Editor(tui, { borderColor: (t: string) => theme.fg("accent", t), selectList: getSelectListTheme() });
      editor.setText(current.prompt);
      const header = new Text("", 1, 0);
      const footer = new Text("", 1, 0);
      const box = new Container();
      box.addChild(new DynamicBorder((t: string) => theme.fg("accent", t)));
      box.addChild(header);
      box.addChild(editor);
      box.addChild(footer);
      box.addChild(new DynamicBorder((t: string) => theme.fg("accent", t)));

      const update = () => {
        const bits = [`${theme.bold("/imagine")}  ${theme.fg("accent", current.workflow)}`];
        if (current.image) bits.push(theme.fg("muted", `edits ${fileLink(current.image)}`));
        if (current.size) bits.push(theme.fg("muted", current.size));
        for (const [key, value] of Object.entries(current.options ?? {})) bits.push(theme.fg("muted", optionText(key, value)));
        if (field === "negative") bits.push(theme.fg("warning", "editing the negative"));
        header.setText(bits.join(theme.fg("dim", " · ")));
        const lines: string[] = [];
        const other = field === "prompt" ? current.negative : current.prompt;
        if (other?.trim()) {
          const shown = other.replace(/\s+/g, " ");
          lines.push(theme.fg("muted", `${field === "prompt" ? "negative" : "prompt"}: ${shown.length > 72 ? `${shown.slice(0, 71)}…` : shown}`));
        }
        if (note) lines.push(theme.fg("warning", `note (not in the prompt): ${note}`));
        if (problem) lines.push(theme.fg("error", problem));
        lines.push(
          theme.fg(
            "dim",
            rewriting
              ? "the chat model is rewriting the prompt (this swaps the GPU) … Esc stops"
              : "Enter generate · Shift+Enter newline · Tab workflow · Ctrl+N negative · Ctrl+R rewrite with the chat model · Esc cancel",
          ),
        );
        footer.setText(lines.join("\n"));
        tui.requestRender();
      };
      editor.onSubmit = (text: string) => {
        if (rewriting) return;
        const next = { ...current };
        if (field === "prompt") next.prompt = text.trim();
        else next.negative = text.trim() || undefined;
        if (!next.prompt.trim()) {
          problem = "the prompt is empty";
          return update();
        }
        done(next);
      };
      update();

      const component = {
        render: (width: number) => box.render(width),
        invalidate: () => box.invalidate(),
        get focused() {
          return editor.focused;
        },
        set focused(value: boolean) {
          editor.focused = value;
        },
        handleInput: (data: string) => {
          if (rewriting) {
            if (matchesKey(data, "escape")) rewriting.abort();
            return;
          }
          if (matchesKey(data, "escape")) return done(null);
          if (matchesKey(data, "ctrl+n")) {
            if (field === "prompt") current.prompt = editor.getText();
            else current.negative = editor.getText().trim() || undefined;
            field = field === "prompt" ? "negative" : "prompt";
            editor.setText((field === "prompt" ? current.prompt : current.negative) ?? "");
            return update();
          }
          if (matchesKey(data, "tab")) {
            const choices = current.image ? s.edits : s.workflows;
            const i = choices.indexOf(current.workflow);
            current.workflow = choices[(i + 1) % choices.length] ?? current.workflow;
            return update();
          }
          if (matchesKey(data, "ctrl+r")) {
            const controller = new AbortController();
            rewriting = controller;
            problem = "";
            update();
            const request = { ...options, hint: note || options.hint };
            const draftNow = field === "prompt" ? { ...current, prompt: editor.getText() } : { ...current, negative: editor.getText().trim() || undefined };
            writePrompt(ctx, s, request, draftNow, controller.signal)
              .then((next) => {
                // The rewrite replaces both texts, so come back to the prompt rather than
                // leaving the editor on a negative the model just changed underneath it.
                current = { ...next, image: current.image, options: current.options };
                field = "prompt";
                editor.setText(current.prompt);
                note = "";
              })
              .catch((e: Error) => {
                if (!controller.signal.aborted) problem = e.message;
              })
              .finally(() => {
                rewriting = undefined;
                update();
              });
            return;
          }
          editor.handleInput(data);
          tui.requestRender();
        },
      };
      return component;
    });
  }

  /** Send the request with a progress box; Esc cancels (returns null). */
  async function generate(ctx: ExtensionContext, s: Spec, request: Record<string, unknown>): Promise<ImageResult | null> {
    return withLoader(ctx, "sending the image request to the broker", (signal, progress) =>
      requestImage(s.broker, request, signal, (msg) => {
        const text = progressText(msg);
        if (text) progress(text);
      }),
    );
  }
}
