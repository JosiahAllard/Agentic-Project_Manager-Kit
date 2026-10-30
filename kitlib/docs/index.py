"""Index and staleness checks for tracked markdown, and the doc index lookups (from the PM kit's
tools/doc_index.py; docs/doc-tools.md "doc_index").

Modes of `run` (one each; tools/doc_index.py's argparse refuses two together):
  index check (default)  every check below at `head`; 1 on a miss
  stale                  report only; no stale line ever fails it
  section                print one section, found by heading text, from any tracked .md
  find                   only the index entries that match; 1 and the keyword list when none does,
                         1 and where to search instead when the repository has no index (tools too)
  tools                  only the `## Tools` section of the index
  role                   every doc section that role reads; 2 on an unknown role

The index is the file `.kit.json` "doc_index".index names (default docs/INDEX.md). Its entries
are each a line holding " | ", shaped `path | what it holds | read when | kw: a, b`, in any of
these forms: a list item (`-`, `*` or `+` bullet, or a numbered `1.` / `1)` item, at any indent,
which may open with prose or an unbackticked path); a table data row (a table's `|---|` rule and
the header row above it are not entries); or a bare line opening with a backtick. A list item
without " | " is prose, never an entry. `keys:` is accepted for `kw:`; a table row's trailing `|`
after the keywords is allowed.

Index checks (`.kit.json` "doc_budget".shard_indexes {"<dir>": "<index file>"}):
  0. the index exists, and every tracked .md (but a "doc_budget" retired path) is named in
     backticks on an ENTRY line of it (listing_misses; moved here from doc_budget). The index
     file alone opts a repository in, `.kit.json` or not (docs/multi-host-plan.md decision 26):
     absent at `head` (and at `config_ref`), the check is skipped with one line saying so;
     "doc_index".enabled false skips it silently, index or not. With `config_ref` (doc_index.py
     --config-ref) every setting is read at that revision;
  1. every .md under a shard dir is linked from its index file;
  2. in a doc that has an index block (a heading whose text starts with "Index"), every
     other `## ` heading's title appears in that block;
  3. every index entry ends in a `| kw: a, b, c` field. Per line, so deleting entries is safe.
The docs are read at `head` by ONE `git cat-file --batch` (kitlib.git.batch.cat_file); a reader
that fails is DOC INDEX FAILED, never a check over empty texts.

find matching ("doc_index".find_match), case-insensitive either way:
  "keywords" (default) every query word starts some word of the entry's kw field or its
             backticked path; the title and read-when columns are not searched. Prints the hits
             under their `## ` section headings, then a count line.
  "text"     every query word is a substring of the whole entry line. Prints only the hit lines,
             an entry with a query word as an exact keyword first.

role ("doc_index"): "roles" {"<role>": ["<topic>" | "<file>#<heading>", ...]}, or "roles_doc" +
"roles_marker" to read the same map from a doc: the bullets after the line starting with
roles_marker, each `- <role>, <role>: `topic`, `topic` ...`. A topic resolves to its index entry
(an entry whose keywords hold the topic exactly first, else the first find hit); a .md entry
prints its quoted "section" or the doc's head, every one capped at "role_lines" (default 80)
with a pointer to the rest; any other entry prints its line.

Stale report ("doc_budget".state_docs, "stale_days"):
  a. a backticked repo path (path_refs) on a line of a state doc that no longer exists;
  b. a line of a state doc unchanged for N days (git blame), oldest first;
  c. a state doc whose blame failed, or that the `head` listing lacks, named as not swept and
     counted on the summary line, which then says INCOMPLETE.
Every mode but find/tools is a function of the `ls-tree` listing of `head`; an unread or empty
listing exits 2 before any mode can announce that emptiness as good news.
"""
import atexit
import collections
import fnmatch
import difflib
import os
import re
import time

from kitlib.config import ConfigError, load, load_at
from kitlib.docs import budget
from kitlib.git import batch, cmd
from kitlib.host import registry

INDEX = 'docs/INDEX.md'          # the schema default of "doc_index".index
# Every entry form (see the module docstring).
LIST_ENTRY_RE = re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+(?=\S)(?=.*\s\|\s)')
BARE_ENTRY_RE = re.compile(r'^`.*\s\|\s')
TABLE_ROW_RE = re.compile(r'^\s*\|')
TABLE_RULE_RE = re.compile(r'^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$')
KEYS_RE = re.compile(r'\|\s*(?:kw|keys):\s*(\S.*?)\s*\|?\s*$')
REF_EXTS = ('md', 'py', 'ps1', 'json', 'java', 'kt', 'kts', 'cs', 'razor', 'js', 'ts', 'go', 'rs',
            'yml', 'yaml', 'toml', 'txt', 'tscn', 'gd')
PATH_REF_RE = re.compile(r'`([\w./\\-]+\.(?:%s))(?::\d+)?`' % '|'.join(REF_EXTS))
WHERE = '.kit.json "doc_index"'
# The doc index is opt-in: what the check and the lookups say in a repository without one.
NO_INDEX_CHECK = ('doc_index: no %s in this repository, so the listing check is skipped (the doc '
                  'index is opt-in: add one to turn it on)')
NO_INDEX_LOOKUP = ('doc_index: this repository has no doc index (no %s). Search its docs instead '
                   '(git grep -n -i "<topic>" -- docs README.md), or read its README.')
BLAME_TIMEOUT = 120              # seconds; a long state doc's blame is slow, not hung
_readers = {}

Git = collections.namedtuple('Git', 'returncode stdout stderr')


def git(root, *args, timeout=cmd.GIT_TIMEOUT):
    """`git <args>` in `root` as Git(returncode, stdout text, stderr text); returncode None when
    git did not run (kitlib.git.cmd.call)."""
    r = cmd.call(list(args), root, timeout=timeout)
    return Git(r.rc, cmd.text(r.out), r.err)


def first_line(text):
    """The first line of a git error, as a string."""
    lines = (text or '').strip().splitlines()
    return lines[0] if lines else ''


def settings(conf):
    """The "doc_index" section of Config `conf` over its defaults, with the cross-key rules the
    schema cannot state. Raises ConfigError."""
    out = conf.section('doc_index')
    roles = out['roles']
    if roles is not None and not all(
            r.strip() and t and all(x.strip() for x in t) for r, t in roles.items()):
        raise ConfigError('%s.roles must map each role to a non-empty list of topics or '
                          '"<file>#<heading>" strings' % WHERE)
    if (out['roles_doc'] is None) != (out['roles_marker'] is None):
        raise ConfigError('%s: roles_doc and roles_marker go together' % WHERE)
    if roles is not None and out['roles_doc'] is not None:
        raise ConfigError('%s: set roles or roles_doc, not both (one home for the map)' % WHERE)
    return out


def _reader(root):
    cat = _readers.get(root)
    if cat is None:
        cat = _readers[root] = batch.CatFile(root)
    return cat


def show(root, ref, path):
    """`ref:path` as text, '' when there is no such blob. Every call for one root shares ONE
    long-lived `git cat-file --batch` (kitlib.git.batch.CatFile)."""
    return _reader(root).text(ref, path)


def reader_died(root):
    """Why `show`'s git reader for `root` died, or None: its reads since answered '' and any check
    over them means nothing."""
    cat = _readers.get(root)
    return cat.dead_reason if cat is not None else None


def close_readers():
    """Stop every `show` reader (atexit does it; a test whose repo is deleted calls it first)."""
    while _readers:
        _readers.popitem()[1].close()


atexit.register(close_readers)


def texts_at(root, ref, paths):
    """`(text, None)` where `text(path)` is each of `paths` at `ref` read by ONE
    `git cat-file --batch` ('' for a path git has no blob for; any other path through `show`),
    or `(None, why)` when the batch failed: '' for every doc would pass every check."""
    got, why = batch.cat_file(['%s:%s' % (ref, p) for p in paths], root)
    if got is None:
        return None, why
    texts = {}
    for p in paths:
        obj = got['%s:%s' % (ref, p)]
        texts[p] = cmd.text(obj[1]) if obj is not None and obj[0] == 'blob' else ''
    return (lambda p: texts[p] if p in texts else show(root, ref, p)), None


def path_refs(text):
    """Every backticked repo-relative path named in `text`, normalised to forward slashes. No `*`,
    `<` or `>` in the class, so a glob or a placeholder never matches. A path with a bare `..` or
    `...` SEGMENT names a file in shorthand, so it is dropped; the test is per segment, so
    `flakes..md` is a path like any other."""
    out = []
    for ref in PATH_REF_RE.findall(text):
        ref = ref.replace('\\', '/')
        if not any(seg in ('..', '...') for seg in ref.split('/')):
            out.append(ref)
    return out


def index_checks(files, cfg, text, index=INDEX):
    """The index check's misses. `cfg` is the "doc_budget" section; `text(path)` returns a file's
    content (a ref's blob, or the file on disk)."""
    bad = []
    for d, shard_index in cfg.get('shard_indexes', {}).items():
        body = text(shard_index)
        base = os.path.dirname(shard_index)
        for f in files:
            if f.startswith(d + '/') and f.endswith('.md'):
                rel = f[len(base) + 1:] if base else f
                if rel not in body:
                    bad.append('  %s: not linked from %s' % (f, shard_index))
    for f in files:
        if not f.endswith('.md'):
            continue
        lines = text(f).splitlines()
        heads = [(i, l) for i, l in enumerate(lines) if re.match(r'^#{1,3} ', l)]
        idx = [k for k, (i, l) in enumerate(heads) if re.match(r'^#{1,3} +index\b', l, re.I)]
        if not idx:
            continue
        k = idx[0]
        end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        block = '\n'.join(lines[heads[k][0]:end]).lower()
        for i, l in heads[k + 1:]:
            if l.startswith('## '):
                title = re.sub(r'[`*]', '', l[3:]).strip().lower()
                num = re.match(r'^(\d+[a-z]?(?:\.\d+)*)[.:)]?\s', title + ' ')
                if num and re.search(r'\|\s*%s\s*\|' % re.escape(num.group(1)), block):
                    continue                     # numbered section, indexed by its number
                # An index row may drop ONE trailing parenthetical, and only when a real stem is
                # left: a tiny stem would match anything.
                short = re.sub(r'\s*\([^()]*\)\s*$', '', title)
                plain = re.sub(r'[`*]', '', block)
                # a short stem must be a whole table cell or list label, not a word found somewhere
                as_cell = bool(short) and re.search(
                    r'(?m)^\s*(?:\||[-*]\s)\s*%s\s*(?:\||$)' % re.escape(short), plain)
                stem_ok = short != title and bool(short) and (as_cell or (len(short) >= 8 and short in plain))
                if title and title not in re.sub(r'[`*]', '', block) and not stem_ok:
                    bad.append('  %s: section "%s" is not in its index block' % (f, l[3:].strip()))
    if index in files:
        bad.extend(keyword_misses(text(index), index))
    return bad


def listing_misses(files, cfg, idx, index=INDEX):
    """The index's own misses: `index` absent at the ref (`idx` is its text, '' when absent), or a
    tracked .md that no ENTRY line names in backticks (a glob entry such as `docs/x/*.md` covers
    what it matches; a path in prose or a table header names nothing). A path that "doc_budget"
    "retired_paths" retires is doc_budget's failure, not a miss here. `cfg` is the "doc_budget"
    section. Pure."""
    if not idx:
        return ['  %s: the index is missing (no such file at the ref checked)' % index]
    listed = {p for _n, _s, l, _k in index_entries(idx) for p in re.findall(r'`([^`]+\.md)`', l)}
    retired = cfg.get('retired_paths', [])
    return ['  %s: not in %s (no entry line names it; add `path | what it holds | read when | '
            'kw: topic, topic`)' % (p, index)
            for p in files if p.endswith('.md')
            and not any(fnmatch.fnmatch(p, gl) for gl in retired)
            and not any(p == l or fnmatch.fnmatch(p, l) for l in listed)]


def is_entry(line, next_line=''):
    """True when `line` is an index entry in any form; `next_line` is the line after it, which
    marks a table's header row (the row above its `|---|` rule)."""
    if TABLE_ROW_RE.match(line):
        return not TABLE_RULE_RE.match(line) and not TABLE_RULE_RE.match(next_line)
    return bool(LIST_ENTRY_RE.match(line) or BARE_ENTRY_RE.match(line))


def index_entries(text):
    """(line number, section heading, line, keys list) for every entry line of the index."""
    out, section = [], ''
    lines = text.splitlines()
    for n, l in enumerate(lines, 1):
        if l.startswith('## '):
            section = l
        elif is_entry(l, lines[n] if n < len(lines) else ''):
            m = KEYS_RE.search(l)
            keys = [k.strip().lower() for k in m.group(1).split(',') if k.strip()] if m else []
            out.append((n, section, l, keys))
    return out


def keyword_misses(text, index=INDEX):
    """The one home of the "entry without keywords" failure."""
    return ['  %s:%d: entry has no keywords (end it with "| kw: a, b"; "keys:" also works): %s'
            % (index, n, l[:80]) for n, _s, l, keys in index_entries(text) if not keys]


def _words(s):
    return re.findall(r'[a-z0-9]+', s.lower())


def find_entries(text, query, match='keywords'):
    """The (n, section, line, keys) entries matching `query` (see the module docstring for the
    two `match` modes). "text" puts an entry with a query word as an exact keyword first."""
    if match == 'text':
        words = query.lower().split()
        hits = [e for e in index_entries(text) if words and all(w in e[2].lower() for w in words)]
        return sorted(hits, key=lambda e: not any(w in e[3] for w in words))
    want = _words(query)
    hits = []
    for n, section, l, keys in index_entries(text):
        paths = re.findall(r'`([^`]+)`', l.split(' | ')[0])
        pool = _words(' '.join(keys + paths))
        if want and all(any(w.startswith(q) for w in pool) for q in want):
            hits.append((n, section, l, keys))
    return hits


def known_keywords(text):
    return sorted({k for _n, _s, _l, keys in index_entries(text) for k in keys})


NEAREST = 10


def nearest_keywords(text, query, n=NEAREST):
    """The `n` index keywords most like `query` (difflib ratio, ties alphabetical)."""
    q = query.lower()
    ranked = sorted(known_keywords(text),
                    key=lambda k: (-difflib.SequenceMatcher(None, q, k.lower()).ratio(), k))
    return ranked[:n]


def read_index(root, index=INDEX):
    """The index from the working tree. Raises OSError: a missing index is reported as one, never
    as "no entry matches"."""
    with open(os.path.join(root, index), encoding='utf-8', errors='replace') as fh:
        return fh.read()


def print_find(text, query, out, match='keywords', index=INDEX):
    """Send the matches to `out`; 0 on a hit, 1 with the keyword list on none."""
    hits = find_entries(text, query, match)
    if not hits:
        out('no %s entry matches "%s". Nearest keywords:' % (index, query))
        out(', '.join(nearest_keywords(text, query)))
        return 1
    if match == 'text':
        out('\n'.join(l for _n, _s, l, _k in hits))
        return 0
    last = None
    for _n, section, l, _k in hits:
        if section != last:
            out(section)
            last = section
        out(l)
    out('%d entr%s match "%s"' % (len(hits), 'y' if len(hits) == 1 else 'ies', query))
    return 0


def tools_section(text):
    """The index's `## Tools` section as lines, heading included, to the next `## `."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if re.match(r'^## +Tools\b', l)), None)
    if start is None:
        return []
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith('## ')), len(lines))
    sec = lines[start:end]
    while sec and not sec[-1].strip():
        sec.pop()
    return sec


def print_tools(text, out, index=INDEX):
    sec = tools_section(text)
    if not sec:
        out('no "## Tools" section in %s' % index)
        return 1
    out('\n'.join(sec))
    return 0


def stale_report(root, cfg, files, days, out, ref='HEAD'):
    """`files` is the `ls-tree` listing of `ref`; every state doc is blamed at `ref`, the tree the
    listing came from, so the report reads one tree and names it."""
    now = time.time()
    tracked = set(files)
    rows, unread = [], []
    for doc in cfg.get('state_docs', []):
        if doc not in tracked:
            # Asked for and not read: named, and counted on the summary, never skipped in silence.
            unread.append('%s: not in the %s listing - NOT swept (uncommitted, moved or deleted? '
                          'fix "state_docs")' % (doc, ref))
            continue
        r = git(root, 'blame', '--line-porcelain', ref, '--', doc, timeout=BLAME_TIMEOUT)
        if r.returncode != 0:
            # A failed blame contributes no rows, so the report has to SAY it swept less.
            unread.append('%s: git blame failed (exit %s) - NOT swept: %s'
                          % (doc, r.returncode, first_line(r.stderr)))
            continue
        when, n = None, 0
        for l in r.stdout.splitlines():
            if l.startswith('committer-time '):
                when = int(l.split()[1])
            elif l.startswith('\t'):
                n += 1
                text = l[1:]
                if not text.strip() or text.startswith('#') or '|' not in text:
                    continue
                age = int((now - when) / 86400)
                for named in path_refs(text):
                    if '/' in named and named not in tracked:
                        rows.append((9999, '%s:%d missing ref `%s`' % (doc, n, named)))
                if age >= days:
                    rows.append((age, '%s:%d %d days | %s' % (doc, n, age, text[:110])))
    for _age, row in sorted(rows, reverse=True):
        out(row)
    for row in unread:
        out(row)
    # A caller may print only this line, so the incompleteness has to ride on it.
    out('STALE REPORT (%s as committed): %d lines to confirm, rewrite or delete.%s'
        % (ref, len(rows), '' if not unread else
           ' INCOMPLETE: %d state doc(s) could not be read (listed above).' % len(unread)))


def _read_lines(root, path):
    try:
        with open(os.path.join(root, path), encoding='utf-8', errors='replace') as fh:
            return fh.read().splitlines()
    except OSError:
        return None


def print_section(root, title, files, out, doc=None, limit=220, ref='HEAD', index=INDEX):
    """Send the section whose heading contains `title`, from the working tree, to `out`. True on
    a hit. Only the .md files in `files` (the listing of `ref`) are searched, and a miss says so."""
    want = title.lower()
    hits = []
    for f in files:
        if not f.endswith('.md') or (doc and f != doc.replace('\\', '/')):
            continue
        lines = _read_lines(root, f)
        for i, l in enumerate(lines or ()):
            m = re.match(r'^(#{1,4}) +(.*)$', l)
            if m and want in re.sub(r'[`*]', '', m.group(2)).lower():
                depth = len(m.group(1))
                end = next((j for j in range(i + 1, len(lines))
                            if re.match(r'^#{1,%d} ' % depth, lines[j])), len(lines))
                hits.append((f, i, end, lines))
    if not hits:
        if doc:
            one = doc.replace('\\', '/')
            searched = '%s as tracked at %s%s' % (one, ref, '' if one in files else
                                                  ' (it is not tracked there, so it was not read)')
        else:
            searched = ('the %d .md files tracked at %s, so an uncommitted new doc is not searched'
                        % (sum(1 for f in files if f.endswith('.md')), ref))
        out('no heading contains "%s": searched only %s. Try %s, or --section with fewer words.'
            % (title, searched, index))
        return False
    hosts = tuple(f + '/' for f in registry.host_folders())     # a host's own copies sort last
    hits.sort(key=lambda h: (h[0].startswith(hosts), len(h[3][h[1]]) - len(title)))
    if len(hits) > 1:
        out('%d headings match; showing the first. Narrow with --doc <file>:' % len(hits))
        for f, i, end, lines in hits[:12]:
            out('  %s:%d  %s  (%d lines)' % (f, i + 1, lines[i], end - i))
        out('')
    f, i, end, lines = hits[0]
    _print_span(f, lines, i, end, limit, out)
    return True


def _print_span(f, lines, i, end, limit, out):
    out('%s:%d-%d' % (f, i + 1, end))
    out('\n'.join(lines[i:min(end, i + limit)]))
    if end - i > limit:
        out('... %d more lines; read %s from line %d' % (end - i - limit, f, i + limit + 1))


def role_map(root, cfg):
    """{role: [lookup, ...]} from "roles", or parsed from "roles_doc" (raises ConfigError when
    that doc cannot be read, or has no marker line or no role bullet after it)."""
    if cfg['roles'] is not None:
        return {r.strip(): [t.strip() for t in ts] for r, ts in cfg['roles'].items()}
    if cfg['roles_doc'] is None:
        return {}
    doc, marker = cfg['roles_doc'].replace('\\', '/'), cfg['roles_marker'].strip()
    lines = _read_lines(root, doc)
    if lines is None:
        raise ConfigError('"doc_index".roles_doc %s cannot be read' % doc)
    start = next((i for i, l in enumerate(lines) if l.lstrip('#').strip().startswith(marker)), None)
    if start is None:
        raise ConfigError('"doc_index".roles_doc %s has no line starting "%s"' % (doc, marker))
    found = {}
    for l in lines[start + 1:]:
        m = re.match(r'^[-*]\s+([^:`]+):(.*)$', l)
        if not m:
            if found or l.strip():
                break
            continue
        topics = re.findall(r'`([^`]+)`', m.group(2))
        for role in (r.strip() for r in m.group(1).split(',')):
            have = found.setdefault(role, []) if role and topics else []
            have.extend(t for t in dict.fromkeys(topics) if t not in have)
    if not found:
        raise ConfigError('"doc_index".roles_doc %s has no "- <role>: `topic`" bullet after "%s"'
                          % (doc, marker))
    return found


def resolve_topic(index_text, topic, match):
    """The index entry a role lookup names: one whose keywords hold `topic` exactly, first in
    index order, else the first find hit; None when nothing matches."""
    entries = index_entries(index_text)
    exact = [e for e in entries if topic.lower() in e[3]]
    if exact:
        return exact[0]
    hits = find_entries(index_text, topic, match)
    return hits[0] if hits else None


def print_role(root, role, cfg, files, out, ref='HEAD'):
    """Send every lookup of `role` to `out`; the exit status (see the module docstring). `cfg` is
    settings(). Raises ConfigError from role_map."""
    index = cfg['index']
    roles = role_map(root, cfg)
    if not roles:
        out('doc_index: no roles configured (.kit.json "doc_index": "roles", or "roles_doc" + '
            '"roles_marker").')
        return 2
    if role not in roles:
        out('doc_index: unknown role "%s". Known roles: %s' % (role, ', '.join(sorted(roles))))
        return 2
    limit, lookups = cfg['role_lines'], roles[role]
    out('ROLE %s: %s' % (role, ', '.join(lookups)))
    try:
        index_text = read_index(root, index)
    except OSError:
        index_text = None
    status = 0
    for item in lookups:
        out('')
        if '#' in item:
            path, heading = item.split('#', 1)
            out('=== %s' % item)
            if not print_section(root, heading.strip(), files, out, doc=path.strip(), limit=limit,
                                 ref=ref, index=index):
                status = 1
            continue
        entry = resolve_topic(index_text, item, cfg['find_match']) if index_text is not None else None
        if entry is None:
            out('=== %s -> no %s entry matches it%s' % (item, index, '' if index_text is not None
                                                        else ' (%s cannot be read: this '
                                                        'repository has no doc index)' % index))
            status = 1
            continue
        line = entry[2]
        head = line.split(' | ')[0]
        path = next(iter(re.findall(r'`([^`]+)`', head)), '')
        quoted = re.search(r'`\s+"([^"]+)"', head)
        out('=== %s -> %s' % (item, head.lstrip('-* ').strip()))
        if not path.endswith('.md') or path not in files:
            out(line)
        elif quoted:
            if not print_section(root, quoted.group(1), files, out, doc=path, limit=limit, ref=ref,
                                 index=index):
                status = 1
        else:
            lines = _read_lines(root, path) or []
            _print_span(path, lines, 0, len(lines), limit, out)
    return status


def has_index(root, index, files, config_ref=None):
    """True when the index file is in `files` (the listing of the head checked) or, with
    `config_ref`, at that revision: a branch cannot switch the listing check off by deleting the
    index its base has. A failed probe of the ref counts as present, so the check runs and fails
    ("the index is missing") rather than being skipped."""
    if index in files:
        return True
    if config_ref is None:
        return False
    r = git(root, 'ls-tree', '--name-only', config_ref, '--', index)
    return r.returncode != 0 or bool(r.stdout.strip())


def no_index(index, out):
    """A lookup in a repository with no doc index: say so and where to look instead; exit 1."""
    out(NO_INDEX_LOOKUP % index)
    return 1


def refuse(error, out):
    """A ConfigError the one way: one line, exit 2."""
    out('doc_index: refused: kit configuration: %s' % error)
    return 2


def run(root, out, section=None, find=None, tools=False, stale=False, role=None, doc=None,
        head='HEAD', days=None, config_ref=None):
    """One doc_index mode over the checkout `root`; the exit status. `find` is the query string.
    `config_ref` set: the whole configuration ("doc_index", "doc_budget" with its shard_indexes)
    is the one that revision holds, never the working tree's, so a landing check run on a branch
    reads the base's and the branch cannot switch its own check off. A git failure reading it is
    a refusal (2), never "no .kit.json". find and tools still read the index FILE from the working
    tree, and section/role read docs at `head`; only the settings come from `config_ref`.
    0 pass, 1 a miss (index check, find, role topic), 2 refused (config, unread index, listing,
    unknown role)."""
    try:
        conf = load(root) if config_ref is None else load_at(root, config_ref)
        cfg = conf.section('doc_budget')
        di = settings(conf)
    except ConfigError as e:
        return refuse(e, out)
    index = di['index']
    if find is not None or tools:
        # Lookups read the index from the working tree, as section reads its docs; no listing.
        try:
            text = read_index(root, index)
        except FileNotFoundError:
            return no_index(index, out)
        except OSError as e:
            out('doc_index: cannot read %s: %s' % (index, e))
            return 2
        if tools:
            return print_tools(text, out, index)
        return print_find(text, find, out, di['find_match'], index)
    # EVERY mode below is a function of this listing, so an unread one is a run that measured
    # nothing, never a finding of "no problems".
    listing = git(root, 'ls-tree', '-r', '--name-only', '-z', head)
    # -z: raw NUL-separated paths; without it git C-quotes a non-ASCII, quoted or newline name
    files = budget.names_from(listing.stdout) if listing.returncode == 0 else []
    if listing.returncode != 0 or not files:
        out('doc_index: cannot list %s (git exit %s): %s' % (
            head, listing.returncode, first_line(listing.stderr) or 'no tracked files'))
        return 2
    if section is not None:
        print_section(root, section, files, out, doc, ref=head, index=index)
        return 0
    if role is not None:
        try:
            return print_role(root, role, di, files, out, ref=head)
        except ConfigError as e:
            return refuse(e, out)
    if stale:
        # `is None`, not `or`: --days 0 is an age of 0 days, and a 0 in the config is kept too.
        limit = days if days is not None else cfg.get('stale_days')
        stale_report(root, cfg, files, 14 if limit is None else limit, out, ref=head)
        return 0
    wanted = sorted({f for f in files if f.endswith('.md')} | set(cfg.get('shard_indexes', {}).values()))
    text, why = texts_at(root, head, wanted)
    if text is None:
        out('DOC INDEX FAILED: the git reader died, so the docs were not read: %s' % why)
        return 1
    bad = index_checks(files, cfg, text, index)
    # The doc index is opt-in (docs/multi-host-plan.md decision 26): the index file is the opt-in,
    # with or without a .kit.json; "doc_index".enabled false is the explicit opt-out.
    if di['enabled'] and not has_index(root, index, files, config_ref):
        out(NO_INDEX_CHECK % index)
    elif di['enabled']:
        bad = listing_misses(files, cfg, text(index), index) + bad
    died = reader_died(root)
    if died:
        out('DOC INDEX FAILED: the git reader died, so the docs were not read: %s' % died)
        return 1
    if bad:
        out('DOC INDEX FAILED:')
        out('\n'.join(bad))
        out('Fix: add the index line in the same commit as the shard or section.')
        return 1
    # Names the tree it checked: the committed head, never the files on disk. "DOC INDEX OK"
    # stays the line's prefix; callers match it as a substring.
    out('DOC INDEX OK (%s as committed, %d .md files)'
        % (head, sum(1 for f in files if f.endswith('.md'))))
    return 0
