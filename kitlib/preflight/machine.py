"""The engines and UDP ports readings (`.kit.json` "preflight".engines / udp_ports), for
preflight's two lines and the board's one `machine:` line.

A failed tasklist or netstat is UNKNOWN, never a count of 0: a measurement nobody took.
"""
import subprocess

from kitlib.proc import spawn

TIMEOUT = 60


def run(argv, timeout=TIMEOUT):
    """(rc, stdout, stderr) text of a command; rc 1 with the reason when it could not start or
    did not finish within `timeout`. The one seam tests replace."""
    try:
        r = spawn.run(argv, capture_output=True, text=True, encoding='utf-8',
                      errors='replace', timeout=timeout)
        return r.returncode, r.stdout or '', (r.stderr or '').strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, '', str(e)


def engines(prefixes):
    """(names of running processes whose image name starts with one of `prefixes`, case-blind,
    None) or (None, why)."""
    rc, out, err = run(['tasklist'])
    if rc != 0:
        return None, err or 'exit %d' % rc
    want = tuple(p.lower() for p in prefixes)
    return [l.split()[0] for l in out.splitlines()
            if l.strip() and l.lower().startswith(want)], None


def udp_ports(lo, hi):
    """(sorted UDP ports in [lo, hi] in use, None) or (None, why), from `netstat -ano -p UDP`."""
    rc, out, err = run(['netstat', '-ano', '-p', 'UDP'])
    if rc != 0:
        return None, err or 'exit %d' % rc
    ports = set()
    for l in out.splitlines():
        cols = l.split()
        if len(cols) >= 2 and ':' in cols[1]:
            p = cols[1].rsplit(':', 1)[1]
            if p.isdigit() and lo <= int(p) <= hi:
                ports.add(int(p))
    return sorted(ports), None


def preflight_lines(s):
    """preflight's `engines` and `ports` lines for settings `s` (none when neither is set)."""
    out = []
    if s['engines']:
        procs, why = engines(s['engines'])
        if procs is None:
            out.append('engines   UNKNOWN - tasklist failed: %s' % why)
        else:
            out.append('engines   %d running%s' % (len(procs), (
                ' (%s) - light runs overlap, not a reason to wait; not yours? do not kill it, '
                'tell the coordinator' % ', '.join(sorted(set(procs)))) if procs else ''))
    if s['udp_ports']:
        lo, hi = s['udp_ports']
        ports, why = udp_ports(lo, hi)
        if ports is None:
            out.append('ports     UNKNOWN - netstat failed: %s' % why)
        else:
            out.append('ports     %d-%d in use: %s' % (lo, hi, ports or 'none'))
    return out


def board_line(s):
    """The board's `machine: engines running N | ports lo-hi in use [...]`, or None when
    `.kit.json` "preflight" sets neither."""
    parts = []
    if s['engines']:
        procs, _why = engines(s['engines'])
        parts.append('engines UNKNOWN (tasklist failed)' if procs is None
                     else 'engines running %d' % len(procs))
    if s['udp_ports']:
        lo, hi = s['udp_ports']
        ports, _why = udp_ports(lo, hi)
        parts.append('ports %d-%d UNKNOWN (netstat failed)' % (lo, hi) if ports is None
                     else 'ports %d-%d in use %s' % (lo, hi, ports or 'none'))
    return 'machine: ' + ' | '.join(parts) if parts else None
