---
name: den-live
description: Hold a live spoken conversation with the user at this machine through den (live_start, talk, live_stop) — listening through the microphone and answering in a voice from the library, interruptible like a person. Use when the user asks to talk by voice, to have a live or spoken conversation, or to "talk to you" out loud.
---

# den-live: a spoken conversation

den is your ears and your voice at the user's PC (ADR live-conversation): it hears them through the microphone
(the transcription model), speaks your words (the speech model, in a voice from the library), and cuts your voice off
when they start talking over you. You stay the one who thinks. While live, den does nothing else:
images, clips and the local model all wait.

## The loop

1. **Start only when asked**: `live_start` with the voice the user wants. None named: your own,
   the voice called `claude` if `list_voices` has one; otherwise ask, or the model's own. It frees the machine first, which can take a minute if
   something is running; don't cancel anyone's work to go faster.
2. **Every exchange is one `talk`**: pass what you say, get back what they said. Your first call
   is a short greeting. Then answer, `talk` again, and so on: the whole conversation is this one
   turn of yours.
3. **End** when they say so ("stop", "that's all", "bye", "end the conversation"), or after two
   silences in a row with a check in between ("are you still there?"): `live_stop`.
4. **Then ask in text** what to do with the transcript `live_stop` returned: keep it under a name
   (`keep_conversation`), delete it (`delete_conversation`), have den's own LLM summarize it
   (`summarize_conversation`, the transcript never leaves the machine), or something else
   (notes, a task list). Every session is kept until they decide, so nothing is lost meanwhile.

## Earlier conversations

`list_conversations` shows every conversation, newest first, kept or not decided yet, with its
summary where there is one; `read_conversation` gives one's transcript. To pick up where you
left off, read the summary (or the transcript) before `live_start`, and say in your greeting
what you remember. Delete one only when the user asks.

## Voice and language

`live_start` takes the voice and the language you speak; `talk` switches either from that turn
on, when the user asks ("talk in grandpa's voice"). A voice switch takes about a second.
Listening needs no language: each utterance's own is detected, so the user may answer your German
in English and switch back on the next turn; you see which from the words. Pass `language` in
`talk` whenever *your* reply's language changes, since the voice needs it to pronounce the words.
Answer in the language the user just used, unless they asked for another.

## How to speak

- **Short and direct**: one to three sentences, like a person in a conversation. It's heard, not
  read: no markdown, lists, code, links or emoji. Spell out numbers and symbols as they're said.
- **Answer at once**: no long deliberation between turns; the user is waiting in silence. A
  longer explanation goes in parts, with room to be interrupted.
- **Interrupted** (`interrupted: true`): they heard `spoken`, not `unspoken`. Answer what they
  said; bring back the unspoken part only if it still matters.
- **They spoke first** (`said_first: true`): nothing of your reply was said. Answer the newer
  thing they said instead.
- **Say something before slow work**: before a tool call that takes more than a few seconds
  (reading notifications, a search), say "one moment, I'm checking" in a short `talk` with a small
  `wait_s`, or the user hears silence and wonders if you're still there.
- **One item at a time**: anything with several items (notifications, results, a list) goes one
  by one: say one, ask what to do with it, wait for the answer, then the next. Don't read out a
  summary of all of them; you're in a conversation, not a screen reader.
- **Links and code go on screen**: write them in the chat, and only say that they're there.
- **Misheard words**: the transcription model can mishear. If what came back doesn't make sense, say what you
  heard and ask, rather than guess.

## Rules

- Never start live mode unasked, and never leave it on: when the user stops talking to you by
  voice, `live_stop`. den also ends it by itself after ten minutes without a `talk`.
- No other den tool works while live; do anything else after `live_stop`.
- The transcript is theirs: don't keep or drop it without asking.
