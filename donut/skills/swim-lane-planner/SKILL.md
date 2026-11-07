---
name: swim-lane-planner
description: Research this project and write small self-contained swim-lane implementation tickets. Use when asked to plan work for Donut; do not implement tickets during planning.
---

# Swim-lane planner

Read project instructions and actual code before writing tickets. Describe the user's intended change, not an invented redesign. Preserve unrelated work. Do not implement unless the user explicitly asks you to switch roles.

Write Markdown tickets in swim_lanes/to_do/. Use NNN_YYYYMMDD-HHMM_specific-slug.md with a unique order number across all lanes. Append after the highest number; never renumber in_progress or done. Reference dependencies by slug, not number. Inspect existing tickets to avoid duplicates.

Each ticket must work in a fresh context. Include exact paths, members, code anchors, dependency signatures, preflight checks, and explicit expected test output. Never invent APIs or assume earlier session memory. Keep one behavior per ticket, preferably ≤2 implementation files and ~40 changed lines; split larger or conceptual changes into independently verifiable steps. Use the project's builders for generated files (including Unity scenes).

Use these sections:
- Title and a short user-facing description.
- **Slug:** `specific-slug`
- **Depends on:** `other-slug`, … or None
- Goal
- Context: actual code paths, members and anchors; required project conventions.
- Preflight: prove dependencies exist before edits.
- Files: explicitly Modify, Create, and Do not touch.
- Requirements: numbered observable behavior, at most five.
- Implementation steps: precise enough for the worker; provide exact math/async/physics logic where guessing is risky.
- Testing: real commands, expected outcomes, relevant existing regression checks. Avoid tests that merely mirror implementation.
- Acceptance criteria: checkboxes, with subjective items explicitly labeled **Human check**.
- Out of scope
- Completion notes: placeholder for the worker's Changes, Tests, Deviations and Open issues.

Do not authorize commits, pushes, installs or external mutations unless the user requested them. Use project-specific skills only when relevant. Do not copy unrelated project rules into tickets. End with a brief ticket list and any unresolved assumptions.
