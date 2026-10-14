"""Every kit command a skill or role shows exists, with the flags it shows (docs/multi-host-plan.md
section 5, form C; docs/content.md).

For each kit tool command in content/skills/**.md, content/roles/*.md and
content/default-profile.md: it is written `python <kit>/tools/<name>.py`, never a bare
`python tools/<name>.py` or another folder's copy (a repository holds no stubs: decision 21 in
docs/multi-host-plan.md section 7); the kit has tools/<name>.py; the command shows `-C` when that
tool's parser defines it, so it names the checkout it acts on and runs the same from any
directory; and every `-x` / `--flag` the command shows is an option string that tool's parser
defines. The tools are never run: their source is parsed (ast) for the option
strings given to `add_argument`, including flags a loop builds as `'--' + name` over a literal or
an imported list; a tool that parses its arguments by hand (no `add_argument`) is read for every
string constant shaped like a flag. A command is read from its code span or fenced block, or in
prose up to the next `,;()`; arguments after a bare `--` belong to the child command and are not
checked. Flags are not checked against the subcommand: the union of the tool's options is used.
"""
import ast
import importlib
import os
import re
import unittest

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS = os.path.join(KIT_ROOT, 'tools')

CMD = re.compile(r'python3?\s+"?([^\s`"]*?)tools/(\w+)\.py\b"?(.*)')
KIT_PREFIX = '<kit>/'
FLAG = re.compile(r'(?<![\w<>/.-])(--?[A-Za-z][\w-]*)')
FLAG_SHAPE = re.compile(r'^--?[A-Za-z][\w-]*$')
QUOTED = re.compile(r'"[^"]*"|\'[^\']*\'')
PROSE_END = re.compile(r'[,;()]')


def content_files(root=KIT_ROOT):
    out = []
    for base, sub in (('content', 'skills'), ('content', 'roles')):
        top = os.path.join(root, base, sub)
        for d, _, files in os.walk(top):
            out.extend(os.path.join(d, f) for f in files if f.endswith('.md'))
    out.append(os.path.join(root, 'content', 'default-profile.md'))    # the roles' fallback profile
    return sorted(out)


def segments(text):
    """(text, is_code) pieces: a fenced block's lines and each backtick span are code; the prose
    between them is not."""
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith('```'):
            fenced = not fenced
            continue
        if fenced:
            out.append((line, True))
            continue
        parts = line.split('`')
        for i, part in enumerate(parts):
            out.append((part, i % 2 == 1))
    return out


def commands(text):
    """[(tool name, the argument text shown)] for every kit tool command in `text`."""
    return [(name, args) for _, name, args in located(text)]


def located(text):
    """[(the folder written before `tools/`, tool name, the argument text shown)] for every kit
    tool command in `text`: `<kit>/` for the kit's own tool, '' for a bare `tools/<name>.py`."""
    found = []
    for seg, code in segments(text):
        for m in CMD.finditer(seg):
            args = m.group(3)
            if not code:
                args = PROSE_END.split(args, 1)[0]
            found.append((m.group(1), m.group(2), args))
    return found


def shown_flags(args):
    """The option strings an argument text shows, up to a bare `--`; quoted values are skipped."""
    args = QUOTED.sub('""', args)
    args = re.split(r'(?:^|\s)--(?=\s|$)', args, 1)[0]
    return FLAG.findall(args)


def _imported(tree, name):
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    return getattr(importlib.import_module(node.module), alias.name)
    raise LookupError(name)


def _loop_values(tree, node):
    it = node.iter
    if isinstance(it, (ast.Tuple, ast.List)) and all(isinstance(e, ast.Constant) for e in it.elts):
        return [e.value for e in it.elts]
    if isinstance(it, ast.Name):
        return list(_imported(tree, it.id))
    raise LookupError(ast.dump(it))


def _builds_flag(node):
    return any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add) and isinstance(n.left, ast.Constant)
               and n.left.value == '--' for n in ast.walk(node))


def _calls_add_argument(node):
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'add_argument'
               for n in ast.walk(node))


def option_strings(source):
    """Every option string the tool source accepts (see the module docstring)."""
    tree = ast.parse(source)
    opts, parsed = set(), False
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'add_argument':
            parsed = True
            opts.update(a.value for a in n.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
                        and a.value.startswith('-'))
    for n in ast.walk(tree):
        if (isinstance(n, ast.For) and isinstance(n.target, ast.Name) and _calls_add_argument(n)
                and _builds_flag(n)):
            for v in _loop_values(tree, n):
                opts.update({'--' + v, '--' + v.replace('_', '-')})
    if not parsed:
        opts.update(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                    and isinstance(n.value, str) and FLAG_SHAPE.match(n.value))
    return opts


class OptionStringsTest(unittest.TestCase):
    def test_add_argument_loop_and_hand_parsed(self):
        src = ("import argparse\np = argparse.ArgumentParser()\np.add_argument('-C', dest='r')\n"
               "p.add_argument('--host', '--provider')\np.add_argument('paths', nargs='+')\n"
               "for f in ('q', 'owner_ask'):\n    p.add_argument('--' + f)\n")
        self.assertEqual(option_strings(src), {'-C', '--host', '--provider', '--q', '--owner_ask', '--owner-ask'})
        self.assertEqual(option_strings("import sys\nif sys.argv[1] == '--apply':\n    x = '-C'\ny = 'a-b'\n"),
                         {'--apply', '-C'})

    def test_a_loop_over_an_unknown_name_is_an_error_not_a_pass(self):
        with self.assertRaises(LookupError):
            option_strings("for f in SOMEWHERE:\n    p.add_argument('--' + f)\n")

    def test_commands_and_flags_read_from_text(self):
        text = ('Run `python tools/land.py <b> --expect-main <sha> [--wait <s>]`, then '
                'python <kit>/tools/machine_lock.py run --kind build -- tool --nope (prose),\n'
                '```\npython <kit>/tools/ledger.py add --q "x --not-a-flag"\n```\n')
        got = [(name, shown_flags(args)) for name, args in commands(text)]
        self.assertEqual(got, [('land', ['--expect-main', '--wait']), ('machine_lock', ['--kind']),
                               ('ledger', ['--q'])])

    def test_the_folder_written_before_tools_is_read(self):
        text = ('`python tools/gate.py -C <w>` and `python <kit>/tools/gate.py -C <w>`, '
                '`python "<kit>/tools/preflight.py" -C <w>`, `python <worktree>/tools/gate.py`')
        self.assertEqual([(where, name) for where, name, _ in located(text)],
                         [('', 'gate'), ('<kit>/', 'gate'), ('<kit>/', 'preflight'), ('<worktree>/', 'gate')])


class SkillCommandsTest(unittest.TestCase):
    def test_every_command_shown_exists_with_its_flags(self):
        cache, seen = {}, 0
        for path in content_files():
            with open(path, encoding='utf-8') as f:
                text = f.read()
            rel = os.path.relpath(path, KIT_ROOT).replace(os.sep, '/')
            for where, name, args in located(text):
                seen += 1
                tool = os.path.join(TOOLS, name + '.py')
                with self.subTest(file=rel, tool=name, args=args.strip()):
                    self.assertEqual(where, KIT_PREFIX, '%s runs %stools/%s.py: a repository holds no copy of '
                                     'the kit\'s tools, so write python <kit>/tools/%s.py' % (rel, where, name, name))
                    self.assertTrue(os.path.isfile(tool), '%s names tools/%s.py, which the kit lacks' % (rel, name))
                    if name not in cache:
                        with open(tool, encoding='utf-8') as f:
                            cache[name] = option_strings(f.read())
                    flags = shown_flags(args)
                    unknown = [fl for fl in flags if fl not in cache[name]]
                    self.assertEqual(unknown, [], '%s shows flags tools/%s.py does not accept' % (rel, name))
                    if '-C' in cache[name]:
                        self.assertIn('-C', flags, '%s runs tools/%s.py without -C: name the checkout it acts on '
                                      '(-C <worktree>)' % (rel, name))
        self.assertGreater(seen, 30, 'the scan found almost no commands: is it reading the files?')


if __name__ == '__main__':
    unittest.main()
