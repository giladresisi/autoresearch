---
name: create-issue
description: >
  Create a Linear issue (default project "ai-trader") that captures a bug, gap, or
  follow-up with full context — a concrete example occurrence and a grounded fix
  recommendation — and open it directly in "In Progress" status. Use this whenever the
  user wants something turned into a tracked issue: "open/create a Linear issue", "file
  an issue", "make a ticket for X", "log this in Linear", "track this as an issue",
  "raise a ticket", or describes a bug/gap and says to record it — even if they don't say
  "Linear" explicitly but clearly want a tracked issue created. Especially apt when the
  issue comes out of a live-session note, a log occurrence, or a problem just discussed
  in the conversation.
---

# create-issue

Turn a problem, bug, gap, or follow-up into a well-formed Linear issue — opened in
**In Progress** status — with enough context that whoever (person or agent) picks it up
later has everything they need: what it is, why it matters, a real example occurrence, and
a concrete fix recommendation.

The value of this skill is the **context gathering**, not the API call. A one-line issue
("fix the kill scoping") is nearly worthless; an issue that includes the exact failure, a
real occurrence with timestamps/IDs, the relevant code, and a grounded recommendation is
something the assignee can act on immediately. Spend your effort there.

## What to gather (the issue's substance)

Assemble these before creating the issue. Pull from the conversation, the codebase, and any
source the user points you to.

1. **Summary / problem** — what the issue is and *why it matters* (impact / risk). One or
   two short paragraphs.
2. **Specific example occurrence** — a concrete instance: the live-session event, the log
   excerpt, the timestamp / pid / price / order-id, the failing input, the exact sequence.
   This is what makes an issue real and reproducible. See "Finding the example" below — and
   if you can't find one, **ask the user** rather than invent one.
3. **Recommendation / how to fix** — if the source you're drawing from already proposes a
   fix (e.g. a `comments.md` note, a design doc), quote/adapt it **and verify it against the
   current code** — it may already be partially or fully implemented. If no recommendation
   exists, **explore the code, analyze the described scenario, and propose one**, grounded
   with `file:line` references.
4. **Severity / scope** — severity/priority (impact + urgency) and scope/simplicity
   (implementation effort). Assess these yourself from the analysis; they help the user
   prioritize.
5. **Code references** — `path:line` for the relevant functions, so the assignee jumps
   straight there.

## Finding the example occurrence (and when to ask)

A specific example is what separates a useful issue from a vague one, so spend real effort
locating it — in this order:

1. **Inline context first** — scan the current conversation for a concrete occurrence (an
   event the user described, a log line, a debugging session, a number they cited).
2. **A source the user directs you to** — if the user points at a file or location ("it's
   in yesterday's `comments.md`", "check the session log", "see the regression output"),
   read it and extract the concrete instance(s): timestamps, IDs, prices, the exact sequence.
3. **If you still can't find one — ask.** Do **not** fabricate an example. Ask the user
   plainly, for example: *"I couldn't find a specific example occurrence in the context I
   have. Do you have one to include — a timestamp, a log excerpt, a session/date where this
   happened, or a place I should look?"* A real occurrence is worth the round-trip; a
   made-up one is worse than none.

## Issue body structure

Write the description in Markdown using this shape. Adapt the headings to fit the issue;
omit a section only if it genuinely doesn't apply.

```markdown
**Source:** <where this came from — the conversation, `sessions/<date>/comments.md`, a design doc, …>

## Summary / problem
<what it is + why it matters / the risk>

## Specific occurrence
<the concrete example: timestamps, pids, prices, order ids, log lines, the exact sequence>

## Recommendation
<the fix — adapted from the source if one exists (verified against current code), else your own analysis with file:line refs>

## Severity / scope
- **Severity:** <Urgent | High | Medium | Low> — <one-line why>
- **Scope:** <Trivial | Small | Medium | Large> — <one-line why>
```

Keep code identifiers, file paths, and `file:line` references literal so they stay
clickable / greppable.

## Creating the issue in Linear

The Linear tools are MCP tools that load on demand — fetch them first:

```
ToolSearch: select:mcp__linear-server__list_projects,mcp__linear-server__save_issue
```

1. **Resolve the project.** Default to **`ai-trader`** (this repo's Linear project) unless
   the user names a different one. Confirm it and grab its team:
   `mcp__linear-server__list_projects` with `query: "<project name>"` → note the project's
   `team` (e.g. "Gilad Resisi") and the project name.
2. **Create the issue** with `mcp__linear-server__save_issue`:
   - `title` — concise and specific; describe the problem, not "fix X".
   - `team` — the project's team (**required** on create).
   - `project` — the project name (e.g. `"ai-trader"`).
   - `description` — the Markdown body above. Use **literal newlines**, not `\n` escapes.
   - `priority` — map from severity: Urgent→1, High→2, Medium→3, Low→4.
   - `state: "In Progress"` — open it directly In Progress (this skill's default). If the
     created issue still comes back in Backlog, follow up with one more `save_issue`
     `{ id: "<identifier>", state: "In Progress" }`.
3. **Report** the issue identifier (e.g. `GIL-12`) and its URL back to the user.

## Notes

- **Don't over-ask.** If you have a clear problem, a real example, and can form a
  recommendation from the code/source, just create the issue and report it. Reserve
  questions for the genuinely missing piece — most often the example occurrence.
- **If the source's proposed fix is already implemented**, say so in the issue and re-scope
  it to the residual/remaining work — a short "Status (read first)" note at the top of the
  body keeps the issue honest.
- **One issue per distinct problem.** If the user lumps several together, confirm whether
  they want separate issues before creating them.
