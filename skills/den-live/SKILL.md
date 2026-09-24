---
name: den-live
description: Hold a live spoken conversation with the user at this machine through den (live_start, talk, live_stop) — listening through the microphone and answering in a voice from the library, interruptible like a person. Use when the user asks to talk by voice, to have a live or spoken conversation, or to "talk to you" out loud.
---

# den-live: a spoken conversation

den is your ears and your voice at the user's PC (ADR 0011): it hears them through the microphone
(Whisper), speaks your words (Chatterbox, in a voice from the library), and cuts your voice off
when they start talking over you. You stay the one who thinks. While live, den does nothing else:
images, clips and the local model all wait.

## The loop

1. **Start only when asked**: `live_start` with the voice the user wants (`list_voices`; none
   named: ask, or the model's own). It frees the machine first, which can take a minute if
   something is running; don't cancel anyone's work to go faster.
2. **Every exchange is one `talk`**: pass what you say, get back what they said. Your first call
   is a short greeting. Then answer, `talk` again, and so on: the whole conversation is this one
   turn of yours.
3. **End** when they say so ("stop", "that's all", "bye", "end the conversation"), or after two
   silences in a row with a check in between ("are you still there?"): `live_stop`.
4. **Then ask in text** what to do with the transcript `live_stop` returned: keep it under a name
   (`keep_conversation`), drop it (`drop_conversation`), or something else (a summary, notes, a
   task list). Memory is always on until they decide.

## How to speak

- **Short and direct**: one to three sentences, like a person in a conversation. It's heard, not
  read: no markdown, lists, code, links or emoji. Spell out numbers and symbols as they're said.
- **Answer at once**: no long deliberation between turns; the user is waiting in silence. A
  longer explanation goes in parts, with room to be interrupted.
- **Interrupted** (`interrupted: true`): they heard `spoken`, not `unspoken`. Answer what they
  said; bring back the unspoken part only if it still matters.
- **They spoke first** (`said_first: true`): nothing of your reply was said. Answer the newer
  thing they said instead.
- **Misheard words**: Whisper can mishear. If what came back doesn't make sense, say what you
  heard and ask, rather than guess.

## Rules

- Never start live mode unasked, and never leave it on: when the user stops talking to you by
  voice, `live_stop`. den also ends it by itself after ten minutes without a `talk`.
- No other den tool works while live; do anything else after `live_stop`.
- The transcript is theirs: don't keep or drop it without asking.
