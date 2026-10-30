"""The backlog roles in the guard (docs/guard.md "Backlog roles"; docs/backlog.md "Roles"): a
subagent's backlog.py run is judged by kitlib.backlog.roles for the role its hook payload names
(backlog-role), and code that imports kitlib.backlog, a write of the backlog's refs and an edit of
a folder store's record files are raw writes no agent session makes (backlog-raw-write). A main
session's backlog.py run is the tool's own check (tests/backlog/test_policy.py)."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from kitlib import agentname, pmdata
from kitlib.backlog import policy, roles
from kitlib.git import refs, repo
from kitlib.guard import backlogrole, context

from tests.backlog.helpers import TODAY, priced
from tests.gitfixture import TempRepo, fixture_env
from tests.home import temp_home

from . import world as W
from .claudehost import decide
from .world import event

COORDINATOR = {context.ROLE_ENV: 'coordinator'}
PM = {context.ROLE_ENV: 'pm'}
OID = 'a' * 40


class Rec(dict):
    """A record as kitlib.backlog.store's Snapshot gives it: get() and all()."""

    def all(self, key):
        v = self.get(key)
        return [] if v is None else v if isinstance(v, list) else [v]


def pulled_by(branch):
    return Rec(id='X-1', state='pulled', pulled_by=branch)


class Base(unittest.TestCase):
    kit = None

    def setUp(self):
        self.w = W.World(kit=self.kit)
        self.addCleanup(self.w.cleanup)
        self.tool = W.fwd(os.path.join(self.w.kit_dir, 'tools', 'backlog.py'))
        self.wt = W.fwd(self.w.wt)
        self.rec = None
        p = mock.patch.object(backlogrole, 'read_record', self.read)
        p.start()
        self.addCleanup(p.stop)
        self.reads = []

    def read(self, root, rid):
        self.reads.append((W.fwd(root), rid))
        return self.rec

    def cmd(self, text, agent_type='worker', tool='Bash', sub=True, env=None, cwd=None):
        ev = event(tool, {'command': text, 'description': 'test'}, cwd or self.w.wt, sub, agent_type)
        return decide(ev, self.w.ctx(environ=env if env is not None else {}))

    def bl(self, args):
        return 'python "%s" -C "%s" %s' % (self.tool, self.wt, args)

    def allowed(self, d):
        self.assertIsNone(d, d and d.reason)

    def refused(self, d, rule='backlog-role'):
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)
        return d

    def denied(self, d):
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual(d.verdict, 'deny', d.reason)


class Verbs(Base):
    def test_a_builder_reads_adds_and_pulls(self):
        for args in ('next', 'show X-1', 'list', 'add --summary "a thing" --scope s', 'pull X-1',
                     '--no-fetch show X-1', 'check'):
            with self.subTest(args):
                self.allowed(self.cmd(self.bl(args)))

    def test_a_builder_may_not_price_close_or_retarget(self):
        for args in ('set X-1 --priority 3', 'set X-1 --priority 1', 'set X-1 --priority -1',
                     'set X-1 --unset priority', 'set X-1 --session kit', 'close X-1', 'retire X-1 --why w',
                     'delete X-1 --why w', 'clear-review X-1', 'set-status X-1 parked --q Q-1',
                     'retarget A B --apply', 'block X-1 --owner-ask why',
                     'migrate --to main'):
            with self.subTest(args):
                d = self.refused(self.cmd(self.bl(args)))
                self.assertIn('kitlib/backlog/roles.py', d.reason)

    def test_the_refusal_names_the_row_and_the_role(self):
        d = self.refused(self.cmd(self.bl('close X-1')))
        self.assertIn('`close`', d.reason)
        self.assertIn('worker', d.reason)

    def test_a_dry_run_is_a_read(self):
        for args in ('retarget A B',):
            with self.subTest(args):
                self.allowed(self.cmd(self.bl(args)))

    def test_help_runs_nothing(self):
        for args in ('close X-1 --help', 'set X-1 --priority 1 -h', '--help'):
            with self.subTest(args):
                self.allowed(self.cmd(self.bl(args)))

    def test_a_reviewer_and_a_built_in_agent_only_read(self):
        for agent_type in ('reviewer', 'test-runner', 'general-purpose', 'Explore'):
            with self.subTest(agent_type):
                self.allowed(self.cmd(self.bl('show X-1'), agent_type))
                self.refused(self.cmd(self.bl('add --summary s'), agent_type))
                self.refused(self.cmd(self.bl('pull X-1'), agent_type))

    def test_a_session_only_role_named_by_a_subagent_is_unlisted(self):
        for agent_type in ('coordinator', 'pm', 'owner', 'deep-worker', 'consultant'):
            with self.subTest(agent_type):
                self.refused(self.cmd(self.bl('add --summary s'), agent_type))

    def test_a_lander_does_not_close_with_the_tool(self):
        self.allowed(self.cmd(self.bl('add --summary s'), 'lander'))
        self.refused(self.cmd(self.bl('close X-1'), 'lander'))

    def test_a_prefixed_agent_type_is_its_role(self):
        self.allowed(self.cmd(self.bl('pull X-1'), agentname.PREFIX + 'worker'))
        self.refused(self.cmd(self.bl('close X-1'), agentname.PREFIX + 'worker'))

    def test_a_main_session_is_the_tools_own_check(self):
        for env in ({}, COORDINATOR, PM):
            with self.subTest(env=env):
                self.allowed(self.cmd(self.bl('close X-1'), sub=False, env=env))

    def test_a_word_the_guard_cannot_read(self):
        for args in ('$VERB X-1', 'set X-1 --priority $P', 'set X-1 $FLAG 2', '-C $W close X-1',
                     'set X-1 --prio 2'):
            with self.subTest(args):
                self.refused(self.cmd('python "%s" %s' % (self.tool, args)))


class Spellings(Base):
    def test_module_and_runpy_runs(self):
        for text in ('python -m tools.backlog close X-1', 'python -Bm tools.backlog close X-1',
                     'python -mtools.backlog close X-1', 'python -m runpy tools.backlog close X-1',
                     'py -3 -m tools.backlog set X-1 --priority 2', 'python3 tools/backlog.py close X-1',
                     'python "%s" close X-1' % self.tool.replace('/', '\\')):
            with self.subTest(text):
                self.refused(self.cmd(text))

    def test_nested_and_chained_runs(self):
        for text in ('cd "%s" && %s' % (self.wt, self.bl('close X-1')),
                     'bash -c \'%s\'' % self.bl('close X-1'),
                     'echo ok; %s' % self.bl('close X-1'),
                     'x=$(%s)' % self.bl('close X-1')):
            with self.subTest(text):
                self.refused(self.cmd(text))

    def test_powershell(self):
        for text in ('& python "%s" -C "%s" close X-1' % (self.tool, self.wt),
                     'python "%s" close X-1; Write-Output done' % self.tool,
                     'powershell -Command "python %s close X-1"' % self.tool):
            with self.subTest(text):
                self.refused(self.cmd(text, tool='PowerShell'))

    def test_start_process(self):
        """A Start-Process -ArgumentList run (rules.tool_run; commands._split_list): its words, a
        list or strings split as Windows splits them, are the tool's, judged for the role as any
        run. A double-quoted segment is ONE argument, as Windows hands it to the tool, so
        `--summary "x --priority 1"` is a summary, not a price."""
        for args in ("'%s','set','X-1','--priority','1'" % self.tool,
                     "'%s set X-1 --priority 1'" % self.tool,
                     "'%s','-C','%s','close','X-1'" % (self.tool, self.wt),
                     "'%s','-C %s close X-1'" % (self.tool, self.wt)):
            with self.subTest(args):
                d = self.refused(self.cmd('Start-Process python -ArgumentList %s' % args, tool='PowerShell'))
                self.assertIn('kitlib/backlog/roles.py', d.reason)
        for args in ("'%s','show','X-1'" % self.tool, "'%s next'" % self.tool,
                     "'%s','-C %s show X-1'" % (self.tool, self.wt)):
            with self.subTest(args):
                self.allowed(self.cmd('Start-Process python -ArgumentList %s' % args, tool='PowerShell'))
        self.rec = pulled_by('kit/wt')
        self.allowed(self.cmd("Start-Process python -ArgumentList '%s -C %s set X-1 --summary \"x --priority 1\"'"
                              % (self.tool, self.wt), tool='PowerShell'))
        self.refused(self.cmd("Start-Process python -ArgumentList '%s -C %s set X-1 --summary x --priority 1'"
                              % (self.tool, self.wt), tool='PowerShell'))

    def test_a_powershell_word_holding_a_double_quote_is_unread(self):
        self.rec = pulled_by('kit/wt')
        self.allowed(self.cmd("python \"%s\" -C \"%s\" set X-1 --scope 'a b'" % (self.tool, self.wt),
                              tool='PowerShell'))
        self.refused(self.cmd("python \"%s\" -C \"%s\" set X-1 --scope 'a \"--priority\" b'"
                              % (self.tool, self.wt), tool='PowerShell'))

    def test_a_mark_set_on_the_line_changes_nothing(self):
        for text, tool in (('PM_KIT_ROLE=coordinator %s' % self.bl('close X-1'), 'Bash'),
                           ('export PM_KIT_ROLE=coordinator; %s' % self.bl('close X-1'), 'Bash'),
                           ('env PM_KIT_ROLE=pm %s' % self.bl('set X-1 --priority 1'), 'Bash'),
                           ("$env:PM_KIT_ROLE='coordinator'; %s" % self.bl('close X-1'), 'PowerShell')):
            with self.subTest(text):
                self.refused(self.cmd(text, tool=tool))
        self.refused(self.cmd(self.bl('close X-1'), env=COORDINATOR))

    def test_another_program_named_backlog_is_not_the_tool(self):
        for text in ('git log -- tools/backlog.py', 'grep close tools/backlog.py', 'cat docs/backlog.md'):
            with self.subTest(text):
                self.allowed(self.cmd(text))


class OwnPull(Base):
    def test_a_builder_edits_text_releases_and_blocks_its_own_pull(self):
        self.rec = pulled_by('kit/wt')
        for args in ('set X-1 --scope "more"', 'set X-1 --summary s --effort e', 'set X-1 --body-file b.md',
                     'release X-1 --reason r', 'block X-1 --on Y-2 --reason r', 'unblock X-1 --on Y-2',
                     '--as kit/wt set X-1 --scope s'):
            with self.subTest(args):
                self.allowed(self.cmd(self.bl(args)))
        self.assertIn((self.wt, 'X-1'), self.reads)

    def test_not_its_own_pull(self):
        for rec in (pulled_by('kit/other'), Rec(id='X-1', state='open'), None,
                    Rec(id='X-1', state='done', pulled_by='kit/wt')):
            with self.subTest(rec=rec):
                self.rec = rec
                self.refused(self.cmd(self.bl('set X-1 --scope s')))
                self.refused(self.cmd(self.bl('release X-1')))

    def test_as_another_branch_is_not_its_own(self):
        self.rec = pulled_by('kit/other')
        self.refused(self.cmd(self.bl('--as kit/other set X-1 --scope s')))
        self.rec = pulled_by('kit/wt')
        self.refused(self.cmd(self.bl('--as kit/other set X-1 --scope s')))

    def test_its_own_pull_is_read_where_the_tool_runs(self):
        self.rec = pulled_by('main')
        self.refused(self.cmd(self.bl('set X-1 --scope s')))        # -C the worktree: kit/wt
        self.allowed(self.cmd('python "%s" -C "%s" set X-1 --scope s' % (self.tool, W.fwd(self.w.main))))
        self.allowed(self.cmd('python "%s" set X-1 --scope s' % self.tool, cwd=self.w.main))

    def test_a_cd_the_guard_cannot_read_is_no_folder(self):
        """The hook's directory is the worktree holding the pull, but the line moves where the
        text cannot tell (commands.run_dir: None): the store is unknown, so the run is refused
        (backlogrole._root fails closed), never judged at the hook's directory."""
        self.rec = pulled_by('kit/wt')
        for prefix in ('cd "$X" && ', 'cd $X; '):
            for args in ('set X-1 --scope s', '-C . set X-1 --scope s'):
                with self.subTest(prefix=prefix, args=args):
                    d = self.refused(self.cmd('%spython "%s" %s' % (prefix, self.tool, args)))
                    self.assertIn('-C <worktree> show X-1', d.reason)
        self.assertEqual(self.reads, [])

    def test_a_start_process_reads_the_store_where_its_working_directory_says(self):
        """Start-Process -WorkingDirectory moves where the tool runs (commands.run_dir): with no
        -C, or a relative one, the store is read there, not at the command's directory."""
        self.rec = pulled_by('kit/wt')                               # main's branch is main: not its own

        def sp(c, workdir, cwd):
            return self.cmd("Start-Process python -ArgumentList '%s %sset X-1 --scope s' -WorkingDirectory \"%s\""
                            % (self.tool, c, workdir), tool='PowerShell', cwd=cwd)
        for c in ('', '-C . '):
            with self.subTest(c=c):
                self.refused(sp(c, self.w.main, self.w.wt))
                self.allowed(sp(c, self.w.wt, self.w.main))
        self.refused(self.cmd("Start-Process python -ArgumentList '%s set X-1 --scope s' -WorkingDirectory $X"
                              % self.tool, tool='PowerShell'))

    def test_own_pull_never_widens_to_a_lead_row(self):
        self.rec = pulled_by('kit/wt')
        for args in ('set X-1 --scope s --priority 3', 'block X-1 --owner-ask q', 'close X-1',
                     'set X-1 --scope $S'):
            with self.subTest(args):
                self.refused(self.cmd(self.bl(args)))

    def test_a_reviewer_is_not_granted_its_own_pull(self):
        self.rec = pulled_by('kit/wt')
        self.refused(self.cmd(self.bl('set X-1 --scope s'), 'reviewer'))

    def test_the_record_is_read_only_when_it_decides(self):
        self.cmd(self.bl('show X-1'))
        self.cmd(self.bl('close X-1'))
        self.cmd(self.bl('set X-1 --scope s'), 'reviewer')
        self.assertEqual(self.reads, [])

    def test_a_record_that_does_not_read_is_not_its_own(self):
        def boom(root, rid):
            raise OSError('no store')
        with mock.patch.object(backlogrole, 'read_record', boom):
            self.refused(self.cmd(self.bl('set X-1 --scope s')))


class AsFromItsWorktree(Base):
    """`-C <store> --as <branch>` from that branch's linked worktree (backlogrole._worktree_on):
    the form the tool's own refusal names (plan.holder_form). A builder blocking its own pull of a
    store in another checkout was refused as not its own (role=subagent). The store here is the
    main checkout, whose branch is main; read_record is patched, so any folder serves."""

    def setUp(self):
        super(AsFromItsWorktree, self).setUp()
        self.store = W.fwd(self.w.main)
        self.rec = pulled_by('kit/wt')

    def run_as(self, branch, args='block X-1 --on Y-2 --reason r', cwd=None, prefix='', agent_type='worker'):
        return self.cmd('%spython "%s" -C "%s" --as %s %s' % (prefix, self.tool, self.store, branch, args),
                        agent_type, cwd=cwd)

    def branches(self, table):
        """FakeGit's current_branch, with `table` (folder -> branch) answered first."""
        orig = self.w.git.current_branch

        def current_branch(cwd):
            for where, branch in table.items():
                if os.path.normcase(os.path.abspath(cwd)).startswith(os.path.normcase(where)):
                    return branch, None
            return orig(cwd)
        p = mock.patch.object(self.w.git, 'current_branch', current_branch)
        p.start()
        self.addCleanup(p.stop)

    def test_its_worktree_on_the_as_branch_is_its_own(self):
        for args in ('block X-1 --on Y-2 --reason r', 'release X-1 --reason r', 'set X-1 --scope s',
                     'unblock X-1 --on Y-2'):
            with self.subTest(args):
                self.allowed(self.run_as('kit/wt', args))
        self.assertIn((self.store, 'X-1'), self.reads)              # read from the store, not the worktree
        self.allowed(self.run_as('kit/wt', agent_type=agentname.PREFIX + 'senior-worker'))

    def test_any_spelling_of_its_worktree(self):
        sub = os.path.join(self.w.wt, 'kitlib')
        os.makedirs(sub)
        for cwd in (sub, self.w.wt.upper(), W.fwd(self.w.wt), self.w.wt + os.sep):
            with self.subTest(cwd=cwd):
                self.allowed(self.run_as('kit/wt', cwd=cwd))
        for prefix in ('cd "%s" && ' % W.fwd(self.w.wt), 'cd "%s"; ' % self.w.wt.replace('/', '\\'),
                       'cd "%s" && ' % W.fwd(sub)):
            with self.subTest(prefix=prefix):
                self.allowed(self.run_as('kit/wt', cwd=self.w.main, prefix=prefix))

    def test_a_start_process_runs_where_its_working_directory_says(self):
        def sp(workdir, cwd):
            return self.cmd("Start-Process python -ArgumentList '%s -C %s --as kit/wt release X-1 --reason r' "
                            "-WorkingDirectory %s" % (self.tool, self.store, workdir), tool='PowerShell', cwd=cwd)
        self.allowed(sp('"%s"' % self.w.wt, self.w.main))
        self.refused(sp('"%s"' % self.w.main, self.w.wt))
        self.refused(sp('$X', self.w.wt))                           # a computed folder is none

    def test_the_main_checkout_is_never_its_worktree(self):
        self.refused(self.run_as('kit/wt', cwd=self.w.main))
        self.refused(self.run_as('kit/wt', cwd=self.w.main, prefix='cd "%s" && ' % W.fwd(self.w.main)))
        self.refused(self.run_as('kit/wt', prefix='cd "%s" && ' % W.fwd(self.w.main)))
        # refused by its kind, not its branch: main checked out on the --as branch, the store elsewhere
        self.store = W.fwd(self.w.plain)
        self.branches({self.w.main: 'kit/wt', self.w.plain: 'main'})
        self.refused(self.run_as('kit/wt', cwd=self.w.main))

    def test_the_pm_data_worktree_is_never_its_worktree(self):
        self.store = W.fwd(self.w.plain)
        self.branches({self.w.data: 'kit/wt', self.w.plain: 'main'})
        self.allowed(self.run_as('kit/wt', cwd=self.w.data))        # a plain linked worktree on kit/wt
        self.w.user_config({'pm_data': {'path': self.w.data}})
        self.refused(self.run_as('kit/wt', cwd=self.w.data))        # answered as MAIN

    def test_no_checkout_or_an_unknown_one_is_not_its_worktree(self):
        for cwd in (self.w.plain, self.w.unknown):
            with self.subTest(cwd=cwd):
                self.refused(self.run_as('kit/wt', cwd=cwd))

    def test_a_directory_the_text_cannot_tell_is_not_its_worktree(self):
        # the hook's directory is the worktree, but the line moves where the text cannot tell
        for prefix in ('cd "$X" && ', 'cd $X; ', 'cd .. && cd "$X"; '):
            with self.subTest(prefix=prefix):
                self.refused(self.run_as('kit/wt', prefix=prefix))

    def test_a_worktree_on_another_branch_is_not_its_own(self):
        self.rec = pulled_by('kit/other')
        self.refused(self.run_as('kit/other'))                      # in kit/wt's worktree
        self.rec = pulled_by('kit/wt')
        self.refused(self.run_as('kit/other'))
        self.refused(self.run_as('Kit/WT'))                         # a branch is as written

    def test_a_branch_that_does_not_hold_the_pull_is_not_its_own(self):
        for rec in (pulled_by('kit/other'), Rec(id='X-1', state='open'), None,
                    Rec(id='X-1', state='done', pulled_by='kit/wt')):
            with self.subTest(rec=rec):
                self.rec = rec
                self.refused(self.run_as('kit/wt'))

    def test_it_never_widens_to_a_lead_row(self):
        for args in ('block X-1 --owner-ask q', 'close X-1', 'set X-1 --scope s --priority 3'):
            with self.subTest(args):
                self.refused(self.run_as('kit/wt', args))
        self.refused(self.run_as('kit/wt', agent_type='reviewer'))

    def test_the_refusal_names_the_accepted_form(self):
        self.rec = pulled_by('kit/other')
        d = self.refused(self.run_as('kit/wt', cwd=self.w.main))
        self.assertIn('run it as one call, `python %s -C <your worktree> block X-1 ...`, with -C your '
                      'branch\'s linked worktree, no --as and no cd'
                      % backlogrole.rules.kit_tool(self.w.ctx(), backlogrole.TOOL), d.reason)
        self.assertNotIn('--as <your branch>', d.reason)
        self.assertNotIn('cd "', d.reason)
        d = self.refused(self.cmd(self.bl('set X-1 --scope s')))
        self.assertIn('-C <your worktree> set X-1 ...', d.reason)
        self.assertIn('the main checkout, the PM data worktree, or a branch that does not hold the pull, '
                      'does not count', d.reason)

    def test_the_form_is_named_only_when_its_own_pull_would_allow_it(self):
        for args in ('close X-1', 'block X-1 --owner-ask q', 'set X-1 --scope $S'):
            with self.subTest(args):
                d = self.refused(self.run_as('kit/wt', args, agent_type='worker'))
                self.assertNotIn('<your worktree>', d.reason)
        d = self.refused(self.run_as('kit/wt', cwd=self.w.main, agent_type='reviewer'))
        self.assertNotIn('<your worktree>', d.reason)


class HeldStore(Base):
    """The stores of OwnPullInstead and OwnPullDecision: `held` maps a folder to the record the
    store it opens holds. The branch's worktree opens the live store (read_record's own opener,
    RealStore below); w.main stands for a checkout holding nothing, w.plain for the PM data
    worktree (read_record is patched, so any folder serves)."""

    def setUp(self):
        super(HeldStore, self).setUp()
        self.store = W.fwd(self.w.plain)
        self.main = W.fwd(self.w.main)
        self.held = {}                          # folder -> the record the store it opens holds

    def read(self, root, rid):
        self.reads.append((W.fwd(root), rid))
        return self.held.get(W.fwd(root))

    def again(self, d, tool='Bash', cwd=None, agent_type='worker'):
        """The decision on the refusal's Instead, run as the refused command was."""
        return self.cmd(d.instead, agent_type, tool=tool, cwd=cwd)

    def form(self, words):
        return 'python %s -C "%s" %s' % (backlogrole.rules.kit_tool(self.w.ctx(), backlogrole.TOOL),
                                         self.wt, words)

    def from_main(self, words, branch='kit/wt'):
        """The run at the main checkout's store, as `branch`."""
        return 'python "%s" -C "%s" --as %s %s' % (self.tool, self.main, branch, words)


class OwnPullInstead(HeldStore):
    """A builder's run on its own pull of the PM data store. `-C <its worktree>` alone is the form:
    one bare call, from any directory, reading the store the worktree opens. The Instead of a run
    aimed elsewhere is that form written out; a chained `cd` is no form (it chains a write with
    another command, AGENTS.md)."""

    def test_the_bare_worktree_run_is_its_own_from_any_directory(self):
        self.held[self.wt] = pulled_by('kit/wt')
        for args in ('set X-1 --scope s', 'release X-1 --reason r', 'block X-1 --on Y-2 --reason r'):
            for tool, text in (('Bash', self.bl(args)), ('PowerShell', '& ' + self.bl(args))):
                for cwd in (None, self.w.main):
                    with self.subTest(args, tool=tool, cwd=cwd):
                        self.allowed(self.cmd(text, tool=tool, cwd=cwd))
        self.assertIn((self.wt, 'X-1'), self.reads)

    def test_the_form_names_the_worktree_and_passes(self):
        self.held[self.wt] = pulled_by('kit/wt')
        for args, words in (('set X-1 --scope s', 'set X-1 --scope s'),
                            ('release X-1 --reason r', 'release X-1 --reason r'),
                            ('block X-1 --on Y-2 --reason "a b"', "block X-1 --on Y-2 --reason 'a b'"),
                            ('--no-fetch --session S set X-1 --scope s', '--no-fetch --session S set X-1 --scope s')):
            for tool in ('Bash', 'PowerShell'):
                with self.subTest(args, tool=tool):
                    text = self.from_main(args) if tool == 'Bash' else '& ' + self.from_main(args)
                    d = self.refused(self.cmd(text, tool=tool))
                    self.assertEqual(d.instead, self.form(words))
                    self.assertIn('The record is pulled by kit/wt', d.reason)
                    self.assertIn(self.wt, d.reason)
                    self.assertNotIn('--as', d.instead)
                    self.assertNotIn('cd ', d.instead)
                    self.allowed(self.again(d, tool))
                    self.allowed(self.again(d, tool, cwd=self.w.main))

    def test_someone_elses_pull_is_refused_with_the_read(self):
        read = 'python %s -C "%s" show X-1' % (backlogrole.rules.kit_tool(self.w.ctx(), backlogrole.TOOL), self.wt)
        for rec in (pulled_by('kit/other'), Rec(id='X-1', state='open'), None,
                    Rec(id='X-1', state='done', pulled_by='kit/wt')):
            with self.subTest(rec=rec):
                self.held[self.wt] = rec
                d = self.refused(self.cmd(self.bl('set X-1 --scope s')))
                self.assertEqual(d.instead, read)
                self.assertNotIn('The record is pulled by', d.reason)
                self.allowed(self.again(d))

    def test_it_never_names_a_branch_the_run_does_not_act_as(self):
        self.held[self.wt] = pulled_by('kit/other')
        d = self.refused(self.cmd(self.bl('--as kit/other set X-1 --scope s')))
        self.assertNotIn('kit/other', d.instead)
        self.assertTrue(d.instead.endswith('show X-1'), d.instead)
        self.held[self.wt] = pulled_by('kit/wt')
        d = self.refused(self.cmd(self.bl('--as kit/other set X-1 --scope s')))
        self.assertTrue(d.instead.endswith('show X-1'), d.instead)
        self.assertNotIn('--as kit/wt', d.instead)

    def test_no_worktree_in_view_is_named_in_the_sentence(self):
        # run from the main checkout as kit/wt: the guard sees no worktree of that branch
        self.held[self.wt] = pulled_by('kit/wt')
        d = self.refused(self.cmd(self.from_main('set X-1 --scope s'), cwd=self.w.main))
        self.assertIn('-C <your worktree> set X-1 ...', d.reason)
        self.assertTrue(d.instead.endswith('show X-1'), d.instead)

    def test_a_lead_row_or_a_reviewer_gets_no_form(self):
        self.held[self.wt] = pulled_by('kit/wt')
        for args, agent_type in (('close X-1', 'worker'), ('set X-1 --scope s --priority 3', 'worker'),
                                 ('set X-1 --scope s', 'reviewer')):
            with self.subTest(args, agent_type=agent_type):
                d = self.refused(self.cmd(self.bl(args), agent_type))
                self.assertNotIn('The record is pulled by', d.reason)
                self.assertNotIn('<your worktree>', d.reason)


class OwnPullDecision(HeldStore):
    """Who passes: the own pull run with -C its worktree, and -C <store> --as <branch> from that
    branch's worktree (unchanged). Someone else's pull, `--as` another branch, a reviewer, a lead
    row, and a run from the main checkout or the PM data worktree stay refused as backlog-role.
    Bash and PowerShell each."""

    def test_refused(self):
        mine, other = pulled_by('kit/wt'), pulled_by('kit/other')
        rows = (('someone else\'s pull', other, 'set X-1 --scope s', 'worker'),
                ('an open record', Rec(id='X-1', state='open'), 'release X-1', 'worker'),
                ('--as another branch', mine, '--as kit/other set X-1 --scope s', 'worker'),
                ('a reviewer', mine, 'set X-1 --scope s', 'reviewer'),
                ('a lead row', mine, 'close X-1', 'worker'),
                ('a price', mine, 'set X-1 --scope s --priority 3', 'worker'))
        for name, rec, args, agent_type in rows:
            for tool in ('Bash', 'PowerShell'):
                with self.subTest(name, tool=tool):
                    self.held = {self.wt: rec}
                    text = self.bl(args) if tool == 'Bash' else '& ' + self.bl(args)
                    self.refused(self.cmd(text, agent_type, tool=tool))

    def test_the_main_checkout_and_the_pm_data_worktree_are_refused(self):
        mine = pulled_by('kit/wt')
        self.w.user_config({'pm_data': {'path': self.w.plain}})
        for folder in (self.main, self.store):
            for cwd in (None, self.w.main):
                with self.subTest(folder=folder, cwd=cwd):
                    self.held = {self.main: mine, self.store: mine, self.wt: mine}
                    self.refused(self.cmd('python "%s" -C "%s" set X-1 --scope s' % (self.tool, folder), cwd=cwd))

    def test_passes(self):
        self.held = {self.wt: pulled_by('kit/wt'), self.store: pulled_by('kit/wt')}
        for tool in ('Bash', 'PowerShell'):
            for cwd in (None, self.w.main):
                with self.subTest('the worktree', tool=tool, cwd=cwd):
                    self.allowed(self.cmd(self.form('set X-1 --scope s'), tool=tool, cwd=cwd))
            with self.subTest('--as from its worktree', tool=tool):
                self.allowed(self.cmd('python "%s" -C "%s" --as kit/wt set X-1 --scope s' % (self.tool, self.store),
                                      tool=tool))


class RealStore(unittest.TestCase):
    """The guard's decision over a real store (read_record unpatched): a linked worktree on the
    pulling branch reads the PM data worktree's live store through the tool's own opener, so
    `-C <worktree>` is its own pull. Another branch's worktree, the main checkout and the PM data
    worktree are not. ctx is the two things the decision reads, over the same checkouts."""

    def setUp(self):
        patch = mock.patch.dict(os.environ, fixture_env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        temp_home(self)
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', self.repo.head())
        self.pm = self.repo.worktree('pm', branch='pmdata')
        self.repo.write('.kit.json', json.dumps({'backlog': {'store': 'kit/backlog'}}) + '\n', root=self.pm)
        self.repo.write('kit/backlog/FOO.md',
                        priced('FOO', state='pulled', pulled_at=TODAY, pulled_by='kit/wk', scope='tools/x/**')['FOO'],
                        root=self.pm)
        self.repo.commit('the live store', '.kit.json', 'kit/backlog/FOO.md', cwd=self.pm)
        self.wt = self.repo.worktree('wk', branch='kit/wk')
        self.other = self.repo.worktree('oth', branch='kit/oth')
        p = mock.patch.object(backlogrole.rules, 'kit_tool', lambda ctx, name: 'tool.py')
        p.start()
        self.addCleanup(p.stop)

    def place(self, where):
        if os.path.normcase(os.path.normpath(where)) == os.path.normcase(os.path.normpath(self.pm)):
            return SimpleNamespace(kind=repo.MAIN, top=self.pm)         # as ctx.place answers it
        got = repo.classify(where)
        return SimpleNamespace(kind=got.kind, top=repo.toplevel(where)[0])

    def decide(self, folder, here=None, rid='FOO'):
        """The refusal sentence of `-C folder set <rid> --scope tools/x/a.py` by a worker, or ''."""
        ctx = SimpleNamespace(git=SimpleNamespace(current_branch=refs.current_branch), place=self.place)
        words = ['-C', folder, 'set', rid, '--scope', 'tools/x/a.py']
        pairs = [(w, True) for w in words]
        cmd = SimpleNamespace(shell=backlogrole.rules.sh.BASH)
        text, _form, _instead = backlogrole.own_pull_refusal(ctx, cmd, policy.read_argv(pairs),
                                                             roles.role_of('worker', True), here, pairs)
        return text

    def test_its_worktree_reads_the_live_store(self):
        rec = backlogrole.read_record(self.wt, 'FOO')
        self.assertIsNotNone(rec, 'the guard read the dead default ref, not the live store')
        self.assertEqual(rec.get('pulled_by'), 'kit/wk')

    def test_its_worktree_passes_from_any_directory(self):
        self.assertFalse(self.decide(self.wt))
        self.assertFalse(self.decide(self.wt, here=self.repo.root))

    def test_another_branchs_worktree_is_refused(self):
        self.assertTrue(self.decide(self.other))

    def test_naming_another_branchs_worktree_acts_as_that_branch(self):
        """The residual docs/guard.md "Known limits" states: only the -C folder's branch is compared,
        so from kit/wk's worktree, -C kit/oth's worktree passes on kit/oth's pull."""
        self.repo.write('kit/backlog/BAR.md',
                        priced('BAR', state='pulled', pulled_at=TODAY, pulled_by='kit/oth', scope='tools/y/**')['BAR'],
                        root=self.pm)
        self.repo.commit('another pull', 'kit/backlog/BAR.md', cwd=self.pm)
        self.assertFalse(self.decide(self.other, here=self.wt, rid='BAR'))
        self.assertTrue(self.decide(self.wt, rid='BAR'))

    def test_the_main_checkout_and_the_pm_data_worktree_are_refused(self):
        self.assertTrue(self.decide(self.repo.root))
        self.assertTrue(self.decide(self.pm))


class QuotedWords(Base):
    """A bash word that is all one double-quoted string (`"$PWD"`) is one word whose text the line
    does not show (backlogrole.argv_words: policy.ONE): read as the value of an option whose value
    sorts nothing, never as the verb, the record or a value that decides a row."""

    def test_a_double_quoted_folder_still_reads(self):
        # a worker's `-C "$PWD" landable <branch>` was refused as set-text: no verb read
        for args in ('landable scripts/gearup-clean', 'show X-1', '--session "$S" next'):
            with self.subTest(args):
                self.allowed(self.cmd('python "%s" -C "$PWD" %s' % (self.tool, args),
                                      agentname.PREFIX + 'worker'))

    def test_a_double_quoted_value_on_its_own_pull(self):
        # a strong-worker's `-C . set <its pull> --scope "$M/..."` was refused: the own pull went unread
        self.rec = pulled_by('kit/wt')
        for where in ('.', '"%s"' % self.wt):
            with self.subTest(where):
                self.allowed(self.cmd('cd "%s"; M=a; T=b; python "%s" -C %s set X-1 --scope "$M/x" '
                                      '--scope "$T/y"' % (self.wt, self.tool, where),
                                      agentname.PREFIX + 'strong-worker'))
        self.assertIn((self.wt, 'X-1'), self.reads)

    def test_its_own_pull_by_dot_or_the_absolute_folder(self):
        self.rec = pulled_by('kit/wt')
        for text in ('python "%s" -C . set X-1 --scope s' % self.tool,
                     'python "%s" -C "%s" set X-1 --scope s' % (self.tool, self.wt),
                     'python "%s" -C %s set X-1 --scope s' % (self.tool, self.wt)):
            with self.subTest(text):
                self.allowed(self.cmd(text, agentname.PREFIX + 'strong-worker', cwd=self.w.wt))

    def test_a_word_that_can_split_or_decides_stays_unread(self):
        self.rec = pulled_by('kit/wt')
        for args in ('-C . set X-1 --scope $M/x', '-C . set X-1 --scope "$@"',
                     '-C . set X-1 --scope "${a[@]}"', '-C . set X-1 --scope "a"$M',
                     '-C . set X-1 --priority "$P"', '-C . set X-1 --unset "$F"',
                     '-C . block X-1 --on "$B" --reason r', '-C "$PWD" set X-1 --scope s',
                     '-C . --as "$B" set X-1 --scope s', '-C . set "$ID" --scope s', '-C . "$VERB" X-1',
                     '-C "$PWD" close X-1'):
            with self.subTest(args):
                self.refused(self.cmd('python "%s" %s' % (self.tool, args), 'strong-worker', cwd=self.w.wt))

    def test_the_refusal_names_the_words_it_could_not_read(self):
        self.rec = pulled_by('kit/wt')
        d = self.refused(self.cmd('python "%s" -C . set X-1 --scope $M/x' % self.tool, 'strong-worker',
                                  cwd=self.w.wt))
        self.assertIn('`$M/x`', d.reason)
        self.assertIn('write them out', d.reason)
        d = self.refused(self.cmd(self.bl('close X-1')))
        self.assertNotIn('write them out', d.reason)

    def test_an_unread_word_that_decides_nothing_is_not_named(self):
        # `close` is a lead row whatever -C names: the unread folder did not decide it (an unquoted
        # `-C $W` can split into a verb, so there no verb reads and the words do decide)
        for args in ('-C "$PWD" close X-1', '-C . close X-1 $R', '-C . close X-1 --reason $R'):
            with self.subTest(args):
                d = self.refused(self.cmd('python "%s" %s' % (self.tool, args), agentname.PREFIX + 'worker',
                                          cwd=self.w.wt))
                self.assertNotIn('write them out', d.reason)

    def test_a_price_or_unset_is_refused_whatever_its_value(self):
        # no value of --priority or --unset gives a row a builder takes, so the unread word decided
        # nothing; an unread --on could name a member a builder may block on, so it did
        self.rec = pulled_by('kit/wt')
        for args, named in (('set X-1 --scope s --priority "$P"', False),
                            ('set X-1 --scope s --unset "$F"', False),
                            ('set X-1 --scope s --priority $P', False),
                            ('block X-1 --on "$B" --reason r', True)):
            with self.subTest(args):
                d = self.refused(self.cmd('python "%s" -C . %s' % (self.tool, args), 'strong-worker',
                                          cwd=self.w.wt))
                (self.assertIn if named else self.assertNotIn)('write them out', d.reason)


class DryRuns(Base):
    """A dry run is a read only when every word reads: an unread word could be the --apply."""

    def test_an_unread_word_on_a_dry_run_is_not_a_read(self):
        for args in ('retarget A B $X', 'retarget A B "$X"', 'catch-up $X p', 'catch-up "$X" p'):
            with self.subTest(args):
                d = self.refused(self.cmd('python "%s" -C . %s' % (self.tool, args), agentname.PREFIX + 'worker',
                                          cwd=self.w.wt))
                self.assertIn('write them out', d.reason)

    def test_a_written_dry_run_still_reads(self):
        for args in ('retarget A B', 'catch-up p'):
            with self.subTest(args):
                self.allowed(self.cmd('python "%s" -C . %s' % (self.tool, args), agentname.PREFIX + 'worker',
                                      cwd=self.w.wt))


class RawCode(Base):
    def test_code_that_imports_the_backlog_package(self):
        for text in ('python -c "from kitlib.backlog import store"',
                     'python -c "import kitlib.backlog.roles as r; r.ACTIONS.clear()"',
                     "python -c 'from kitlib import backlog'",
                     'python -m kitlib.backlog.migrate',
                     'python -m runpy kitlib.backlog.store',
                     "python - <<'EOF'\nimport kitlib.backlog.store as s\nEOF",
                     'echo "import kitlib.backlog" | python -'):
            with self.subTest(text):
                self.refused(self.cmd(text), 'backlog-raw-write')

    def test_code_that_runs_the_tool(self):
        self.refused(self.cmd('python -c "import runpy; runpy.run_path(\'tools/backlog.py\')"'),
                     'backlog-raw-write')

    def test_other_code_runs(self):
        for text in ('python -c "print(1)"', 'python -m unittest tests.backlog.test_policy',
                     "python - <<'EOF'\nprint(open('docs/backlog.md').read())\nEOF"):
            with self.subTest(text):
                self.allowed(self.cmd(text))

    def test_a_main_session_is_not_judged_here(self):
        self.allowed(self.cmd('python -c "from kitlib.backlog import store"', sub=False, env=COORDINATOR))


class HubWriterCode(Base):
    """Code that names the hub's record writer (kitlib.hubsite.recordwrite) or the store's
    plan_answer is a raw write in every session but the owner's, main sessions included
    (backlogrole.HUB_WRITER_RE): only the hub files a bound question's answer."""
    TEXTS = ('python -c "from kitlib.hubsite import recordwrite; recordwrite.answer(1)"',
             'python -c "import kitlib.hubsite.recordwrite as w"',
             "python -c 'from kitlib import hubsite; hubsite.recordwrite.add(1)'",
             "python -c \"import importlib; importlib.import_module('kitlib.hubsite.recordwrite')\"",
             'python -m kitlib.hubsite.recordwrite',
             'python -m runpy kitlib.hubsite.recordwrite',
             "python - <<'EOF'\nfrom kitlib.backlog.plan import plan_answer\nEOF",
             'echo "p.plan_answer(s, c)" | python -')

    def test_every_agent_session_is_refused(self):
        for text in self.TEXTS:
            for who, kw in (('pm', dict(sub=False, env=PM)), ('coordinator', dict(sub=False, env=COORDINATOR)),
                            ('subagent', dict())):
                with self.subTest(text=text, who=who):
                    d = self.refused(self.cmd(text, **kw), 'backlog-raw-write')
                    self.assertIn('Instead:', d.reason)

    def test_a_main_session_refusal_names_the_hub_filing(self):
        d = self.refused(self.cmd(self.TEXTS[0], sub=False, env=PM), 'backlog-raw-write')
        self.assertIn('questions file', d.instead)
        self.assertIn('hub.py', d.instead)
        self.assertIn('recordwrite', d.reason.split('Instead:')[0])

    def test_the_owner_runs_it(self):
        for text in self.TEXTS:
            with self.subTest(text):
                self.allowed(self.cmd(text, sub=False, env={}))

    def test_other_main_session_code_is_unchanged(self):
        for env in (PM, COORDINATOR):
            for text in ('python -c "from kitlib.backlog import store"', 'python -m kitlib.backlog.migrate',
                         "python -c 'from kitlib.hubsite import serve'", 'python -c "print(1)"',
                         'python tools/hub.py questions file EX-1'):
                with self.subTest(env=env, text=text):
                    self.allowed(self.cmd(text, sub=False, env=env))

    # the attribute or alias form and a multi-line import
    ALIAS_TEXTS = ("python -c 'from kitlib.hubsite import serve; serve.recordwrite.add(1)'",
                   'python -c "import kitlib.hubsite as h; h.recordwrite.answer(1)"',
                   'python -c "import kitlib.hubsite as h; h . recordwrite.edit(1)"',
                   "python - <<'EOF'\nfrom kitlib import hubsite as h\nh.\\\n  recordwrite.delete(1)\nEOF",
                   "python - <<'EOF'\nfrom kitlib.hubsite import (\n    serve,\n    recordwrite)\nEOF",
                   "python - <<'EOF'\nfrom kitlib.hubsite import (recordwrite as w,\n    serve)\nEOF",
                   "python - <<'EOF'\nfrom kitlib.hubsite import serve, \\\n    recordwrite\nEOF")

    def test_the_alias_and_multi_line_forms_are_refused(self):
        for text in self.ALIAS_TEXTS:
            for who, kw in (('pm', dict(sub=False, env=PM)), ('subagent', dict())):
                with self.subTest(text=text, who=who):
                    self.refused(self.cmd(text, **kw), 'backlog-raw-write')

    def test_the_owner_runs_the_alias_forms(self):
        for text in self.ALIAS_TEXTS:
            with self.subTest(text):
                self.allowed(self.cmd(text, sub=False, env={}))

    def test_near_names_are_not_the_writer(self):
        for text in ('python -c "import x; x.recordwriter(1)"', 'python -c "x.recordwrite_log(1)"',
                     'python -c "print(\'recordwrite\')"', 'python -c "recordwrite = 1"',
                     "python - <<'EOF'\nfrom kitlib.hubsite import (serve)\nrecordwrite = 1\nEOF",
                     "python - <<'EOF'\nfrom kitlib.hubsite import serve\nrecordwrite = 1\nEOF",
                     "python -c 'from kitlib.hubsite import recordwriter'"):
            with self.subTest(text):
                self.allowed(self.cmd(text, sub=False, env=PM))

    # code that only reads the writer's file, by its path (seen refused live as backlog-raw-write)
    READS = ("python - <<'EOF'\nimport pathlib\n"
             "t = pathlib.Path('kitlib/hubsite/recordwrite.py').read_text(encoding='utf-8')\n"
             "print(t[:400])\nEOF",
             "python - <<'EOF'\n"
             "t = open(r'Z:\\src\\kit\\kitlib\\hubsite\\recordwrite.py').read()\n"
             "print(t.count('def '))\nEOF",
             'python -c "print(open(\'kitlib/hubsite/recordwrite.py\').read()[:200])"')

    def test_code_that_only_reads_the_writer_s_file_passes(self):
        for text in self.READS:
            for who, kw in (('pm', dict(sub=False, env=PM)), ('coordinator', dict(sub=False, env=COORDINATOR)),
                            ('subagent', dict())):
                with self.subTest(text=text, who=who):
                    self.allowed(self.cmd(text, **kw))

    def test_a_read_that_also_imports_the_writer_is_refused(self):
        text = ("python - <<'EOF'\nimport pathlib\n"
                "print(pathlib.Path('kitlib/hubsite/recordwrite.py').stat().st_size)\n"
                "from kitlib.hubsite import recordwrite\nrecordwrite.answer(1)\nEOF")
        for who, kw in (('pm', dict(sub=False, env=PM)), ('subagent', dict())):
            with self.subTest(who=who):
                self.refused(self.cmd(text, **kw), 'backlog-raw-write')

    def test_a_subagent_keeps_the_backlog_code_refusal(self):
        d = self.refused(self.cmd('python -c "from kitlib.backlog.plan import plan_answer"'), 'backlog-raw-write')
        self.assertIn('backlog.py', d.instead)


class Refs(Base):
    def git(self, args, env=PM, sub=False, cwd=None):
        return self.cmd('git -C "%s" %s' % (W.fwd(cwd or self.w.wt), args), sub=sub, env=env)

    def test_a_write_of_the_store_ref(self):
        for args in ('update-ref refs/backlog/main %s' % OID, 'update-ref -d refs/backlog/main',
                     'push origin refs/backlog/main', 'push origin +%s:refs/backlog/main' % OID,
                     'push origin :refs/backlog/main', 'push origin HEAD:refs/backlog/main',
                     'fetch origin +refs/heads/x:refs/backlog/main'):
            with self.subTest(args):
                self.refused(self.git(args), 'backlog-raw-write')
                self.denied(self.git(args, env=COORDINATOR))

    def test_a_mirror_push_writes_it(self):
        self.denied(self.git('push --mirror origin'))

    def test_the_store_ref_in_another_case_is_the_store_ref(self):
        # on this machine's file system refs/Backlog/main is the file refs/backlog/main
        for args in ('update-ref refs/Backlog/main %s' % OID, 'update-ref REFS/BACKLOG/main %s' % OID,
                     'push origin HEAD:refs/BackLog/main', 'fetch origin +refs/heads/x:refs/BACKLOG/main'):
            with self.subTest(args):
                self.refused(self.git(args), 'backlog-raw-write')

    UNREAD_UPDATE_REFS = (
        # (command, host tool, a word the refusal names before Instead:)
        ('echo "update refs/backlog/main %s" | git -C "{wt}" update-ref --stdin' % OID, 'Bash', 'input'),
        ('git -C "{wt}" update-ref --stdin < cmds.txt', 'Bash', 'input'),
        ('git -C "{wt}" update-ref -z --stdin < cmds.txt', 'Bash', 'input'),
        ('"update refs/backlog/main %s" | git -C "{wt}" update-ref --stdin' % OID, 'PowerShell', 'input'),
        ('git -C "{wt}" update-ref --std < cmds.txt', 'Bash', 'input'),            # git takes an abbreviation
        ('git -C "{wt}" update-ref --stdi < cmds.txt', 'Bash', 'input'),
        ('echo refs/backlog/main %s | xargs git -C "{wt}" update-ref' % OID, 'Bash', 'xargs'),
        ('git -C "{wt}" update-ref "$R" %s' % OID, 'Bash', '$R'),                  # a computed ref
        ('git -C "{wt}" update-ref $R %s' % OID, 'PowerShell', '$R'),
    )

    def test_update_ref_from_its_input_may_write_it(self):
        """An update-ref whose ref the guard cannot read (rules._update_ref_unread: --stdin, an
        option it does not know such as an abbreviation, xargs, a computed word) may write the
        store's ref: refused for every session but the owner's (fail closed) where a store reads.
        git-shared-ref holds an agent session's first; the PM and a plain mark meet this."""
        for text, tool, said in self.UNREAD_UPDATE_REFS:
            text = text.format(wt=self.wt)
            for env in (PM, {context.ROLE_ENV: 'plain'}):
                with self.subTest(text=text, env=env):
                    d = self.refused(self.cmd(text, tool=tool, sub=False, env=env), 'backlog-raw-write')
                    why = d.reason.split('Instead:')[0].split('`', 2)[2]       # after the quoted command
                    self.assertIn(said, why)
                    self.assertIn('store', why)
            with self.subTest(text=text, who='subagent'):
                self.denied(self.cmd(text, tool=tool, env={}))
            with self.subTest(text=text, who='owner'):
                self.allowed(self.cmd(text, tool=tool, sub=False, env={}))

    def test_an_unread_update_ref_where_no_store_reads(self):
        """Where no backlog store reads (a folder in no checkout: store_kind None for no fault),
        an update-ref the guard cannot read is not this rule's: the PM keeps it, as
        git-shared-ref leaves the PM free on refs. A ref named literally under refs/backlog/ is
        still the store's, whatever else leaves the update-ref unread (a computed word before it,
        an option the guard does not know)."""
        plain = W.fwd(self.w.plain)
        for text, tool, _said in self.UNREAD_UPDATE_REFS:
            text = text.format(wt=plain)
            with self.subTest(text=text):
                self.allowed(self.cmd(text, tool=tool, sub=False, env=PM, cwd=self.w.plain))
        for args in ('update-ref refs/backlog/main %s' % OID, 'update-ref -m "$msg" refs/backlog/main %s' % OID,
                     'update-ref --no-create-reflog refs/backlog/main %s' % OID):
            for env in (PM, {context.ROLE_ENV: 'plain'}):
                with self.subTest(args=args, env=env):
                    self.refused(self.git(args, env=env, cwd=self.w.plain), 'backlog-raw-write')

    def test_an_unread_update_ref_where_the_store_cannot_be_told(self):
        """store_kind None for a fault is no ground to allow (fail closed): a .kit.json that does not
        load (ctx.config_error), a directory git runs in that the text does not name (gc.unknown,
        here run from a folder in no checkout), a place git cannot tell about (repo.UNKNOWN), git
        re-pointed from a folder in no checkout (gc.blind: --git-dir, GIT_DIR). A
        repository with no .kit.json reads the default ref store, so it is refused as any store."""
        cases = ((None, 'git -C "%s" update-ref --stdin < cmds.txt' % self.wt, 'kit', '.kit.json does not load'),
                 (self.w.plain, 'git -C $d update-ref --stdin < cmds.txt', 'plain', 'directory git runs in is unknown'),
                 (self.w.unknown, 'git update-ref --stdin < cmds.txt', 'unknown', 'git cannot tell the place'),
                 (self.w.plain, 'git --git-dir=/x/.git update-ref --stdin < cmds.txt', 'blind', 're-pointed: --git-dir'),
                 (self.w.plain, 'GIT_DIR=/x/.git git update-ref --stdin < cmds.txt', 'blind', 're-pointed: GIT_DIR'))
        broken = os.path.join(self.w.main, '.kit.json')
        for cwd, text, name, cause in cases:
            if name == 'kit':
                with open(broken, 'w', encoding='utf-8') as f:
                    f.write('{"backlog": {"store": 7}}')
            for env in (PM, {context.ROLE_ENV: 'plain'}):
                with self.subTest(text=text, case=name, env=env):
                    d = self.refused(self.cmd(text, sub=False, env=env, cwd=cwd), 'backlog-raw-write')
                    self.assertUnsure(d, cause)
            if name == 'kit':
                for t, tool, _said in self.UNREAD_UPDATE_REFS:
                    t = t.format(wt=self.wt)
                    with self.subTest(text=t, case=name):
                        self.refused(self.cmd(t, tool=tool, sub=False, env=PM), 'backlog-raw-write')
                os.remove(broken)
        with self.subTest(case='no .kit.json'):
            self.refused(self.cmd('git -C "%s" update-ref --stdin < cmds.txt' % self.wt, sub=False, env=PM),
                         'backlog-raw-write')

    def assertUnsure(self, d, cause):
        """The refusal says the guard cannot tell whether a store reads, and names its cause."""
        why = d.reason.split('Instead:')[0].split('`', 2)[2]               # after the quoted command
        self.assertIn('the guard cannot tell whether a backlog store reads there (', why)
        self.assertIn(cause, why)

    def test_an_unread_update_ref_where_no_main_checkout_is_found(self):
        """A linked worktree whose git common dir is not named .git (a bare clone's worktree, a
        --separate-git-dir repository's): ctx.main_top finds no main checkout, so no .kit.json
        is read and store_kind is None for no proof that none reads there (fail closed). Any
        place but a folder in no checkout counts so."""
        for common in ('bare.git', 'sep'):
            top = os.path.join(self.w.base, 'linked-' + common.replace('.', '-'))
            os.makedirs(top)
            git_common = os.path.join(self.w.base, common)
            here = os.path.normcase(top)

            def classify(path, orig=self.w.classify, top=top, here=here, git_common=git_common):
                if os.path.normcase(os.path.abspath(path)).startswith(here):
                    return repo.Place(repo.LINKED, top, '', os.path.join(git_common, 'worktrees', 'w'), git_common)
                return orig(path)
            self.w.classify = classify
            try:
                for text in ('git update-ref --stdin < cmds.txt', 'git -C "%s" update-ref --stdin < cmds.txt' % W.fwd(top)):
                    for env in (PM, {context.ROLE_ENV: 'plain'}):
                        with self.subTest(common=common, text=text, env=env):
                            d = self.refused(self.cmd(text, sub=False, env=env, cwd=top), 'backlog-raw-write')
                            self.assertUnsure(d, 'place')
                with self.subTest(common=common, case='a literal ref outside the store'):
                    self.allowed(self.cmd('git update-ref refs/heads/x %s' % OID, sub=False, env=PM, cwd=top))
            finally:
                del self.w.classify

    def test_an_unread_update_ref_where_the_kit_json_is_not_text(self):
        """A .kit.json that is not UTF-8 raises out of the config load (UnicodeDecodeError, no
        ConfigError, so no ctx.config_error): store_kind's fault is no ground to allow. The rule
        `heavy`, which reads the same config before this one, is stubbed out: its raise is the
        hook's fail-closed failure (kitlib.host.hookmain), which would hide this rule's answer."""
        with open(os.path.join(self.w.main, '.kit.json'), 'wb') as f:
            f.write(b'{"repo": {"name": "d\xff\xfeo"}}')
        with mock.patch('kitlib.guard.rules.heavy', lambda *a: None):
            for env in (PM, {context.ROLE_ENV: 'plain'}):
                with self.subTest(env=env):
                    d = self.refused(self.cmd('git -C "%s" update-ref --stdin < cmds.txt' % self.wt, sub=False,
                                              env=env), 'backlog-raw-write')
                    self.assertUnsure(d, '.kit.json does not read: UnicodeDecodeError')

    def test_a_subagent_is_refused_too(self):
        for args in ('update-ref refs/backlog/main %s' % OID, 'push origin refs/backlog/main'):
            with self.subTest(args):
                self.denied(self.git(args, env={}, sub=True))

    def test_the_owner_writes_it(self):
        self.allowed(self.git('update-ref refs/backlog/main %s' % OID, env={}))

    def test_reads_and_other_refs(self):
        for args in ('show-ref refs/backlog/main', 'log refs/backlog/main', 'fetch origin',
                     'fetch origin refs/backlog/main', 'push origin kit/wt', 'push origin backlog'):
            with self.subTest(args):
                self.allowed(self.git(args))


class BranchStoreRefs(Base):
    kit = {'repo': {'name': 'demo', 'push_block': True}, 'backlog': {'store': 'branch'}}

    def test_the_backlog_branch_is_the_store(self):
        self.denied(self.cmd('git -C "%s" branch -D backlog' % self.wt, sub=False, env=PM))
        for args in ('push origin backlog', 'push origin HEAD:backlog', 'push origin +x:refs/heads/backlog',
                     'branch -f backlog HEAD', 'update-ref refs/heads/backlog %s' % OID,
                     'update-ref refs/remotes/origin/backlog %s' % OID):
            with self.subTest(args):
                self.refused(self.cmd('git -C "%s" %s' % (self.wt, args), sub=False, env=PM),
                             'backlog-raw-write')

    def test_a_fetch_into_the_backlog_branch_by_its_short_name(self):
        # git's fetch reads a destination with no refs/ as a branch: backlog is refs/heads/backlog
        for args in ('fetch origin x:backlog', 'fetch origin +x:heads/backlog', 'fetch origin x:Backlog'):
            with self.subTest(args):
                self.refused(self.cmd('git -C "%s" %s' % (self.wt, args), sub=False, env=PM), 'backlog-raw-write')

    def test_other_branches(self):
        self.allowed(self.cmd('git -C "%s" push origin kit/wt' % self.wt, sub=False, env=PM))
        self.allowed(self.cmd('git -C "%s" branch --list backlog' % self.wt, sub=False, env=PM))


class FolderStore(Base):
    kit = {'repo': {'name': 'demo', 'push_block': True}, 'backlog': {'store': 'main'}}
    folder = ('docs', 'backlog')

    def target(self, *rest):
        return os.path.join(self.w.wt, *(self.folder + rest))

    def write(self, target, sub=True, env=None, agent_type='worker'):
        ev = event('Write', {'file_path': target, 'content': 'x'}, self.w.wt, sub, agent_type)
        return decide(ev, self.w.ctx(environ=env if env is not None else {}))

    def test_a_record_file_edit_is_a_raw_write(self):
        self.refused(self.write(self.target('X-1.md')), 'backlog-raw-write')
        self.refused(self.write(self.target('new', 'Y-2.md')), 'backlog-raw-write')
        self.refused(self.write(self.target('X-1.md'), sub=False, env=COORDINATOR), 'backlog-raw-write')
        self.refused(self.write(self.target('X-1.md'), sub=False, env=PM), 'backlog-raw-write')

    def test_a_shell_write_into_the_store(self):
        rel = '/'.join(self.folder)
        for text in ('echo x > %s/X-1.md' % rel, 'rm %s/X-1.md' % rel, 'cp a.md %s/' % rel):
            with self.subTest(text):
                self.refused(self.cmd(text), 'backlog-raw-write')

    def test_the_owner_edits_it(self):
        self.allowed(self.write(self.target('X-1.md'), sub=False, env={}))

    def test_beside_the_store(self):
        self.allowed(self.write(os.path.join(self.w.wt, 'docs', 'backlog.md')))
        self.allowed(self.write(os.path.join(self.w.wt, 'docs', 'other', 'X-1.md')))


class PathStore(FolderStore):
    kit = {'repo': {'name': 'demo', 'push_block': True}, 'backlog': {'store': 'kit/backlog'}}
    folder = ('kit', 'backlog')


class RefStoreFolder(Base):
    def test_a_ref_store_has_no_record_files(self):
        ev = event('Write', {'file_path': os.path.join(self.w.wt, 'docs', 'backlog', 'X-1.md'), 'content': 'x'},
                   self.w.wt, True, 'worker')
        self.assertIsNone(decide(ev, self.w.ctx(environ={})))


if __name__ == '__main__':
    unittest.main()
