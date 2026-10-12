"""Find repetitive agent work worth turning into a tool. Read-only; reads Claude Code transcripts.

usage: python ~/.claude/tools/usage/repetition_report.py <repo path> [--days 7] [--top 15] [--config-dir <dir> ...]

Reports, for that repo's sessions and subagents in the window:
  1. shell command shapes repeated most (paths, hashes and numbers normalised away) - with failure rate
  2. inline scripts (python -c / heredocs) grouped by what they touch - each group is a missing tool
  3. tool calls before the first edit (median, p75) - the startup cost the existing tools should be cutting
  4. adoption: how often each `tools/*.py` was called, next to the hand-rolled commands it replaces
The folders it reads are printed on the first line, so a report that missed an account says so.
A shape seen 30+ times, or an inline-script group seen 10+ times, is a tool candidate: add one line to the
repo's docs/ops/tool-requests.md. Old candidates that a tool now covers should show falling counts.
"""
import argparse
import collections
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _usage as U  # noqa: E402

INLINE = re.compile(r"python3? - <<|python3? -c |<<'?(?:PY|EOF)", re.I)
TOPICS = (('test results / xml', r'test-results|\.trx|junit|testcase'), ('json / fixtures', r'json\.load|\.json'),
          ('patch a file', r"\.replace\(|re\.sub\(|open\([^)]*['\"]w"), ('git data', r'\bgit\b|subprocess'),
          ('count / size', r'getsize|len\(|splitlines|count\('), ('docs scan', r'\.md\b'))
# Each repo tool, and a regex over normalised shell shapes for the hand-rolled calls it replaces.
# The gate pattern lists common build/test drivers; add your stack's own if it is missing.
REPLACED = {'preflight.py': r'git (status|rev-parse|worktree list|fetch|log)',
            'gate.py': r'gradlew|dotnet (build|test)|pytest|unittest|npm (test|run)|cargo (build|test)|mvn|test\.bat|godot',
            'commit.py': r'git (add|commit)', 'sync_base.py': r'git merge', 'codemap.py': r'\b(grep|rg|find|git grep)\b',
            'doc_index.py': r'\b(sed -n|cat|head|tail)\b.*\.md', 'review_pack.py': r'git (show|diff)',
            'backlog.py': r'docs/backlog', 'testlist.py': r'test-results|\.trx'}


def shape(cmd):
    c = cmd.strip().replace('"', '').replace("'", '')
    c = re.sub(r'^(cd|Set-Location)\s+\S+\s*(&&|;)\s*', '', c)
    c = re.sub(r'[A-Za-z]:[\\/][^\s|;&)]+|/[a-z]/[^\s|;&)]+|\.[\\/][^\s|;&)]+', '<path>', c)
    c = re.sub(r'\b[0-9a-f]{7,40}\b', '<sha>', c)
    c = re.sub(r'\b\d+\b', '<n>', c)
    words = c.split()
    if not words:
        return ''
    if words[0] == 'git':
        rest = [w for w in words[1:] if w not in ('-C', '<path>')]
        return 'git ' + ' '.join(rest[:2])
    return ' '.join(words[:3])[:60]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('repo')
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--top', type=int, default=15)
    ap.add_argument('--config-dir', action='append')
    a = ap.parse_args()
    key = re.sub(r'[^A-Za-z0-9]', '-', os.path.abspath(a.repo)).lower()
    # Every account config folder whose transcripts should count: pass --config-dir once per folder
    # when this machine runs more than one account, or the report silently covers only one of them.
    roots = a.config_dir or U.config_dirs(None)
    since = time.time() - a.days * 86400
    shapes, fails, inline, tools = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
    before_edit, files = [], 0
    for root in roots:
        for p in glob.glob(os.path.join(root, 'projects', '*', '**', '*.jsonl'), recursive=True):
            proj = os.path.relpath(p, os.path.join(root, 'projects')).split(os.sep)[0].lower()
            if not proj.startswith(key) or os.path.getmtime(p) < since:
                continue
            files += 1
            n, first_edit, pend = 0, None, {}
            for line in open(p, encoding='utf-8', errors='replace'):
                if '"tool_' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                m = o.get('message') or {}
                for c in (m.get('content') or []) if isinstance(m.get('content'), list) else []:
                    if not isinstance(c, dict):
                        continue
                    if c.get('type') == 'tool_use':
                        n += 1
                        if c.get('name') in ('Edit', 'Write') and first_edit is None:
                            first_edit = n
                        if c.get('name') in ('Bash', 'PowerShell'):
                            cmd = (c.get('input') or {}).get('command', '')
                            t = re.search(r'tools[\\/](\w+\.py)', cmd)
                            if t:
                                tools[t.group(1)] += 1
                            if INLINE.search(cmd):
                                low = cmd.lower()
                                inline[next((name for name, rx in TOPICS if re.search(rx, low)), 'other')] += 1
                                pend[c['id']] = 'inline script'
                            else:
                                s = shape(cmd)
                                if s:
                                    shapes[s] += 1
                                    pend[c['id']] = s
                    elif c.get('type') == 'tool_result' and c.get('is_error') and c.get('tool_use_id') in pend:
                        fails[pend[c['tool_use_id']]] += 1
            if first_edit:
                before_edit.append(first_edit)
    if not files:
        print('no transcripts for %s in the last %d days (looked for project folders starting %s)' % (a.repo, a.days, key))
        sys.exit(1)
    before_edit.sort()
    print('%s | last %d days | %d transcripts | read: %s' % (a.repo, a.days, files, ', '.join(roots)))
    if before_edit:
        print('tool calls before the first edit: median %d, p75 %d (n=%d)' % (
            before_edit[len(before_edit) // 2], before_edit[3 * len(before_edit) // 4], len(before_edit)))
    print('\n1. repeated shell shapes (count | failed | shape)   candidate at 30+')
    for s, c in shapes.most_common(a.top):
        print('  %5d | %3d%% | %s' % (c, 100 * fails[s] // c, s))
    print('\n2. inline scripts by what they touch   candidate at 10+   (failed %d%% overall)' % (
        100 * fails['inline script'] // max(1, sum(inline.values()))))
    for s, c in inline.most_common():
        print('  %5d | %s' % (c, s))
    print('\n3. tool adoption (calls to the tool | hand-rolled calls it should replace)')
    for t, rx in REPLACED.items():
        hand = sum(c for s, c in shapes.items() if re.search(rx, s))
        print('  %5d | %5d | tools/%s' % (tools[t], hand, t))
    other = {t: c for t, c in tools.items() if t not in REPLACED}
    if other:
        print('  other tools called: ' + ', '.join('%s %d' % (t, c) for t, c in sorted(other.items(), key=lambda x: -x[1])))


if __name__ == '__main__':
    main()
