"""Which ledger file holds what (docs/ledger.md "Files"), from the machine config.

With `ledger_dir` set the ledger is split: one file `<ledger_dir>/<repo>.md` per `ledger_repos`
name, ids `H-<repo>-nnnn`, and the standing digest `<ledger_dir>/STANDING.md`; the one-file
`ledger_path`, when set too, is the frozen ledger of the `R-nnnn` ids, read and never written.
Without `ledger_dir`, `ledger_path` is the one ledger, written as before.

Pure over the config dict (kitlib.config.user.user_config's), except `others`, which reads files.
"""
import collections
import os

from kitlib.config.user import files_of

from . import read, rulebook, standing
from .fmt import repo_of

# split: True with ledger_dir. repos: {repo: path} in ledger_repos order ({} unsplit). frozen: the
# ledger_path file or None. digest: the standing digest's path. rules: the rulebooks' folder
# (kitlib.ledger.rulebook), `rules` beside the log files.
Files = collections.namedtuple('Files', 'split repos frozen digest rules', defaults=(None,))


def of_config(cfg):
    """The Files of a machine config, or None when it sets neither ledger_dir nor ledger_path."""
    folder, frozen = cfg.get('ledger_dir'), cfg.get('ledger_path')
    if folder:
        return Files(True, files_of(cfg), frozen, os.path.join(folder, standing.NAME),
                     os.path.join(folder, rulebook.FOLDER))
    if frozen:
        return Files(False, {}, frozen, standing.path_beside(frozen),
                     os.path.join(os.path.dirname(os.path.abspath(frozen)), rulebook.FOLDER))
    return None


def path_of(files, rid):
    """The file that holds `rid`: an R-nnnn id is the one-file (or frozen) ledger's, an
    H-<repo>-nnnn id its repo's file; None when no configured file can hold it."""
    repo = repo_of(rid)
    if repo is None:
        return files.frozen
    return files.repos.get(repo)


def readable(files):
    """`[(repo or None, path)]`: the frozen (or one-file) ledger first, then each repo's file."""
    return ([(None, files.frozen)] if files.frozen else []) + list(files.repos.items())


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def others(files, path):
    """`(texts, whys)`: the text of every readable file but `path` that exists (LF), and one line
    for each that cannot be read. Where `under <id>` may point, and what may cite an entry."""
    texts, whys = [], []
    for _, p in readable(files):
        if _same(p, path) or not os.path.exists(p):
            continue
        text, why = read.read_file(p)
        if text is None:
            whys.append(why)
        else:
            texts.append(text.replace('\r\n', '\n'))
    return texts, whys
