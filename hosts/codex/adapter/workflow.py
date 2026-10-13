"""Local task acceptance over authoritative kit stores and existing tool entry points (from the
Codex branch's local_workflow.py).

The controller binds separate tool-only hosts and performs edits through its trusted
runtime. This module neither launches agents nor stores a second task ledger. A
review pack is a handoff, never an independent review verdict or landing permission.
"""
from dataclasses import dataclass
from pathlib import Path
import json

from . import dispatch as kit_adapter
from .kit import HOST, discover, load_kit, trailer


@dataclass(frozen=True)
class Task:
    coordinator: str
    worker: str
    branch: str
    item: str
    session: str
    model: str
    base: str
    paths: tuple
    profile: object
    main: str = ''
    creation_config: str = ''


def _creation_config(cfg):
    """The main-checkout settings consumed by shared worktree creation."""
    return json.dumps({'repo': cfg.repo, 'landing': cfg.section('landing')}, sort_keys=True)


class Stopped(RuntimeError):
    """Do not retry an uncertain mutation: inspect kit/Git facts first."""

    def __init__(self, stage, detail):
        self.stage = stage
        super().__init__(stage + ': ' + str(detail) +
                         '; inspect authoritative state before retrying')


def _git(args, cwd):
    from kitlib.git import cmd
    value, why = cmd.run_text(list(args), cwd)
    if value is None:
        raise ValueError('Git could not answer: ' + str(why))
    return value.strip()


def _head(cwd):
    from kitlib.git import refs
    value, why = refs.head(cwd)
    if not value:
        raise ValueError('HEAD could not be read: ' + str(why))
    return value


def plan(coordinator, branch, item, session, model, paths, *, profile=None):
    """Read-only plan. Exact base and destination come from the current kit/Git."""
    profile = profile or discover()
    load_kit(profile)
    from kitlib.git import repo, refs
    from kitlib.worktree import new
    from kitlib.backlog.opener import open_repo
    from kitlib.backlog.errors import Refusal
    cwd = str(Path(coordinator).resolve())
    place = repo.classify(cwd)
    if place.kind != repo.LINKED or not repo.same_path(place.top, cwd):
        raise ValueError('a linked coordinator checkout root is required')
    if not new.branch_ok(branch, False) or not branch.startswith(HOST + '/'):
        raise ValueError('a new %s/ branch is required' % HOST)
    try:
        _, cfg, rules = open_repo(cwd)
    except Refusal as exc:
        raise ValueError(str(exc)) from exc
    if rules.id_error(item):
        raise ValueError('invalid backlog item')
    if not isinstance(session, str) or not session.strip() or any(c in session for c in '\r\n\0'):
        raise ValueError('an explicit session is required')
    trailer(model)
    paths = tuple(paths)
    if not paths or any(not isinstance(p, str) or not p or p.startswith('-') or
                        '\\' in p or ':' in p or Path(p).is_absolute() or
                        any(part in {'', '.', '..', '.git'} for part in p.split('/'))
                        for p in paths):
        raise ValueError('explicit relative file paths are required')
    main = repo.main_checkout_toplevel(cwd, place)
    if not main:
        raise ValueError('main checkout could not be read')
    from kitlib.config import ConfigError, load
    from kitlib.worktree.hostdirs import folder as worktrees_directory
    try:
        cfg = load(main)
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc
    base, why = refs.resolve(cfg.main_branch, main)
    if not base:
        raise ValueError('base could not be read: ' + str(why))
    destination = worktrees_directory(cfg, HOST)
    worker = str((Path(main) / destination / branch.split('/', 1)[1]).resolve())
    return Task(cwd, worker, branch, item, session, model, base, paths, profile,
                str(Path(main).resolve()), _creation_config(cfg))


def _run(task, tool, args, role, cwd, host):
    """The normal dispatcher checks arguments; only the supplied runtime executes."""
    try:
        from kitlib.git import repo
        from .runtime import BoundKitHost
        if not isinstance(host, BoundKitHost):
            raise ValueError('a controller-owned fresh-bound kit executor is required')
        binding = getattr(host, 'binding', None)
        if binding is None or binding.role != role or not repo.same_path(binding.cwd, cwd):
            raise ValueError('kit host must be bound to the requested role and checkout')
        if tool == 'commit' and binding.model != task.model:
            raise ValueError('commit attribution differs from the native worker model')
        result = host.run_tool(tool, args)
        if not isinstance(result, dict) or set(result) != {'exitCode', 'stdout', 'stderr'} or \
                type(result['exitCode']) is not int or \
                any(not isinstance(result[k], str) for k in ('stdout', 'stderr')):
            raise ValueError('invalid CommandHost result')
        if result['exitCode']:
            detail = (result['stdout'] + '\n' + result['stderr']).strip()[:4000]
            raise ValueError('tool exit ' + str(result['exitCode']) + ': ' + detail)
        return result
    except Exception as exc:
        raise Stopped(tool, exc) from exc


def prepare(task, coordinator_host):
    """Create and claim once. Partial success stays visible for PM recovery."""
    from kitlib.git import refs, repo
    from kitlib.config import load
    main = repo.main_checkout_toplevel(task.coordinator)
    if not main or not task.main or not repo.same_path(main, task.main):
        raise Stopped('prepare', 'main checkout changed')
    cfg = load(main)
    if _creation_config(cfg) != task.creation_config:
        raise Stopped('prepare', 'main creation configuration changed; no worktree was created')
    base, why = refs.resolve(cfg.main_branch, main)
    if base != task.base:
        raise Stopped('prepare', 'base changed: ' + str(why or base))
    existing, why = refs.resolve(task.branch, task.coordinator, known=True)
    if existing is None or existing or Path(task.worker).exists():
        raise Stopped('prepare', 'branch/destination exists or Git cannot answer')
    from kitlib.backlog.opener import open_store
    from kitlib.backlog.store import require_readable
    ctx = open_store(task.coordinator, fetch=True, session=task.session)
    snap = ctx.store.load()
    require_readable(snap)
    rec = snap.get(task.item)
    if rec is None or rec.get('state') != 'open' or rec.get('session') != task.session:
        raise Stopped('prepare', 'item must be open and assigned to this session')
    _run(task, 'new_worktree', (task.branch, '--host', HOST, '--no-wait'),
         'coordinator', task.coordinator, coordinator_host)
    _checkout(task)
    if _head(task.worker) != task.base:
        raise Stopped('prepare', 'created checkout has a different base; claim was not made')
    _run(task, 'backlog', ('--session', task.session, '--as', task.branch, 'pull', task.item),
         'coordinator', task.coordinator, coordinator_host)
    _claimed(task)
    return {'stage': 'claimed', 'worker': task.worker, 'base': task.base,
            'bridge': 'not_required', 'webpage': 'not_required'}


def _checkout(task):
    from kitlib.git import repo
    coordinator, worker = repo.classify(task.coordinator), repo.classify(task.worker)
    if worker.kind != repo.LINKED or not repo.same_path(worker.top, task.worker) or \
            coordinator.kind != repo.LINKED or worker.common != coordinator.common:
        raise Stopped('checkout', 'worker is not this repository\'s linked checkout')
    if _git(('symbolic-ref', '--short', 'HEAD'), task.worker) != task.branch:
        raise Stopped('checkout', 'worker branch changed')


def _claimed(task):
    from kitlib.backlog.opener import open_store
    from kitlib.backlog.store import require_readable
    ctx = open_store(task.coordinator, fetch=True, session=task.session)
    snap = ctx.store.load()
    require_readable(snap)
    rec = snap.get(task.item)
    if rec is None or rec.get('state') != 'pulled' or rec.get('pulled_by') != task.branch or \
            rec.get('session') != task.session:
        raise Stopped('claim', 'task is not held by this branch and session')


def complete(task, subject, worker_host, test_host, reviewer_host):
    """Commit caller edits, gate the exact tip, then request independent review.

    This is intentionally not idempotent. After interruption, use handoff() only
    when the existing gate record proves the expected commit passed; otherwise
    inspect/recover with the shared tools. No mutation is automatically replayed.
    """
    _checkout(task)
    _claimed(task)
    if not _git(('status', '--porcelain'), task.worker):
        raise Stopped('commit', 'no edits; do not replay a previous commit')
    before = _head(task.worker)
    _run(task, 'commit', ('--model', task.model, '-m', subject,
                         '-b', 'Closes: ' + task.item, *task.paths),
         'worker', task.worker, worker_host)
    tip = _head(task.worker)
    if tip == before or _git(('status', '--porcelain'), task.worker):
        raise Stopped('commit', 'commit did not advance a clean checkout')
    _run(task, 'gate', ('--base', task.base), 'test-runner', task.worker, test_host)
    return handoff(task, tip, reviewer_host)


def handoff(task, expected_tip, reviewer_host):
    """Read-only recovery/handoff for a clean, still-claimed, exactly gated tip."""
    from kitlib.git import refs
    from kitlib.config import load
    from kitlib.records import gate_ok
    from kitlib.gate.runner import gate_config
    if not refs.is_sha(expected_tip):
        raise ValueError('a full expected commit hash is required')
    _checkout(task)
    _claimed(task)
    if _head(task.worker) != expected_tip or _git(('status', '--porcelain'), task.worker):
        raise Stopped('review', 'checkout changed or is dirty')
    good, why = gate_ok.verify(task.worker, expected_tip,
                               gate_config=gate_config(load(task.worker)))
    if not good:
        raise Stopped('review', 'exact tip gate proof is missing: ' + str(why))
    ancestor, why = refs.merge_base(task.base, expected_tip, task.worker)
    if ancestor != task.base:
        raise Stopped('review', 'prepared base is not an ancestor of the reviewed tip: ' + str(why))
    if good.get('base') != ancestor:
        raise Stopped('review', 'gate record base does not cover the prepared task scope')
    result = _run(task, 'review_pack', (task.branch, '--base', task.base),
                  'reviewer', task.worker, reviewer_host)
    _checkout(task)
    _claimed(task)
    if _head(task.worker) != expected_tip or _git(('status', '--porcelain'), task.worker):
        raise Stopped('review', 'checkout changed during review preparation')
    return {'stage': 'review_requested', 'branch': task.branch, 'tip': expected_tip,
            'base': task.base, 'item': task.item, 'review_pack': result['stdout'],
            'independent_review': 'required', 'landing': 'PM_only_disabled',
            'bridge': 'not_required', 'webpage': 'not_required'}


def recovery_plan(task):
    """Read-only shared-tool inspections after interruption; no automatic cleanup.

    The PM decides release/abandon after inspecting these facts. Builders never
    close claims or remove potentially useful uncommitted work automatically.
    """
    return tuple(kit_adapter.plan(tool, args, task.coordinator, role='coordinator',
                                  profile=task.profile) for tool, args in (
        ('backlog', ('--session', task.session, 'show', task.item)),
        ('worktree_paths', (task.worker,)),
    ))


def describe(task):
    """Serializable read-only preview for the thin CLI and controller consumers."""
    return {'coordinator': task.coordinator, 'worker': task.worker, 'branch': task.branch,
            'item': task.item, 'session': task.session, 'model': task.model,
            'base': task.base, 'paths': list(task.paths),
            'operations': ['new_worktree', 'backlog pull', 'caller runtime edits',
                           'commit', 'gate', 'review_pack'],
            'execution': 'requires separate bound coordinator/worker/test/reviewer hosts',
            'independent_review': 'required', 'landing': 'PM_only_disabled',
            'bridge': 'not_required', 'webpage': 'not_required'}
