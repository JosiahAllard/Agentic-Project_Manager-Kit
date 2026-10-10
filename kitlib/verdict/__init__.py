"""One judgement rule for a tool that runs another tool and prints its verdict (from the PM
kit's tools/verdict.py).

usage: `from kitlib import verdict` then `verdict.judge(rc, text)`

THE RULE IS CONJUNCTIVE - all three legs, per run:
  1. the exit status is 0, AND
  2. the expected pass line is present, AND
  3. no fail line is present.
Each leg alone is a way to print green over red. Judging on the text alone reads green when a
tool prints its pass string and exits 1, or when its failure path ends in advice prose (every
failure path of doc_budget.py does, so the tail of a red run reads exactly like a green one).
Judging on the status alone reads green when a tool is replaced by a stub or its output never
arrives. Green has to be asserted, not inferred from the absence of red.

A judgement that CANNOT use one of the legs is not a reason to drop the rule quietly: keep the
legs that work, and say at the site, in the output a reader sees, which leg is missing and why.

`git` has no verdict string, so a caller reading git judges on the exit status alone (kitlib.git
returns `(None, why)` for a failed call); that is not this module's case.

A COMPARATOR (a tool that reads two or more runs, files or outputs and reports whether they
agree) gets its verdict from `compared(count, differences)`, never from the absence of a
difference: "no differences found" over an empty set is not agreement (a comparator once printed
"bit-identical across 3 runs" having compared zero files). It counts the comparisons it MADE and
exits with what `compared` returns: EXIT_SAME only when the count is above zero and nothing
differed, EXIT_DIFFERENT when something did, EXIT_NOTHING_COMPARED (4) when nothing was examined -
never 0 or 1, and distinct from 2 (usage) and 3 (the gate's STUCK). `judge` treats it,
like every non-zero status, as a failure, and says why.
"""

DOC_BUDGET_OK = 'DOC BUDGET OK'
DOC_BUDGET_FAIL = 'DOC BUDGET FAILED'

EXIT_SAME = 0
EXIT_DIFFERENT = 1
EXIT_NOTHING_COMPARED = 4
NOTHING_COMPARED = 'NOTHING COMPARED'


def compared(count, differences, what='comparisons'):
    """(exit status, verdict line) for a comparator that made `count` comparisons and found
    `differences` of them unequal. Pure.

    `count` is the comparisons actually made - a file that was missing, unreadable or skipped
    is not one. Zero gives EXIT_NOTHING_COMPARED and a NOTHING COMPARED line whatever
    `differences` says. A negative count, a negative difference count, or more differences than
    comparisons is a bug in the caller's tally and raises ValueError.
    """
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError('count must be a non-negative int, got %r' % (count,))
    if isinstance(differences, bool) or not isinstance(differences, int) or differences < 0:
        raise ValueError('differences must be a non-negative int, got %r' % (differences,))
    if differences > count:
        raise ValueError('%d differences in %d %s' % (differences, count, what))
    if count == 0:
        return EXIT_NOTHING_COMPARED, ('%s: 0 %s made - not agreement, nothing was examined'
                                       % (NOTHING_COMPARED, what))
    if differences:
        return EXIT_DIFFERENT, 'DIFFERENT: %d of %d %s differ' % (differences, count, what)
    return EXIT_SAME, 'SAME: %d of %d %s agree' % (count, count, what)


def judge(returncode, text, ok=DOC_BUDGET_OK, fail=DOC_BUDGET_FAIL):
    """(passed, reason). `passed` is True only when all three legs hold; `reason` is '' then,
    and otherwise names EVERY leg that failed, so a caller prints why rather than just that.

    Pure: no subprocess, no filesystem. `text` is the sub-tool's whole output, stdout and stderr
    together - argparse errors and tracebacks go to stderr, and a caller that captured only
    stdout would see an empty string and could satisfy leg 3 by accident.
    """
    text = text or ''
    why = []
    if returncode == EXIT_NOTHING_COMPARED:
        why.append('exit %d (nothing compared)' % returncode)
    elif returncode != 0:
        why.append('exit %d' % returncode)
    if ok not in text:
        why.append('no %r line in its output' % ok)
    if fail in text:
        why.append('%r in its output' % fail)
    return (not why), ', '.join(why)


def line(label, returncode, text, width=9, keep=1, from_start=False,
         ok=DOC_BUDGET_OK, fail=DOC_BUDGET_FAIL):
    """The one line a caller prints for a sub-tool run: `<label> [FAILED (<why>)] <quoted>`.

    `keep` is how many of the sub-tool's own non-blank lines to quote after the verdict - the
    last ones, or the first ones with `from_start` (doc_budget names what is wrong at the top of
    a failure and gives advice at the bottom). They are a GUIDE, never the verdict, which is why
    the verdict is written first and comes from judge(), not from the text being quoted.
    """
    passed, why = judge(returncode, text, ok, fail)
    lines = [l.strip() for l in (text or '').strip().splitlines() if l.strip()]
    quoted = ' / '.join(lines[:keep] if from_start else lines[-keep:]) if lines else 'no output'
    return '%-*s %s%s' % (width, label, '' if passed else 'FAILED (%s) ' % why, quoted)
