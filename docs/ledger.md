# The rulings ledger

The commit/push options take `--host NAME`: the agent host for the Co-Authored-By trailer, a host in `hosts.json`, default `PM_KIT_HOST`, else the registry's default host (Claude); `--provider` is its old name, accepted for one release. The trailer comes from `kitlib.attribution` (docs/commit.md); a Codex trailer needs the actual explicit --model id. The host and model are checked before anything is written; the provenance, branch, path and owner checks keep their rules.

One append-only file holds the owner's rulings for every repository on the machine. It is the file the machine config's `ledger_path` names (docs/kit-json.md "Machine config keys"); no flag or environment variable names another. Entry point `tools/ledger.py`; logic `kitlib/ledger` (`read` the one reader, `fmt` the format, `write` the writes, `vcs` the git, `standing` the standing digest, `precommit` the opt-in hook); tests `tests/ledger`. Ported from the PM repository's writer.

## Format

Logic: `kitlib/ledger/fmt.py`.

```
## R-nnnn | YYYY-MM-DD HH:MMZ | <repo> | <prov>
Q: <the question as asked>
A: <the owner's answer>
Decides: <what it decides, one line>
Source: <where it was given>
Supersedes: R-nnnn | -
```

- **Ids** are four or more digits, numbered from 1 in file order, one apart. The time is UTC.
- **Repo column:** `all`, or a name from the machine config's `ledger_repos` (unset: any lower-case name; "Configuration").
- **Provenance:** `typed`, `selected`, `relayed-from-terminal` (the owner's own answer) or `delegated`. A delegated entry's Source says `under R-nnnn`, naming an earlier owner entry.
- **Values:** a value's continuation lines start with two spaces. An older entry may end with an `Items: A, B` line. It is still read and validated; `add` no longer writes one.
- **Tombstone:** keeps its header, and its body is one line: `Retired: spent|superseded|duplicate - <why> (see R-nnnn|-)`.
- **Append-only:** an entry never changes once written, except that a full entry may become its own tombstone.

## CLI

```
python tools/ledger.py add --repo REPO --prov PROV --q Q --a A --decides D --source S
                           [--supersedes R-nnnn] [--when 'YYYY-MM-DD HH:MMZ']
                           [--commit | --push] [--title T] [--branch-ok] [--model NAME]
python tools/ledger.py retire R-nnnn --class spent|superseded|duplicate --why WHY [--by R-nnnn] [--ruling R-nnnn]
python tools/ledger.py show R-nnnn
python tools/ledger.py grep TEXT
python tools/ledger.py check [--base REV]
python tools/ledger.py list --prov PROV --since YYYY-MM-DD
python tools/ledger.py standing WORD
```

- **`add`** appends one entry and prints its id. It validates the whole ledger before and after writing, and writes the file atomically with LF line ends. A Decides over 400 characters (`fmt.DECIDES_NUDGE`) draws a warning on stderr; the entry is still written. `--when` backfills a time.
- **`retire`** turns an entry into its tombstone in place and prints its id. It is refused while a live entry's Decides or Items still names the entry. `--ruling` names the live entry whose Decides orders this retirement, and that entry's mention does not count.
- **`show`** prints one entry; `grep` prints the header of every entry holding the text, ignoring case. Either exits 1 when nothing is found.
- **`check`** validates the ledger and compares it with the ledger at `--base`: every older entry must be unchanged, or have become its own tombstone. The default base is `<repo.remote>/<main branch>` of the ledger's repository (`origin` unless its `.kit.json` sets `repo.remote`). A base that cannot be read prints `append-only NOT CHECKED` and validates only. It also fails on each standing-digest line that cites a retired or missing entry ("Standing digest").
- **`list`** is the read-only digest feed (docs/engineering-rulings.md "Kit pieces").
- **`standing WORD`** prints the standing digest's rule lines for a topic word, under their headings. Exits 1 on no match or no digest.
- **Exit codes:** 0 ok; 1 no ledger configured, a ledger that cannot be read, a refused write, a failed check, nothing found, or a commit or push that failed after the entry was written; 2 usage.

## One reader

`kitlib/ledger/read.py` (`parse`, `read_entries`, `Entry`) is the only parser of the ledger's entries. `kitlib.ledger.entries` and `list_rulings` (the digest feed) and `kitlib/ledger/standing.py` (`check`'s standing-digest check) read through it; `kitlib.backlog.ledger` re-exports `read_entries` and `Entry`, and no backlog write or `check` reads the ledger. It reads a header leniently and says whether it is also in the writer's form (`Entry.strict`); the validator and the append-only check in `kitlib/ledger/fmt.py` work on the text and stay apart, since they judge the format instead of reading entries. A file is read as UTF-8 with a leading BOM dropped.

## Path checks

Before the ledger, or the standing digest beside it, is read or written, `kitlib/ledger/safepath.py` (`refusal`) checks its path. This is defence in depth beside the guard, which refuses only a direct edit of the ledger file. It is not a lock: the check and the open are separate calls, so a swap made between them is not caught. Three checks:

- **Reparse points.** The file and every existing folder above it, except the drive root, must not be a reparse point (a junction, a symlink or any other tag; `os.lstat` `st_file_attributes`).
- **Hard links.** The file must have one link (`st_nlink`). Through another name, an edit would reach the ledger by a path the guard does not watch. The writer's atomic replace leaves one link. Only a link present when the check runs is seen.
- **Owner.** The file and its own folder, where they exist, must be owned by this process's user, Administrators or SYSTEM (`GetNamedSecurityInfoW` through ctypes). An owner that cannot be read is refused.

A part that does not exist yet is skipped, so the first `add` can still create the ledger. A refused path is never opened, and the refusal names the fix:

- `kitlib/ledger/read.py` `read_file` returns the refusal as its reason, and a consumer treats it as an unreadable ledger.
- `kitlib/ledger/write.py` raises `Refused`, so every `tools/ledger.py` verb exits 1.

Cases that lock a ledger out, and the fix for each:

- A FAT or exFAT volume has no owner to read, and a network share may report an owner that is not you. Keep the ledger on a local NTFS disk.
- A folder copied or restored from elsewhere can keep a foreign owner. Run `takeown /f <path>`, or `icacls <path> /setowner <you>`.
- Mount points and folders relocated through a junction are reparse points: point `ledger_path` at the real target. OneDrive placeholders are too: move the ledger off OneDrive.
- A hard-linked ledger: remove the other names (`fsutil hardlink list <path>`), or copy the file to a new one.

Tests: `tests/ledger/test_safepath.py`.

## Configuration

The keys are in the machine config (docs/kit-json.md "Machine config keys"):

- `ledger_path` (absolute): the ledger file.
- `ledger_repos`: the repo columns `add --repo` accepts, besides `all`. A name outside the list is a usage error, and `check` reports an entry with one. Unset, any lower-case name is accepted.

## A new ledger

Set `ledger_path`; the first `add` creates the file from `templates/common/LEDGER.md`. To commit the ledger, put it inside a git work tree whose `.kit.json` names the main branch.

## Commit and push

Logic: `kitlib/ledger/vcs.py`.

`add --commit` commits the ledger file alone (`git commit -- <file>`). Other staged files stay staged and out of the commit.

- **Subject:** `ledger: R-nnnn <title>`. The title defaults to the start of Decides, cut to 60 characters.
- **Trailer:** the kit's Co-Authored-By trailer (`kitlib.commit.coauthor`) for `--model`, else `CLAUDE_AGENT_MODEL`. Neither set is a usage error, and nothing is written.
- **`--push`** also pushes the current branch to the ledger repository's `repo.remote` (`origin` by default).

Before writing, `add --commit` refuses in any of these cases:

- the ledger is in no git work tree;
- that repository's `.kit.json` is broken;
- HEAD is detached;
- the branch is not that repository's `"repo".main_branch` and `--branch-ok` is not given;
- the ledger file has uncommitted changes.

A commit that fails because git's `index.lock` is busy (another git process in that repository) is retried, add and commit together, for `INDEX_LOCK_WAIT` seconds (3) in `kitlib/ledger/vcs.py` `commit_one`; any other failure is not retried, and after the bound the refusal says `index.lock busy`. A commit or push that fails after the write leaves the entry written and says what was not done.

## Citing rulings

The ledger is the PM's private record. No repository, tool or text cites its ids; the PM relays a decision in words.

The guard denies a direct edit of the ledger file (docs/guard.md, rule `ledger`); `add` and `retire` are the only writers.

## Standing digest

Logic: `kitlib/ledger/standing.py`.

`STANDING.md` beside the ledger file lists the rules still in force: one `- R-nnnn: <rule>` line each, under `##` (and deeper) headings for repository and topic. It is written by hand; the ledger tool only reads it. No digest is no finding.

- `check` fails on each line citing an id that is retired or not in the ledger.
- `standing WORD` matches, ignoring case, a rule line holding the word or sitting under a heading that holds it. Each heading prints once.

## Pre-commit check

Hook `githooks/pre-commit`; dispatch `kitlib/githooks/precommit.py`; ledger step `kitlib/ledger/precommit.py`; tests `tests/ledger/test_precommit.py` and `tests/githooks/test_precommit.py`.

Opt-in: a repository sets `"ledger": {"pre_commit_check": true}` in `.kit.json` itself, not in a section file, and `git config core.hooksPath <kit>/githooks`. The hook then judges each commit that adds, changes or deletes the ledger file the machine config's `ledger_path` names, or the standing digest beside it: it runs `tools/ledger.py check --base HEAD` and refuses the commit when that fails. Any other commit passes after one `git diff --cached` (the index the commit is built from, so `commit -a` and `commit -- <paths>` count). The check reads the working files of both, so such a commit is refused before it runs when either the ledger or the digest has unstaged changes, whichever of them is staged.

`githooks/pre-commit.py` makes one call, `kitlib.githooks.precommit.main`, which runs the kit's checks (`CHECKS`: the render step, `kitlib.githooks.render`, which acts only in a kit checkout with a render source staged (docs/content.md "Render on commit"), then the ledger step) in order and stops at the first refusal. Nothing there runs a repository's own `hooks/pre-commit`; a later step would be one more entry in `CHECKS` (docs/pm-data-branch.md "Decided by the owner").

| Case | Result |
|---|---|
| `.kit.json` absent, or its text never names `pre_commit_check` | pass; no python started (below) |
| `.kit.json` sets `"pre_commit_check": false` | pass; python starts, loads the config, no check |
| Opted in, ledger and digest not in the commit | pass; no check |
| Ledger or digest in the commit, check passes | pass |
| Ledger or digest in the commit, check fails | refused |
| Ledger or digest in the commit, and either of them has unstaged changes | refused |
| `.kit.json` names `pre_commit_check` but cannot be loaded | refused |
| `.kit.json` cannot be loaded and does not name it | pass with a note |
| Opted in, machine config unreadable or no `ledger_path` | refused |
| Git cannot list the staged files, the check cannot start, a step raises, or the kit code cannot be imported | refused |
| No python on PATH | pass with a note (the front end cannot tell what the commit holds) |

- **Cost when not opted in:** `githooks/pre-commit` is a `sh` front end using builtins only. With no `.kit.json`, or one whose text never names `pre_commit_check`, it exits 0 and starts no other process, except in a kit checkout (`content/roles/` and `tools/render_content.py`), where one `git diff --cached` tells whether a render source is staged. Otherwise it runs `githooks/pre-commit.py`. With no python on PATH and a render source staged it refuses the commit with the render command.

## Not ported

- **The PM writer's default co-author environment variable:** replaced by `--model`, else `CLAUDE_AGENT_MODEL`, the variable `tools/commit.py` reads.
- **The PM writer's fixed list of repository names:** replaced by `ledger_repos`.
