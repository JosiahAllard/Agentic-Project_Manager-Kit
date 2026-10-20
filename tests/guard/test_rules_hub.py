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
        # a shell command reading the store passes; a write into it is
        # still refused (the write's target, as an edit's)
        st = W.fwd(self.store)
        self.allowed(lambda sub: bash('ls "%s"' % st, self.w.wt, sub))
        self.allowed(lambda sub: bash('cat "%s/questions/Q-1.json"' % st, self.w.wt, sub))
        self.allowed(lambda sub: bash('cd "%s" && ls hub' % W.fwd(self.w.data), self.w.wt, sub))
        self.denied(lambda sub: ps('Remove-Item "%s\\todos\\T-1.json"' % self.store, self.w.wt, sub), 'hub-secrets')
        self.denied(lambda sub: bash('echo x > "%s/questions/Q-1.json"' % st, self.w.wt, sub), 'hub-secrets')

    def test_an_explicit_store(self):
        store = os.path.join(self.w.temp, 'hubstore')
        self.w.user_config({'pm_data': {'path': self.w.data}, 'hub': {'store': store}})
        self.allowed(lambda sub: bash('cat "%s/todos/T-1.json"' % W.fwd(store), self.w.wt, sub))
        self.denied(lambda sub: bash('rm "%s/todos/T-1.json"' % W.fwd(store), self.w.wt, sub), 'hub-secrets')

    def test_python_code_that_reads_a_token(self):
        code = "import os; print(open(os.path.expanduser('~/.pm-kit/hub/owner.token')).read())"
        self.denied(lambda sub: bash('python -c "%s"' % code, self.w.wt, sub), 'hub-secrets')
        self.denied(lambda sub: bash("python - <<'EOF'\n%s\nEOF" % code, self.w.wt, sub), 'hub-secrets')
        self.denied(lambda sub: bash("echo \"%s\" | python -" % code, self.w.wt, sub), 'hub-secrets')
        self.denied(lambda sub: bash("node -e \"require('fs').readFileSync(process.env.H + '/owner.token')\"",
                                     self.w.wt, sub), 'hub-secrets')

    def test_a_hub_verb_piped_into_a_parser_passes(self):
        # observed refusal (2026-10-18, a coordinator in PowerShell): a read verb of the hub tool
        # piped into python -c that parses its JSON. Naming the hub tool is not read.
        out = W.fwd(os.path.join(self.w.temp, 'out.txt'))
        parse = ("import json,sys; d=json.load(sys.stdin); [print(i['id'], i['title'][:80], '->', "
                 "i['answer']['label'], '|', i['answer'].get('note','')) for i in d['items']]")
        line = ('Get-Content %s; python "%s" questions --status answered --repo Game --json | python -c "%s"'
                % (out, self.hub, parse))
        self.allowed(lambda sub: ps(line, self.w.wt, sub))
        self.allowed(lambda sub: ps(line, self.w.main, sub), (('coordinator', False, COORDINATOR),))
        self.allowed(lambda sub: bash('python "%s" questions --json | python -c "import json,sys; '
                                      'print(len(json.load(sys.stdin)))"' % self.hub, self.w.wt, sub))
        self.allowed(lambda sub: bash('python -m tools.hub todos --json | python -c "import json,sys; '
                                      'print(len(json.load(sys.stdin)))"', self.w.wt, sub))
        self.allowed(lambda sub: bash('python tools/hub.py questions list | head', self.w.wt, sub))

    def test_the_alternative_runs_the_hub_tool_of_the_kit_the_hook_runs_from(self):
        # the kit whose kitlib the guard loaded (ctx.kit, kitlib.kitroot.root), not a copy elsewhere
        d = decide(bash('cat "%s/owner.token"' % W.fwd(self.secrets), self.w.wt, True), self.ctx(OWNER))
        self.assertEqual(d.rule, 'hub-secrets', d.reason)
        self.assertIn(' Instead: python "%s" questions' % self.hub, d.reason)

    def test_a_parser_of_a_hub_verb_still_may_not_name_the_secrets(self):
        # the parser's own code is read as before, and an owner verb upstream is still one
        sec = W.fwd(self.secrets)
        head = 'python "%s" questions --json | ' % self.hub
        for make, rule in (
                (lambda sub: bash(head + 'python -c "print(open(\'%s/owner.token\').read())"' % sec,
                                  self.w.wt, sub), 'hub-secrets'),
                (lambda sub: bash('python "%s" answer Q-1 --label yes | python -c "import sys; print(1)"'
                                  % self.hub, self.w.wt, sub), 'hub-owner-verb')):
            with self.subTest(make(True)['tool_input']['command']):
                self.denied(make, rule)

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
        # the hub rules run on what the parser did read, before the fallback's name pattern
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

    def test_an_unclean_line_naming_the_hub_tool_is_refused(self):
        # docs/guard.md "Fallback": any hub tool call on a line the parser cannot read, read
        # verbs included, is refused as hub-owner-verb; the owner's own session is not held
        hub = self.hub
        rows = (
            ('read verb after eval', lambda sub: bash('eval "$c"; python "%s" questions' % hub, self.w.wt, sub)),
            ('module after eval', lambda sub: bash('eval "$c"; python -m tools.hub todos --json', self.w.wt, sub)),
            ('-m hub', lambda sub: bash('eval "$c"; python -m hub questions', self.w.wt, sub)),
            # the forms the parsed rule reads: -m ending a flag cluster, and a quoted module
            ('-Im hub', lambda sub: bash('eval "$c"; python -Im hub questions', self.w.wt, sub)),
            ("-m 'hub'", lambda sub: bash("eval \"$c\"; python -m 'hub' questions", self.w.wt, sub)),
            ('-m "hub"', lambda sub: ps('iex $c; python -m "hub" questions', self.w.wt, sub)),
            ('-BIm"hub"', lambda sub: bash('eval "$c"; python -BIm"hub" questions', self.w.wt, sub)),
            ('unclosed quote', lambda sub: bash('python "%s" questions add --title "x' % hub, self.w.wt, sub)),
            ('PowerShell iex', lambda sub: ps('iex $c; python "%s" questions' % hub, self.w.wt, sub)),
            ('owner verb unread', lambda sub: bash('echo \'x ; python "%s" answer Q-1' % hub, self.w.wt, sub)),
        )
        for name, make in rows:
            with self.subTest(name):
                self.denied(make, 'hub-owner-verb')
                self.owner_allowed(make)
                d = decide(make(True), self.ctx(OWNER))
                self.assertIn('could not read this command', d.reason)
                self.assertIn('names the hub tool', d.reason)

    def test_an_unclean_line_naming_another_hub_file_is_not_held(self):
        # test_rules_hub.py, a hub.pyw or a word holding hub are not the hub tool
        for cmd in ('eval "$c"; python -m unittest tests/guard/test_rules_hub.py',
                    'eval "$c"; cat docs/hub.pyw', 'eval "$c"; ls kitlib/hub',
                    'eval "$c"; python -Im hubble x', "eval \"$c\"; python -m 'hub.x' y"):
            with self.subTest(cmd):
                for label, sub, environ in HELD:
                    d = decide(bash(cmd, self.w.wt, sub), self.ctx(environ))
                    self.assertTrue(d is None or d.rule != 'hub-owner-verb', d and d.reason)

    def test_hub_tool_with_the_store_passes(self):
        st = W.fwd(self.store)
        self.allowed(lambda sub: bash('python "%s" validate --store "%s"' % (self.hub, st), self.w.wt, sub))
        self.allowed(lambda sub: bash('python "%s" validate --store="%s"' % (self.hub, st), self.w.wt, sub))


class HomeAndWildcards(Hub):
    """The home folder spelled as `~` or a home variable ($HOME, ${HOME}, $env:USERPROFILE,
    $env:HOME, %USERPROFILE%, $env:HOMEDRIVE$env:HOMEPATH, %HOMEDRIVE%%HOMEPATH%), in a word or
    in the directory a cd (or a bare cd) moves to, read as the guard's
    home (the World's, never the real one); and wildcard paths that can name the folders."""

    def rows_denied(self, rows):
        for shell, line in rows:
            with self.subTest(line=line):
                self.denied(lambda sub, shell=shell, line=line: shell(line, self.w.wt, sub), 'hub-secrets')

    def rows_allowed(self, rows):
        for shell, line in rows:
            with self.subTest(line=line):
                self.allowed(lambda sub, shell=shell, line=line: shell(line, self.w.wt, sub))

    # a wildcard path is not read, so each wildcard row below passes and
    # the same place spelled literally is still refused. A literal relative path after a cd into
    # the home's .pm-kit folder (`hub\\x`) names neither the folder nor a token file by itself:
    # those rows show the cd is followed.

    def test_home_variables(self):
        self.rows_denied((
            (bash, 'cat $HOME/.pm-kit/hub/x'),
            (bash, 'cat ${HOME}/.pm-kit/hub/x'),
            (bash, 'cat "$HOME"/.pm-kit/hub/owner.token'),
            (bash, 'cat $USERPROFILE/.pm-kit/hub/agent.token'),
            (ps, 'gc $HOME\\.pm-kit\\hub\\x'),
            (ps, 'gc "$env:USERPROFILE\\.pm-kit\\hub\\owner.token"'),
            (ps, 'gc $env:HOME\\.pm-kit\\hub\\x'),
            (ps, 'gc ${env:USERPROFILE}\\.pm-kit\\hub\\x'),
            (ps, 'Get-Content -Path:$HOME\\.pm-kit\\hub\\x'),
            (ps, 'cmd /c "type %USERPROFILE%\\.pm-kit\\hub\\x"'),
        ))
        self.rows_allowed((
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

    def test_the_home_drive_and_path_pair(self):
        self.rows_denied((
            (ps, 'cd $env:HOMEDRIVE$env:HOMEPATH\\.pm-kit; gc hub\\x'),
            (ps, 'cd "${env:HOMEDRIVE}${env:HOMEPATH}\\.pm-kit"; gc hub\\x'),
            (bash, 'cd $HOMEDRIVE$HOMEPATH/.pm-kit && cat hub/x'),
        ))
        self.rows_allowed((
            (ps, 'gc $env:HOMEDRIVE$env:HOMEPATH\\.pm-kit\\h*\\o*'),
            (ps, 'cd $env:HOMEDRIVE$env:HOMEPATH; gc .pm-kit\\h*\\o*'),
            (ps, 'cmd /c "type %HOMEDRIVE%%HOMEPATH%\\.pm-kit\\h*\\o*"'),
            (ps, 'cd $env:HOMEDRIVE\\.pm-kit; gc hub\\x'),
            (ps, 'cd $env:HOMEPATH$x\\.pm-kit; gc hub\\x'),
        ))

    def test_a_cd_with_no_place_goes_home(self):
        # bash's cd, and PowerShell 7's, go home with no place; cd's own options and -- change nothing
        self.rows_denied((
            (bash, 'cd -L -- $HOME/.pm-kit && cat hub/x'),
        ))
        self.rows_allowed((
            (bash, 'cd && cat .gitconfig'),
            (bash, 'cd && cd "%s" && cat hub/x' % W.fwd(self.w.wt)),
            (bash, 'cd -; cat .pm-kit/h*/o*'),
            (bash, 'cd && cat .pm-kit/h*/o*'),
            (ps, 'Set-Location; gc .pm-kit\\h*\\o*'),
        ))

    def test_a_cd_behind_builtin_or_command(self):
        # bash's `builtin cd` and `command cd` are the cd itself
        self.rows_denied((
            (bash, 'command cd ~/.pm-kit && cat hub/x'),
            (bash, 'builtin cd ~/.pm-kit && cat hub/x'),
        ))
        self.rows_allowed((
            (bash, 'command -v cd && cat hub/x'),
            (bash, 'builtin cd ~/.pm-kit && cd "%s" && cat hub/x' % W.fwd(self.w.wt)),
            (ps, 'cd $null; gc README.md'),
            (ps, 'cd $null; gc .pm-kit\\h*\\o*'),
        ))

    def test_review_rows_for_the_home_folder(self):
        # the home-folder strings from the branch's two re-reviews, spelled literally
        self.rows_denied((
            (bash, 'pushd ~/.pm-kit && cat hub/x'),
            (bash, '(cd ~/.pm-kit && cat hub/x)'),
        ))
        self.rows_allowed((
            (bash, "cd '' && cat hub/x"),
            (bash, 'pushd && cat .pm-kit/h*/o*'),
            (ps, 'chdir; gc .pm-kit\\h*\\o*'),
            (ps, 'gc ${ENV:HomeDrive}$Env:HOMEPATH\\.pm-kit\\h*\\o*'),
        ))
        # a home folder the line computes is not read (docs/guard.md "Hub secrets", Limits):
        # these pass, and a change here means updating that list
        self.rows_allowed((
            (bash, 'H=$HOME; cd $H/.pm-kit && cat hub/x'),
            (bash, 'cd $(echo ~)/.pm-kit && cat hub/x'),
            (bash, 'cd ~/.pm-kit && cd - && cd - && cat hub/x'),
            (bash, 'pushd ~/.pm-kit && pushd /tmp && popd && cat hub/x'),
        ))

    def test_a_cd_into_the_home_folder_is_followed(self):
        self.rows_denied((
            (bash, 'cd ~/.pm-kit && cat hub/x'),
            (bash, 'cd $HOME/.pm-kit && cat hub/x'),
            (bash, 'cd ~/.pm-kit && ls hub'),
            (bash, 'cd "$HOME/.pm-kit/hub" && cat *'),
            (ps, 'Set-Location ~/.pm-kit; gc hub/x'),
            (ps, 'sl ~\\.pm-kit; gc hub\\x'),
            (ps, 'Push-Location ~\\.pm-kit; gc hub\\x'),
            (ps, 'Set-Location $env:USERPROFILE\\.pm-kit; gc hub\\x'),
            (ps, 'cmd /c "cd /d %USERPROFILE%\\.pm-kit && type hub\\x"'),
        ))
        self.rows_allowed((
            (bash, 'cd ~/.pm-kit && cat h*/o*'),
            (ps, 'cd $HOME; gc .pm-kit\\h*\\*'),
        ))

    def test_a_hub_call_with_a_home_variable(self):
        self.rows_denied((
            (bash, 'python "%s" questions add --title t --context-file "$HOME/.pm-kit/hub/x"' % self.hub),
            (bash, 'python "%s" questions add --title t --context-file=$HOME/.pm-kit/hub/x' % self.hub),
            (ps, 'python "%s" questions add --title t --context-file $HOME\\.pm-kit\\hub\\x' % self.hub),
        ))

    def test_the_rest_of_the_home_folder_passes(self):
        self.rows_allowed((
            (bash, 'cat $HOME/.gitconfig'),
            (bash, 'ls $HOME/.pm-kit'),
            (bash, 'cat $HOME/.pm-kit/*.json'),
            (bash, 'cat $HOME/$x/h*'),
            (bash, 'cd ~ && ls'),
            (bash, 'cd $HOME && cat .gitconfig'),
            (bash, 'cd ~ && cat .gitconfig'),
            (bash, 'cd ~ && cd /tmp && cat .pm-kit/h*/o*'),
            (bash, 'cd $HOME && cd $x && cat .pm-kit/h*/o*'),
            (ps, 'gc $env:USERPROFILE\\.pm-kit\\hub.json'),
            (ps, 'Set-Location $env:USERPROFILE; gc .gitconfig'),
        ))

    def test_absolute_wildcards_are_not_read(self):
        # a wildcard that can name the folders passes; the literal folder
        # is still refused
        home, data = W.fwd(self.w.home), W.fwd(self.w.data)
        self.rows_allowed((
            (bash, 'cat %s/.pm-kit/h?b/*' % home),
            (bash, 'ls %s/.pm-kit/*' % home),
            (bash, 'ls %s/**' % home),
            (bash, 'cat %s/*/questions/*.json' % data),
            (bash, 'ls %s/h*' % data),
            (ps, 'gc %s\\.pm-kit\\[h]ub\\o*' % self.w.home),
            (bash, 'ls %s/.pm-kit/*.json' % home),
            (bash, 'cat %s/*.json' % data),
        ))
        self.rows_denied((
            (bash, 'ls %s/.pm-kit/hub' % home),
            (ps, 'gc %s\\.pm-kit\\hub\\x' % self.w.home),
        ))

    def test_option_values_are_read_as_paths(self):
        # `--opt=value` and PowerShell's `-Opt:value`: the value is the path (hub_path_texts)
        sec = W.fwd(self.secrets)
        self.rows_denied((
            (ps, 'Get-Content -LiteralPath:%s\\x' % self.secrets),
            (bash, 'some-tool --input=%s/x' % sec),
            (bash, 'python "%s" questions add --title t --context-file=%s/x' % (self.hub, sec)),
            # the token folder spelled literally, whatever follows it
            (ps, 'Get-Content -LiteralPath:%s\\o*' % self.secrets),
        ))
        self.rows_allowed((
            (bash, 'some-tool --input=%s/.kit.json' % W.fwd(self.w.data)),
            (ps, 'Get-Content -Path:README.md'),
            (ps, 'Get-Content -Path:%s\\questions\\Q-1.json' % self.store),
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
        self.denied(lambda sub: bash('echo x > "%s/todos/T-1.json"' % W.fwd(store), self.w.wt, sub), 'hub-secrets')


class BareWordsInTheDataFolder(Hub):
    """Run from the PM data folder, where a bare `hub` is the default store's own name. The shell
    rule does not read the store, so a word naming it passes, whatever
    the program does with it; a write whose target is in the store is still refused."""

    def rows(self, rows, deny):
        for shell, line in rows:
            with self.subTest(line=line):
                make = lambda sub, shell=shell, line=line: shell(line, self.w.data, sub)
                if deny:
                    self.denied(make, 'hub-secrets')
                else:
                    self.allowed(make)

    def test_a_search_pattern_is_not_a_path(self):
        self.rows((
            (bash, 'grep -n -i "hub" launcher/coordinators.json'),
            (bash, 'grep -n -i hub launcher/coordinators.json'),
            (bash, 'grep -rn --include=*.json hub launcher'),
            (bash, 'egrep -c hub launcher/coordinators.json'),
            (bash, 'rg -n -i hub launcher'),
            (bash, 'findstr /i /n hub launcher/coordinators.json'),
            (ps, 'Select-String -Pattern hub -Path launcher\\coordinators.json'),
            (ps, 'Select-String -Path launcher\\coordinators.json -Pattern:hub'),
            (ps, 'sls -SimpleMatch hub launcher\\coordinators.json'),
        ), deny=False)

    def test_a_python_scripts_option_value_is_not_a_path(self):
        launch = W.fwd(os.path.join(self.w.kit_dir, 'tools', 'launch.py'))
        self.rows((
            (bash, 'python %s handoff --session hub --recycle --what-if' % launch),
            (bash, 'python %s handoff --session=hub --what-if' % launch),
            (ps, 'python %s handoff --session hub --recycle --what-if' % launch),
        ), deny=False)

    def test_a_write_into_the_store_is_still_refused(self):
        self.rows((
            (bash, 'echo x > hub/todos/T-1.json'),
            (bash, 'rm -rf hub'),
            (ps, 'Remove-Item -Recurse -Path hub'),
        ), deny=True)

    def test_what_a_program_opens_in_the_store_passes(self):
        st = W.fwd(self.store)
        self.rows((
            (bash, 'ls hub'),
            (bash, 'cat hub/todos/T-1.json'),
            (bash, 'cat ./hub/questions/Q-1.json'),
            (ps, 'Get-Content .\\hub\\questions\\Q-1.json'),
            (ps, 'Get-ChildItem hub'),
            # a search's files, and a pattern the parse cannot place
            (bash, 'grep -rn owner hub'),
            (bash, 'grep -n x hub/todos/T-1.json'),
            (bash, 'grep -n hub "%s/todos/T-1.json"' % st),
            (bash, 'grep -e x hub'),
            (bash, 'grep -f hub launcher/coordinators.json'),
            (bash, 'grep --file=x hub'),
            (bash, 'grep -A 3 x hub'),
            (bash, 'grep h* launcher/coordinators.json'),
            (bash, 'grep $p hub'),
            (bash, 'rg --files hub'),
            (bash, 'findstr /c:x hub'),
            (ps, 'Select-String -Pattern x -Path hub\\todos\\T-1.json'),
            (ps, 'Select-String -Pattern x hub'),
            (ps, 'sls -Context 2 x hub'),
            (ps, 'sls -SimpleMatch x hub'),
            (bash, 'grep --file hub launcher/coordinators.json'),
            # a python script's positional argument, a path-shaped value, a module's option
            (bash, 'python tools/x.py hub'),
            (bash, 'python tools/x.py read hub'),
            (bash, 'python tools/x.py -o hub'),
            (bash, 'python tools/x.py --store hub/todos'),
            (bash, 'python tools/x.py --store ./hub'),
            (bash, 'python tools/x.py --store=hub/todos'),
            (bash, 'python -m http.server --directory hub'),
        ), deny=False)

    def test_a_search_after_the_end_of_options(self):
        self.rows((
            (bash, 'grep -n -- hub launcher/coordinators.json'),
            (bash, 'rg -i -- hub launcher'),
            (bash, 'grep -n -- x hub'),
            (bash, 'grep -n -- x hub/todos/T-1.json'),
        ), deny=False)

    def test_findstrs_slash_options_are_skipped(self):
        self.rows((
            (ps, 'findstr /n /a:4 hub launcher\\coordinators.json'),
            (bash, 'findstr /i /s hub launcher/coordinators.json'),
            (ps, 'findstr /a:4 x hub\\todos\\T-1.json'),
            (bash, 'findstr /i x hub/todos/T-1.json'),
        ), deny=False)

    def test_a_scripts_options_naming_the_store_pass(self):
        # a script's options that name a folder it opens pass too: the store is not read
        self.rows((
            (bash, 'python tools/cleanup.py --force hub'),
            (bash, 'python tools/x.py --dir hub'),
            (bash, 'python tools/x.py --dir=hub'),
            (ps, 'python tools/x.py --what-if hub'),
            # a session value shaped as a path is read as one
            (bash, 'python tools/x.py --session ./hub'),
            (bash, 'python tools/x.py --session hub/todos'),
            (bash, 'python -Bm http.server --session hub'),
            (bash, 'python -Im http.server --session hub'),
            (bash, 'python -um tools.x --session hub'),
        ), deny=False)

    def test_code_given_on_the_line_is_refused_as_a_write_not_as_the_store(self):
        # python -c writes the directory it runs in (docs/guard.md "Shell writes"), and the data
        # folder is a main checkout: a subagent is refused that write, never by the store's rule,
        # and the sessions that may edit a main checkout pass
        make = lambda sub: bash('python -Ic pass --session hub', self.w.data, sub)
        self.denied(make, 'unplaced', HELD[:1])
        self.allowed(make, HELD[1:])

    def test_the_token_folder_is_still_refused(self):
        # from the home's .pm-kit folder a bare `hub` is the token folder
        pmkit = os.path.join(self.w.home, '.pm-kit')
        os.makedirs(pmkit, exist_ok=True)
        launch = W.fwd(os.path.join(self.w.kit_dir, 'tools', 'launch.py'))
        for shell, line, cwd in (
                (bash, 'python %s handoff --session hub --what-if' % launch, pmkit),
                (bash, 'python %s handoff --session=hub' % launch, pmkit),
                (bash, 'cd ~/.pm-kit && python %s handoff --session hub' % launch, self.w.data),
                (bash, 'grep -n hub launcher.json', pmkit),
                (ps, 'Select-String -Pattern hub -Path x.json', pmkit)):
            with self.subTest(line=line, cwd=cwd):
                self.denied(lambda sub, shell=shell, line=line, cwd=cwd: shell(line, cwd, sub), 'hub-secrets')


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
        'approvals show A-1',
    )

    def test_owner_verbs_are_denied(self):
        for args in self.DENIED:
            with self.subTest(args):
                self.denied(self.run_hub(args), 'hub-owner-verb')
                self.owner_allowed(self.run_hub(args))

    def test_server_and_device_verbs_are_the_owners(self):
        # stop and restart (the hub server) and unpair (every paired browser) are owner verbs, in both
        # forms of the allowed shape; the owner's own session runs them
        for verb in ('stop', 'restart', 'unpair'):
            for line in ('python "%s" %s' % (self.hub, verb), 'python -m tools.hub %s' % verb):
                with self.subTest(line):
                    self.denied(lambda sub, line=line: bash(line, self.w.wt, sub), 'hub-owner-verb')
                    self.owner_allowed(lambda sub, line=line: bash(line, self.w.wt, sub))

    def test_hub_run_as_a_module(self):
        # `python -m tools.hub` from the kit folder, or `-m hub` from tools/, runs the same parser
        for line in ('python -m tools.hub answer Q-1 --choice 1', 'cd tools && python -m hub todos done T-1',
                     'python -mtools.hub open', 'python -B -m tools.hub to-pm send',
                     # -m at the end of a cluster of python's flags, or after options with values
                     'python -Bm tools.hub answer Q-1 --choice 1', 'python -Im tools.hub answer Q-1 --choice 1',
                     'python -um tools.hub answer Q-1', 'python -Em tools.hub answer Q-1',
                     'python -BImtools.hub answer Q-1', 'python -X utf8 -W ignore -m tools.hub answer Q-1',
                     'py -3 -m tools.hub answer Q-1', 'python -m "tools.hub" answer Q-1'):
            with self.subTest(line):
                self.denied(lambda sub, line=line: bash(line, self.w.wt, sub), 'hub-owner-verb')
        self.denied(lambda sub: ps('python -m tools.hub pair', self.w.wt, sub), 'hub-owner-verb')
        self.denied(lambda sub: ps('python -Bm tools.hub pair', self.w.wt, sub), 'hub-owner-verb')
        for line in ('python -m tools.hub questions', 'python -m tools.hub todos add --title t',
                     'python -m hubby answer Q-1', 'python -m tools.hubsite answer',
                     'python -m runpy tools.hubby answer Q-1', 'python -c "import hubby" answer Q-1',
                     'python -c "import kitlib.hubmcp" answer Q-1'):
            with self.subTest(line):
                self.allowed(lambda sub, line=line: bash(line, self.w.wt, sub))

    def test_hub_run_as_a_program(self):
        # the hub tool run as the program itself (the .py association, a shebang) is a hub run as
        # python's script is
        for line in ('./tools/hub.py answer Q-1 --choice 1', 'tools/hub.py open',
                     '"%s" todos done T-1' % self.hub.replace('\\', '/')):
            with self.subTest(line):
                self.denied(lambda sub, line=line: bash(line, self.w.wt, sub), 'hub-owner-verb')
        for line in ('.\\tools\\hub.py answer Q-1 --choice 1', '& "%s" pair' % self.hub):
            with self.subTest(line):
                self.denied(lambda sub, line=line: ps(line, self.w.wt, sub), 'hub-owner-verb')
        for line in ('./tools/hub.py questions', './tools/hubby.py answer Q-1'):
            with self.subTest(line):
                self.allowed(lambda sub, line=line: bash(line, self.w.wt, sub))
        self.allowed(lambda sub: ps('.\\tools\\hub.py questions', self.w.wt, sub))

    def test_hub_run_through_runpy(self):
        # `python -m runpy <module>` runs that module as -m does: runpy's argument is the script
        for line in ('python -m runpy tools.hub answer Q-1', 'python -m runpy hub todos done T-1',
                     'python -B -m runpy tools.hub to-pm send'):
            with self.subTest(line):
                self.denied(lambda sub, line=line: bash(line, self.w.wt, sub), 'hub-owner-verb')
        self.denied(lambda sub: ps('python -m runpy tools.hub pair', self.w.wt, sub), 'hub-owner-verb')
        for line in ('python -m runpy tools.hub questions', 'python -m runpy tools.hubby answer Q-1',
                     'python -m runpy tools.gate', 'python -m runpy $m answer Q-1'):
            with self.subTest(line):
                self.allowed(lambda sub, line=line: bash(line, self.w.wt, sub))

    def test_a_module_after_options_the_guard_cannot_read(self):
        # an option whose value the text does not show still leaves python's own -m: a parsed run
        for line in ('python -X $v -m tools.hub answer Q-1', 'python -W "$w" -m tools.hub answer Q-1'):
            with self.subTest(line):
                self.denied(lambda sub, line=line: bash(line, self.w.wt, sub), 'hub-owner-verb')
        for line in ('python -m cProfile -m unittest tests.guard', 'python -X $v -m unittest',
                     'python -m pdb -m tools.hubsite answer',
                     # known limit: what an unreadable word holds is not read; the text
                     # names no hub module
                     'python $X -m hubby answer Q-1'):
            with self.subTest(line):
                self.allowed(lambda sub, line=line: bash(line, self.w.wt, sub))

    def test_ordinary_python_passes(self):
        for line in ('python -m unittest', 'python -m unittest tests.guard.test_rules_hub',
                     'python tools/gate.py --full', 'python -c "print(1)"', 'py -3 -m pip install x',
                     'python -m tools.hub questions', 'python -I -c "import sys; print(sys.path)"',
                     'python -m json.tool x.json', 'python -m runpy tools.gate'):
            with self.subTest(line):
                self.allowed(lambda sub, line=line: bash(line, self.w.wt, sub))
        self.allowed(lambda sub: ps('py -3 -m pip list', self.w.wt, sub))

    def test_hub_code_reads_imports_of_hub_only(self):
        # the guard's own kitlib.guard.hub is not tools/hub.py; a from-import of another module
        # does not import hub, and python's arguments after the -c code are not code
        for line in ('python -c "from kitlib.guard import hub; print(hub.NAME_ONLY_PROGRAMS)"',
                     'python -c "from kitlib.guard import hub as h, os; print(h)"',
                     'python -c "from kitlib.guard import (hub)"',
                     'python -c "import kitlib.guard.hub"'):
            with self.subTest(line):
                self.allowed(lambda sub, line=line: bash(line, self.w.wt, sub))

    def test_review_rows_for_owner_verbs(self):
        # every owner-verb string from the branch's two re-reviews, with its verdict
        for line in ('python -OOm tools.hub answer Q-1', 'python -W x -m tools.hub answer Q-1',
                     'python -Wignore -m tools.hub answer Q-1', 'python -X dev -m tools.hub answer Q-1',
                     'py -3.12-64 -m tools.hub answer Q-1', 'py -V:3.12 -m tools.hub answer Q-1',
                     'python3.12 -m tools.hub answer Q-1', 'pythonw -m tools.hub answer Q-1',
                     'python.exe -m tools.hub answer Q-1', 'env python -m tools.hub answer Q-1',
                     'bash -c "python -m tools.hub answer Q-1"', 'cmd /c python -m tools.hub answer Q-1'):
            with self.subTest(line):
                self.denied(lambda sub, line=line: bash(line, self.w.wt, sub), 'hub-owner-verb')
        self.denied(lambda sub: ps('powershell -Command "python -m tools.hub pair"', self.w.wt, sub),
                    'hub-owner-verb')
        self.denied(lambda sub: ps('Start-Process python -ArgumentList "-m tools.hub pair"', self.w.wt, sub),
                    'hub-owner-verb')
        # text that only names the hub tool is not read: a search pattern, a message
        for line in ('py -3.12 tools/x.py', 'uv run python -m unittest', 'python -m $m questions',
                     'python -m pip install hub', 'echo "see tools/hub.py"',
                     # known limit: the module name is in a variable the text does not show
                     'python -m $m answer Q-1'):
            with self.subTest(line):
                self.allowed(lambda sub, line=line: bash(line, self.w.wt, sub))

    def test_agent_verbs_pass(self):
        for args in self.ALLOWED:
            with self.subTest(args):
                self.allowed(self.run_hub(args))

    def test_quoted_variables_in_values_are_denied(self):
        # over-reach, fail-closed rule: the allowed shape has every word literal, so a quoted variable in an
        # agent verb's value is denied too (these passed before the fail-closed rule)
        self.denied(lambda sub: bash('t="Ask"; python "%s" todos add --title "$t"' % self.hub, self.w.wt, sub),
                    'hub-owner-verb')
        self.denied(lambda sub: bash('python "%s" todos add --title="$t"' % self.hub, self.w.wt, sub),
                    'hub-owner-verb')

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
        )
        for name, make in rows:
            with self.subTest(name):
                d = decide(make(True), self.ctx(OWNER))
                self.assertIsNotNone(d, 'allowed')
                self.assertIn(d.rule, ('hub-owner-verb', 'hub-secrets'), d.reason)

    def test_reading_hub_py_passes(self):
        # naming hub.py as a file to read is not a run of it
        for cmd in ('cat tools/hub.py', 'grep -n answer tools/hub.py', 'git diff tools/hub.py',
                    'git add tools/hub.py tests/hubsite/test_cli.py'):
            with self.subTest(cmd):
                self.allowed(lambda sub, cmd=cmd: bash(cmd, self.w.wt, sub))
                self.owner_allowed(lambda sub, cmd=cmd: bash(cmd, self.w.wt, sub))
        for cmd in ('cat tools/hub.py; echo x', 'cd tools && cat hub.py', 'cat tools/hub.py | head'):
            with self.subTest(cmd):
                self.allowed(lambda sub, cmd=cmd: bash(cmd, self.w.wt, sub))

    def test_text_naming_an_owner_verb_is_not_a_run(self):
        # a read of hub.py, or a quoted argument naming a run of it, passes with nothing said
        note = W.fwd(os.path.join(self.w.temp, 'notes.txt'))
        for make in (lambda sub: bash('grep -rn "getattr(.*push" tools/hub.py | head -3', self.w.wt, sub),
                     lambda sub: bash('grep -n "answer" tools/hub.py | head -3', self.w.wt, sub),
                     lambda sub: ps('Select-String -Path tools\\hub.py -Pattern "answer" | Select-Object -First 3',
                                    self.w.wt, sub),
                     lambda sub: bash("echo 'then python tools/hub.py questions answer Q-1' >> %s" % note,
                                      self.w.wt, sub)):
            self.allowed(make)

    def test_lines_seen_refused_that_only_name_hub_py_pass(self):
        """Each line here was refused live as hub-owner-verb; none runs the hub tool, so each passes
        whole in every held session (a subagent, the coordinator, the pm, a plain session)."""
        kit = W.fwd(self.w.main)                # a checkout path, as the live line named one
        for cmd in ('ls %s/tools/no_window.py %s/tools/hub.py %s/optional/procwatch/procwatch.py' % (kit, kit, kit),
                    'sed -n 16,39p tests/guard/test_rules_hub.py; '
                    'grep -n "def run_hub\\|self.hub =" -A3 tests/guard/test_rules_hub.py | head -12',
                    'grep -rn "import_module\\|def get_adapter" kitlib/host/ | head',
                    'grep -rn "getattr(.*push" tools/hub.py | head -3',
                    'echo "hub.py answer is the owner\'s" | grep -c hub.py'):
            with self.subTest(cmd):
                self.allowed(lambda sub, cmd=cmd: bash(cmd, self.w.wt, sub))
        self.allowed(lambda sub: ps('python "%s" todos --json | ConvertFrom-Json' % self.hub, self.w.wt, sub))
        self.allowed(self.run_hub('todos'))

    def test_a_heredoc_that_only_mentions_the_hub_is_not_a_run(self):
        """File output whose heredoc text names the hub and hub.py runs nothing: hub-owner-verb stays
        out of it (seen refused live as role=main). Other rules on the write target are not this test's."""
        note = W.fwd(os.path.join(self.w.temp, 'notes.md'))
        text = ("cat >> %s <<'EOF'\nThe owner answers in the hub; agents never run "
                "`python tools/hub.py answer Q-1` or open the hub page.\nEOF" % note)
        for label, sub, environ in HELD:
            with self.subTest(session=label):
                d = decide(bash(text, self.w.wt, sub), self.ctx(environ))
                self.assertTrue(d is None or d.rule != 'hub-owner-verb', d and d.reason)

    def test_a_list_read_passes_whatever_follows_the_pipe(self):
        # todos and questions are reads in every session; an owner verb piped the same way is still denied
        for shell, tail in ((ps, ' | ConvertFrom-Json'), (ps, ' | ConvertFrom-Json | Select-Object -First 3'),
                            (bash, ' | head -5')):
            for args in ('todos --json', 'questions --json', 'todos --repo demo --json'):
                with self.subTest(line=args + tail):
                    self.allowed(lambda sub, s=shell, a=args, t=tail: s('python "%s" %s%s' % (self.hub, a, t),
                                                                        self.w.wt, sub))
            for args in ('answer Q-1 --choice 1', 'to-pm send', 'approvals approve A-1'):
                with self.subTest(line=args + tail):
                    self.denied(lambda sub, s=shell, a=args, t=tail: s('python "%s" %s%s' % (self.hub, a, t),
                                                                       self.w.wt, sub), 'hub-owner-verb')

    def test_the_alternative_names_the_kit_hub_tool(self):
        d = decide(self.run_hub('answer Q-1 --choice 1')(True), self.ctx(OWNER))
        self.assertEqual(d.instead, 'python "%s" questions add --title "<what the owner should decide>"' % self.hub)


class ParsedRuns(Hub):
    """hub-owner-verb on the runs the parser reads, however the line spells them. Text that only
    names the hub tool, and a name built at run time, are not read: the rule catches mistakes,
    not an adversary (docs/guard.md "Hub secrets", Limits)."""

    def rows(self, rows, deny):
        for shell, line in rows:
            # a keyword, not the message: a nested session subTest keeps the line in its report
            with self.subTest(line=line):
                make = lambda sub, shell=shell, line=line: shell(line, self.w.wt, sub)
                if deny:
                    self.denied(make, deny)
                    self.owner_allowed(make)
                else:
                    self.allowed(make)

    def test_names_the_shell_puts_together(self):
        # the parse takes the quotes and escapes out: a parsed run
        self.rows((
            (bash, 'python -m tools.h"u"b answer Q-1'), (bash, "python tools/h'u'b.py answer Q-1"),
            (bash, "cd tools && python -m 'hub' answer Q-1"), (ps, 'python -m tools.h`ub answer Q-1'),
        ), 'hub-owner-verb')

    def test_names_built_at_run_time_pass(self):
        # known limit: a name built at run time (string concatenation, an encoding, a
        # variable's value, a script the agent wrote) is not read
        self.rows((
            (bash, 'python -c "import importlib; importlib.import_module(\'tools.h\' + \'ub\').main()"'),
            (bash, 'python -c "__import__(\'tools\', fromlist=[\'h\' + \'ub\'])"'),
            (bash, 'python -c "import base64; exec(base64.b64decode(\'aW1wb3J0IHRvb2xzLmh1Yg==\'))"'),
            (bash, 'python -m $m answer Q-1'), (bash, 'python $X -m hubby answer Q-1'),
            (bash, 'python scratch/run.py answer Q-1'),
        ), False)

    def test_an_owner_verb_after_an_agent_verb(self):
        self.rows(((bash, 'python "%s" questions; python -m tools.hub answer Q-1' % self.hub),), 'hub-owner-verb')

    def test_pythons_own_m_after_a_script_word(self):
        # python's own -m the parser reads: a parsed run of an owner verb; commit.py's -m message
        # after its script word is not python's
        self.rows((
            (bash, 'git commit -m "Hub store: import the todos"'),
            (bash, 'python Z:/k/tools/commit.py -C $PWD -m "Hub store: x" --model M kitlib/hubsite/store.py'),
        ), False)
        self.rows((
            (bash, 'python Z:/k/tools/commit.py -m "$(python -m hub answer Q-1)" x.py'),
            (bash, 'cd tools && python -W ignore -m hub answer Q-1'), (bash, 'cd tools && python -Bm hub answer Q-1'),
            (ps, 'cd tools; python -X dev -m hub answer Q-1'),
            # the shell joins what the guard's split parts: an escaped space, a quote, a backtick, a
            # variable or an expansion before the script word may hand it to -X or -W as a value,
            # leaving python's own -m (rules.hub_py_script_first)
            (bash, 'cd tools && python -Xa\\ commit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X a\\ commit.py -m hub answer Q-1'),
            (ps, 'cd tools; python -X a` commit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -W a\\ commit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X a\\\tcommit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X "a "commit.py -m hub answer Q-1'),
            (bash, "cd tools && python -X a' 'commit.py -m hub answer Q-1"),
            (ps, "cd tools; python -X 'a commit.py' -m hub answer Q-1"),
            # bash splits words on space and tab only: a no-break space, a control or an em space
            # between -X's value and commit.py is part of that value, so the -m is python's
            (bash, 'cd tools && python -X a commit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X a\x1fcommit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X a\vcommit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X a\fcommit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X a commit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -Xa commit.py -m hub answer Q-1'),
            # and inside a nested shell, read by its words as well as the line's text
            (bash, "bash -c 'cd tools && python -X a commit.py -m hub answer Q-1'"),
            (bash, 'bash -c "cd tools && python -X \'a commit.py\' -m hub answer Q-1"'),
            (ps, 'cmd /c "cd tools && python -X a commit.py -m hub answer Q-1"'),
        ), 'hub-owner-verb')

    def test_an_owner_verb_beside_a_read_or_a_message(self):
        self.rows(((bash, 'git diff tools/hub.py; python -m tools.hub answer Q-1'),), 'hub-owner-verb')
        self.rows((
            (bash, 'python Z:/k/tools/commit.py -m "fix tools.hub" tools/x.py; python -m tools.hub answer Q-1'),
        ), 'hub-owner-verb')

    def test_a_python_with_a_path_runs_hub_as_a_module(self):
        self.rows((
            (bash, 'D:/Programs/Python312/python.exe -m hub answer Q-1'),
            (ps, '& D:\\Programs\\Python312\\python.exe -m hub answer Q-1'),
            (bash, 'cd tools && C:/Python312/python.exe -X a\\ commit.py -m hub answer Q-1'),
        ), 'hub-owner-verb')

    def test_a_separator_inside_one_of_pythons_words(self):
        # a ; | & or newline the shell keeps inside one of python's words (quoted, escaped, or
        # inside a nested shell's string): the parser reads python's words whole, a parsed run
        self.rows((
            (bash, "cd tools && python -X ';' -m hub answer Q-1"),
            (bash, 'cd tools && python -X a\\;b -m hub answer Q-1'),
            (bash, "cd tools && python -X 'a|b' -m hub answer Q-1"),
            (ps, "cd tools && python -X 'a|b' -m hub answer Q-1"),
            (ps, "cd tools; python -X 'a|b' -m hub answer Q-1"),
            (ps, 'cmd /c "cd tools && python -X a;b -m hub answer Q-1"'),
            (bash, 'cmd //c "cd tools && python -X a;b -m hub answer Q-1"'),
            (bash, "cd tools && python -X 'a&b' -m hub answer Q-1"),
            (bash, 'cd tools && python -X "a;b" -m hub answer Q-1'),
            (bash, "cd tools && python -X $'a\\nb' -m hub answer Q-1"),
            (bash, "cd tools && python -X 'a\nb' -m hub answer Q-1"),
            (bash, "cd tools && python -W 'a;b' -m hub answer Q-1"),
            (ps, "cd tools; python -X 'a;b' -m hub answer Q-1"),
            (ps, 'cd tools; python -X "a;b" -m hub answer Q-1'),
            (ps, 'cd tools; python -X a`;b -m hub answer Q-1'),
            # every spelling of python's -m after such a word
            (bash, "cd tools && python -X ';' -Bm hub answer Q-1"),
            (bash, "cd tools && python -X ';' -mhub answer Q-1"),
            (bash, "cd tools && python -X ';' -m 'hub' answer Q-1"),
            # inside a nested shell's string
            (bash, 'bash -c "cd tools && python -X \';\' -m hub answer Q-1"'),
            (ps, 'powershell -Command "cd tools; python -X \'a;b\' -m hub answer Q-1"'),
            # a line the parser cannot read whole (an unclosed quote)
            (bash, "cd tools && python -X ';' -m hub answer Q-1 \""),
            (ps, "cd tools; python -X 'a;b' -m hub answer Q-1 \""),
            # a python in a quoted folder with a space in it
            (ps, 'cd tools; & "C:\\Program Files\\Python312\\python.exe" -X \'a;b\' -m hub answer Q-1'),
            (bash, 'cd tools && "C:/Program Files/Python312/python.exe" -X \'a;b\' -m hub answer Q-1'),
        ), 'hub-owner-verb')
        # cmd ends a command at an unquoted |, so b, not python, gets -m hub: the guard reads
        # cmd's string as cmd does and passes it; a | cmd quotes or escapes is python's word
        self.rows(((ps, 'cmd /c "cd tools && python -X a|b -m hub answer Q-1"'),), False)
        self.rows((
            (bash, 'cmd //c \'cd tools && python -X "a|b" -m hub answer Q-1\''),
            (bash, 'cmd //c \'cd tools && python -X a^|b -m hub answer Q-1\''),
            (ps, 'cmd /c "cd tools && python -X a^|b -m hub answer Q-1"'),
            (ps, 'cmd /c \'cd tools && python -X "a|b" -m hub answer Q-1\''),
            (ps, 'cmd /c "cd tools && python -X a^&b -m hub answer Q-1"'),
        ), 'hub-owner-verb')

    def test_a_python_in_a_quoted_folder_with_a_space(self):
        self.rows((
            (ps, 'cd tools; & "C:\\Program Files\\Python312\\python.exe" -m hub answer Q-1'),
            (bash, 'cd tools && "C:/Program Files/Python312/python.exe" -m hub answer Q-1'),
        ), 'hub-owner-verb')

    def test_an_owner_verb_beside_backlog_or_another_command(self):
        backlog = 'python Z:/k/tools/backlog.py -C Z:/pm --session kit '
        self.rows((
            (bash, backlog + 'set EX-RETIRE --scope tools/hub.py; python tools/hub.py answer Q-1'),
            (bash, 'python tools/hub.py answer Q-1 --scope tools/hub.py'),
            (bash, 'python -m tools.hub answer Q-1'),
            (bash, 'cd x && python tools/hub.py answer Q-1'), (bash, 'echo x & python tools/hub.py answer Q-1'),
        ), 'hub-owner-verb')


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


setUpModule = W.deny_softened           # unreadable held at deny: these pin the kept rules alone
tearDownModule = W.allow_unplaced


if __name__ == '__main__':
    unittest.main()
