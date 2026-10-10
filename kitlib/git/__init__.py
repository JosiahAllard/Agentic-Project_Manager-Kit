"""kitlib.git: repository, worktree and main-checkout detection, safe git invocation, and
branch and ancestry queries (ARCHITECTURE.md).

  cmd    call/run/run_text: every git child, environment scrubbed, timeout always set
  repo   classify (MAIN/LINKED/NONE/UNKNOWN), common_dir, worktrees, toplevel, check_write
  refs   resolve, tree_of, current_branch, is_ancestor, is_fast_forward, merge_in_progress, status,
         default_base (the one default-base rule)
  batch  one git process for many reads: cat_file, commits, ancestry, CatFile, fixture_env
  cache  a test process's memo of a full hash's resolve (off unless enable()); clear()
"""
from .cmd import (  # noqa: F401
    GIT_TIMEOUT, NETWORK_ENV, SCRUB_ENV, Result, call, run, run_text, scrubbed_env, text,
)
from .repo import (  # noqa: F401
    LINKED, MAIN, NONE, UNKNOWN, Place, Worktree, WriteRefused, check_write,
    classify, common_dir, existing_dir, git_path, inside, is_main_checkout, linked_worktrees,
    main_checkout_toplevel, norm, real, same_path, same_repository, toplevel, worktrees,
)
from .refs import (  # noqa: F401
    bad_rev, branch_tip, current_branch, default_base, head, is_ancestor, is_clean,
    is_fast_forward, is_sha, ls_remote, merge_base, merge_in_progress, parse_porcelain_z, remote_tip, resolve, status,
    tree_of,
)
