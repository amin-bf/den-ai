# Voice lessons, with the tests behind them

Measured with Chatterbox Multilingual V3 through den's broker on a 12 GB card.

- **Speech takes longer than a tight script allows.** "Every morning I walk down to the harbour"
  given 1.6 s took 2.4 s, and "The sea is calm and silver" given 1.4 s took 2.1 s: each line ran
  past its time and pushed the next. About 2.5 words a second fits. On a clip, den lengthened it
  from 4 s to 5 s to hold the voice, and said so.
- **A voice job costs about 20 s at the least.** Two short lines took 21 s and one line in a
  cloned voice 18 s, most of it starting the speech model; each further line adds a second or
  two. Batch a script's lines into one request rather than one request per line.
- **Lines in turn come out a breath apart, and the SRT says where.** "For forty years, I kept
  this light burning." and "Tonight, someone else will keep it." as `lines` landed at
  0.3–2.38 s and 2.78–4.66 s; the SRT beside the track holds exactly those times.
- **Whisper writes numbers as digits and times a little coarsely.** The narration above came back
  as "For 40 years, I kept this light burning." at 0.0–2.76 s (said at 0.4 s) and "Tonight
  someone else will keep it." at 4.32–5.5 s; 36 s including Whisper's first download. Fix the
  words and the times before speaking a transcript again.
- **Cloning works from any clean recording.** A 6.5-second track of the model's own voice kept
  as a voice gave a line in that voice, in one request with no other setup.
