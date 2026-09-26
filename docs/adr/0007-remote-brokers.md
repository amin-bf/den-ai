---
status: accepted (CLI built and used; an Android client built and tested in the emulator; the MCP server's remote mode built, not registered; pi's image extension still local only)
---

# Using the den on another machine, through an SSH tunnel

den runs on more than one machine now (ADR running-on-macos), and a machine should be able to use the den on
another one — its LLM and its images — as if that were the only den: a **remote**. The broker
listens only on `127.0.0.1` and has no authentication: anything that can reach its port can
load models, cancel jobs and turn den off. It must not be opened to the network as it is.

## Decisions

- **The broker stays on `127.0.0.1`; an SSH tunnel carries the traffic.** The client runs
  `ssh -N -L 127.0.0.1:<port>:127.0.0.1:11435 <host>` (a user unit keeps it up) and talks to its
  local end. SSH already authenticates, encrypts and is set up on both systems, so den carries
  no auth code, certificates or tokens of its own, and nothing but SSH is reachable on the
  network. For now the tunnel runs on the home network only.
- **The tunnel's key can do nothing else.** On the broker's machine it is listed in
  `authorized_keys` as
  `restrict,port-forwarding,permitopen="127.0.0.1:11435",command="/usr/bin/false" <key>`: no
  shell, no command, no forwarding to any other port. It has no passphrase, because the tunnel
  starts unattended; the restriction is what makes that acceptable. A person's own key, for a
  shell, is a separate one. The broker's machine accepts keys only (`PasswordAuthentication no`,
  and `KbdInteractiveAuthentication no`, since macOS asks for passwords through it too).
- **A remote is used as if den existed only there.** Everything den keeps stays with the
  broker: config, state, the tasks and their prompts, the model, the workflows and their model
  files, the pose library, the image log and the delegation log. The client reads only where
  the broker is: `[remotes.<name>] base_url`, in the git-ignored `config.local.toml`, picked with
  `DEN_BROKER=<name>` or `den --on <name>`. `[broker] base_url` stays where this machine's own
  den listens, and it keeps working unchanged beside the remote.
- **No path crosses, in either direction; only bytes.** A path names a file on one machine and
  means nothing on the other, and would hand over home folders and user names. So the files a
  delegation reads are read on the client and sent under their name with their contents (they
  still never enter Claude's context); an input image — an edit's source, a reference, a guide,
  a pose photo — is a file on the client, sent as its name and bytes; and a request that asks
  with `"bytes"` gets its images (and any maps den drew) back as bytes, which the client saves
  as a local den would. For the user, a remote works on the client's own files. A saved pose is
  named `pose:NAME`, from the remote's library. The broker writes inputs sent as bytes into a
  private temporary folder for the request's length and removes them after; `save_pose` keeps
  the photo in the library, as it always does.
- **The request says what it is, not a header.** Input images may be paths (a string, as a
  local caller sends them) or file objects (`{"name", "base64"}`), and `"bytes"` asks for bytes
  back, as `preview` asks for a small copy. A local caller that sends paths gets paths, exactly
  as before. What only a remote client asks for — `/client`, `/poses` — names no file.
- **The broker serves what such a client needs over HTTP:** `GET /client` (mode, model,
  enabled tasks, the image spec with its tool texts and a listing without folders),
  `POST /delegate` and `POST /feedback` (the task runs and is logged there) and `GET /poses`
  (the library's table of contents, or one pose with small copies of its images, without its
  files). `/status` also carries `llm_model` and `num_ctx`.
- **A remote serves its skills too, and a client loads one when asked.** `GET /skills` and
  `GET /skill` hand over den's own skills (`skills/<name>/SKILL.md` and its references), so a
  chat elsewhere can be given the strategy that belongs with the tools it just fetched. Never
  by default: a skill costs context on every turn, and its references cost more, so the user
  picks them per conversation and sees what they add.
- **Claude can get a remote as a second MCP server, `den-<name>`, but nothing registers it.**
  `bin/den-mcp` with `DEN_BROKER` set serves the remote's tools beside this machine's
  (`mcp__den-<name>__local_llm`), their texts saying where things run, and its
  `release_resources` frees the remote machine. It isn't needed yet, and a second
  `release_resources` would meet the "release before heavy work" rule, which is about this
  machine; so it is registered by hand when wanted
  (`claude mcp add --scope user den-<name> -e DEN_BROKER=<name> -- <repo>/bin/den-mcp`).
- **The CLI follows the same line.** `den --on <name>` runs `status`, `mode`, `unload`, `ask`,
  `task` (listing), `image` and `pose list | show | save` against the remote. What changes den's
  files there — picking the model, toggling a task, renaming or deleting a pose, `den log`,
  `den serve` — is refused with a pointer to run it on that machine.
- **A phone uses the same API, with its own key.** The Android client (`clients/android/`)
  makes an ed25519 key on the device, kept encrypted under the Android Keystore, and shows the
  restricted `authorized_keys` line to add for it, so a lost phone is revoked by deleting one
  line. It opens one SSH channel per request (direct-tcpip to `127.0.0.1:11435`) instead of a
  forwarded local port, so no other app on the phone can reach the broker through it. It pins
  the host key on first use and refuses a changed one.
- **An older broker is asked `/status` first.** It would treat an unknown path such as
  `/client` as an LLM request and load a model for it; its `/status` lacks `llm_model`, and
  the client says to update den there instead.

## Considered

- **Bind the broker to the LAN and add a bearer token and TLS.** Doable with the stdlib, but den
  would own certificates, token storage and the auth code, and a mistake there opens the broker
  to the network. SSH does all of it already.
- **A VPN (WireGuard, Tailscale) alone.** It encrypts and limits who can reach the machine, but
  the broker would listen on the VPN's address with no authentication of its own. Useful later
  as the path the tunnel takes away from home; not a replacement for it.
- **Running den's MCP server on the other machine over SSH.** It would be that den with no new
  API at all, but only Claude could use it: pi and a future phone client talk to the broker's
  HTTP API, so that API is what a remote has to offer.
- **Keeping the tasks and the delegation log on the client.** It made "which den am I using"
  a mix of both machines; with a remote, all of den is the remote's.
- **Paths on the broker's machine, or ids for them.** Results named by their path there (or an
  id under its image folder, or `~/…`) could be passed back as the next input without moving
  bytes, but they tie the client to the other machine's filesystem, hand over its layout, and a
  client still needs the bytes to show the image. Sending bytes both ways keeps each machine's
  paths its own.
- **Telling a remote caller apart by a header.** Every remote request arrives on `127.0.0.1`
  through the tunnel, so a header would be the only sign; the request's own shape (file objects,
  `"bytes"`) says the same without a second channel, like `preview` already does.

## Consequences

- The broker's machine must run a den new enough to serve `/client`; the client says so
  otherwise.
- The tunnel is set up by hand (key, `authorized_keys` line, user unit), since the host and the
  key are the user's; `setup.sh` does nothing for a remote.
- Error messages from the broker's machine may still name a file there, such as its ComfyUI log:
  they are what the person reading them needs to look at, on that machine.
- pi reaches a remote's LLM as another provider whose `baseUrl` is the tunnel's local end. Its
  image extension still reads `den image --json` and result paths on its own machine, so images
  through a remote need that extension to send and take bytes too.
