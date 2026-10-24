"""The owner's delegation list (kitlib.guard.delegfile, docs/guard.md "Owner delegation list"): every
session but the owner's own is refused a write, move or delete of either copy, the home one
(~/.pm-kit/owner-delegations.json) and the PM repository's (<pointer>/.pm-kit/...), by file
tool, shell or git; reads and copies out stay open."""
import os
import unittest
from unittest import mock

from kitlib.guard import context, delegfile, rules

from . import world as W
from .claudehost import decide
from .world import bash, event, ps

RULE = delegfile.RULE
# (environ, subagent) of every session the rule holds: the pm, a coordinator, a plain-marked
# session (one the write jail does not hold), a worker's mark, and a subagent
SESSIONS = (({context.ROLE_ENV: 'pm'}, False), ({context.ROLE_ENV: 'coordinator'}, False),
            ({context.ROLE_ENV: 'plain'}, False), ({context.ROLE_ENV: 'senior-worker'}, False),
            ({}, True))
OWNER = ({}, False)


class OwnerDelegations(unittest.TestCase):

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.pointer = self.w.data
        self.home_copy = os.path.join(self.w.home, '.pm-kit', 'owner-delegations.json')
        self.pm_copy = os.path.join(self.w.data, '.pm-kit', 'owner-delegations.json')
        for f in (self.home_copy, self.pm_copy):
            os.makedirs(os.path.dirname(f), exist_ok=True)
            with open(f, 'w', encoding='utf-8') as fh:
                fh.write('{"schema": 1, "operations": {}}\n')
        with open(os.path.join(self.w.wt, 'new.json'), 'w', encoding='utf-8') as fh:
            fh.write('{}\n')

    def ctx(self, environ, **kw):
        kw.setdefault('pointer', lambda: self.pointer)
        return self.w.ctx(environ=environ, **kw)

    def run_one(self, payload_of, environ, sub, **kw):
        return decide(payload_of(sub), self.ctx(environ, **kw))

    def assert_refused(self, payload_of, label, sessions=SESSIONS, instead='python -m json.tool "', **kw):
        for environ, sub in sessions:
            with self.subTest(label, environ=environ, sub=sub):
                d = self.run_one(payload_of, environ, sub, **kw)
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', RULE), d.reason)
                self.assertIn("the owner's delegation list", d.reason)
                self.assertTrue(d.instead.startswith(instead), d.instead)

    def assert_not_refused(self, payload_of, label, sessions=SESSIONS + (OWNER,), **kw):
        for environ, sub in sessions:
            with self.subTest(label, environ=environ, sub=sub):
                d = self.run_one(payload_of, environ, sub, **kw)
                self.assertTrue(d is None or d.rule != RULE, d and d.reason)

    # ------------------------------------------------------------------ file tools

    def edits(self, path):
        return (
            ('Write', lambda sub: event('Write', {'file_path': path, 'content': '{}'}, self.w.wt, sub)),
            ('Edit', lambda sub: event('Edit', {'file_path': path, 'old_string': '{', 'new_string': '['},
                                       self.w.wt, sub)),
            ('Write fwd', lambda sub: event('Write', {'file_path': W.fwd(path), 'content': '{}'}, self.w.wt, sub)),
        )

    def test_a_file_tool_edit_of_either_copy_is_refused(self):
        for path in (self.home_copy, self.pm_copy):
            for name, payload in self.edits(path):
                self.assert_refused(payload, '%s %s' % (name, path))

    def test_the_owner_session_may_edit_it(self):
        for path in (self.home_copy, self.pm_copy):
            for name, payload in self.edits(path):
                self.assert_not_refused(payload, name, sessions=(OWNER,))

    def test_a_case_or_separator_spelling_is_the_same_file(self):
        upper = self.home_copy.upper()
        self.assert_refused(lambda sub: event('Write', {'file_path': upper, 'content': '{}'}, self.w.wt, sub), 'upper')

    def test_with_no_pointer_only_the_home_copy_is_held(self):
        for name, payload in self.edits(self.home_copy):
            self.assert_refused(payload, name, pointer=lambda: None)
        for name, payload in self.edits(self.pm_copy):
            self.assert_not_refused(payload, name, pointer=lambda: None)

    def test_a_pointer_the_check_would_refuse_is_still_held(self):
        """The pointer's value is held as stored: one naming the main checkout holds its copy."""
        copy = os.path.join(self.w.main, '.pm-kit', 'owner-delegations.json')
        self.assert_refused(lambda sub: event('Write', {'file_path': copy, 'content': '{}'}, self.w.wt, sub),
                            'main', pointer=lambda: self.w.main)

    def test_another_file_of_the_folder_is_not_the_list(self):
        for path in (os.path.join(self.w.home, '.pm-kit', 'config.json'),
                     os.path.join(self.w.data, '.pm-kit', 'notes.json'),
                     os.path.join(self.w.wt, '.pm-kit', 'owner-delegations.json')):
            self.assert_not_refused(lambda sub, p=path: event('Write', {'file_path': p, 'content': '{}'},
                                                               self.w.wt, sub), path)

    # ------------------------------------------------------------------ shell writes

    def shell_rows(self):
        h, p = W.fwd(self.home_copy), W.fwd(self.pm_copy)
        hb, pb = self.home_copy, self.pm_copy
        hdir, data = W.fwd(os.path.dirname(self.home_copy)), W.fwd(self.w.data)
        return [
            (ps, 'Remove-Item "%s"' % hb), (ps, 'ri "%s"' % pb), (ps, 'del "%s"' % hb),
            (ps, 'Move-Item "%s" "%s"' % (pb, os.path.join(self.w.temp, 'x.json'))),
            (ps, 'Rename-Item "%s" old.json' % hb),
            (ps, 'Set-Content "%s" "{}"' % hb), (ps, 'Add-Content -Path "%s" -Value x' % pb),
            (ps, 'Clear-Content "%s"' % hb), (ps, '"{}" | Out-File "%s"' % pb),
            (ps, '"{}" > "%s"' % hb), (ps, '"{}" >> "%s"' % pb),
            (ps, 'Copy-Item new.json "%s"' % hb),
            (ps, "[IO.File]::WriteAllText('%s', '{}')" % hb),
            (ps, 'cmd /c del "%s"' % pb), (ps, 'cmd /c move "%s" x.json' % hb),
            (bash, 'rm "%s"' % h), (bash, 'rm -f %s' % p), (bash, 'mv %s /tmp/x.json' % h),
            (bash, 'echo {} | tee %s' % p), (bash, "sed -i 's/a/b/' %s" % h), (bash, 'truncate -s 0 %s' % p),
            (bash, 'cp new.json %s' % h), (bash, 'echo {} > %s' % p), (bash, 'echo {} >> %s' % h),
            # the home folder's spellings, and a relative path after a cd home
            (bash, 'rm ~/.pm-kit/owner-delegations.json'), (bash, 'rm "$HOME/.pm-kit/owner-delegations.json"'),
            (ps, 'Remove-Item $env:USERPROFILE\\.pm-kit\\owner-delegations.json'),
            (ps, 'Remove-Item ~\\.pm-kit\\owner-delegations.json'),
            (bash, 'cd ~ && rm .pm-kit/owner-delegations.json'),
            (bash, 'cd "$HOME" && echo {} > .pm-kit/owner-delegations.json'),
            # brace groups bash writes out: every group, nested ones too
            (bash, 'rm ~/.pm-kit/{owner,x}-{delegations,y}.json'),
            (bash, 'rm ~/.pm-kit/{x,{owner-delegations,y}}.json'),
            (bash, 'rm %s/.pm-kit/owner{-x,}{-delegations,}.json' % data),
            # a folder that holds it
            (bash, 'rm -rf %s' % hdir), (bash, 'rm -rf ~/.pm-kit'), (ps, 'Remove-Item -Recurse -Force "%s"' % data),
            (bash, 'mv %s/.pm-kit %s/old' % (data, data)),
            # code on the line that names the list and writes it
            (bash, "python -c \"open(r'%s', 'w').write('{}')\"" % h),
            (bash, "python -c \"import os; os.remove(os.path.expanduser('~/.pm-kit/owner-delegations.json'))\""),
            (bash, "python -c \"from kitlib.delegations import load; import os; os.remove(load.delegations_file())\""),
            (bash, "python -c \"import pathlib; pathlib.Path('%s').write_text('{}')\"" % p),
            (bash, "node -e \"require('fs').writeFileSync('%s', '{}')\"" % h),
            (bash, "python - <<'EOF'\nimport shutil\nshutil.move('%s', 'x.json')\nEOF" % h),
        ]

    def test_a_shell_write_of_either_copy_is_refused(self):
        for shell, text in self.shell_rows():
            self.assert_refused(lambda sub, s=shell, t=text: s(t, self.w.wt, sub), text)

    def test_the_owner_session_may_write_it_by_shell(self):
        for shell, text in self.shell_rows():
            self.assert_not_refused(lambda sub, s=shell, t=text: s(t, self.w.wt, sub), text, sessions=(OWNER,))

    def test_reads_and_copies_out_stay_open(self):
        h, p = W.fwd(self.home_copy), W.fwd(self.pm_copy)
        for shell, text in (
                (bash, 'cat %s' % h), (ps, 'Get-Content "%s"' % self.pm_copy),
                (bash, 'python -m json.tool %s' % h),
                (bash, 'cp %s %s/copy.json' % (h, W.fwd(self.w.temp))),
                (ps, 'Copy-Item "%s" "%s"' % (self.pm_copy, os.path.join(self.w.temp, 'copy.json'))),
                (bash, "python -c \"print(open(r'%s').read())\"" % p),
                (bash, "python -c \"import json; print(json.load(open(r'%s', encoding='ascii')))\"" % h),
                (bash, 'grep -n schema %s' % p),
                (bash, 'git -C %s show HEAD:.pm-kit/owner-delegations.json' % W.fwd(self.w.data)),
                (bash, 'git -C %s log -- .pm-kit/owner-delegations.json' % W.fwd(self.w.data))):
            self.assert_not_refused(lambda sub, s=shell, t=text: s(t, self.w.wt, sub), text)

    def test_an_ordinary_brace_line_gets_no_refusal(self):
        """Every brace group written out leaves a line inside the worktree as open as before: no
        rule refuses or warns."""
        for folder in ('a', 'b', os.path.join('b', 'a')):
            os.makedirs(os.path.join(self.w.wt, folder), exist_ok=True)
        for name in ('x.py', 'y.py', 'x1.txt', 'y2.txt'):
            with open(os.path.join(self.w.wt, 'a', name), 'w', encoding='utf-8') as fh:
                fh.write('x\n')
        for text in ('cp a/{x,y}.py b/', 'cp a/{x,y}{1,2}.txt b/a/', 'mkdir -p src/{a,b{c,d}}'):
            for environ, sub in (({context.ROLE_ENV: 'senior-worker'}, False), ({}, True)):
                with self.subTest(text, environ=environ, sub=sub):
                    d = self.run_one(lambda sub, t=text: bash(t, self.w.wt, sub), environ, sub)
                    self.assertIsNone(d, d and (d.verdict, d.rule, d.reason))

    def test_a_brace_line_past_the_limit_is_refused(self):
        """A word whose brace groups give more than 256 words is not written out, so no path rule
        sees the words bash makes from it: rule brace-limit refuses the line in every session, the
        owner's included (docs/guard.md "Rules"). At 256 words every word is read, and the list's
        own rule refuses the line."""
        many = ','.join('a%d' % n for n in range(1, 257))
        over = ('rm ~/.pm-kit/owner-{delegations,%s}.json' % many,
                'rm -rf ~/{.pm-kit,%s}' % ','.join('x%d' % n for n in range(1, 257)),
                "bash -c 'rm ~/.pm-kit/owner-{delegations,%s}.json'" % many)
        for text in over:
            for environ, sub in SESSIONS + (OWNER,):
                with self.subTest(text[:50], environ=environ, sub=sub):
                    d = self.run_one(lambda sub, t=text: bash(t, self.w.wt, sub), environ, sub)
                    self.assertIsNotNone(d, 'allowed')
                    self.assertEqual((d.verdict, d.rule), ('deny', 'brace-limit'), d.reason)
                    self.assertIn('more than 256 words', d.reason)
        at = 'rm ~/.pm-kit/owner-{delegations,%s}.json' % ','.join('a%d' % n for n in range(1, 256))
        self.assert_refused(lambda sub: bash(at, self.w.wt, sub), at[:50])

    # ------------------------------------------------------------------ wildcards, the pipeline, stand-ins

    def placed_rows(self):
        """(shell, text, the folder it runs in) that reach the list by a wildcard, by the paths the
        command before a | gives, or as the folder a program that writes there runs in."""
        data, wt, home = self.w.data, self.w.wt, self.w.home
        d = W.fwd(data)
        pm_kit = os.path.dirname(self.home_copy)
        return [
            # a wildcard that can match the list or a folder above it
            (bash, 'rm ~/.pm-kit/*.json', wt), (bash, 'rm -f ~/.pm-kit/owner-*', wt),
            (bash, 'rm -rf ~/.pm-*', wt), (bash, 'rm -rf ~/*', wt),
            (bash, 'rm .pm-kit/owner-delegations.jso?', data), (bash, 'rm [o]wner-delegations.json', pm_kit),
            (bash, 'mv %s/.pm-kit/* x/' % d, wt), (bash, 'rm -r %s/**/x' % d, wt),
            (ps, 'Remove-Item .pm-kit\\*', data), (ps, 'Remove-Item "$HOME\\.pm-kit\\*.json"', wt),
            (ps, 'Remove-Item -Recurse -Force %s\\.pm-k*' % data, wt), (ps, 'del *', pm_kit),
            # a path fed through the pipeline
            (ps, 'Get-Item "$HOME\\.pm-kit\\owner-delegations.json" | Remove-Item', wt),
            (ps, 'Get-ChildItem "%s" | Remove-Item' % pm_kit, wt),
            (ps, 'Get-ChildItem | Remove-Item', pm_kit), (ps, 'gci -Recurse | ri', data),
            (bash, 'ls ~/.pm-kit/owner-delegations.json | xargs rm', wt),
            (bash, 'find %s -name "*.json" | xargs rm' % d, wt),
            (bash, 'git ls-files | xargs rm', data), (bash, 'git ls-files "*.json" | xargs rm -f', data),
            # a computed word read through a leading home variable, and a bare folder name
            (ps, 'Get-ChildItem "$HOME\\.pm-k*" | Remove-Item', wt), (bash, 'ls "$HOME"/.pm-k*/* | xargs rm', wt),
            (ps, 'Get-ChildItem .\\x, %s -Recurse -Filter owner-deleg* | Remove-Item' % os.path.basename(home),
             os.path.dirname(home)),
            (ps, 'Get-ChildItem .\\x, %s -Recurse -Filter owner-deleg* | Remove-Item' % os.path.basename(data),
             os.path.dirname(data)),
            # a program that writes in the folder it runs in, run in the list's folder or above it
            (bash, 'git archive HEAD~3 | tar -x', data), (bash, 'unzip -o x.zip', data),
            (bash, 'cd ~ && tar -xf backup.tar', wt), (bash, 'curl -O http://example.invalid/y', home),
            (bash, 'tar -xf backup.tar', pm_kit),
        ]

    def test_a_wildcard_a_piped_path_or_a_program_writing_where_it_runs_is_refused(self):
        for shell, text, cwd in self.placed_rows():
            self.assert_refused(lambda sub, s=shell, t=text, c=cwd: s(t, c, sub), text)

    def test_the_owner_session_may_run_those(self):
        for shell, text, cwd in self.placed_rows():
            self.assert_not_refused(lambda sub, s=shell, t=text, c=cwd: s(t, c, sub), text, sessions=(OWNER,))

    def test_a_wildcard_a_piped_path_or_a_program_elsewhere_is_not_this_rule(self):
        data, wt, home = self.w.data, self.w.wt, self.w.home
        for shell, text, cwd in (
                (bash, 'rm ~/.pm-kit/*.log', wt), (bash, 'rm ~/.pm-kit/x/*', wt), (bash, 'rm -rf ~/.cache/*', wt),
                (bash, 'rm %s/.pm-kit/*.md' % W.fwd(data), wt), (ps, 'Remove-Item .pm-kit\\*.log', data),
                (ps, 'Get-ChildItem *.log | Remove-Item', home), (ps, 'Get-ChildItem | Remove-Item', wt),
                (bash, 'ls *.tmp | xargs rm', home), (bash, 'git ls-files | xargs rm', wt),
                (bash, 'git ls-files docs | xargs rm', wt), (bash, 'git ls-files "$d" | xargs rm', wt),
                (ps, 'Get-ChildItem $p | Remove-Item', wt), (ps, 'Get-ChildItem "$HOME\\.cache" | Remove-Item', wt), (bash, 'find docs -name "*.md" | xargs rm', data),
                (bash, 'git archive HEAD | tar -x', wt), (bash, 'unzip -o x.zip', wt),
                (bash, 'tar -xf backup.tar', self.w.temp), (bash, 'curl -O http://example.invalid/y', wt)):
            self.assert_not_refused(lambda sub, s=shell, t=text, c=cwd: s(t, c, sub), text)

    def test_a_program_run_in_the_pm_repository_does_not_write_it(self):
        """The folder a program runs in stands for what it may write: it counts as the list only
        when it is the file, so python and the kit's tools run in the PM repository pass."""
        data = W.fwd(self.w.data)
        for text in ('python -c "print(1)"', 'python script.py', 'cd %s && python -c "print(2)"' % data,
                     'python tools/backlog.py -C %s show EX-1' % data,
                     "python -c \"import json; print(json.load(open('.pm-kit/owner-delegations.json')))\""):
            self.assert_not_refused(lambda sub, t=text: bash(t, self.w.data, sub), text)

    def test_code_that_names_the_list_and_writes_another_file_is_refused(self):
        """The inline check reads names and writes, not data flow: code that reads the list and
        writes elsewhere is refused (it can be run as a script file instead)."""
        text = ("python -c \"d = open('%s').read(); open('out.json', 'w').write(d)\""
                % W.fwd(self.home_copy))
        self.assert_refused(lambda sub: bash(text, self.w.wt, sub), text)

    # ------------------------------------------------------------------ git in the PM repository

    GIT_REFUSED = (
        'rm .pm-kit/owner-delegations.json', 'rm -r .pm-kit', 'rm -r .', 'rm --cached -r .pm-kit',
        'rm ".pm-kit/*.json"', 'rm :/.pm-kit/owner-delegations.json',
        'mv .pm-kit/owner-delegations.json old.json', 'mv .pm-kit old',
        'checkout main', 'checkout HEAD~1 -- .pm-kit/owner-delegations.json', 'checkout -- .',
        'switch other', 'switch -c new',
        'restore .pm-kit/owner-delegations.json', 'restore --source HEAD~1 :/', 'restore -SW .pm-kit',
        'restore --staged --w .pm-kit/owner-delegations.json',     # `--w` is `--worktree` to git
        'restore --staged --wor .pm-kit/owner-delegations.json',
        'restore --staged --no- .pm-kit/owner-delegations.json',   # git rejects `--no-` as ambiguous; judged
        'restore -S --no-st .pm-kit/owner-delegations.json',       # `--no-st` is `--no-staged`
        'checkout-index -a -f', 'checkout-index -f .pm-kit/owner-delegations.json',
        'reset --hard', 'reset --hard HEAD~1', 'reset --merge', 'reset --keep HEAD~1',
        'clean -fdx', 'clean -f .pm-kit', 'stash', 'stash push', 'stash pop', 'stash drop',
        'stash -- show', 'stash -- list', 'stash -q list',     # a verb is only the first word: these push
        'read-tree -u -m HEAD',
        'sparse-checkout set docs', 'sparse-checkout disable', 'sparse-checkout reapply',
        'bisect start', 'bisect bad HEAD~3', 'bisect reset',
    )
    # the same, run in the list's own folder: paths read from there
    GIT_REFUSED_BELOW = (
        'rm owner-delegations.json', 'rm -r .', 'rm -r ..', 'mv owner-delegations.json old.json',
        'restore owner-delegations.json', 'restore :/.pm-kit', 'checkout-index -f owner-delegations.json',
        'checkout HEAD~1 -- .', 'clean -f .', 'rm "*.json"', 'rm ":(top).pm-kit/owner-delegations.json"',
        'checkout main', 'reset --hard', 'clean -fdx', 'stash',
    )
    GIT_OPEN = (
        'status', 'log', 'diff', 'show HEAD', 'stash list', 'stash show', 'stash show -p', 'clean -n', 'clean -fn',
        'clean --dry-run -f', 'clean -f docs', 'rm docs/x.md', 'mv docs/a.md docs/b.md',
        'restore --staged .pm-kit/owner-delegations.json', 'restore docs/x.md',
        'checkout -- docs/x.md', 'checkout-index -f docs/x.md', 'reset', 'reset --soft HEAD~1',
        'reset -- .pm-kit/owner-delegations.json', 'rm ":!.pm-kit" docs',
        'add .pm-kit/owner-delegations.json', 'read-tree HEAD', 'sparse-checkout list', 'bisect log',
    )

    def test_git_that_removes_or_overwrites_the_pm_copy_is_refused(self):
        data = W.fwd(self.w.data)
        for rest in self.GIT_REFUSED:
            self.assert_refused(lambda sub, r=rest: bash('git %s' % r, self.w.data, sub), 'in: ' + rest)
            self.assert_refused(lambda sub, r=rest: bash('git -C %s %s' % (data, r), self.w.wt, sub), '-C: ' + rest)
        for rest in self.GIT_REFUSED_BELOW:
            self.assert_refused(lambda sub, r=rest: ps('git %s' % r, os.path.join(self.w.data, '.pm-kit'), sub),
                                'below: ' + rest)

    def test_the_owner_session_may_run_that_git(self):
        for rest in self.GIT_REFUSED:
            self.assert_not_refused(lambda sub, r=rest: bash('git %s' % r, self.w.data, sub), rest, sessions=(OWNER,))

    def test_other_git_in_the_pm_repository_is_not_this_rule(self):
        for rest in self.GIT_OPEN:
            self.assert_not_refused(lambda sub, r=rest: bash('git %s' % r, self.w.data, sub), rest)

    def test_git_in_another_tree_is_not_this_rule(self):
        for rest in ('rm .pm-kit/owner-delegations.json', 'reset --hard', 'checkout main', 'clean -fdx', 'stash'):
            self.assert_not_refused(lambda sub, r=rest: bash('git %s' % r, self.w.wt, sub), rest)

    def test_a_pointer_on_the_main_checkout_holds_git_there(self):
        main = W.fwd(self.w.main)
        for rest in ('reset --hard', 'rm -r .pm-kit', 'clean -fdx'):
            self.assert_refused(lambda sub, r=rest: bash('git -C %s %s' % (main, r), self.w.wt, sub), rest,
                                pointer=lambda: self.w.main)

    def test_removing_the_pm_repositorys_worktree_is_refused_from_any_place(self):
        data, main = W.fwd(self.w.data), W.fwd(self.w.main)
        for shell, text, cwd in ((bash, 'git worktree remove %s' % data, self.w.main),
                                 (bash, 'git -C %s worktree remove --force %s' % (main, data), self.w.wt),
                                 (ps, 'git worktree move "%s" "%s"' % (self.w.data, self.w.temp), self.w.main),
                                 (bash, 'git worktree remove ../pmdata', self.w.main)):
            self.assert_refused(lambda sub, s=shell, t=text, c=cwd: s(t, c, sub), text)
        self.assert_not_refused(lambda sub: bash('git worktree remove %s' % W.fwd(self.w.wt), self.w.main, sub),
                                'another worktree')

    def test_a_computed_pathspec_in_the_pm_repository_is_refused(self):
        self.assert_refused(lambda sub: bash('git rm "$f"', self.w.data, sub), 'dyn')

    # ------------------------------------------------------------------ a line the guard cannot read

    def test_an_unreadable_line_that_names_the_list_is_refused(self):
        text = '"$p" ~/.pm-kit/owner-delegations.json'
        self.assert_refused(lambda sub: bash(text, self.w.wt, sub), text, instead='bash "')   # plainly(): a script file
        self.assert_not_refused(lambda sub: bash(text, self.w.wt, sub), text, sessions=(OWNER,))

    def test_an_unreadable_line_that_writes_it_gives_the_rules_own_refusal(self):
        text = 'eval "$c"; rm ~/.pm-kit/owner-delegations.json'
        for environ, sub in SESSIONS:
            with self.subTest(environ=environ, sub=sub):
                d = self.run_one(lambda s: bash(text, self.w.wt, s), environ, sub)
                self.assertEqual((d.verdict, d.rule), ('deny', RULE), d.reason)
                self.assertIn('rm ~/.pm-kit/owner-delegations.json', d.reason)

    # ------------------------------------------------------------------ the refusal

    def test_the_refusal_names_the_session_and_the_read_alternative(self):
        d = self.run_one(lambda sub: bash('rm %s' % W.fwd(self.pm_copy), self.w.wt, sub),
                         {context.ROLE_ENV: 'coordinator'}, False)
        self.assertIn('This session is marked coordinator.', d.reason)
        self.assertIn('Ask the owner (through the PM) to change it.', d.reason)
        self.assertEqual(d.instead, 'python -m json.tool %s' % rules.quote(self.pm_copy))


class FolderWildcards(unittest.TestCase):
    """A wildcard in a folder part of a write target (delegfile.glob_config) is resolved as
    Windows would, against the folders on disk: one that can match a machine config folder names
    it (rule machine-config), one that cannot stays open."""

    CONFIG = 'machine-config'                   # rules.MACHINE_CONFIG

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.pointer = None
        for top in (self.w.wt, self.w.plain):
            for name in (os.path.join('.pm-kit', 'config.json'), os.path.join('.pm-kit', 'local.json'),
                         os.path.join('.claude', 'settings.json')):
                path = os.path.join(top, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'w', encoding='utf-8') as fh:
                    fh.write('{}\n')
        os.makedirs(os.path.join(self.w.wt, 'sub', '.pm-kit'))
        with open(os.path.join(self.w.wt, 'sub', '.pm-kit', 'config.json'), 'w', encoding='utf-8') as fh:
            fh.write('{}\n')

    def decide(self, payload, environ):
        return decide(payload, self.w.ctx(environ=environ, pointer=lambda: self.pointer))

    def assert_config(self, payload_of, label):
        for environ, sub in SESSIONS:
            with self.subTest(label, environ=environ, sub=sub):
                d = self.decide(payload_of(sub), environ)
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', self.CONFIG), d.reason)
        d = self.decide(payload_of(False), OWNER[0])
        self.assertTrue(d is None or d.rule != self.CONFIG, d and d.reason)

    def assert_not_config(self, payload_of, label):
        for environ, sub in SESSIONS + (OWNER,):
            with self.subTest(label, environ=environ, sub=sub):
                d = self.decide(payload_of(sub), environ)
                self.assertTrue(d is None or d.rule != self.CONFIG, d and d.reason)

    def test_a_star_in_a_folder_part_names_the_config_folder(self):
        wt = W.fwd(self.w.wt)
        for name, payload in (
                ('rm fwd', lambda sub: bash('rm .pm-k*t/config.json', self.w.wt, sub)),
                ('rm abs fwd', lambda sub: bash('rm %s/.p*/config.json' % wt, self.w.wt, sub)),
                ('Remove-Item back', lambda sub: ps('Remove-Item .pm-k*t\\config.json', self.w.wt, sub)),
                ('Remove-Item fwd', lambda sub: ps('Remove-Item .PM*/local.json', self.w.wt, sub))):
            self.assert_config(payload, name)

    def test_a_question_mark_in_a_folder_part_names_the_config_folder(self):
        for name, payload in (
                ('rm fwd', lambda sub: bash('rm .pm-k?t/local.json', self.w.wt, sub)),
                ('rm back', lambda sub: bash("rm '.pm-?it\\local.json'", self.w.wt, sub)),
                ('Remove-Item back', lambda sub: ps('Remove-Item .pm-k?t\\local.json', self.w.wt, sub)),
                ('Set-Content fwd', lambda sub: ps("Set-Content .p?-kit/config.json '{}'", self.w.wt, sub))):
            self.assert_config(payload, name)

    def test_a_folder_wildcard_reaches_the_pointer_folder(self):
        self.pointer = self.w.plain
        plain = W.fwd(self.w.plain)
        self.assert_config(lambda sub: bash('rm %s/.pm-k*/config.json' % plain, self.w.plain, sub), 'rm')
        self.assert_config(lambda sub: ps('Remove-Item "%s\\.pm-?it\\config.json"' % self.w.plain,
                                          self.w.plain, sub), 'Remove-Item')

    def test_a_last_part_wildcard_that_matches_the_config_folder_names_it(self):
        self.assert_config(lambda sub: bash('rm -rf .pm-k*', self.w.wt, sub), 'rm -rf')
        self.assert_config(lambda sub: ps('Remove-Item -Recurse .pm-?it', self.w.wt, sub), 'Remove-Item')

    def test_a_folder_wildcard_that_cannot_match_a_config_folder_stays_open(self):
        for name, payload in (
                ('other folder', lambda sub: bash('rm .cl*de/settings.json', self.w.wt, sub)),
                ('other folder ?', lambda sub: ps('Remove-Item .cl?ude\\settings.json', self.w.wt, sub)),
                ('no such folder', lambda sub: bash('rm .pm-x*/config.json', self.w.wt, sub)),
                ('deeper', lambda sub: bash('rm sub*/.pm-kit/config.json', self.w.wt, sub)),
                ('not the folder', lambda sub: bash('rm .pm-kit*.json', self.w.wt, sub))):
            self.assert_not_config(payload, name)

    def test_a_folder_wildcard_names_only_a_path_that_exists(self):
        # every worktree of the PM repository holds a .pm-kit at its top: a folder wildcard that
        # reaches it names nothing there unless the joined path exists
        for name, payload in (
                ('rm -rf */__pycache__', lambda sub: bash('rm -rf */__pycache__', self.w.wt, sub)),
                ('rm */*.log', lambda sub: bash('rm */*.log', self.w.wt, sub)),
                ('Remove-Item */bin', lambda sub: ps('Remove-Item */bin -Recurse', self.w.wt, sub)),
                ('Remove-Item *\\new.json', lambda sub: ps('Remove-Item *\\new.json', self.w.wt, sub))):
            self.assert_not_config(payload, name)
        self.assert_config(lambda sub: ps('Remove-Item */config.json', self.w.wt, sub), 'Remove-Item */config.json')

    def test_a_bash_leading_wildcard_skips_dot_names_and_powershell_does_not(self):
        for name, payload in (
                ('rm */config.json', lambda sub: bash('rm */config.json', self.w.wt, sub)),
                ('rm -rf *', lambda sub: bash('rm -rf *', self.w.wt, sub)),
                ('rm ?pm-kit/config.json', lambda sub: bash('rm ?pm-kit/config.json', self.w.wt, sub))):
            self.assert_not_config(payload, name)
        for name, payload in (
                ('rm .*/config.json', lambda sub: bash('rm .*/config.json', self.w.wt, sub)),
                ('Remove-Item *', lambda sub: ps('Remove-Item -Recurse *', self.w.wt, sub)),
                ('Remove-Item ?pm-kit', lambda sub: ps('Remove-Item ?pm-kit\\config.json', self.w.wt, sub))):
            self.assert_config(payload, name)

    def test_a_bash_caret_set_is_negated(self):
        self.assert_config(lambda sub: bash('rm .pm-[^x]it/config.json', self.w.wt, sub), '[^x]')
        self.assert_not_config(lambda sub: bash('rm .pm-[^k]it/config.json', self.w.wt, sub), '[^k]')

    def test_a_walk_past_the_limit_is_refused(self):
        for name in ('a', 'b', 'c'):
            os.makedirs(os.path.join(self.w.wt, name))
        with mock.patch.object(delegfile, 'WALK_LIMIT', 3):
            self.assert_config(lambda sub: bash('rm */x/*.log', self.w.wt, sub), 'past the limit')
            d = self.decide(bash('rm */x/*.log', self.w.wt, True), {})
            self.assertIn('stopped resolving after 3 names', d.reason)
        self.assert_not_config(lambda sub: bash('rm */x/*.log', self.w.wt, sub), 'under the limit')


class FolderWildcardShortNames(unittest.TestCase):
    """A folder wildcard that matches only the 8.3 short name of a checkout's .pm-kit names it
    (delegfile.glob_config_below through rules.glob_part). Run on a volume that gives short
    names (world.short_name_root); skipped when no temp folder has them."""

    def setUp(self):
        root = W.short_name_root()
        if root is None:
            self.skipTest('no temp folder\'s volume gives short (8.3) names')
        self.w = W.World(root=root)
        self.addCleanup(self.w.cleanup)
        folder = os.path.join(self.w.wt, '.pm-kit')
        os.makedirs(folder)
        with open(os.path.join(folder, 'config.json'), 'w', encoding='utf-8') as fh:
            fh.write('{}\n')
        short = W.short_name(folder)
        if not short:
            self.skipTest('the volume gave .pm-kit no short (8.3) name')
        self.short = os.path.basename(short)

    def test_a_short_name_folder_wildcard_names_the_config_folder(self):
        pattern = self.short[:-1] + '?'
        self.assertNotEqual(pattern.lower()[:1], '.')
        for payload in (bash('rm %s/config.json' % pattern, self.w.wt, True),
                        ps('Remove-Item %s\\config.json' % pattern, self.w.wt, True)):
            with self.subTest(payload['tool_input']):
                d = decide(payload, self.w.ctx(environ={}, pointer=lambda: None))
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', FolderWildcards.CONFIG), d.reason)


class Matchers(unittest.TestCase):
    """delegfile's inline-code pattern: what it reads as a write of a file."""

    WRITES = ("open(p, 'w')", 'open(p, "a+")', "open(p, mode='wb')", "open(os.path.join(a, b), 'x')",
              'p.write_text(s)', 'p.write_bytes(b)', 'os.remove(p)', 'os.unlink(p)', 'os.rename(a, b)',
              'os.replace(a, b)', 'p.unlink()', 'p.rename(q)', 'p.replace(q)', 'p.touch()',
              'shutil.copy(a, p)', 'shutil.rmtree(d)', "fs.writeFileSync(p, '{}')", 'fs.unlinkSync(p)',
              'fs.rmSync(d, {recursive: true})', 'fs.appendFile(p, s, cb)', 'fs.copyFileSync(a, p)')
    READS = ("open(p)", "open(p, 'r')", "open(p, 'rb')", "open(p, encoding='utf-8')", "open(p, encoding='ascii')",
             "json.load(open(p, errors='replace'))", "s.replace('a', 'b')", 'p.read_text()', 'fs.readFileSync(p)',
             'os.path.exists(p)')

    def test_writes(self):
        for code in self.WRITES:
            self.assertIsNotNone(delegfile.WRITE_RE.search(code), code)

    def test_reads(self):
        for code in self.READS:
            self.assertIsNone(delegfile.WRITE_RE.search(code), code)


if __name__ == '__main__':
    unittest.main()
