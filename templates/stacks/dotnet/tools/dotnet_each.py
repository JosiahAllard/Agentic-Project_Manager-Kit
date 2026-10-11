"""One `dotnet build` or `dotnet test` per project, in order: the gate's .NET step command.

usage: python tools/dotnet_each.py build|test [<csproj> ...]

Each run carries the no-reuse flags (no node reuse, no shared compilation), so a gate run leaves no
build server behind. `test` adds --no-build, so run `build` first. With no project the verb runs once
at the working directory (the repository root, which must hold the solution). Building a test project
builds every project it references. Exit status: the first failing `dotnet`'s, 0 when all passed, 2 for
a bad verb.
"""
import subprocess
import sys

NO_REUSE = ['-nodeReuse:false', '-p:UseSharedCompilation=false']


def dotnet_each(verb, projects, run=subprocess.run):
    """One `dotnet <verb>` per project (or one at the root when there is none), in order, stopping
    at the first failure. Returns its exit status; 2 for a bad verb."""
    if verb not in ('build', 'test'):
        print('dotnet_each: the first argument is build or test')
        return 2
    for p in (projects or [None]):
        cmd = ['dotnet', verb, '--nologo'] + (['--no-build'] if verb == 'test' else []) \
            + ([p] if p else []) + NO_REUSE
        print('dotnet_each: %s' % ' '.join(cmd), flush=True)
        rc = run(cmd).returncode
        if rc != 0:
            print('dotnet_each: dotnet %s failed%s (exit %d)' % (verb, ' for ' + p if p else '', rc))
            return rc
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    return dotnet_each(argv[0] if argv else '', argv[1:])


if __name__ == '__main__':
    sys.exit(main())
