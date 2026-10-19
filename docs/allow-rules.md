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
- **Exit codes:** 0 when the lines are printed. 2 with `REFUSED: <reason>` on stderr (nothing on stdout) or on a usage error.

The lines name:
- **K**: the current plugin export folder, `<plugin.export_dir>/<folder>`, where `<folder>` comes from the export root's marketplace.json and must hold the export marker (docs/plugin-source.md). No export hash is spelled in the code.
- **R**: the repository's main checkout (`kitlib.git.repo.main_checkout_toplevel`), so a worktree path gives the same lines.

Both are written with forward slashes.

The tool refuses when:
- there is no current export;
- K or R holds a space, a quote, a backtick, `( ) * ? [ ] ; & | $ % < >`, a control character (U+0000-U+001F, U+007F), or any other character a deny line names (`_UNSAFE` in `kitlib/allowrules/__init__.py`). A bare rule cannot spell such a path, and a deny naming a character of the path would refuse every submit; the quoted-only form is not emitted;
- the repository's config cannot be read;
- a lane name is not `[A-Za-z0-9][A-Za-z0-9_.-]*`.

## The lines

The lanes are `full` plus the `"landing".lanes` of the main checkout's `.kit.json` at HEAD, which is
the set `kitlib/gate/land.py` checks `--lane` against. `land.py`'s own `--lane` is free text.

The lane lines limit only a trailing `--lane <lane>`, the one after `--submit`. A `--lane` inside
the first `*` takes any value. That is safe because `land.py` itself refuses a lane that is not
`full` or in `.kit.json` (`config_refusal` in `kitlib/gate/land.py`), not because of the rules.

Each line comes in two script spellings, `K/tools/land.py` and `"K/tools/land.py"`. The allows
are `PowerShell(...)` only; the denies come in both `Bash(...)` and `PowerShell(...)`. So the lines are:

- **Allow:** `PowerShell(python <script> -C R * --expect-tip * --submit)`, plus one line per lane
  that adds ` --lane <lane>`. There is never a `--lane *`: a `*` at the end would carry `> file`.
  With no extra lanes this is 4 lines.
- **Deny:** 32 lines per shell and spelling, 128 in all:
  - `python <script> * -C*`: a second `-C` after a space, or a glued one;
  - `python <script> -C R *-C*`: the text `-C` anywhere after the fixed `-C R` prefix;
  - `python <script> *--lo*`: `--log-dir` or any prefix of it, so an allowed submit cannot send the gate's logs anywhere (`land.py` takes abbreviations);
  - `python <script> *X*` for each X of `METACHARS` in `kitlib/allowrules/rules.py`: `> < | ; & $ ( % ' " \ { } #`, the backtick, tab, CR, LF and NUL, the typographic quotes U+2018-U+201E and the dashes U+2013-U+2015.

argparse keeps the last `-C`, so a second one would move the landing to another checkout. The
`-C` denies close it however it is spelled: `'-C'`, `"-C"`, `\-C`, `{-C,...}`, a tab before it, or
`,-C`. They also deny legitimate text that holds `-C`. In PowerShell that includes `-c`, since
PowerShell matches case-insensitively, so a branch such as `kit/x-check` is denied there.

There is no Bash allow, because Bash globs. `-[C]`, `-?` or `-*` becomes `-C` when a file named `-C`
is in Bash's directory, and `--l[o]g-dir` becomes `--log-dir` the same way. A deny cannot name a
literal `*`, so no deny set closes a second `-C` there. PowerShell and Python do not glob arguments
on Windows. The Bash denies stay; they cost nothing. A Bash submit gets the normal prompt, and so
does a PowerShell submit whose text holds `-c` (a branch such as `kit/x-check`).

The metacharacter denies close what an allow's inner `*` would otherwise carry:
- a redirect;
- a chain;
- `$(...)`, backticks and `$var`;
- PowerShell's `(expr)` and `@(...)`;
- `%VAR%`;
- brace expansion;
- a quote, backslash or tab that hides a second `-C`, or glues a flag into a value. In Bash, `--expect-tip abc\ --submit` is one word, so `land.py` would run without `--submit`;
- a cut that drops the trailing `--submit`, so `land.py` would run a foreground landing (say from an agent-written `--submitted` payload): ` #` starts a PowerShell comment, PowerShell ends a statement at a bare CR, and a native command line ends at NUL;
- PowerShell's other quotes, U+2018-U+201B (single) and U+201C-U+201E (double). It strips them like `'` and `"`, so `-‘C’` reaches `land.py` as `-C` and `--l‘o’g-dir` as `--log-dir`;
- PowerShell's other parameter dashes, U+2013-U+2015. Windows PowerShell 5.1 passes them to python as written, so `–C` is no `-C` there; they are denied anyway, since no submit needs one.

How PowerShell treats `#`, CR, NUL, these quotes and dashes was probed on Windows PowerShell 5.1 on 2026-10-19: a script printed its `sys.argv` for each case run through `Invoke-Expression`. PowerShell 7 was not probed.

A legitimate submit holds none of them. A summary with spaces needs quotes, so it is denied. So is a summary with `;`, `&`, `%`, `$`, `(`, `|` or a brace. Use a one-word summary or leave it out.

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
  - A `\` or a tab in a rule is literal text, not an escape.
  - A deny is also checked against the whole, unsplit command.
  - The kit guard (a PreToolUse hook) still runs before these rules and can refuse.

## Abbreviations

argparse takes any unique prefix of a long option. A rule that names a flag can be stepped around
with a prefix of it, unless the tool sets `allow_abbrev=False`.

- **`land.py`:** a prefix inside the first `*` is no hole.
  - `--sweep-landed`, `--submitted`, `--queue` and `--apply`, and every prefix of them, are options `land.py` refuses beside `--submit` (exit 2). `test_abbreviation_matches_the_rule_and_land_refuses_it` pins this.
  - `--log-dir` and its prefixes (`--lo`, `--log`, ...) would let a submit write the gate's logs anywhere. The `*--lo*` deny closes them (`test_log_dir_denied`).
- **`tools/launch.py` and `tools/ledger.py`:** these set `allow_abbrev=False` on the parser and every subparser, so their rules can be written later without this gap (`tests/launcher/test_no_abbrev.py`, `tests/ledger/test_no_abbrev.py`).

## Known limits

- `--` before `--submit` makes `--submit` a branch name, so `land.py` would land without submitting. The allows match it, and `land.py` refuses it as a bad revision (`bad_rev`), not the rules.
- PowerShell splatting (`@var`) is not denied: `@` alone is no metacharacter here. A splat needs a variable set earlier in the same shell, which the allow does not carry.
- Other control characters and Unicode spaces are not denied. In the 5.1 probe, VT, FF, U+00A0, U+0085, U+2028 and U+2029 split arguments as a space does, and the other control characters tried (U+001A, U+001B) reached python as written. Every deny matches text, so neither hides a `-C` or `--lo`.
- Whether Claude Code reads a rule holding CR, LF or NUL as that literal character is INFERENCE, like the tab.

## Open note

The allows name the export folder under `%LOCALAPPDATA%`. If an agent could write that folder, an
allow would run code the agent wrote. Whether the guard refuses agent writes there is not checked
here.

## Session start

pm-start's `allow_rules` check (docs/pm-start.md "Allow rules") scans the repository settings of
`launcher.repos` and the PM repository for rules that name a vanished or superseded export.
