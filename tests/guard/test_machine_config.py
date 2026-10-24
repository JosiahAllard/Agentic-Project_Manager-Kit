"""The machine config folder the PM repository carries (rules.machine_config, docs/guard.md "Owner
machine setup"): every session but the owner's own is refused a write of a file in
<pointer>/.pm-kit or in a .pm-kit folder at the top of a checkout; the hub's token folder there
is hub-secrets' and the owner's delegation list keeps its own rule; the write jail drops a root
on or inside the PM repository's folder but keeps the PM repository's own checkout."""
import os
import unittest
from unittest import mock

from kitlib.config import migration
from kitlib.guard import context, delegfile, jail, prepush

from tests.realgit import real_git

from . import prepush_case as pc
from . import world as W
from .claudehost import decide
from .world import bash, event, ps

RULE = 'machine-config'                 # rules.MACHINE_CONFIG
# (environ, subagent) of every session the rule holds: the pm, a coordinator, a plain-marked
# session (one the write jail does not hold), a worker's mark, and a subagent
SESSIONS = (({context.ROLE_ENV: 'pm'}, False), ({context.ROLE_ENV: 'coordinator'}, False),
            ({context.ROLE_ENV: 'plain'}, False), ({context.ROLE_ENV: 'senior-worker'}, False),
            ({}, True))
OWNER = ({}, False)


def write(path, text='{}\n'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


class Base(unittest.TestCase):

    pointer = None

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def ctx(self, environ=None, **kw):
        kw.setdefault('pointer', lambda: self.pointer)
        return self.w.ctx(environ=environ if environ is not None else {}, **kw)

    def decide(self, payload, environ=None, **kw):
        return decide(payload, self.ctx(environ, **kw))

    def assert_refused(self, payload_of, label, rule=RULE, sessions=SESSIONS):
        for environ, sub in sessions:
            with self.subTest(label, environ=environ, sub=sub):
                d = self.decide(payload_of(sub), environ)
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)

    def assert_allowed(self, payload_of, label, sessions):
        for environ, sub in sessions:
            with self.subTest(label, environ=environ, sub=sub):
                d = self.decide(payload_of(sub), environ)
                self.assertIsNone(d, d and d.reason)

    def writes(self, path, cwd):
        return lambda sub: event('Write', {'file_path': path, 'content': '{}'}, cwd, sub)


class PointerFolder(Base):
    """<pointer>/.pm-kit, wherever the pointer names."""

    def test_a_pointer_folder_outside_git_is_held_for_every_session_but_the_owner(self):
        # T1: the pointer names a folder git does not place, so only the pointer path finds it
        for where in (self.w.plain, self.w.unknown):
            self.pointer = where
            for name in ('config.json', 'local.json', 'managed-settings.json', os.path.join('sub', 'x.txt')):
                target = os.path.join(where, '.pm-kit', name)
                self.assert_refused(self.writes(target, self.w.wt), target)
            self.assert_allowed(self.writes(os.path.join(where, '.pm-kit', 'config.json'), self.w.wt),
                                'owner', sessions=(OWNER,))

    def test_the_refusal_names_the_folder_and_a_read(self):
        self.pointer = self.w.plain
        d = self.decide(self.writes(os.path.join(self.w.plain, '.pm-kit', 'config.json'), self.w.wt)(True))
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, RULE, d.reason)
        self.assertIn('.pm-kit', d.reason)
        self.assertIn('This session is a subagent', d.reason)
        self.assertTrue(d.instead.startswith('Get-ChildItem -Force "'), d.instead)
        self.assertIn('[kit guard: rule=machine-config role=subagent]', d.reason)

    def test_the_pm_repository_checkout_stays_a_root_and_its_folder_is_held(self):
        # T4: the pointer is the worktree the session runs in: its own checkout holds the folder,
        # which the write jail does not count, so it still writes the rest of the checkout
        self.pointer = self.w.wt
        self.assert_allowed(self.writes(os.path.join(self.w.wt, 'a.py'), self.w.wt), 'a.py',
                            sessions=SESSIONS + (OWNER,))
        self.assert_refused(self.writes(os.path.join(self.w.wt, '.pm-kit', 'local.json'), self.w.wt), 'local.json')

    def test_a_root_on_or_inside_the_pointer_folder_is_dropped_and_one_holding_it_kept(self):
        # T5: guard_mark_roots naming <pointer>/.pm-kit/hub (or the folder) opens nothing; the
        # pointer itself, a folder above the PM repository's .pm-kit, stays
        self.pointer = self.w.plain
        hub = os.path.join(self.w.plain, '.pm-kit', 'hub')
        folder = os.path.join(self.w.plain, '.pm-kit')
        self.w.user_config({'guard_mark_roots': {'coordinator': [
            {'path': hub, 'mode': 'open', 'why': 't'}, {'path': folder, 'mode': 'open', 'why': 't'},
            {'path': self.w.plain, 'mode': 'open', 'why': 't'},
            {'path': os.path.join(hub, 'ro'), 'mode': 'read-only', 'why': 't'}]}})
        ctx = self.ctx({context.ROLE_ENV: 'coordinator'})
        got = [(jail.real(r.path), r.mode) for r in jail.roots(ctx, self.w.wt, lambda p: False, 'coordinator')
               if r.kind == jail.MARK]
        self.assertEqual(got, [(jail.real(self.w.plain), 'open'), (jail.real(os.path.join(hub, 'ro')), 'read-only')])

    def test_the_owner_may_write_the_folder(self):
        # T6
        self.pointer = self.w.data
        for target in (os.path.join(self.w.data, '.pm-kit', 'config.json'),
                       os.path.join(self.w.wt, '.pm-kit', 'local.json')):
            self.assert_allowed(self.writes(target, self.w.wt), target, sessions=(OWNER,))
            self.assert_refused(self.writes(target, self.w.wt), target)

    def test_the_delegation_list_keeps_its_rule_beside_the_others(self):
        # T7: owner-delegations is checked first; another file of the folder is this rule's
        self.pointer = self.w.data
        lst = os.path.join(self.w.data, '.pm-kit', 'owner-delegations.json')
        write(lst)
        self.assert_refused(self.writes(lst, self.w.wt), 'the list', rule=delegfile.RULE)
        self.assert_refused(self.writes(os.path.join(self.w.data, '.pm-kit', 'config.json'), self.w.wt), 'config')

    def test_a_hub_token_in_the_pointer_folder_is_a_hub_secret(self):
        # T11: read, written or named by the shell, the second token folder is hub-secrets'
        self.pointer = self.w.data
        token = os.path.join(self.w.data, '.pm-kit', 'hub', 'owner.token')
        write(token, 'x')
        payloads = (
            ('Read', lambda sub: event('Read', {'file_path': token}, self.w.wt, sub)),
            ('Write', self.writes(token, self.w.wt)),
            ('cat', lambda sub: bash('cat %s' % W.fwd(token), self.w.wt, sub)),
            ('Get-Content', lambda sub: ps('Get-Content "%s"' % token, self.w.wt, sub)),
        )
        for name, payload in payloads:
            self.assert_refused(payload, name, rule='hub-secrets')
        d = self.decide(payloads[0][1](True))
        self.assertIn(W.fwd(os.path.join(self.w.data, '.pm-kit', 'hub')).lower(), W.fwd(d.reason).lower())

    def test_a_relative_pointer_names_no_folder_and_the_name_still_holds(self):
        # T12
        self.pointer = os.path.join('relative', 'pm')
        ctx = self.ctx()
        self.assertEqual(jail.config_dirs(ctx), [os.path.join(self.w.home, '.pm-kit'),
                                                 os.path.join(self.w.home, migration.OLD_DIR_NAME)])
        self.assert_refused(self.writes(os.path.join(self.w.wt, '.pm-kit', 'config.json'), self.w.wt), 'by name')


class ByName(Base):
    """A .pm-kit folder at the top of a checkout, with no pointer."""

    def test_a_pm_kit_folder_at_a_checkout_top_is_held(self):
        # T2: a linked worktree and a main checkout; the case is ignored
        for target in (os.path.join(self.w.wt, '.pm-kit', 'config.json'),
                       os.path.join(self.w.wt, '.Pm-Kit', 'Local.json'),
                       os.path.join(self.w.wt, '.pm-kit', 'hub', 'agent.token'),
                       os.path.join(self.w.main, '.pm-kit', 'config.json'),
                       os.path.join(self.w.data, '.pm-kit', 'config.json')):
            self.assert_refused(self.writes(target, self.w.wt), target)

    def test_a_pm_kit_folder_deeper_in_a_checkout_is_not(self):
        # T3: and a file at the top named like the folder
        for target in (os.path.join(self.w.wt, 'sub', '.pm-kit', 'x.json'),
                       os.path.join(self.w.wt, '.pm-kit.json'),
                       os.path.join(self.w.wt, 'pm-kit', 'config.json')):
            self.assert_allowed(self.writes(target, self.w.wt), target, sessions=(({}, True),))
        self.assert_refused(self.writes(os.path.join(self.w.wt, '.pm-kit', 'x.json'), self.w.wt), 'control')

    def test_a_shell_write_into_the_folder_is_held(self):
        # T8: the shell's placed write targets reach the rule through edit_target
        payloads = (
            ('Remove-Item', lambda sub: ps('Remove-Item .pm-kit\\config.json', self.w.wt, sub)),
            ('rm', lambda sub: bash('rm .pm-kit/config.json', self.w.wt, sub)),
            ('redirect', lambda sub: bash('echo {} > .pm-kit/local.json', self.w.wt, sub)),
            ('Set-Content', lambda sub: ps('Set-Content -Path .pm-kit\\local.json -Value x', self.w.wt, sub)),
        )
        for name, payload in payloads:
            self.assert_refused(payload, name)


class Wildcards(Base):
    """A wildcard write target whose literal folder is a machine config folder (T9)."""

    def assert_not_this_rule(self, payload_of, label, sessions=SESSIONS + (OWNER,)):
        for environ, sub in sessions:
            with self.subTest(label, environ=environ, sub=sub):
                d = self.decide(payload_of(sub), environ)
                self.assertTrue(d is None or d.rule != RULE, d and d.reason)

    def test_a_wildcard_in_a_checkout_top_folder_is_held(self):
        payloads = (
            ('rm', lambda sub: bash('rm .pm-kit/c*', self.w.wt, sub)),
            ('rm -r', lambda sub: bash('rm -rf .Pm-Kit/hub/*', self.w.wt, sub)),
            ('Remove-Item', lambda sub: ps('Remove-Item .pm-kit\\*', self.w.wt, sub)),
        )
        for name, payload in payloads:
            self.assert_refused(payload, name)
        self.assert_not_this_rule(payloads[0][1], 'owner', sessions=(OWNER,))

    def test_a_wildcard_in_the_pointer_folder_is_held(self):
        self.pointer = self.w.plain
        folder = os.path.join(self.w.plain, '.pm-kit')
        write(os.path.join(folder, 'config.json'))
        self.assert_refused(lambda sub: bash('rm %s/c*.json' % W.fwd(folder), self.w.plain, sub), 'rm')
        self.assert_refused(lambda sub: ps('Remove-Item "%s\\c*"' % folder, self.w.plain, sub), 'Remove-Item')
        d = self.decide(bash('rm %s/c*.json' % W.fwd(folder), self.w.plain, True))
        self.assertIn('a wildcard in', d.reason)
        self.assertTrue(d.instead.startswith('Get-ChildItem -Force "'), d.instead)

    def test_a_wildcard_elsewhere_is_not_this_rule(self):
        for text in ('rm docs/*.md', 'rm sub/.pm-kit/*', 'rm .pm-kit*.json'):
            self.assert_not_this_rule(lambda sub, t=text: bash(t, self.w.wt, sub), text)

    def test_a_wildcard_that_can_match_the_delegation_list_keeps_its_rule(self):
        self.pointer = self.w.data
        write(os.path.join(self.w.data, '.pm-kit', 'owner-delegations.json'))
        self.assert_refused(lambda sub: bash('rm .pm-kit/*', self.w.data, sub), 'rm', rule=delegfile.RULE)
        self.assert_refused(lambda sub: bash('rm .pm-kit/c*', self.w.data, sub), 'rm c*')


class PointerGit(Base):
    """git run in the pointer's work tree whose pathspec lands in <pointer>/.pm-kit (T10)."""

    REFUSED = (
        'checkout HEAD~1 -- .pm-kit/config.json', 'rm .pm-kit/config.json', 'rm -r --cached .pm-kit/sub',
        'mv .pm-kit/config.json x.json', 'restore .pm-kit/local.json', 'restore -s HEAD~1 -W .pm-kit/config.json',
        'checkout-index -f .pm-kit/config.json', 'clean -f .pm-kit/sub', 'clean -fdx .pm-kit/sub',
        'rm ":(top).pm-kit/config.json"', 'restore --staged --w .pm-kit/config.json',    # `--w` is `--worktree`
        'restore --staged --no- .pm-kit/config.json', 'restore -S --no-st .pm-kit/config.json',  # `--no-staged`
    )
    OPEN = (
        'restore --staged .pm-kit/config.json', 'clean -n .pm-kit', 'clean --dry-run -f .pm-kit',
        'rm docs/x.md', 'checkout -- docs/x.md', 'checkout main', 'add .pm-kit/config.json',
        'log -- .pm-kit', 'rm ":!.pm-kit" docs',
    )

    def setUp(self):
        super(PointerGit, self).setUp()
        self.pointer = self.w.data

    def assert_not_this_rule(self, payload_of, label, sessions=SESSIONS + (OWNER,)):
        for environ, sub in sessions:
            with self.subTest(label, environ=environ, sub=sub):
                d = self.decide(payload_of(sub), environ)
                self.assertTrue(d is None or d.rule != RULE, d and d.reason)

    def test_git_that_changes_a_file_of_the_folder_is_held(self):
        data = W.fwd(self.w.data)
        for rest in self.REFUSED:
            self.assert_refused(lambda sub, r=rest: bash('git %s' % r, self.w.data, sub), 'in: ' + rest)
            self.assert_refused(lambda sub, r=rest: bash('git -C %s %s' % (data, r), self.w.wt, sub), '-C: ' + rest)
        d = self.decide(bash('git rm .pm-kit/config.json', self.w.data, True))
        self.assertIn('changes a file in', d.reason)

    def test_a_pathspec_the_other_rules_place_keeps_their_rule(self):
        # the whole folder holds the delegation list; the hub folder is hub-secrets'
        for rest, rule in (('rm -r --cached .pm-kit', delegfile.RULE), ('clean -fdx .pm-kit', delegfile.RULE),
                           ('clean -f .pm-kit/hub', 'hub-secrets')):
            self.assert_refused(lambda sub, r=rest: bash('git %s' % r, self.w.data, sub), rest, rule=rule)

    def test_the_owner_may_run_it(self):
        for rest in self.REFUSED:
            self.assert_not_this_rule(lambda sub, r=rest: bash('git %s' % r, self.w.data, sub), rest,
                                      sessions=(OWNER,))

    def test_other_git_in_the_pointer_tree_is_not_this_rule(self):
        for rest in self.OPEN:
            self.assert_not_this_rule(lambda sub, r=rest: bash('git %s' % r, self.w.data, sub), rest)


@real_git('git log --remotes=<name>: the fake lacks it')
class PrePushSecrets(pc.PrePushCase):
    """The pre-push check refuses a push whose commits add or change a file of a top .pm-kit/hub/
    folder or .pm-kit/local.json, on any remote (T13 to T15)."""

    ZERO = '0' * 40

    def check(self, sha, remote_sha=None, ref='refs/heads/b'):
        line = '%s %s %s %s' % (ref, sha, ref, remote_sha or self.ZERO)
        return prepush.check(self.repo.root, [line], gate_ok=lambda r, s: (None, 'none'))

    def add(self, rel, text='x\n'):
        self.repo.write(rel, text)
        return self.repo.commit('add %s' % rel, rel)

    def assert_secret_refused(self, rel):
        sha = self.add(rel)
        ok, msg = self.check(sha)
        self.assertFalse(ok)
        self.assertIn('sends %s, added or changed in commit %s' % (rel, sha[:12]), msg)
        self.assertIn('docs/guard.md "Pre-push"', msg)

    def test_a_hub_token_is_refused(self):
        # T13
        self.assert_secret_refused('.pm-kit/hub/agent.token')

    def test_a_case_spelling_of_the_folder_is_refused(self):
        # T13
        self.assert_secret_refused('.Pm-Kit/Hub/owner.token')

    def test_local_json_is_refused_on_every_remote_and_ref(self):
        # T14
        sha = self.add('.pm-kit/local.json', '{}\n')
        for ref in ('refs/heads/b', 'refs/heads/main', 'refs/tags/v1'):
            with self.subTest(ref):
                ok, msg = self.check(sha, ref=ref)
                self.assertFalse(ok)
                self.assertIn('.pm-kit/local.json', msg)
        ok, msg = prepush.check(self.repo.root, ['refs/heads/b %s refs/heads/b %s' % (sha, self.ZERO)],
                                gate_ok=lambda r, s: (None, 'none'), remote='https://example.invalid/x.git')
        self.assertFalse(ok)

    def test_a_secret_a_later_commit_deletes_is_still_refused(self):
        # T15: the content is in the commit that added it
        added = self.add('.pm-kit/hub/agent.token')
        self.repo.git('rm', '-q', '.pm-kit/hub/agent.token')
        self.repo.git('commit', '-q', '-m', 'drop it')
        ok, msg = self.check(self.repo.head())
        self.assertFalse(ok)
        self.assertIn(added[:12], msg)

    def test_a_deletion_of_a_secret_the_remote_has_and_other_files_pass(self):
        # T15: only the deletion is sent; the rest of the folder and a deeper .pm-kit are not secrets
        added = self.add('.pm-kit/hub/agent.token')
        self.repo.git('push', '-q', 'origin', '%s:refs/heads/b' % added)
        self.repo.git('rm', '-q', '.pm-kit/hub/agent.token')
        self.repo.git('commit', '-q', '-m', 'drop it')
        self.assertEqual(self.check(self.repo.head(), remote_sha=added), (True, None))
        for rel in ('.pm-kit/config.json', 'sub/.pm-kit/hub/x.token', 'sub/.pm-kit/local.json', '.pm-kit/hub.md'):
            self.add(rel)
        self.assertEqual(self.check(self.repo.head(), remote_sha=added), (True, None))

    def test_a_secret_the_remote_tracks_is_not_sent_again(self):
        # a new ref: its commits the remote's tracking refs hold are not sent, by tip or by pattern
        added = self.add('.pm-kit/hub/agent.token')
        self.repo.git('push', '-q', 'origin', '%s:refs/heads/b' % added)
        self.repo.git('rm', '-q', '.pm-kit/hub/agent.token')
        self.repo.git('commit', '-q', '-m', 'drop it')
        for most in (prepush.MAX_TRACKING_ARGS, 0):
            with self.subTest(most), mock.patch.object(prepush, 'MAX_TRACKING_ARGS', most):
                self.assertEqual(self.check(self.repo.head(), ref='refs/heads/c'), (True, None))
                ok, msg = prepush.check(self.repo.root, ['refs/heads/c %s refs/heads/c %s'
                                                         % (self.repo.head(), self.ZERO)],
                                        gate_ok=lambda r, s: (None, 'none'), remote='https://example.invalid/x.git')
                self.assertFalse(ok)            # a URL has no tracking refs: everything is sent
                self.assertIn(added[:12], msg)

    def test_a_deletion_line_and_an_unknown_sha_pass(self):
        sha = self.add('.pm-kit/local.json')
        self.assertEqual(prepush.secrets_refusal(self.repo.root, ['refs/heads/b %s refs/heads/b %s'
                                                                  % (self.ZERO, sha)], 'origin'), None)
        self.assertEqual(prepush.secrets_refusal(self.repo.root, ['refs/heads/b %s refs/heads/b %s'
                                                                  % ('1' * 40, self.ZERO)], 'origin'), None)


if __name__ == '__main__':
    unittest.main()
