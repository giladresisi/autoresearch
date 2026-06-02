---
name: live-comment
description: >
  Use to jot a timestamped observation into the current session's comments.md while
  the live orchestrator is running and the trading session is open — the user is
  watching the live chart and wants to capture a thought about price action, a level
  touch, a setup, or something to follow up on. Trigger phrases: "live-comment ...",
  "note that ...", "log a comment", "add a session note", "jot this down", "comment
  on the session", "make a note about ...", "remember that the price ...", or any
  free-text observation the user wants recorded against today's live session. The
  text the user provides IS the observation to record. Do NOT use this for post-session
  review (that's session-analysis), for plotting (live-trading), or when no orchestrator
  is running — the skill self-checks and exits early if it isn't a live, open session.
---

# live-comment

Capture the user's in-the-moment observation as a timestamped note in the current
session's `comments.md`. The user is watching a live chart and wants a thought
recorded — either as a brand-new note, or folded into an existing note if it's a
follow-up on something already written. Getting the *update-vs-new* decision right is
the heart of this skill: notes should read as a coherent log, not a pile of near-duplicates.

The observation to record is whatever text the user passed when invoking the skill
(the argument / the message). If they invoked the skill with no observation text, ask
them what they'd like to note before doing anything else.

## Step 1 — Gate: is this a live, open session?

This skill only makes sense mid-session, while the orchestrator is live and the session
window is open. Commenting into a stale or non-existent session would scatter notes into
the wrong folder, so verify first:

```bash
uv run python .claude/skills/live-comment/scripts/check_live_session.py
```

- If it prints `STATUS: NOT_LIVE`, **stop here.** Tell the user the reason it reported
  (orchestrator not running, or the session window is in the 16:55–18:05 ET maintenance
  gap) and do not create or edit anything.
- If it prints `STATUS: LIVE`, read the fields it emits and continue:
  - `COMMENTS_PATH` — the file to write to (absolute path)
  - `COMMENTS_EXISTS` — `true` if a comments.md already exists for this session
  - `SESSION_DATE` — the session date (TH-calendar, stable within a CME session)
  - `NOW_ET` — current Eastern time; this is the timestamp for the note's heading

## Step 2 — Ensure comments.md exists, and read existing notes

If `COMMENTS_EXISTS` is `false`, create the file with this header (use the Write tool),
then add the user's observation as the first note (Step 4 — it's necessarily a new note):

```markdown
# Session Comments — {SESSION_DATE}
```

If `COMMENTS_EXISTS` is `true`, **Read the file** so you can compare the new observation
against what's already there. This read is required before you can edit it, and it's
what makes the next step possible.

## Step 3 — Decide: update an existing note, or write a new one?

Read every existing note and ask: *is the user telling me more about something already
recorded, or is this a distinct observation?*

- **Same thing → update that note.** Use this when the new text is a follow-up,
  resolution, or continued development of an existing note: the same level, the same
  setup, the same price-action episode. Example: a note about "the 21:36 daily-mid touch
  and sideways zigzag" already exists, and the user now says where the price finally
  broke — that belongs *inside* that note, because it closes the loop the earlier note
  opened. Append the new detail under the existing note (see the update format in Step 4),
  rather than starting a fresh one that would fragment the story.

- **Different thing → write a new note.** Use this when the observation is about a
  distinct event, level, or idea. If one or more existing notes are *topically related*
  but not the same thing (e.g. a different level, or an earlier setup that this one
  rhymes with), add a `Related:` line to the new note that points to them by heading, so
  the connection is discoverable later.

- **Genuinely ambiguous → ask the user.** If you can read it both ways — it could be a
  fresh note or a fold-into-existing — don't guess. Use AskUserQuestion to present the
  candidate note(s) you'd update versus creating a new one, and let the user decide.
  Ambiguity here is cheap to resolve and expensive to get wrong, because a mis-merged
  note buries information and a wrongly-split note hides a connection. When unsure, ask.

Be decisive when it's clear, though — don't ask on an obvious new-vs-existing call.

## Step 4 — Write the note

**Timestamping:** every note's heading carries the invocation time from `NOW_ET` — when
the comment was made. If the user's text references a specific chart/event time (e.g.
"the 21:36 touch"), preserve that *in the body*; the heading time is when they commented.
This keeps comments.md readable as a chronological log of when observations were made.

**New note** — append a section to the end of the file:

```markdown

## {HH:MM} ET — {short subject}

{the user's observation, lightly cleaned up; preserve any event times they mention}

Related: see "{HH:MM} ET — {other subject}" above.   ← only if a related note exists
```

**Update an existing note** — append the new detail beneath the matching note's body,
tagged with the current time so the note's internal progression stays clear:

```markdown
_Update {HH:MM} ET:_ {the new development / resolution}
```

Keep the user's voice. Light cleanup (fix obvious typos, complete a clipped sentence) is
welcome; rewriting their meaning is not. Don't invent analysis they didn't express — if
they only noted *what* happened, don't fabricate a *why*.

## Step 5 — Confirm

Briefly tell the user what you did: which note you created or updated, and the file it
landed in (e.g. `sessions/2026-06-02/comments.md`). One or two lines — they're watching a
live chart and want to get back to it.

## Worked example

The user invokes: `live-comment following the 21:36 ET touch of the daily mid the graph
started zigzagging sideways below it; maybe we should have anticipated this. I'll follow
up when I see where it heads next.`

- Gate passes (orchestrator alive, 22:xx ET, window open).
- comments.md doesn't exist → create it with the header, then add the first note:

```markdown
## 22:10 ET — daily mid touch, then sideways zigzag below

Following the 21:36 ET touch of the daily mid, the graph started zigzagging and
moving sideways below the daily mid. Maybe we should have anticipated this.

Followup pending: revisit once we see where the graph heads next and when.
```

Later, the user invokes: `it finally broke down out of that range at 23:40 and ran to the
day low.` That's the *same* episode the first note opened — so update it rather than
create a new note:

```markdown
_Update 23:42 ET:_ Broke down out of the range at 23:40 ET and ran to the day low —
resolves the followup above.
```
