---
status: proposed (not built; written down for later)
---

# A den CLI that installs on its own, so pi on a phone can draw images through a remote

pi runs on Android in Termux, and its LLM already works against a remote den (ADR remote-brokers): the
phone keeps a restricted tunnel key, opens `ssh -N -L 127.0.0.1:11435:127.0.0.1:11435 <host>`
before pi starts, and pi's `models.json` points at that local end as it does on the broker's
machine. Images don't work there. pi's extension (`integrations/pi/den.ts`) builds its
`generate_image` tool from `den image --json`, and Termux has no `den`, so the extension leaves
the tool out and `/imagine` reports the error. Even with a spec, the extension posts to the
broker itself, sends input images as paths and reads the results from the paths that come
back, which name files on the broker's machine.

The CLI already does the remote half: `den --on <name> image --json` gives the remote's spec
from `GET /client`, and `den --on <name> image "<prompt>"` sends input files as bytes, asks
for `"bytes"` back and saves the images here (`den/remote.py`). It is stdlib-only Python 3.11,
which Termux has. What's missing is a way to have that CLI without the repo, and an extension
that lets it do the work.

## Decisions

- **One installable `den` command, the same code.** Package the existing `den/` as a CLI that
  installs without a checkout (a `pyproject.toml` with a console script, installed with
  `pip install git+…`, or a single-file zipapp, `den.pyz`). No second client in another
  language: the remote logic stays in `den/remote.py`.
- **A client-only install needs no repo files.** Its config is one file, e.g.
  `~/.config/den/config.local.toml` with `[remotes.<name>] base_url =
  "http://127.0.0.1:11435"`, and it picks that remote by default (`DEN_BROKER=<name>`).
  `config.toml`, `state.json`, workflows and poses stay with the broker. `CONFIG_PATH` and
  friends in `core.py` point at the repo today (`ROOT`); they need a user-config fallback.
- **Nothing that belongs to the broker's machine runs on the client.** Importing the CLI
  must not touch services, GPUs or `platform.py`'s system checks; what a client can't do is
  refused as `den --on` refuses it now. Check this on Termux, not only on Linux and macOS.
- **The CLI uses the same tunnel as pi.** It talks to the `base_url` it is given; on the phone
  that is the tunnel's local end, so no second connection or key.
- **pi's extension hands image work to the CLI when den is a remote.** It already reads its
  spec from `den image --json`; with a remote, generation (and pose saving) runs as
  `den image …` instead of its own `requestBroker`, so input files go as bytes and results
  are saved on the phone. For the extension's progress display the CLI gets a machine-readable
  mode, e.g. `den image --jsonl`, printing the broker's progress lines and the result (with
  local paths) as JSON lines. Against a local den the extension keeps its direct path.
- **Results fit the phone.** Termux can't draw kitty graphics, so the extension shows the
  path (openable with `termux-open`) instead of an inline image. Saving into shared storage
  (`~/storage/pictures`, after `termux-setup-storage`) puts images in the gallery.

## Considered

- **Teaching the extension bytes itself.** It would need file objects for every input image
  (source, references, control, poses) and its own saving, the same work `den/remote.py`
  already does, now in TypeScript too, and kept in step twice.
- **A new standalone client (Go, Node).** One binary is easy to install, but it is a third
  implementation of the remote API beside the Python CLI and the Android app.
- **The Android app (`clients/android/`) instead.** It already generates images from the phone,
  but outside pi: this is about pi's own `generate_image` tool and `/imagine`.

## Consequences

- The phone needs Python in Termux (`pkg install python`) plus the installed CLI; nothing
  from the repo.
- `den image` gains a JSON-lines output that other callers can use too.
- The extension has two paths to keep working: direct to a local broker, and through the CLI
  to a remote.
- ADR remote-brokers's "pi's image extension still local only" is closed by this once built.
