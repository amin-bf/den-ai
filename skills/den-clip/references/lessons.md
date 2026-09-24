# Clip lessons, with the tests behind them

Measured at 1280x704 with den's Wan 2.2 5B clip workflow (3-second clips) unless noted.

- **A keyframe comes through exactly; make the look there.** A still made with klein 4B at the
  clip's size was the clip's first frame unchanged, and the scene moved on from it sensibly
  (waves breaking and spraying). A clip from text alone gave a coherent scene too, but in a
  look nobody had approved, at three minutes a try.
- **Camera instructions are weak.** "The camera stays still" still gave a slow drift to the
  right, and a lamp "beam sweeping across the sky" didn't appear. Put the motion you need most
  first, and don't count on a static camera.
- **Wan 5B takes about a minute per second of clip** (186 s and 196 s for 3 s at 20 steps); LTX 13B
  about a sixth of that (39 s for 3 s, 69 s for 4 s with three keyframes). A shorter duration
  is the cheap way to try a motion before the full length.
- **Keyframes made by editing one still give a clip that changes, not one that morphs.** Dusk,
  then twilight and night made by klein edits of the dusk still (same rocks, same framing), at
  0, 50 % and the end of a 4-second clip: LTX 13B and Wan Fun Inpaint both passed through all
  three, with the light changing gradually and no visible seam. The waves kept moving
  throughout, though no keyframe showed them move.
- **A head turn works as an edit of the still.** "She turns her head to look straight at the
  camera and gives a small, warm smile. Keep everything else exactly the same: …" kept the
  hands, cup, window, light and framing, and changed only the head and expression. The face
  read slightly younger from the front. As a start and end keyframe, the clip passed through
  both, lowering her eyes on the way.
- **ltx23-distilled took 130 s for a 4-second clip** at 960x544 with two keyframes and sound,
  its model partly in RAM. Its sound at cfg 1 averaged −39 dB, a quiet but present track; a
  cfg-3.5 LTX-2.3 fine-tune made the same prompt's track louder (−21 dB), in line with the model
  card's "higher cfg, more audio".
- **A lip-synced clip carries the voice alone.** The keeper speaking two lines (ltx23-distilled,
  one keyframe facing the camera, `lines` in the default voice): the voice was fixed in the
  model, the clip lengthened from 4 s to 5 s to hold it, and its sound was the clean voice with
  silence (−91 dB) around it, so no ambience from the prompt. 136 s for 5 s. A recording (an
  M4A) instead of a script went the same way in 143 s. Whether the lips follow the words shows
  only in the video, not on the contact sheet: ask the user.
- **A quoted line at cfg 1 comes out slurred.** The keeper (ltx23-distilled, cfg 1, one keyframe)
  prompted to say "Tonight, someone else will keep the light." spoke for 3 s with his mouth moving,
  but Whisper heard "The nigher, summer else will hick the light.": the line's rhythm, not its
  words. The same seed at cfg 3, the voice described ("a deep, clear, slow voice"), gave
  "Tenaya, someone else will heave the light.": most words clear, in 172 s instead of 117 s.
  For a quoted line use cfg 3; for words that must all be understood, a voice-over with `sync`.
