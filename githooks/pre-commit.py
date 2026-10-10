"""Python half of the kit's git pre-commit hook (docs/ledger.md "Pre-commit check"). githooks/pre-commit
runs it only when the repository's .kit.json text names pre_commit_check; it runs
tools/ledger.py check --base HEAD when .kit.json ledger.pre_commit_check is true and refuses the
commit when that fails. Logic: kitlib.ledger.precommit."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.ledger import precommit                                # noqa: E402

if __name__ == '__main__':
    code, message = precommit.main()
    if message:
        sys.stderr.write(message + '\n')
    sys.exit(code)
