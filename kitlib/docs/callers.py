"""What the kit tools that run the doc tools share for the doc index check: preflight, commit and
review_pack run `<kit>/tools/doc_index.py -C <repo>` (default mode) next to doc_budget, as they
once got the index checks through doc_budget (docs/doc-tools.md "doc_index"). Pure helpers; each
caller does its own spawn, as it does for doc_budget."""
import os

from kitlib import verdict
from kitlib.config import ConfigError

OK = 'DOC INDEX OK'
FAIL = 'DOC INDEX FAILED'
LABEL = 'doc index'
TIMEOUT = 600     # seconds


def tool(kit_root):
    """The kit's tools/doc_index.py (it may not exist)."""
    return os.path.join(kit_root, 'tools', 'doc_index.py')


def switched_off(conf):
    """True when Config `conf` sets "doc_index".enabled false: the caller runs nothing and prints
    nothing, as doc_budget did. A bad "doc_index" section is not off: the tool reports it."""
    try:
        return not conf.section('doc_index')['enabled']
    except ConfigError:
        return False


def not_checked():
    return '%-9s NOT CHECKED - no tools/doc_index.py in the kit, so nothing was measured' % LABEL


def line(rc, text, width=9, keep=4):
    """`(line, failed)` for one doc_index run: judged conjunctively (kitlib.verdict), the first
    `keep` output lines quoted since a failure names its misses first."""
    return (verdict.line(LABEL, rc, text, width=width, keep=keep, from_start=True, ok=OK, fail=FAIL),
            not verdict.judge(rc, text, OK, FAIL)[0])
