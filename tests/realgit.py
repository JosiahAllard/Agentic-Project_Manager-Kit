"""The integration tier: test classes that run on real git (docs/test-tools.md "Fake git").

Every other kit test runs on the fake git (tests/__init__.py). A class goes here only for what
the fake does not model and says why: hooks, worktree locking, ref races, the git process
itself (timeouts, jobs, pipes), and the few commands the fake lacks (blame, reflog).

    @real_git('hooks: the fake runs no hooks')
    class PrePushTest(unittest.TestCase): ...

KIT_FAKE_GIT is 0 from the class's setUpClass to its last class cleanup, then restored, so the
class's own processes and the tools it starts all run real git."""
import os

REASONS = {}        # 'module.Class' -> why it needs real git


def real_git(reason):
    """Class decorator: run the class on real git; `reason` names what the fake lacks."""
    def mark(cls):
        REASONS['%s.%s' % (cls.__module__, cls.__qualname__)] = reason
        own = cls.__dict__.get('setUpClass')

        def setUpClass(klass):
            old = os.environ.get('KIT_FAKE_GIT')
            os.environ['KIT_FAKE_GIT'] = '0'
            klass.addClassCleanup(_restore, old)
            if own is not None:
                own.__func__(klass)
            else:
                super(cls, klass).setUpClass()
        cls.setUpClass = classmethod(setUpClass)
        return cls
    return mark


def _restore(old):
    if old is None:
        os.environ.pop('KIT_FAKE_GIT', None)
    else:
        os.environ['KIT_FAKE_GIT'] = old
