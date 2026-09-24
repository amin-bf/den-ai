# den for Android

A small Android client for a den on another machine. It uses that den's broker the way `den --on
<name>` does ([ADR 0007](../../docs/adr/0007-remote-brokers.md)): everything den knows (tasks,
model, workflows, pose library, logs) stays with the broker, and the app keeps none of it. No path
of either machine crosses, only bytes: a file you attach goes as its name and contents, an input
image as its name and bytes, and generated images come back as bytes and are saved to the phone's
gallery (`Pictures/den/`), clips to `Movies/den/`.

Screens: **Connect**, **Status** (mode, model, load, tasks, workflows), **Chat** (see below),
**Ask** (run a delegated task, optionally with a file, and give a verdict), **Image** (prompt,
workflow, the workflow's settings, an optional input image to edit; progress streams while it
runs), **Clip** (on the Image screen's second tab, below), **Poses** (browse the saved pose
library, make a pose, and delete one) and **Voices** (the voice library: each voice with what it
is, to listen to or delete).

Deleting a pose or a voice asks first: it's gone from the den's library for every client. No
tool of a model deletes either; only a person does, here or with `den pose rm` / `den voice --rm`.

## Clips and voice-overs

The Clip tab makes a short video on the den ([ADR 0009](../../docs/adr/0009-clip-generation.md)):
a prompt, a workflow, seconds, a size, keyframes (pictures from the phone at their moments) and
the workflow's LoRAs. A clip is a detached request: it goes on if you leave the app, and is
picked up again when you come back. The clip is saved to `Movies/den/` with its contact sheet
shown, and its summary lists any notes.

- **Sound:** a workflow that makes sound shows a Sound switch (on by default); describe the sounds
  in the prompt. The others say they make silent clips.
- **Voice-over** ([ADR 0010](../../docs/adr/0010-voice-overs.md)), where the den has speech: the
  field takes an SRT script (typed, or **Load SRT**), several lines without times (the den
  speaks them in turn and times them), or one line. Pick a voice and a language; on a workflow
  that can, **Lip-sync** makes a person on screen speak it instead of a narrator over the picture.
- **Record narration:** speak the lines yourself; the den writes down what you said (Whisper) and
  the timed script lands in the field. **Use my recording** makes your recording the voice-over
  itself (lip-synced too, with the switch); off, the chosen voice reads the script.
- **The timed script** of the last clip: **Use the timed script** puts it back in the field,
  **Save SRT** keeps it in `Download/den/`.
- **A new voice:** type its name, then **Record** (about 10 seconds of natural speech, then Stop)
  or **File** (a recording from the phone). It's kept in the den's voice library for everyone.

The microphone is asked for the first time you record, and used only while you do.

## Chat

A conversation with the den's model over `POST /v1/chat/completions`, streamed, so words appear
as they are written. The model and the context window come from `/status`. Qwen-style thinking is
off by default, as it is for den's own delegation; the ⚙ button in the chat turns it on.

The model is offered one tool, `generate_image`, built from the den's own `/client` spec: the
workflows it runs and the settings they take, without anything that names a file. When the model
calls it, the app checks the arguments against that spec (an unknown workflow or setting goes
back to the model as a tool error it can correct), runs `POST /image` itself and shows the
broker's progress in the bubble, because an image makes the GPU swap sides and that takes a
while. The picture appears in the conversation, and the model is told what was made so it can
comment or refine it. For a one-off image without a conversation, use the Image screen.

Conversations are kept on the phone. Each one has its own folder in the gallery,
`Pictures/den/chat/<conversation>/`, named after its first message; deleting a conversation asks
whether those images should go too. Renaming a conversation leaves the folder as it was.

**The model, and its thinking.** A conversation uses the model the den has selected, and the ⚙
lets it ask for another one from what that machine has (`GET /v1/models`). The choice is kept
with the conversation and shown in its header; the picker says what it costs, because the broker
unloads one model and loads the other. Thinking is off by default, as it is for den's own
delegation; with it on, what the model thought arrives as `reasoning_content` and appears above
the answer in a collapsed "Thinking" section.

**Attaching pictures.** The button at the end of the input row is a paperclip while there is
nothing to send, with Gallery and Camera behind it, and becomes a send arrow as soon as there is
text or a picture; a paperclip then appears at the start of the row, so attaching stays within
reach. Several pictures can go with one message, and each thumbnail above the input is removed by
tapping it. A message can carry images from the gallery or the camera. They are
turned upright by their EXIF orientation first, then scaled to a long side of 1024 as JPEG, and the chat says so, because a phone photo
would otherwise fill the model's context; the scaled copy stays with the conversation, so the
history still shows it later. They travel as OpenAI-style `image_url` parts with a `data:` URL,
which llama-server takes when the model has vision and den loaded its projector (`[llm] vision`
there). If it hasn't, the app says that plainly instead of showing the server's error. The model
may also ask to edit an attached picture (`edit_attached` on the image tool), and the app then
sends it to `/image` with a workflow that edits.

**Picture ids.** Every picture in a conversation — attached or generated — gets an id when it
enters it (`img-1`, `img-2`, …), shown next to it in the chat and given to the model before the
picture itself. The model refers to a picture only by its id, so an older one can be edited by
name; an id it doesn't know comes back as an error listing the ones that exist, never a guess.
The tool's `image` argument takes an id, several ids, or `all` for the pictures of the last
message, and all the calls of one answer run together, so the den swaps its GPU once rather than
once per picture.

**Links.** Web addresses and file paths in a message or a tool result are tappable. A link
offers Copy, and Open where this phone can: a URL in the browser, one of den's own pictures in
the gallery. A path on the broker's machine can only be copied, and says so — it means nothing
here (ADR 0007). A picture in the chat opens full screen, with Copy and Share; a long press
copies a message's text.

**Stopping a turn.** While the model answers, the spinner by the input box holds a Stop square.
It closes the request's channel, which the broker takes for a caller who hung up: it stops the
model, or cancels a picture being made. What was written and thought so far stays, marked
"stopped". Pressed while the model is still loading, it takes effect once the answer starts.

**Taking the end back.** Under the last message, **Retry** throws the last answer away (with any
tool calls and results it made) and asks again from the same history; **Edit & resend** takes the
last message sent, and everything after it, back into the input box with its pictures. Both only
work at the end of a conversation and not while an answer is streaming. Images already generated
stay in the gallery; only their bubbles go.

**Skills.** The den's own agent skills (`GET /skills`) can be loaded into a conversation from the
Skills menu: what you pick is fetched, kept with the conversation and put in the system prompt
under a heading naming it, and a chip shows what it costs in tokens. Nothing is loaded until you
pick it, and a skill's reference files are offered separately, under the skill. A den that
doesn't serve skills leaves the menu empty.

## Transports

- **SSH (the real one).** The broker listens only on `127.0.0.1` on its machine and has no
  authentication of its own, so the app reaches it through SSH: it logs in with its own key and
  opens one direct-tcpip channel per request to the broker's port there (`ssh -W`, not `ssh -L`).
  Nothing listens on the phone, so no other app can use the connection. The server's host key is
  trusted on first use: compare the fingerprint the app shows with the server's
  (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`), then trust it. After that a changed host key
  is refused. The session keeps itself alive, and if it dies anyway — the phone slept, Wi-Fi
  went — the next request opens a new one by itself; the pinned key still applies, so a changed
  key is refused rather than reconnected. Connect, and the chat, show connected, reconnecting or
  disconnected.
- **Direct URL (development only).** Plain HTTP to a broker you can reach directly, for the
  emulator: `http://10.0.2.2:11435` is the host machine's loopback. Cleartext is allowed only to
  `10.0.2.2` and `127.0.0.1` (`app/src/main/res/xml/network_security_config.xml`).

## Setting up the key

On the Connect screen, pick **SSH tunnel** and tap **Create key**. The app makes an ed25519 key on
the phone; the private key never leaves it and is stored encrypted with a key held in the
Android Keystore. The app then shows the line to add to `~/.ssh/authorized_keys` on the broker's
machine:

```
restrict,port-forwarding,permitopen="127.0.0.1:11435",command="/usr/bin/false" ssh-ed25519 AAAA… den-android
```

The key can open a channel to the broker's port and nothing else: no shell, no command, no other
port. Use **Copy line** to copy it. If the broker listens on another port, set **Broker port on the
server**; the line follows it. The server must accept public keys (it usually does).

## Building and installing

You need the Android SDK and a JDK that the Android Gradle Plugin supports; Android Studio's
bundled JBR works. The Gradle wrapper downloads Gradle itself.

```sh
cd clients/android
echo "sdk.dir=$HOME/Android/Sdk" > local.properties   # your SDK location; git-ignored
export JAVA_HOME=/opt/android-studio/jbr               # or wherever Android Studio's JBR is
./gradlew assembleDebug testDebugUnitTest
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

With more than one device attached, add `-s <serial>` to `adb` (see `adb devices`).

To try it in the emulator against the den on the same machine, start an emulator, install the
APK, pick **Direct URL** and connect to `http://10.0.2.2:11435`. Status, Poses and the lists
load nothing; Ask and Image load a model on that machine.

## Not supported yet

- Reference images, guide images (`control`) and saved poses as inputs to an image; the Poses
  screen only browses the library.
- LoRAs on images (clips have them), upscaling and saving a pose.
- Editing an image from inside a chat: the tool generates, it doesn't take an input image.
- Switching the mode, releasing the machine, picking the model or toggling tasks: do those on
  the broker's machine.

A den too old to know `/skills` would treat the Skills menu's request as a model request and load
a model for it; the menu only asks when you open it.
