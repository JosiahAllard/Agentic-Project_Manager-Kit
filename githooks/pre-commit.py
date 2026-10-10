"""Python half of the kit's git pre-commit hook (docs/ledger.md "Pre-commit check"). githooks/pre-commit
runs it only when the repository's .kit.json text names pre_commit_check. It makes one call,
kitlib.githooks.precommit.main, which runs the kit's pre-commit checks in order (today only the
ledger check, kitlib.ledger.precommit). Kit code that cannot be imported refuses the commit."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if __name__ == '__main__':
    try:
        from kitlib.githooks import precommit
    except Exception as e:                       # fail closed: a hook that cannot load cannot judge
        sys.stderr.write('pre-commit: cannot load the kit hook code: %s: %s; commit refused\n'
                         % (type(e).__name__, e))
        sys.exit(1)
    code, message = precommit.main()
    if message:
        sys.stderr.write(message + '\n')
    sys.exit(code)
