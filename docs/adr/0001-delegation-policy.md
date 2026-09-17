---
status: accepted (trial period; review after ~20 delegations)
---

# Delegate big inputs and private data, and verify during a trial period

Claude delegates to the local LLM for two reasons: to keep big inputs out of its context,
and, as a hard rule, to keep private data off Anthropic's servers. The standing rule lives
in `~/.claude/CLAUDE.md` (*when* to delegate, applying only while the `local_llm` tool is
listed). The `local_llm` tool description holds *how* to ask, so adding a task in
`config.toml` never needs a CLAUDE.md edit. The local model isn't trusted yet: Claude
spot-checks answers, records a verdict per call with `local_llm_feedback`, and
`den log` shows the results so trust can be adjusted per task.

## The rule

- **When:** summarize/extract inputs over ~20 KB or spanning several files; classify more
  than ~30 items; draft only from source material over ~10 KB. Below that, delegating
  (instructions, checks, waiting) costs more than it saves.
- **Private data always goes local**, whatever its size: `.env` files, credentials and
  keys, DB dumps with personal data, exported mail or chats, or anything the user marks
  private. Work code isn't private. Claude passes it via `files` and never opens it, not
  even to verify; if delegation isn't possible, it stops and asks.
- **Trust per task (trial):** summarize: spot-check specific facts and conditional rules
  (only / unless / when), which it flattens into blanket statements or drops to fit a
  length limit; for rules or procedures, don't cap the length and ask for each condition
  as its own bullet (in the tool description). extract: trust what it
  returns, never that the list is complete, nor counts or totals; for "find every X", use
  grep or a script and delegate only the reading or condensing. classify: numbered input
  with the expected count, output checked by script. draft: raw material that Claude
  always rewrites.
- **Verification is cheap on purpose:** grep the source for claimed facts, or read only
  the part a decision depends on. Reading the whole source would undo both goals.
- **Too large for the context window:** split by file or line range. Claude merges the
  partial answers itself (the local merge swapped PR numbers in the test), except for
  private data, where the local LLM merges.
- **Unavailable:** Claude does the work itself and says so; for private data it asks.
- **Visibility:** one line to the user per delegation, plus a verdict
  (`ok | partly | wrong | unchecked`) in `~/.local/state/den/delegations.jsonl`.
- `keep_alive` is `30m`: a cold start costs ~15 s of loading, and the first request reads
  input at about half speed.
- `think` stays `false`: in a three-round test, thinking kept no more conditions than the
  "every condition as its own bullet" request already does. It took ~3.5× as long and
  used ~6K of the 32K context, and added small errors in 2 of 3 rounds.

## Evidence: test on 2026-09-16

`qwen3.6:35b-a3b` (`num_ctx` 32768) on real inputs from a work repo, checked by Claude:

| Test | Input | Time | Result |
|---|---|---|---|
| Summarize a CI/CD doc | 8K tokens | 36 s | Accurate; one small overstatement |
| Summarize 40 commits | 70K tokens | — | Refused as too large (correct) |
| … split into 4 parts | 13–17K each | 23–27 s each | Specific numbers right; one change called "proposed" that was done |
| … local merge of the parts | 1K | 11 s | Swapped two PR numbers; ignored "max 8 bullets" |
| Extract schema properties | 7K | 11 s | Values right; silently merged two schemas when the instruction was ambiguous |
| Extract 90 changelog versions | 11K in / 3.8K out | 62 s | All versions and dates right; ~¼ of entry counts wrong |
| Classify 40 PR titles | 1K | 5 s | 31 labels for 40 items, invalid labels, misaligned |
| … numbered, with count | 1K | 4 s | 40 aligned, ~35 agree with Claude; 3 invalid labels |
| Draft a commit message | 4K | 10 s | Content right; format rules ignored |
| Draft a PR description | 1K | 3 s | Accurate and usable |
| Extract every "never/don't" rule (follow-up, same day) | 11K | 16 s | All 8 returned rules real; found ~1 in 4 of ~33 and stopped partway through the file |
| Summarize 5 sections of the CI/CD doc (second session) | 8.5K | 31 s | Names and numbers right; turned "cancel a running `selected` run" into "cancel running targeted tests" (a running `full` is kept) |
| Classify 40 numbered test paths by operation | 1K | 4 s | 40 aligned, all labels valid, 2 files that aren't tests caught; better than Claude's rule script |
| Summarize the release/recovery sections, 3–6 bullets each (third session) | 8.5K | 19 s | Nothing wrong, but dropped four conditional rules (testing fallback, tag reuse, 2 of 3 regeneration checks, re-approval) |
| Classify 40 numbered test paths by operation, explicit keyword rules | 1K | 4 s | 40 aligned, format exact; 39 match the rules, one ambiguous name mislabelled |
| Summarize the release/recovery sections, no length cap, each condition as a bullet, `think = false` (fourth session, 3 rounds) | 8.5K in / ~0.9K out | 22 s | All four conditions dropped before are kept; nearly a copy of the source (~4.1 of 4.1 KB); no errors |
| … same with `think = true` | 8.5K in / 6.1–8.3K out | 77 s | All four conditions kept; only reformatted (3.8–4.2 KB); `[[…]]` in a JSON example twice, a "because" turned into "when", an "only when" softened, workflow names dropped once |

Speed when warm: reads input at ~720 tokens/s and writes at ~65 tokens/s. It never
invented facts. Its weak spots are the completeness of "find every X" lists, counting,
exact formats and length limits, conditional rules that it flattens when summarizing, and it guesses silently instead of flagging ambiguity.

## Considered options

- **Always verify by reading the source:** rejected; it brings the big input back into
  Claude's context and exposes private data.
- **Verdicts through a CLI command (`den feedback`):** rejected; it causes Bash
  permission prompts in other projects. A second MCP tool behaves the same everywhere and
  disappears with `local_llm`.
- **Rule only in the tool description:** rejected; it's short and easy to ignore. It keeps
  the *how*, and CLAUDE.md holds the *when*.
