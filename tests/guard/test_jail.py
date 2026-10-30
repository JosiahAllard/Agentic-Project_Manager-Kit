"""The write jail (docs/guard.md "Write jail"): an agent session writes only inside its own
checkout, the temp folder and the .kit.json "guard" data_roots; the kit-json rule keeps an agent
off the file that sets those roots. Real folders throughout, so symlinks, junctions and `..` are
resolved the way the guard resolves them."""
import json
import os
import shutil
import tempfile
import unittest

from kitlib import config as kconfig
from kitlib import guard
from kitlib.git import repo
from kitlib.guard import commands as cm
from kitlib.guard import context, rules
from kitlib.guard import shell as sh
from kitlib.lockmcp import PLUGIN_NAME

from . import world as W
from .claudehost import action, decide
from .world import bash, event, ps

P = PLUGIN_NAME + ':'                 # kitlib.agentname.PREFIX (tests/agentname pins it)
COORDINATOR = {context.ROLE_ENV: 'coordinator'}
OLD_MARK = {context.ROLE_ENV: 'conductor'}   # the coordinator's retired old mark: no longer held
PM = {context.ROLE_ENV: 'pm'}
DEEP = {context.ROLE_ENV: 'deep-worker'}
CONSULTANT = {context.ROLE_ENV: 'consultant'}
PLAIN = {context.ROLE_ENV: 'plain'}          # a launcher mark the jail does not hold by default


class JailWorld(W.World):
    """World plus a sibling worktree of the same main checkout and three data folders."""

    def __init__(self, data_roots=None):
        base_kit = {'repo': {'name': 'demo', 'push_block': True}}
        W.World.__init__(self, kit=base_kit)
        self.sib = os.path.join(self.main, '.claude', 'worktrees', 'sib')
        self.foreign = os.path.join(self.main, '.claude', 'worktrees', 'foreign')   # another repository's
        self.add = os.path.join(self.base, 'data', 'add')
        self.ro = os.path.join(self.base, 'data', 'ro')
        self.open = os.path.join(self.base, 'data', 'open')
        for d in (self.sib, self.foreign, self.add, self.ro, self.open):
            os.makedirs(d)
        roots = data_roots if data_roots is not None else [
            {'path': self.add, 'mode': 'add-only'},
            {'path': self.ro, 'mode': 'read-only'},
            {'path': self.open, 'mode': 'open'}]
        self.kit({'repo': {'name': 'demo', 'push_block': True}, 'guard': {'data_roots': roots}})

    def kit(self, data):
        with open(os.path.join(self.main, '.kit.json'), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def classify(self, path):
        p = os.path.normcase(os.path.abspath(path))
        if p.startswith(os.path.normcase(self.sib)):
            return repo.Place(repo.LINKED, self.sib, '', os.path.join(self.main, '.git', 'worktrees', 'sib'),
                              os.path.join(self.main, '.git'))
        if p.startswith(os.path.normcase(self.foreign)):
            other = os.path.join(self.base, 'other', '.git')
            return repo.Place(repo.LINKED, self.foreign, '', os.path.join(other, 'worktrees', 'foreign'), other)
        return W.World.classify(self, path)


def touch(path, text='x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def other_drive(path):
    here = os.path.splitdrive(path)[0].upper()
    return 'Q:\\' if here != 'Q:' else 'R:\\'


class Base(unittest.TestCase):
    def setUp(self):
        self.w = JailWorld()
        self.addCleanup(self.w.cleanup)

    def write(self, target, cwd=None, sub=True, env=None, tool='Write'):
        key = 'notebook_path' if tool == 'NotebookEdit' else 'file_path'
        ev = event(tool, {key: target, 'content': 'x', 'old_string': 'a', 'new_string': 'b'},
                   cwd or self.w.wt, sub)
        return decide(ev, self.w.ctx(environ=env or {}))

    def run_bash(self, text, cwd=None, sub=True, env=None):
        return decide(bash(text, cwd or self.w.wt, sub), self.w.ctx(environ=env or {}))

    def run_ps(self, text, cwd=None, sub=True, env=None):
        return decide(ps(text, cwd or self.w.wt, sub), self.w.ctx(environ=env or {}))

    def allowed(self, d):
        self.assertIsNone(d, d and d.reason)

    def jailed(self, d, rule='write-jail'):
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)
        return d


class Roots(Base):
    """Each root is open to an agent session; everything else is not."""

    def test_own_worktree(self):
        self.allowed(self.write(os.path.join(self.w.wt, 'src', 'a.py')))

    def test_temp_folder(self):
        self.allowed(self.write(os.path.join(self.w.temp, 'gate.log')))

    def test_scratch_folder_under_temp(self):
        self.allowed(self.write(os.path.join(self.w.temp, 'claude', 'proj', 'session', 'scratchpad', 'note.md')))

    def test_agents_temp_folder_when_named(self):
        agents = os.path.join(self.w.base, 'agenttemp')
        os.makedirs(agents)
        self.allowed(self.write(os.path.join(agents, 'x.txt'), env={'AGENT_TEMP': agents}))
        self.jailed(self.write(os.path.join(agents, 'x.txt')))

    def test_open_data_root(self):
        touch(os.path.join(self.w.open, 'old.bin'))
        self.allowed(self.write(os.path.join(self.w.open, 'old.bin')))

    def test_sibling_worktree(self):
        self.allowed(self.write(os.path.join(self.w.sib, 'a.py')))

    def test_main_checkout_for_a_session_that_may_edit_one(self):
        # a coordinator may edit a main checkout, but from its worktree the main checkout is outside
        self.jailed(self.write(os.path.join(self.w.main, 'README.md'), env=COORDINATOR, sub=False))

    def test_main_checkout_for_a_subagent_keeps_the_main_checkout_rule(self):
        self.jailed(self.write(os.path.join(self.w.main, 'README.md')), rule='main-checkout')

    def test_home_folder(self):
        self.jailed(self.write(os.path.join(self.w.home, 'notes.txt')))

    def test_other_drive(self):
        self.jailed(self.write(os.path.join(other_drive(self.w.base), 'elsewhere', 'x.txt')))

    def test_a_folder_outside_git(self):
        self.jailed(self.write(os.path.join(self.w.plain, 'x.txt')))

    def test_dotdot_is_normalised(self):
        escape = os.path.join(self.w.wt, 'src', '..', '..', '..', '..', '..', 'plain', 'x.txt')
        self.jailed(self.write(escape))
        self.allowed(self.write(os.path.join(self.w.wt, 'src', '..', 'docs', 'x.md')))

    def test_junction_escape(self):
        try:
            import _winapi
            _winapi.CreateJunction(self.w.plain, os.path.join(self.w.wt, 'link'))
        except (ImportError, AttributeError, OSError) as e:
            self.skipTest('no junction: %s' % e)
        self.jailed(self.write(os.path.join(self.w.wt, 'link', 'x.txt')))

    def test_symlink_escape(self):
        try:
            os.symlink(self.w.plain, os.path.join(self.w.wt, 'sym'), target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest('no symlink: %s' % e)
        self.jailed(self.write(os.path.join(self.w.wt, 'sym', 'x.txt')))

    def test_every_file_tool(self):
        for tool in ('Edit', 'MultiEdit', 'NotebookEdit'):
            with self.subTest(tool):
                self.jailed(self.write(os.path.join(self.w.plain, 'x.ipynb'), tool=tool))

    def test_the_message_names_the_roots_and_the_coordinator(self):
        d = self.jailed(self.write(os.path.join(self.w.plain, 'x.txt')))
        for root in (self.w.wt, self.w.temp, self.w.add, self.w.open):
            self.assertIn(W.fwd(root), d.reason)
        self.assertIn('ask the coordinator', d.reason.lower())
        self.assertTrue(d.reason.endswith(rules.tag('write-jail', 'subagent')), d.reason)
        self.assertIn('Instead: Write(file_path="%s/x.txt")' % W.fwd(self.w.wt), d.reason)


class Cwd(Base):
    """The session's own root comes from where it runs."""

    def test_a_session_in_the_main_checkout_has_the_whole_checkout(self):
        self.allowed(self.write(os.path.join(self.w.wt, 'a.py'), cwd=self.w.main))
        self.allowed(self.write(os.path.join(self.w.main, 'README.md'), cwd=self.w.main, env=COORDINATOR, sub=False))

    def test_outside_git_the_folder_it_runs_in(self):
        self.allowed(self.write(os.path.join(self.w.plain, 'x.txt'), cwd=self.w.plain))
        self.jailed(self.write(os.path.join(self.w.home, 'x.txt'), cwd=self.w.plain))

    def test_the_home_folder_is_never_a_root(self):
        self.jailed(self.write(os.path.join(self.w.home, 'x.txt'), cwd=self.w.home))


class Siblings(Base):
    """A session in a linked worktree reaches the other linked worktrees of its repository under
    the main checkout, as a session in the main checkout does (jail.sibling); nothing more."""

    def test_a_subagent_and_a_coordinator_write_a_sibling(self):
        self.allowed(self.write(os.path.join(self.w.sib, 'src', 'a.py')))
        self.allowed(self.write(os.path.join(self.w.sib, 'a.py'), env=COORDINATOR, sub=False))
        self.allowed(self.write(os.path.join(self.w.wt, 'a.py'), cwd=self.w.sib))

    def test_git_in_a_sibling(self):
        for text in ('git -C "%s" commit -m x' % W.fwd(self.w.sib), 'git -C "%s" add a.py' % W.fwd(self.w.sib),
                     'cd "%s" && git commit -m x' % W.fwd(self.w.sib)):
            with self.subTest(text):
                d = self.run_bash(text)
                if 'commit' in text:
                    self.jailed(d, 'git-plain-commit')      # the jail passes it; the commit is commit.py's
                else:
                    self.allowed(d)

    def test_the_main_checkout_stays_outside(self):
        self.jailed(self.write(os.path.join(self.w.main, 'README.md'), env=COORDINATOR, sub=False))
        self.jailed(self.write(os.path.join(self.w.main, '.claude', 'worktrees', 'x.txt'), env=COORDINATOR, sub=False))
        self.jailed(self.run_bash('git -C "%s" commit -m x' % W.fwd(self.w.main), env=COORDINATOR, sub=False))

    def test_another_repositorys_worktree_stays_outside(self):
        self.jailed(self.write(os.path.join(self.w.foreign, 'a.py')))
        self.jailed(self.run_bash('git -C "%s" commit -m x' % W.fwd(self.w.foreign)))

    def test_a_worktree_outside_the_main_checkout_stays_outside(self):
        # base/pmdata is a linked worktree of the same repository, but not under the main checkout
        self.jailed(self.write(os.path.join(self.w.data, 'a.md')))

    def test_a_link_out_of_a_sibling_stays_outside(self):
        try:
            import _winapi
            _winapi.CreateJunction(self.w.main, os.path.join(self.w.sib, 'to-main'))
            _winapi.CreateJunction(self.w.plain, os.path.join(self.w.sib, 'to-plain'))
        except (ImportError, AttributeError, OSError) as e:
            self.skipTest('no junction: %s' % e)
        self.jailed(self.write(os.path.join(self.w.sib, 'to-main', 'README.md'), env=COORDINATOR, sub=False))
        self.jailed(self.write(os.path.join(self.w.sib, 'to-plain', 'x.txt')))

    def test_a_session_outside_git_gets_no_sibling(self):
        self.jailed(self.write(os.path.join(self.w.sib, 'a.py'), cwd=self.w.plain))

    def test_git_worktree_add_and_clone_into_a_sibling(self):
        for text in ('git worktree add "%s" -b x' % W.fwd(os.path.join(self.w.sib, 'nested')),
                     'git clone https://example.invalid/r.git "%s"' % W.fwd(os.path.join(self.w.sib, 'r'))):
            with self.subTest(text):
                self.allowed(self.run_bash(text))
        self.jailed(self.run_bash('git worktree add "%s" -b x' % W.fwd(os.path.join(self.w.foreign, 'nested'))))

    def test_a_data_root_mode_inside_a_sibling_still_holds(self):
        ro, add = os.path.join(self.w.sib, 'ro'), os.path.join(self.w.sib, 'add')
        self.w.kit({'repo': {'name': 'demo', 'push_block': True},
                    'guard': {'data_roots': [{'path': ro, 'mode': 'read-only'}, {'path': add, 'mode': 'add-only'}]}})
        touch(os.path.join(add, 'old.md'))
        self.jailed(self.write(os.path.join(ro, 'x.md')))
        self.jailed(self.run_bash('git -C "%s" commit -m x' % W.fwd(ro)))
        self.jailed(self.write(os.path.join(add, 'old.md')))
        self.allowed(self.write(os.path.join(add, 'new.md')))
        self.allowed(self.write(os.path.join(self.w.sib, 'a.py')))

    def test_no_sibling_while_the_pm_data_worktree_cannot_be_told(self):
        # a marked main session is not stopped by the pm-data rule, so sibling itself refuses
        self.w.user_config({'pm_data': {'path': 'relative/pmdata'}})
        self.jailed(self.write(os.path.join(self.w.sib, 'a.py'), env=COORDINATOR, sub=False))
        self.allowed(self.write(os.path.join(self.w.wt, 'a.py'), env=COORDINATOR, sub=False))

    def test_a_sibling_holding_a_machine_config_folder_stays_outside(self):
        # the home folder is the sibling, so the machine config folder lies inside it
        ev = event('Write', {'file_path': os.path.join(self.w.sib, 'a.py'), 'content': 'x'}, self.w.wt, True)
        self.jailed(decide(ev, self.w.ctx(environ={}, home=self.w.sib)))
        ev = event('Write', {'file_path': os.path.join(self.w.wt, 'a.py'), 'content': 'x'}, self.w.wt, True)
        self.allowed(decide(ev, self.w.ctx(environ={}, home=self.w.sib)))

    def test_the_message_still_names_the_own_worktree(self):
        d = self.jailed(self.write(os.path.join(self.w.plain, 'x.txt')))
        self.assertIn('Instead: Write(file_path="%s/x.txt")' % W.fwd(self.w.wt), d.reason)


class Sessions(Base):
    """Agent sessions are the guard's (docs/guard.md "Session mark"): subagents and the marks in
    guard_agent_marks (default: coordinator, deep-worker, consultant, pm). The owner's
    unmarked session and other marks (plain, the retired old coordinator mark) are not jailed."""

    def outside(self, env, sub=False):
        return self.write(os.path.join(self.w.plain, 'x.txt'), env=env, sub=sub)

    def test_subagent(self):
        self.jailed(self.outside({}, sub=True))

    def test_coordinator(self):
        self.jailed(self.outside(COORDINATOR))
        self.allowed(self.outside(OLD_MARK))

    def test_the_owner_is_exempt(self):
        self.allowed(self.outside({}))

    def test_every_launcher_agent_mark_is_held_by_default(self):
        for env in (PM, DEEP, CONSULTANT, {context.ROLE_ENV: 'PM'}):
            with self.subTest(env):
                self.jailed(self.outside(env))

    def test_a_mark_not_listed_is_exempt(self):
        self.allowed(self.outside(PLAIN))
        self.allowed(self.outside({context.ROLE_ENV: 'strong-worker'}))     # start --agent <other>

    def test_a_plugin_prefixed_mark_is_the_bare_mark(self):
        # docs/guard.md "Agent names"
        for mark in (P + 'coordinator', P + 'deep-worker', P + 'consultant', P.upper() + 'pm', P + P + 'pm'):
            with self.subTest(mark):
                self.jailed(self.outside({context.ROLE_ENV: mark}))
        self.allowed(self.outside({context.ROLE_ENV: P + 'plain'}))
        self.allowed(self.outside({context.ROLE_ENV: P + 'strong-worker'}))

    def test_marks_from_the_machine_config(self):
        # the setting replaces the default list
        self.w.user_config({'guard_agent_marks': ['coordinator', 'plain']})
        self.jailed(self.outside(PLAIN))
        self.allowed(self.outside(PM))


class MarkRoots(Base):
    """The machine config's guard_mark_roots: more write roots for the main sessions of one
    launcher mark, never for a subagent, never on, over or in the machine config's folder
    (docs/guard.md "Write jail")."""

    def setUp(self):
        Base.setUp(self)
        self.notes = os.path.join(self.w.base, 'pmnotes')
        os.makedirs(self.notes)

    def roots(self, table):
        self.w.user_config({'guard_mark_roots': table})

    def test_a_mark_root_opens_a_folder_to_that_mark_only(self):
        target = os.path.join(self.notes, 'x.md')
        self.jailed(self.write(target, env=PM, sub=False))
        self.roots({'pm': [{'path': self.notes, 'mode': 'open', 'why': 'test'}]})
        self.allowed(self.write(target, env=PM, sub=False))
        self.allowed(self.write(target, env={context.ROLE_ENV: 'PM'}, sub=False))     # case ignored
        self.jailed(self.write(target, env=COORDINATOR, sub=False))
        self.jailed(self.write(target, env=DEEP, sub=False))
        self.jailed(self.write(target, env=PM, sub=True))       # a subagent of the PM's session
        self.allowed(self.run_bash('echo x > "%s/y.md"' % W.fwd(self.notes), env=PM, sub=False))

    def test_a_plugin_prefixed_mark_gets_the_bare_marks_roots(self):
        # docs/guard.md "Agent names": the roots are looked up by the mark without the prefix
        target = os.path.join(self.notes, 'x.md')
        self.roots({'pm': [{'path': self.notes, 'mode': 'open', 'why': 'test'}]})
        for mark in (P + 'pm', P.upper() + 'PM'):
            with self.subTest(mark):
                self.allowed(self.write(target, env={context.ROLE_ENV: mark}, sub=False))
                self.jailed(self.write(os.path.join(self.w.plain, 'x.md'), env={context.ROLE_ENV: mark}, sub=False))
        self.jailed(self.write(target, env={context.ROLE_ENV: P + 'coordinator'}, sub=False))

    def test_a_mark_root_keeps_its_mode(self):
        touch(os.path.join(self.notes, 'old.md'))
        self.roots({'pm': [{'path': self.notes, 'mode': 'add-only'}]})
        self.allowed(self.write(os.path.join(self.notes, 'new.md'), env=PM, sub=False))
        self.jailed(self.write(os.path.join(self.notes, 'old.md'), env=PM, sub=False))
        d = self.jailed(self.write(os.path.join(self.w.plain, 'x.md'), env=PM, sub=False))
        self.assertIn('"%s" (mark, add-only)' % W.fwd(self.notes), d.reason)

    def test_a_home_path_reads_from_the_home_folder(self):
        memory = os.path.join(self.w.home, 'projects', 'p', 'memory')
        os.makedirs(memory)
        self.roots({'pm': [{'path': '~/projects/p/memory', 'mode': 'open'}]})
        self.allowed(self.write(os.path.join(memory, 'MEMORY.md'), env=PM, sub=False))
        self.jailed(self.write(os.path.join(self.w.home, 'projects', 'other.md'), env=PM, sub=False))

    def test_no_root_on_over_or_in_the_machine_config_folder(self):
        cfg = os.path.join(self.w.home, kconfig.user.DIR_NAME)
        name = kconfig.user.DIR_NAME
        for path in ('~/', '~/' + name, '~/%s/sub' % name, self.w.home, self.w.base):
            with self.subTest(path):
                self.roots({'pm': [{'path': path, 'mode': 'open'}]})
                self.jailed(self.write(os.path.join(cfg, 'config.json'), env=PM, sub=False))
                self.jailed(self.write(os.path.join(cfg, 'sub', 'x.json'), env=PM, sub=False))
        # a sibling whose name only starts the same is not the folder
        self.roots({'pm': [{'path': '~/%s-notes' % name, 'mode': 'open'}]})
        self.allowed(self.write(os.path.join(self.w.home, name + '-notes', 'x.md'), env=PM, sub=False))

    def test_a_config_the_strict_reader_refuses_gives_no_mark_roots(self):
        for table in ({'pm': [{'path': self.notes, 'mode': 'open'}, {'path': 'relative', 'mode': 'open'}]},
                      {'pm': [{'path': self.notes, 'mode': 'write'}]},
                      {'owner': [], 'pm': [{'path': self.notes, 'mode': 'open'}]}):
            with self.subTest(table):
                self.roots(table)
                self.jailed(self.write(os.path.join(self.notes, 'x.md'), env=PM, sub=False))

    def test_a_mark_root_over_another_checkout_lets_that_mark_run_git_there(self):
        # the PM's red-merge reset in a repository's main checkout, from its own folder
        reset = 'git -C "%s" reset --keep %s' % (W.fwd(self.w.main), 'a' * 40)
        self.jailed(self.run_bash(reset, env=PM, sub=False))
        self.roots({'pm': [{'path': self.w.main, 'mode': 'open'}]})
        self.allowed(self.run_bash(reset, env=PM, sub=False))
        self.jailed(self.run_bash(reset, env=COORDINATOR, sub=False))

    def test_a_junction_into_the_lock_state_under_a_mark_root_is_still_lock_state(self):
        # the mark root holds the lock state's parent; a junction in the worktree reaches the state
        # folder by another spelling, so lock-state must judge the resolved path
        state = os.path.join(self.w.home, '.claude', 'machine-lock')
        os.makedirs(state)
        link = os.path.join(self.w.wt, 'lk')
        try:
            import _winapi
            _winapi.CreateJunction(state, link)
        except (ImportError, AttributeError, OSError) as e:
            self.skipTest('no junction: %s' % e)
        self.roots({'pm': [{'path': '~/.claude', 'mode': 'open'}]})
        self.jailed(self.write(os.path.join(link, 'state.json'), env=PM, sub=False), 'lock-state')
        self.jailed(self.run_bash('echo {} > "%s/state.json"' % W.fwd(link), env=PM, sub=False), 'lock-state')
        self.jailed(self.write(os.path.join(link, 'state.json'), env=COORDINATOR, sub=False), 'lock-state')

    def test_the_rules_before_the_jail_still_hold(self):
        self.roots({'pm': [{'path': self.w.main, 'mode': 'open'}, {'path': '~/.claude', 'mode': 'open'}]})
        self.jailed(self.write(os.path.join(self.w.main, '.kit.json'), env=PM, sub=False), 'kit-json')
        self.jailed(self.write(os.path.join(self.w.home, '.claude', 'machine-lock', 'state.json'), env=PM, sub=False),
                    'lock-state')


class DataRoots(Base):
    def test_add_only_takes_a_new_file(self):
        self.allowed(self.write(os.path.join(self.w.add, 'model-2.bin')))
        self.allowed(self.run_bash('cp out/model.bin "%s/model-3.bin"' % W.fwd(self.w.add)))
        self.allowed(self.run_bash('echo x > "%s/new.txt"' % W.fwd(self.w.add)))

    def test_add_only_takes_a_copy_into_the_folder(self):
        # a copy or move whose destination is the existing folder writes <folder>/<the source's name>
        add = W.fwd(self.w.add)
        for text in ('cp out/model.bin "%s/"' % add, 'cp out/model.bin "%s"' % add, 'cp -t "%s" out/model.bin' % add,
                     'mv out/model.bin "%s/"' % add, 'cp -r out/run7 "%s/"' % add):
            with self.subTest(text):
                self.allowed(self.run_bash(text))
        for text in ('Copy-Item out/model.bin "%s"' % add, 'Move-Item -Path out/model.bin -Destination "%s"' % add):
            with self.subTest(text):
                self.allowed(self.run_ps(text))

    def test_add_only_copy_into_the_folder_never_overwrites(self):
        touch(os.path.join(self.w.add, 'model-1.bin'))
        add = W.fwd(self.w.add)
        self.jailed(self.run_bash('cp out/model-1.bin "%s/"' % add))
        self.jailed(self.run_bash('cp out/new.bin out/model-1.bin "%s"' % add))
        self.jailed(self.run_ps('Copy-Item out/model-1.bin "%s"' % add))
        self.jailed(self.run_bash('cp -T out/run7 "%s"' % add))      # -T: the folder itself is the target
        self.jailed(self.run_bash('cp "$F" "%s/"' % add))              # a source it cannot name: the folder
        self.jailed(self.run_bash('cp out/*.bin "%s/"' % add))         # a wildcard: the folder
        self.jailed(self.run_bash('robocopy out "%s" new.bin' % add))  # docs/guard.md "Known limits"
        self.jailed(self.run_bash('cp out/new.bin "%s/"' % W.fwd(self.w.ro)))   # read-only stays read-only
        self.jailed(self.run_bash('cp other/.kit.json "%s/"' % W.fwd(self.w.wt)), rule='kit-json')

    def test_add_only_takes_a_link_or_an_install_into_the_folder(self):
        add = W.fwd(self.w.add)
        for text in ('ln -s out/new.bin "%s/"' % add, 'ln out/new.bin "%s"' % add, 'ln -st "%s" out/new.bin' % add,
                     'install out/new.bin "%s/"' % add, 'install -m 644 out/new.bin "%s"' % add,
                     'install -t "%s" out/new.bin' % add, 'cp -rt "%s" out/run7' % add,
                     'cp --target-dir="%s" out/new.bin' % add, 'cd "%s" && ln -s ../x/new.bin' % add):
            with self.subTest(text):
                self.allowed(self.run_bash(text))

    def test_add_only_target_directory_forms_never_overwrite(self):
        # -t in a cluster, -tDIR, --target-directory=DIR and its abbreviations name the folder
        touch(os.path.join(self.w.add, 'model-1.bin'))
        add = W.fwd(self.w.add)
        for text in ('cp -rt "%s" out/model-1.bin' % add, 'cp -at "%s" out/model-1.bin' % add,
                     'cp -t"%s" out/model-1.bin' % add, 'cp --target-directory="%s" out/model-1.bin' % add,
                     'cp --target-dir="%s" out/model-1.bin' % add, 'cp --target-d "%s" out/model-1.bin' % add,
                     'mv -vt "%s" out/model-1.bin' % add, 'ln -st "%s" out/model-1.bin' % add,
                     'ln --target-directory="%s" out/model-1.bin' % add, 'install -vt "%s" out/model-1.bin' % add,
                     'install -t"%s" out/model-1.bin' % add, 'ln -s out/model-1.bin "%s/"' % add,
                     'install out/model-1.bin "%s/"' % add, 'cd "%s" && ln -s ../x/model-1.bin' % add):
            with self.subTest(text):
                self.jailed(self.run_bash(text))

    def test_add_only_copy_options_off_the_list_judge_the_folder(self):
        # an option that can change what lands in the folder (--parents writes <add>/out/sub/x, a
        # backup renames the old file, an abbreviation of --no-target-directory) judges the folder
        add = W.fwd(self.w.add)
        for text in ('cp --parents out/sub/x "%s/"' % add, 'cp --par out/sub/x "%s/"' % add,
                     'cp --no-ta out/run7 "%s"' % add, 'cp --no-target-dir out/run7 "%s"' % add,
                     'mv --no-target out/run7 "%s"' % add, 'cp -b out/new.bin "%s/"' % add,
                     'cp --backup=numbered out/new.bin "%s/"' % add, 'cp -S .old out/new.bin "%s/"' % add,
                     'cp -f out/new.bin "%s/"' % add, 'cp -u out/new.bin "%s/"' % add,
                     'cp --bogus out/new.bin "%s/"' % add, 'cp --no out/new.bin "%s/"' % add,
                     'cp $OPT out/new.bin "%s/"' % add, 'cp -t "%s" -t "%s" out/new.bin' % (add, add),
                     'ln -sf out/new.bin "%s/"' % add,
                     'install -D out/new.bin "%s/"' % add, 'cp -- out/D:new.bin "%s/"' % add):
            with self.subTest(text):
                self.jailed(self.run_bash(text))

    def test_add_only_cmd_copy_and_move(self):
        touch(os.path.join(self.w.wt, 'out', 'new.bin'))
        touch(os.path.join(self.w.wt, 'out', 'model-1.bin'))
        touch(os.path.join(self.w.wt, 'out', 'run7', 'a.bin'))
        touch(os.path.join(self.w.add, 'model-1.bin'))
        add = self.w.add
        self.allowed(self.run_ps('cmd /c copy out\\new.bin "%s"' % add))
        self.allowed(self.run_ps('cmd /c move /y out\\new.bin "%s\\"' % add))
        self.jailed(self.run_ps('cmd /c copy out\\model-1.bin "%s"' % add))
        self.jailed(self.run_ps('cmd /c move out\\model-1.bin "%s"' % add))
        self.jailed(self.run_ps('cmd /c copy out\\run7 "%s"' % add))       # cmd copies the folder's files

    def test_add_only_never_overwrites_moves_or_deletes(self):
        old = os.path.join(self.w.add, 'model-1.bin')
        touch(old)
        fold = W.fwd(old)
        self.jailed(self.write(old))
        self.jailed(self.write(old, tool='Edit'))
        self.jailed(self.run_bash('rm "%s"' % fold))
        self.jailed(self.run_bash('mv "%s" "%s/model-9.bin"' % (fold, W.fwd(self.w.add))))
        self.jailed(self.run_bash('echo x >> "%s"' % fold))
        self.jailed(self.run_ps('Remove-Item -Recurse "%s"' % W.fwd(self.w.add)))
        self.jailed(self.run_ps('Set-Content -Path "%s" -Value x' % fold))

    def test_read_only(self):
        self.jailed(self.write(os.path.join(self.w.ro, 'new.txt')))
        self.jailed(self.run_bash('touch "%s/new.txt"' % W.fwd(self.w.ro)))

    def test_read_only_wins_inside_another_root(self):
        inner = os.path.join(self.w.temp, 'frozen')
        os.makedirs(inner)
        self.w.kit({'repo': {'name': 'demo'}, 'guard': {'data_roots': [{'path': inner, 'mode': 'read-only'}]}})
        self.jailed(self.write(os.path.join(inner, 'x.txt')))
        self.allowed(self.write(os.path.join(self.w.temp, 'x.txt')))

    def test_a_worktree_kit_json_names_no_root(self):
        touch(os.path.join(self.w.wt, '.kit.json'),
              json.dumps({'guard': {'data_roots': [{'path': self.w.plain, 'mode': 'open'}]}}))
        self.jailed(self.write(os.path.join(self.w.plain, 'x.txt')))

    def test_exempt_sessions_ignore_modes(self):
        old = os.path.join(self.w.add, 'model-1.bin')
        touch(old)
        self.allowed(self.write(old, sub=False))


class Config(unittest.TestCase):
    """guard.data_roots in .kit.json: entries {path, mode}, path absolute, an inline object only."""

    def load(self, guard):
        w = W.World(kit={'repo': {'name': 'demo'}, 'guard': guard})
        self.addCleanup(w.cleanup)
        return kconfig.load(w.main)

    def test_default_is_empty(self):
        w = W.World()
        self.addCleanup(w.cleanup)
        self.assertEqual(kconfig.load(w.main).section('guard')['data_roots'], [])

    def test_entries(self):
        roots = [{'path': 'D:\\data\\models', 'mode': 'add-only'}, {'path': '//host/share/x', 'mode': 'read-only'},
                 {'path': 'E:/scratch', 'mode': 'open', 'why': 'test'}]
        self.assertEqual(self.load({'data_roots': roots}).section('guard')['data_roots'], roots)

    def test_bad_entries_refuse(self):
        for bad in ([{'path': 'D:/x', 'mode': 'append'}], [{'path': 'data/models', 'mode': 'open'}],
                    [{'mode': 'open'}], [{'path': 'D:/x'}], [{'path': 'D:/x', 'mode': 'open', 'extra': 1}]):
            with self.subTest(bad):
                with self.assertRaises(kconfig.ConfigError):
                    self.load({'data_roots': bad})

    def test_not_a_section_file(self):
        with self.assertRaises(kconfig.ConfigError):
            self.load('tools/guard.json')


class ShellForms(Base):
    """The parsed write targets the jail judges (docs/guard.md "Shell writes")."""

    def out(self, name='x.txt'):
        return W.fwd(os.path.join(self.w.plain, name))

    def test_bash_writers(self):
        for text in ('echo hi > "%s"' % self.out(), 'rm -rf "%s"' % self.out('d'), 'mv a.txt "%s"' % self.out(),
                     'cp a.txt "%s"' % self.out(), 'sed -i s/a/b/ "%s"' % self.out(), 'touch "%s"' % self.out(),
                     'cd "%s" && rm x.txt' % W.fwd(self.w.plain)):
            with self.subTest(text):
                self.jailed(self.run_bash(text))

    def test_awk_in_place_writes_its_files(self):
        # gawk's inplace library in each spelling makes every file operand a target; so does a
        # file -d, -o or -p names. -v inplace::enable=0 is not read: its files still count
        out, inside = self.out(), W.fwd(os.path.join(self.w.wt, 'x.txt'))
        forms = ("gawk -i inplace '{print}' \"%s\"", "awk -iinplace '{print}' \"%s\"",
                 "gawk --include=inplace '{print}' \"%s\"", "gawk --include inplace '{print}' \"%s\"",
                 "gawk --incl=inplace.awk '{print}' \"%s\"", "gawk -W include=inplace '{print}' \"%s\"",
                 "gawk -W include inplace '{print}' \"%s\"", "gawk -ni inplace '{print}' \"%s\"",
                 "gawk -f inplace -e '{print}' \"%s\"", "gawk -e '@include \"inplace\"' -e '{print}' \"%s\"",
                 "gawk '@include \"inplace\"; {print}' \"%s\"", "gawk -i inplace -- '{print}' a.txt \"%s\"",
                 "gawk -v inplace::enable=0 -i inplace '{print}' \"%s\"", "gawk -i \"$L\" '{print}' \"%s\"",
                 "gawk -d\"%s\" '{print}' a.txt", "gawk --profile=\"%s\" '{print}' a.txt",
                 "gawk -o\"%s\" '{print}' a.txt")
        for form in forms:
            with self.subTest(form):
                self.jailed(self.run_bash(form % out))
                self.allowed(self.run_bash(form % inside))
        # an assignment and stdin are no files: from the main checkout, where a relative x=1 or -
        # would be a main-checkout write, the in-worktree file alone is judged
        for extra in ('x=1', '-'):
            with self.subTest(extra):
                self.allowed(self.run_bash("gawk -i inplace '{print}' %s \"%s\"" % (extra, inside), cwd=self.w.main))
        # -d, -o or -p with no file writes awkvars.out or awkprof.out where it runs
        for opt in ('-d', '-o', '-p', '--dump-variables', '--profile'):
            with self.subTest(opt):
                self.jailed(self.run_bash("gawk %s '{print}' a.txt" % opt, cwd=self.w.main), 'main-checkout')
                self.allowed(self.run_bash("gawk %s '{print}' a.txt" % opt))

    def test_awk_that_does_not_edit_in_place_writes_nothing(self):
        out = self.out()
        for text in ("awk '{print}' \"%s\"" % out, "gawk -F: -v x=1 '{print $1}' \"%s\"" % out,
                     "gawk -f prog.awk \"%s\"" % out, "gawk -i lib '{print}' \"%s\"" % out,
                     "gawk -v inplace::enable=0 '{print}' \"%s\"" % out, "gawk -E prog.awk -i inplace \"%s\"" % out):
            with self.subTest(text):
                self.allowed(self.run_bash(text))

    def test_an_in_place_target_the_line_computes_is_unplaced(self):
        # from a main checkout a computed target is a write the guard cannot place: rule unplaced,
        # which warns by default (modes.DEFAULTS); from a worktree it is not judged
        from unittest import mock
        from kitlib import guard
        from kitlib.guard import modes
        from .claudehost import action
        for text in ('gawk -i inplace \'{print}\' "$F"', 'sed -i s/a/b/ "$F"', 'ruby -i -pe 1 "$F"',
                     'dos2unix "$F"'):
            with self.subTest(text):
                self.jailed(self.run_bash(text, cwd=self.w.main), 'unplaced')
                with mock.patch.dict(modes.DEFAULTS, {'unplaced': modes.WARN}):
                    d = guard.decide(action(bash(text, self.w.main, True)), self.w.ctx(environ={}))
                self.assertEqual((d.verdict, d.rule), ('allow', rules.WARNED), d.reason)
                self.allowed(self.run_bash(text))

    def test_other_in_place_editors(self):
        # ruby -i as perl -i, under its -C folder; dos2unix and its kin, sponge; vi, vim, ex and
        # ed write every file operand (an operand-only read is refused too: accepted strictness)
        out, inside = self.out(), W.fwd(os.path.join(self.w.wt, 'x.txt'))
        forms = ('ruby -i -pe \'sub(/a/, "b")\' "%s"', 'ruby -pi.bak -e 1 "%s"', 'ruby -i edit.rb "%s"',
                 'dos2unix "%s"', 'unix2dos -q "%s"', 'mac2unix "%s"', 'unix2mac "%s"', 'echo x | sponge "%s"',
                 'vim -es -c \'%%s/a/b/|x\' "%s"', 'vi -c wq "%s"', 'ex -sc \'wq\' "%s"', 'nvim --cmd x +wq "%s"',
                 'ed -s "%s"', 'ed -p ">" "%s"', 'vim -es -c q "%s"')
        for form in forms:
            with self.subTest(form):
                self.jailed(self.run_bash(form % out))
                self.allowed(self.run_bash(form % inside))
        self.jailed(self.run_bash('ruby -C "%s" -i -pe 1 x.txt' % W.fwd(self.w.plain)))
        self.allowed(self.run_bash('ruby -C sub -i -pe 1 x.txt'))
        self.jailed(self.run_bash('ruby -x%s -i -pe 1 x.txt' % W.fwd(self.w.plain)))
        self.allowed(self.run_bash('ruby -xsub -i -pe 1 x.txt'))
        self.jailed(self.run_bash('ruby tool.rb -i "%s"' % out))     # options after the script are read, as perl
        self.allowed(self.run_bash('ruby -pe 1 "%s"' % out))                # no -i: reads
        self.jailed(self.run_bash('vim -es -w "%s" -c q a.txt' % out))       # -w's script output

    def test_copy_target_directory_forms_judge_the_directory(self):
        # the directory a -t cluster, -tDIR or --target-directory[=] (or an abbreviation) names is
        # what cp, mv, ln and install write, not the source
        d = W.fwd(self.w.plain)
        for text in ('cp -rt "%s" a.txt' % d, 'cp -at "%s" a.txt' % d, 'cp -t"%s" a.txt' % d, 'cp -t "%s" a.txt' % d,
                     'cp --target-directory="%s" a.txt' % d, 'cp --target-directory "%s" a.txt' % d,
                     'cp --target-dir="%s" a.txt' % d, 'cp --target "%s" a.txt' % d, 'mv -ft "%s" a.txt' % d,
                     'mv --target-d="%s" a.txt' % d, 'ln -sft "%s" a.txt' % d, 'ln -t"%s" a.txt' % d,
                     'ln --target-directory="%s" a.txt' % d, 'install -Dt "%s" a.txt' % d,
                     'install -m644 -t"%s" a.txt' % d, 'install --target-directory="%s" a.txt' % d,
                     'cp --bogus "%s" a.txt' % d):
            with self.subTest(text):
                self.jailed(self.run_bash(text))
        self.allowed(self.run_bash('cp -rt out a.txt'))
        self.allowed(self.run_bash('cp -S "%s" a.txt b.txt' % d))         # -S takes a suffix: b.txt is written

    def test_copy_end_of_options_and_a_trailing_target_option(self):
        # after `--` a word that looks like -tDIR is an operand, so the last operand is written;
        # -t or --target-directory with no word after it names no directory
        d = W.fwd(self.w.plain)
        for text in ('cp -- -tout "%s/x.txt"' % d, 'mv -- -tout "%s/x.txt"' % d, 'ln -s -- -tout "%s/x"' % d,
                     'cp a.txt "%s/x.txt" -t' % d, 'ln -s a.txt "%s/x" --target-directory' % d):
            with self.subTest(text):
                self.jailed(self.run_bash(text))
        self.allowed(self.run_bash('cp -t out -- "%s/a.txt"' % d))
        self.allowed(self.run_bash('cp -- out/new.bin "%s/"' % W.fwd(self.w.add)))   # `--` is no option

    def test_find_exec_writer_is_judged_where_find_searches(self):
        # {} under -exec, -execdir, -ok or -okdir is a path find found under its start paths, so a
        # writer given it (or a target the line computes, or one -execdir places in a found
        # file's folder) writes there, as -delete does
        d, sq = W.fwd(self.w.plain), "';'"
        for text in ('find "%s" -name "*.tmp" -exec rm {} +' % d, 'find "%s" -ok rm {} %s' % (d, sq),
                     'find "%s" -execdir rm {} %s' % (d, sq), 'find "%s" -okdir rm -f {} %s' % (d, sq),
                     'find "%s" -exec cp a.txt {}/x %s' % (d, sq), 'find "%s" -exec touch "$X" %s' % (d, sq),
                     "find \"%s\" -exec sh -c 'rm \"$1\"' _ {} %s" % (d, sq),
                     'find -L "%s" . -exec rm {} +' % d, 'find . "%s" -exec rm {} +' % d,
                     'find "%s" -delete' % d):
            with self.subTest(text):
                self.jailed(self.run_bash(text))
        # a find nested in find's sh -c is refused before it is placed: its text is not literal
        self.jailed(self.run_bash("find \"%s\" -exec sh -c 'find \"$1\" -exec rm {} +' _ {} %s" % (d, sq)), 'unreadable')
        for text in ('find . -name "*.tmp" -exec rm {} +', 'find "%s" -exec rm {} +' % W.fwd(self.w.temp),
                     'find "%s" -exec rm {} +' % W.fwd(self.w.wt), 'find "%s" -exec grep -l x {} +' % d,
                     'find "%s" -exec wc -l {} %s' % (d, sq), 'find "%s" -exec cp {} . %s' % (d, sq)):
            with self.subTest(text):
                self.allowed(self.run_bash(text))

    def test_find_exec_writer_alternative_searches_the_worktree(self):
        d = self.jailed(self.run_bash('find "%s" -name "*.tmp" -exec rm {} +' % W.fwd(self.w.plain)))
        self.assertIn('Instead: find "%s/plain" -name "*.tmp" -exec rm {} +' % W.fwd(self.w.wt), d.reason)

    def test_powershell_writers(self):
        for text in ('Remove-Item -Recurse -Force "%s"' % self.out('d'), 'Set-Content -Path "%s" -Value x' % self.out(),
                     '"x" | Out-File "%s"' % self.out(), 'Copy-Item a.txt "%s"' % self.out()):
            with self.subTest(text):
                self.jailed(self.run_ps(text))

    def test_git_dash_c(self):
        for text in ('git -C "%s" commit -m x' % W.fwd(self.w.plain), 'git -C "%s" reset --keep HEAD~1' % W.fwd(self.w.plain),
                     'git -C "%s" checkout -b x' % W.fwd(self.w.plain), 'git -C "%s" add a.py' % W.fwd(self.w.plain),
                     'cd "%s" && git commit -m x' % W.fwd(self.w.plain)):
            with self.subTest(text):
                self.jailed(self.run_bash(text))

    def test_git_judged_by_the_folder_it_makes(self):
        self.jailed(self.run_bash('git worktree add "%s" -b x' % W.fwd(os.path.join(self.w.plain, 'wt2'))))
        self.jailed(self.run_bash('git clone https://example.invalid/r.git "%s"' % W.fwd(os.path.join(self.w.plain, 'r'))))
        self.allowed(self.run_bash('git worktree add "%s" -b x' % W.fwd(os.path.join(self.w.temp, 'wt2'))))
        self.allowed(self.run_bash('git clone https://example.invalid/r.git "%s"' % W.fwd(os.path.join(self.w.temp, 'r'))))

    def test_git_reads_anywhere(self):
        for text in ('git -C "%s" status' % W.fwd(self.w.sib), 'git -C "%s" log --oneline -3' % W.fwd(self.w.plain),
                     'git -C "%s" diff main' % W.fwd(self.w.sib), 'git -C "%s" worktree list' % W.fwd(self.w.sib),
                     'git -C "%s" branch -a' % W.fwd(self.w.sib), 'git -C "%s" stash list' % W.fwd(self.w.sib)):
            with self.subTest(text):
                self.allowed(self.run_bash(text))

    def test_git_in_own_worktree(self):
        self.jailed(self.run_bash('git commit -m x'), 'git-plain-commit')     # the jail passes it
        self.allowed(self.run_bash('git -C "%s" add a.py' % W.fwd(self.w.wt)))

    def test_writes_inside_the_roots(self):
        for text in ('echo hi > out.txt', 'rm -rf build', 'mkdir -p "%s/run"' % W.fwd(self.w.temp),
                     'echo x > /tmp/kit-x.log', 'python tools/x.py > "%s/x.log" 2>&1' % W.fwd(self.w.temp)):
            with self.subTest(text):
                self.allowed(self.run_bash(text))
        self.allowed(self.run_ps('Set-Content -Path out.txt -Value x'))

    def test_bash_tmp_is_the_agents_temp_folder_when_named(self):
        # /tmp stands for AGENT_TEMP when it is set, so a junction there out of the roots is judged
        agents = os.path.join(self.w.base, 'agenttemp')
        os.makedirs(agents)
        try:
            import _winapi
            _winapi.CreateJunction(self.w.plain, os.path.join(agents, 'esc'))
        except (ImportError, AttributeError, OSError) as e:
            self.skipTest('no junction: %s' % e)
        env = {'AGENT_TEMP': agents}
        self.allowed(self.run_bash('echo x > /tmp/kit-x.log', env=env))
        self.jailed(self.run_bash('echo x > /tmp/esc/x.log', env=env))

    def test_the_alternative_is_the_same_command_in_the_worktree(self):
        d = self.jailed(self.run_bash('echo hi > "%s"' % self.out()))
        self.assertIn('Instead: echo hi > "%s/x.txt"' % W.fwd(self.w.wt), d.reason)
        d = self.jailed(self.run_bash('git -C "%s" rm -q x' % W.fwd(self.w.plain)))
        self.assertIn('Instead: git -C "%s" rm -q x' % W.fwd(self.w.wt), d.reason)
        # a commit: plain git in the worktree is refused too (git-plain-commit), so the kit's commit tool there
        d = self.jailed(self.run_bash('git -C "%s" commit -m x' % W.fwd(self.w.plain)))
        self.assertIn('Instead: python %s -C "%s" -m "<subject>" --model "<your model>" <each path you changed>'
                      % (rules.kit_tool(self.w.ctx(), 'commit'), W.fwd(self.w.wt)), d.reason)


class MainGit(Base):
    """A repo whose .kit.json lets the lander role edit a main checkout; git run as a role."""

    def setUp(self):
        Base.setUp(self)
        self.roles({'lander': {'edit_main_checkout': True, 'why': 'test'}})
        self.main = W.fwd(self.w.main)

    def roles(self, table):
        self.w.kit({'repo': {'name': 'demo', 'push_block': True}, 'roles': table})

    def git(self, text, agent_type='lander', tool='Bash'):
        ev = event(tool, {'command': text, 'description': 'test'}, self.w.wt, True, agent_type)
        return decide(ev, self.w.ctx(environ={}))

    def denied(self, d):
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual(d.verdict, 'deny', d.reason)

    def each_denied(self, texts, tool='Bash'):
        for text in texts:
            with self.subTest(text):
                self.denied(self.git(text, tool=tool))


class LanderMainCheckout(MainGit):
    """The lander role, and no other, may run exactly `git fetch <remote> <main>` and `git merge
    --ff-only <remote>/<main>` in the main checkout from its worktree; nothing else it writes
    there passes (docs/guard.md "Write jail")."""

    def test_a_lander_may_fetch_and_merge_main_from_its_worktree(self):
        m = self.main
        for text in ('git -C "%s" fetch origin main' % m, 'git -C "%s" merge --ff-only origin/main' % m,
                     'cd "%s" && git fetch origin main' % m, 'cd "%s" && git merge --ff-only origin/main' % m,
                     'git.exe -C "%s" fetch origin main' % m):
            with self.subTest(text):
                self.allowed(self.git(text))
        for text in ('git -C "%s" fetch origin main' % m, '& git -C "%s" merge --ff-only origin/main' % m,
                     '& git.exe -C "%s" fetch origin main' % m, 'git.exe -C "%s" merge --ff-only origin/main' % m):
            with self.subTest(text):
                self.allowed(self.git(text, tool='PowerShell'))

    def test_a_lander_may_fetch_the_whole_remote(self):
        # docs/guard.md "Write jail", git bullet: `git fetch <remote>` writes only remote-tracking refs and FETCH_HEAD
        m = self.main
        for text in ('git -C "%s" fetch origin' % m, 'cd "%s" && git fetch origin' % m,
                     'cd "%s"; git fetch origin' % m):
            with self.subTest(text):
                self.allowed(self.git(text))
        self.allowed(self.git('& git -C "%s" fetch origin' % m, tool='PowerShell'))
        self.allowed(self.git('Set-Location "%s"; git fetch origin' % m, tool='PowerShell'))

    def test_the_powershell_landing_forms(self):
        m = self.main
        for text in ('Set-Location "%s"; git merge --ff-only origin/main' % m,
                     'Set-Location -Path "%s"; git fetch origin main' % m,
                     'cd "%s"; & git fetch origin' % m,
                     'cd "%s" && git fetch origin main' % m,           # PowerShell 7's &&
                     'pushd "%s"; git merge --ff-only origin/main' % m,
                     'Push-Location "%s"; git fetch origin' % m):
            with self.subTest(text):
                self.allowed(self.git(text, tool='PowerShell'))
        for text in ('Set-Location "%s"; git fetch origin main; git status' % m,
                     'Set-Location "%s" *> sl.log; git fetch origin main' % m,
                     'Set-Location "%s"; git fetch origin main | Out-File f.log' % m):
            with self.subTest(text):
                d = self.jailed(self.git(text, tool='PowerShell'))
                self.assertIn('git is not alone on the line', d.reason)
        # a piped Set-Location leaves where git runs unknown: refused before the landing tests
        self.each_denied(['Set-Location "%s" | Out-Null; git fetch origin main' % m], tool='PowerShell')
        self.jailed(self.git('git -C "%s" fetch origin' % m, 'worker'))

    def test_a_lander_verb_must_be_alone_on_its_line(self):
        m = self.main
        for text in ('git -C "%s" fetch origin main && git -C "%s" status' % (m, m),
                     'git -C "%s" status; git -C "%s" merge --ff-only origin/main' % (m, m),
                     'echo go && git -C "%s" fetch origin' % m,
                     'cd "%s" && echo x && git fetch origin main' % m,
                     'git -C "%s" fetch origin main | tee x.log' % m,
                     'git -C "%s" fetch origin main || true' % m,
                     '(git -C "%s" fetch origin main)' % m,
                     'bash -c \'git -C "%s" fetch origin main\'' % m,
                     'cd "%s" && git -C "%s" fetch origin main' % (W.fwd(self.w.wt), m),
                     'cd "%s" && cd . && git fetch origin main' % m,
                     'cd "%s" > cd.log && git fetch origin main' % m,       # a redirect on the cd
                     'cd "%s" 2>/dev/null; git fetch origin main' % m):
            with self.subTest(text):
                d = self.jailed(self.git(text))
                self.assertIn('git is not alone on the line', d.reason)
        self.each_denied(['cd "%s" || git fetch origin main' % m])     # where git runs is unknown
        self.each_denied(['git -C "%s" fetch origin main; Remove-Item x' % m,
                          'Set-Location "%s"; git status; git fetch origin main' % m], tool='PowerShell')
        # the lander's own worktree is its root: a fetch there needs no landing form
        self.allowed(self.git('cd "%s" && git fetch origin main && git status' % W.fwd(self.w.wt)))

    def test_a_refusal_names_the_test_that_failed(self):
        m = self.main
        cases = (('git -C "%s" fetch origin main && echo x' % m, 'write-jail', 'git is not alone on the line',
                  'git -C "%s" fetch origin main' % m),
                 ('git -C "%s" fetch --prune origin main' % m, 'write-jail', 'its arguments are not exactly one of',
                  'git -C "%s" fetch origin main' % m),
                 ('git -C "%s" merge --no-ff origin/main' % m, 'write-jail', 'its arguments are not exactly one of',
                  'git -C "%s" merge --ff-only origin/main' % m),
                 ('git -c x.y=z -C "%s" fetch origin main' % m, 'write-jail', 'a wrapper, an environment assignment',
                  'git -C "%s" fetch origin main' % m))
        for text, rule, why, instead in cases:
            with self.subTest(text):
                d = self.jailed(self.git(text), rule)
                self.assertIn(why, d.reason)
                self.assertIn('A lander may run only git fetch origin, git fetch origin main or git merge --ff-only '
                              'origin/main there', d.reason)
                self.assertIn(' Instead: %s [kit guard: rule=%s role=subagent]' % (instead, rule), d.reason)
        d = self.jailed(self.git('git -C "%s" commit -m x' % m))
        self.assertNotIn('landing step', d.reason)          # not a landing verb: the jail's own refusal

    def test_the_refusal_names_the_role_setting_when_the_repo_has_none(self):
        # a repository whose .kit.json does not let the lander write its main checkout: the cause is
        # roles.lander.edit_main_checkout, and the refusal says so (fetch: the jail; merge: main history)
        self.w.kit({'repo': {'name': 'demo', 'push_block': True}})
        kit = 'python -m json.tool "%s/.kit.json"' % self.main
        for text, rule in (('git -C "%s" fetch origin main' % self.main, 'write-jail'),
                           ('git -C "%s" fetch origin' % self.main, 'write-jail'),
                           ('git -C "%s" merge --ff-only origin/main' % self.main, 'git-main-history')):
            with self.subTest(text):
                d = self.jailed(self.git(text), rule)
                self.assertIn('does not set roles.lander.edit_main_checkout to true', d.reason)
                self.assertIn(' Instead: %s [kit guard:' % kit, d.reason)
        d = self.jailed(self.git('git -C "%s" merge --ff-only origin/main' % self.main, 'worker'), 'git-main-history')
        self.assertNotIn('roles.lander', d.reason)          # another role: the rule's own refusal

    def test_without_the_role_a_malformed_landing_step_still_names_the_role(self):
        # every landing form is refused until the role is set, so the role is named first: a refusal
        # that named the arguments would offer an Instead that is refused too
        self.w.kit({'repo': {'name': 'demo', 'push_block': True}})
        kit = 'python -m json.tool "%s/.kit.json"' % self.main
        for text, rule in (('git -C "%s" merge feature' % self.main, 'git-main-history'),
                           ('git -C "%s" merge --no-ff origin/main' % self.main, 'git-main-history'),
                           ('git -C "%s" fetch --all' % self.main, 'write-jail'),
                           ('git -C "%s" fetch origin main && echo x' % self.main, 'write-jail'),
                           ('git -c x.y=z -C "%s" fetch origin' % self.main, 'write-jail')):
            with self.subTest(text):
                d = self.jailed(self.git(text), rule)
                self.assertIn('does not set roles.lander.edit_main_checkout to true', d.reason)
                self.assertIn(' Instead: %s [kit guard:' % kit, d.reason)

    def test_the_remote_and_main_come_from_the_config(self):
        self.w.kit({'repo': {'name': 'demo', 'push_block': True, 'remote': 'upstream', 'main_branch': 'trunk'},
                    'roles': {'lander': {'edit_main_checkout': True, 'why': 'test'}}})
        self.allowed(self.git('git -C "%s" fetch upstream trunk' % self.main))
        self.allowed(self.git('git -C "%s" merge --ff-only upstream/trunk' % self.main))
        self.each_denied(['git -C "%s" fetch origin main' % self.main,
                          'git -C "%s" merge --ff-only origin/main' % self.main])

    def test_a_lander_fetches_nothing_but_the_remote_or_its_main(self):
        m = self.main
        self.each_denied(['git -C "%s" fetch' % m, 'git -C "%s" fetch --all' % m,
                          'git -C "%s" fetch origin other' % m, 'git -C "%s" fetch other main' % m,
                          'git -C "%s" fetch origin main:main' % m, 'git -C "%s" fetch origin +main' % m,
                          'git -C "%s" fetch origin +main:refs/heads/main' % m,
                          'git -C "%s" fetch origin main other' % m, 'git -C "%s" fetch --prune origin main' % m,
                          'git -C "%s" fetch origin main --prune' % m,
                          'git -C "%s" fetch --upload-pack=x origin main' % m,
                          'git -C "%s" fetch --update-head-ok origin main' % m,
                          'git -C "%s" fetch origin "$B"' % m])
        self.each_denied(['git -C "%s" fetch' % m, '& git -C "%s" fetch origin main:main' % m,
                          'git.exe -C "%s" fetch --prune origin main' % m], tool='PowerShell')

    def test_a_lander_merges_nothing_but_a_fast_forward_to_the_remote_main(self):
        m = self.main
        self.each_denied(['git -C "%s" merge origin/main' % m, 'git -C "%s" merge --no-ff origin/main' % m,
                          'git -C "%s" merge --ff-only -s ours origin/main' % m,
                          'git -C "%s" merge --ff-only -X theirs origin/main' % m,
                          'git -C "%s" merge --squash origin/main' % m, 'git -C "%s" merge --ff-only origin/other' % m,
                          'git -C "%s" merge --ff-only main' % m, 'git -C "%s" merge --ff-only FETCH_HEAD' % m,
                          'git -C "%s" merge --ff-only origin/main other' % m,
                          'git -C "%s" merge --ff-only --no-verify origin/main' % m])
        self.each_denied(['& git -C "%s" merge --no-ff origin/main' % m,
                          'git.exe -C "%s" merge -s ours origin/main' % m], tool='PowerShell')

    def test_a_lander_main_git_takes_no_git_option_or_environment(self):
        m = self.main
        self.each_denied(['git -c x.y=z -C "%s" fetch origin main' % m,
                          'git -C "%s" -c core.hooksPath=h merge --ff-only origin/main' % m,
                          'git --config-env=x.y=V -C "%s" fetch origin main' % m,
                          'git --no-pager -C "%s" fetch origin main' % m,
                          'git -C "%s" -C . fetch origin main' % m,
                          'git --git-dir="%s/.git" fetch origin main' % m,
                          'git --git-dir="%s/.git" --work-tree="%s" merge --ff-only origin/main' % (m, m),
                          'GIT_DIR="%s/.git" git fetch origin main' % m,
                          'GIT_DIR="%s/.git" GIT_WORK_TREE="%s" git merge --ff-only origin/main' % (m, m),
                          'export GIT_DIR="%s/.git"; git fetch origin main' % m,
                          'env FOO=1 git -C "%s" fetch origin main' % m,
                          'FOO=1 git -C "%s" fetch origin main' % m,
                          'git -C "$M" fetch origin main'])
        self.each_denied(['& git -c x.y=z -C "%s" fetch origin main' % m,
                          'git.exe --git-dir="%s/.git" fetch origin main' % m,
                          '$env:GIT_DIR = "%s/.git"; git fetch origin main' % m,
                          '$env:GIT_WORK_TREE = "%s"; & git merge --ff-only origin/main' % m], tool='PowerShell')

    def test_a_lander_writes_nothing_else_in_main(self):
        for text in ('git -C "%s" commit -m x' % self.main, 'git -C "%s" reset --keep HEAD~1' % self.main,
                     'git -C "%s" pull' % self.main, 'git -C "%s" cherry-pick abc123' % self.main):
            with self.subTest(text):
                self.jailed(self.git(text))
        self.denied(self.git('git -C "%s" checkout -b x' % self.main))
        ev = event('Write', {'file_path': os.path.join(self.w.main, 'README.md'), 'content': 'x'}, self.w.wt, True,
                   'lander')
        self.jailed(decide(ev, self.w.ctx(environ={})))
        self.jailed(self.git('echo x > "%s/out.txt"' % self.main))

    def test_a_lander_fetches_only_in_a_main_checkout(self):
        self.jailed(self.git('git -C "%s" fetch origin main' % W.fwd(self.w.foreign)))
        self.jailed(self.git('git -C "%s" merge --ff-only origin/main' % W.fwd(self.w.foreign)))

    def test_no_other_role_may_fetch_or_merge_main(self):
        fetch, merge = 'git -C "%s" fetch origin main' % self.main, 'git -C "%s" merge --ff-only origin/main' % self.main
        for agent_type in ('worker', 'senior-worker'):
            with self.subTest(agent_type):
                self.jailed(self.git(fetch, agent_type))
                self.denied(self.git(merge, agent_type))
        # a role the repo lets edit a main checkout still gains nothing from the jail
        self.roles({'lander': {'edit_main_checkout': True, 'why': 'test'},
                    'worker': {'edit_main_checkout': True, 'why': 'test'}})
        self.jailed(self.git(fetch, 'worker'))
        self.jailed(self.git(merge, 'worker'))
        self.jailed(self.git('GIT_DIR="%s/.git" git fetch origin main' % self.main, 'worker'))
        d = decide(bash(fetch, self.w.wt), self.w.ctx(environ=COORDINATOR))
        self.jailed(d)

    def test_the_plugin_prefixed_lander_may_land_and_no_other_spelling(self):
        # docs/guard.md "Agent names": <plugin>:lander is the lander; any other prefix is not
        fetch, merge = 'git -C "%s" fetch origin main' % self.main, 'git -C "%s" merge --ff-only origin/main' % self.main
        self.allowed(self.git(fetch, P + 'lander'))
        self.allowed(self.git(merge, P + 'lander'))
        self.allowed(self.git('& git -C "%s" fetch origin' % self.main, P + 'lander', tool='PowerShell'))
        self.jailed(self.git('git -C "%s" commit -m x' % self.main, P + 'lander'))
        for kind in (P + 'worker', P + 'senior-worker', 'other:lander', P + P + 'lander', P.upper() + 'lander', P,
                     P + 'main', P.rstrip(':') + 'lander', 'x' + P + 'lander', P[1:] + 'lander'):
            with self.subTest(kind):
                self.jailed(self.git(fetch, kind))
                self.denied(self.git(merge, kind))

    def test_a_lander_the_repo_keeps_out_of_main_may_not(self):
        self.roles({'lander': {'edit_main_checkout': False, 'why': 'test'}})
        self.jailed(self.git('git -C "%s" fetch origin main' % self.main))
        self.denied(self.git('git -C "%s" merge --ff-only origin/main' % self.main))

    def test_a_lander_commits_nowhere_through_a_computed_dash_c(self):
        # the main-checkout history rule refuses the commit first; the jail refuses the rest
        self.each_denied(['git -C "$M" commit -m x', 'git -C "$(git rev-parse --show-toplevel)" commit -m x'])
        self.each_denied(['git -C "$M" commit -m x'], tool='PowerShell')
        # a computed -C is a checkout the guard cannot place (unplaced); GIT_DIR re-points git on purpose;
        # a symbolic-ref write moves a ref every worktree shares (git-shared-ref), from any place
        # a copy is a write only the jail refuses there; a rename deletes main's name (git-shared-ref)
        for text, rule in (('git -C "$M" symbolic-ref HEAD refs/heads/x', 'git-shared-ref'),
                           ('git -C "$M" branch -m main old', 'git-shared-ref'),
                           ('git -C "$M" branch -c main old', 'unplaced'),
                           ('GIT_DIR="%s/.git" git tag v9' % self.main, 'write-jail')):
            with self.subTest(text):
                self.jailed(self.git(text), rule)


class RefDeleteHint(MainGit):
    """`git update-ref -d` of a shared ref is refused (git-shared-ref) with a ref delete's own
    Instead, never a worktree: every worktree shares the repository's refs (docs/guard.md
    "Rules"). tests/guard/test_shared_ref.py has the rule's own cases."""

    def git_at(self, text, cwd):
        ev = event('Bash', {'command': text, 'description': 'test'}, cwd, True, 'worker')
        return decide(ev, self.w.ctx(environ={}))

    def test_a_branch_ref_delete_points_at_branch_d(self):
        # from the session's worktree the Instead runs there; from the main checkout, there
        m = self.main
        for cwd, at in ((self.w.wt, W.fwd(self.w.wt)), (self.w.main, m)):
            for text in ('git -C "%s" update-ref -d refs/heads/kit/x' % m,
                         'git -C "%s" update-ref -m why -d refs/heads/kit/x' % m,
                         'git -C "%s" update-ref -d refs/heads/kit/x 0123abc' % m):
                with self.subTest(text, cwd=cwd):
                    d = self.jailed(self.git_at(text, cwd), 'git-shared-ref')
                    self.assertIn('Instead: git -C "%s" branch -d kit/x [' % at, d.reason)
                    self.assertIn('shares this repository\'s refs', d.reason)
                    self.assertNotIn('worktree add', d.instead)
                    self.assertNotIn('update-ref', d.instead)
                    with self.subTest('the Instead itself'):
                        self.allowed(self.git_at(d.instead, cwd))

    def test_another_ref_delete_shows_the_ref(self):
        m, wt = self.main, W.fwd(self.w.wt)
        pm, coordinator = 'the PM\'s to delete', 'a coordinator\'s to delete'
        for text, ref, who in (('git -C "%s" update-ref -d refs/tags/v1' % m, 'refs/tags/v1', pm),
                               ('git -C "%s" update-ref -d refs/remotes/origin/x' % m, 'refs/remotes/origin/x',
                                coordinator),
                               ('git -C "%s" update-ref -d "$R"' % m, '<ref>', pm),
                               ('git -C "%s" update-ref -d refs/heads/' % m, 'refs/heads/', pm)):
            with self.subTest(text):
                d = self.jailed(self.git_at(text, self.w.wt), 'git-shared-ref')
                self.assertIn('Instead: git -C "%s" show-ref %s [' % (wt, ref), d.reason)
                self.assertIn(who, d.reason)

    def test_a_shared_ref_update_shows_the_ref_and_never_a_worktree(self):
        # a worktree shares the ref, so running the update there moves the same ref: the Instead
        # shows it; only a ref the rule lets through (refs/tmp) keeps the worktree hint
        m, wt = self.main, W.fwd(self.w.wt)
        for text, cwd, at in (('git -C "%s" update-ref refs/heads/x HEAD' % m, self.w.wt, wt),
                              ('git update-ref refs/heads/x HEAD', self.w.main, m)):
            with self.subTest(text, cwd=cwd):
                d = self.jailed(self.git_at(text, cwd), 'git-shared-ref')
                self.assertIn('Instead: git -C "%s" show-ref refs/heads/x [' % at, d.reason)
                self.assertNotIn('worktree add', d.instead)
        d = self.jailed(self.git_at('git -C "%s" update-ref refs/tmp/x HEAD' % m, self.w.wt), 'git-main-history')
        self.assertIn('Instead: git -C "%s" update-ref refs/tmp/x HEAD [' % wt, d.reason)
        d = self.jailed(self.git_at('git update-ref refs/tmp/x HEAD', self.w.main), 'git-main-history')
        self.assertIn('worktree add', d.instead)
        d = self.jailed(self.git_at('git -C "%s" update-ref -d refs/tmp/x' % m, self.w.wt), 'git-main-history')
        self.assertIn('Instead: git -C "%s" update-ref -d refs/tmp/x [' % wt, d.reason)


class UnplacedGit(MainGit):
    """A git write whose checkout the text cannot place (GIT_DIR, GIT_WORK_TREE, --git-dir,
    --work-tree, core.worktree, a computed -C) is refused for every agent role: it may be a main
    checkout, where re-pointing HEAD or renaming main sends the next lander's merge to the wrong
    branch (docs/guard.md "Write jail")."""

    def test_a_worker_may_not_repoint_main_through_git_dir(self):
        m = self.main
        # a symbolic-ref write and a rename are git-shared-ref's: they move a ref every worktree shares
        for text, rule in (('git --git-dir="%s/.git" symbolic-ref HEAD refs/heads/x' % m, 'git-shared-ref'),
                           ('git --git-dir="%s/.git" branch -m main old' % m, 'git-shared-ref'),
                           ('GIT_DIR="%s/.git" git branch -m main old' % m, 'git-shared-ref'),
                           ('git --git-dir="%s/.git" branch -c main old' % m, 'write-jail'),
                           ('GIT_DIR="%s/.git" git symbolic-ref HEAD refs/heads/x' % m, 'git-shared-ref'),
                           ('GIT_DIR="%s/.git" git branch -c main old' % m, 'write-jail')):
            with self.subTest(text):
                self.jailed(self.git(text, 'worker'), rule)
        self.jailed(self.git('$env:GIT_DIR = "%s/.git"; git branch -c main old' % m, 'worker', tool='PowerShell'))

    def test_a_worker_writes_no_checkout_it_cannot_place(self):
        m = self.main
        for text in ('git --git-dir="%s/.git" tag v9' % m, 'git --git-dir="%s/.git" config user.name x' % m,
                     'git --git-dir="%s/.git" --work-tree="%s" update-index --assume-unchanged a.py' % (m, m),
                     'git --git-dir="%s/.git" --work-tree="%s" add a.py' % (m, m), 'git --git-dir="%s/.git" gc' % m,
                     'GIT_WORK_TREE="%s" git add a.py' % m, 'git -c core.worktree="%s" add a.py' % m):
            with self.subTest(text):
                self.jailed(self.git(text, 'worker'))
        # a fetch into refs/heads writes a ref every worktree shares: git-shared-ref's
        self.jailed(self.git('git --git-dir="%s/.git" fetch origin x:refs/heads/y' % m, 'worker'), 'git-shared-ref')
        self.jailed(self.git('git -C "$M" tag v9', 'worker'), 'unplaced')     # a computed -C

    def worker_denied(self, texts, rule='write-jail', tool='Bash'):
        for text in texts:
            with self.subTest(text):
                self.jailed(self.git(text, 'worker', tool=tool), rule)

    def test_a_later_dash_c_never_places_an_unplaceable_git(self):
        # -C after --git-dir, --work-tree, core.worktree or GIT_DIR names the cwd, not the git dir
        m, wt = self.main, W.fwd(self.w.wt)
        for text in ('git --git-dir="%s/.git" -C "%s" symbolic-ref HEAD refs/heads/x' % (m, wt),
                     'git -C "%s" --git-dir="%s/.git" symbolic-ref HEAD refs/heads/x' % (wt, m)):
            with self.subTest(text):
                self.jailed(self.git(text, 'worker'), 'git-shared-ref')     # it moves a shared ref
        for text in ('GIT_DIR="%s/.git" git -C "%s" branch -c main old' % (m, wt),
                     'git -c core.worktree="%s" -C "%s" add a.py' % (m, wt),
                     'git --work-tree="%s" -C "%s" add a.py' % (m, wt)):
            with self.subTest(text):
                self.jailed(self.git(text, 'worker'))
        self.worker_denied(['git --git-dir="%s/.git" -C "%s" commit -m x' % (m, wt),
                            'git --git-dir="%s/.git" -C "%s" merge kit/x' % (m, wt),
                            'git --git-dir="%s/.git" -C "%s" rebase main' % (m, wt)], 'git-main-history')
        self.worker_denied(['$env:GIT_DIR = "%s/.git"; git -C "%s" branch -c main old' % (m, wt)], tool='PowerShell')
        for text in ('git --git-dir="%s/.git" -C "%s" log --oneline -3' % (m, wt),
                     'GIT_DIR="%s/.git" git -C "%s" status' % (m, wt)):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker'))

    def test_more_git_environment_is_unplaceable(self):
        for var in ('GIT_COMMON_DIR', 'GIT_INDEX_FILE', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0',
                    'GIT_CONFIG_PARAMETERS'):
            self.worker_denied(['%s=x git add a.py' % var, 'env %s=x git add a.py' % var,
                                'export %s=x; git add a.py' % var])
            self.worker_denied(['$env:%s = "x"; git add a.py' % var, 'Set-Item env:%s x; git add a.py' % var],
                               tool='PowerShell')
            with self.subTest(var):
                self.allowed(self.git('%s=x git log -3' % var, 'worker'))
        self.worker_denied(['git --config-env=user.name=V add a.py', 'git --config-env user.name=V add a.py'])
        self.worker_denied(['git --config-env=core.hooksPath=V commit -m x'], 'git-main-history')

    def test_an_unplaceable_git_reads_only_what_is_plainly_a_read(self):
        m = self.main
        for text in ('git --git-dir="%s/.git" branch --unset-up' % m, 'git --git-dir="%s/.git" branch --set-up=o/x' % m,
                     'git --git-dir="%s/.git" tag v9' % m, 'git --git-dir="%s/.git" config --unset x.y' % m,
                     'git --git-dir="%s/.git" log --output=x.txt' % m, 'git --git-dir="%s/.git" show --output x.txt' % m,
                     'git --git-dir="%s/.git" diff --outp=x.txt' % m, 'git --git-dir="%s/.git" log "$R"' % m):
            with self.subTest(text):
                self.jailed(self.git(text, 'worker'))
        # a tag delete moves a ref every worktree shares: git-shared-ref refuses it before the jail
        self.jailed(self.git('git --git-dir="%s/.git" tag --del v1' % m, 'worker'), 'git-shared-ref')
        for text in ('git --git-dir="%s/.git" branch --list' % m, 'git --git-dir="%s/.git" branch -a' % m,
                     'git --git-dir="%s/.git" tag -l' % m, 'git --git-dir="%s/.git" config --get user.name' % m,
                     'git --git-dir="%s/.git" log --oneline -3' % m):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker'))

    def test_every_spelling_of_a_git_environment_assignment_is_unplaceable(self):
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        self.worker_denied(['$env:GIT_DIR="%s/.git"; %s' % (m, g), '$Env:Git_Dir="%s/.git"; %s' % (m, g),
                            '${env:GIT_DIR} = "%s/.git"; %s' % (m, g), '${env:GIT_DIR}="%s/.git"; %s' % (m, g),
                            '${env:GIT_DIR} += "x"; %s' % g,
                            'Set-Content env:GIT_DIR "%s/.git"; %s' % (m, g),
                            'Set-Content -Path env:GIT_DIR -Value "%s/.git"; %s' % (m, g),
                            'sc env:GIT_DIR "%s/.git"; %s' % (m, g), 'Add-Content env:GIT_DIR "x"; %s' % g,
                            'New-Item -Path env: -Name GIT_DIR -Value "%s/.git"; %s' % (m, g),
                            'New-Item -Name:GIT_DIR -Path:env: -Value "%s/.git"; %s' % (m, g),
                            'ni -Path env:\\ -Name GIT_DIR -Value "%s/.git"; %s' % (m, g),
                            'Rename-Item env:MY_DIR -NewName GIT_DIR; %s' % g,
                            'Copy-Item env:MY_DIR env:GIT_DIR; %s' % g], tool='PowerShell')
        self.worker_denied(['GIT_DIR+="%s/.git" %s' % (m, g), 'GIT_DIR+="%s/.git"; %s' % (m, g),
                            'export GIT_DIR+=x; %s' % g, 'read GIT_DIR <<< "%s/.git"; export GIT_DIR; %s' % (m, g)])
        # naming a variable is not setting it
        for text in ('Write-Output $env:GIT_DIR; %s' % g, 'Get-Content env:GIT_DIR; %s' % g):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker', tool='PowerShell'))

    def test_git_environment_set_anywhere_in_the_line_leaves_all_its_git_unplaceable(self):
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        self.worker_denied(['eval "export GIT_DIR=%s/.git"; %s' % (m, g),
                            '. /dev/stdin <<< "export GIT_DIR=%s/.git"; %s' % (m, g),
                            'source /dev/stdin <<EOF\nexport GIT_DIR=%s/.git\nEOF\n%s' % (m, g),
                            'env -S "GIT_DIR=%s/.git true"; %s' % (m, g),
                            'bash -c "export GIT_DIR=%s/.git"; %s' % (m, g),
                            '%s; export GIT_DIR=%s/.git' % (g, m)])
        self.worker_denied(['Invoke-Expression \'$env:GIT_DIR = "%s/.git"\'; %s' % (m, g),
                            'iex \'$env:GIT_DIR = "%s/.git"\'; %s' % (m, g)], tool='PowerShell')
        self.allowed(self.git('eval "export GIT_DIR=%s/.git"; git -C "%s" status' % (m, wt), 'worker'))

    def test_set_dashdash_git_dir_not_setter(self):
        # bash's set takes options and positional parameters, never a variable: set -- GIT_DIR and
        # set GIT_DIR=x set $1 (commands.exporter_names); cmd's set NAME=value is the setter
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        self.judged_as_written(['set -- GIT_DIR; %s' % g, 'set -- GIT_DIR "%s/.git"; %s' % (m, g),
                                'set GIT_DIR="%s/.git"; %s' % (m, g), 'set -e -- GIT_WORK_TREE; %s' % g])
        self.worker_denied(['cmd /c "set GIT_DIR=%s/.git && %s"' % (m, g)], tool='PowerShell')

    def test_declare_p_git_dir_not_setter(self):
        # declare, typeset and local -p list the variables named (any other attribute is then
        # ignored): no value is set
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        self.judged_as_written(['declare -p GIT_DIR; %s' % g, 'typeset -p GIT_DIR GIT_WORK_TREE; %s' % g,
                                'declare -xp GIT_DIR; %s' % g, 'export -p; %s' % g, 'readonly -p; %s' % g])
        # without -p the name is set or exported: the line's git stays unplaceable
        self.worker_denied(['declare -x GIT_DIR; %s' % g, 'export GIT_DIR; %s' % g,
                            'declare -x GIT_DIR="%s/.git"; %s' % (m, g), 'typeset -r GIT_DIR=x; %s' % g])
        # export -p and readonly -p with a name still export, mark or set it (bash 5): unplaceable
        self.worker_denied(['export -p GIT_DIR="%s/.git" GIT_WORK_TREE="%s"; %s' % (m, m, g),
                            'readonly -p GIT_DIR="%s/.git"; %s' % (m, g), 'export -p GIT_DIR; %s' % g,
                            'export -np GIT_DIR="%s/.git"; %s' % (m, g), 'readonly -p GIT_DIR; %s' % g])

    def test_git_index_file_prefix_scoped_to_its_command(self):
        # GIT_INDEX_FILE=x before git reaches that git alone (the temp-index pattern): the line's
        # other git is placed as written, while the prefixed one stays unplaceable
        wt = W.fwd(self.w.wt)
        line = 'git -C "%s" add a.py && GIT_INDEX_FILE=/tmp/idx git -C "%s" write-tree' % (wt, wt)
        p = cm.read(line, sh.BASH, wt)
        self.assertEqual([c.git_env for c in p.commands], [False, 'GIT_INDEX_FILE'])
        d = self.jailed(self.git(line, 'worker'))
        self.assertIn('GIT_INDEX_FILE=/tmp/idx git', d.reason)
        self.assertNotIn('`git -C "%s" add a.py`' % wt, d.reason)
        for text in ('git -C "%s" add a.py && GIT_INDEX_FILE=/tmp/idx git -C "%s" log -3' % (wt, wt),
                     'env GIT_INDEX_FILE=/tmp/idx git -C "%s" log -3; git -C "%s" add a.py' % (wt, wt),
                     'GIT_INDEX_FILE=/tmp/idx git -C "%s" log -3; git -C "%s" branch -c main old' % (wt, wt)):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker'))
                self.allowed(self.marked(text, COORDINATOR))
        # an export, or another git variable beside it, still reaches every git of the line
        self.worker_denied(['export GIT_INDEX_FILE=/tmp/idx; git -C "%s" add a.py' % wt,
                            'GIT_INDEX_FILE=/tmp/idx GIT_DIR=/x git log -3; git -C "%s" add a.py' % wt,
                            'GIT_DIR=/x git log -3; git -C "%s" add a.py' % wt,
                            'GIT_INDEX_FILE=/tmp/idx bash -c "git -C %s add a.py"' % wt,
                            'GIT_INDEX_FILE=/tmp/idx git -C "%s" add a.py' % wt])

    def test_remove_item_env_passes(self):
        # removing an env: item writes no file and sets no git variable, from the main checkout too
        wt = W.fwd(self.w.wt)
        for text in ('Remove-Item Env:X', 'Remove-Item Env:\\X', 'ri env:FOO', 'Remove-Item -Path Env:X'):
            for cwd in (self.w.wt, self.w.main):
                with self.subTest(text, cwd=cwd):
                    self.allowed(decide(event('PowerShell', {'command': text, 'description': 'test'}, cwd, True,
                                              'worker'), self.w.ctx(environ={})))
        self.judged_as_written(['Remove-Item Env:GIT_DIR; git -C "%s" branch -c main old' % wt], tool='PowerShell')

    def test_ps_double_quoted_text_not_scanned(self):
        # a string is text: SetEnvironmentVariable(...) written inside one (a commit message, a
        # Write-Host line) sets nothing, so the line's git is placed as written
        wt = W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        self.judged_as_written([
            'git -C "%s" commit --dry-run -m "SetEnvironmentVariable(\'GIT_DIR\', 1)"' % wt,
            "git -C \"%s\" commit --dry-run -m 'SetEnvironmentVariable(\"GIT_DIR\", 1)'" % wt,
            'Write-Host "[Environment]::SetEnvironmentVariable(\'GIT_DIR\',\'x\')"; %s' % g,
            '$s = "SetEnvironmentVariable(\'GIT_INDEX_FILE\'"; %s' % g], tool='PowerShell')
        # the call itself still sets one
        self.held_everywhere([
            '[Environment]::SetEnvironmentVariable(\'GIT_DIR\', \'x\'); %s' % g,
            '$null = [Environment]::SetEnvironmentVariable("GIT_DIR", "x"); %s' % g,
            '[System.Environment]::SetEnvironmentVariable(\'GIT_WORK_TREE\', \'x\', \'Process\'); %s' % g,
            # a $( ) subexpression in a string runs
            'Write-Output "$([Environment]::SetEnvironmentVariable(\'GIT_DIR\', \'x\'))"; %s' % g],
            tool='PowerShell')

    def test_a_sourced_here_string_is_read_like_eval(self):
        self.worker_denied(['. /dev/stdin <<< "git -C %s commit -m x"' % self.main,
                            'source /dev/stdin <<EOF\ngit -C "%s" commit -m x\nEOF' % self.main], 'git-main-history')

    def test_a_plus_equals_assignment_is_not_the_program(self):
        m = self.main
        self.worker_denied(['X+=1 git -C "%s" branch -c main old' % m, 'env X+=1 git -C "%s" branch -c main old' % m])
        self.worker_denied(['X+=1 git -C "%s" commit -m x' % m], 'git-main-history')
        self.worker_denied(['X+=1 taskkill /IM node.exe', 'A=1 X+=1 pkill node'], 'kill-by-name')

    def test_a_backslash_in_the_variable_name_hides_nothing(self):
        # bash drops the backslash: export and env are handed GIT_DIR=...
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        self.worker_denied(['export GIT_D\\IR="%s/.git"; %s' % (m, g), 'declare -x GIT_D\\IR="%s/.git"; %s' % (m, g),
                            'export GIT_DIR\\="%s/.git"; %s' % (m, g), 'export GIT\\_DIR="%s/.git"; %s' % (m, g),
                            'export GIT_D\\IR; %s' % g,
                            'env GIT_D\\IR="%s/.git" %s' % (m, g), 'env GIT_D\\IR=%s/.git %s' % (m, g)])

    def test_allexport_and_a_read_are_not_read_as_a_git_environment(self):
        # set -a with read, printf -v or for sets GIT_DIR by no NAME=value the text shows: the
        # guard does not read it (honest mistakes only), and the git is judged where -C places it
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        self.judged_as_written(['set -a; read GIT_DIR <<< "%s/.git"; %s' % (m, g),
                                'set -a; printf -v GIT_DIR "%%s" "%s/.git"; %s' % (m, g),
                                'set -a; for GIT_DIR in "%s/.git"; do %s; done' % (m, g),
                                'set -o allexport; read GIT_DIR <<< "%s/.git"; %s' % (m, g),
                                '%s; set -a' % g,
                                'set -e; read X <<< y; %s' % g, 'set -euo pipefail; %s' % g, 'set +a; %s' % g])

    def test_a_computed_variable_name_is_not_read(self):
        # the name may be GIT_DIR, but the guard reads only a name the text spells (honest mistakes only): the
        # git is judged where -C places it, for a worker and the coordinator's mark alike
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        self.judged_as_written(['V=GIT_DIR; export $V="%s/.git"; %s' % (m, g),
                                'V=GIT_DIR; declare -x "$V=%s/.git"; %s' % (m, g),
                                'V=GIT_DIR; export "$V"; %s' % g,
                                'export FOO=$x; %s' % g, 'set -- "$@"; %s' % g])
        self.judged_as_written(["$n='GIT_DIR'; Set-Item \"env:$n\" x; %s" % g,
                                "[Environment]::SetEnvironmentVariable($n, 'x'); %s" % g,
                                "[Environment]::SetEnvironmentVariable(\"GIT_$n\", 'x'); %s" % g,
                                "[Environment]::SetEnvironmentVariable('FOO', $v); %s" % g,
                                "[Environment]::SetEnvironmentVariable('FOO', 'x'); %s" % g], tool='PowerShell')
        # a computed write target is still refused before the git is judged: New-Item's item (env:
        # joined with $n) as a provider item the guard cannot place, the others as not literal
        text = "$n='GIT_DIR'; New-Item -Path env: -Name $n -Value x; %s" % g
        self.jailed(self.git(text, 'worker', tool='PowerShell'), 'unplaced')
        self.jailed(self.marked(text, COORDINATOR, 'PowerShell'), 'unplaced')
        self.unplaced_behind_a_computed_target(['Set-Content $f x; %s' % g,
                                                "New-Item -Path $p -Name GIT_DIR -Value '%s/.git'; %s" % (m, g)])

    def unplaced_behind_a_computed_target(self, texts):
        """Each PowerShell line writes to a computed item path: the target, not literal, is refused
        before the git is judged (unplaced, for a worker and the coordinator's mark alike)."""
        for text in texts:
            with self.subTest(text):
                self.assertTrue(cm.read(text, sh.PS, W.fwd(self.w.wt)).clean, text)
                self.assertIn('not literal', self.jailed(self.git(text, 'worker', tool='PowerShell'),
                                                         'unplaced').reason)
                self.assertIn('not literal', self.jailed(self.marked(text, COORDINATOR, 'PowerShell'),
                                                         'unplaced').reason)

    def git_env_jailed(self, texts, tool='Bash'):
        """Each line sets a git variable by name (commands.git_env_set) before a git write: its
        git is unplaceable (write-jail for a worker) though the line parses clean."""
        for text in texts:
            with self.subTest(text):
                self.assertTrue(cm.read(text, sh.BASH if tool == 'Bash' else sh.PS, W.fwd(self.w.wt)).clean, text)
                self.jailed(self.git(text, 'worker', tool=tool))

    def judged_as_written(self, texts, tool='Bash'):
        """Each line may change the environment in a way the guard does not read (a computed name,
        a sourced file, a nested shell's options, an alias, a script-text call, a variable outside
        the git place names): the guard catches honest mistakes, not evasion, so the line's
        git write in the session's own worktree passes for a worker and the coordinator's mark."""
        for text in texts:
            with self.subTest(text):
                self.assertTrue(cm.read(text, sh.BASH if tool == 'Bash' else sh.PS, W.fwd(self.w.wt)).clean, text)
                self.allowed(self.git(text, 'worker', tool=tool))
                self.allowed(self.marked(text, COORDINATOR, tool))

    def test_bash_env_changers_the_guard_does_not_read_pass(self):
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        self.judged_as_written([
            'export \\GIT_DIR="%s/.git"; %s' % (m, g),
            'env \\GIT_DIR="%s/.git" %s' % (m, g),
            "bash -a -c 'read GIT_DIR <<< \"%s/.git\"; %s'" % (m, g),
            "bash -ac 'read GIT_DIR <<< \"%s/.git\"; %s'" % (m, g),
            "bash -o allexport -c 'read GIT_DIR <<< \"%s/.git\"; %s'" % (m, g),
            'set +e -a; read GIT_DIR <<< "%s/.git"; %s' % (m, g),
            'shopt -so allexport; read GIT_DIR <<< "%s/.git"; %s' % (m, g),
            "env SHELLOPTS=allexport bash -c 'read GIT_DIR <<< \"%s/.git\"; %s'" % (m, g),
            'export SHELLOPTS; %s' % g,
            'declare -n r=GIT_DIR; export r="%s/.git"; %s' % (m, g),
            'f() { local -x $1="%s/.git"; %s; }; f GIT_DIR' % (m, g),
            'export GIT_EXEC_PATH=/x; %s' % g,
            '. ./.env; %s' % g,
            'source ./.env; %s' % g,
            'set -a; . ./.env; set +a; %s' % g,
            'export $(cat .env | xargs); %s' % g])
        self.judged_as_written(['cmd /c "set %%V%%=%s/.git & %s"' % (m, g), 'cmd /c "set !V!=%s/.git & %s"' % (m, g)],
                               tool='PowerShell')

    def test_powershell_env_changers_the_guard_does_not_read_pass(self):
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        self.judged_as_written([
            "$p='env:GIT_DIR'; Set-Item -Path $p '%s/.git'; %s" % (m, g),
            "Set-Item \"${e}:GIT_DIR\" '%s/.git'; %s" % (m, g),
            "Set-Item -Path (\"env:GIT_\"+\"DIR\") '%s/.git'; %s" % (m, g),
            "Set-Location env:; Set-Item GIT_DIR '%s/.git'; %s" % (m, g),
            "[Environment]::$m('GIT_DIR', '%s/.git'); %s" % (m, g),
            "$env:TEMP = 'env:'; Set-Item \"$env:TEMP\\GIT_DIR\" '%s/.git'; %s" % (m, g),
            "'env:GIT_DIR' | Set-Item -Value '%s/.git'; %s" % (m, g),
            '. .\\setenv.ps1; %s' % g], tool='PowerShell')

    def test_a_git_variable_assigned_by_name_in_powershell_is_read(self):
        # one target among several, and a chained assignment, are still a git variable set by name
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        self.held_everywhere(["$env:A, $env:GIT_DIR = 1, '%s/.git'; %s" % (m, g),
                              "$a = $env:GIT_DIR = '%s/.git'; %s" % (m, g)], tool='PowerShell')

    def test_an_env_read_in_a_path_is_not_an_env_write(self):
        g = 'git -C "%s" branch -c main old' % W.fwd(self.w.wt)
        # (a $env:TEMP write target is refused as not literal: test_ps_drive_paths DynamicTargets)
        for text in ('Get-Content "$env:TEMP\\x.txt"', 'Test-Path "$env:TEMP\\x"',
                     'Get-ChildItem $env:TEMP', 'Set-Item env:PATH "$env:PATH;C:\\tools"',
                     "[Environment]::GetEnvironmentVariable('FOO')"):
            for line in (text, '%s; %s' % (text, g)):
                with self.subTest(line):
                    self.assertTrue(cm.read(line, sh.PS, W.fwd(self.w.wt)).clean, line)
                    self.allowed(self.git(line, 'worker', tool='PowerShell'))

    def test_env_changers_on_a_line_with_no_git_write_stay_allowed(self):
        wt = W.fwd(self.w.wt)
        for text in ('set -a; . ./.env; set +a', 'export $(cat .env | xargs)', 'for v in A B; do export $v=1; done',
                     'export $(cat .env | xargs); git -C "%s" status' % wt,
                     'GIT_AUTHOR_DATE=x git -C "%s" branch -c main old' % wt,
                     'export FOO=1 PATH="$PATH:/x"; git -C "%s" branch -c main old' % wt):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker'))
        self.allowed(self.git('cmd /c "set FOO=1 & git -C "%s" branch -c main old"' % wt, 'worker', tool='PowerShell'))

    def test_the_hint_names_what_leaves_git_unplaceable(self):
        m, wt = self.main, W.fwd(self.w.wt)
        for text, cause, instead in (
                ('GIT_DIR="%s/.git" git branch -c main old' % m, 'GIT_DIR', 'git -C "%s" branch -c main old' % wt),
                ('git --git-dir="%s/.git" tag v9' % m, '--git-dir', 'git -C "%s" tag v9' % wt),
                ('git --work-tree="%s" add a.py' % m, '--work-tree', 'git -C "%s" add a.py' % wt),
                ('git -c core.worktree="%s" add a.py' % m, 'core.worktree', 'git -C "%s" add a.py' % wt),
                ('git --config-env=user.name=V add a.py', '--config-env', 'git -C "%s" add a.py' % wt),
                ('git -C "$M" tag v9', 'computed -C', 'git -C "<checkout>" tag v9')):
            with self.subTest(text):
                d = self.jailed(self.git(text, 'worker'), 'unplaced' if '$M' in text else 'write-jail')
                self.assertIn(cause, d.reason)
                self.assertIn('Instead: %s [' % instead, d.reason)
        for text, cause in (('GIT_DIR="%s/.git" git commit -m x' % m, 'GIT_DIR'),
                            ('git --git-dir="%s/.git" commit -m x' % m, '--git-dir')):
            with self.subTest(text):
                # in the worktree plain git's commit is refused too (git-plain-commit): the kit's tool
                d = self.jailed(self.git(text, 'worker'), 'git-main-history')
                self.assertIn(cause, d.reason)
                self.assertIn('Instead: python %s -C "%s" -m "<subject>" --model "<your model>" <each path you changed> ['
                              % (rules.kit_tool(self.w.ctx(), 'commit'), wt), d.reason)

    def test_an_unplaceable_git_grep_reads_with_or_without_a_pager(self):
        # grep -O runs the pager it names; only an attacker hides a write there (honest mistakes only)
        m = self.main
        for text in ('git --git-dir="%s/.git" grep -O x' % m, 'git --git-dir="%s/.git" grep -Ovim x' % m,
                     'git --git-dir="%s/.git" grep -nO x' % m,
                     'git --git-dir="%s/.git" grep --open-files-in-pager=vim x' % m,
                     'git --git-dir="%s/.git" grep --open x' % m,
                     'git --git-dir="%s/.git" grep x' % m, 'git --git-dir="%s/.git" grep -n --or x' % m):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker'))

    def test_a_later_dash_c_never_places_a_config_from_the_environment_or_a_computed_one(self):
        wt = W.fwd(self.w.wt)
        self.worker_denied(['git --config-env=core.worktree=V -C "%s" branch -c main old' % wt,
                            'git --config-env core.worktree=V -C "%s" add a.py' % wt,
                            'git -c "$x" -C "%s" branch -c main old' % wt, 'git -c"$x" -C "%s" add a.py' % wt])

    def test_the_strict_listing_forms_still_read(self):
        m = self.main
        for text in ('git --git-dir="%s/.git" branch -vv' % m, 'git --git-dir="%s/.git" tag -n5' % m,
                     'git --git-dir="%s/.git" config --file x.cfg --get a.b' % m,
                     'git --git-dir="%s/.git" config -f x.cfg --get a.b' % m):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker'))
        for text in ('git --git-dir="%s/.git" branch -vv new' % m, 'git --git-dir="%s/.git" config -f x.cfg a.b c' % m,
                     'git --git-dir="%s/.git" config --file x.cfg a.b c' % m):
            with self.subTest(text):
                self.jailed(self.git(text, 'worker'))

    def test_a_worker_still_reads_a_checkout_it_cannot_place(self):
        m = self.main
        for text in ('git --git-dir="%s/.git" log --oneline -3' % m, 'GIT_DIR="%s/.git" git rev-parse HEAD' % m,
                     'git -C "$M" status', 'git --git-dir="%s/.git" branch --show-current' % m,
                     # a computed git directory: a read is a read wherever it points
                     'git --git-dir="$D" log --oneline -3', 'git --git-dir=$D show HEAD',
                     'git --git-dir "$D" status', 'git --git-dir="$D" branch --show-current'):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker'))
        for text in ('git --git-dir $d log -1', 'git --git-dir (Join-Path $a .git) show HEAD'):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker', tool='PowerShell'))
        # and a write through it is still refused
        for text in ('git --git-dir="$D" tag v9', 'git --git-dir="$D" branch -c main old'):
            with self.subTest(text):
                self.jailed(self.git(text, 'worker'))

    # ---------------------------------------------------------------- every jailed role, not only a worker

    def marked(self, text, environ, tool='Bash'):
        """The decision for a session with this launcher mark (no agent_id), run in its worktree."""
        return decide(event(tool, {'command': text, 'description': 'test'}, self.w.wt), self.w.ctx(environ=environ))

    def held_everywhere(self, texts, tool='Bash'):
        """git_env_jailed for a worker, and write-jail for the coordinator's mark."""
        self.git_env_jailed(texts, tool)
        for text in texts:
            for environ in (COORDINATOR,):
                with self.subTest(text, mark=environ[context.ROLE_ENV]):
                    self.jailed(self.marked(text, environ, tool))

    def test_a_jailed_mark_gets_no_git_on_a_line_the_guard_cannot_read(self):
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        cases = [('Bash', 'eval "$c"; %s' % g),
                 ('Bash', "V='GIT_DIR=%s/.git'; env $V %s" % (m, g)),
                 ('Bash', 'bash -c "$x"; %s' % g),
                 ('Bash', 'eval "$c"; git -C "%s" status' % wt),
                 ('PowerShell', 'iex $x; %s' % g),
                 ('PowerShell', '& $x; %s' % g),
                 ('PowerShell', 'cmd /c "for %%i in (a) do set GIT_DIR=%s/.git & %s"' % (m, g))]
        for tool, text in cases:
            with self.subTest(text):
                self.assertFalse(cm.read(text, sh.BASH if tool == 'Bash' else sh.PS, W.fwd(self.w.wt)).clean, text)
                self.jailed(self.git(text, 'worker', tool=tool), 'unreadable')
            for environ in (COORDINATOR, PM, DEEP, CONSULTANT):
                with self.subTest(text, mark=environ[context.ROLE_ENV]):
                    d = self.jailed(self.marked(text, environ, tool), 'unplaced')
                    self.assertIn('run the git in its own call', d.reason)
            # the owner's session and the marks the jail does not hold keep the line in a worktree
            for environ in ({}, PLAIN):
                with self.subTest(text, mark=environ.get(context.ROLE_ENV, 'owner')):
                    self.allowed(self.marked(text, environ, tool))
        # an unreadable line with no git word, or only a .git path, is not the jail's
        for text in ('eval "$c"; ls', 'eval "$c"; ls .git', 'eval "$c"; echo "$GIT_DIR"'):
            with self.subTest(text):
                self.allowed(self.marked(text, COORDINATOR))

    def test_a_trap_or_mapfile_callback_that_sets_a_git_variable_leaves_git_unplaceable(self):
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        self.held_everywhere(["trap 'export GIT_DIR=%s/.git' DEBUG; %s" % (m, g),
                              "trap 'export GIT_DIR=%s/.git' ERR; %s" % (m, g),
                              "trap 'export GIT_DIR=%s/.git' RETURN; %s" % (m, g),
                              "trap 'export GIT_DIR=%s/.git' SIGDEBUG; %s" % (m, g),
                              "mapfile -C 'export GIT_DIR=%s/.git #' -c 1 a < f; %s" % (m, g)])
        # a handler or callback the text names but does not spell is not read (honest mistakes only)
        self.judged_as_written(["trap x $s; %s" % g, "readarray -C f -c 1 a < f; %s" % g,
                                "mapfile -tC f a < f; %s" % g])
        for text in ('trap - DEBUG; %s' % g, "trap '' ERR; %s" % g, "trap 'rm -f x' EXIT; %s" % g,
                     'trap -p DEBUG; %s' % g, 'mapfile -t a < f; %s' % g):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker'))
                self.allowed(self.marked(text, COORDINATOR))

    def test_xargs_that_fills_in_git_words_leaves_git_unplaceable(self):
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        # with no subcommand, the input may hold --git-dir before it
        self.held_everywhere(['echo branch -c main old | xargs git -C "%s"' % wt])
        # an environment xargs fills in for env is not read (honest mistakes only)
        self.judged_as_written(['echo GIT_DIR=%s/.git | xargs -I{} env {} %s' % (m, g),
                                'echo GIT_DIR=x | xargs -I {} env {} %s' % g,
                                'echo x | xargs -i env {} %s' % g,
                                'echo x | xargs -0 -iQ env Q %s' % g,
                                'echo x | xargs --replace=Q env Q %s' % g,
                                'echo x | xargs --rep env {} %s' % g])
        # the input in a branch option reads as a computed word, so the line is unclean: refused
        text = 'echo old | xargs -I{} git -C "%s" branch -c main {}' % wt
        self.assertFalse(cm.read(text, sh.BASH, wt).clean, text)
        self.denied(self.git(text, 'worker'))
        self.denied(self.marked(text, COORDINATOR))
        for text in ('echo x | xargs -n1 echo; %s' % g, 'ls | xargs git -C "%s" add' % wt,
                     'echo x | xargs -I{} git -C "%s" status' % wt):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker'))
                self.allowed(self.marked(text, COORDINATOR))

    def test_powershell_splats_script_text_and_renamed_env_items(self):
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        # a git place variable set by name, = at the start of the value's word, is read
        self.held_everywhere(['$env:GIT_DIR +="x"; %s' % g], tool='PowerShell')
        # a splat, script text and a variable outside the git place names are not read (honest mistakes only)
        self.judged_as_written([
            "$h=@{Path='env:GIT_DIR';Value='%s/.git'}; Set-Item @h; %s" % (m, g),
            "$h=@{Path='env:'}; Set-Location @h; %s" % g,
            "$ExecutionContext.InvokeCommand.InvokeScript('$env:GIT_DIR=1'); %s" % g,
            "$ExecutionContext.InvokeCommand.NewScriptBlock($s).Invoke(); %s" % g,
            "[scriptblock]::Create($s).Invoke(); %s" % g,
            "[System.Management.Automation.ScriptBlock]::Create($s).Invoke(); %s" % g,
            '$env:GIT_EXEC_PATH ="x"; %s' % g,
            '$env:BASH_ENV ="x"; %s' % g], tool='PowerShell')
        # a name an env: item is renamed, copied or moved to may then hold env:, a spelling no honest
        # command uses: the provider item is not read as a write target and the line passes, for a
        # worker and the coordinator's mark alike (docs/guard-v2.md B16, cut)
        for text in ("Rename-Item env:FOO TEMP; Set-Item \"$env:TEMP\\GIT_DIR\" '%s/.git'; %s" % (m, g),
                     "Rename-Item env:FOO -NewName TEMP; Set-Item \"$env:TEMP\\GIT_DIR\" '%s/.git'; %s" % (m, g),
                     "Copy-Item env:FOO env:TEMP; Set-Item \"$env:TEMP\\GIT_DIR\" '%s/.git'; %s" % (m, g),
                     "Move-Item env:FOO -Destination env:TEMP; Set-Item \"$env:TEMP\\GIT_DIR\" '%s/.git'; %s" % (m, g)):
            with self.subTest(text):
                self.assertTrue(cm.read(text, sh.PS, W.fwd(self.w.wt)).clean, text)
                self.allowed(self.git(text, 'worker', tool='PowerShell'))
                for environ in (COORDINATOR,):
                    self.allowed(self.marked(text, environ, 'PowerShell'))
        for text in ('$env:FOO ="x"; %s' % g, "Rename-Item env:FOO BAR; Get-Content \"$env:TEMP\\x.txt\"; %s" % g):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker', tool='PowerShell'))

    def test_double_colons_in_a_powershell_string_are_text_not_a_static_call(self):
        # an expandable string runs code only in $( ): "$b :: $f" and "$t::M()" are text, so a
        # line that appends one to a plain variable leaves its git placed (observed with
        # PowerShell 5.1: "$t::GetEnvironmentVariable('OS')" prints the call as text)
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        loop = ('foreach ($b in $branches) { foreach ($f in $files) { if (git merge-base --is-ancestor $b '
                'origin/master) { $hits += "$b :: $f" } } }')
        texts = [loop, '$hits += "$b :: $f"; %s' % g, '$hits += "$t::SetItem($b)"; %s' % g,
                 'Write-Output "${b}::x"; %s' % g, '$hits = @(); $hits += "$b :: $f"; %s' % g,
                 # a backtick-escaped $( opens no subexpression; "" is a quote inside the one string
                 'Write-Output "`$(x) :: $b"; %s' % g, 'Write-Output "$t""::$m"; %s' % g]
        for text in texts:
            with self.subTest(text):
                p = cm.read(text, sh.PS, wt)
                self.assertTrue(p.clean, text)
                self.assertEqual([c.git_env for c in p.commands], [False] * len(p.commands), text)
                self.allowed(self.git(text, 'worker', tool='PowerShell'))
                for environ in (COORDINATOR,):
                    with self.subTest(text, mark=environ[context.ROLE_ENV]):
                        self.allowed(self.marked(text, environ, 'PowerShell'))
        # a git variable set beside the string and an env write of the string's value still leave
        # the git unplaceable
        self.held_everywhere([
            '$env:GIT_DIR += "%s/.git"; %s' % (m, g),
            '$env:GIT_DIR += "$b :: $f"; %s' % g,
            '$hits += "$b :: $f"; $env:GIT_DIR = "%s/.git"; %s' % (m, g),
            'Set-Item env:GIT_DIR "$b :: $f"; %s' % g,
            '[Environment]::SetEnvironmentVariable(\'GIT_DIR\', "$b :: $f"); %s' % g], tool='PowerShell')
        # a static call on a computed type, outside a string or inside $( ), is not read (honest mistakes only)
        self.judged_as_written([
            '$hits += $t::$n("GIT_DIR", "$b :: $f"); %s' % g,
            'Write-Output "$($t::$n(\'GIT_DIR\', \'x\'))"; %s' % g,
            '$hits += "$b :: $($t::$n(\'GIT_DIR\', \'x\'))"; %s' % g], tool='PowerShell')
        # a string piped to Invoke-Expression is code, and a $ in it expands before it runs: the
        # line is unreadable, with or without a ::, and so is a shell reading such a string from a
        # pipe (unreadable for a worker; for a mark, a git the guard cannot place: unplaced)
        for text in ('"$t::$n(\'GIT_DIR\', \'x\')" | iex; %s' % g, '"a :: $b" | Invoke-Expression; %s' % g,
                     '"x $b" | iex; %s' % g, '"git $b" | powershell -Command -'):
            with self.subTest(text):
                self.assertFalse(cm.read(text, sh.PS, wt).clean, text)
                self.jailed(self.git(text, 'worker', tool='PowerShell'), 'unreadable')
                for environ in (COORDINATOR,):
                    with self.subTest(text, mark=environ[context.ROLE_ENV]):
                        self.jailed(self.marked(text, environ, 'PowerShell'), 'unplaced')
        # a single-quoted string and a backtick-escaped $ do not expand: iex runs the text as
        # written (x $b), and the git after it stays placed
        for text in ("'x $b' | iex; %s" % g, '"x `$b" | iex; %s' % g):
            with self.subTest(text):
                p = cm.read(text, sh.PS, wt)
                self.assertTrue(p.clean, text)
                self.assertEqual([c.git_env for c in p.commands], [False] * len(p.commands), text)
                self.allowed(self.git(text, 'worker', tool='PowerShell'))
                for environ in (COORDINATOR,):
                    with self.subTest(text, mark=environ[context.ROLE_ENV]):
                        self.allowed(self.marked(text, environ, 'PowerShell'))
        # a string handed to Invoke-Expression is code, and a $( inside ${...} is a substitution:
        # the line is unreadable
        for text in ('iex "$t::$n(\'GIT_DIR\', \'x\')"; %s' % g, 'Write-Output "${a$(x)}::y"; %s' % g):
            with self.subTest(text):
                self.assertFalse(cm.read(text, sh.PS, wt).clean, text)
                self.denied(self.git(text, 'worker', tool='PowerShell'))
                self.denied(self.marked(text, COORDINATOR, 'PowerShell'))
        # a property cmdlet on a computed Environment:: path is refused: its -Path is a write
        # target that is not literal ($n), so the guard cannot tell where it writes (unplaced, for a
        # worker and the coordinator's mark alike), before its git is judged
        text = 'Set-ItemProperty -Path "Environment::$n" -Name GIT_DIR -Value x; %s' % g
        self.assertIn('not literal', self.jailed(self.git(text, 'worker', tool='PowerShell'), 'unplaced').reason)
        self.assertIn('not literal', self.jailed(self.marked(text, COORDINATOR, 'PowerShell'), 'unplaced').reason)
        # a curly double quote may end the string where the guard reads on: the static call on a
        # computed type it would leave is not read either
        self.judged_as_written(['$hits += "$b :: “ x"; %s' % g, '$hits += "$b :: ” x"; %s' % g],
                               tool='PowerShell')

    def test_text_piped_into_set_content_or_add_content_is_its_value(self):
        wt = W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        for text in ("'text' | Set-Content notes.md; %s" % g, "'text' | Add-Content -Path notes.md; %s" % g,
                     "'text' | Set-Content notes.md; git -C \"%s\" add notes.md" % wt):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker', tool='PowerShell'))
                self.allowed(self.marked(text, COORDINATOR, 'PowerShell'))
        # with no path in the text, the piped object may name the item: not read (honest mistakes only)
        self.judged_as_written(["$o | Set-Content -Value x; %s" % g, "'env:GIT_DIR' | Set-Item -Value x; %s" % g],
                               tool='PowerShell')

    def test_each_env_changer_unread_passes_for_every_jailed_role(self):
        """Branches the rows above reach only together, each on its own: an alias, a startup file,
        a computed name, a module, an output variable, a process environment or a batch file is not
        read (honest mistakes only); a literal setx of a git place name still is."""
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        self.judged_as_written(['alias git="git --git-dir=%s/.git"; %s' % (m, g),
                                'export BASH_ENV=./x.sh; bash -c \'%s\'' % g,
                                'BASH_ENV=./x.sh %s' % g,
                                'export BASHOPTS=x; %s' % g,
                                'ENV=./x.sh %s' % g,
                                'env "$n"=x %s' % g])
        self.judged_as_written(['Set-Alias git x; %s' % g,
                                'New-Alias g x; %s' % g,
                                'New-PSDrive -Name Q -PSProvider Environment -Root \\; %s' % g,
                                'Import-Module x; %s' % g,
                                'Get-Item x -OutVariable env:GIT_DIR; %s' % g,
                                'Get-Item x -ov:env:GIT_DIR; %s' % g,
                                "Start-Process git -Environment @{GIT_DIR='%s/.git'} "
                                "-ArgumentList '-C','%s','branch','-c','main','old'" % (m, wt),
                                'cmd /c "call x.bat & %s"' % g,
                                'cmd /c "x.bat & %s"' % g], tool='PowerShell')
        self.held_everywhere(['cmd /c "setx GIT_DIR x & %s"' % g], tool='PowerShell')

    def denied_for_every_jailed_role(self, texts, tool='Bash'):
        """Each line is refused for a worker and for the coordinator's mark and its old name, by
        whichever rule reaches it first (the parse may be unclean)."""
        for text in texts:
            with self.subTest(text):
                self.denied(self.git(text, 'worker', tool=tool))
            for environ in (COORDINATOR,):
                with self.subTest(text, mark=environ[context.ROLE_ENV]):
                    self.denied(self.marked(text, environ, tool))

    def test_xargs_fills_its_replace_string_into_any_later_word(self):
        # the program word and a nested shell's text are filled in as much as git's own words
        m, wt = self.main, W.fwd(self.w.wt)
        self.denied_for_every_jailed_role([
            "printf '%%s\\n' --git-dir=%s/.git | xargs -I{} sh -c 'git {} symbolic-ref HEAD refs/heads/x'" % m,
            "printf '%%s\\n' --git-dir=%s/.git | xargs -I{} env -S 'git {} symbolic-ref HEAD refs/heads/x'" % m,
            "printf '%%s\\n' --git-dir=%s/.git | xargs -I{} bash -c 'git {} branch -c main old'" % m,
            'echo git | xargs -I{} {} -C %s symbolic-ref HEAD refs/heads/x' % m,
            'echo git | xargs -IQ Q -C %s symbolic-ref HEAD refs/heads/x' % m])
        for text in ('ls | xargs -I{} wc -l {}', 'echo x | xargs -I{} git -C "%s" status' % wt):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker'))
                self.allowed(self.marked(text, COORDINATOR))

    def test_a_trap_on_any_signal_but_exit_leaves_git_unplaceable(self):
        # a handler on a signal the line may raise (kill, a child ending) runs before the git
        m, wt = self.main, W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        self.held_everywhere(["trap 'export GIT_DIR=%s/.git' USR1; kill -USR1 $$; %s" % (m, g),
                              "trap 'export GIT_DIR=%s/.git' CHLD; sleep 0 & %s" % (m, g),
                              "trap 'export GIT_DIR=%s/.git' SIGINT; %s" % (m, g),
                              "trap 'export GIT_DIR=%s/.git' EXIT USR1; %s" % (m, g)])
        # a handler the text names but does not spell is not read (honest mistakes only)
        self.judged_as_written(["trap 1 USR1; %s" % g])
        for text in ("trap 'rm -f x' EXIT; %s" % g, "trap 'rm -f x' 0; %s" % g, "trap 'rm -f x' EXIT 0; %s" % g,
                     'trap - USR1; %s' % g, "trap '' USR1; %s" % g, 'trap USR1; %s' % g):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker'))
                self.allowed(self.marked(text, COORDINATOR))

    def test_the_git_a_trap_or_mapfile_callback_runs_is_read(self):
        # the handler is text this shell runs later, wherever it then is: its git cannot be placed
        m, wt = self.main, W.fwd(self.w.wt)
        self.denied_for_every_jailed_role([
            "trap 'git -C %s symbolic-ref HEAD refs/heads/x' EXIT; true" % m,
            "trap 'git symbolic-ref HEAD refs/heads/x' EXIT; cd %s" % m,
            "mapfile -C 'git -C %s symbolic-ref HEAD refs/heads/x #' -c 1 a < f" % m])
        # a handler the text does not spell is text the guard cannot read, as eval "$c" is
        for text in ('trap "$h" EXIT; git status', 'trap -- "$h" DEBUG; git -C "%s" branch -c main old' % wt):
            with self.subTest(text):
                self.assertFalse(cm.read(text, sh.BASH, wt).clean, text)
        self.denied_for_every_jailed_role(['trap "$h" EXIT; git status',
                                           'trap -- "$h" DEBUG; git -C "%s" branch -c main old' % wt])

    def test_powershell_script_text_in_every_spelling_is_not_read(self):
        # script text the line builds may set anything; the guard reads only what the text
        # spells (honest mistakes only), so the git is judged where -C places it
        wt = W.fwd(self.w.wt)
        g = 'git -C "%s" branch -c main old' % wt
        self.judged_as_written([
            "[Management.Automation.ScriptBlock]::Create($s).Invoke(); %s" % g,
            "[ScriptBlock]::Create($s).Invoke(); %s" % g,
            "([type]'scriptblock')::Create($s).Invoke(); %s" % g,
            "[powershell]::Create().AddScript($s).Invoke(); %s" % g,
            "[System.Management.Automation.PowerShell]::Create().AddScript($s).Invoke(); %s" % g,
            "$ExecutionContext.InvokeCommand.ExpandString($s); %s" % g,
            "$ExecutionContext.InvokeCommand.GetScriptBlock($s).Invoke(); %s" % g], tool='PowerShell')

    def test_script_text_named_in_a_quoted_string_is_not_run(self):
        wt = W.fwd(self.w.wt)
        for text in ('git -C "%s" commit --dry-run -m "Handle InvokeScript in the guard"' % wt,
                     "git -C \"%s\" commit --dry-run -m 'Read [scriptblock]::Create and NewScriptBlock'" % wt,
                     'git -C "%s" commit --dry-run -m "Read [Environment]::SetEnvironmentVariable and InvokeProvider"' % wt):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker', tool='PowerShell'))
                self.allowed(self.marked(text, COORDINATOR, 'PowerShell'))
        # a string that runs a subexpression is code, but script text it builds is not read either
        # (honest mistakes only): the git after it is judged where -C places it
        text = 'git -C "%s" commit --dry-run -m "$([scriptblock]::Create($s).Invoke())"; git -C "%s" branch -c main old' % (wt, wt)
        self.judged_as_written([text], tool='PowerShell')

    def test_find_execdir_and_a_filled_in_exec_word_leave_git_unplaceable(self):
        # -execdir runs in each found file's folder; {} under -exec is the found path
        m, wt = self.main, W.fwd(self.w.wt)
        self.denied_for_every_jailed_role([
            "find %s -maxdepth 1 -name README.md -execdir git symbolic-ref HEAD refs/heads/x ';'" % m,
            "find %s -maxdepth 1 -name README.md -okdir git branch -c main old ';'" % m,
            "find %s -maxdepth 0 -exec git -C {} symbolic-ref HEAD refs/heads/x ';'" % m,
            "find %s -maxdepth 0 -exec git --git-dir={}/.git branch -c main old ';'" % m,
            "find %s -maxdepth 1 -name git -exec {} -C %s symbolic-ref HEAD refs/heads/x ';'" % (m, m)])
        for text in ("find . -name '*.py' -exec wc -l {} +", "find . -name '*.py' -execdir wc -l {} +",
                     'find "%s" -maxdepth 0 -exec git -C "%s" status \';\'' % (wt, wt)):
            with self.subTest(text):
                self.allowed(self.git(text, 'worker'))
                self.allowed(self.marked(text, COORDINATOR))


class KitJson(Base):
    """An agent session may not edit a .kit.json in a checkout: it names the session's own roots."""

    def test_agent_edits_refused(self):
        d = self.jailed(self.write(os.path.join(self.w.wt, '.kit.json'), tool='Edit'), rule='kit-json')
        self.assertIn('Ask the coordinator to take the change to the owner', d.reason)
        self.jailed(self.write(os.path.join(self.w.main, '.kit.json'), cwd=self.w.main, env=COORDINATOR, sub=False),
                    rule='kit-json')
        self.jailed(self.run_bash('echo {} > .kit.json'), rule='kit-json')
        self.jailed(self.run_ps('Remove-Item .kit.json'), rule='kit-json')

    def test_owner_may(self):
        self.allowed(self.write(os.path.join(self.w.main, '.kit.json'), cwd=self.w.main, sub=False))

    def test_outside_a_checkout_it_is_only_a_file(self):
        self.allowed(self.write(os.path.join(self.w.temp, 'fixture', '.kit.json')))


class DevDrive(Base):
    """The outer bound (docs/guard.md "Write jail", "Outer bound"): with the machine config's
    dev_drive set, an agent session's write that its roots allow is still refused off the Dev
    Drive (rule dev-drive), and a data or mark root off it is dropped with a warning. Here the
    Dev Drive is the world's base folder and `off` a folder beside it."""

    def setUp(self):
        Base.setUp(self)
        self.off = tempfile.mkdtemp(prefix='kit-guard-off-', dir=os.path.dirname(self.w.base))
        self.addCleanup(shutil.rmtree, self.off, True)
        self.agents = {'AGENT_TEMP': self.off}

    def bound(self, **more):
        self.w.user_config(dict({'dev_drive': self.w.base}, **more))

    def judged(self, payload):
        return guard.decide(action(payload), self.w.ctx(environ=self.agents))

    def test_unset_the_bound_is_off(self):
        self.allowed(self.write(os.path.join(self.off, 'x.txt'), env=self.agents))
        self.w.user_config({})
        self.allowed(self.write(os.path.join(self.off, 'x.txt'), env=self.agents))

    def test_a_write_its_roots_allow_is_refused_off_the_dev_drive(self):
        self.bound()
        for tool in ('Write', 'Edit', 'NotebookEdit'):
            with self.subTest(tool):
                d = self.jailed(self.write(os.path.join(self.off, 'x.txt'), env=self.agents, tool=tool), 'dev-drive')
                self.assertIn('is not on the Dev Drive (%s)' % W.fwd(self.w.base), d.reason)
                self.assertIn('Instead: ', d.reason)
                self.assertIn(W.fwd(self.w.wt), d.instead)          # the same write in its own worktree
        d = self.jailed(self.run_bash('echo x > "%s/y.txt"' % W.fwd(self.off), env=self.agents), 'dev-drive')
        self.assertIn('is not on the Dev Drive', d.reason)
        self.jailed(self.run_ps('Set-Content -Path "%s/y.txt" -Value x' % W.fwd(self.off), env=self.agents),
                    'dev-drive')

    def test_writes_on_the_dev_drive_and_null_targets_pass(self):
        self.bound()
        self.allowed(self.write(os.path.join(self.w.wt, 'src', 'a.py'), env=self.agents))
        self.allowed(self.write(os.path.join(self.w.temp, 'gate.log'), env=self.agents))
        self.allowed(self.write(os.path.join(self.w.sib, 'a.py'), env=self.agents))
        self.allowed(self.run_bash('echo x > /dev/null 2>&1', env=self.agents))
        self.allowed(self.run_ps('echo x 2>$null > NUL', env=self.agents))

    def test_the_owner_session_is_not_held(self):
        self.bound()
        self.allowed(self.write(os.path.join(self.off, 'x.txt'), sub=False, env=self.agents))

    def test_a_drive_root_dev_drive(self):
        drive = os.path.splitdrive(self.w.base)[0] + '/'
        away = os.path.join(other_drive(self.w.base), 'agents')
        self.bound(dev_drive=drive)
        self.allowed(self.write(os.path.join(self.w.wt, 'a.py'), env={'AGENT_TEMP': away}))
        self.jailed(self.write(os.path.join(away, 'x.txt'), env={'AGENT_TEMP': away}), 'dev-drive')

    def test_a_root_reached_through_a_junction_off_the_dev_drive(self):
        link = os.path.join(self.w.base, 'agentlink')
        try:
            import _winapi
            _winapi.CreateJunction(self.off, link)
        except (ImportError, AttributeError, OSError) as e:
            self.skipTest('no junction: %s' % e)
        self.bound()
        self.jailed(self.write(os.path.join(link, 'x.txt'), env={'AGENT_TEMP': link}), 'dev-drive')

    def test_git_making_a_folder_off_the_dev_drive(self):
        self.bound()
        d = self.jailed(self.run_bash('git worktree add "%s/w" -b x/y' % W.fwd(self.off), env=self.agents),
                        'dev-drive')
        self.assertIn('is not on the Dev Drive', d.reason)
        self.allowed(self.run_bash('git worktree add "%s/w" -b x/y' % W.fwd(self.w.temp), env=self.agents))

    def test_a_mark_root_off_the_dev_drive_is_dropped_with_a_warning(self):
        notes = os.path.join(self.off, 'pmnotes')
        os.makedirs(notes)
        self.bound(guard_mark_roots={'pm': [{'path': notes, 'mode': 'open', 'why': 'test'}]})
        d = self.jailed(self.write(os.path.join(notes, 'x.md'), env=PM, sub=False), 'dev-drive')
        self.assertIn('is not on the Dev Drive', d.reason)
        ok = guard.decide(action(event('Write', {'file_path': os.path.join(self.w.wt, 'a.md'), 'content': 'x'},
                                       self.w.wt, False)), self.w.ctx(environ=PM))
        self.assertEqual((ok.verdict, ok.rule), ('allow', rules.WARNED), ok.reason)
        self.assertIn('the mark root "%s" is not on the Dev Drive' % W.fwd(notes), ok.reason)
        self.assertIn("guard_mark_roots", ok.reason)

    def test_a_data_root_off_the_dev_drive_is_dropped_with_a_warning(self):
        data = os.path.join(self.off, 'research')
        os.makedirs(data)
        self.w.kit({'repo': {'name': 'demo', 'push_block': True},
                    'guard': {'data_roots': [{'path': data, 'mode': 'open', 'why': 'test'}]}})
        self.allowed(self.write(os.path.join(data, 'x.md')))          # unset: the root stands
        self.bound()
        self.jailed(self.write(os.path.join(data, 'x.md')), 'dev-drive')
        ok = guard.decide(action(event('Write', {'file_path': os.path.join(self.w.wt, 'a.md'), 'content': 'x'},
                                       self.w.wt, True)), self.w.ctx(environ={}))
        self.assertEqual((ok.verdict, ok.rule), ('allow', rules.WARNED), ok.reason)
        self.assertIn('the data root "%s" is not on the Dev Drive' % W.fwd(data), ok.reason)
        self.assertIn('.kit.json guard.data_roots', ok.reason)

    def test_the_owner_sets_the_rule_to_warn_or_off(self):
        notes = os.path.join(self.off, 'pmnotes')
        os.makedirs(notes)
        roots = {'pm': [{'path': notes, 'mode': 'open', 'why': 'test'}]}
        self.bound(guard_rules={'dev-drive': 'warn'}, guard_mark_roots=roots)
        warned = self.judged(event('Write', {'file_path': os.path.join(self.off, 'x.txt'), 'content': 'x'},
                                   self.w.wt, True))
        self.assertEqual((warned.verdict, warned.rule), ('allow', rules.WARNED), warned.reason)
        self.assertIn('is not on the Dev Drive', warned.reason)
        self.allowed(self.write(os.path.join(notes, 'x.md'), env=PM, sub=False))   # warn: not dropped
        self.bound(guard_rules={'dev-drive': 'off'}, guard_mark_roots=roots)
        quiet = self.judged(event('Write', {'file_path': os.path.join(self.off, 'x.txt'), 'content': 'x'},
                                  self.w.wt, True))
        self.assertEqual((quiet.verdict, quiet.rule), ('allow', ''), quiet.reason)


setUpModule = W.deny_softened           # these pin what the guard cannot place and the dropped rules (world.deny_softened)
tearDownModule = W.allow_unplaced


if __name__ == '__main__':
    unittest.main()
