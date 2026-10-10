# The rulings ledger

One append-only file holds the owner's rulings for every repository on the machine. It is the file the machine config's `ledger_path` names (docs/kit-json.md "`~/.claude-pm-kit/config.json` keys"); no flag or environment variable names another. Entry point `tools/ledger.py`; logic `kitlib/ledger` (`read` the one reader, `fmt` the format, `write` the writes, `vcs` the git, `orphans` the orphan scan, `standing` the standing digest, `precommit` the opt-in hook); tests `tests/ledger`. Ported from the PM repository's writer.

## Format

Logic: `kitlib/ledger/fmt.py`.

```
## R-nnnn | YYYY-MM-DD HH:MMZ | <repo> | <prov>
Q: <the question as asked>
A: <the owner's answer>
Decides: <what it decides, one line>
Source: <where it was given>
Supersedes: R-nnnn | -
Items: A, B
```

- **Ids** are four or more digits, numbered from 1 in file order, one apart. The time is UTC.
- **Repo column:** `all`, or a repository's ledger name.
- **Provenance:** `typed`, `selected`, `relayed-from-terminal` (the owner's own answer) or `delegated`. A delegated entry's Source says `under R-nnnn`, naming an earlier owner entry.
- **Values:** a value's continuation lines start with two spaces. `Items` (backlog record ids) is optional and comes last.
- **Tombstone:** keeps its header, and its body is one line: `Retired: spent|superseded|duplicate - <why> (see R-nnnn|-)`.
- **Append-only:** an entry never changes once written, except that a full entry may become its own tombstone.

## CLI

```
python tools/ledger.py add --repo REPO --prov PROV --q Q --a A --decides D --source S
                           [--supersedes R-nnnn] [--items A,B] [--when 'YYYY-MM-DD HH:MMZ']
                           [--commit | --push] [--title T] [--branch-ok] [--model NAME]
python tools/ledger.py retire R-nnnn --class spent|superseded|duplicate --why WHY [--by R-nnnn] [--ruling R-nnnn]
python tools/ledger.py show R-nnnn
python tools/ledger.py grep TEXT
python tools/ledger.py check [--base REV] [--root DIR]
python tools/ledger.py list --prov PROV --since YYYY-MM-DD
python tools/ledger.py standing WORD
python tools/ledger_refs.py R-nnnn [R-nnnn ...] | --from-json FILE [FILE ...]
```

- **`add`** appends one entry and prints its id. It validates the whole ledger before and after writing, and writes the file atomically with LF line ends. A Decides over 400 characters (`fmt.DECIDES_NUDGE`) draws a warning on stderr; the entry is still written. `--when` backfills a time.
- **`retire`** turns an entry into its tombstone in place and prints its id. It is refused while a live entry's Decides or Items still names the entry. `--ruling` names the live entry whose Decides orders this retirement, and that entry's mention does not count.
- **`show`** prints one entry; `grep` prints the header of every entry holding the text, ignoring case. Either exits 1 when nothing is found.
- **`check`** validates the ledger and compares it with the ledger at `--base`: every older entry must be unchanged, or have become its own tombstone. The default base is `<repo.remote>/<main branch>` of the ledger's repository (`origin` unless its `.kit.json` sets `repo.remote`). A base that cannot be read prints `append-only NOT CHECKED` and validates only. It also fails on each standing-digest line that cites a retired or missing entry ("Standing digest").

  After the validation it runs the ruling-orphan scan (`kitlib/ledger/orphans.py`, wired by `kitlib.backlog.ledger.orphan_report`) over the repository at `--root` (default: the current directory). A live entry whose repo column is that repository's ledger name (`.kit.json` "backlog".ledger_repo, else "repo".name) or `all`, and whose `Items:` last line names a record that is open in the backlog store now and that no store commit closed or retired, prints `WARN R-nnnn: Items: ...`. A tombstone, a missing Items line and a malformed one name nobody and are not read. A WARN never changes the exit code. The scan prints `ruling orphans   SKIPPED - <why>` and the check goes on when `--root` is in no repository, its `.kit.json` or store cannot be read, the store does not exist, or the ledger holds no entry. The scan line is `ruling orphans   N Items line(s) read, M WARN`.
- **`list`** is the read-only digest feed (docs/engineering-rulings.md "Kit pieces").
- **`standing WORD`** prints the standing digest's rule lines for a topic word, under their headings. Exits 1 on no match or no digest.
- **Exit codes:** 0 ok; 1 no ledger configured, a ledger that cannot be read, a refused write, a failed check, nothing found, or a commit or push that failed after the entry was written; 2 usage.

## One reader

`kitlib/ledger/read.py` (`parse`, `read_entries`, `Entry`) is the only parser of the ledger's entries. `kitlib.ledger.entries` and `list_rulings` (the digest feed), `kitlib.backlog.ledger` (the backlog's ruling gates, which re-exports `read_entries` and `Entry`), `kitlib/rulingcites` (through `kitlib.backlog.ledger`) and the orphan scan all read through it. It reads a header leniently and says whether it is also in the writer's form (`Entry.strict`); the validator and the append-only check in `kitlib/ledger/fmt.py` work on the text and stay apart, since they judge the format instead of reading entries. A file is read as UTF-8 with a leading BOM dropped.

## Configuration

The keys are in `~/.claude-pm-kit/config.json`:

- `ledger_path` (absolute): the ledger file.
- `ledger_repos`: the repo columns `add --repo` accepts, besides `all`. A name outside the list is a usage error, and `check` reports an entry with one. Unset, any lower-case name is accepted.
- `ledger_refs`: where `tools/ledger_refs.py` searches ("ledger_refs").

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

A commit or push that fails after the write leaves the entry written and says what was not done.

## Citing rulings

A repository cites a ruling as `PM R-nnnn`, the form `tools/ruling_cites.py` reads (docs/repo-checks.md "ruling_cites"). The entry's repo column must be that repository's ledger name or `all`. Cite by id; never copy an entry's words into the repository.

The guard denies a direct edit of the ledger file (docs/guard.md, rule `ledger`); `add` and `retire` are the only writers.

## Standing digest

Logic: `kitlib/ledger/standing.py`.

`STANDING.md` beside the ledger file lists the rules still in force: one `- R-nnnn: <rule>` line each, under `##` (and deeper) headings for repository and topic. It is written by hand; the ledger tool only reads it. No digest is no finding.

- `check` fails on each line citing an id that is retired or not in the ledger.
- `standing WORD` matches, ignoring case, a rule line holding the word or sitting under a heading that holds it. Each heading prints once.

## ledger_refs

Entry point `tools/ledger_refs.py`; logic `kitlib/ledgerrefs`; tests `tests/ledgerrefs`. Run it before retiring entries, so no citation breaks silently.

It prints one block per id: `R-nnnn  refs=N  ledger-mentions=M`, then `  place:file:line` per hit. An id matches `R-nnnn` and `Rnnnn` not followed by a digit. `ledger-mentions` counts the ledger's lines naming the id, less its header.

The places are the machine config's `ledger_refs` (docs/kit-json.md):

- `repos`: name -> `{path, ref}`, searched with `git grep` at the committed ref.
- `folders`: name -> a folder or file, searched on disk (`.md .py .ps1 .json .txt .cs .java .yml`).

Unset, only the ledger is counted. A repository git cannot search, or a missing folder, prints `  ERROR ...` and exits 1; the other hits still print. Exit codes: 0 ok; 1 an error or a broken config; 2 usage.

## Pre-commit check

Hook `githooks/pre-commit`; dispatch `kitlib/githooks/precommit.py`; ledger step `kitlib/ledger/precommit.py`; tests `tests/ledger/test_precommit.py` and `tests/githooks/test_precommit.py`.

Opt-in: a repository sets `"ledger": {"pre_commit_check": true}` in `.kit.json` itself, not in a section file, and `git config core.hooksPath <kit>/githooks`. The hook then judges each commit that adds, changes or deletes the ledger file the machine config's `ledger_path` names, or the standing digest beside it: it runs `tools/ledger.py check --base HEAD --root <repo>` and refuses the commit when that fails. Any other commit passes after one `git diff --cached` (the index the commit is built from, so `commit -a` and `commit -- <paths>` count). The check reads the working files of both, so such a commit is refused before it runs when either the ledger or the digest has unstaged changes, whichever of them is staged.

`githooks/pre-commit.py` makes one call, `kitlib.githooks.precommit.main`, which runs the kit's checks (`CHECKS`, today the ledger step only) in order and stops at the first refusal. Nothing there runs a repository's own `hooks/pre-commit`; a later step would be one more entry in `CHECKS` (docs/pm-data-branch.md "Decided by the owner").

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

- **Cost when not opted in:** `githooks/pre-commit` is a `sh` front end using builtins only. With no `.kit.json`, or one whose text never names `pre_commit_check`, it exits 0 and starts no other process. Otherwise it runs `githooks/pre-commit.py`.

## Not ported

- **The PM writer's default co-author environment variable:** replaced by `--model`, else `CLAUDE_AGENT_MODEL`, the variable `tools/commit.py` reads.
- **The PM writer's fixed list of repository names:** replaced by `ledger_repos`.
- **The PM `ledger_refs.py`'s fixed repositories, folders and their environment variables:** replaced by `ledger_refs`.
