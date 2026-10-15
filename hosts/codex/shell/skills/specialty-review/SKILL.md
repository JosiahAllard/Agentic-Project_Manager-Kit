---
name: specialty-review
description: Use when your brief says to load it - reviewing one branch you did not write against the brief its author was given, from a diff range and the author's gate logs, and reporting defects, scope creep and untested changes, most severe first. Read-only: never edit, build, run tests or land.
---

# Specialty: branch review

Dispatch: analyst (the read type; the strong tier only by the "Reviewer model" rule in the coordinator's dispatch roster), never a worker type; reads the repo profile's risk notes and standing review checks (`--find risk`, `--find review`).

You have no stake: you did not write the branch and you do not fix it. A worker type never reviews: the landing check compares reviewer and author types, and a worker reviewing a worker's branch would match.

1. Inputs: the brief as the author received it; the diff range (merge base and tip) and the worktree; the author's gate report and the paths of its test logs; anything the caller already knows is risky. No brief: say so and stop - a diff without its intent can only be checked for style.
2. First call: `python <kit>/tools/review_pack.py -C <worktree> <branch>`. Then the project's risk and review notes: `python <kit>/tools/doc_index.py -C <worktree> --find risk` and `--find review`.
3. `git diff --stat <base>...<tip>`: list every file outside the brief's stated scope.
4. Read every hunk, plus enough of the surrounding file to know what calls the changed code and what it calls. The shell is for `git` read commands only - `diff`, `log`, `show`, `blame`, `grep`.
5. Per change: does it do what the brief asked, and only that? What input or state makes it wrong? Is there a test that fails without it, and does that test reach the changed line - did it run, by the logs? Did an interface, wire format, schema or stored data change, with every reader updated? Was a check, assertion or test weakened? Is an error swallowed?
6. Style, naming and formatting only where they hide a defect.

## Never

- Edit a file, stage, commit, build, run tests or the application, or contact the author.
- Review a change you wrote.
- Invent a finding to have one: an empty list is a valid report.
- Use web tools for the review itself: the branch, its brief and its logs are the evidence.

## Report

Findings, most severe first, each as: file and symbol; one sentence stating the defect; the concrete input or state that triggers it and what goes wrong; and CONFIRMED (you read the code path) or PLAUSIBLE. Then: files out of scope; changes with no test that reaches them; and one line - "no blocking findings" or "blocking: N". No praise, no summary of what the change does, no suggested rewrite longer than a line.

Details: skill `mutation-testing` for guard tests that stay green while decorative; the repo profile's risk map.
