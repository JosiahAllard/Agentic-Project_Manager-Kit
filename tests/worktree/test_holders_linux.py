"""Worktree holders on Linux (docs/linux-support-plan.md section 8, slice 9;
docs/worktree-remove.md): holders.blockers, cwd_holders and describe, and classify.process_cwds,
read from /proc (kitlib.platform.linux.paths.process_paths). blockers no longer fails open: a
process of this user using the tree is HELD, one of this user that cannot be read is UNREAD, and
no /proc (or an empty one, or one that does not list this process) is NOT_SEARCHED, each a refusal;
it is empty only when every process of this user was read and none uses the tree.

The classes but Live run on every platform against a fake /proc: a folder tree with one folder per
pid (comm, stat, status, fd/, maps, task/) whose links (cwd, root, exe, fd/*) come from a table
(paths._link) and whose folder owners come from another (paths._owner), holding this process
(paths._pid), with NAME patched to 'linux'. Live starts real
processes and runs on Linux only."""
import errno
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import kitlib.platform
from kitlib.platform.linux import paths
from kitlib.worktree import classify, holders
from tests.platforms import posix_only

ME, OTHER, ROOT = 1000, 1001, 0
SELF = 4242                     # the pid of the scan's own process (paths._pid)
TOP = os.path.abspath(os.sep)   # where it stands: / as this host spells it
MNT, OTHER_MNT = 'mnt:[4026531841]', 'mnt:[4026532999]'     # mount namespaces (ns/mnt links)
PROC_OPTIONS = 'rw,nosuid,nodev,noexec,relatime'
# the / of the fake mount table: the root of the drive the temp trees are on (/ on Linux)
FS_TOP = os.path.splitdrive(os.path.realpath(tempfile.gettempdir()))[0] + os.sep
DISK = '8:1'                    # the device of the file system mounted at FS_TOP


def escape(path):
    """`path` escaped as the kernel escapes a mountinfo path."""
    return path.replace('\\', '\\134').replace(' ', '\\040')


def inside(path):
    """Where `path` (under FS_TOP) is inside the file system mounted at FS_TOP: /a/b."""
    return '/' + os.path.relpath(path, FS_TOP).replace(os.sep, '/')


class FakeProc(object):
    """A fake /proc in a temp folder, holding this process (SELF) until clear(). add() makes one
    process; links and folder owners are tables, the real user is its status file. SELF's
    mountinfo shows the fake /proc mounted with mount() options (PROC_OPTIONS at first)."""

    def __init__(self, case):
        self.root = tempfile.mkdtemp(prefix='kit-fake-proc-')
        case.addCleanup(shutil.rmtree, self.root, True)
        self.links, self.owners = {}, {}
        for patch in (mock.patch.object(kitlib.platform, 'NAME', 'linux'),
                      mock.patch.object(paths, 'PROC', self.root),
                      mock.patch.object(paths, '_uid', lambda: ME),
                      mock.patch.object(paths, '_pid', lambda: SELF),
                      mock.patch.object(paths, '_owner', self.owner),
                      mock.patch.object(paths, '_link', self.link)):
            patch.start()
            case.addCleanup(patch.stop)
        self.reset()

    def clear(self):
        """No process at all, this one included."""
        shutil.rmtree(self.root)
        os.makedirs(self.root)
        self.links.clear()
        self.owners.clear()

    def reset(self):
        """Only this process, standing at TOP."""
        self.clear()
        self.add(SELF, cwd=TOP, comm='kit-test')
        self.mount(PROC_OPTIONS)

    def mount(self, options, fstype='proc', point=None, more=(), top=FS_TOP):
        """SELF's mountinfo: `top` (FS_TOP: / as the temp trees are spelt) on ext4, then `more`
        mounts (device, root inside its file system, mount point), then the fake /proc (or
        `point`) of `fstype` mounted with superblock `options` (a tuple: one mount each), every
        path escaped as the kernel escapes one."""
        point = self.root if point is None else point
        with open(os.path.join(self.root, str(SELF), 'mountinfo'), 'w', newline='\n') as f:
            f.write('21 1 %s / %s rw,relatime shared:1 - ext4 /dev/sda1 rw\n' % (DISK, escape(top)))
            for n, (device, root, where) in enumerate(more):
                f.write('%d 21 %s %s %s rw,relatime shared:1 - ext4 /dev/sdx rw\n'
                        % (30 + n, device, escape(root), escape(where)))
            for n, each in enumerate((options,) if isinstance(options, str) else options):
                f.write('%d 21 0:21 / %s rw,nosuid,nodev,noexec,relatime shared:12 - %s proc %s\n'
                        % (22 + n * 100, escape(point), fstype, each))

    def owner(self, folder):
        got = self.owners.get(folder)
        if got is None:
            raise FileNotFoundError(errno.ENOENT, 'no such process', folder)
        if isinstance(got, BaseException):
            raise got
        return got

    def link(self, path):
        got = self.links.get(path)
        if got is None:
            raise FileNotFoundError(errno.ENOENT, 'no such link', path)
        if isinstance(got, BaseException):
            raise got
        return got

    def _links(self, folder, cwd, root, exe, fds, maps):
        os.makedirs(os.path.join(folder, 'fd'))
        for kind, value in (('cwd', cwd), ('root', root), ('exe', exe)):
            if value is not None:
                self.links[os.path.join(folder, kind)] = value
        for n, value in enumerate(fds):
            open(os.path.join(folder, 'fd', str(n)), 'w').close()
            self.links[os.path.join(folder, 'fd', str(n))] = value
        with open(os.path.join(folder, 'maps'), 'w', newline='\n') as f:
            for path in maps:
                f.write('7f0000000000-7f0000001000 r--p 00000000 08:01 123      %s\n' % path)

    def add(self, pid, cwd='/', uid=ME, comm='python3', state='S', root='/', exe='/usr/bin/python3',
            fds=(), maps=(), owner=None, status=True, tasks=None, mnt=MNT):
        """Process `pid` of real user `uid` (its status; a tuple gives the real, effective, saved
        and file system users), whose /proc folder `owner` owns (the effective user when None;
        root for one the kernel marks not dumpable). status=False writes no Uid line.
        A link value of None leaves that link out (it reads ENOENT); an exception is raised when it
        is read. `mnt` is its mount namespace link. `tasks` {tid: {'cwd':, 'root':, 'exe':, 'fds':,
        'maps':}} are its other threads."""
        folder = os.path.join(self.root, str(pid))
        uids = (uid,) * 4 if isinstance(uid, int) else uid
        self._links(folder, cwd, root, exe, fds, maps)
        if mnt is not None:
            self.links[os.path.join(folder, 'ns', 'mnt')] = mnt
        with open(os.path.join(folder, 'comm'), 'w') as f:
            f.write(comm + '\n')
        with open(os.path.join(folder, 'stat'), 'w') as f:
            f.write('%d (%s) %s 1 1 1 0 -1' % (pid, comm, state))
        with open(os.path.join(folder, 'status'), 'w', newline='\n') as f:
            f.write('Name:\t%s\nState:\t%s\n' % (comm, state))
            if status:
                f.write('Uid:\t%d\t%d\t%d\t%d\n' % uids)
        self.owners[folder] = uids[1] if owner is None else owner
        os.makedirs(os.path.join(folder, 'task', str(pid)))
        for tid, links in (tasks or {}).items():
            self._links(os.path.join(folder, 'task', str(tid)), links.get('cwd', '/'),
                        links.get('root', '/'), links.get('exe', exe), links.get('fds', ()),
                        links.get('maps', ()))
            if links.get('mnt', MNT) is not None:
                self.links[os.path.join(folder, 'task', str(tid), 'ns', 'mnt')] = links.get('mnt', MNT)
        return folder


class Base(unittest.TestCase):
    def setUp(self):
        self.tree = tempfile.mkdtemp(prefix='kit-held-tree-')
        self.addCleanup(shutil.rmtree, self.tree, True)
        self.real = os.path.realpath(self.tree)
        self.sub = os.path.join(self.real, 'sub')
        os.makedirs(self.sub)
        self.proc = FakeProc(self)
        self.whole = os.path.abspath(self.tree)


class Blockers(Base):
    def test_nothing_using_the_tree_is_free(self):
        self.proc.add(10, cwd='/home/u', fds=['/dev/null', 'socket:[1]', 'pipe:[2]'])
        self.proc.add(11, cwd=self.real + '-other')       # a sibling sharing the prefix
        self.assertEqual(holders.blockers(self.tree), [])

    def test_a_current_folder_inside_is_held(self):
        self.proc.add(10, cwd=self.sub)
        self.assertEqual(holders.blockers(self.tree), [(self.sub, holders.HELD)])

    def test_the_root_itself_is_held(self):
        self.proc.add(10, cwd=self.real)
        self.assertEqual(holders.blockers(self.tree), [(self.real, holders.HELD)])

    def test_an_open_file_a_program_a_mapping_and_a_root_inside_are_held(self):
        inner = os.path.join(self.sub, 'f.txt')
        for pid, kwargs in ((10, {'fds': ['/dev/null', inner]}), (11, {'exe': inner}),
                            (12, {'maps': ['/usr/lib/libc.so.6', inner, '[heap]']}),
                            (13, {'root': self.sub})):
            with self.subTest(kwargs=kwargs):
                self.proc.reset()
                self.proc.add(pid, **kwargs)
                got = holders.blockers(self.tree)
                self.assertEqual(got, [(self.sub if 'root' in kwargs else inner, holders.HELD)])

    def test_a_removed_file_s_path_is_still_matched(self):
        self.proc.add(10, cwd=self.sub + paths.DELETED)
        self.assertEqual(holders.blockers(self.tree), [(self.sub, holders.HELD)])

    def test_each_path_is_named_once(self):
        self.proc.add(10, cwd=self.sub)
        self.proc.add(11, cwd=self.sub, fds=[self.sub])
        self.assertEqual(holders.blockers(self.tree), [(self.sub, holders.HELD)])

    def test_another_user_s_process_is_not_seen(self):
        self.proc.add(10, cwd=self.sub, uid=OTHER)
        self.assertEqual(holders.blockers(self.tree), [])

    def test_every_unreadable_case_of_this_user_refuses(self):
        denied = PermissionError(errno.EACCES, 'Permission denied')
        cases = {
            'cwd denied': lambda: self.proc.add(10, cwd=denied),
            'root denied': lambda: self.proc.add(10, root=denied),
            'exe denied': lambda: self.proc.add(10, exe=denied),
            'an fd denied': lambda: self.proc.add(10, fds=[denied]),
            'cwd missing while alive': lambda: self.proc.add(10, cwd=None),
            'fd list unreadable': lambda: self._replace(self.proc.add(10), 'fd', 'file'),
            'maps unreadable': lambda: self._replace(self.proc.add(10), 'maps', 'folder'),
            'not dumpable, its links denied': lambda: self.proc.add(10, owner=ROOT, cwd=denied),
            'no status, its folder unreadable': lambda: self._no_status(denied),
            'no status, its folder owned by root': lambda: self._no_status(ROOT),
            'no Uid line, its folder owned by root': lambda: self.proc.add(10, status=False, owner=ROOT),
            'a zombie leader whose thread is denied': lambda: self.proc.add(
                10, cwd=None, state='Z', tasks={11: {'cwd': denied}}),
            'a zombie leader whose task list is unreadable': lambda: self._replace(
                self.proc.add(10, cwd=None, state='Z'), 'task', 'file'),
            'a live leader whose thread cwd is denied': lambda: self.proc.add(
                10, tasks={11: {'cwd': denied}}),
            'a live leader whose thread root is denied': lambda: self.proc.add(
                10, tasks={11: {'root': denied}}),
            'a live leader whose task list is unreadable': lambda: self._replace(
                self.proc.add(10), 'task', 'file'),
            'a live leader whose thread is in another mount namespace': lambda: self.proc.add(
                10, tasks={11: {'mnt': OTHER_MNT}}),
            'a live leader whose thread namespace is denied': lambda: self.proc.add(
                10, tasks={11: {'mnt': denied}}),
            'a live leader whose thread namespace is missing': lambda: self.proc.add(
                10, tasks={11: {'mnt': None}}),
            'its mount namespace denied': lambda: self.proc.add(10, mnt=denied),
            'its mount namespace missing while alive': lambda: self.proc.add(10, mnt=None),
            'another mount namespace': lambda: self.proc.add(10, cwd='/workspaces/x', mnt=OTHER_MNT),
        }
        for name, make in cases.items():
            with self.subTest(name):
                self.proc.reset()
                make()
                self.assertEqual(holders.blockers(self.tree), [(self.whole, holders.UNREAD)])

    def test_an_unreadable_process_and_a_holder_are_both_named(self):
        self.proc.add(10, cwd=PermissionError(errno.EACCES, 'Permission denied'))
        self.proc.add(11, cwd=self.sub)
        self.assertEqual(holders.blockers(self.tree),
                         [(self.sub, holders.HELD), (self.whole, holders.UNREAD)])

    def test_a_process_that_ended_while_read_is_skipped(self):
        self.proc.add(10, cwd=None, state='Z')             # a zombie: its links read ENOENT
        gone = self.proc.add(11, cwd=None)                 # its folder went away
        shutil.rmtree(gone)
        self.proc.owners[os.path.join(self.proc.root, '12')] = None
        os.makedirs(os.path.join(self.proc.root, '12'))    # listed, then gone before its owner
        self.assertEqual(holders.blockers(self.tree), [])

    def test_a_zombie_leader_with_no_other_thread_has_ended(self):
        self.proc.add(10, cwd=None, state='Z', tasks={})
        self.assertEqual(holders.blockers(self.tree), [])

    def test_a_zombie_leader_s_running_thread_holds_the_tree(self):
        inner = os.path.join(self.sub, 'f.so')
        for links, held in (({'cwd': self.sub}, self.sub), ({'root': self.sub}, self.sub),
                            ({'fds': ['/dev/null', self.sub]}, self.sub), ({'exe': inner}, inner),
                            ({'maps': ['/usr/lib/libc.so.6', inner]}, inner)):
            with self.subTest(links=links):
                self.proc.reset()
                self.proc.add(10, cwd=None, state='Z', tasks={11: links})
                self.assertEqual(holders.blockers(self.tree), [(held, holders.HELD)])

    def test_a_zombie_leader_s_namespace_is_its_threads(self):
        # an exited leader has no ns/mnt link of its own
        self.proc.add(10, cwd=None, state='Z', mnt=None)                # no thread: ended
        self.proc.add(11, cwd=None, state='Z', mnt=None, tasks={12: {'cwd': self.sub}})
        self.assertEqual(holders.blockers(self.tree), [(self.sub, holders.HELD)])
        for links in ({'mnt': OTHER_MNT}, {'mnt': PermissionError(errno.EACCES, 'denied')}):
            with self.subTest(links=links):
                self.proc.reset()
                self.proc.add(11, cwd=None, state='Z', mnt=None, tasks={12: links})
                self.assertEqual(holders.blockers(self.tree), [(self.whole, holders.UNREAD)])

    def test_a_live_leader_s_thread_standing_in_the_tree_holds_it(self):
        # a thread that called unshare(CLONE_FS) and then chdir or chroot
        for links in ({'cwd': self.sub}, {'root': self.sub}):
            with self.subTest(links=links):
                self.proc.reset()
                self.proc.add(10, cwd='/home/u', tasks={11: links})
                self.assertEqual(holders.blockers(self.tree), [(self.sub, holders.HELD)])

    def test_a_process_in_another_mount_namespace_is_unread_not_free(self):
        # a devcontainer or rootless podman process of this user: its paths are another tree's
        self.proc.add(10, cwd='/workspaces/kit', fds=['/workspaces/kit/x'], mnt=OTHER_MNT)
        self.assertEqual(holders.blockers(self.tree), [(self.whole, holders.UNREAD)])

    def test_this_process_s_mount_namespace_unread_is_not_searched(self):
        self.proc.links[os.path.join(self.proc.root, str(SELF), 'ns', 'mnt')] = PermissionError(
            errno.EACCES, 'Permission denied')
        self.assertEqual(holders.blockers(self.tree), [(self.whole, holders.NOT_SEARCHED)])

    def test_a_proc_mounted_to_hide_processes_is_not_searched(self):
        for option in ('hidepid=2', 'hidepid=invisible', 'hidepid=4', 'hidepid=ptraceable',
                       'subset=pid'):
            with self.subTest(option=option):
                self.proc.mount('rw,%s' % option)
                self.assertEqual(holders.blockers(self.tree), [(self.whole, holders.NOT_SEARCHED)])

    def test_any_proc_mount_at_proc_that_hides_processes_is_not_searched(self):
        for options in (('rw,hidepid=2', 'rw,gid=10'), ('rw,gid=10', 'rw,subset=pid', 'rw')):
            with self.subTest(options=options):
                self.proc.mount(options)
                self.assertEqual(holders.blockers(self.tree), [(self.whole, holders.NOT_SEARCHED)])
        self.proc.mount(('rw,hidepid=1', 'rw,gid=10'))
        self.assertEqual(holders.blockers(self.tree), [])

    def test_a_tree_its_file_system_shows_at_another_path_too_is_unread_not_free(self):
        # a bind mount: a process standing in the other path holds the tree under a path that is
        # not under it, so the tree is not known to be free
        elsewhere = os.path.join(FS_TOP, 'srv-bind')
        cases = {
            'the whole file system again': [(DISK, '/', elsewhere)],
            'a folder above the tree': [(DISK, inside(os.path.dirname(self.real)), elsewhere)],
            'the tree itself': [(DISK, inside(self.real), elsewhere)],
            'a folder inside the tree': [(DISK, inside(self.sub), elsewhere)],
            'the tree on its own disk, mounted twice': [
                ('8:5', '/', os.path.dirname(self.real)), ('8:5', '/', elsewhere)],
        }
        for name, more in cases.items():
            with self.subTest(name):
                self.proc.mount(PROC_OPTIONS, more=more)
                self.assertEqual(holders.blockers(self.tree), [(self.whole, holders.UNREAD)])

    def test_another_folder_or_disk_mounted_elsewhere_leaves_the_tree_free(self):
        elsewhere = os.path.join(FS_TOP, 'srv-bind')
        cases = {
            'another folder of its file system': [(DISK, inside(self.real) + '-other', elsewhere)],
            'another disk': [('8:2', '/', elsewhere)],
            'the disk it was on, once it has its own': [
                ('8:5', '/', os.path.dirname(self.real)), (DISK, '/', elsewhere)],
        }
        for name, more in cases.items():
            with self.subTest(name):
                self.proc.mount(PROC_OPTIONS, more=more)
                self.assertEqual(holders.blockers(self.tree), [])

    def test_a_tree_no_mount_covers_is_unread(self):
        # this process's mount table does not reach the tree (a chroot): where else it shows is
        # not known
        self.proc.mount(PROC_OPTIONS, top=os.path.join(FS_TOP, 'nowhere'))
        self.assertEqual(holders.blockers(self.tree), [(self.whole, holders.UNREAD)])

    def test_a_proc_that_hides_only_others_contents_is_searched(self):
        for option in ('hidepid=0', 'hidepid=1', 'hidepid=off', 'hidepid=noaccess', 'gid=10'):
            with self.subTest(option=option):
                self.proc.mount('rw,%s' % option)
                self.assertEqual(holders.blockers(self.tree), [])

    def test_mount_options_not_known_is_not_searched(self):
        cases = {
            'no proc mount at PROC': lambda: self.proc.mount(PROC_OPTIONS, point='/elsewhere'),
            'another file system at PROC': lambda: self.proc.mount(PROC_OPTIONS, fstype='tmpfs'),
            'no mountinfo': lambda: os.remove(os.path.join(self.proc.root, str(SELF), 'mountinfo')),
        }
        for name, make in cases.items():
            with self.subTest(name):
                self.proc.reset()
                make()
                self.assertEqual(holders.blockers(self.tree), [(self.whole, holders.NOT_SEARCHED)])

    def test_a_process_of_this_user_the_kernel_marks_not_dumpable_is_unread_not_free(self):
        # as a real kernel shows ssh-agent: its /proc folder is root's and its links are denied;
        # status still names this user as its real user
        denied = PermissionError(errno.EACCES, 'Permission denied')
        self.proc.add(10, cwd=denied, root=denied, exe=denied, mnt=denied, owner=ROOT,
                      comm='ssh-agent')
        self.assertEqual(holders.blockers(self.tree), [(self.whole, holders.UNREAD)])

    def test_this_user_as_either_the_real_user_or_the_folder_owner_is_this_user_s(self):
        self.proc.add(10, cwd=self.sub, uid=OTHER, owner=ME)    # run as this user by another
        self.assertEqual(holders.blockers(self.tree), [(self.sub, holders.HELD)])

    def test_this_user_as_any_of_the_four_status_users_is_this_user_s(self):
        # real, effective, saved, file system: a setuid program run by another user, or one that
        # dropped to this user, in a folder the kernel gives root
        for uids in ((OTHER, ME, OTHER, OTHER), (OTHER, OTHER, ME, OTHER),
                     (OTHER, OTHER, OTHER, ME)):
            with self.subTest(uids=uids):
                self.proc.reset()
                self.proc.add(10, cwd=self.sub, uid=uids, owner=ROOT)
                self.assertEqual(holders.blockers(self.tree), [(self.sub, holders.HELD)])

    def test_status_naming_another_user_is_another_user_s_even_in_a_root_folder(self):
        self.proc.add(10, cwd=self.sub, uid=OTHER, owner=ROOT)
        self.proc.add(11, cwd=self.sub, uid=ROOT)
        self.assertEqual(holders.blockers(self.tree), [])

    def test_no_proc_is_not_searched(self):
        shutil.rmtree(self.proc.root)
        self.assertEqual(holders.blockers(self.tree), [(self.whole, holders.NOT_SEARCHED)])

    def test_an_empty_proc_is_not_searched(self):
        self.proc.clear()
        self.assertEqual(holders.blockers(self.tree), [(self.whole, holders.NOT_SEARCHED)])

    def test_a_proc_that_does_not_list_this_process_is_not_searched(self):
        self.proc.clear()
        self.proc.add(10, cwd='/home/u')                   # some other process list
        self.assertEqual(holders.blockers(self.tree), [(self.whole, holders.NOT_SEARCHED)])

    def _no_status(self, owner):
        """Process 10 whose status cannot be read and whose folder `owner` owns (or raises)."""
        folder = self.proc.add(10)
        self._replace(folder, 'status', 'folder')
        self.proc.owners[folder] = owner

    def _replace(self, folder, name, kind):
        """Make `folder`/`name` a `kind` ('file' or 'folder') where the other belongs."""
        path = os.path.join(folder, name)
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        if kind == 'file':
            open(path, 'w').close()
        else:
            os.makedirs(path)


class MountOptions(unittest.TestCase):
    def test_every_proc_mount_at_the_point_is_read_and_unescaped(self):
        text = ('21 1 8:1 / / rw - ext4 /dev/sda1 rw\n'
                '22 21 0:21 / /my\\040proc rw,nosuid shared:12 - proc proc rw,hidepid=2\n'
                '23 21 0:22 / /my\\040proc rw master:3 - proc proc rw,gid=10\n')
        self.assertEqual(paths.proc_options(text, '/my proc'),
                         {'rw', 'nosuid', 'hidepid=2', 'gid=10'})
        self.assertIsNone(paths.proc_options(text, '/proc'))


class CwdHolders(Base):
    def test_holders_are_named_with_the_unreadable_and_total_counts(self):
        self.proc.add(10, cwd=self.sub, comm='godot')
        self.proc.add(11, cwd='/home/u', fds=[self.sub])     # an fd is no cwd holder
        self.proc.add(12, cwd=PermissionError(errno.EACCES, 'Permission denied'))
        self.proc.add(13, cwd=self.sub, uid=OTHER)
        self.assertEqual(holders.cwd_holders(self.tree), ([(10, 'godot', self.sub)], 1, 5))

    def test_no_proc_names_nobody(self):
        shutil.rmtree(self.proc.root)
        self.assertEqual(holders.cwd_holders(self.tree), ([], 0, 0))


class Describe(Base):
    def test_each_code_has_its_words(self):
        self.proc.add(10, cwd=self.sub, comm='godot')
        text = '\n'.join(holders.describe(self.tree, [(self.sub, holders.HELD),
                                                      (self.whole, holders.UNREAD)], cwd='/'))
        self.assertIn('blocked  %s  in use by a process of this user' % self.sub, text)
        self.assertIn('blocked  %s  not known to be free: a process of this user could not be read'
                      % self.whole, text)
        self.assertIn('holder   pid 10  godot  cwd %s' % self.sub, text)
        text = '\n'.join(holders.describe(self.tree, [(self.whole, holders.NOT_SEARCHED)], cwd='/'))
        self.assertIn('not searched: the process list (%s) cannot be read' % paths.PROC, text)


class ProcessCwds(Base):
    def test_the_scan_reads_proc_on_linux(self):
        self.proc.add(10, cwd=self.sub, comm='bash')
        self.proc.add(11, cwd=PermissionError(errno.EACCES, 'Permission denied'))
        self.proc.add(12, cwd='/', uid=OTHER)
        (cwds, unreadable, total), why = classify.process_cwds()
        self.assertIsNone(why)
        self.assertEqual(sorted(cwds), [(10, 'bash', self.sub), (SELF, 'kit-test', TOP)])
        self.assertEqual((unreadable, total), (1, 4))

    def test_no_proc_is_no_answer(self):
        shutil.rmtree(self.proc.root)
        got, why = classify.process_cwds()
        self.assertIsNone(got)
        self.assertIn('the process list cannot be read', why)

    def test_an_empty_list_is_no_answer(self):
        self.proc.clear()
        got, why = classify.process_cwds()
        self.assertIsNone(got)
        self.assertIn('lists no process', why)


@posix_only('real processes read through the real /proc')
class Live(unittest.TestCase):
    def setUp(self):
        self.tree = tempfile.mkdtemp(prefix='kit-held-live-')
        self.addCleanup(shutil.rmtree, self.tree, True)

    def child(self, code, cwd=None):
        p = subprocess.Popen([sys.executable, '-c', code], cwd=cwd, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.wait, 30)
        self.addCleanup(p.kill)
        self.assertEqual(p.stdout.readline().strip(), 'ready')
        return p

    def test_a_process_standing_in_the_tree_holds_it(self):
        p = self.child('print("ready", flush=True); input()', cwd=self.tree)
        real = os.path.realpath(self.tree)
        self.assertIn((real, holders.HELD), holders.blockers(self.tree))
        found, _unread, _total = holders.cwd_holders(self.tree)
        self.assertIn(p.pid, [pid for pid, _n, _w in found])
        cwds, why = classify.process_cwds()
        self.assertIsNone(why)
        self.assertIn((p.pid, real), [(pid, where) for pid, _n, where in cwds[0]])

    def test_an_open_file_in_the_tree_holds_it(self):
        path = os.path.join(self.tree, 'f.txt')
        with open(path, 'w') as f:
            f.write('x')
        self.child('f = open(%r); print("ready", flush=True); input()' % path)
        self.assertIn((os.path.realpath(path), holders.HELD), holders.blockers(self.tree))

    def test_a_tree_nothing_uses_is_free(self):
        self.assertEqual(holders.blockers(self.tree), [])


if __name__ == '__main__':
    unittest.main()
