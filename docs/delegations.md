# The owner's delegation list

`kitlib.delegations` reads the list of operations the owner lets the PM run for them. It only reads and checks the list. It runs, grants and writes nothing. A runner is a later record (`kitlib/delegations/run.py`, `tools/delegated.py`).

## Where it is

`~/.pm-kit/owner-delegations.json`, beside the machine config (`kitlib.config.user.DIR_NAME`). Only the owner writes it. Nothing moves it: there is no setting and no environment variable, and the old `~/.claude-pm-kit` folder is never read. Tests pass `home=` with a temp folder.

`load(home=None)` returns `Delegations(path, schema, operations)`. It raises these errors:

- `DelegationMissing` when there is no file. That means nothing is delegated.
- `DelegationError` for anything else, naming every problem in one message:
  - a folder in the file's place;
  - a link or junction on the file or on `.pm-kit`;
  - text that is not UTF-8 or not JSON;
  - a key written twice;
  - any schema problem.

One bad entry refuses the whole file. A BOM is read.

## Schema 1

The top level holds:

- `schema`: exactly `1`.
- `operations`: a list, which may be empty.
- `never`: optional free text for the owner. The exclusions below are in code, not in this list.

Every object may carry notes: a key starting `_`, or `comment`. Any other unknown key refuses, and so does `null` for any key.

Each operation needs every key below except `undo`:

| Key | Value |
|---|---|
| `id` | `D-` and three or more digits, unique |
| `what` | what it does, in words |
| `repo` | a lower-case repo name, or `*` |
| `command` | the command (see below) |
| `shell` | `PowerShell` or `Bash`, the host tool that runs it |
| `args` | `[{name, pattern}]`: `name` is `[a-z][a-z0-9_]{0,30}`, unique, and used by a `{name}` token in the command or a precondition; `pattern` is a regex that compiles and matches none of `''`, `a b`, `a;b`, `a\|b`, `a&b`, `$x`, a line break or `a"b` |
| `preconditions` | a list of `{kind: file-exists, path}`, `{kind: branch-landed, repo_path (absolute), branch}` or `{kind: checklist-line, text}` |
| `reversible` | true or false |
| `undo` | how it is undone; required when `reversible` is true |
| `trigger` | a non-empty, unique list of `owner-session`, `hub-answer`, `standing:after-landing`, `standing:pm-start-checklist`. `hub-answer` needs `reversible` true: an irreversible operation waits for the owner at the machine |
| `elevated` | must be `false` |

### The command

The command is split on whitespace only. Each token is passed as it stands, so the following are refused:

- these characters anywhere: `` " ' ` ; | & < > $ % ( ) { } [ ] @ , * ? # `` and a line break or NUL;
- a `{name}` token that is not the whole token, or that names no declared argument;
- any `<...>` other than `<kit>`. `<kit>` is allowed as a whole token or as a `<kit>/...` prefix, and a runner fills it in. Precondition strings follow the same placeholder rules.

`check_arg(arg, value)` lists the problems of one argument value; an empty list means the value may be used. The value must pass all of these:

- fullmatch the owner's pattern;
- fullmatch `[A-Za-z0-9._:/\\-]{1,200}`;
- not start with `-`;
- hold no `..`;
- hit no hard exclusion.

## The hard exclusions

These are fixed in `kitlib/delegations/schema.py`, and the file cannot lift them. They are checked against every command token, every precondition token and every argument value. Tokens are compared in lower case, with `\` read as `/` and the `.exe` dropped from an executable's name. A runner must check the filled-in command line again with `exclusion_hits`.

| Id | What is refused |
|---|---|
| E1 elevation | `elevated: true`; `runas`, `sudo`, `gsudo`, `psexec`, `psexec64`, `schtasks`, `Register-ScheduledTask`, `New-ScheduledTaskPrincipal`, `Set-ScheduledTask`, `Set-ExecutionPolicy`, `/RL`; a prefix of `-Verb`, of `-RunLevel` (4 characters or more) or of `-Credential` (3 or more) |
| E2 settings, rules and kit control files | any token holding one of: `settings.json`, `settings.local.json`, `.claude.json`, `.claude/`, `.codex/`, `.pm-kit/`, `.claude-pm-kit/`, `CLAUDE.md`, `AGENTS.md`, `owner-delegations`, `.kit.json`, `content/roles`, `content/skills`, `.git/hooks`, `.git/config`, `hooksPath`, `machine_setup`, `render_content` |
| E3 the machine lock's owner controls | any token holding `machine_lock`, `machine-lock`, `lock_ui`, `lockmcp`, `lock_server`, `lock_set_` or `lock_hold` |
| E4 opaque code | `iex`, `Invoke-Expression`, `Invoke-Command`, `.`, `cmd`, `wsl`; `powershell`/`pwsh` without `-File`, or with `-Command`, `-EncodedCommand`, `-ec` (or a prefix of either) or a bare command before `-File`; `python` (`py`, `pythonw`, `python3`) with an option holding `c` or `m` before the script; `bash`, `sh`, `zsh`, `dash` with an option holding `c` |
| E5 launcher options the owner keeps | a prefix of `--no-agent`, `--prompt`, `--force`, `--replace` or `--pid` (with or without `=value`), because argparse accepts prefixes; `install-autostart`; `homes` |

A refusal names the operation, the field, the token and the exclusion. For example: `operations[3] (D-004).command: 'runas' is elevation (E1, never delegable)`.

## Not enforced here

These are not enforced by the loader:

- `repo`: whether the named repository exists.
- `shell`: whether the shell matches the command.
- `trigger`: when an operation may run.
- `preconditions`: whether they hold.

The runner owns all four.
