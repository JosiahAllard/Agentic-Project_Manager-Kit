# Allow rules for landing

`tools/allow_rules.py` prints the Claude Code permission rules that let an agent submit a landing
of one repository (`land.py ... --submit`) without a prompt, and nothing wider. The owner pastes
them into the main checkout's `.claude/settings.local.json`. The tool writes no file, and neither
does the session-start check that finds rules naming an old export. Code: `kitlib/allowrules`
(`rules.py` holds the line text and the reference matcher); tests: `tests/allowrules/test_rules.py`,
`tests/pmstart/test_allow_rules_check.py`.

## The command

It runs from the kit, not through a repository stub: it is in `KIT_ONLY` in `kitlib/stubs/__init__.py`.

```
python <kit>/tools/allow_rules.py -C <any checkout of the repository>
```

- **stdout** is one JSON object, `{"permissions": {"allow": [...], "deny": [...]}}`.
- **stderr** carries these notes:
  - `target:`, the file to paste into;
  - `export:`, the export folder the lines name;
  - a `stale:` line for each rule in the repository's settings that names another export;
  - a `note:` when the kit running the tool is not the current export.
- **Exit codes:** 0 when the lines are printed. 2 with `REFUSED: <reason>` (nothing on stdout) or on a usage error.

The lines name:
- **K**: the current plugin export folder, `<plugin.export_dir>/<folder>`, where `<folder>` comes from the export root's marketplace.json and must hold the export marker (docs/plugin-source.md). No export hash is spelled in the code.
- **R**: the repository's main checkout (`kitlib.git.repo.main_checkout_toplevel`), so a worktree path gives the same lines.

Both are written with forward slashes.

The tool refuses when:
- there is no current export;
- K or R holds a space, a quote, a backtick, `( ) * ? [ ] ; & | $ % < >` or a control character. A bare rule cannot spell such a path; the quoted-only form is not emitted;
- the repository's config cannot be read;
- a lane name is not `[A-Za-z0-9][A-Za-z0-9_.-]*`.

## The lines

The lanes are `full` plus the `"landing".lanes` of the main checkout's `.kit.json` at HEAD, which is
the set `kitlib/gate/land.py` checks `--lane` against. `land.py`'s own `--lane` is free text.

Each line comes in two shells (`Bash(...)`, `PowerShell(...)`) and two script spellings:
`K/tools/land.py` and `"K/tools/land.py"`. So the lines are:

- **Allow:** `python <script> -C R * --expect-tip * --submit`, plus one line per lane that adds
  ` --lane <lane>`. There is never a `--lane *`: a `*` at the end would carry `> file`.
  With no extra lanes this is 8 lines.
- **Deny:** `python <script> * -C*` (a second or glued `-C`), plus `python <script> *X*` for each
  X in `> < | ; & $ ( %` and the backtick. That is 40 lines.

The metacharacter denies close what an allow's inner `*` would otherwise carry:
- a redirect;
- a chain;
- `$(...)`, backticks and `$var`;
- PowerShell's `(expr)` and `@(...)`;
- `%VAR%`.

A legitimate submit holds none of them. A summary with `;`, `&`, `%`, `$`, `(` or `|` in it is denied as well; reword it.

## Matching

`decide()` in `kitlib/allowrules/rules.py` is a reference matcher the tests pin every line against.
It models Claude Code; it is not Claude Code. Its rules:
- `*` matches any text.
- A trailing `:*` is ` *`.
- The command is split on `&& || ; | |& &` and newlines (PowerShell: `&& || ; |` and newlines).
- A deny that matches the whole command or any part wins.
- Otherwise every part must match an allow.
- Bash compares case-sensitively; PowerShell compares case-insensitively.

What rests on Claude Code's documentation and what does not. "The doc" means the code.claude.com
permissions, permission-modes and settings pages, read 2026-10-19:

- **From the doc:**
  - `*` and `:*`;
  - the split operators;
  - every part must match;
  - deny beats allow;
  - PowerShell matches case-insensitively.
- **INFERENCE (the doc is silent):**
  - Bash matches case-sensitively.
  - Runs of spaces are not collapsed. The reference denies `land.py  -C R ...` (two spaces), a harmless false positive, but Claude Code's handling is unknown.
  - Quotes are compared as literal text, not stripped. This is why both script spellings are emitted, and why `-C "R"` or `-C R` with backslashes does not match.
  - A deny is also checked against the whole, unsplit command.
  - The kit guard (a PreToolUse hook) still runs before these rules and can refuse.

## Abbreviations

argparse takes any unique prefix of a long option. A rule that names a flag can be stepped around
with a prefix of it, unless the tool sets `allow_abbrev=False`.

- **`land.py`:** a prefix inside the first `*` is no hole. Every option it can resolve to (`--sweep-landed`, `--submitted`, `--queue`, `--apply`) is one `land.py` refuses beside `--submit` (exit 2). `test_abbreviation_matches_the_rule_and_land_refuses_it` pins this.
- **`tools/launch.py` and `tools/ledger.py`:** these set `allow_abbrev=False` on the parser and every subparser, so their rules can be written later without this gap (`tests/launcher/test_no_abbrev.py`, `tests/ledger/test_no_abbrev.py`).

## Open note

The allows name the export folder under `%LOCALAPPDATA%`. If an agent could write that folder, an
allow would run code the agent wrote. Whether the guard refuses agent writes there is not checked
here.

## Session start

pm-start's `allow_rules` check (docs/pm-start.md "Allow rules") scans the repository settings of
`launcher.repos` and the PM repository for rules that name a vanished or superseded export.
