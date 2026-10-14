"""The hub rules (docs/guard.md "Hub secrets"): hub-secrets, hub-owner-verb, hub-mcp and
hub-browser. Each holds every session but the owner's own: a subagent, a launcher-marked
session (coordinator, pm, plain). The owner's unmarked session is never held. Agents still run
the agent verbs of tools/hub.py and its validate, serve and status, which read agent.token
in-process; those pass."""
import os
import subprocess
import sys
import unittest

from kitlib.guard import context

from . import world as W
from .claudehost import action, decide
from .world import bash, event, ps

COORDINATOR = {context.ROLE_ENV: 'coordinator'}
PM = {context.ROLE_ENV: 'pm'}
PLAIN = {context.ROLE_ENV: 'plain'}
OWNER = {}

# every session the rules hold: (label, subagent, environ)
HELD = (('subagent', True, OWNER), ('coordinator', False, COORDINATOR), ('pm', False, PM),
        ('plain', False, PLAIN))


class Hub(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.w.git.ancestors.add((W.SHA_WT, 'main'))
        self.w.user_config({'pm_data': {'path': self.w.data}})
        self.secrets = os.path.join(self.w.home, '.pm-kit', 'hub')
        self.store = os.path.join(self.w.data, 'hub')
        self.hub = W.fwd(os.path.join(self.w.kit_dir, 'tools', 'hub.py'))

    def ctx(self, environ):
        return self.w.ctx(environ=dict(environ))

    def denied(self, make, rule, sessions=HELD):
        """`make(sub)` builds the payload; every session in `sessions` is denied by `rule`."""
        for label, sub, environ in sessions:
            with self.subTest(session=label):
                d = decide(make(sub), self.ctx(environ))
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)
                self.assertIn(' Instead: ', d.reason)

    def allowed(self, make, sessions=HELD):
        for label, sub, environ in sessions:
            with self.subTest(session=label):
                d = decide(make(sub), self.ctx(environ))
                self.assertIsNone(d, d and d.reason)

    def owner_allowed(self, make):
        self.allowed(make, (('owner', False, OWNER),))


# ---------------------------------------------------------------- hub-secrets: edits

class SecretEdits(Hub):
    def write(self, path):
        return lambda sub: event('Write', {'file_path': path, 'content': 'x'}, self.w.wt, sub)

    def test_token_and_key_files(self):
        for name in ('owner.token', 'agent.token', 'approval.key'):
            with self.subTest(name):
                self.denied(self.write(os.path.join(self.secrets, name)), 'hub-secrets')
                self.owner_allowed(self.write(os.path.join(self.secrets, name)))

    def test_a_temp_file_in_the_secret_folder(self):
        self.denied(self.write(os.path.join(self.secrets, 'owner.token.0a1b.tmp')), 'hub-secrets')

    def test_the_store(self):
        self.denied(self.write(os.path.join(self.store, 'questions', 'Q-1.json')), 'hub-secrets')
        self.owner_allowed(self.write(os.path.join(self.store, 'questions', 'Q-1.json')))

    def test_an_explicit_store(self):
        store = os.path.join(self.w.temp, 'hubstore')
        self.w.user_config({'pm_data': {'path': self.w.data}, 'hub': {'store': store}})
        self.denied(self.write(os.path.join(store, 'todos', 'T-1.json')), 'hub-secrets')
        self.allowed(self.write(os.path.join(self.w.temp, 'other.txt')))

    def test_an_unrelated_write_passes(self):
        self.allowed(lambda sub: event('Write', {'file_path': os.path.join(self.w.wt, 'a.py'), 'content': 'x'},
                                       self.w.wt, sub), (('subagent', True, OWNER),))


# ---------------------------------------------------------------- hub-secrets: shell

class SecretShell(Hub):
    def test_reads_of_the_token_files(self):
        sec = W.fwd(self.secrets)
        rows = [
            ('cat ~', lambda sub: bash('cat ~/.pm-kit/hub/owner.token', self.w.wt, sub)),
            ('cat absolute', lambda sub: bash('cat "%s/owner.token"' % sec, self.w.wt, sub)),
            ('type absolute', lambda sub: ps('type "%s\\agent.token"' % self.secrets, self.w.wt, sub)),
            ('Get-Content env', lambda sub: ps('Get-Content "$env:USERPROFILE\\.pm-kit\\hub\\agent.token"',
                                               self.w.wt, sub)),
            ('Join-Path', lambda sub: ps("$d = 'x'; Get-Content (Join-Path $d 'owner.token')", self.w.wt, sub)),
            ('approval key', lambda sub: bash('cat "%s/approval.key"' % sec, self.w.wt, sub)),
            ('list the folder', lambda sub: bash('ls "%s"' % sec, self.w.wt, sub)),
            ('cd then read', lambda sub: bash('cd "%s" && cat agent.token' % sec, self.w.wt, sub)),
            ('copy out', lambda sub: bash('cp "%s/owner.token" /tmp/x' % sec, self.w.wt, sub)),
            ('nested shell', lambda sub: ps('powershell -Command "Get-Content %s\\owner.token"' % self.secrets,
                                            self.w.wt, sub)),
        ]
        for name, make in rows:
            with self.subTest(name):
                self.denied(make, 'hub-secrets')

    def test_writes_of_the_token_files(self):
        sec = W.fwd(self.secrets)
        self.denied(lambda sub: bash('echo x > "%s/owner.token"' % sec, self.w.wt, sub), 'hub-secrets')
        self.denied(lambda sub: ps('Set-Content -Path "%s\\agent.token" -Value x' % self.secrets, self.w.wt, sub),
                    'hub-secrets')
        self.denied(lambda sub: bash('rm "%s/approval.key"' % sec, self.w.wt, sub), 'hub-secrets')

    def test_the_store(self):
        st = W.fwd(self.store)
        self.denied(lambda sub: bash('ls "%s"' % st, self.w.wt, sub), 'hub-secrets')
        self.denied(lambda sub: bash('cat "%s/questions/Q-1.json"' % st, self.w.wt, sub), 'hub-secrets')
        self.denied(lambda sub: bash('cd "%s" && ls hub' % W.fwd(self.w.data), self.w.wt, sub), 'hub-secrets')
        self.denied(lambda sub: ps('Remove-Item "%s\\todos\\T-1.json"' % self.store, self.w.wt, sub), 'hub-secrets')

    def test_an_explicit_store(self):
        store = os.path.join(self.w.temp, 'hubstore')
        self.w.user_config({'pm_data': {'path': self.w.data}, 'hub': {'store': store}})
        self.denied(lambda sub: bash('cat "%s/todos/T-1.json"' % W.fwd(store), self.w.wt, sub), 'hub-secrets')

    def test_python_code_that_reads_a_token(self):
        code = "from kitlib.hubsite import auth; print(auth.read_token(auth.folder(), 'owner'))"
        self.denied(lambda sub: bash('python -c "%s"' % code, self.w.wt, sub), 'hub-secrets')
        self.denied(lambda sub: bash("python - <<'EOF'\n%s\nEOF" % code, self.w.wt, sub), 'hub-secrets')
        self.denied(lambda sub: bash("echo \"%s\" | python -" % code, self.w.wt, sub), 'hub-secrets')
        self.denied(lambda sub: bash("node -e \"require('fs').readFileSync(process.env.H + '/owner.token')\"",
                                     self.w.wt, sub), 'hub-secrets')

    def test_a_hub_call_naming_a_token_file(self):
        self.denied(lambda sub: bash('python "%s" questions add --title t --context-file "%s/owner.token"'
                                     % (self.hub, W.fwd(self.secrets)), self.w.wt, sub), 'hub-secrets')

    def test_the_owner_reads_them(self):
        self.owner_allowed(lambda sub: bash('cat "%s/owner.token"' % W.fwd(self.secrets), self.w.wt, sub))
        self.owner_allowed(lambda sub: bash('ls "%s"' % W.fwd(self.store), self.w.wt, sub))

    def test_unrelated_commands_pass(self):
        for cmd in ('cat README.md', 'grep -rn owner.token kitlib', 'rg "agent.token" docs',
                    'git log --oneline -3', 'ls "%s"' % W.fwd(self.w.data),
                    'cat "%s/.kit.json"' % W.fwd(self.w.data), 'python -m unittest discover -s tests/hubsite'):
            with self.subTest(cmd):
                self.allowed(lambda sub, cmd=cmd: bash(cmd, self.w.wt, sub))
        self.allowed(lambda sub: ps('Select-String -Path docs\\*.md -Pattern owner.token', self.w.wt, sub))

    def test_unclean_lines_are_judged_on_what_was_read(self):
        # the fallback has no hub pattern; the hub rules run on what the parser did read
        rows = (
            ('unclosed quote', lambda sub: bash('cat ~/.pm-kit/hub/owner.token "', self.w.wt, sub), 'hub-secrets'),
            ('after eval', lambda sub: bash('eval "$c"; cat ~/.pm-kit/hub/agent.token', self.w.wt, sub),
             'hub-secrets'),
            ('PowerShell iex', lambda sub: ps('iex $c; Get-Content $HOME\\.pm-kit\\hub\\owner.token', self.w.wt, sub),
             'hub-secrets'),
            ('owner verb', lambda sub: bash('eval "$c"; python "%s" answer Q-1' % self.hub, self.w.wt, sub),
             'hub-owner-verb'),
        )
        for name, make, rule in rows:
            with self.subTest(name):
                self.denied(make, rule)

    def test_hub_tool_with_the_store_passes(self):
        st = W.fwd(self.store)
        self.allowed(lambda sub: bash('python "%s" validate --store "%s"' % (self.hub, st), self.w.wt, sub))
        self.allowed(lambda sub: bash('python "%s" validate --store="%s"' % (self.hub, st), self.w.wt, sub))


class HomeAndWildcards(Hub):
    """The home folder however a line spells it (`~`, $HOME, ${HOME}, $env:USERPROFILE,
    $env:HOME, %USERPROFILE%), in a word or in the directory a cd moves to, read as the guard's
    home (the World's, never the real one); and wildcard paths that can name the folders."""

    def rows_denied(self, rows):
        for shell, line in rows:
            with self.subTest(line=line):
                self.denied(lambda sub, shell=shell, line=line: shell(line, self.w.wt, sub), 'hub-secrets')

    def rows_allowed(self, rows):
        for shell, line in rows:
            with self.subTest(line=line):
                self.allowed(lambda sub, shell=shell, line=line: shell(line, self.w.wt, sub))

    def test_home_variables_with_wildcards_are_denied(self):
        self.rows_denied((
            (bash, 'cat $HOME/.pm-kit/h*/*'),
            (bash, 'cat ${HOME}/.pm-kit/h*/*'),
            (bash, 'cat "$HOME"/.pm-kit/h*/owner.t*'),
            (bash, 'cat $USERPROFILE/.pm-kit/h?b/a*'),
            (ps, 'gc $HOME\\.pm-kit\\*\\*.token'),
            (ps, 'gc "$env:USERPROFILE\\.pm-kit\\h?b\\owner.t*"'),
            (ps, 'gc $env:HOME\\.pm-kit\\h*\\a*'),
            (ps, 'gc ${env:USERPROFILE}\\.pm-kit\\h*\\o*'),
            (ps, 'Get-Content -Path:$HOME\\.pm-kit\\h*\\o*'),
            (ps, 'cmd /c "type %USERPROFILE%\\.pm-kit\\h*\\o*"'),
        ))

    def test_a_cd_into_the_home_folder_is_followed(self):
        self.rows_denied((
            (bash, 'cd ~/.pm-kit && cat h*/o*'),
            (bash, 'cd ~ && cat .pm-kit/h*/*'),
            (bash, 'cd $HOME/.pm-kit && cat h*/o*'),
            (bash, 'cd "$HOME/.pm-kit/hub" && cat *'),
            (bash, 'cd ~/.pm-kit && bash -c "cat h*/o*"'),
            (ps, 'Set-Location ~/.pm-kit; gc h*/o*'),
            (ps, 'sl ~\\.pm-kit; gc h*\\o*'),
            (ps, 'Push-Location ~\\.pm-kit; gc h*\\*'),
            (ps, 'Set-Location $env:USERPROFILE\\.pm-kit; gc h*\\o*'),
            (ps, 'cd $HOME; gc .pm-kit\\h*\\*'),
            (ps, 'cmd /c "cd /d %USERPROFILE%\\.pm-kit && type h*\\o*"'),
        ))

    def test_a_hub_call_with_a_home_variable(self):
        self.rows_denied((
            (bash, 'python "%s" questions add --title t --context-file "$HOME/.pm-kit/h*/owner.t*"' % self.hub),
            (bash, 'python "%s" questions add --title t --context-file $HOME/.pm-kit/h*/owner.t*' % self.hub),
            (bash, 'python "%s" questions add --title t --context-file=$HOME/.pm-kit/h*/owner.t*' % self.hub),
            (ps, 'python "%s" questions add --title t --context-file $HOME\\.pm-kit\\h*\\owner.t*' % self.hub),
        ))

    def test_the_rest_of_the_home_folder_passes(self):
        self.rows_allowed((
            (bash, 'cat $HOME/.gitconfig'),
            (bash, 'ls $HOME/.pm-kit'),
            (bash, 'cat $HOME/.pm-kit/*.json'),
            (bash, 'cat $HOME/$x/h*'),
            (bash, 'cd ~ && ls'),
            (bash, 'cd $HOME && cat .gitconfig'),
            (ps, 'gc $env:USERPROFILE\\.pm-kit\\hub.json'),
            (ps, 'Set-Location $env:USERPROFILE; gc .gitconfig'),
        ))

    def test_absolute_wildcards_that_reach_the_folders(self):
        home, data = W.fwd(self.w.home), W.fwd(self.w.data)
        self.rows_denied((
            (bash, 'cat %s/.pm-kit/h?b/*' % home),
            (bash, 'ls %s/.pm-kit/*' % home),
            (bash, 'ls %s/**' % home),
            (bash, 'cat %s/*/questions/*.json' % data),
            (bash, 'ls %s/h*' % data),
            (ps, 'gc %s\\.pm-kit\\[h]ub\\o*' % self.w.home),
        ))
        self.rows_allowed((
            (bash, 'ls %s/.pm-kit/*.json' % home),
            (bash, 'cat %s/*.json' % data),
        ))

    def test_option_values_are_read_as_paths(self):
        # `--opt=value` and PowerShell's `-Opt:value`: the value is the path (hub_path_texts)
        sec, st = W.fwd(self.secrets), W.fwd(self.store)
        self.rows_denied((
            (ps, 'Get-Content -Path:%s\\questions\\Q-1.json' % self.store),
            (ps, 'Get-Content -LiteralPath:%s\\o*' % self.secrets),
            (bash, 'some-tool --input=%s/questions/Q-1.json' % st),
            (bash, 'python "%s" questions add --title t --context-file=%s/a*' % (self.hub, sec)),
        ))
        self.rows_allowed((
            (bash, 'some-tool --input=%s/.kit.json' % W.fwd(self.w.data)),
            (ps, 'Get-Content -Path:README.md'),
        ))

    def test_an_edit_path_in_the_folder_with_wildcard_characters(self):
        # an edit's path is literal (no wildcard runs), so it is judged by where it is: a name
        # with wildcard characters in the token folder is in it
        write = lambda path: (lambda sub: event('Write', {'file_path': path, 'content': 'x'}, self.w.wt, sub))
        self.denied(write(os.path.join(self.secrets, 'owner.t*')), 'hub-secrets')
        self.denied(write(os.path.join(self.store, '[q]uestions', 'Q-1.json')), 'hub-secrets')
        # a folder literally named h* is not the token folder (other rules may still hold the write)
        d = decide(write(os.path.join(self.w.home, '.pm-kit', 'h*', 'owner.token'))(True), self.ctx(OWNER))
        self.assertNotEqual(d and d.rule, 'hub-secrets', d and d.reason)

    def test_a_config_the_strict_read_refuses_still_names_the_store(self):
        # hub_config: a config with a fault elsewhere still gives its hub.store
        store = os.path.join(self.w.temp, 'hubstore')
        self.w.user_config({'pm_data': {'path': self.w.data}, 'hub': {'store': store}, 'no_such_key': 1})
        self.assertTrue(self.ctx(COORDINATOR).user().get('error'), 'the strict read took the config')
        self.denied(lambda sub: bash('cat "%s/todos/T-1.json"' % W.fwd(store), self.w.wt, sub), 'hub-secrets')


# ---------------------------------------------------------------- hub-owner-verb

class OwnerVerbs(Hub):
    def run_hub(self, args, shell=bash):
        return lambda sub: shell('python "%s" %s' % (self.hub, args), self.w.wt, sub)

    DENIED = (
        'answer Q-1 --choice 1',
        'answer Q-1 --choice other --note "fine" --if-version 2',
        'todos done T-1',
        'todos done T-1 --undo',
        'todos --port 8790 done T-1',
        'todos --port=8790 done T-1',
        'todos --po 8790 done T-1',
        'todos --json --status open done T-1',
        'todos -- done T-1',
        'stories add --repo r --title t --story-file s.md',
        'stories withdraw S-1',
        'to-pm add --text hi',
        'to-pm send',
        'inbox read M-1',
        'open',
        'open --print',
        'pair',
        'approvals approve A-1',
        'approvals deny A-1',
        'todos add --title t --owner',
        'todos add --title t --own',
        'todos add --owner=1 --title t',
        '-h answer Q-1 --choice 1',
    )
    ALLOWED = (
        'questions',
        'questions --status open',
        'questions add --title "Which way?" --option "a::first" --option "b::second"',
        'questions log Q-1 R-1 --if-version 2',
        'todos',
        'todos --repo demo',
        'todos add --title t --detail d --from worker',
        'todos ack T-1',
        'stories',
        'stories set S-1 --status review --if-version 1',
        'to-pm',
        'to-pm reply P-1 "done" R-2 --if-version 3',
        'inbox',
        'inbox add --text "landed" --title t',
        'pending',
        'wait --pending --timeout 5',
        'status',
        'validate',
        'serve',
        'setup --whatif',
        'approvals',
    )

    def test_owner_verbs_are_denied(self):
        for args in self.DENIED:
            with self.subTest(args):
                self.denied(self.run_hub(args), 'hub-owner-verb')
                self.owner_allowed(self.run_hub(args))

    def test_hub_run_as_a_module(self):
        # `python -m tools.hub` from the kit folder, or `-m hub` from tools/, runs the same parser
        for line in ('python -m tools.hub answer Q-1 --choice 1', 'cd tools && python -m hub todos done T-1',
                     'python -mtools.hub open', 'python -B -m tools.hub to-pm send'):
            with self.subTest(line):
                self.denied(lambda sub, line=line: bash(line, self.w.wt, sub), 'hub-owner-verb')
        self.denied(lambda sub: ps('python -m tools.hub pair', self.w.wt, sub), 'hub-owner-verb')
        for line in ('python -m tools.hub questions', 'python -m tools.hub todos add --title t',
                     'python -m hubby answer Q-1', 'python -m tools.hubsite answer'):
            with self.subTest(line):
                self.allowed(lambda sub, line=line: bash(line, self.w.wt, sub))

    def test_agent_verbs_pass(self):
        for args in self.ALLOWED:
            with self.subTest(args):
                self.allowed(self.run_hub(args))

    def test_quoted_variables_in_values_pass(self):
        self.allowed(lambda sub: bash('t="Ask"; python "%s" todos add --title "$t"' % self.hub, self.w.wt, sub))
        self.allowed(lambda sub: bash('python "%s" todos add --title="$t"' % self.hub, self.w.wt, sub))
        self.allowed(lambda sub: bash('python "%s" questions add --title "$T"' % self.hub, self.w.wt, sub))

    def test_powershell_variables_after_hub_py_are_denied(self):
        # Windows PowerShell 5.1 passes a value holding a double quote on unescaped, so even a
        # quoted variable can split into --owner; a literal word holding one is denied too
        self.denied(lambda sub: ps('$t = "Ask"; python "%s" todos add --title "$t" --detail "$d"' % self.hub,
                                   self.w.wt, sub), 'hub-owner-verb')
        self.denied(lambda sub: ps('python "%s" todos add --title \'a" --owner "b\'' % self.hub, self.w.wt, sub),
                    'hub-owner-verb')
        self.allowed(lambda sub: ps('python "%s" todos add --title "Ask the owner"' % self.hub, self.w.wt, sub))

    def test_values_the_guard_cannot_read_are_denied(self):
        # an unquoted variable may split into --owner, a sub-verb or an option
        rows = (
            ('verb', 'v=answer; python "%s" $v Q-1 --choice 1'),
            ('sub-verb', 'python "%s" todos "$s" T-1'),
            ('unquoted value', 'python "%s" todos --repo $r done T-1'),
            ('splitting title', 'python "%s" todos add --title $t'),
            ('list option', 'python "%s" todos add --title t --refs R-1 "$x"'),
        )
        for name, cmd in rows:
            with self.subTest(name):
                self.denied(lambda sub, cmd=cmd: bash(cmd % self.hub, self.w.wt, sub), 'hub-owner-verb')

    def test_launchers(self):
        rows = (
            ('py -3', lambda sub: ps('py -3 tools\\hub.py answer Q-1 --choice 1', self.w.wt, sub)),
            ('relative', lambda sub: bash('python tools/hub.py todos done T-1', self.w.wt, sub)),
            ('python -X', lambda sub: bash('python -X utf8 tools/hub.py to-pm send', self.w.wt, sub)),
            ('Start-Process', lambda sub: ps('Start-Process python -ArgumentList "tools/hub.py answer Q-1 --choice 1"',
                                             self.w.wt, sub)),
            ('nested bash', lambda sub: bash('bash -c "python tools/hub.py inbox read M-1"', self.w.wt, sub)),
            ('nested powershell', lambda sub: ps('powershell -Command "python tools/hub.py stories withdraw S-1"',
                                                 self.w.wt, sub)),
            ('cmd', lambda sub: ps('cmd /c python tools\\hub.py open', self.w.wt, sub)),
            ('script variable', lambda sub: ps('$s = "tools/hub.py"; python $s answer Q-1 --choice 1', self.w.wt, sub)),
            ('python -c', lambda sub: bash('python -c "import runpy, sys; sys.argv = [\'hub.py\', \'to-pm\', '
                                           '\'send\']; runpy.run_path(\'tools/hub.py\')"', self.w.wt, sub)),
        )
        for name, make in rows:
            with self.subTest(name):
                d = decide(make(True), self.ctx(OWNER))
                self.assertIsNotNone(d, 'allowed')
                self.assertIn(d.rule, ('hub-owner-verb', 'hub-secrets'), d.reason)

    def test_reading_hub_py_passes(self):
        for cmd in ('cat tools/hub.py', 'grep -n answer tools/hub.py', 'git diff tools/hub.py',
                    'git add tools/hub.py tests/hubsite/test_cli.py'):
            with self.subTest(cmd):
                self.allowed(lambda sub, cmd=cmd: bash(cmd, self.w.wt, sub), (('subagent', True, OWNER),))

    def test_the_alternative_names_the_kit_hub_tool(self):
        d = decide(self.run_hub('answer Q-1 --choice 1')(True), self.ctx(OWNER))
        self.assertEqual(d.instead, 'python "%s" questions add --title "<what the owner should decide>"' % self.hub)


# ---------------------------------------------------------------- hub-mcp

class HubMcp(Hub):
    def call(self, tool):
        return lambda sub: event(tool, {'id': 'Q-1', 'choice': 1}, self.w.wt, sub)

    def test_owner_tools_are_denied(self):
        for tool in ('mcp__plugin_pm-kit_hub__hub_answer', 'mcp__hub__hub_todo_done',
                     'mcp__plugin_pm-kit_hub__hub_story_submit', 'mcp__hub__hub_to_pm_send',
                     'mcp__plugin_pm-kit_hub__hub_approval_decide', 'mcp__hub__hub_approve',
                     'mcp__other__hub_answer'):
            with self.subTest(tool):
                self.denied(self.call(tool), 'hub-mcp')
                self.owner_allowed(self.call(tool))

    def test_agent_tools_pass(self):
        for tool in ('mcp__plugin_pm-kit_hub__hub_questions_list', 'mcp__hub__hub_question_ask',
                     'mcp__hub__hub_approvals_list', 'mcp__other__decide_later'):
            with self.subTest(tool):
                self.allowed(self.call(tool))

    def test_the_list_is_the_servers(self):
        # the guard's list of owner-only hub tools is the server's own
        from kitlib import hubmcp
        from kitlib.guard import hub
        self.assertEqual(set(hub.OWNER_MCP_TOOLS), set(hubmcp.OWNER_ONLY))

    def test_the_guard_does_not_load_the_server(self):
        # a fresh interpreter: loading the guard's rules imports kitlib.hubmcp's names only
        code = ('import sys; import kitlib.guard.rules; '
                'print(sorted(m for m in sys.modules if m.startswith(("kitlib.hubmcp", "kitlib.hubsite"))))')
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        out = subprocess.run([sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, %r); %s' % (root, code)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "['kitlib.hubmcp']")


# ---------------------------------------------------------------- hub-browser

class Browser(Hub):
    NAV = 'mcp__claude-in-chrome__navigate'

    def nav(self, url, tool=None):
        return lambda sub: event(tool or self.NAV, {'url': url, 'tabId': 1}, self.w.wt, sub)

    def test_the_hub_origin_is_denied(self):
        for url in ('http://127.0.0.1:8790/', 'http://localhost:8791/q/Q-1', 'http://[::1]:8790',
                    'http://127.1:8790/', 'http://2130706433:8790/', 'http://0x7f.0.0.1:8790/',
                    'http://LOCALHOST.:8790/', 'http://hub.localhost:8790/', '127.0.0.1:8790',
                    'http://user@127.0.0.1:8790/', 'http://[::ffff:127.0.0.1]:8790/', 'http://0.0.0.0:8790/',
                    'http://127.0.0.%31:8790/'):
            with self.subTest(url):
                self.denied(self.nav(url), 'hub-browser')
                self.owner_allowed(self.nav(url))

    def test_a_configured_port(self):
        self.w.user_config({'hub': {'port': 9000, 'tunnel_port': 9001}})
        self.denied(self.nav('http://127.0.0.1:9000/'), 'hub-browser')
        self.denied(self.nav('http://127.0.0.1:8790/'), 'hub-browser')

    def test_other_tools_and_inputs(self):
        rows = (
            ('tabs_create', 'mcp__claude-in-chrome__tabs_create', {'url': 'http://localhost:8790'}),
            ('javascript', 'mcp__claude-in-chrome__javascript_tool',
             {'action': 'javascript_exec', 'tabId': 1,
              'text': "fetch('http://127.0.0.1:8790/api/questions/Q-1/answer', {method: 'POST'})"}),
            ('form_input', 'mcp__claude-in-chrome__form_input', {'ref': 'r1', 'value': 'http://localhost:8790/pair'}),
            ('computer-use type', 'mcp__computer-use__type', {'text': 'localhost:8790\n'}),
            ('nested input', 'mcp__claude-in-chrome__computer', {'action': 'key', 'steps': [{'text': '127.0.0.1:8791'}]}),
        )
        for name, tool, ti in rows:
            with self.subTest(name):
                self.denied(lambda sub, tool=tool, ti=ti: event(tool, ti, self.w.wt, sub), 'hub-browser')

    def test_other_origins_pass(self):
        for url in ('https://example.com/', 'http://127.0.0.1:3000/', 'http://localhost:87900/',
                    'http://10.0.0.1:8790/', 'http://example.com:8790/', 'http://127.0.0.1/'):
            with self.subTest(url):
                self.allowed(self.nav(url))

    def test_other_servers_pass(self):
        self.allowed(lambda sub: event('mcp__search__fetch', {'url': 'http://127.0.0.1:8790/'}, self.w.wt, sub))


# ---------------------------------------------------------------- plumbing

class Plumbing(unittest.TestCase):
    def test_the_guard_constants_are_the_hub_sites(self):
        from kitlib.guard import hub
        from kitlib.hubsite import auth, settings
        self.assertEqual(hub.FOLDER_NAME, auth.FOLDER_NAME)
        self.assertEqual((hub.OWNER_FILE, hub.AGENT_FILE), (auth.OWNER_FILE, auth.AGENT_FILE))
        self.assertEqual(hub.STORE_DIR, settings.STORE_DIR)
        self.assertEqual((hub.PORT, hub.TUNNEL_PORT), (settings.PORT, settings.TUNNEL_PORT))
        self.assertEqual(hub.secret_folder(os.path.join('C:\\', 'h', '.pm-kit', 'config.json')),
                         os.path.join('C:\\', 'h', '.pm-kit', auth.FOLDER_NAME))

    def test_the_claude_adapter_passes_mcp_input(self):
        a = action(event('mcp__claude-in-chrome__navigate', {'url': 'http://x/'}, 'C:\\w'))
        self.assertEqual(a.params, {'url': 'http://x/'})

    def test_the_codex_adapter_passes_mcp_input(self):
        from hosts.codex.adapter.host import ADAPTER
        a = ADAPTER.parse('PreToolUse', {'tool_name': 'mcp__browser__navigate', 'tool_input': {'url': 'http://x/'},
                                         'cwd': 'C:\\w', 'hook_event_name': 'PreToolUse'})
        a = a[0] if isinstance(a, tuple) else a
        self.assertEqual(a.params, {'url': 'http://x/'})

    def test_params_do_not_change_equality(self):
        from kitlib.host import Action
        self.assertEqual(Action('mcp', tool='t', params={'a': 1}), Action('mcp', tool='t'))


if __name__ == '__main__':
    unittest.main()
