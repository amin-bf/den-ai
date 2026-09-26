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
const CLIP_TOOL = "generate_clip";
const VOICE_TOOL = "generate_voice";
const TRANSCRIBE_TOOL = "transcribe_audio";
const DESIGN_TOOL = "design_voice";
const LIST_VOICES_TOOL = "list_voices";
const SAVE_VOICE_TOOL = "save_voice";
const CLIP_POLL_MS = 3000;
/** The pose library's tools, listed alongside the image tool; their text never lists the library. */
const POSE_TOOLS = ["list_poses", "save_pose"];
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
  /** Saved pose names, for /imagine's completions. */
  poses?: string[];
  pose_tools?: Record<string, { description: string; parameters: Record<string, any> }>;
  /** The voice tool's spec (ADR voice-overs), where speech is installed. */
  voice?: {
    /** The languages voices can be designed in; empty where the designer isn't installed. */
    design_languages?: string[];
    description: string;
    parameters: { type: "object"; properties: Record<string, any>; required?: string[] };
  };
  /** The clip tool's spec (ADR clip-generation); an older den has none. */
  clip?: {
    clip_on: boolean;
    workflows: string[];
    description: string | null;
    parameters: { type: "object"; properties: Record<string, any>; required: string[] } | null;
  };
};

type PoseResult = {
  name: string;
  description: string;
  width: number;
  height: number;
  aspect: string;
  map: string;
  source: string | null;
  keypoints?: string | null;
  toc: string;
  seconds: number;
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
  /** The maps den drew from photos (a pose skeleton, canny edges), when save_maps asked for them. */
  maps?: string[];
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
  inflight: { id: number; side: string; caller: string; model: string | null; request?: string; left_s?: number | null }[];
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
 * POST /image or /pose and hand each progress line to onLine; resolves with the result line.
 * Uses node:http rather than fetch, whose body timeout would cut long generations off.
 * Aborting closes the connection, and the broker cancels the request.
 */
function requestBroker<T = ImageResult>(
  broker: string,
  request: Record<string, unknown>,
  signal: AbortSignal | undefined,
  onLine: (msg: Record<string, any>) => void,
  path = "/image",
): Promise<T> {
  return new Promise((done, fail) => {
    if (signal?.aborted) return fail(new Error("cancelled"));
    const body = JSON.stringify(request);
    let settled = false;
    const finish = (err: Error | null, result?: T) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener("abort", onAbort);
      err ? fail(err) : done(result!);
    };
    const req = http.request(
      `${broker}${path}`,
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
          finish(new Error(`the broker ended the ${path.slice(1)} request without a result`));
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
  if (msg.starting) return `starting ${msg.starting}`;
  if (msg.speaking) return `speaking line ${msg.speaking.line} of ${msg.speaking.of}`;
  if (msg.transcribing) return `transcribing ${msg.transcribing}`;
  if (msg.designing) return `designing the voice ${msg.designing}`;
  if (msg.drawing) return `drawing the pose for ${msg.drawing.name}`;
  if (msg.stopping) return `stopping ${msg.stopping}`;
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
  lines.push(...(g.result.maps ?? []).map((p) => `map: ${p}`));
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
  const others = s.inflight.filter((r) => r.side !== w.side).map((r) => `${r.caller} ${r.request === "POST /clip" ? `clip${timeLeft(r.left_s)}` : r.side}`);
  const more = s.waiting.length > 1 ? ` (+${s.waiting.length - 1})` : "";
  return others.length
    ? `den: ${w.caller} ${w.side} waits for ${others.join(", ")} to finish${more}`
    : `den: ${w.caller} ${w.side} waits for a swap${more}`;
}

// --- clips (ADR clip-generation) ---

type ClipResult = {
  summary: string[];
  path: string;
  sheet: string | null;
  copies: string[];
  seconds: number;
  waited_s: number;
  voiceover_track?: string;
  notes?: string[];
};

type ClipView = {
  id: number;
  state: "waiting" | "running" | "done" | "failed" | "cancelled";
  progress?: Record<string, any>;
  running_s?: number;
  left_s?: number | null;
  error?: string;
  result?: ClipResult;
};

function timeLeft(seconds: number | null | undefined): string {
  if (seconds === undefined) return "";
  if (seconds === null) return ", time left unknown";
  return seconds < 60 ? ", under a minute left" : `, about ${Math.round(seconds / 60)} min left`;
}

/** One JSON request to the broker's own endpoints that answer at once (/clip, /status). */
function brokerJson<T = any>(broker: string, method: string, path: string, body?: unknown): Promise<T> {
  return new Promise((done, fail) => {
    const data = body === undefined ? undefined : JSON.stringify(body);
    const headers: Record<string, string | number> = { "X-Den-Caller": CALLER };
    if (data) Object.assign(headers, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) });
    const req = http.request(`${broker}${path}`, { method, headers, timeout: 60000 }, (res) => {
      let text = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => (text += chunk));
      res.on("end", () => {
        let parsed: any;
        try {
          parsed = JSON.parse(text);
        } catch {
          return fail(new Error(`den broker: HTTP ${res.statusCode}: ${text.trim()}`));
        }
        if (res.statusCode !== 200) {
          const error = typeof parsed.error === "string" ? parsed.error : (parsed.error?.message ?? text.trim());
          return fail(new Error(error.startsWith("den:") ? error : `den: ${error}`));
        }
        done(parsed);
      });
    });
    req.on("timeout", () => req.destroy(new Error("timed out")));
    req.on("error", (e) => fail(brokerDown(broker, e)));
    req.end(data);
  });
}

function clipText(r: ClipResult): string {
  const lines = [`saved: ${r.path}`, ...r.copies.map((p) => `copied to: ${p}`)];
  if (r.sheet) lines.push(`contact sheet: ${r.sheet}`);
  if (r.voiceover_track) lines.push(`voice-over track: ${r.voiceover_track}`);
  for (const note of r.notes ?? []) lines.push(`note: ${note}`);
  lines.push(`[${[...r.summary, `${r.seconds}s${r.waited_s >= 1 ? `, waited ${Math.round(r.waited_s)}s` : ""}`].join(" · ")}]`);
  return lines.join("\n");
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

/** A saved pose's name after pose:, as den tells it from a path: no dot, no slash. */
const POSE_NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

function isSavedPose(value: string): boolean {
  const match = /^pose:(.+)$/.exec(value.trim());
  return !!match && POSE_NAME.test(match[1]);
}

/** A guide image as den takes it: a path made absolute, or pose:NAME for a saved pose. */
function guidePath(value: string, cwd: string): string {
  return isSavedPose(value) ? value.trim() : absolutePath(value, cwd);
}

/** A reference as den takes it: PATH or TYPE:PATH (pose:photo.png), the path made absolute, or
 *  pose:NAME for a saved pose. */
function referencePath(value: string, cwd: string, last?: () => string | undefined): string {
  if (isSavedPose(value)) return value.trim();
  const match = /^([a-z]+):(.+)$/.exec(value.trim());
  const [kind, raw] = match ? [match[1], match[2]] : [undefined, value];
  const path = raw.trim() === "last" && last ? last() : absolutePath(raw, cwd);
  if (!path) throw new Error("reference last: no image was generated in this session yet");
  return kind ? `${kind}:${path}` : path;
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
  if (Array.isArray(request.references)) request.references = request.references.map((p: string) => referencePath(p, ctx.cwd));
  if (request.control?.image) request.control = { ...request.control, image: guidePath(request.control.image, ctx.cwd) };
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
  for (const path of [...g.result.paths, ...g.result.copies, ...(g.result.maps ?? [])]) {
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
  const registered: Record<string, string> = {}; // per tool, the text last registered, to skip identical updates
  const hiddenByUs = new Set<string>();
  const views = new Map<string, ImageView>(); // for /imagine messages, which have no row state

  // --- the tool follows den's mode and the downloaded models ---

  async function refreshTool(): Promise<Spec | null> {
    try {
      spec = await loadSpec(pi);
    } catch {
      spec = null; // den missing or failing: no tool; /imagine reports the error
    }
    const s = spec;
    const ready = !!(s?.image_on && s.workflows.length && s.description && s.parameters);
    syncTool(TOOL, ready ? JSON.stringify([s!.description, s!.parameters]) : null, () => toolDefinition(s!));
    for (const name of POSE_TOOLS) {
      const def = ready ? s!.pose_tools?.[name] : undefined;
      syncTool(name, def ? JSON.stringify(def) : null, () => poseToolDefinition(name, def!));
    }
    const clip = s?.clip;
    const clipReady = !!(clip?.clip_on && clip.workflows.length && clip.description && clip.parameters);
    syncTool(CLIP_TOOL, clipReady ? JSON.stringify([clip!.description, clip!.parameters]) : null, () => clipToolDefinition(s!));
    const voice = s?.voice;
    syncTool(VOICE_TOOL, voice ? JSON.stringify([voice.description, voice.parameters]) : null, () => voiceToolDefinition(s!));
    syncTool(TRANSCRIBE_TOOL, voice ? "1" : null, () => transcribeToolDefinition(s!));
    // Its text names no voice, so keeping one doesn't change the tools (as list_poses).
    syncTool(LIST_VOICES_TOOL, voice ? "1" : null, () => listVoicesToolDefinition(s!));
    const designs = voice?.design_languages ?? [];
    syncTool(DESIGN_TOOL, designs.length ? JSON.stringify(designs) : null, () => designToolDefinition(s!, designs));
    syncTool(SAVE_VOICE_TOOL, designs.length ? "1" : null, () => saveVoiceToolDefinition(s!));
    return spec;
  }

  /** Register a tool when its text changed (key), and hide it while den can't serve it (null). */
  function syncTool(name: string, key: string | null, define: () => any) {
    const active = pi.getActiveTools();
    if (key === null) {
      if (active.includes(name)) {
        pi.setActiveTools(active.filter((n) => n !== name));
        hiddenByUs.add(name);
      }
      return;
    }
    // Only a real change re-registers: a new tool list makes the model reread the conversation.
    if (registered[name] !== key) {
      pi.registerTool(define());
      registered[name] = key;
    }
    if (hiddenByUs.has(name) && !pi.getActiveTools().includes(name)) {
      pi.setActiveTools([...pi.getActiveTools(), name]);
    }
    hiddenByUs.delete(name);
  }

  function poseToolDefinition(name: string, def: { description: string; parameters: Record<string, any> }) {
    if (name === "list_poses") {
      return {
        name,
        label: "List saved poses",
        description: def.description,
        promptSnippet: "List the saved poses in den's pose library",
        parameters: def.parameters,
        async execute(_id: string, params: Record<string, any>) {
          const name = typeof params?.name === "string" && params.name.trim() ? params.name.trim() : null;
          const run = await pi.exec(DEN, name ? ["pose", "show", name, "--json"] : ["pose", "list"], { timeout: 15000 });
          if (run.code !== 0) throw new Error((run.stderr || run.stdout).trim() || `${DEN} pose failed (exit ${run.code})`);
          if (!name) return { content: [{ type: "text", text: run.stdout.trim() }], details: {} };
          const pose = JSON.parse(run.stdout);
          const text = [
            `${pose.name}: ${pose.description}`,
            `size: ${pose.width}x${pose.height} (${pose.aspect}); saved ${pose.saved ?? "?"}`,
            `skeleton: ${pose.map}`,
            `photo: ${pose.source ?? "(missing)"}`,
            `keypoints: ${pose.keypoints ?? "(not recorded; den pose refresh draws them)"}`,
            "The user sees the skeleton and the photo.",
          ].join("\n");
          return { content: [{ type: "text", text }], details: { pose } };
        },

        renderResult(result: any, options: { isPartial: boolean }, theme: Theme, context: any) {
          const text = result.content?.find((c: any) => c.type === "text")?.text ?? "";
          const pose = result.details?.pose;
          if (options.isPartial || context.isError || !pose) {
            return new Text(theme.fg(context.isError ? "error" : "muted", text), 0, 0);
          }
          // One pose: the skeleton and its stand-in photo, each with a link.
          const box = new Container();
          box.addChild(new Text(theme.fg("muted", `pose:${pose.name} · ${pose.aspect} · ${pose.width}x${pose.height}`), 0, 0));
          const views: Map<string, ImageView> = (context.state.views ??= new Map());
          for (const path of [pose.map, pose.source].filter(Boolean)) {
            if (context.showImages) {
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
        },
      };
    }
    const properties = { ...def.parameters.properties };
    properties.image = {
      ...properties.image,
      description: `${properties.image.description} A path relative to the working directory, or "last" for the last image generated in this session, works too.`,
    };
    return {
      name,
      label: "Save pose",
      description:
        `${def.description}\n` +
        "The GPU holds either you (the chat model) or the image model, so this call unloads you, like " +
        "generate_image: write your text reply first and call save_pose last. Your turn ends when the pose is " +
        "saved, and the user sees the skeleton.",
      promptSnippet: "Save the pose in a photo to den's pose library under a name",
      parameters: { ...def.parameters, properties },

      async execute(_id: string, params: Record<string, any>, signal: AbortSignal | undefined, onUpdate: any, ctx: ExtensionContext) {
        const s = spec;
        if (!s) throw new Error(`den isn't available: ${DEN} image --json failed`);
        const photo = String(params.image ?? "").trim();
        const image = photo === "last" ? lastImage(ctx) : absolutePath(photo, ctx.cwd);
        if (!image) throw new Error('image "last": no image was generated in this session yet');
        const result = await requestBroker<PoseResult>(
          s.broker,
          { ...params, image },
          signal,
          (msg) => {
            const text = progressText(msg);
            if (text) onUpdate?.({ content: [{ type: "text", text }], details: { progress: text } });
          },
          "/pose",
        );
        // For /imagine's completions until the next refresh.
        s.poses = [...new Set([...(s.poses ?? []), result.name])].sort();
        const text = [
          `saved pose ${result.name} (${result.aspect}, ${result.width}x${result.height}), use it as pose:${result.name}`,
          `skeleton: ${result.map}`,
          `photo: ${result.source}`,
          `keypoints: ${result.keypoints ?? "(not recorded)"}`,
          "",
          "Pose library, updated:",
          result.toc,
          "The user sees the skeleton.",
        ].join("\n");
        // Like generate_image, the model wrote its reply first: end the turn instead of reloading it now.
        return { content: [{ type: "text", text }], details: { pose: result }, terminate: true };
      },

      renderCall(args: Record<string, any>, theme: Theme) {
        return new Text(
          `${theme.fg("toolTitle", theme.bold("save_pose "))}${theme.fg("muted", String(args.name ?? ""))}${theme.fg("dim", `: ${String(args.description ?? "")}`)}`,
          0,
          0,
        );
      },

      renderResult(result: any, options: { isPartial: boolean }, theme: Theme, context: any) {
        const text = result.content?.find((c: any) => c.type === "text")?.text ?? "";
        if (options.isPartial) return new Text(theme.fg("muted", text || "sending to the broker"), 0, 0);
        const pose = result.details?.pose as PoseResult | undefined;
        if (context.isError || !pose) return new Text(theme.fg("error", text), 0, 0);
        const box = new Container();
        box.addChild(new Text(theme.fg("muted", `pose:${pose.name} · ${pose.aspect} · ${pose.width}x${pose.height} · ${pose.seconds}s`), 0, 0));
        if (context.showImages) {
          const views: Map<string, ImageView> = (context.state.views ??= new Map());
          let view = views.get(pose.map);
          if (!view) {
            view = new ImageView(pose.map);
            views.set(pose.map, view);
          }
          box.addChild(view);
        }
        box.addChild(new Text(theme.fg("accent", fileLink(pose.map)), 0, 0));
        return box;
      },
    };
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
        const result = await requestBroker(broker, request, signal, (msg) => {
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

  function voiceToolDefinition(s: Spec) {
    const voice = s.voice!;
    return {
      name: VOICE_TOOL,
      label: "Generate voice",
      description:
        `${voice.description}\n` +
        "Like generate_image this unloads you: write your reply first and call generate_voice last.",
      promptSnippet: "Speak a text or an SRT script as a voice-over track (local speech model)",
      parameters: voice.parameters,

      async execute(_id: string, params: Record<string, any>, signal: AbortSignal | undefined, onUpdate: any, ctx: ExtensionContext) {
        const broker = (spec ?? s).broker;
        const request: Record<string, any> = { ...params };
        // A script or a recording given as a path is a file here: it goes as its content, so a
        // den on another machine gets it too.
        if (typeof params.srt === "string" && !params.srt.includes("\n")) {
          request.srt = readFileSync(absolutePath(params.srt, ctx.cwd), "utf8");
        }
        if (typeof params.voice === "string" && /[/.]/.test(params.voice)) {
          const path = absolutePath(params.voice, ctx.cwd);
          request.voice = { name: path.split("/").pop(), base64: readFileSync(path).toString("base64") };
        }
        if (typeof params.out === "string") request.out = absolutePath(params.out, ctx.cwd);
        const r = await requestBroker<Record<string, any>>(broker, request, signal, (msg) => {
          const text = progressText(msg);
          if (text) onUpdate?.({ content: [{ type: "text", text }], details: { progress: text } });
        }, "/voice");
        const lines = [`saved: ${r.path}`, ...r.copies.map((p: string) => `copied to: ${p}`)];
        lines.push(`script at the spoken times: ${String(r.path).replace(/\.wav$/, ".srt")}`);
        lines.push(...r.notes.map((n: string) => `note: ${n}`));
        lines.push(`[${[...r.summary, `${r.seconds}s`].join(" · ")}]`);
        return { content: [{ type: "text", text: `${lines.join("\n")}\nThe user can listen to it; you can't.` }], details: r, terminate: true };
      },
    };
  }

  function listVoicesToolDefinition(s: Spec) {
    return {
      name: LIST_VOICES_TOOL,
      label: "List voices",
      description:
        "The voice library: each voice's name, what it sounds like and how it was made (designed from a " +
        "description, or recorded). Call it once per conversation before choosing a voice for generate_voice or a " +
        "clip's voiceover, and keep the list; with a name, one voice's details.",
      promptSnippet: "List the den's voices with what each sounds like",
      parameters: { type: "object", properties: { name: { type: "string", description: "One voice, for its details." } } },

      async execute(_id: string, params: Record<string, any>) {
        const broker = (spec ?? s).broker;
        if (params.name) {
          const v = await brokerJson(broker, "GET", `/voices?name=${encodeURIComponent(String(params.name))}`);
          const keys = ["name", "description", "source", "language", "seed", "created", "seconds"];
          return { content: [{ type: "text", text: keys.filter((k) => v[k] != null).map((k) => `${k}: ${v[k]}`).join("\n") }], details: v };
        }
        const list = await brokerJson(broker, "GET", "/voices");
        const rows: any[] = list.toc ?? [];
        const text = rows.length
          ? "Voices:\n" + rows.map((r) => `- ${r.name}${r.source ? ` [${r.source}]` : ""}: ${r.description ?? "(no description)"}`).join("\n")
          : "No voices yet: design_voice makes one from a description.";
        return { content: [{ type: "text", text }], details: list };
      },
    };
  }

  function designToolDefinition(s: Spec, languages: string[]) {
    return {
      name: DESIGN_TOOL,
      label: "Design voice",
      description:
        "Design a new voice from a description: a sample in that voice, which Chatterbox then clones for generate_voice " +
        "and clip voice-overs (also lip-synced). Describe age, gender, pitch, texture, pace and mood, e.g. " +
        '"an old man with a deep, raspy, slow voice", "a cheerful eight-year-old girl with a high, bright voice". ' +
        "Without a name it's a draft, not in the library yet: the user listens to its sample, or you try it on a real " +
        "line with generate_voice and voice draft:ID; keep it with save_voice once they like it, or design again with " +
        "another seed. About a minute. The den-voice skill has the stages.",
      promptSnippet: "Design a new voice from a description (local Qwen3-TTS)",
      parameters: {
        type: "object",
        properties: {
          description: { type: "string", description: "What the voice sounds like." },
          language: { type: "string", enum: languages, description: "The sample's language (default en)." },
          seed: { type: "integer", description: "Another seed gives another voice for the same description." },
          name: { type: "string", description: "Keep it at once under this name, no draft: only when the user asked for that." },
          replace: { type: "boolean", description: "With name: replace a voice of that name." },
        },
        required: ["description"],
      },

      async execute(_id: string, params: Record<string, any>, signal: AbortSignal | undefined, onUpdate: any) {
        const broker = (spec ?? s).broker;
        const r = await requestBroker<Record<string, any>>(broker, { ...params }, signal, (msg) => {
          const text = progressText(msg);
          if (text) onUpdate?.({ content: [{ type: "text", text }], details: { progress: text } });
        }, "/voices/design");
        const text = r.voice
          ? `voice ${r.voice} kept (${r.duration}s sample). Voices: ${r.voices.join(", ")}.`
          : `draft ${r.draft}: a ${r.duration}s sample at ${r.path}, not in the library yet. The user can listen to it; ` +
            `try it on a line with voice draft:${r.draft}, and keep it with save_voice once they like it.`;
        return { content: [{ type: "text", text }], details: r };
      },
    };
  }

  function saveVoiceToolDefinition(s: Spec) {
    return {
      name: SAVE_VOICE_TOOL,
      label: "Save voice",
      description:
        "Keep a designed draft voice (design_voice) in the voice library under a name, with its description, language " +
        "and seed, once the user has heard it and likes it. From then on list_voices shows it.",
      promptSnippet: "Keep a designed draft voice in den's voice library",
      parameters: {
        type: "object",
        properties: {
          draft: { type: "string", description: "The draft's id, from design_voice." },
          name: { type: "string", description: "The voice's name: lower case letters, digits and dashes." },
          replace: { type: "boolean", description: "Replace a voice of that name." },
        },
        required: ["draft", "name"],
      },

      async execute(_id: string, params: Record<string, any>) {
        const broker = (spec ?? s).broker;
        const r = await brokerJson(broker, "POST", "/voices", { draft: params.draft, name: params.name, replace: !!params.replace });
        return { content: [{ type: "text", text: `voice ${r.voice} kept. Voices: ${r.voices.join(", ")}.` }], details: r };
      },
    };
  }

  function transcribeToolDefinition(s: Spec) {
    return {
      name: TRANSCRIBE_TOOL,
      label: "Transcribe audio",
      description:
        "Write down what a recording says, as an SRT with each line at the time it was said (Whisper, many " +
        "languages): subtitles, a script to speak again in another voice (generate_voice) or to put on a clip, or " +
        "to check what a finished clip says (it takes a clip's mp4 too, since you can't hear it). " +
        "The den-voice skill has the recipes.",
      promptSnippet: "Transcribe a recording into a timed SRT (local Whisper)",
      parameters: {
        type: "object",
        properties: {
          audio: { type: "string", description: "Path of the recording (wav, mp3, m4a, …) or a clip (mp4)." },
          language: { type: "string", description: "Its language as a code, e.g. en, de; default: detected." },
        },
        required: ["audio"],
      },

      async execute(_id: string, params: Record<string, any>, signal: AbortSignal | undefined, onUpdate: any, ctx: ExtensionContext) {
        const broker = (spec ?? s).broker;
        const path = absolutePath(String(params.audio), ctx.cwd);
        // Sent as bytes, so a den on another machine can hear it too.
        const request: Record<string, any> = { audio: { name: path.split("/").pop(), base64: readFileSync(path).toString("base64") } };
        if (params.language) request.language = params.language;
        const r = await requestBroker<Record<string, any>>(broker, request, signal, (msg) => {
          const text = progressText(msg);
          if (text) onUpdate?.({ content: [{ type: "text", text }], details: { progress: text } });
        }, "/transcribe");
        return { content: [{ type: "text", text: `${r.srt}\n[${r.segments.length} line(s) · ${r.duration}s of audio] saved: ${r.path}` }], details: r };
      },
    };
  }

  function clipToolDefinition(s: Spec) {
    const clip = s.clip!;
    const properties = { ...clip.parameters!.properties };
    properties.keyframes = {
      ...properties.keyframes,
      description: `${properties.keyframes.description} An image path may be relative to the working directory, or "last" for the last image generated in this session.`,
    };
    return {
      name: CLIP_TOOL,
      label: "Generate clip",
      description:
        `${clip.description}\n` +
        "A clip takes minutes (about a minute per second of it). The user sees its contact sheet, four frames " +
        "first to last, and gets the video's path; you get the path and settings back. For a scene that has to " +
        "look a certain way, make the first frame with generate_image first, let the user approve it, and pass " +
        "it as the first keyframe (\"last\").\n" +
        "Like generate_image this unloads you: write your reply first and call generate_clip last. Your turn " +
        "ends when the clip is done.",
      promptSnippet: "Generate a short video clip with a local video model (ComfyUI)",
      parameters: { ...clip.parameters!, properties },

      async execute(_id: string, params: Record<string, any>, signal: AbortSignal | undefined, onUpdate: any, ctx: ExtensionContext) {
        const broker = (spec ?? s).broker;
        // An older broker would take /clip for an LLM request and load a model for it.
        if (!("clips" in (await getStatus(broker)))) {
          throw new Error("den: this broker predates clips; restart it: systemctl --user restart den");
        }
        const request: Record<string, any> = { ...params };
        if (Array.isArray(params.keyframes)) {
          request.keyframes = params.keyframes.map((k: Record<string, any>) => {
            const image = String(k.image ?? "").trim();
            const path = image === "last" ? lastImage(ctx) : absolutePath(image, ctx.cwd);
            if (!path) throw new Error('keyframe "last": no image was generated in this session yet');
            return { ...k, image: path };
          });
        }
        if (typeof params.out === "string") request.out = absolutePath(params.out, ctx.cwd);
        const started = await brokerJson<{ id: number; estimate_s: number | null; summary: string[] }>(broker, "POST", "/clip", request);
        const say = (text: string) => onUpdate?.({ content: [{ type: "text", text }], details: { progress: text } });
        say(`clip ${started.id}: ${started.summary.join(" · ")}${timeLeft(started.estimate_s)}`);
        // A clip is a detached request: Esc here cancels it, since nobody else is waiting for it.
        while (true) {
          if (signal?.aborted) {
            await brokerJson(broker, "POST", "/clip/cancel", { id: started.id }).catch(() => undefined);
            throw new Error("cancelled");
          }
          const view = await brokerJson<ClipView>(broker, "GET", `/clip?id=${started.id}`);
          if (view.state === "done" && view.result) {
            return {
              content: [{ type: "text", text: `${clipText(view.result)}\nThe user sees the contact sheet.` }],
              details: { clip: view.result },
              terminate: true,
            };
          }
          if (view.state === "failed" || view.state === "cancelled") throw new Error(`den: clip ${view.state}: ${view.error ?? ""}`);
          say(
            view.state === "running"
              ? `making the clip: ${view.running_s}s in${timeLeft(view.left_s)}`
              : (view.progress && progressText(view.progress)) || "waiting for the image side",
          );
          await new Promise((r) => setTimeout(r, CLIP_POLL_MS));
        }
      },

      renderCall(args: Record<string, any>, theme: Theme) {
        const prompt = String(args.prompt ?? "").replace(/\s+/g, " ");
        const keys = Array.isArray(args.keyframes) && args.keyframes.length ? `, ${args.keyframes.length} keyframe(s)` : "";
        return new Text(
          `${theme.fg("toolTitle", theme.bold(`${CLIP_TOOL} `))}${theme.fg("muted", `${args.workflow ?? "default"}${keys}: `)}${theme.fg("dim", prompt)}`,
          0,
          0,
        );
      },

      renderResult(result: any, options: { isPartial: boolean }, theme: Theme, context: any) {
        const text = result.content?.find((c: any) => c.type === "text")?.text ?? "";
        if (options.isPartial) return new Text(theme.fg("muted", text || "sending to the broker"), 0, 0);
        const r = result.details?.clip as ClipResult | undefined;
        if (context.isError || !r) return new Text(theme.fg("error", text), 0, 0);
        const box = new Container();
        box.addChild(new Text(theme.fg("muted", [...r.summary, `${r.seconds}s`].join(" · ")), 0, 0));
        if (r.sheet && context.showImages) {
          const views: Map<string, ImageView> = (context.state.views ??= new Map());
          let view = views.get(r.sheet);
          if (!view) {
            view = new ImageView(r.sheet);
            views.set(r.sheet, view);
          }
          box.addChild(view);
        }
        for (const path of [r.path, ...r.copies]) box.addChild(new Text(theme.fg("accent", fileLink(path)), 0, 0));
        return box;
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
      "[--image [PATH]] [--negative TEXT] [--reference [pose:]PATH|pose:NAME] [--seed N] [--size WxH] [--lora NAME[:STRENGTH]] " +
      "[--strength 0-1] [--control PATH --control-type TYPE] [--upscale NAME[:FACTOR]] [--save-maps] — quote values with spaces",
    getArgumentCompletions(prefix: string) {
      const words = prefix.split(/\s+/);
      const current = words.at(-1) ?? "";
      if (words.at(-2) === "--workflow" || words.at(-2) === "-w") {
        const items = (spec?.workflows ?? []).filter((w) => w.startsWith(current));
        return items.length ? items.map((w) => ({ value: [...words.slice(0, -1), w].join(" "), label: w })) : null;
      }
      if (["--reference", "-r", "--control"].includes(words.at(-2) ?? "") && current.startsWith("pose:")) {
        const items = (spec?.poses ?? []).map((name) => `pose:${name}`).filter((p) => p.startsWith(current));
        return items.length ? items.map((p) => ({ value: [...words.slice(0, -1), p].join(" "), label: p })) : null;
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
    const flag = (name: string) => `--${singular[name] ?? name.replace(/_/g, "-")}`;
    return ["--yes", ...own.map(flag), ...("control" in properties ? control : [])].sort();
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
        (options.extras.references ??= []).push(referencePath(raw, ctx.cwd, () => lastImage(ctx)));
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
        control.image = guidePath(value(name, words[++i]), ctx.cwd);
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
      // den's names use underscores (save_maps); the flags read better with hyphens (--save-maps).
      const key = name in properties ? name : name.replace(/-/g, "_");
      const schema = properties[key];
      if (!schema) throw new Error(`unknown option --${name}; /imagine takes: ${imagineFlags(properties).join(" ")}`);
      if (schema.type === "boolean") options.extras[key] = true;
      else if (schema.type === "number" || schema.type === "integer") options.extras[key] = number(name, words[++i]);
      else if (schema.type === "array") (options.extras[key] ??= []).push(value(name, words[++i]));
      else options.extras[key] = value(name, words[++i]);
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
      requestBroker(s.broker, request, signal, (msg) => {
        const text = progressText(msg);
        if (text) progress(text);
      }),
    );
  }
}
