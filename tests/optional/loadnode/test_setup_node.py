"""optional/loadnode/setup-node.sh, the one-time load-node setup the owner runs with sudo on an
Ubuntu 24.04 box. Nothing here runs it for real: the checks are shell functions the script defines
when sourced with PMKIT_SETUP_NODE_LIB=1, run under sh (Git Bash on Windows, in POSIX mode), and
the argument refusals run the script itself, which stops before it reads or changes anything."""
import os
import shutil
import subprocess
import tempfile
import unittest

from kitlib.proc import bash

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SCRIPT = os.path.join(ROOT, 'optional', 'loadnode', 'setup-node.sh').replace('\\', '/')
SHELL = bash.git_bash() if os.name == 'nt' else shutil.which('sh')
POSIX = ['--posix'] if os.name == 'nt' else []      # Git Bash's bash, as sh

BLOB = 'AAAAC3NzaC1lZDI1NTE5AAAAIM29PY5ZpchBl3wl01C85mrtPNEbhLNEeLxqHCdqz3+6'   # a throwaway key
KEY = 'ssh-ed25519 ' + BLOB + ' pmkit-main'
IP = '192.168.1.20'


@unittest.skipUnless(SHELL, 'no POSIX shell (Git Bash on Windows) to run setup-node.sh')
class SetupNodeSh(unittest.TestCase):

    def lib(self, call, *args):
        """Source the script as a library and run one of its functions: (exit code, stdout)."""
        env = dict(os.environ, PMKIT_SETUP_NODE_LIB='1')
        argv = [SHELL] + POSIX + ['-c', '. "$0"; ' + call + ' "$@"', SCRIPT] + list(args)
        r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout

    def ok(self, call, *args):
        return self.lib(call, *args)[0] == 0

    def test_syntax(self):
        r = subprocess.run([SHELL] + POSIX + ['-n', SCRIPT], capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stderr), (0, ''))

    def test_authorized_keys_line_is_exactly_the_template(self):
        code, out = self.lib('ak_line', IP, KEY)
        self.assertEqual(code, 0)
        self.assertEqual(out, 'from="192.168.1.20",command="/usr/bin/python3.14 -I -B '
                              '/opt/pm-kit/optional/loadnode/loadnode.py serve",restrict ' + KEY + '\n')

    def test_key_line_accepted_with_or_without_a_comment(self):
        self.assertEqual(len(BLOB), 68)
        self.assertTrue(self.ok('valid_key_line', KEY))
        self.assertTrue(self.ok('valid_key_line', 'ssh-ed25519 ' + BLOB))
        self.assertTrue(self.ok('valid_key_line', 'ssh-ed25519 ' + BLOB + ' user@main pc'))

    def test_key_line_refusals(self):
        for bad in ('',
                    'restrict ' + KEY,                                   # options before the key
                    'command="sh" ' + KEY,
                    'from="1.2.3.4" ' + KEY,
                    ' ' + KEY,
                    KEY + '\n',                                          # a trailing newline
                    KEY + '\nssh-ed25519 ' + BLOB + ' second',           # two lines
                    KEY.replace(' pmkit-main', '\tpmkit-main'),
                    KEY + '\r',
                    'ssh-ed25519  ' + BLOB,                              # two spaces
                    'ssh-ed25519 ' + BLOB + ' ',                         # trailing space
                    'ssh-ed25519 ' + BLOB + '  x',
                    'ssh-ed25519 ' + BLOB[:-1],                          # too short
                    'ssh-ed25519 ' + BLOB + 'A',                         # too long
                    'ssh-ed25519 ' + BLOB[:-2] + '=' + BLOB[-1],         # not base64
                    'ssh-rsa ' + BLOB,
                    'ssh-ed25519 AAAAB3NzaC1yc2EAAAADAQABAAABAQC' + BLOB[25:],  # another key's header
                    'ssh-ed25519 ' + BLOB + ' café'):               # not ASCII
            self.assertFalse(self.ok('valid_key_line', bad), repr(bad))

    def test_ipv4_checks(self):
        for good in ('192.168.1.20', '10.0.0.1', '172.30.128.1', '1.0.0.0', '223.255.255.254'):
            self.assertTrue(self.ok('main_ip_ok', good), good)
        for bad in ('', '192.168.1', '192.168.1.20.1', '192.168.01.20', '256.1.1.1', '1.2.3.4/24',
                    '192.168.1.', '.1.2.3', '1..2.3', 'a.b.c.d', '1.2.3.4 ', '-1.2.3.4',
                    '0.0.0.0', '127.0.0.1', '224.0.0.1', '255.255.255.255', '1234.1.1.1'):
            self.assertFalse(self.ok('main_ip_ok', bad), bad)

    def test_admin_sources(self):
        for good in ('192.168.1.7', '192.168.1.0/24', '10.0.0.0/8', '10.1.2.0/31'):
            self.assertTrue(self.ok('valid_src', good), good)
        for bad in ('192.168.1.0/7', '192.168.1.0/33', '192.168.1.0/024', '192.168.1.0/', '0.0.0.0/0',
                    '192.168.1.0/x', 'host',
                    # ufw stores /32 as the bare address and masks host bits: refuse both
                    '10.1.2.3/32', '192.168.1.7/24', '10.1.2.3/31', '10.0.0.1/8'):
            self.assertFalse(self.ok('valid_src', bad), bad)
        self.assertTrue(self.ok('ip_in_src', '192.168.1.77', '192.168.1.0/24'))
        self.assertTrue(self.ok('ip_in_src', '192.168.1.77', '192.168.1.77'))
        self.assertFalse(self.ok('ip_in_src', '192.168.2.77', '192.168.1.0/24'))
        self.assertFalse(self.ok('ip_in_src', '192.168.1.78', '192.168.1.77'))
        self.assertTrue(self.ok('ip_in_src', '10.200.3.4', '10.0.0.0/8'))

    def test_user_names(self):
        for good in ('kitvm', 'owner', '_svc', 'a-b_c9'):
            self.assertTrue(self.ok('valid_user', good), good)
        for bad in ('', 'Owner', '9lives', '-x', 'a b', 'a@b', 'x' * 33, 'own;er'):
            self.assertFalse(self.ok('valid_user', bad), bad)

    def os_release(self, text):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, 'os-release')
        with open(p, 'w', newline='\n') as f:
            f.write(text)
        return p.replace('\\', '/')

    def test_only_ubuntu_24_04(self):
        noble = self.os_release('PRETTY_NAME="Ubuntu 24.04.3 LTS"\nNAME="Ubuntu"\nVERSION_ID="24.04"\n'
                                'ID=ubuntu\nID_LIKE=debian\n')
        self.assertTrue(self.ok('os_release_ok', noble))
        for text in ('PRETTY_NAME="Ubuntu 26.04.1 LTS"\nVERSION_ID="26.04"\nID=ubuntu\n',
                     'PRETTY_NAME="Ubuntu 22.04.5 LTS"\nVERSION_ID="22.04"\nID=ubuntu\n',
                     'PRETTY_NAME="Debian GNU/Linux 12"\nVERSION_ID="12"\nID=debian\n',
                     'PRETTY_NAME="Mint"\nVERSION_ID="24.04"\nID=linuxmint\nID_LIKE="ubuntu debian"\n'):
            self.assertFalse(self.ok('os_release_ok', self.os_release(text)), text)

    def test_sshd_drop_in(self):
        code, out = self.lib('sshd_conf_text', 'pmnode owner')
        self.assertEqual(code, 0)
        lines = [l for l in out.splitlines() if not l.startswith('#')]
        self.assertEqual(lines, ['PasswordAuthentication no', 'KbdInteractiveAuthentication no',
                                 'PermitRootLogin no', 'AllowUsers pmnode owner', 'Match User pmnode',
                                 '\tAuthorizedKeysFile /etc/ssh/authorized_keys/%u',
                                 '\tAuthenticationMethods publickey', '\tPermitTTY no',
                                 '\tDisableForwarding yes'])

    def test_cgroup_delegation_drop_in(self):
        code, out = self.lib('delegate_conf_text')
        self.assertEqual(code, 0)
        lines = [l for l in out.splitlines() if not l.startswith('#')]
        self.assertEqual(lines, ['[Service]', 'Delegate=cpu cpuset io memory pids'])

    def delegate_step(self, have=None):
        """Run step_delegate on a dry run against a drop-in folder in a temp dir (holding `have`,
        if given, as the drop-in, reported root:root 644): (exit code, stdout, files after)."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        tmp = os.path.join(d, 'tmp')
        os.mkdir(tmp)
        conf_dir = os.path.join(d, 'user@.service.d')
        if have is not None:
            os.mkdir(conf_dir)
            with open(os.path.join(conf_dir, 'pmkit-delegate.conf'), 'w', newline='\n') as f:
                f.write(have)
        call = ('DRY=1; TMP="$1"; DELEGATE_DIR="$2"; DELEGATE_CONF="$2/pmkit-delegate.conf"; '
                'owner_mode() { echo "root:root 644"; }; step_delegate; echo "CHANGED=$CHANGED"; :')
        code, out = self.lib(call, tmp.replace('\\', '/'), conf_dir.replace('\\', '/'))
        files = sorted(os.listdir(conf_dir)) if os.path.isdir(conf_dir) else None
        return code, out, files

    def test_delegation_step_on_a_dry_run_changes_nothing_and_says_what_it_would(self):
        code, out, files = self.delegate_step()
        self.assertEqual(code, 0, out)
        self.assertIsNone(files)                     # the folder was not made
        self.assertIn('[h] would change: folder ', out)
        self.assertIn('/pmkit-delegate.conf: Delegate=cpu cpuset io memory pids', out)
        self.assertIn('would run: systemctl daemon-reload', out)
        self.assertIn('CHANGED=h\n', out)            # step h listed once

    def test_delegation_step_leaves_a_matching_drop_in_alone(self):
        _, text = self.lib('delegate_conf_text')
        code, out, files = self.delegate_step(have=text)
        self.assertEqual(code, 0, out)
        self.assertIn('[h] unchanged: ', out)
        self.assertNotIn('daemon-reload', out)
        self.assertIn('CHANGED=\n', out)
        self.assertEqual(files, ['pmkit-delegate.conf'])

    def test_delegation_step_replaces_another_drop_in(self):
        code, out, _ = self.delegate_step(have='[Service]\nDelegate=cpu memory pids\n')
        self.assertEqual(code, 0, out)
        self.assertIn('[h] would change: ', out)
        self.assertNotIn('folder', out)
        self.assertIn('would run: systemctl daemon-reload', out)

    def run_script(self, *args):
        r = subprocess.run([SHELL] + POSIX + [SCRIPT] + list(args), capture_output=True, text=True,
                           timeout=60)
        return r.returncode, r.stdout + r.stderr

    def refused(self, *args):
        code, out = self.run_script(*args)
        self.assertEqual(code, 2, out)
        self.assertIn('REFUSED - ', out)
        return out

    def test_argument_refusals(self):
        self.assertIn('give <main-ip>', self.refused())
        self.assertIn('give <main-ip>', self.refused(IP))
        self.assertIn('too many arguments', self.refused(IP, 'ssh-ed25519', BLOB))
        self.assertIn('not a usable IPv4', self.refused('192.168.1.020', KEY))
        self.assertIn('the key line must be exactly', self.refused(IP, 'restrict ' + KEY))
        self.assertIn('the key line must be exactly', self.refused(IP, KEY + '\nssh-ed25519 ' + BLOB))
        self.assertIn('unknown option --force', self.refused('--force', IP, KEY))
        self.assertIn('--allow-user Root is not', self.refused('--allow-user', 'Root', IP, KEY))
        self.assertIn('root and pmnode are not admin', self.refused('--allow-user', 'pmnode', IP, KEY))
        self.assertIn('--admin-src 10.0.0.0/4 is not', self.refused('--admin-src', '10.0.0.0/4', IP, KEY))
        self.assertIn("main's address already", self.refused('--admin-src', IP, IP, KEY))
        self.assertIn('--kit-src needs a path', self.refused(IP, KEY, '--kit-src'))

    def test_valid_arguments_still_refused_off_ubuntu_before_any_change(self):
        # Here (Windows, or a test box) the first box check stops it: no step runs.
        code, out = self.run_script('--dry-run', '--allow-user', 'owner', IP, KEY)
        self.assertEqual(code, 2, out)
        self.assertRegex(out, r'REFUSED - (no /etc/os-release|this is .*, not Ubuntu 24\.04|systemd|run it as root with sudo)')
        self.assertNotIn('[b]', out)


if __name__ == '__main__':
    unittest.main()
