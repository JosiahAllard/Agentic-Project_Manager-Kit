"""start, deep and consultant (kitlib/launcher/start.py). Carried over from the PM repo's
launcher/test_start_deep.py and test_start_consultant.py: there each case ran the PowerShell in
dry-run mode (-Plan/-WhatIf), or a real run against a fake start-conductor.ps1; here --plan is
plan=True and a real run opens its window on a FakeHost, so no case can open a window."""
import datetime
import json
import os
import unittest

from kitlib.launcher import start, window
from kitlib.launcher import settings as S
from kitlib.lockmcp import PLUGIN_NAME
from tests.launcher.fakes import EXE, NOW, FakeHost, Home, role_of, session

DAY = datetime.timedelta(days=1)
STORE_EXE = 'C:' + chr(92) + 'Program Files' + chr(92) + 'WindowsApps' + chr(92) + 'a' + chr(92) + 'claude.exe'


def proc(pid, cmd, exe=EXE, name='claude.exe'):
    return {'Name': name, 'ProcessId': pid, 'ParentProcessId': 1, 'ExecutablePath': exe, 'CommandLine': cmd}


class Base(unittest.TestCase):
    def setUp(self):
        self.h = Home(self, accounts=('1', '2', '3'), repos=('alpha',))
        for n in ('1', '2', '3'):
            self.h.usage(n, five=(10, NOW + DAY / 8), week=(10, NOW + 2 * DAY))
        self.repo = self.h.repos['alpha']
        self.host = FakeHost(self.h.root)

    def full(self, n, pct, days=2, window_='week'):
        kw = {'five': (10, NOW + DAY / 8), 'week': (10, NOW + 2 * DAY)}
        kw[window_] = (pct, NOW + days * DAY)
        self.h.usage(n, **kw)

    def signed_out(self, n):
        os.remove(os.path.join(self.h.accounts[n][0], '.credentials.json'))


class Deep(Base):
    def setUp(self):
        Base.setUp(self)
        self.brief = os.path.join(self.repo, 'brief.md')
        with open(self.brief, 'w', encoding='utf-8') as f:
            f.write('brief')
        self.briefdir = os.path.join(self.h.root, 'briefs')

    def deep(self, plan=True, **kw):
        args = dict(repo_path=self.repo, requester='alpha-coord-a1', slug='net-race', brief=self.brief, account='1',
                    brief_dir=self.briefdir, plan=plan)
        args.update(kw)
        return '\n'.join(start.deep(self.h.settings, self.host, **args))

    def refused(self, msg, **kw):
        with self.assertRaises(S.LaunchError) as c:
            self.deep(**kw)
        self.assertIn(msg, str(c.exception))
        self.assertEqual(self.host.opened, [])
        return c.exception

    def test_the_fallback_names_the_pm_by_both_live_names(self):
        out = self.deep()
        self.assertIn("ListAgents shows its session as ''Project Manager'' in the desktop app, or "
                      "''pm-session'' when the launcher resumed it", out)

    def test_command_has_agent_model_effort_window_and_name(self):
        out = self.deep()
        self.assertIn('Would open a new window running:', out)
        self.assertIn("claude --agent pm-kit:deep-worker --model 'opus[1m]' --effort xhigh --autocompact 500000 "
                      "--permission-mode auto --remote-control 'deep-net-race' -n 'deep-net-race' ", out)
        self.assertIn("$Host.UI.RawUI.WindowTitle = 'deep-net-race'", out)
        self.assertIn('Set-Location %s' % window.q(self.repo), out)
        self.assertIn("stop --name 'deep-net-race'", out)
        self.assertEqual(role_of(out), 'deep-worker')
        self.assertEqual(self.host.opened, [])

    def test_prompt_names_brief_requester_and_worktree(self):
        out = self.deep()
        self.assertIn('Read your brief %s first' % self.brief, out)
        self.assertIn("SendMessage your report to ''alpha-coord-a1''", out)
        self.assertIn('make your own git worktree', out)

    def test_relative_brief_resolves_against_the_repo(self):
        out = self.deep(brief='brief.md', account='2')
        self.assertIn('Read your brief %s first' % self.brief, out)
        self.assertIn('$env:CLAUDE_CONFIG_DIR = %s' % window.q(self.h.accounts['2'][0]), out)

    def test_autocompact_is_configurable_and_bounded(self):
        self.assertIn('--autocompact 800000 ', self.deep(autocompact=800000))
        for bad in (50000, 1500000, 0):
            self.refused('100000..1000000', autocompact=bad)

    def test_brief_text_is_not_written_in_plan_mode(self):
        out = self.deep(brief=None, brief_text='find the race')
        self.assertIn('Would write the brief text (13 characters) to %s'
                      % os.path.join(self.briefdir, 'deep-net-race-'), out)
        self.assertFalse(os.path.exists(self.briefdir))

    def test_requester_quote_is_escaped(self):
        self.assertIn("SendMessage your report to ''o''brien'' (", self.deep(requester="o'brien"))

    def test_slug_requester_and_brief_refusals(self):
        for slug in ('Bad_Slug', 'a', '-ab', 'x' * 42, ''):
            self.refused("--slug '%s' must be" % slug, slug=slug)
        self.assertIn('deep-ab', self.deep(slug='ab'))
        for req in ('', '  ', 'a\nb'):
            self.refused('--requester must be', requester=req)
        self.refused('one of them', brief_text='x')
        self.refused('Give --brief', brief=None)
        self.refused('No brief file', brief='missing.md')
        self.refused('No folder', repo_path=os.path.join(self.h.root, 'none'))
        self.refused('account 4 is not in launcher.accounts', account='4')
        self.refused("--model 'opus;rm'", model='opus;rm')

    def test_full_account_is_refused_unless_forced(self):
        self.full('1', 96)
        e = self.refused('Account 1 is at 96% of its seven_day window')
        self.assertIn('--force', str(e))
        out = self.deep(force=True)
        self.assertIn('--force: starting on account 1 anyway; it is at 96% of its seven_day window', out)
        self.assertIn('Would open a new window running:', out)

    def test_five_hour_window_counts_too(self):
        self.full('2', 95, days=0.1, window_='five')
        self.refused('Account 2 is at 95% of its five_hour window', account='2')

    def test_a_window_past_its_reset_is_fresh(self):
        self.full('1', 99, days=-1)
        self.assertIn('Would open', self.deep())

    def test_auto_skips_a_full_account_and_refuses_when_all_are_full(self):
        self.full('1', 95, days=1)
        self.full('3', 95, days=2)
        self.full('2', 40, days=5)
        out = self.deep(account='auto')
        self.assertIn('auto picked account 2', out)
        self.assertIn(window.q(self.h.accounts['2'][0]), out)
        self.full('2', 96, days=5)
        e = self.refused('Account 1 is at 95%', account='auto')
        self.assertIn('auto picked account 1', e.lines)               # the soonest reset, when every one is full

    def test_account_not_signed_in(self):
        self.signed_out('3')
        self.refused('Account 3 is not signed in', account='3')

    def test_running_session_with_the_name_is_refused(self):
        self.host.procs = [session(4242, 'deep-net-race', agent='deep-worker')]
        self.refused("A session named 'deep-net-race' is already running (PID 4242)")

    def test_only_an_exact_live_name_blocks(self):
        self.host.procs = [proc(1, "claude --remote-control 'deep-net-race-2' -n x"),
                           proc(2, "claude --remote-control 'Deep-Net-Race'"),
                           proc(3, 'claude --remote-control deep-net-race', exe=STORE_EXE),
                           proc(4, 'node x --remote-control deep-net-race', name='node.exe')]
        self.assertIn('Would open', self.deep())

    def briefs(self):
        return os.listdir(self.briefdir) if os.path.isdir(self.briefdir) else []

    def test_brief_text_is_written_then_launched(self):
        out = self.deep(plan=False, brief=None, brief_text='the brief', slug='ab')
        self.assertEqual(len(self.host.opened), 1)
        self.assertIn("-n 'deep-ab'", self.host.opened[0])
        name, = self.briefs()
        with open(os.path.join(self.briefdir, name), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'the brief')
        self.assertIn('Wrote the brief to', out)
        self.assertIn(os.path.join(self.briefdir, name), self.host.opened[0])

    def test_failed_launch_removes_the_brief(self):
        class Broken(FakeHost):
            def open_window(self, text):
                raise RuntimeError('fake launch failure')
        self.host = Broken(self.h.root)
        e = self.refused('fake launch failure', plan=False, brief=None, brief_text='the brief')
        self.assertTrue(any(line.startswith('Launch failed; removed') for line in e.lines), e.lines)
        self.assertEqual(self.briefs(), [])

    def test_refusals_write_nothing(self):
        self.full('2', 97)
        self.signed_out('3')
        for kw, msg in [({'model': 'opus;rm'}, '--model'), ({'account': '2'}, 'Account 2 is at 97%'),
                        ({'account': '3'}, 'Account 3 is not signed in')]:
            self.refused(msg, plan=False, brief=None, brief_text='x', **kw)
            self.assertFalse(os.path.exists(self.briefdir), kw)

    def test_agent_temp_is_the_default_brief_folder(self):
        self.host = FakeHost(self.h.root, env={'AGENT_TEMP': os.path.join(self.h.root, 'agent-temp')})
        out = self.deep(brief=None, brief_text='x', brief_dir=None)
        self.assertIn(os.path.join(self.h.root, 'agent-temp', 'deep-briefs', 'deep-net-race-'), out)


class Consultant(Base):
    def setUp(self):
        Base.setUp(self)
        self.repo = os.path.join(self.h.root, 'repos', 'MyRepo')
        os.makedirs(os.path.join(self.repo, 'docs'))
        with open(os.path.join(self.repo, 'docs', 'conventions-digest.md'), 'w', encoding='utf-8') as f:
            f.write('digest')
        self.host = FakeHost(self.h.root, ignored=True)

    def cons(self, plan=True, **kw):
        args = dict(repo_path=self.repo, account='1', plan=plan)
        args.update(kw)
        return '\n'.join(start.consultant(self.h.settings, self.host, **args))

    def refused(self, msg, **kw):
        with self.assertRaises(S.LaunchError) as c:
            self.cons(**kw)
        self.assertIn(msg, str(c.exception))
        self.assertEqual(self.host.opened, [])
        return c.exception

    def live(self):
        self.host.procs = [session(4242, 'consultant-myrepo', agent='consultant')]

    def test_command_has_agent_model_effort_window_and_name(self):
        out = self.cons()
        self.assertIn('Would open a new window running:', out)
        self.assertIn("claude --agent pm-kit:consultant --model 'opus[1m]' --effort high --autocompact 350000 "
                      "--permission-mode auto --remote-control 'consultant-myrepo' -n 'consultant-myrepo' ", out)
        self.assertIn("$Host.UI.RawUI.WindowTitle = 'consultant-myrepo'", out)
        self.assertIn('Set-Location %s' % window.q(self.repo), out)
        self.assertIn("stop --name 'consultant-myrepo'", out)
        self.assertNotIn('Warning', out)
        self.assertEqual(role_of(out), 'consultant')

    def test_repo_slug_names_the_session(self):
        out = self.cons(repo='other')
        self.assertIn("-n 'consultant-other' ", out)
        self.assertIn('You are consultant-other, a consultant session', out)

    def test_prompt_reads_digest_and_notes_then_idles(self):
        out = self.cons()
        notes = start.consultant_notes_path(self.host, 'myrepo')        # agent temp: tests/launcher/test_consultant_notes.py
        self.assertIn('You are consultant-myrepo, a consultant session', out)
        self.assertIn('digest docs/conventions-digest.md, then your notes file %s ' % notes, out)
        self.assertIn("reply ''ready'', and stay idle until asked", out)
        self.assertIn("as ''engineering call for delegated logging''", out)
        self.assertIn('Never edit code, commit, land or dispatch builds', out)

    def test_notes_path_relative_or_absolute(self):
        out = self.cons(notes_path='notes/c.local.md')
        self.assertIn('your notes file %s ' % os.path.join(self.repo, 'notes', 'c.local.md'), out)
        elsewhere = os.path.join(self.h.root, 'outside.md')
        self.assertIn('your notes file %s ' % elsewhere, self.cons(notes_path=elsewhere))

    def test_autocompact_is_configurable_and_never_above_500k(self):
        for good in (100000, 500000):
            self.assertIn('--autocompact %d ' % good, self.cons(autocompact=good))
        for bad in (99999, 500001, 1000000, 0):
            self.refused('100000..500000', autocompact=bad)

    def test_refusals(self):
        for kw, msg in [({'repo': 'Bad_Repo'}, "--repo 'Bad_Repo'"),
                        ({'account': '4'}, 'account 4 is not in launcher.accounts'),
                        ({'model': 'opus;rm'}, '--model'),
                        ({'effort': 'huge'}, '--effort'),
                        ({'notes_path': "it's.md"}, '--notes-path'),
                        ({'notes_path': 'docs'}, 'is a folder'),
                        ({'repo_path': os.path.join(self.h.root, 'none')}, 'No folder')]:
            self.refused(msg, **kw)

    def test_full_account_is_refused_unless_forced(self):
        self.full('1', 96)
        self.refused('Account 1 is at 96% of its seven_day window')
        out = self.cons(force=True)
        self.assertIn('--force: starting on account 1 anyway', out)
        self.assertIn('Would open', out)

    def test_auto_picks_an_account_with_headroom(self):
        self.full('1', 95, days=1)
        self.full('3', 95, days=2)
        out = self.cons(account='auto')
        self.assertIn('auto picked account 2', out)
        self.assertIn(window.q(self.h.accounts['2'][0]), out)

    def test_account_not_signed_in(self):
        self.signed_out('3')
        self.refused('Account 3 is not signed in', account='3')

    def test_the_consultant_logs_calls_to_the_pm_by_both_live_names(self):
        out = self.cons()
        self.assertIn("ListAgents shows its session as ''Project Manager'' in the desktop app, or "
                      "''pm-session'' when the launcher resumed it", out)

    def test_one_consultant_per_repo_unless_replace(self):
        self.live()
        e = self.refused("A session named 'consultant-myrepo' is already running (PID 4242): one consultant per repo")
        self.assertIn('--replace', str(e))
        out = self.cons(replace=True)
        self.assertIn("--replace: a session named 'consultant-myrepo' is already running (PID 4242). "
                      'After the new one says ready, stop the old one:', out)
        self.assertIn("stop --name 'consultant-myrepo' --pid 4242", out)
        self.assertIn('Would open', out)

    def test_force_alone_still_refuses_a_duplicate(self):
        self.live()
        self.refused('is already running (PID 4242): one consultant per repo', force=True)

    def test_replace_alone_still_refuses_a_full_account(self):
        self.live()
        self.full('1', 95)
        self.refused('Account 1 is at 95% of its seven_day window', replace=True)
        out = self.cons(replace=True, force=True)
        self.assertIn('--force: starting on account 1 anyway', out)
        self.assertIn("--replace: a session named 'consultant-myrepo' is already running", out)
        self.assertIn('Would open', out)

    def test_only_an_exact_live_name_blocks(self):
        self.host.procs = [proc(1, "claude --remote-control 'consultant-myrepo-2' -n x"),
                           proc(2, "claude --remote-control 'Consultant-MyRepo'"),
                           proc(3, 'node x --remote-control consultant-myrepo', name='node.exe')]
        self.assertIn('Would open', self.cons())

    def test_missing_digest_warns_but_starts(self):
        digest = os.path.join(self.repo, 'docs', 'conventions-digest.md')
        os.remove(digest)
        out = self.cons()
        self.assertIn('Warning: no conventions digest at %s' % digest, out)
        self.assertIn('Would open', out)

    def test_a_notes_file_git_would_commit_warns(self):
        self.host.ignored = False
        self.assertIn('Warning: git does not ignore', self.cons(notes_path='docs/ops/c.local.md'))
        self.assertNotIn('Warning', self.cons())         # the default is in agent temp, outside the repo
        for ignored in (True, None):                     # ignored, or git could not say
            self.host.ignored = ignored
            self.assertNotIn('Warning', self.cons(notes_path='docs/ops/c.local.md'))

    def test_launch_passes_consultant_settings_and_writes_nothing(self):
        self.cons(plan=False)
        text, = self.host.opened
        self.assertIn("--agent pm-kit:consultant --model 'opus[1m]' --effort high --autocompact 350000 ", text)
        self.assertIn('$env:CLAUDE_CONFIG_DIR = %s' % window.q(self.h.accounts['1'][0]), text)
        self.assertFalse(os.path.exists(os.path.join(self.repo, 'docs', 'ops')))

    def test_refusals_launch_nothing(self):
        self.full('2', 97)
        self.signed_out('3')
        for kw, msg in [({'autocompact': 600000}, '100000..500000'), ({'account': '2'}, 'Account 2 is at 97%'),
                        ({'account': '3'}, 'Account 3 is not signed in')]:
            self.refused(msg, plan=False, **kw)


class Start(Base):
    def go(self, **kw):
        args = dict(repo='alpha', account='1', whatif=True)
        args.update(kw)
        return '\n'.join(start.start(self.h.settings, self.host, **args))

    def test_coordinator_command_carries_only_the_default_compact_window(self):
        out = self.go(name='x-coordinator-a1', prompt='go')
        self.assertIn("claude --agent pm-kit:coordinator --autocompact 200000 --permission-mode auto "
                      "--remote-control 'x-coordinator-a1' -n 'x-coordinator-a1' 'go'", out)
        for flag in ('--model', '--effort'):
            self.assertNotIn(flag, out)

    def test_repo_or_repo_path_and_account_or_org_one_of_each(self):
        for kw in ({'repo': None}, {'repo_path': self.repo}):
            with self.assertRaisesRegex(S.LaunchError, '--repo-path'):
                self.go(**kw)
        for kw in ({'account': None}, {'org': '1'}):
            with self.assertRaisesRegex(S.LaunchError, 'Give --account N or --org N'):
                self.go(**kw)

    def test_org_uses_its_folder_and_tag(self):
        org = os.path.join(self.h.root, 'org1')
        os.makedirs(org)
        s = S.Settings(dict(self.h.obj, orgs={'1': org}), self.h.root)
        with self.assertRaisesRegex(S.LaunchError, 'Org 1 is not signed in'):
            start.start(s, self.host, repo='alpha', org='1', whatif=True)
        with open(os.path.join(org, '.claude.json'), 'w', encoding='utf-8') as f:
            json.dump({'oauthAccount': {'x': 1}}, f)
        out = '\n'.join(start.start(s, self.host, repo='alpha', org='1', whatif=True))
        self.assertIn('$env:CLAUDE_CONFIG_DIR = %s' % window.q(org), out)
        self.assertIn("-n 'alpha-coordinator-o1'", out)
        with self.assertRaisesRegex(S.LaunchError, 'org 2 is not in launcher.orgs'):
            start.start(s, self.host, repo='alpha', org='2', whatif=True)

    def test_prompt_composition(self):
        self.assertIn(window.q(start.DEFAULT_PROMPT), self.go())
        self.assertIn("'Start up now: read your standing brief b.md first, then run your start-up routine",
                      self.go(brief='b.md'))
        self.assertIn("'Read your standing brief b.md first. go'", self.go(brief='b.md', prompt='go'))
        out = self.go(prompt='go', predecessor='old-a1')
        self.assertIn("'You succeed old-a1, which has checkpointed", out)
        self.assertTrue(out.rstrip().endswith("go'\""), out[-40:])      # the what-if line closes its quote

    def test_default_prompt_points_at_the_coordinator_state_note_in_agent_temp(self):
        self.assertNotIn('state.local.md', start.DEFAULT_PROMPT)
        self.assertIn('<repo slug>-coordinator', start.DEFAULT_PROMPT)
        self.assertIn('AGENT_TEMP else TEMP', start.DEFAULT_PROMPT)
        self.assertIn('state-<your session name>.md', start.DEFAULT_PROMPT)

    def test_every_prompt_names_the_pm_by_both_live_session_names(self):
        for out in (self.go(), self.go(brief='b.md')):
            self.assertIn("ListAgents shows its session as ''Project Manager'' in the desktop app, or "
                          "''pm-session'' when the launcher resumed it", out)
        self.assertEqual((start.PM_DESKTOP_NAME, start.PM_NAME), ('Project Manager', 'pm-session'))

    def test_a_relaunched_pm_is_named_first_by_coordinators_json_pm_name(self):
        relaunched = "ListAgents shows its session as ''pm-b'', the name launch.py pm-relaunch gave it"
        self.h.write_state('coordinators.json', {'alpha': 1, 'pm_session_id': 's', 'pm_name': 'pm-b'})
        for out in (self.go(), self.go(brief='b.md')):
            self.assertIn(relaunched, out)
            self.assertIn("else ''Project Manager'' or ''pm-session''", out)
        for unset in ({'alpha': 1}, {'alpha': 1, 'pm_name': '<the PM name>'}):
            self.h.write_state('coordinators.json', unset)
            out = self.go()
            self.assertNotIn('pm-relaunch gave it', out)
            self.assertIn(window.q(start.DEFAULT_PROMPT), out)

    def test_the_predecessor_text_serialises_no_landing(self):
        text = start.predecessor_text('x')
        for old in (r'leave .*landing slot', 'hands them over', r'(?<!never )wait'):
            self.assertNotRegex(text, old)
        self.assertIn('lands nothing', text)
        self.assertIn('every LAND verdict', text)
        self.assertIn('cleared for landing', text)
        self.assertIn('hold the rest until the owner gives the go', text)
        self.assertIn('never wait on the old session', text)
        self.assertNotIn('--auto', text)

    def test_only_a_coordinators_successor_is_told_to_land(self):
        out = self.go(prompt='go', predecessor='old-a1')
        self.assertIn('Queue every LAND verdict', out)
        for kw in ({'agent': 'consultant'}, {'agent': 'deep-worker'}, {'no_agent': True}):
            out = self.go(prompt='go', predecessor='old-a1', **kw)
            self.assertIn('You succeed old-a1', out)
            self.assertNotIn('LAND', out)
            self.assertNotIn('lands nothing', out)

    def test_the_default_account_folder_removes_config_dir(self):
        default = os.path.join(self.h.root, '.claude')
        os.makedirs(default)
        open(os.path.join(default, '.credentials.json'), 'w').close()
        s = S.Settings(dict(self.h.obj, accounts={'1': ['~/.claude']}), self.h.root)
        out = '\n'.join(start.start(s, self.host, repo='alpha', account='1', whatif=True))
        self.assertIn('Remove-Item Env:CLAUDE_CONFIG_DIR', out)
        self.assertNotIn('$env:CLAUDE_CONFIG_DIR =', out)
        self.assertIn('$env:CLAUDE_CONFIG_DIR = %s' % window.q(self.h.accounts['1'][0]), self.go())

    def test_agent_temp_lines(self):
        self.assertNotIn('$env:TEMP', self.go())
        temp = os.path.join(self.h.root, 'agent-temp')
        self.host = FakeHost(self.h.root, env={'AGENT_TEMP': temp})
        out = self.go()
        for line in ('if (-not (Test-Path %s)) { New-Item -ItemType Directory -Force %s | Out-Null }'
                     % (window.q(temp), window.q(temp)),
                     '$env:AGENT_TEMP = %s' % window.q(temp),
                     '$env:TEMP = %s' % window.q(temp), '$env:TMP = %s' % window.q(temp)):
            self.assertIn(line, out)
        self.assertLess(out.index('$env:TMP'), out.index('Set-Location'))

    def test_the_compact_window_defaults_per_kind(self):
        self.assertIn('--autocompact 200000 ', self.go(no_agent=True))
        self.assertIn('--autocompact 200000 ', self.go(agent='analyst'))
        self.assertIn('--autocompact 300000 ', self.go(autocompact=300000))
        self.assertNotIn('--autocompact', self.go(autocompact=0))
        self.assertEqual((start.DEFAULT_AUTOCOMPACT, start.CONSULTANT_DEFAULT_AUTOCOMPACT, start.DEEP_AUTOCOMPACT),
                         (200000, 350000, 500000))

    def test_a_hand_off_or_reboot_start_gets_the_default_too(self):
        self.h.write_state('sessions.json', {'rev': {'repo': 'alpha', 'agent': True, 'stem': 'alpha-coord',
                                                     'state': 's.md', 'account': 2, 'name': 'alpha-coord-a2'}})
        out = '\n'.join(start.start_entry(self.h.settings, self.host, 'rev', whatif=True))
        self.assertIn('--autocompact 200000 ', out)

    def test_an_entry_of_a_long_role_gets_that_roles_window(self):
        for agent, window_ in (('consultant', 350000), ('deep-worker', 500000), ('analyst', 200000)):
            self.h.write_state('sessions.json', {'k': {'repo': 'alpha', 'agent': agent, 'stem': 'alpha-x',
                                                       'state': 's.md', 'account': 2}})
            out = '\n'.join(start.start_entry(self.h.settings, self.host, 'k', whatif=True))
            self.assertIn('--autocompact %d ' % window_, out)

    def test_a_plugin_prefixed_agent_is_named_bare_and_keeps_its_roles_window(self):
        p = PLUGIN_NAME + ':'
        out = self.go(agent=p + 'deep-worker')
        self.assertIn('--agent %sdeep-worker ' % p, out)
        self.assertIn("-n 'alpha-deep-worker-a1'", out)
        for agent, window_ in ((p + 'consultant', 350000), (p + 'deep-worker', 500000), (p + 'analyst', 200000),
                               ('other:consultant', 200000)):
            with self.subTest(agent):
                args = start.entry_start_args(self.h.settings, 'k', {'repo': 'alpha', 'agent': agent,
                                                                     'stem': 'alpha-x'}, '2', None)
                self.assertEqual(args.get('autocompact', start.DEFAULT_AUTOCOMPACT), window_)
                self.assertEqual(args['agent'], agent)
        for bad in ('other:deep-worker', 'a:b:c', ':x', p, p + p + 'x'):
            with self.subTest(bad):
                with self.assertRaisesRegex(S.LaunchError, 'is not an agent name'):
                    self.go(agent=bad)

    def test_a_bare_kit_agent_starts_under_the_plugins_prefix(self):
        """The plugin registers its agents only as pm-kit:<name>; a bare one is qualified, named bare."""
        out = self.go(agent='analyst')
        self.assertIn('--agent pm-kit:analyst ', out)
        self.assertIn("-n 'alpha-analyst-a1'", out)
        self.h.write_state('sessions.json', {'k': {'repo': 'alpha', 'agent': 'analyst', 'stem': 'alpha-x',
                                                   'state': 's.md', 'account': 2}})
        out = '\n'.join(start.start_entry(self.h.settings, self.host, 'k', whatif=True))
        self.assertIn('--agent pm-kit:analyst ', out)

    def test_a_repos_own_agent_starts_as_given(self):
        for agent in ('my-repo-agent', 'conductor'):
            with self.subTest(agent):
                self.assertIn('--agent %s ' % agent, self.go(agent=agent))

    def test_the_pm_starts_as_the_kits_pm_agent_with_the_pm_mark(self):
        """decision 11: the PM is an agent file; a launched PM runs --agent pm-kit:pm, marked pm."""
        out = self.go(agent='pm')
        self.assertIn('--agent pm-kit:pm ', out)
        self.assertIn("PM_KIT_ROLE = 'pm'", out)

    def test_an_old_kit_agent_name_starts_as_its_new_one(self):
        """opus-worker became senior-worker; the old name is an alias for one release."""
        self.assertEqual(start.run_as('opus-worker'), 'pm-kit:senior-worker')
        self.assertIn('--agent pm-kit:senior-worker ', self.go(agent='opus-worker'))
        for old, new in start.AGENT_ALIASES.items():
            self.assertIn(new, start.KIT_AGENTS)
            self.assertNotIn(old, start.KIT_AGENTS)

    def test_kit_agents_are_the_plugins_agent_files(self):
        here = os.path.dirname(os.path.abspath(__file__))
        folder = os.path.join(here, '..', '..', 'hosts', 'claude', 'shell', 'agents')
        stems = sorted(f[:-3] for f in os.listdir(folder) if f.endswith('.md'))
        self.assertEqual(sorted(start.KIT_AGENTS), stems)
        roles = os.path.join(here, '..', '..', 'content', 'roles')        # the source they render from
        self.assertEqual(sorted(start.KIT_AGENTS), sorted(f[:-3] for f in os.listdir(roles) if f.endswith('.md')))

    def rec(self, **kw):
        e = {'repo': 'alpha', 'agent': True, 'stem': 'alpha-coord', 'state': 's.md', 'account': 1,
             'name': 'alpha-coord-a1-r2'}
        e.update(kw)
        self.h.write_state('sessions.json', {'rev': e})

    def test_start_entry_keeps_the_recorded_name_on_the_same_account(self):
        self.rec()
        for acct in (None, '1'):
            out = '\n'.join(start.start_entry(self.h.settings, self.host, 'rev', account=acct, whatif=True))
            self.assertIn("-n 'alpha-coord-a1-r2'", out)
        out = '\n'.join(start.start_entry(self.h.settings, self.host, 'rev', account='2', whatif=True))
        self.assertIn("-n 'alpha-coord-a2'", out)
        self.rec(name=None)
        out = '\n'.join(start.start_entry(self.h.settings, self.host, 'rev', whatif=True))
        self.assertIn("-n 'alpha-coord-a1'", out)

    def test_start_entry_refuses_while_its_stem_runs(self):
        self.rec()
        self.host.procs = [proc(5, "claude --remote-control 'alpha-coord-a1-r2' -n 'alpha-coord-a1-r2' 'x'")]
        with self.assertRaisesRegex(S.LaunchError, 'already running'):
            start.start_entry(self.h.settings, self.host, 'rev')
        self.assertEqual(self.host.opened, [])


if __name__ == '__main__':
    unittest.main()
