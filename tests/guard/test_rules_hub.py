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
        code = "from kitlib.hubsite import auth; print(auth.read_token(auth.folder(), 'owner'))"
        self.denied(lambda sub: bash('python -c "%s"' % code, self.w.wt, sub), 'hub-secrets')
        self.denied(lambda sub: bash("python - <<'EOF'\n%s\nEOF" % code, self.w.wt, sub), 'hub-secrets')
        self.denied(lambda sub: bash("echo \"%s\" | python -" % code, self.w.wt, sub), 'hub-secrets')
        self.denied(lambda sub: bash("node -e \"require('fs').readFileSync(process.env.H + '/owner.token')\"",
                                     self.w.wt, sub), 'hub-secrets')

    def test_a_hub_verb_piped_into_a_parser_passes(self):
        # observed refusal (2026-10-18, a coordinator in PowerShell): a read verb of the hub tool,
        # run in the one shape the guard reads in full, piped into python -c that parses its JSON.
        # The parser's code names nothing of the hub; the command piped in is the tool itself.
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

    def test_what_the_skip_lets_through_by_design(self):
        # deliberate (from the review of the hub-pipe false positive): the skip is not a read check. Code
        # given with -c that names nothing of the hub passes even when it runs what is piped in as
        # code, since no text test tells exec(...) from json.load(...) in code the guard cannot
        # run; and the command piped in may be any verb the shape allows, an agent write too.
        self.allowed(lambda sub: bash('python "%s" questions --json | python -c "import sys; '
                                      'exec(sys.stdin.read())"' % self.hub, self.w.wt, sub))
        self.allowed(lambda sub: bash('python "%s" questions add --title x | python -c "import sys; '
                                      'print(sys.stdin.read())"' % self.hub, self.w.wt, sub))

    def test_the_alternative_runs_the_hub_tool_of_the_kit_the_hook_runs_from(self):
        # the kit whose kitlib the guard loaded (ctx.kit, kitlib.kitroot.root), not a copy elsewhere
        d = decide(bash('cat "%s/owner.token"' % W.fwd(self.secrets), self.w.wt, True), self.ctx(OWNER))
        self.assertEqual(d.rule, 'hub-secrets', d.reason)
        self.assertIn(' Instead: python "%s" questions' % self.hub, d.reason)

    def test_a_parser_of_a_hub_verb_still_may_not_name_the_secrets(self):
        # the parser's own code is read as before, and only the tool's own shape is let through
        # upstream: standard input run as code, any other program, an owner verb
        sec = W.fwd(self.secrets)
        head = 'python "%s" questions --json | ' % self.hub
        for make, rule in (
                (lambda sub: bash(head + 'python -c "print(open(\'%s/owner.token\').read())"' % sec,
                                  self.w.wt, sub), 'hub-secrets'),
                (lambda sub: bash(head + 'python -c "from kitlib.hubsite import auth; '
                                         'print(auth.read_token(auth.folder(), \'agent\'))"',
                                  self.w.wt, sub), 'hub-secrets'),
                (lambda sub: ps(head + 'python -c "import subprocess; subprocess.run([\'python\', '
                                       '\'tools/hub.py\', \'serve\'])"', self.w.wt, sub), 'hub-owner-verb'),
                (lambda sub: bash('echo tools/hub.py | python -c "import sys; print(sys.stdin.read())"',
                                  self.w.wt, sub), 'hub-owner-verb'),
                (lambda sub: bash('python "%s" questions --json $X | python -c "import sys; print(1)"' % self.hub,
                                  self.w.wt, sub), 'hub-owner-verb'),
                (lambda sub: bash('python "%s" answer Q-1 --label yes | python -c "import sys; print(1)"'
                                  % self.hub, self.w.wt, sub), 'hub-owner-verb')):
            with self.subTest(make(True)['tool_input']['command']):
                self.denied(make, rule)
        # the tool's output run as code names no token file: the hub tool by name is not read
        self.allowed(lambda sub: bash(head + 'python -', self.w.wt, sub))

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
            (bash, 'python -Ic pass --session hub'),
        ), deny=False)

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
                     'python --check-hash-based-pycs never -m tools.hub answer Q-1',
                     'py -3 -m tools.hub answer Q-1', 'python -m "tools.hub" answer Q-1',
                     # runpy running it, from the command line or from code
                     'python -m runpy tools.hub answer Q-1 --choice 1', 'python -Im runpy hub answer Q-1',
                     'cd tools && python -c "import hub; hub.main()" answer Q-1 --choice 1',
                     'python -c "from tools import hub; hub.main()" answer Q-1',
                     'python -c "from tools.hub import main; main()" answer Q-1',
                     'python -c "import os, tools.hub as h; h.main()" answer Q-1',
                     'python -c "import runpy,sys; sys.argv=[\'x\',\'answer\']; '
                     'runpy.run_module(\'tools.hub\', run_name=\'__main__\')"',
                     'python -c "import importlib; importlib.import_module(\'hub\')"'):
            with self.subTest(line):
                self.denied(lambda sub, line=line: bash(line, self.w.wt, sub), 'hub-owner-verb')
        self.denied(lambda sub: ps('python -m tools.hub pair', self.w.wt, sub), 'hub-owner-verb')
        self.denied(lambda sub: ps('python -Bm tools.hub pair', self.w.wt, sub), 'hub-owner-verb')
        # over-reach, fail-closed rule: an agent verb is allowed only in the one shape the guard reads in
        # full (`-m tools.hub` as two words after plain options), so these are denied now
        for line in ('python -Bm tools.hub questions', 'python -m runpy tools.hub questions',
                     'python -Bc "print(1)" -m tools.hub answer'):
            with self.subTest(line):
                self.denied(lambda sub, line=line: bash(line, self.w.wt, sub), 'hub-owner-verb')
        for line in ('python -m tools.hub questions', 'python -m tools.hub todos add --title t',
                     'python -m hubby answer Q-1', 'python -m tools.hubsite answer',
                     'python -m runpy tools.hubby answer Q-1', 'python -c "import hubby" answer Q-1',
                     'python -c "import kitlib.hubmcp" answer Q-1'):
            with self.subTest(line):
                self.allowed(lambda sub, line=line: bash(line, self.w.wt, sub))

    def test_a_module_after_options_the_guard_cannot_read(self):
        # a word the text does not show, or another module's -m (cProfile, pdb, runpy), is outside
        # the allowed shape: any literal -m (or --module) naming hub on the line counts
        # (rules.HUB_NAME_RES)
        for line in ('python $X -m tools.hub answer Q-1 --choice 1', 'python "$@" -m tools.hub answer Q-1',
                     'python -X $v -m tools.hub answer Q-1', 'python -W "$w" -m tools.hub answer Q-1',
                     'python $X -Bm tools.hub answer Q-1', 'python $X -mtools.hub answer Q-1',
                     'python $X -m runpy tools.hub answer Q-1',
                     'python -m cProfile -m tools.hub answer Q-1 --choice 1',
                     'python -m cProfile -o out.prof -m tools.hub answer Q-1',
                     'python -m cProfile -mtools.hub answer Q-1', 'python -m pdb -m tools.hub answer Q-1',
                     'python -m profile -m tools.hub todos done T-1',
                     'python -m cProfile -m runpy tools.hub answer Q-1',
                     'python -m trace --count --module tools.hub answer Q-1',
                     'python -m trace --count --module=tools.hub answer Q-1',
                     'cd tools && python -m cProfile -m hub answer Q-1',
                     # over-reach, fail-closed rule: hub named outside the allowed shape, whatever the verb
                     'python $X -m tools.hub questions', 'python -m cProfile -m tools.hub questions'):
            with self.subTest(line):
                self.denied(lambda sub, line=line: bash(line, self.w.wt, sub), 'hub-owner-verb')
        self.denied(lambda sub: ps('python $X -m tools.hub pair', self.w.wt, sub), 'hub-owner-verb')
        self.denied(lambda sub: ps('python -m cProfile -m tools.hub pair', self.w.wt, sub), 'hub-owner-verb')
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
        for line in ('python -c "from kitlib.guard import os; import hub; hub.main()"',
                     # over-reach, fail-closed rule: an import of hub anywhere in the text counts, even in
                     # python's arguments after the -c code
                     'python -c "print(1)" "import hub"', 'python -c "print(1)" import hub',
                     'python -Bc "print(1)" "from tools import hub"',
                     'python -c "from kitlib.guard import hub; import tools.hub"',
                     'python -c "from os import path; from tools import hub"',
                     'python -cimport\\ hub', 'python -Bc "import hub"'):
            with self.subTest(line):
                self.denied(lambda sub, line=line: bash(line, self.w.wt, sub), 'hub-owner-verb')
        self.denied(lambda sub: bash('python - <<EOF\nimport hub\nEOF', self.w.wt, sub), 'hub-owner-verb')

    def test_review_rows_for_owner_verbs(self):
        # every owner-verb string from the branch's two re-reviews, with its verdict
        for line in ('python -OOm tools.hub answer Q-1', 'python -W x -m tools.hub answer Q-1',
                     'python -Wignore -m tools.hub answer Q-1', 'python -X dev -m tools.hub answer Q-1',
                     'py -3.12-64 -m tools.hub answer Q-1', 'py -V:3.12 -m tools.hub answer Q-1',
                     'python3.12 -m tools.hub answer Q-1', 'pythonw -m tools.hub answer Q-1',
                     'python.exe -m tools.hub answer Q-1', 'env python -m tools.hub answer Q-1',
                     'python -m runpy hub answer Q-1',
                     'bash -c "python -m tools.hub answer Q-1"', 'cmd /c python -m tools.hub answer Q-1',
                     # a program the guard does not unwrap is read closed
                     'uv run python -m tools.hub answer Q-1', 'uv run -m tools.hub answer Q-1',
                     'poetry run python -m tools.hub todos done T-1',
                     'uv run python -c "import hub; hub.main()"',
                     # python options the guard cannot read to the end are read closed: a word
                     # naming hub, or after an unreadable word an owner verb, counts
                     'python -m cProfile -m -s time tools.hub answer Q-1',
                     # an import mention in a string or comment of -c code is over-reach
                     # (docs/guard.md "Hub secrets", Over-reach)
                     'python -c "print(\'import hub\')"', 'python -c "import x  # import hub"'):
            with self.subTest(line):
                self.denied(lambda sub, line=line: bash(line, self.w.wt, sub), 'hub-owner-verb')
        self.denied(lambda sub: ps('powershell -Command "python -m tools.hub pair"', self.w.wt, sub),
                    'hub-owner-verb')
        self.denied(lambda sub: ps('Start-Process python -ArgumentList "-m tools.hub pair"', self.w.wt, sub),
                    'hub-owner-verb')
        # over-reach, fail-closed rule: any text naming hub as a module or script outside the allowed shape
        # is denied, a search pattern, a message or an agent verb under a wrapper included
        for line in ('grep -rn "import hub" .', 'rg "import hub"', 'git log --grep "import hub"',
                     'echo "import hub"', 'python -m unittest -k "import hub"', 'python tools/x.py "import hub"',
                     'uv run python -m tools.hub questions'):
            with self.subTest(line):
                self.denied(lambda sub, line=line: bash(line, self.w.wt, sub), 'hub-owner-verb')
        for line in ('py -3.12 tools/x.py', 'uv run python -m unittest', 'python -m $m questions',
                     'python -m pip install hub',
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
        self.denied(lambda sub: bash('python "%s" questions add --title "$T"' % self.hub, self.w.wt, sub),
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
            ('script variable', lambda sub: ps('$s = "tools/hub.py"; python $s answer Q-1 --choice 1', self.w.wt, sub)),
            ('python -c', lambda sub: bash('python -c "import runpy, sys; sys.argv = [\'hub.py\', \'to-pm\', '
                                           '\'send\']; runpy.run_path(\'tools/hub.py\')"', self.w.wt, sub)),
        )
        for name, make in rows:
            with self.subTest(name):
                d = decide(make(True), self.ctx(OWNER))
                self.assertIsNotNone(d, 'allowed')
                self.assertIn(d.rule, ('hub-owner-verb', 'hub-secrets'), d.reason)

    def test_reading_hub_py_is_the_path_family(self):
        # these were denied by the fail-closed rule; the path family (rules.hub_path_family) now
        # reads them in full: one simple command of a reader or git, hub.py only as a path word
        for cmd in ('cat tools/hub.py', 'grep -n answer tools/hub.py', 'git diff tools/hub.py',
                    'git add tools/hub.py tests/hubsite/test_cli.py'):
            with self.subTest(cmd):
                self.allowed(lambda sub, cmd=cmd: bash(cmd, self.w.wt, sub))
                self.owner_allowed(lambda sub, cmd=cmd: bash(cmd, self.w.wt, sub))
        # beside other commands on a plain line it is still the family (rules.hub_family)
        for cmd in ('cat tools/hub.py; echo x', 'cd tools && cat hub.py'):
            with self.subTest(cmd):
                self.allowed(lambda sub, cmd=cmd: bash(cmd, self.w.wt, sub))
        # piped on, its output reaches another program: denied
        for cmd in ('cat tools/hub.py | head',):
            with self.subTest(cmd):
                self.denied(lambda sub, cmd=cmd: bash(cmd, self.w.wt, sub), 'hub-owner-verb')

    def test_the_alternative_names_the_kit_hub_tool(self):
        d = decide(self.run_hub('answer Q-1 --choice 1')(True), self.ctx(OWNER))
        self.assertEqual(d.instead, 'python "%s" questions add --title "<what the owner should decide>"' % self.hub)


class FailClosed(Hub):
    """Fail-closed: in a held session, any command text naming hub as a module or script is denied
    unless the parser reads that command in full as the one allowed shape (rules.hub_shape).
    Names built at run time are out of scope: the rule guards against accidents, not an
    adversary (docs/guard.md "Hub secrets", Limits)."""

    def rows(self, rows, deny):
        for shell, line in rows:
            # a keyword, not the message: a nested session subTest keeps the line in its report
            with self.subTest(line=line):
                make = lambda sub, shell=shell, line=line: shell(line, self.w.wt, sub)
                if deny:
                    self.denied(make, 'hub-owner-verb')
                    self.owner_allowed(make)
                else:
                    self.allowed(make)

    def test_review_3_options_before_c(self):
        # re-review 3 item 1: a -W or -X value holding a c hid the -c code
        code = '"import tools.hub as h; h.main()"'
        self.rows(((bash, 'python -Wonce -c %s' % code), (bash, 'python -Xtracemalloc -c %s' % code),
                   (bash, 'python -Xpycache_prefix=x -c %s' % code), (ps, 'python -Wonce -c %s' % code),
                   (ps, 'python -Xtracemalloc -c %s' % code)), True)

    def test_review_3_other_ways_to_run_hub(self):
        # re-review 3 item 2: modules that run code, code that runs a command line, other import
        # spellings, git's own command runners and a wrapper option whose value names python
        self.rows((
            (bash, 'python -m timeit -n1 -s "import tools.hub as h" "h.main([\'answer\', \'Q-1\'])"'),
            (bash, "printf 'import tools.hub as h; h.main()' | python -m code"),
            (bash, "printf 'import tools.hub as h; h.main()' | python -m asyncio"),
            (bash, 'python -m pdb -c "import tools.hub" -m json.tool'),
            (bash, 'python -c "import os; os.system(\'python -m tools.hub answer Q-1\')"'),
            (bash, 'python -c "import subprocess; subprocess.run([\'python\', \'-m\', \'tools.hub\', \'answer\'])"'),
            (bash, 'python -c "__import__(\'tools\', fromlist=[\'hub\']).hub.main()"'),
            (bash, 'python -c "import importlib; importlib.import_module(\'.hub\', \'tools\').main()"'),
            (bash, 'git rebase --exec "python -m tools.hub answer Q-1" HEAD~1'),
            (bash, 'git bisect run python -m tools.hub answer Q-1'),
            (bash, 'git submodule foreach "python -m tools.hub answer Q-1"'),
            (bash, 'uv run -p python python -c "import tools.hub as h; h.main()"'),
            (ps, 'python -m timeit -s "import tools.hub as h" "h.main()"'),
            (ps, 'git rebase --exec "python -m tools.hub answer Q-1" HEAD~1'),
        ), True)

    def test_names_the_shell_puts_together(self):
        # quotes and escapes inside a word, and a wildcard python's script word expands to hub.py
        self.rows((
            (bash, 'python -m tools.h"u"b answer Q-1'), (bash, "python tools/h'u'b.py answer Q-1"),
            (bash, 'python tools/h*.py answer Q-1'), (bash, 'python tools/hu?.py answer Q-1'),
            (bash, "cd tools && python -m 'hub' answer Q-1"), (ps, 'python -m tools.h`ub answer Q-1'),
        ), True)

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

    def test_ordinary_work_passes(self):
        self.rows((
            (bash, 'python -m unittest discover -s tests/guard -p "test_*.py"'), (bash, 'python -m unittest -k hub'),
            (bash, 'python -m unittest -k owner tests.guard.test_rules_hub'), (bash, 'python tools/gate.py --full'),
            (bash, 'python Z:/r/tools/gate.py -C Z:/r --full'), (bash, 'py -3 -m pip install x'),
            (bash, 'python -m json.tool x.json'), (bash, 'python -c "print(1)"'),
            (bash, 'python tools/backlog.py list --status open'), (bash, 'python $K/tools/doc_index.py --find open'),
            (bash, 'python $K/tools/backlog.py list --status open'), (bash, 'git log --oneline -3'),
            (bash, 'git commit -m "fix the hub page"'), (bash, 'git commit -m "hub: fix the page"'),
            (bash, 'git merge kit/hub-sessions'), (bash, 'uv run python -m unittest'),
            (bash, 'cat docs/action-hub.md'), (bash, 'python -m unittest discover -s tests/hubsite'),
            (bash, 'python -c "from kitlib.guard import hub; print(hub.OWNER_VERBS)"'),
            (bash, 'python -c "from kitlib.guard import rules, hub as h"'),
            (bash, 'python -c "import kitlib.guard.hub as h"'), (bash, 'python -m kitlib.guard.hub'),
            (bash, 'cat kitlib/guard/hub.py'), (bash, 'grep -rn hub kitlib/guard'),
            (ps, 'py -3 -m pip list'), (ps, 'python -m unittest -k hub'),
        ), False)

    def test_the_allowed_shape(self):
        hub = self.hub
        self.rows((
            (bash, 'python "%s" questions' % hub), (bash, 'python -B "%s" questions' % hub),
            (bash, 'python -m tools.hub questions'), (bash, 'python3.12 -OO -m tools.hub todos'),
            (bash, 'cd tools && python "%s" todos ack T-1' % hub),
            (bash, 'python "%s" questions > out.txt 2>&1' % hub), (bash, 'python "%s" inbox | head -3' % hub),
            (bash, 'python "%s" questions; python "%s" todos' % (hub, hub)),
            (ps, 'py -3 tools\\hub.py questions'), (ps, '& python "%s" questions' % hub),
        ), False)
        self.rows((
            (bash, 'python -i "%s" questions' % hub), (bash, 'python -X dev "%s" questions' % hub),
            (bash, 'python -W ignore -m tools.hub questions'), (bash, 'python "%s" questions $(echo x)' % hub),
            (bash, 'python "%s" questions "$t"' % hub), (bash, 'PYTHONINSPECT=1 python "%s" questions' % hub),
            (bash, 'bash -c "python %s questions"' % hub), (bash, 'uv run python "%s" questions' % hub),
            (bash, 'python "%s" questions; python -m tools.hub answer Q-1' % hub),
            (bash, 'python "%s" questions <<EOF\nx\nEOF' % hub), (bash, 'python -m runpy tools.hub questions'),
            (bash, 'python -mtools.hub questions'), (bash, './tools/hub.py questions'),
            (bash, 'python "%s" questions; echo tools.hub' % hub),
        ), True)

    def test_wildcard_words_leave_the_allowed_shape(self):
        # the shell may expand an unquoted wildcard to a file named like an owner verb or --owner,
        # so a word holding one is no literal word (rules.hub_word_globs); a redirect target too
        hub = self.hub
        self.rows((
            (bash, 'python "%s" a*' % hub), (bash, 'python -m tools.hub todos d?ne T-1'),
            (bash, 'python "%s" todos add --title t --o*' % hub), (bash, 'python "%s" [a]nswer Q-1' % hub),
            (bash, 'python "%s" questions > o*' % hub), (bash, 'python "%s" questions add --title "a"*' % hub),
            (ps, 'python "%s" a*' % hub), (ps, 'python -m tools.hub todos d?ne T-1'),
        ), True)
        # a quoted or escaped wildcard is literal text
        self.rows((
            (bash, 'python "%s" questions add --title "Which way?"' % hub),
            (bash, "python \"%s\" questions add --title 'a*b'" % hub),
            (bash, 'python "%s" questions add --title a\\*b' % hub),
            (bash, "python \"%s\" questions add --title $'a\\'*'" % hub),
            (ps, "python \"%s\" questions add --title 'Which way?'" % hub),
            (ps, 'python "%s" questions add --title a`*b' % hub),
        ), False)

    def test_hub_in_python_code_that_imports(self):
        # Python joins a line ending in a backslash, and takes a comment inside a bracketed import
        # list, so any standalone hub on a line that runs python and holds import counts, wherever
        # python takes its code from (rules.hub_code_names)
        self.rows((
            (bash, "python - <<'EOF'\nimport tools.\\\nhub\nEOF"),
            (bash, "python - <<'EOF'\nimport tools\\\n.hub as h\nEOF"),
            (bash, "python - <<'EOF'\nfrom tools import (os,\n# a comment\nhub)\nEOF"),
            (bash, "python -c 'from tools import (\n# x\nhub)'"),
            (bash, "python -c 'import tools.\\\nhub as h; h.main()'"),
            (bash, "uv run python -c 'import tools.\\\nhub'"),
            (bash, "printf 'import tools.\\\\\nhub' | python"),
            (bash, "python <<< 'from tools import \\\nhub'"),
            # over-reach: hub in a string of python code that imports
            (bash, "python - <<'EOF'\nimport json\nprint('hub page')\nEOF"),
        ), True)
        self.rows((
            (bash, "python - <<'EOF'\nimport json\nprint('the page')\nEOF"),
            (bash, "python -c 'print(\"hub\")'"),
            (bash, "python - <<'EOF'\nfrom kitlib.guard import hub\nprint(hub.OWNER_VERBS)\nEOF"),
        ), False)

    def test_a_hub_folder_in_a_path_is_not_the_hub_tool(self):
        # a folder named hub (hub/ followed by a separator) is a path, not the hub tool, also on a
        # line that runs python and imports
        todos, store, mid = 'Z:/repos/other/hub/todos', 'Z:/x/hub/store', 'Z:/x/a/hub/b/c.txt'
        self.rows((
            (bash, 'ls %s/' % todos), (ps, 'Get-ChildItem %s/' % todos), (bash, 'cat %s/T-1.json' % todos),
            (ps, 'Get-Content %s/T-1.json' % todos), (bash, 'ls %s' % store), (bash, 'cat %s' % mid),
            (ps, 'dir Z:\\x\\hub\\todos'), (ps, 'Get-ChildItem Z:\\x\\a\\hub\\b'),
            (bash, 'ls %s/ && python -c "import json"' % todos),
            (bash, 'python -c "import os; print(os.listdir(\'%s\'))"' % todos),
            (ps, 'python -c "import os; print(os.listdir(\'%s\'))"' % todos),
            (bash, 'python -c "import json; print(json.load(open(\'%s/q.json\')))"' % store),
            (bash, "python - <<'EOF'\nimport os\nprint(os.listdir('hub/todos'))\nEOF"),
            (bash, "python - <<'EOF'\nimport pathlib\nprint(pathlib.Path('%s').read_text())\nEOF" % mid),
            (ps, "Get-ChildItem %s | python -c 'import sys; print(sys.stdin.read())'" % todos),
        ), False)
        for path in (todos + '/T-1.json', store + '/q.json', mid):
            with self.subTest(path):
                self.allowed(lambda sub, path=path: event('Read', {'file_path': path}, self.w.wt, sub))
        # on purpose: a backslash after hub may be Python's line join (import tools.\<newline>hub
        # in a string the shell decodes), and hub with no separator after it may be the name itself
        self.rows((
            (ps, 'python -c "import os; print(os.listdir(r\'Z:\\x\\hub\\todos\'))"'),
            (bash, 'python -c "import os; print(os.listdir(\'Z:/x/hub\'))"'),
            (bash, 'python -c "import os; print(os.listdir(\'Z:/x/hub/\'))"; echo hub'),
        ), True)

    def test_hub_in_a_folder_name_a_message_or_a_file_written_is_not_the_hub_tool(self):
        # observed refusals (2026-10-15): hub inside a folder or file name (hub-conductor,
        # docs/hub-mcp.md), a heredoc that cat writes to a file (data, as a file the Write tool
        # writes: rules.hub_code_names), commit.py's -m message after its script word (python's own
        # -m comes before the script: rules.hub_module_names), and a log row quoting such a command
        body = ("import json, os\nroot = 'hub-conductor/docs-tab-check/test-store'\n"
                "hub = os.path.join(root, 'questions')\nprint(hub, 'Hub store ready')\n")
        gen = "mkdir -p hub-conductor/docs-tab-check/test-store && cat > gen.py <<'EOF'\n" + body + "EOF\n"
        commit = ('python Z:/k/tools/commit.py -C $PWD -m "Hub store: pass the PM\'s configured repo names" '
                  '--model M kitlib/hubsite/store.py')
        self.rows((
            (bash, gen), (bash, gen + 'python gen.py'), (bash, gen + 'python gen.py --out docs/hub-mcp.md'),
            (bash, "mkdir -p hub-conductor/x && cat >> gen.py <<EOF\nimport os\n# write the hub store\nEOF"),
            (bash, 'cat docs/hub-mcp.md'), (bash, 'mkdir -p hub-conductor/refusals && python -c "import json"'),
            # hub followed by a hyphen, or inside a backslash path, in python code with an import
            (bash, 'python -c "import os; print(os.listdir(\'Z:/x/hub-conductor\'))"'),
            (ps, 'python -c "import os; print(os.listdir(r\'Z:\\x\\hub-conductor\\y\'))"'),
            (bash, 'python -c "import os; print(os.listdir(r\'Z:\\x\\hub-conductor\\y\'))"'),
            (ps, 'python -c "import os; print(os.path.exists(\'docs/hub-mcp.md\'))"'),
            (bash, 'git commit -m "Hub store: fix x"'), (bash, 'git commit -m "Hub store: import the todos"'),
            (bash, 'cd %s && %s' % (W.fwd(self.w.wt), commit)), (bash, commit),
            (bash, 'python Z:/k/tools/backlog.py add K-1 -m "Hub store" --summary x'),
            (bash, 'python -I "Z:/k/tools/ledger.py" add -m hub --q x'),
            (ps, 'cd %s; %s' % (W.fwd(self.w.wt), commit)),
            (ps, "Add-Content -Encoding utf8 -LiteralPath refusals.md -Value '| 2026-10-15 | worker | Bash | "
                 "%s | names the hub tool (... -m \"Hub) [kit guard: rule=hub-owner-verb role=subagent] |'"
                 % commit.replace("'", "''")),
            (ps, "Add-Content -Encoding utf8 -LiteralPath refusals.md -Value '| 2026-10-15 | planner | Bash | "
                 "mkdir -p Z:/AgentTemp/hub-conductor/x && cat > gen.py <<''EOF'' ... EOF | names the hub tool "
                 "(hub) [kit guard: rule=hub-owner-verb role=subagent] |'"),
        ), False)
        # what reaches python stays read: a heredoc python or a pipe takes, a hub path or import in
        # a file cat writes, and python's own -m before any script word
        self.rows((
            (bash, "cat <<'EOF' | python -\nimport json\nprint('hub page')\nEOF"),
            (bash, "cat > gen.py <<'EOF' | python -\nimport json\nprint('hub page')\nEOF"),
            (bash, "cat > gen.py <<'EOF'\nimport tools.hub as h\nh.main(['answer', 'Q-1'])\nEOF\npython gen.py"),
            (bash, "cat > gen.py <<'EOF'\nimport json\nEOF\npython - <<'EOF'\nimport json\nhub = 1\nEOF"),
            (bash, "cat > $f <<'EOF'\nimport json\nhub = 1\nEOF\npython gen.py"),
            (bash, "cat > /dev/stdout <<'EOF'\nimport json\nhub = 1\nEOF\npython gen.py"),
            (bash, "cat > gen.py <<EOF\nimport json\n$(python -c 'print(1)')\nhub = 1\nEOF\npython gen.py"),
            (bash, "cat() { python -; }; cat > gen.py <<'EOF'\nimport json\nhub = 1\nEOF"),
            (bash, "{ cat > gen.py <<'EOF'\nimport json\nhub = 1\nEOF\n} ; python gen.py"),
            # a device however the path spells it, and a line that can open a stream into python
            # (exec, eval, source, ., coproc): rules.hub_device_target, HUB_CAT_REDEFINED_RE
            (bash, "cat > /./dev/stdout <<'EOF'\nimport json\nhub = 1\nEOF\npython gen.py"),
            (bash, "cat > /dev/../dev/stdout <<'EOF'\nimport json\nhub = 1\nEOF\npython gen.py"),
            (bash, "cat > //dev/stdout <<'EOF'\nimport json\nhub = 1\nEOF\npython gen.py"),
            (bash, "cat > dev/stdout <<'EOF'\nimport json\nhub = 1\nEOF\npython gen.py"),
            (bash, "cat > /proc/self/fd/1 <<'EOF'\nimport json\nhub = 1\nEOF\npython gen.py"),
            (bash, "exec 3> >(python -); cat > /./dev/fd/3 <<'EOF'\nimport json\nhub = 1\nEOF"),
            (bash, "exec 3> >(python -); cat > gen.py <<'EOF'\nimport json\nhub = 1\nEOF"),
            (bash, "exec 3> >(python -); cat >&3 <<'EOF'\nimport json\nhub = 1\nEOF"),
            (bash, "eval x; cat > gen.py <<'EOF'\nimport json\nhub = 1\nEOF\npython gen.py"),
            (bash, "source x; cat > gen.py <<'EOF'\nimport json\nhub = 1\nEOF\npython gen.py"),
            (bash, ". x; cat > gen.py <<'EOF'\nimport json\nhub = 1\nEOF\npython gen.py"),
            (bash, "coproc python -; cat > gen.py <<'EOF'\nimport json\nhub = 1\nEOF"),
            (bash, 'python Z:/k/tools/commit.py -m "$(python -m hub answer Q-1)" x.py'),
            (bash, 'python -- Z:/k/tools/commit.py -m hub'), (bash, 'python -m Z:/k/tools/commit.py -m hub'),
            (bash, 'cd tools && python -W ignore -m hub answer Q-1'), (bash, 'cd tools && python -Bm hub answer Q-1'),
            (bash, 'cd tools && python C:/Python/Lib/cProfile.py -m hub answer Q-1'),
            (bash, 'cd tools && python "$S" -m hub answer Q-1'), (bash, 'cd tools && python $S -m hub answer Q-1'),
            (bash, 'cd tools && python -m cProfile -m hub answer Q-1'),
            (bash, 'cd tools && python -c "x" Z:/k/tools/commit.py -m hub answer Q-1'),
            (ps, 'cd tools; python -X dev -m hub answer Q-1'),
            # the shell joins what the guard's split parts: an escaped space, a quote, a backtick, a
            # variable or an expansion before the script word may hand it to -X or -W as a value,
            # leaving python's own -m (rules.hub_py_script_first)
            (bash, 'cd tools && python -Xa\\ commit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X a\\ commit.py -m hub answer Q-1'),
            (ps, 'cd tools; python -X a` commit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -W a\\ commit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X a\\\tcommit.py -m hub answer Q-1'),
            (bash, 'cd tools && python --check-hash-based-pycs a\\ commit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X "a "commit.py -m hub answer Q-1'),
            (bash, "cd tools && python -X a' 'commit.py -m hub answer Q-1"),
            (bash, 'cd tools && python -X $v commit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X ${v} commit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X {a,b} commit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X * commit.py -m hub answer Q-1'),
            (ps, 'cd tools; python -X $v commit.py -m hub answer Q-1'),
            (ps, "cd tools; python -X 'a commit.py' -m hub answer Q-1"),
            # bash splits words on space and tab only: a no-break space, a control or an em space
            # between -X's value and commit.py is part of that value, so the -m is python's
            (bash, 'cd tools && python -X a commit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X a\x1fcommit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X a\vcommit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X a\fcommit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X a commit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -Xa commit.py -m hub answer Q-1'),
            (bash, 'cd tools && python -X a commit.py -m hub answer Q-1'),
            # and inside a nested shell, read by its words as well as the line's text
            (bash, "bash -c 'cd tools && python -X a commit.py -m hub answer Q-1'"),
            (bash, 'bash -c "cd tools && python -X \'a commit.py\' -m hub answer Q-1"'),
            (ps, 'cmd /c "cd tools && python -X a commit.py -m hub answer Q-1"'),
        ), True)

    def test_hub_in_python_code_from_anywhere_on_the_line(self):
        # a pipe of any length, a module that runs code it reads (code, asyncio, timeit -s,
        # pdb -c), and a newline the shell decodes ($'...', PowerShell's backtick n) all reach
        # python; the line is read whole, not where its code comes from (rules.hub_code_names)
        self.rows((
            (bash, "echo 'from tools import \\\nhub as h; h.main()' | cat | python - answer Q-1"),
            (bash, "cat <<'EOF'| cat | python - answer Q-1\nfrom tools import \\\nhub as h; h.main()\nEOF"),
            (bash, "cat <<'EOF' | python - answer Q-1\nfrom tools import \\\nhub as h; h.main()\nEOF"),
            (bash, "python -m code <<'EOF'\nfrom tools import \\\nhub as h\nEOF"),
            (bash, "echo 'from tools import \\\nhub as h' | python -m code"),
            (bash, "echo 'import tools.\\\nhub as h' | python -m asyncio"),
            (bash, "python -m timeit -s 'from tools import \\\nhub as h' 'h.main()'"),
            (bash, "python -m pdb -c 'from tools import \\\nhub' -m json.tool"),
            (bash, "echo $'from tools import \\\\\\nhub as h; h.main()' | python"),
            (bash, "printf '%s\\\\n' $'import tools.\\\\\\nhub as h' | python"),
            (bash, "cat <<< $'import tools.\\\\\\nhub as h' | python"),
            (bash, "echo 'from tools import (os,' 'hub)' | python"),
            (ps, "'from tools import \\\nhub as h; h.main()' | Write-Output | python - answer Q-1"),
            (ps, '@"\nfrom tools import \\\nhub as h\n"@ | python -m code'),
            (ps, "@'\nimport tools.\\\nhub as h\n'@ | python -m asyncio"),
            (ps, "python -m timeit -s 'from tools import \\\nhub as h' 'h.main()'"),
            (ps, 'echo "import tools.\\`nhub as h; h.main()" | python - answer Q-1'),
            (ps, '"import tools.\\`nhub as h; h.main()" | python - answer Q-1'),
            (ps, 'Write-Output "from tools import \\`nhub as h`nh.main()" | python'),
            (ps, "'from tools import \\','hub as h; h.main()' | python - answer Q-1"),
            # ipython and pypy (a version, .exe and a path too) run python code as well
            (bash, "pypy3 -c 'from tools import \\\nhub'"),
            (bash, "echo 'from tools import \\\nhub' | ipython"),
            (ps, "'from tools import \\','hub as h; h.main()' | ipython"),
            (bash, "pypy3.10.exe -c 'import tools.\\\nhub'"), (bash, "C:/x/pypy.exe -c 'from tools import \\\nhub'"),
            # over-reach: python and import anywhere on the line, hub anywhere else on it
            (bash, 'python -c "import json"; echo hub'),
        ), True)
        self.rows((
            (bash, 'python -m code < x.py'), (ps, 'Get-Content x | python -m code'),
            (bash, "python -c 'from tools import (os,' -c 'x'"),
            (bash, "echo 'import json' | python -m code"), (bash, 'ipython -c "print(1)"'),
            (bash, 'python "%s" questions add --title "import x"' % self.hub),
        ), False)

    def test_the_path_family(self):
        # a single simple command of a reader, git's reads and staging, commit.py or backlog.py,
        # naming the hub tool only as a path word (or anyhow in a commit or backlog message), is read
        # in full (rules.hub_path_family); these were denied by the fail-closed rule
        self.rows((
            (bash, 'cat tools/hub.py'), (bash, 'grep -n answer tools/hub.py'), (bash, 'rg -n answer tools/hub.py'),
            (bash, 'rg --pretty answer tools/hub.py'),
            (bash, 'git diff tools/hub.py'), (bash, 'git diff tools/hub.py 2>&1'),
            (bash, 'git -C Z:/r diff -- tools/hub.py'), (bash, 'git --no-pager log -p -- tools/hub.py'),
            (bash, 'git show HEAD:tools/hub.py'), (bash, 'git blame tools/hub.py'), (bash, 'git status tools/hub.py'),
            (bash, 'git add tools/hub.py tests/hubsite/test_cli.py'), (bash, 'git restore --staged tools/hub.py'),
            (bash, 'cat tools/hub.py > notes.txt'),
            (ps, 'Get-Content tools/hub.py'), (ps, 'type tools\\hub.py'), (ps, '& git diff tools/hub.py'),
            (ps, 'Select-String -Path tools/hub.py -Pattern answer'),
            (bash, 'python Z:/k/tools/commit.py -C Z:/r -m "guard: read tools/hub.py" --model M tools/hub.py'),
            (bash, 'python -B Z:/k/tools/commit.py -m x --owner-authored -- tools/hub.py'),
            # a plain chain of family commands and others (rules.hub_family); git commit is family too
            (bash, 'git diff tools/hub.py && echo ok'), (bash, 'git commit -m x tools/hub.py'),
        ), False)
        # a program or form outside the list, an outlet beside it, or a wildcard leaves the family
        self.rows((
            (bash, 'git diff tools/hub.py; python -m tools.hub answer Q-1'),
            (bash, 'cat tools/hub.py | python'), (bash, 'cat $(echo tools/hub.py)'), (bash, 'cat tools/hub.py &'),
            (bash, 'cat tools/hub.py # import hub'), (bash, 'GIT_EXTERNAL_DIFF=x git diff tools/hub.py'),
            (bash, 'command cat tools/hub.py'), (bash, 'sudo cat tools/hub.py'), (bash, 'less tools/hub.py'),
            (bash, 'cat tools/hub.py > tools/h*.py'), (bash, 'grep -n answer tools/hub.py tools/hu?.py'),
            (bash, 'git restore tools/hub.py'),
            (bash, 'git log --grep tools.hub'), (bash, 'grep -rn tools.hub kitlib'), (bash, 'grep "import hub" tools/hub.py'),
            (bash, 'python -i Z:/k/tools/commit.py -m x tools/hub.py'),
            (bash, 'python Z:/k/tools/commit.py -m "$m" tools/hub.py'),
            (bash, 'python Z:/k/tools/commit.py -m x tools/h*.py'),
            (bash, 'python Z:/k/tools/commit.py -m x tools.hub'),
            (bash, 'python Z:/k/tools/commit.py -m x --body-file tools/hub.py tools/x.py'),
            (bash, 'python Z:/k/tools/commit.py -m x --model tools.hub tools/x.py'),
            (bash, 'python Z:/k/tools/commit.py -m x --unknown tools/hub.py'),
            # the script word is the commit.py or backlog.py path itself, naming no hub
            (bash, 'python tools/hub.py/../commit.py -m x a.py'), (bash, 'python tools.hub/backlog.py list'),
            (ps, 'python tools\\hub.py\\..\\commit.py -m x a.py'),
        ), True)

    def test_the_path_family_refuses_options_that_run_a_program(self):
        # git's -c and --exec-path (only -C, --no-pager and -P come before git's subcommand),
        # --ext-diff and --textconv (a unique prefix too), add's editor and prompts, restore's
        # writes to the work tree, and rg's --pre, --pre-glob and --hostname-bin
        self.rows((
            (bash, 'rg --hostname-bin x answer tools/hub.py'), (bash, 'rg --hostname-bin=x answer tools/hub.py'),
            (bash, 'rg --hostn x answer tools/hub.py'), (ps, 'rg --hostname-bin x answer tools/hub.py'),
            (bash, 'git -c diff.external=x diff tools/hub.py'), (bash, 'git -c core.pager=x log tools/hub.py'),
            (bash, 'git --exec-path=x diff tools/hub.py'), (bash, 'git --exec-path x diff tools/hub.py'),
            (bash, 'git diff --ext-diff tools/hub.py'), (bash, 'git diff --ext tools/hub.py'),
            (bash, 'git log --textconv -p tools/hub.py'), (bash, 'git show --textconv=1 HEAD:tools/hub.py'),
            (bash, 'git blame --textc tools/hub.py'),
            (bash, 'git add -e tools/hub.py'), (bash, 'git add --edit tools/hub.py'), (bash, 'git add -p tools/hub.py'),
            (bash, 'git add -Ai tools/hub.py'), (bash, 'git add --interactive tools/hub.py'),
            (bash, 'git restore --staged --worktree tools/hub.py'), (bash, 'git restore -SW tools/hub.py'),
            (bash, 'git restore --staged -p tools/hub.py'),
            (bash, 'rg --pre x answer tools/hub.py'), (bash, 'rg --pre=x answer tools/hub.py'),
            (bash, "rg --pre-glob '*.py' --pre x answer tools/hub.py"), (bash, 'rg --pre-glob=x answer tools/hub.py'),
            (ps, 'git -c diff.external=x diff tools/hub.py'), (ps, 'rg --pre x answer tools/hub.py'),
        ), True)

    def test_messages_naming_the_hub_tool(self):
        # commit.py's -m and -b, and backlog.py's message options, are message text: the hub tool
        # may be named there in any spelling; elsewhere on the line it is still denied
        self.rows((
            (bash, 'python Z:/k/tools/commit.py -m "guard: fix tools.hub parsing" tools/x.py'),
            (bash, 'python Z:/k/tools/commit.py -m "import hub fix" -b "tools/hub.py and tools.hub" tools/hub.py'),
            (bash, 'python Z:/k/tools/commit.py -mtools.hub tools/x.py'),
            (bash, 'python Z:/k/tools/backlog.py add K-1 --summary "tools.hub answers twice" --found-on none '
                   '--why-not-folded "needs tools/hub.py"'),
            (bash, 'python Z:/k/tools/backlog.py add K-1 --summary="python -m tools.hub" --found-on none '
                   '--why-not-folded x'),
            (bash, 'python Z:/k/tools/backlog.py close K-1 --reason "fixed in tools/hub.py"'),
            (ps, 'python Z:/k/tools/commit.py -m "guard: fix tools.hub" tools/x.py'),
            # git commit's message is message text too (rules.hub_git_family)
            (bash, 'git commit -m "fix tools.hub"'),
        ), False)
        self.rows((
            # argparse reads --summ as --summary, the guard reads only the option written in full
            (bash, 'python Z:/k/tools/backlog.py add K-1 --summ "tools.hub" --found-on none --why-not-folded x'),
            (bash, 'python Z:/k/tools/backlog.py add K-1 --summary x --found-on tools.hub --why-not-folded x'),
            (bash, 'echo "tools.hub" | python Z:/k/tools/commit.py -m x a'),
            (bash, 'python Z:/k/tools/commit.py -m "fix tools.hub" tools/x.py; python -m tools.hub answer Q-1'),
            (bash, 'python -c "import x" Z:/k/tools/commit.py -m "tools.hub" tools/x.py'),
        ), True)

    def test_git_commit_message_spellings(self):
        # every spelling of git commit's message is text (rules.hub_git_words): a flag cluster
        # ending in m with its value apart or joined, --message apart or with =
        self.rows((
            (bash, 'git commit -am "fix tools.hub"'), (bash, 'git commit -qam "fix tools.hub" tools/hub.py'),
            (bash, 'git commit -amtools.hub'), (bash, 'git commit -mtools.hub tools/hub.py'),
            (bash, 'git commit --message "fix tools.hub" tools/hub.py'),
            (bash, 'git commit --message="fix tools.hub" tools/hub.py'),
            (ps, 'git commit -am "fix tools.hub"'), (ps, 'git commit --message="fix tools.hub"'),
        ), False)
        # the cluster's other letters and the words after the message are still read
        self.rows((
            (bash, 'git commit -am x tools.hub'), (bash, 'git commit -amx tools.hub'),
            (bash, 'git commit --message=x tools.hub'), (bash, 'git commit --message x tools.hub'),
            (bash, 'git commit -em "fix tools.hub"'), (bash, 'git commit -pm x tools/hub.py'),
            (bash, 'git commit -m x -- tools.hub'),
        ), True)

    def test_the_compiled_hub_tool(self):
        # python runs a .pyc as it runs hub.py (rules.HUB_NAME_RES)
        self.rows((
            (bash, 'python tools/__pycache__/hub.cpython-312.pyc answer Q-1'),
            (bash, 'cd tools/__pycache__ && python hub.cpython-312.pyc answer Q-1'),
            (bash, 'python tools/hub.pyc answer Q-1'), (ps, 'python tools\\hub.pyw answer Q-1'),
            (bash, 'python tools/__pycache__/hub.cpython-312.opt-1.pyc answer Q-1'),
            (bash, 'python tools/__pycache__/hub.cpython-312.opt-2.pyc answer Q-1'),
            # over-reach: a read of the compiled file is denied too, as the path family takes only
            # a path ending in hub.py
            (bash, 'cat tools/__pycache__/hub.cpython-312.pyc'),
            (ps, 'Get-Content tools\\__pycache__\\hub.cpython-312.pyc'),
        ), True)
        # owner ruling Q-1010: only a bare python word takes commit.py's -m as its message; a python
        # with a path before it, in a folder named like python or not, keeps the -m python's
        # (fail-closed; agents use bare python). A variable keeps the line out of the path family
        self.rows((
            (bash, 'D:/bin/python.exe Z:/k/tools/commit.py -C $PWD -m "Hub store: x" a.py'),
            (ps, '& D:\\bin\\python.exe Z:/k/tools/commit.py -C $PWD -m "Hub store: x" a.py'),
            (bash, 'D:/bin/python.exe Z:/k/tools/commit.py -C $PWD -m Hub a.py'),
            (bash, 'D:/Programs/Python/Python312/python.exe Z:/k/tools/commit.py '
                   '-C $PWD -m "Hub store: x" a.py'),
            (ps, '& D:\\Programs\\Python\\Python312\\python.exe Z:/k/tools/commit.py '
                 '-C $PWD -m "Hub store: x" a.py'),
            (bash, 'C:/Python312/python.exe -X utf8 Z:/k/tools/commit.py -C $PWD -m "Hub store: x" a.py'),
            (ps, 'C:\\Python312\\python Z:/k/tools/commit.py -C $PWD -m "Hub store: x" a.py'),
            (bash, '"D:/Programs/Python312/python.exe" Z:/k/tools/commit.py -C $PWD -m "Hub store: x" a.py'),
            (bash, 'bash -c "D:/bin/python.exe Z:/k/tools/commit.py -C . -m \'Hub store: x\' a.py"'),
            # a ( inside a path is no group: cmd runs D:/a(python.exe as the program
            (ps, 'cmd /c "D:/a(python.exe Z:/k/tools/commit.py -C . -m Hub a.py"'),
            (bash, 'cmd //c "D:/a((python.exe Z:/k/tools/commit.py -C . -m Hub a.py"'),
        ), True)
        # a bare python word still does (owner ruling Q-1009)
        self.rows((
            (bash, 'python Z:/k/tools/commit.py -C $PWD -m "Hub store: x" a.py'),
            (ps, '& python Z:/k/tools/commit.py -C $PWD -m "Hub store: x" a.py'),
            (bash, 'python3.12 -X utf8 Z:/k/tools/commit.py -C $PWD -m "Hub store: x" a.py'),
            (ps, 'python.exe Z:/k/tools/commit.py -C $PWD -m "Hub store: x" a.py'),
            (bash, 'echo $(python Z:/k/tools/commit.py -C $PWD -m "Hub store: x" a.py)'),
        ), False)
        # and a python in that folder runs hub as a module
        self.rows((
            (bash, 'D:/Programs/Python312/python.exe -m hub answer Q-1'),
            (ps, '& D:\\Programs\\Python312\\python.exe -m hub answer Q-1'),
            (bash, 'cd tools && C:/Python312/python.exe -X a\\ commit.py -m hub answer Q-1'),
            # a python word before / or \ with no python word after it in that word is the program:
            # cmd ends a command's name at an unquoted /, so `python/x` runs python with /x
            (bash, 'cd tools && python/x -m hub answer Q-1'),
            (ps, 'cd tools; python/x -m hub answer Q-1'),
            (ps, 'cd tools; python\\x -m hub answer Q-1'),
            (ps, 'cmd /c "cd tools && python/x -m hub answer Q-1"'),
            (bash, 'cmd //c "cd tools && python/x -m hub answer Q-1"'),
            # and what is glued to it is the program's own name (argv[0]), never its script
            (bash, 'cmd //c "cd tools && python/commit.py -m hub answer Q-1"'),
            (ps, 'cmd /c "cd tools && python\\commit.py -m hub answer Q-1"'),
            # a quote glued to a python word makes words of its own: the whole quoted run is argv[0]
            (bash, 'cmd //c \'cd tools && python/" commit.py x" -m hub answer Q-1\''),
            (ps, 'cmd /c \'cd tools && python/" commit.py x" -m hub answer Q-1\''),
            (bash, 'cmd //c \'cd tools && python/python" commit.py x" -m hub answer Q-1\''),
            (ps, 'cmd /c \'cd tools && python/python" commit.py x" -m hub answer Q-1\''),
            (bash, 'cd tools && python/" commit.py x" -m hub answer Q-1'),
            (ps, 'cmd /c "cd tools && python/\' commit.py x\' -m hub answer Q-1"'),
            (bash, 'cd tools && "python/" commit.py x" -m hub answer Q-1'),
            # cmd ends a command's name at ; , = as at a space, and none of them ends the command
            (ps, 'cmd /c "cd tools && python;x -m hub answer Q-1"'),
            (bash, 'cmd //c "cd tools && python;x -m hub answer Q-1"'),
            (ps, 'cmd /c "cd tools && python;-m hub answer Q-1"'),
            (ps, 'cmd /c "cd tools && python;a; -m hub answer Q-1"'),
            (ps, 'cmd /c "cd tools && python,x -m hub answer Q-1"'),
            (ps, 'cmd /c "cd tools && python=x -m hub answer Q-1"'),
        ), True)
        # owner ruling Q-1012: a python word in quotes, a path or not, keeps commit.py's -m python's
        self.rows((
            (ps, '& "D:\\Programs\\Python312\\python.exe" Z:/k/tools/commit.py -C $PWD -m "Hub store: x" a.py'),
            (bash, "'D:/Programs/Python312/python.exe' Z:/k/tools/commit.py -C $PWD -m \"Hub store: x\" a.py"),
            (bash, '"python" Z:/k/tools/commit.py -C $PWD -m "Hub store: x" a.py'),
            (ps, "& 'python.exe' Z:/k/tools/commit.py -C $PWD -m \"Hub store: x\" a.py"),
        ), True)

    def test_a_separator_inside_one_of_pythons_words(self):
        # round-6 review item 1: a ; | & or newline the shell keeps inside one of python's words
        # (quoted, escaped, or inside a nested shell's string) ended the read of the line's text
        # before python's -m; python's parsed words are read too (rules.hub_word_module_names),
        # and a line the parser cannot read whole is read past every separator
        # (rules.HUB_PY_MODULE_ANY_RE)
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
            (bash, "cd tools && python -X ';' -m runpy hub answer Q-1"),
            (bash, "cd tools && python -X ';' -mrunpy hub answer Q-1"),
            (bash, "cd tools && python -X ';' -m trace --count --module=hub answer Q-1"),
            (bash, "cd tools && python -X ';' -m trace --count --module hub answer Q-1"),
            # inside a nested shell's string
            (bash, 'bash -c "cd tools && python -X \';\' -m hub answer Q-1"'),
            (ps, 'powershell -Command "cd tools; python -X \'a;b\' -m hub answer Q-1"'),
            # a quoted word before commit.py is not plain, so the -m after it is python's
            (bash, "cd tools && python -X 'a b' commit.py -X ';' -m hub answer Q-1"),
            # Windows PowerShell 5.1 passes a word holding a double quote on unescaped, so python
            # gets -X a; and then -m hub as words of their own
            (ps, 'cd tools; python -X \'a;" -m hub "b\' answer Q-1'),
            # and the program's C runtime drops a quote inside a word: python gets -Bm
            (ps, 'cd tools; python \'-B"m"\' hub answer Q-1'),
            # a line the parser cannot read whole (an unclosed quote)
            (bash, "cd tools && python -X ';' -m hub answer Q-1 \""),
            (ps, "cd tools; python -X 'a;b' -m hub answer Q-1 \""),
            # a python in a quoted folder with a space in it
            (ps, 'cd tools; & "C:\\Program Files\\Python312\\python.exe" -X \'a;b\' -m hub answer Q-1'),
            (bash, 'cd tools && "C:/Program Files/Python312/python.exe" -X \'a;b\' -m hub answer Q-1'),
        ), True)
        # cmd ends a command at an unquoted |, so b, not python, gets -m hub: the guard reads
        # cmd's string as cmd does and passes it; a | cmd quotes or escapes is python's word
        self.rows(((ps, 'cmd /c "cd tools && python -X a|b -m hub answer Q-1"'),), False)
        self.rows((
            (bash, 'cmd //c \'cd tools && python -X "a|b" -m hub answer Q-1\''),
            (bash, 'cmd //c \'cd tools && python -X a^|b -m hub answer Q-1\''),
            (ps, 'cmd /c "cd tools && python -X a^|b -m hub answer Q-1"'),
            (ps, 'cmd /c \'cd tools && python -X "a|b" -m hub answer Q-1\''),
            (ps, 'cmd /c "cd tools && python -X a^&b -m hub answer Q-1"'),
        ), True)
        # over-reach, fail-closed (round-6 review item 3): a ; glued to a python word is read as cmd
        # reads it, ending only the command's name, so the next python word counts as python's
        # argument and commit.py's -m as python's own
        self.rows(((bash, 'which python; python Z:/k/tools/commit.py -C $PWD -m "Hub store: x" a.py'),), True)
        # a separator inside a message, or in python's words with no hub module named, passes
        self.rows((
            (bash, 'python -X utf8 Z:/k/tools/commit.py -C $PWD -m "Hub store: a; b | c & d" a.py'),
            (ps, 'python Z:/k/tools/commit.py -C $PWD -m "Hub store: a; b | c" a.py'),
            (bash, 'python Z:/k/tools/backlog.py add K-1 -m "Hub store; a | b" --summary x'),
            (bash, "cd tools && python -X utf8 Z:/k/tools/commit.py -X ';' -m hub answer Q-1"),
            (bash, "cd tools && python -X ';' -m hubby answer Q-1"),
            (bash, "python -X 'a;b' -m unittest"), (ps, "python -X 'a|b' -m json.tool x.json"),
        ), False)

    def test_a_python_in_a_quoted_folder_with_a_space(self):
        # owner rulings Q-1010 and Q-1012: a quoted python path with a space in it is a full path
        # and quoted, so commit.py's -m after it stays python's (fail-closed; agents use bare python)
        self.rows((
            (ps, '& "C:\\Program Files\\Python312\\python.exe" Z:/k/tools/commit.py -C $PWD '
                 '-m "Hub store: x" a.py'),
            (ps, '& "C:\\Program Files\\Python312\\python.exe" -X utf8 Z:/k/tools/commit.py -C $PWD '
                 '-m "Hub store: x" a.py'),
            (bash, '"C:/Program Files/Python312/python.exe" Z:/k/tools/commit.py -C $PWD -m "Hub store: x" a.py'),
            (bash, '"C:/Program Files/Python312/python.exe" -X utf8 Z:/k/tools/commit.py -C $PWD '
                   '-m "Hub store: x" a.py'),
            (bash, "'C:/Program Files/Python312/python.exe' Z:/k/tools/commit.py -C $PWD -m \"Hub store: x\" a.py"),
            (bash, "'C:/Program Files/Python/python.exe' Z:/k/tools/commit.py -C $PWD -m Hub a.py"),
        ), True)
        # and the folder's python runs hub as a module, and a quoted run glued after its closing
        # quote is read as part of python's own name
        self.rows((
            (ps, 'cd tools; & "C:\\Program Files\\Python312\\python.exe" -m hub answer Q-1'),
            (bash, 'cd tools && "C:/Program Files/Python312/python.exe" -m hub answer Q-1'),
            (ps, 'cd tools; & "C:\\Program Files\\Python312\\python.exe"" commit.py x" -m hub answer Q-1'),
            (bash, 'cd tools && "C:/Program Files/Python312/python.exe"" commit.py x" -m hub answer Q-1'),
        ), True)

    def test_backlog_paths_naming_the_hub_tool(self):
        # backlog.py records paths (--scope, --body-file, a positional) and runs none of them, so a
        # path word to hub.py passes there as in commit.py's paths, on every verb
        backlog = 'python Z:/k/tools/backlog.py -C Z:/pm --session kit '
        self.rows((
            (bash, backlog + 'add EX-HUBMIRROR-RETIRE --summary "drop the leftovers in tools/hub.py" --found-on none '
                             '--why-not-folded "needs tools/hub.py" --scope tools/hub.py --scope tests/hubsite'),
            (bash, backlog + 'set EX-RETIRE --scope tools/hub.py --scope kitlib/hubsite'),
            (bash, backlog + 'set EX-RETIRE --scope=tools/hub.py'),
            (bash, backlog + 'add EX-RETIRE --body-file tools/hub.py --found-on none --why-not-folded x'),
            (bash, backlog + 'close EX-RETIRE --reason "fixed in tools/hub.py"'),
            (ps, backlog + 'set EX-RETIRE --scope tools\\hub.py'),
            # backlog.py in a folder named hub is still backlog.py, not the hub tool
            (bash, 'python tools/hub/backlog.py set EX-RETIRE --scope tools/hub.py'),
        ), False)
        # the hub tool run, imported or named as a module, beside backlog.py or in its words
        self.rows((
            (bash, backlog + 'set EX-RETIRE --scope tools.hub'),
            (bash, backlog + 'set EX-RETIRE --scope=tools.hub'),
            (bash, backlog + 'set EX-RETIRE --scope tools/hub.py; python tools/hub.py answer Q-1'),
            (bash, backlog + 'set EX-RETIRE --scope tools/hub.py && python -c "import tools.hub"'),
            (bash, 'python tools/hub.py answer Q-1 --scope tools/hub.py'),
            (bash, 'python -c "import tools.hub" Z:/k/tools/backlog.py set EX-RETIRE --scope kitlib/x'),
            (bash, 'python -m tools.hub answer Q-1'),
        ), True)

    def test_backlog_show_of_a_hub_path(self):
        # backlog.py show only prints what it is given; a positional path word to hub.py passes
        self.rows((
            (bash, 'python Z:/k/tools/backlog.py show tools/hub.py'),
            (ps, 'python Z:/k/tools/backlog.py show tools\\hub.py'),
        ), False)
        self.rows(((bash, 'python Z:/k/tools/backlog.py show tools.hub'),), True)

    def test_whole_tree_commits_pass(self):
        # a commit or staging that names no hub path passes whatever it holds (tools/hub.py among
        # the changes included); only text naming the hub tool is read
        self.rows((
            (bash, 'git add -A'), (bash, 'git add tools'), (bash, 'python Z:/k/tools/commit.py -m x tools'),
        ), False)

    def test_family_commands_on_a_plain_line(self):
        # the string-literal false positives: a line of plain commands joined by ;, &&, || or
        # a newline, where every command naming the hub tool is a family command, passes
        # (rules.hub_family); git commit and ledger.py are family commands, their messages text
        wt = W.fwd(self.w.wt)
        self.rows((
            (bash, 'cd %s && python tools/commit.py -m "Hub: x" tools/hub.py' % wt),
            (ps, 'cd %s; python tools/commit.py -m "Hub: x" tools/hub.py' % wt),
            (bash, 'python Z:/k/tools/ledger.py add --repo kit --q "does tools/hub.py answer twice?" --a no'),
            (bash, 'git commit -m "Hub: fix tools/hub.py"'),
            (bash, 'git add tools/hub.py && python Z:/k/tools/commit.py -m "Hub: x" tools/hub.py'),
            (bash, 'python Z:/k/tools/backlog.py close K-1 --reason "fixed in tools/hub.py" 2>&1; git status'),
            (bash, 'git diff tools/hub.py\ngit status'),
        ), False)
        # a way for one command's output, name or state to reach another, or a command that is not
        # plain, leaves the family; the hub tool run anywhere on the line stays denied
        self.rows((
            (bash, 'cat tools/hub.py; python "$_" answer Q-1'), (bash, 'cat tools/hub.py; python ${_} answer Q-1'),
            (ps, 'gc tools/hub.py -ov c; python -c "$c" answer Q-1'), (ps, 'gc tools/hub.py; python -c @c'),
            (ps, 'Get-Content tools/hub.py; python $$ answer Q-1'), (ps, 'gc tools/hub.py; python $^'),
            (ps, 'gc tools/hub.py -OutVariable c; python -c "$c"'),
            (bash, 'cat tools/hub.py > x.py; python x.py answer Q-1'),
            (bash, 'cat() { python "$1" answer Q-1; }; cat tools/hub.py'),
            (ps, 'function cat { python $args }; cat tools/hub.py'),
            (ps, 'Set-Alias cat python; cat tools/hub.py answer Q-1'),
            (bash, 'alias cat=python; cat tools/hub.py answer Q-1'),
            (bash, 'cd x && python tools/hub.py answer Q-1'),
            (bash, 'echo x & python tools/hub.py answer Q-1'), (bash, 'cat tools/hub.py & echo x'),
            (bash, 'git commit -m x tools/hub.py; python -c "import tools.hub"'),
            (bash, 'git commit -e -m x tools/hub.py'), (bash, 'git commit --edit -m x tools/hub.py'),
            (bash, 'git commit -p -m x tools/hub.py'),
            (bash, 'git -c user.name=x commit -m x tools/hub.py'),
            (bash, 'GIT_EDITOR=python git commit -e tools/hub.py'),
            (bash, 'git show HEAD:tools/hub.py --output=x.py; python x.py answer Q-1'),
            (bash, 'git commit -m x tools.hub'),
            (bash, 'python Z:/k/tools/ledger.py add --repo tools.hub --q x'),
            # a drive made on the line, or a write to a provider drive, can define what a later
            # name runs (a function, PATH) on PowerShell: a drive maker is refused on the line
            # (rules.HUB_LINE_DENIED), a write to env: is a variable set (commands.line_env_names),
            # and function: by its provider's name is a drive (rules.HUB_LINE_DRIVES)
            (ps, 'New-PSDrive -Name f -PSProvider Function -Root \\; Get-Content tools/hub.py'),
            (ps, 'ndr f Function \\; Get-Content tools/hub.py'),
            (ps, 'mount f Function \\; Get-Content tools/hub.py'),
            (ps, 'Set-Content env:PATH Z:\\x; git diff tools/hub.py'),
            (ps, 'Set-Content -Path Env:\\Path -Value Z:\\x; git diff tools/hub.py'),
            (ps, 'Add-Content -Path:env:PATH -Value ;Z:\\x; git diff tools/hub.py'),
            (ps, "Copy-Item Z:\\x\\f.ps1 Microsoft.PowerShell.Core\\Function::cat; Get-Content tools/hub.py"),
        ), True)
        # a write to a file beside a family command stays plain
        self.rows((
            (ps, 'Get-Content tools/hub.py; Set-Content notes.txt x'),
            (ps, 'git show HEAD:tools/hub.py; Add-Content -Path notes.txt -Value x'),
        ), False)

    def test_only_a_bare_python_is_a_family_command(self):
        # owner rulings Q-1010 and Q-1012: a python with a path, or in quotes, before commit.py is
        # no family command (rules.hub_family_cmd), on a plain line or alone, so its -m is python's
        wt = W.fwd(self.w.wt)
        self.rows((
            (bash, 'cd %s && D:/bin/python.exe Z:/k/tools/commit.py -m "Hub store: x" a.py' % wt),
            (ps, 'cd %s; D:\\bin\\python.exe Z:/k/tools/commit.py -m "Hub store: x" a.py' % wt),
            (bash, 'cd %s && "D:/bin/python.exe" Z:/k/tools/commit.py -m "Hub store: x" a.py' % wt),
            (ps, "cd %s; & 'D:\\bin\\python.exe' Z:/k/tools/commit.py -m \"Hub store: x\" a.py" % wt),
            (bash, 'D:/bin/python.exe Z:/k/tools/commit.py -m "Hub store: x" a.py'),
            (ps, '& D:\\bin\\python.exe Z:/k/tools/commit.py -m "Hub store: x" a.py'),
            (bash, '"D:/bin/python.exe" Z:/k/tools/commit.py -m "Hub store: x" a.py'),
            (ps, "& 'D:\\bin\\python.exe' Z:/k/tools/commit.py -m \"Hub store: x\" a.py"),
            (bash, '"python" Z:/k/tools/commit.py -m "Hub store: x" a.py'),
            # a backslash-newline joins the lines; the python after it still has a path
            (bash, 'cd %s && \\\nD:/bin/python.exe Z:/k/tools/commit.py -m "Hub store: x" a.py' % wt),
        ), True)
        # a bare python is (owner ruling Q-1009); pyth""on reaches python bare
        self.rows((
            (bash, 'python Z:/k/tools/commit.py -m "Hub store: x" a.py'),
            (ps, '& python Z:/k/tools/commit.py -m "Hub store: x" a.py'),
            (ps, 'cd %s; & python Z:/k/tools/commit.py -m "Hub store: x" a.py' % wt),
            (bash, 'pyth""on Z:/k/tools/commit.py -m "Hub store: x" a.py'),
        ), False)
        # a bare python starting a nested shell's text is judged by that text (owner ruling Q-1009)
        self.rows((
            (bash, "bash -c 'python Z:/k/tools/commit.py -m \"Hub store: x\" a.py'"),
            (ps, "cmd /c 'python Z:/k/tools/commit.py -m \"Hub store: x\" a.py'"),
        ), False)
        # a full or quoted python path inside the nested text keeps its -m python's
        self.rows((
            (bash, 'bash -c \'"D:/bin/python.exe" Z:/k/tools/commit.py -m "Hub store: x" a.py\''),
            (bash, "bash -c 'D:/bin/python.exe Z:/k/tools/commit.py -m \"Hub store: x\" a.py'"),
        ), True)


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


class CrtWords(unittest.TestCase):
    """rules.hub_crt_words: the C runtime's split of a native program's command-line text."""

    def test_splits(self):
        from kitlib.guard.rules import hub_crt_words
        for text, words in (
                ('a b\tc', ['a', 'b', 'c']),
                ('"a;" -m hub "b"', ['a;', '-m', 'hub', 'b']),
                ('a;" -m hub "b', ['a; -m hub b']),
                ('-B"m"', ['-Bm']),
                ('"a ""b"" c"', ['a "b" c']),
                ('a\\\\"b c"', ['a\\b c']),
                ('a\\"b', ['a"b']),
                ('a\\b', ['a\\b']),
                ('""', ['']),
                ('', [])):
            with self.subTest(text=text):
                self.assertEqual(hub_crt_words(text), words)


setUpModule = W.deny_softened           # these pin hub-owner-verb, off by default (test_cheap.py)
tearDownModule = W.allow_unplaced


if __name__ == '__main__':
    unittest.main()
