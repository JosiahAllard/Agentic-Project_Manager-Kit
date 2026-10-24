"""kit.py: run a kit tool of the plugin export the machine uses now (kitlib.kitcmd).

  kit.py TOOL [ARGUMENTS...]     runs <export>/tools/TOOL.py ARGUMENTS... in this directory
  kit.py [-h | --help]           lists the tools

The export is the folder <plugin.export_dir>/.claude-plugin/marketplace.json names, plugin.export_dir
read from the machine config at each run, so a refreshed export is used at once. ARGUMENTS reach
the tool unchanged (-C among them): kit takes no options of its own. Windows only.

A thin entry point (ARCHITECTURE.md, Layers): kitlib.kitcmd resolves, checks and runs. Exit codes:
the tool's own; 2 a usage error or a refusal (not Windows, an unknown tool name, kit itself, a
machine config that cannot be read, no current export, an export folder that is not the configured
one), each one `kit: ...` line on stderr; 130 Ctrl+C.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import kitcmd                                           # noqa: E402

USAGE = 'usage: kit TOOL [ARGUMENTS...]   (kit with no arguments lists the tools)'


def refuse(why):
    sys.stderr.write('kit: %s\n' % why)
    return kitcmd.REFUSED


def main(argv=None, platform=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if (platform or sys.platform) != 'win32':
        return refuse('the kit runs on Windows only')
    if argv and argv[0].startswith('-') and argv[0] not in ('-h', '--help'):
        return refuse('kit takes no options of its own; the tool name comes first (%s)' % USAGE)
    kit, why = kitcmd.resolve()
    if why:
        return refuse(why)
    have = kitcmd.commands(kit)
    if not argv or argv[0] in ('-h', '--help'):
        sys.stdout.write('%s\nkit folder: %s\ntools: %s\n' % (USAGE, kit, ' '.join(have)))
        return 0
    why = kitcmd.check_name(argv[0], have)
    if why:
        return refuse(why)
    try:
        return kitcmd.run(kit, argv[0], argv[1:])
    except OSError as e:
        return refuse('could not start %s: %s' % (kitcmd.argv(kit, argv[0], [])[1], e))


if __name__ == '__main__':
    try:
        code = main()
    except KeyboardInterrupt:               # before the tool started (once it runs, kitcmd.wait)
        code = kitcmd.INTERRUPTED
    sys.exit(code)
