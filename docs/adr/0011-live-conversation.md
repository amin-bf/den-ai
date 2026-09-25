---
status: proposed
---

# Live conversation: den as Claude's ears and voice

Claude should be able to hold a spoken conversation with the user at the PC: hear them through
the microphone, answer in a voice of its own through the speakers or headphones, and be
interrupted mid-sentence like a person. The thinking stays with Claude; den provides only the
ears (voice activity detection and Whisper) and the voice (Chatterbox with a voice from the
library). No chat model of den's, no pi, no phone.

## Decisions

- **Live mode is exclusive.** `live_start` makes the broker release both sides, as `den unload`
  does, and load the live engine; until `live_stop` every other request, from any client, is
  refused with "den is in a live conversation". The conversation never competes for the GPU, the
  CPU or RAM. A watchdog ends live mode when Claude hasn't called for ten minutes (a closed
  terminal, a crashed session), so the machine can't stay locked.
- **One tool call per exchange.** `talk(say)` speaks Claude's reply and returns what the user
  says next, as text. Claude Code works in turns, so a conversation is one long turn of Claude's:
  talk, think, talk. The reply is spoken sentence by sentence as it's synthesized, so the voice
  starts after the first sentence.
- **Interrupting.** The microphone is listened to all the time, also while den speaks. When the
  user starts speaking over the voice, playback stops at once, the rest of the reply is dropped,
  and `talk` returns what they said and how far the reply had got, so Claude knows what was heard.
- **Echo cancellation always.** The live engine loads PipeWire's echo-cancel module (WebRTC) for
  the session and records and plays through it, so den's own voice from speakers is never taken
  for the user interrupting; with headphones it costs nothing. The module is unloaded after.
- **The live engine is one process in the speech venv** (`speech/live.py`) that owns the
  microphone, the speakers and the models: Silero VAD, Whisper large-v3-turbo and Chatterbox, all
  on the GPU, which live mode has to itself. The broker starts and stops it, as it does the speech
  server, and talks to it over a private UNIX socket.
- **Memory is always on; what to keep is decided at the end.** Every turn is written to the
  session's transcript as it happens, so a crash loses nothing. `live_stop` returns the transcript,
  and the user decides: keep it under a name (`~/.local/share/den/conversations/`), delete it,
  have den's own LLM summarize it (the summarize task, so the transcript stays on the machine;
  the summary is kept beside it), or have Claude turn it into something else. Every session is
  kept until then, with no limit: it's text. `list_conversations` and `read_conversation` let a
  later session pick up an earlier conversation.
- **Listening detects every utterance's language; speaking is told.** Whisper writes each utterance
  down in its own language, so the user may answer German in English and switch back; Chatterbox
  must be told the language of the words it speaks, so `talk` carries the language of Claude's
  reply, from that turn on.
- **A turn ends by what was said, never while the user speaks.** After a pause, den waits 0.6 s
  after a finished sentence, 1.8 s after a very short answer ("No.", often the start of more) and
  2.5 s after words that don't sound finished ("maybe that's gonna"), and not at all while the
  user is speaking or Whisper is still writing down their last words.

## Consequences

- Latency per exchange is the end of the user's speech (about 0.7 s of silence), Whisper (about
  half a second), Claude's reply (a round trip, seconds) and the first sentence's audio (about a
  second). Only Claude's part is outside den's reach; a skill asks for short, direct replies.
- While live, nothing else runs: images, clips, delegation and pi's chat all wait for the end.
