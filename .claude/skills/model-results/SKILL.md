---
name: model-results
description: Compare the models used through Donut on result quality, the user's satisfaction and cost, from Donut's own saved logs. Use when asked which model gives the best results, the best results for the money, which worker, planner or soloist to use, or how a model has been doing.
---

# Model results from Donut's logs

Donut only logs. This skill does the analysis, read-only, from what Donut saved. Do not change, move or delete any log, and do not send log content anywhere: the logs hold the user's prompts and project details.

## Where the logs are

The state folder is `$XDG_STATE_HOME/donut` (default `~/.local/state/donut`). A named profile has its own folder, `profiles/NAME/` inside it. Ask which profile when the user has more than one, or report each separately; never mix them.

### `/work` runs: `run-*/results.json`

A list of records, one per ticket attempt, in order. One ticket can have several records in a run (a requeued or recovered ticket). Group by `task` within a run; the last record holds the final status.

| Field | Meaning |
| --- | --- |
| `task` | Ticket file name |
| `status` | `Done`, `Partial` (done with acceptance checks left unticked), `Blocked` |
| `review` | `{"rounds", "verdict", "feedback"}`: `verdict` is `approved`, `changes`, or `unavailable` |
| `recovery` | The planner's judgement of a block: `requeued` or `split` (the ticket was wrong), `fixed` (the planner finished a small remainder), `needs human`, or missing |
| `unticked` | Acceptance checks a Partial ticket left unticked |
| `worker_calls`, `review_calls`, `planner_calls` | One entry per agent call: `{"model", "where", "dollars"}`. `where` is `plan`, `api`, `bedrock`, `vertex` or `local`. `dollars` is `null` when the model has no price |
| `verify` | Result of the ticket's Verify command, when it has one |
| `completion_notes` | The worker's notes, shortened |

Records saved before 2026-11-12 have no `*_calls` and no `recovery`. For those, find the worker model in the run's first worker log, `NNN.jsonl` (not `.review`, `.recover` or `.rework`): a Claude or Qwen log has a JSON line with `"type": "system", "subtype": "init"` and a `model`; a Codex log has only `"type": "thread.started"`, so its model is not recorded. Report those as "Codex, model not recorded", never as a guessed model.

### Conversations: `conversations.json` and `transcripts/ID.jsonl`

`conversations.json` lists conversations: `id`, `project`, `session`, `mode` (`plan`, `solo`, `review`), `alias`, `model` (the latest model), `turns`, `updated`.

`transcripts/ID.jsonl` has one line per exchange, in order: `you` (the user's message), `answer`, and, from 2026-11-12, `model` (the model that answered) and `dollars` (what the turn cost; `0` on a plan counted as free). For older lines use the record's `model`, and say that a model switch inside such a conversation cannot be seen.

## How to judge

- **Quality of a worker:** share of tickets that ended `Done`; share approved on the first review; share blocked where `recovery` is not `requeued`, `split` or `fixed` (a block the planner did not blame on the ticket). `Partial` is not `Done`.
- **Satisfaction:** for each answer, read the user's next message in the same conversation and judge it yourself: satisfied (thanks, a go-ahead, moving on to the next step), not satisfied (a correction, a complaint, the same question again, "?" alone, "this should not be happening"), or neutral. The last answer of a conversation has no reaction; leave it out. Quote two or three short examples per model for the judgement, never long passages.
- **Value for money:** dollars per `Done` ticket, and dollars per satisfied answer, only over records that have costs. A plan-covered amount is the list price of the work, not a bill: say so.
- **Best for the money above a quality floor:** first drop models below the floor (ask the user for it; if they give none, use: at least 70% `Done` and at least 80% satisfied), then rank the rest by cost.

## What to say about the limits

Always state, briefly:

- The sample size per model; mark any model with fewer than 10 tickets or 10 rated answers as too few to judge.
- That models were given different work, so a difference can come from the tickets, not the model.
- Which records had no model or no cost, and how many.
- That satisfaction is inferred from wording.

## Output

One table for workers and one for conversations, a short recommendation per role (planner, worker, reviewer, soloist), and the limits above. Keep it short.
