---
status: proposed
---

# Standby: listening for a wake word without taking the machine

A client that keeps an assistant a word away needs den to listen between live
conversations for a wake word ("hey" and a name the user picks), and to start a conversation when
it's said. A live conversation takes the machine (ADR live-conversation); standby must not, since images, clips
and the LLM should keep working in between. And nothing said in standby may reach any model but
the wake word detector, or leave the machine.

## Decisions

- **The detector is a small transcription model on the CPU, not a wake word model.** Dedicated
  wake word models are trained per phrase, so a name picked at runtime would mean training a
  model first. A small transcription model, on two CPU threads, hears the first two seconds of
  every utterance the voice-activity detector cuts and den compares what it wrote with the wake
  word; the text goes no further. It costs about 5 % of a core while the room is quiet, about a
  second of two cores per utterance, some 750 MB of RAM, and nothing on the GPU.
- **By sound, and strict.** Transcription spells a name as it likes ("Elli", "Ellie", "Ely"), so
  the start of the utterance and the wake word are compared joined and roughly by sound. A near
  name ("hey Olli", "hey Emily") doesn't count: a false wake takes the machine for a conversation,
  a missed one is only said again.
- **Only at the start of an utterance.** The wake word counts when an utterance begins with it
  (after a pause, or one filler word). Words before it in the same breath would otherwise be in
  the recording that the conversation's transcription writes down; cutting at the word by the
  detector's timings was the other way, more forgiving and more fragile.
- **Standby isn't a side.** It holds no GPU memory, so every side stays available, a release
  (`den unload`) leaves it listening, and the busy gate doesn't apply. `den mode off` ends it: off
  means den is off.
- **The conversation starts in the same process.** Standby is the live engine started with a wake
  word; `live_start` loads the conversation's models into it, so the microphone never stops. From
  the wake word on, what the user says is kept as audio (up to two minutes) and written down once
  the conversation's transcription model is loaded, the wake word taken off: the first words after "hey Elli"
  aren't lost while the machine is freed, even when that means waiting for a clip to finish.
  Nothing begun before the wake word is kept. The echo canceller loads with the conversation, not
  in standby, where den doesn't speak and a new microphone on the system would only be in the way;
  the microphone itself is shared, and other programs can go on recording from it.
- **The client starts the conversation.** The wake word is an event, not a conversation: den
  reports it and the client calls `live_start` (or doesn't). Unanswered for 60 s, what was said
  since is forgotten and den listens for the wake word again.
- **One state, counted.** Standby is `off`, `starting`, `listening`, `woke` or `live` (paused for a
  conversation, whoever started it); every change counts up `seq`, and `POST /standby/wait {after}`
  answers once it has passed `after`. A client learns that way that the wake word was heard, that
  a conversation paused standby, that it came back after it, or that it ended and why.
- **It belongs to the client that asked.** Standby lives in the broker's memory, not in
  `state.json`: after a restart or a reboot den listens to nobody's room until a client turns it
  on again.

## Consequences

- The wake word fires about a second and a half after the user stops speaking: the VAD's pause,
  then the detector. The conversation's models take longer to load than that, and a conversation
  still waits for running work to finish before it has the machine.
- A client polls nothing: it holds a `POST /standby/wait` open and asks again with the new `seq`.
