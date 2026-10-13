# commit.py and machine_paths.py

Two tools, both ported from the PM kit (PM 9813c73). `tools/commit.py` is the entry over `kitlib/commit`. `tools/machine_paths.py` is the entry over `kitlib/machinepaths`. Each library's module docstring holds the full rules, and this page holds the contract.

## commit.py

`commit.py [-C DIR] -m SUBJECT [-b BODY | --body-file FILE] [--model NAME] [--host NAME] [--owner-authored] [--owner-approved VALUE ...] [--lander-main-checkout] PATH ...`

`--host NAME` names the agent host for the Co-Authored-By trailer: a host in `hosts.json`, default `PM_KIT_HOST`, else the registry's default host (Claude). `--provider` is its old name, accepted for one release. A name `hosts.json` lacks is a usage error (exit 2) before any work. The trailer comes from `kitlib.attribution`: Claude's is `Claude <model>` as below; Codex's needs the actual explicit model id, emits `Codex <model-id> <noreply@openai.com>` and never uses CLAUDE_AGENT_MODEL. Explicit paths, owner identity/approval, staged-file and document checks keep their rules. The shared trailer classifier recognizes both forms.

It stages exactly the PATHs named and commits them in the work tree that DIR is in (default: here). `-C` is resolved by `kitlib.git.repo.toplevel`.
- A path is relative to that work tree's root, or absolute inside it.
- `./` and `..` are folded before the owner-only check.
- A relative `--body-file` is relative to the caller's directory.
- No flag may be abbreviated.

What it refuses (`kitlib/commit/__init__.py`, `_commit`):
- **Explicit paths only.** A directory, `.`, `-A` or a glob is a usage error. A path outside the work tree is a usage error too.
- **Foreign staged files.** Anything already staged and not named is refused, because it may belong to another session.
  - With `"commit".merge_fold` on, during a merge with one MERGE_HEAD, the files the merge staged itself are exempt (`merge_owned`).
  - An owner-only file is exempt only when it is the merged branch's blob byte for byte.
- **Machine paths.** A machine path in the subject or body is refused (`kitlib.machinepaths.find`), unless the commit is `--owner-authored`.
  - A machine path in the staged copy of any file being committed is refused too, under the repository's own `"machine_paths"` config. There is no owner exemption here. This check fails closed.
  - Either refusal unstages what this call staged.
- **Owner-only paths** (`"owner"`, `kitlib/config/owner.py`) are committed only by the owner, with `--owner-authored`.
  - `--owner-authored` requires the author identity git will record (`git var GIT_AUTHOR_IDENT`) to be `"owner".email`. It must not be in `"owner".agent_emails`.
  - `--owner-approved` is required only when an owner-only path is in the commit.
  - Every `--owner-approved` value must be a blanket entry or a committed path. It is written in its canonical spelling under `"owner".trailer`.
- **The main checkout** (`kitlib.git.checkout.main_refusal`), unless `--lander-main-checkout` is given. That flag is a usage error anywhere else.

The message is the subject, then the body, then the trailers:
- The trailers are the owner-approval lines, then `Co-Authored-By: Claude <model> <noreply@anthropic.com>`.
- An `--owner-authored` commit carries `Owner-Authored: yes` instead of `Co-Authored-By`.
- The model comes from `--model`, else `CLAUDE_AGENT_MODEL`. A blank value counts as unset.
- `"Claude "` is added unless the name is a `"commit".coauthor_families` family, or starts with one followed by a space, `.` or `-`.
- The body comes from `-b TEXT`, where a typed `\n` becomes a newline (`~~` also becomes a newline; for a literal `~~` use --body-file), or from `--body-file FILE`, read as UTF-8 with CRLF changed to LF.
  - `-m` takes the subject once; a second `-m` is a usage error (exit 2) pointing at `-b` / `--body-file`, and nothing is committed.
  - A `-b` whose whole value names an existing file, or looks like an absolute path, is a usage error.

After a commit that includes a `.md` file or an owner-only file, it prints the doc-budget verdict.
- The kit's `tools/doc_budget.py -C <root>` produces it, and `kitlib.verdict` judges it.
- A `doc index` verdict follows (`kitlib.commit.doc_index_check`: `tools/doc_index.py -C <root>`, judged on `DOC INDEX OK` / `DOC INDEX FAILED`). Either failing prints the follow-up-commit notice. `.kit.json` "doc_index".enabled false prints nothing.
- With no such tool, the verdict is `docs      NOT CHECKED - ...`.
- A failing verdict leaves the commit in place. It only asks for a follow-up commit.

| Code | Meaning |
|---|---|
| 0 | committed (`committed <hash>  <subject>`, `files     ...`) |
| 1 | refused (`refused: ...`), or git failed (`git add failed`, `commit failed`, `nothing to commit in`) |
| 2 | `USAGE: ...`, the main-checkout refusal, or a refused `.kit.json` |

## machine_paths.py

`machine_paths.py [-C DIR]` searches every tracked file, and every untracked file git does not ignore, in the work tree. It looks for a drive-rooted `GitHub`, `research` or `AgentTemp` folder, or a drive-rooted user profile. The `kitlib/machinepaths` docstring gives the pattern and the allow-list rules.

| Code | Meaning |
|---|---|
| 0 | `MACHINE PATHS OK (...)`; WARNING lines (a skipped file, a stale allow entry) do not fail |
| 1 | `MACHINE PATHS FAILED: ...`, a hit off the allow-list |
| 2 | `MACHINE PATHS NOT CHECKED: ...`: git could not list the tree, nothing was read, the config was refused, or `-C` is not in a work tree; or `USAGE:` |

## `.kit.json` keys read

docs/kit-json.md lists every key with its default.
- `commit.merge_fold` and `commit.coauthor_families` are read by commit.py.
- `owner.*` (paths, paths_except, email, agent_emails, trailer) is read by commit.py. It comes from the working tree's `.kit.json`.
- `checkout_guard.rule_pointer` is the refusal's pointer to where the rule is written down.
- `repo.main_branch` is named in the `--lander-main-checkout` usage error.
- `machine_paths.*` (allow, fixtures, known_binary, scan, fallback_vars) is read by both tools. commit.py reads it through `scan_staged`.

## Not ported

Each item below was dropped on purpose (port brief).
- **Backlog claim folding.** The kit backlog has no claim queue.
- **The `-C` hand-off** (chdir to main, `KIT_CALLER_CWD`, a glued `-Cdir`, `-C` given twice), `KIT_REPO_ROOT`, `KIT_CONFIG` and `_repo_guard`. Here `-C` is a plain option, and argparse takes the last value given.
- **machine_paths built-in allow entries.** These were the PM kit's `_paths.py` fallbacks, its `test_paths.py` table, the kit stubs and backlog.py's ledger. They named the older kit's files and one drive. A repository that relied on them needs explicit `allow` or `fixtures` entries.
- **`machine_paths.piece_fixtures`.** kitlib has no pieces-table resolver, so a non-empty list is refused rather than ignored. Use `fixtures` with the file's glob.
- **The doc_budget PowerShell twin.** Only `tools/doc_budget.py` is run.
