# Allow rules for landing

`tools/allow_rules.py` prints the Claude Code permission rules that let an agent submit a landing
of one repository (`land.py ... --submit`, or the kit command's `kit land ... --submit`) without a
prompt, and nothing wider. The owner pastes
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
- **K**: the current plugin export folder, `<plugin.export_dir>/<folder>`, where `<folder>` comes from the export root's marketplace.json and must hold the export marker (docs/plugin-source.md). No export hash is spelled in the code. The `kit land` lines do not name K.
- **R**: the repository's main checkout (`kitlib.git.repo.main_checkout_toplevel`), so a worktree path gives the same lines.

Both are written with forward slashes.

The tool refuses when:
- there is no current export;
- K or R holds a space, a quote, a backtick, `( ) * ? [ ] ; & | $ % < >`, a control character (U+0000-U+001F, U+007F), or any other character a deny line names (`_UNSAFE` in `kitlib/allowrules/__init__.py`). A bare rule cannot spell such a path, and a deny naming a character of the path would refuse every submit; the quoted-only form is not emitted.

## The lines

Each line comes in three spellings of the land command (`rules.commands`): `python K/tools/land.py`,
`python "K/tools/land.py"` and the kit command's `kit land` (`KIT_COMMAND`; tools/kit.py runs the same
land.py). The kit spelling names no export folder, so its lines outlive a plugin refresh, and the
session-start check for old exports does not look at them. Below, `<script>` stands for each
spelling. The allows are `PowerShell(...)` only; the denies come in both `Bash(...)` and
`PowerShell(...)`. So the lines are:

- **Allow:** `PowerShell(python <script> -C R * --expect-tip * --submit)`, one line per spelling,
  3 in all (`PowerShell(kit land -C R * --expect-tip * --submit)` for the kit). Nothing may follow
  `--submit`: a `*` at the end would carry `> file`.
- **Deny:** 55 lines per shell and spelling, and one more for the kit's (`^`, below), 332 in all:
  - `python <script> * -C*`: a second `-C` after a space, or a glued one;
  - `python <script> -C R *-C*`: the text `-C` anywhere after the fixed `-C R` prefix;
  - `python <script> *--lo*`: `--log-dir` or any prefix of it, so an allowed submit cannot send the gate's logs anywhere (`land.py` takes abbreviations);
  - `python <script> * -- *`: a bare `--` (`END_OF_OPTIONS`). argparse reads everything after it as a branch name, the trailing `--submit` included, so `land.py -C R --expect-tip T -- kit/a --submit` would run a foreground landing. With `--submitted P` before the `--` it would also skip `land.py`'s "`--submit` and `--submitted`, not both" check (`test_double_dash_makes_submit_a_branch_denied`);
  - `python <script> *X*` for each X of `METACHARS` in `kitlib/allowrules/rules.py`: `> < | ; & $ ( % ' " \ { } # @`, the backtick, tab, CR, LF and NUL, the typographic quotes U+2018-U+201E, the dashes U+2013-U+2015, and `SEPARATORS`: VT, FF, U+0085, U+00A0, U+1680, U+2000-U+200A, U+2028, U+2029, U+202F, U+205F and U+3000;
  - `kit land *^*`, the kit spelling only (`CMD_ESCAPE`): cmd.exe's escape character. INFERENCE, not yet probed: if `kit` on the PATH is a `kit.cmd` shim, cmd.exe reads the line before python does and strips each `^`, so `kit land -C R kit/a -^C <other> --expect-tip T --submit` would match the allow, miss the `* -C*` deny and give `land.py` a second `-C`. The other characters cmd.exe reads (`%`, `&`, `|`, `<`, `>`, `(`, `"`) are in `METACHARS` already. `!` is not denied: it expands only under delayed expansion, which is off by default under `cmd /c`. If `kit` is an `.exe`, the `^` deny costs nothing (`test_cmd_escape_denied_in_the_kit_spelling`).

The script spellings' lines take no `^` deny: a python command line does not pass through cmd.exe, so `-^C` reaches `land.py` as written, and argparse refuses it.

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
- PowerShell splatting, `@var`, which passes a variable's contents as arguments. An automatic variable needs no setting: `@PSVersionTable` passes `-CLRVersion:...`, which argparse reads as a second `-C`. A variable set by an earlier approved call may still be there: after `$x=,'--'`, `... @x kit/a --expect-tip T --submit` would step around the `-- ` deny. Probed on 5.1: an array splats one argument per item, and a plain string splats one argument per character. No submit needs `@` (`test_splatting_denied`);
- `%VAR%`;
- brace expansion;
- a quote, backslash or tab that hides a second `-C`, or glues a flag into a value. In Bash, `--expect-tip abc\ --submit` is one word, so `land.py` would run without `--submit`;
- a cut that drops the trailing `--submit`, so `land.py` would run a foreground landing (say from an agent-written `--submitted` payload): ` #` starts a PowerShell comment, PowerShell ends a statement at a bare CR, and a native command line ends at NUL;
- PowerShell's other quotes, U+2018-U+201B (single) and U+201C-U+201E (double). It strips them like `'` and `"`, so `-‘C’` reaches `land.py` as `-C` and `--l‘o’g-dir` as `--log-dir`;
- PowerShell's other parameter dashes, U+2013-U+2015. Windows PowerShell 5.1 passes them to python as written, so `–C` is no `-C` there; they are denied anyway, since no submit needs one;
- PowerShell's other argument separators (`SEPARATORS`). Each splits a native argument as a space does, so `T<U+00A0>--<U+00A0>kit/a` reaches `land.py` as `T`, `--`, `kit/a` and would step around the spaced `-- ` deny. With them denied, a space is the only separator an allow's `*` carries.

How PowerShell treats `#`, CR, NUL, these quotes, dashes and separators was probed on Windows PowerShell 5.1 on 2026-10-19: a script printed its `sys.argv` for each case run through `Invoke-Expression`. The same probe showed that a bare `--` reaches python as written, and that a comma neither splits a native argument (`T,-C` arrives as one) nor forms a `--` token (`T,--` is a parse error). PowerShell 7 was not probed.

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

- Variables set by earlier calls are not seen. The rules match one command's text, so they deny every way the text names a variable (`$`, `@`, `%`), not the variables themselves.
- Other control characters and the zero-width characters are not denied. In the 5.1 probe they reached python as written, inside the argument: U+001A, U+001B, U+180E, U+200B and U+FEFF. None splits an argument, so none forms a `--` token, and every other deny matches text, so none hides a `-C` or `--lo`.
- Whether Claude Code reads a rule holding CR, LF or NUL as that literal character is INFERENCE, like the tab.
- `^` is not in `_UNSAFE`, so the tool does not refuse an R that holds one. The kit spelling's `^` deny then matches every `kit land` submit of that repository: those get the normal prompt, and the script spellings are still allowed.

## Open note

The allows name the export folder under `%LOCALAPPDATA%`. If an agent could write that folder, an
allow would run code the agent wrote. Whether the guard refuses agent writes there is not checked
here. The `kit land` allow names no folder at all: it runs whichever `kit` the shell finds first on
the PATH, so the same holds for every folder on the PATH before the kit's own.

## Session start

pm-start's `allow_rules` check (docs/pm-start.md "Allow rules") scans the repository settings of
`launcher.repos` and the PM repository for rules that name a vanished or superseded export.
