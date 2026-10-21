"""The backlog roles in the guard (docs/guard.md "Backlog roles"; docs/backlog.md "Roles"): a
subagent's backlog.py run is judged by kitlib.backlog.roles for the role its hook payload names
(backlog-role), and code that imports kitlib.backlog, a write of the backlog's refs and an edit of
a folder store's record files are raw writes no agent session makes (backlog-raw-write). A main
session's backlog.py run is the tool's own check (tests/backlog/test_policy.py)."""
import os
import unittest
from unittest import mock

from kitlib import agentname
from kitlib.git import repo
from kitlib.guard import backlogrole, context

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
        for args in ('retarget A B', 'migrate --to main --dry-run'):
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
