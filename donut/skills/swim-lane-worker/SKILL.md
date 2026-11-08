---
name: swim-lane-worker
description: Implement scoped tickets from swim_lanes, verify their tests, record completion notes, and move them between lanes. Use for next-task, named-task, and whole-queue requests; not task planning.
---

# Swim-lane worker

Paths are relative to the project root containing QWEN.md. Follow that project's rules and the assigned ticket. Do not load planner instructions from .claude/, .cursor/, or CLAUDE.md.

## Pick work

- A named task or a batch-runner assignment means that task only, then stop. This overrides picking other in-progress tasks.
- “Next task” means finish one in-progress task first; otherwise pick the lowest-numbered eligible ticket from swim_lanes/to_do/.
- “All” means recheck the queue after each ticket. Do not retry tasks blocked during this run; continue independent tasks, skipping blocked dependencies.
- Tickets have **Slug:** `name` and **Depends on:** `other-slug`, … or None. Dependencies must be in swim_lanes/done/ before starting.
- Keep the complete filename unchanged. Move to_do → in_progress before editing; in_progress → done only after verification.

## On-hold tickets

`swim_lanes/on_hold/` contains deliberately parked tickets. Never pick, move, or edit them, even for a named-task assignment. Report that the ticket is on hold instead. It must first be explicitly released back to `to_do/` by the user or by a planner following the user's request. A held dependency is not complete; skip dependent work. Do not move blocked work into `on_hold/` automatically: keep the existing BLOCKED workflow in `in_progress/`.

## Execute one ticket

1. Read the entire ticket and files it names. Run its Preflight checks before editing. Missing dependencies mean BLOCKED, not permission to rebuild them.
2. Change only files listed in its scope. Read relevant installed skills named by the task, but do not expand task scope based on a skill.
3. Run every listed test. Preserve existing unrelated work. Never weaken tests to make them pass or claim results you did not observe.
4. After three failed attempts at the same problem, or if the specification is ambiguous, stop. Leave the ticket in in_progress and put `BLOCKED:` with the exact reason and output in Completion notes.
5. Tick only objectively verified acceptance boxes; leave Human check boxes unticked. Record Changed, Tests (actual commands/output), Deviations, and Open issues / human checks under Completion notes.
6. Verify the changed files against the allowed scope, accounting for pre-existing changes. Move the completed ticket to done.

Never create, delete, renumber, or rewrite tickets. Only move them, tick acceptance boxes, and fill Completion notes. Do not commit, push, create branches, or install packages unless explicitly authorized by the task/user. Report extra work as open issues, not new tasks.

## Unity projects

Use installed unity-cli and unity-pipeline skills for Editor operations and the task's exact tests. Never hand-edit Unity scene/prefab YAML; use builder or Editor scripts. Do not assume a particular Unity version, rendering pipeline, physics system, input abstraction, asset style, or project path. Derive them from this project. If required Editor access is unavailable, report BLOCKED. Restore any temporary test settings you changed.

## Final summary

End with one brief table, one row per touched task (plus skipped dependencies for a whole-queue run):

| Task | Status | What changed | Tests | Human check |
|---|---|---|---|---|

Statuses: Done, Blocked, Skipped. Report actual test outcomes and outstanding human checks. In batch mode, stop after the assigned ticket; the outer runner launches the next fresh session and aggregates the run.
