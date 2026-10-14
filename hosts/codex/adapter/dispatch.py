"""One kit tool run as literal argv for a Codex role, with the role and workflow prechecks the command
broker applies before anything runs (from the Codex branch's kit_adapter.py; runtime.py runs the
plan inside the native sandbox).

  plan(tool, args, cwd, role, kit, review_verdict) -> ToolPlan(argv, cwd, role)
      `python <kit>/tools/<tool>.py <args>` for a tool in TOOLS, refused when the role's mode does
      not run it (a read, test or lander role runs reads; tests for a test role; the landing for
      the lander with the coordinator's verdict and full hashes), when an argument names an owner
      flag, a checkout other than cwd or an abbreviated option, or a lock verb an agent does not
      run. A commit, a sync or a ledger or pending write needs an explicit --model, the actual
      model id (Codex's attribution has no fallback: hosts.json). The host is not inserted: the
      run's environment carries PM_KIT_HOST=codex, which every shared tool reads; a --host or
      --provider naming another host is refused.
  execute(plan)              runs it here, in this session's environment (lock token kept)
  execute_in_host(plan, host)   runs it through a CommandHost (runtime.SandboxCommandHost)
Landing execution is refused in both until it is reviewed.
"""
from dataclasses import dataclass
from pathlib import Path
import re
import sys

from .kit import HOST, catalog, child_environment, current_role, discover, load_kit, trailer


TOOLS = frozenset(("preflight", "board", "backlog", "gate", "land", "landed", "review_pack",
                   "machine_lock", "worktree_paths", "new_worktree", "worktree_remove",
                   "worktree_sweep_task", "lock_release_check", "commit", "sync_base", "ledger",
                   "pending", "doc_budget", "doc_index", "codemap", "pieces",
                   "forbidden", "trailers", "machine_paths", "checkout_guard",
                   "agent_roster", "pytests", "testlist", "tool_deps", "redfirst", "daily_full"))
READ_TOOLS = {"preflight", "board", "landed", "review_pack", "worktree_paths", "lock_release_check",
              "doc_index", "codemap", "trailers", "machine_paths",
              "checkout_guard", "agent_roster", "testlist", "tool_deps"}
READ_BACKLOG = {"next", "show", "list", "check", "landable", "stale"}
READ_LEDGER = {"show", "grep", "check", "list", "standing"}
OWNER_FLAGS = {"--owner-authored", "--owner-approved", "--owner-confirmed", "--owner-said"}


@dataclass(frozen=True)
class ToolPlan:
    argv: tuple
    cwd: str
    role: str


def _flag_values(args, name):
    values = []
    for i, arg in enumerate(args):
        if arg == name:
            if i + 1 == len(args):
                raise ValueError("missing value for " + name)
            values.append(args[i + 1])
        elif arg.startswith(name + "="):
            values.append(arg.split("=", 1)[1])
    return values


def _flag(args, name):
    values = _flag_values(args, name)
    if len(values) > 1:
        raise ValueError("duplicate workflow flag: " + name)
    return values[0] if values else None


def _workflow(tool, args, role, review_verdict):
    for arg in args:
        if arg == "--":
            break
        if arg.startswith("--"):
            key = arg.split("=", 1)[0]
            # argparse can accept abbreviated options; do not let a spelling grant an owner flag.
            if any(flag.startswith(key) for flag in OWNER_FLAGS):
                raise ValueError("owner flags are unavailable to Codex roles")
            repo_label = key == "--repo" and tool in {"machine_lock", "ledger"}
            if not repo_label and any(flag.startswith(key) for flag in ("--root", "--repo-path", "--directory")):
                raise ValueError("select the checkout with --cwd, not a tool target override")
        if arg.startswith("-C"):
            raise ValueError("select the checkout with --cwd")
    if tool in {"agent_roster", "doc_index", "doc_budget", "machine_paths", "forbidden", "pieces"}:
        if any(flag.startswith(a.split("=", 1)[0]) for a in args if a.startswith("--") and a != "--"
               for flag in ("--write", "--fix", "--add", "--remove")):
            raise ValueError("shared configuration/tool edits require a separate reviewed owner change")
    mode = catalog()[role]["mode"]
    landing = (tool == "land" and args != ("--queue",)) or (tool == "gate" and bool(args) and args[0] == "land")
    if tool == "machine_lock":
        verb = args[0] if args else ""
        forced = any("--force".startswith(a.split("=", 1)[0]) for a in args if a.startswith("--") and a != "--")
        if verb not in {"status", "stats", "run", "background", "stop"} or forced:
            raise ValueError("owner lock changes are unavailable; use the existing owner workflow")
        if mode in {"read", "lander"} and verb not in {"status", "stats"}:
            raise ValueError("read-only role cannot launch or stop jobs")
    if tool == "ledger" and (not args or args[0] not in READ_LEDGER) and role != "pm":
        raise ValueError("only the PM workflow records supplied rulings")
    if tool == "pending" and args != ("list",) and role != "pm":
        raise ValueError("only the PM workflow changes the owner's pending steps")
    if mode in {"read", "lander", "test"}:
        reading = tool in READ_TOOLS or (tool == "backlog" and _backlog_verb(args) in READ_BACKLOG) or \
                  (tool == "ledger" and bool(args) and args[0] in READ_LEDGER) or \
                  (tool == "machine_lock" and bool(args) and args[0] in {"status", "stats"}) or \
                  (tool == "land" and args == ("--queue",))
        reading = reading or (tool == "pending" and args == ("list",))
        testing = mode == "test" and (tool in {"pytests", "redfirst"} or
                  (tool == "gate" and not landing) or
                  (tool == "machine_lock" and bool(args) and args[0] in {"run", "background", "stop"}))
        if not reading and not testing and not (mode == "lander" and landing):
            raise ValueError("read-only role cannot run this operation")
    if landing:
        for arg in args:
            key = arg.split("=", 1)[0]
            if key.startswith("--") and any(flag.startswith(key) and key != flag
                                              for flag in ("--expect-main", "--expect-tip")):
                raise ValueError("use full expected-hash option names")
        if role != "lander" or review_verdict not in {"LAND", "LAND WITH FOLLOW-UP"}:
            raise ValueError("landing requires a dedicated lander workflow and coordinator review verdict")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", _flag(args, "--expect-main") or ""):
            raise ValueError("landing requires a full hash for --expect-main")
        tips = _flag_values(args, "--expect-tip")
        if tool == "gate":
            _flag(args, "--expect-tip")       # gate has one scalar tip, land has an append option
        if not tips or any(not re.fullmatch(r"[0-9a-fA-F]{40}", tip) for tip in tips):
            raise ValueError("landing requires a full hash for every --expect-tip")


def _backlog_verb(args):
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--no-fetch" or arg.startswith(("--session=", "--as=")):
            index += 1
        elif arg in {"--session", "--as"}:
            if index + 1 >= len(args):
                raise ValueError("missing backlog global argument")
            index += 2
        elif arg.startswith("-"):
            raise ValueError("use full documented backlog global options")
        else:
            return arg
    return ""


def _host_flags(args):
    """Refuse a --host or --provider (or an abbreviation of one) naming another host."""
    for name in ("--host", "--provider"):
        keys = [arg.split("=", 1)[0] for arg in args if arg.startswith("--") and arg != "--"]
        if any(key != name and len(key) >= 4 and name.startswith(key) for key in keys):
            raise ValueError("use the full %s option name" % name)
        value = _flag(args, name)
        if value is not None and value != HOST:
            raise ValueError("a Codex role runs kit tools as %s; %s %s is refused" % (HOST, name, value))


def _provider_args(tool, args):
    if tool in {"ledger", "pending"}:
        for arg in args:
            key = arg.split("=", 1)[0]
            if key.startswith("--") and key != "--" and any(
                    flag.startswith(key) and flag != key for flag in ("--commit", "--push")):
                raise ValueError("use full ledger --commit/--push option names")
    committing_ledger = tool in {"ledger", "pending"} and any(a in {"--commit", "--push"} for a in args)
    _host_flags(args)
    if tool not in {"commit", "sync_base"} and not committing_ledger:
        return args
    trailer(_flag(args, "--model"))  # the explicit active model; never an inherited identity
    if committing_ledger:
        verbs = {"add"} if tool == "ledger" else {"add", "edit", "done"}
        if not args or args[0] not in verbs:
            raise ValueError("commit/push dispatch requires a documented write verb and full option names")
    return args


def plan(tool, args, cwd, role=None, profile=None, review_verdict=None):
    profile = profile or discover()
    load_kit(profile)
    role = role or current_role()
    if role not in catalog():
        raise ValueError("unknown kit role")
    if tool not in TOOLS or not isinstance(args, (tuple, list)) or any(not isinstance(a, str) or "\x00" in a for a in args):
        raise ValueError("unknown tool or invalid arguments")
    cwd = str(Path(cwd).resolve())
    if not Path(cwd).is_dir():
        raise ValueError("checkout directory does not exist")
    args = tuple(args)
    _workflow(tool, args, role, review_verdict)
    args = _provider_args(tool, args)
    entry = profile.kit_root / "tools" / (tool + ".py")
    if not entry.is_file():
        raise ValueError("tool is absent from this shared kit: " + tool)
    return ToolPlan((sys.executable, str(entry), *args), cwd, role)


def execute(planned, runner=None):
    """Keep shared CLI output/exit semantics. No shell or library patching is involved."""
    entry, args = Path(planned.argv[1]).stem, planned.argv[2:]
    if (entry == "land" and args != ("--queue",)) or (entry == "gate" and args[:1] == ("land",)):
        raise ValueError("landing execution is disabled pending reviewed host authorization and sandbox design")
    from kitlib.proc import spawn
    run = runner or spawn.run
    import os
    # This is the same session's next tool, not a new agent. The shared lock verifies
    # nesting against the live ancestor; dropping its token can self-deadlock gate.
    env = child_environment(os.environ, {"PM_KIT_HOST": HOST}, preserve_lock=True)
    return run(list(planned.argv), cwd=planned.cwd, env=env, shell=False).returncode


def execute_in_host(planned, host):
    """Send an existing kit operation through a runtime-bound sandboxed CommandHost."""
    if host is None or not callable(getattr(host, "run_argv", None)):
        raise ValueError("a runtime-bound sandboxed CommandHost is required")
    tool, args = Path(planned.argv[1]).stem, planned.argv[2:]
    if (tool == "land" and args != ("--queue",)) or (tool == "gate" and args[:1] == ("land",)):
        raise ValueError("landing execution is disabled pending reviewed host authorization")
    checked = plan(tool, args, planned.cwd, role=planned.role)
    return host.run_argv(checked.argv, cwd=checked.cwd, stdin_closed=True)
