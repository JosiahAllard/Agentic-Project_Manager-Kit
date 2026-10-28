#!/bin/sh
# Sets this Linux box up for the kit, as one reviewed command (docs/machine-setup.md "Linux"). The
# Linux twin of installer/machine-setup.ps1, and a wrapper the same way: the work is
# tools/machine_setup.py. It writes nothing into any repository.
#
#   sh installer/machine-setup.sh [--owner-email EMAIL] [--pm-settings FILE] [--config-dir DIR]...
#                                 [--agent-temp DIR] [--no-sweep-task] [--shortcut] [--apply]
#   sudo sh installer/machine-setup.sh --pm-repo PATH [--apply]
#   sudo sh installer/machine-setup.sh --delegation [--apply]
#
# --pm-repo sets the PM-repo pointer, /etc/pm-kit/pointer, the first step on a new box (declining
# sudo, the dry run gives the line that writes the user's own pointer file instead). --delegation is
# the one root step that gives strong containment (S1): the systemd drop-in that delegates the
# cgroup controllers to every user's manager, and /etc/pm-kit/kit-user, the box's one kit user (the
# user who ran sudo); sign out and in once after it. Each is a mode of its own and takes only
# --python and --apply. The default mode runs as the kit user, never as root: the settings entries,
# the kit plugin per account, the owner's email, the agents' temp folder (--agent-temp: Linux has
# no default one; it must be outside your home folder and every checkout), the kit command
# ~/.local/bin/kit, and the box checks (WSL and a second kit user refused, a container warned of,
# git and tmux, the containment tier with the delegation step offered below S1).
#
# Dry run by default: prints every change it would make; --apply makes them. --home is for tests (a
# fake home folder). The tool runs under --python FILE when given, else under the python3 on PATH.
#
# Exit: the tool's own (0 done or nothing to do, 1 refused or stopped); 2 usage (an unknown option,
# an option without its value, --pm-repo empty, --pm-repo or --delegation with any option but
# --python and --apply, both of them, --python not a file, no python3 on PATH) or a refusal here
# (not Linux, no tools/machine_setup.py).
set -u

usage() { echo "machine-setup: usage - $1"; exit 2; }

kit=$(cd "$(dirname "$0")/.." && pwd) || usage 'cannot find the kit folder'
tool="$kit/tools/machine_setup.py"
[ "$(uname -s)" = Linux ] || { echo "machine-setup: REFUSED - this is the Linux installer (this is $(uname -s)); on Windows run installer\\machine-setup.ps1"; exit 2; }
[ -f "$tool" ] || { echo "machine-setup: REFUSED - the kit at $kit has no tools/machine_setup.py"; exit 2; }

python='' mode='' pm_repo='' apply='' other=''
# The arguments are read off the front by count (n left unread) and the kept ones put back at the
# end, so no marker word can collide with a value the user gave.
n=$#
while [ "$n" -gt 0 ]; do
    arg=$1; shift; n=$((n - 1))
    case $arg in
        --owner-email|--pm-settings|--config-dir|--agent-temp|--home)
            [ "$n" -gt 0 ] || usage "$arg needs a value"
            other="$other $arg"
            set -- "$@" "$arg" "$1"; shift; n=$((n - 1)) ;;
        --no-sweep-task|--shortcut)
            other="$other $arg"
            set -- "$@" "$arg" ;;
        --python)
            [ "$n" -gt 0 ] || usage '--python needs a value'
            python=$1; shift; n=$((n - 1)) ;;
        --pm-repo)
            [ "$n" -gt 0 ] || usage '--pm-repo needs the PM repository folder'
            [ -n "$(printf '%s' "$1" | tr -d ' \t')" ] || usage '--pm-repo needs the PM repository folder, not an empty value'
            [ -z "$mode" ] || usage "give --pm-repo or --delegation, not both"
            mode=--pm-repo pm_repo=$1; shift; n=$((n - 1)) ;;
        --delegation)
            [ -z "$mode" ] || usage "give --pm-repo or --delegation, not both"
            mode=--delegation ;;
        --apply)
            apply=--apply ;;
        *)
            usage "unknown option $arg" ;;
    esac
done                                    # the default mode's options remain
if [ -n "$mode" ] && [ -n "$other" ]; then
    usage "$mode is a mode of its own: it takes only --python and --apply (not$other)"
fi

if [ -n "$python" ]; then
    [ -f "$python" ] || usage "--python $python is not a file"
elif command -v python3 >/dev/null 2>&1; then
    python=python3
else
    usage 'no python3 on PATH'
fi

case $mode in
    --pm-repo) set -- --pm-repo "$pm_repo" ;;
    --delegation) set -- --delegation ;;
esac
[ -z "$apply" ] || set -- "$@" --apply
exec "$python" "$tool" --kit "$kit" "$@"
