"""kitlib.loadnode.reply: the node's reply is a projection of its lock, and on the main machine a
reply is untrusted: only known keys, checked types, clean short strings."""
import json
import os
import tempfile
import unittest

from kitlib.loadnode import registry, remote, reply
from tests import gitfixture
from tests.loadnode.test_registry import KEY
from tests.loadnode.test_remote import ok_reply
from tests.optional.loadnode.test_loadnode import snapshot


def good(**over):
    data = json.loads(ok_reply())
    for path, v in over.items():
        d = data
        keys = path.split('__')
        for k in keys[:-1]:
            d = d[k]
        d[keys[-1]] = v
    return json.dumps(data)


class Build(unittest.TestCase):

    def test_the_reply_carries_counts_not_names(self):
        got = reply.build(snapshot(store='/srv/STORE', repo='REPO'), '0.4.0')
        text = reply.dumps(got)
        for secret in ('STORE', 'REPO', 'a note', 'r1', 'h1'):
            self.assertNotIn(secret, text)
        self.assertEqual(reply.parse(text)['lock'], got['lock'])

    def test_unreadable_parts_and_a_pending_change_are_told(self):
        snap = snapshot()
        snap.update(holds_error='cannot read /x', kinds_error='bad')
        snap['queue']['error'] = 'locked'
        snap['layout']['hardware_change']['pending'] = True
        lock = reply.build(snap, '1')['lock']
        self.assertEqual((lock['unreadable'], lock['pending']), (['queue', 'holds', 'kinds'], True))

    def test_an_empty_snapshot_still_builds(self):
        got = reply.build({}, '1')
        self.assertEqual(reply.parse(reply.dumps(got))['lock']['cpu_slots'], 0)


class Parse(unittest.TestCase):

    def bad(self, text):
        with self.assertRaises(reply.ReplyError) as cm:
            reply.parse(text)
        return str(cm.exception)

    def test_a_good_reply_reads(self):
        got = reply.parse(good())
        self.assertEqual(got['kit_version'], '0.4.0')
        self.assertEqual(got['hardware']['memory_mb'], {'total': 15900, 'free': 9100})

    def test_unknown_keys_are_dropped(self):
        got = reply.parse(good(extra='x', lock__extra=1, hardware__extra=[1]))
        self.assertNotIn('extra', got)
        self.assertNotIn('extra', got['lock'])
        self.assertNotIn('extra', got['hardware'])

    def test_strings_lose_control_and_format_characters_and_are_cut(self):
        got = reply.parse(good(kit_version='0.4\x1b[2J‮.0' + 'v' * 100,
                               hardware__gpus=[{'name': 'gpu\r\nfake line', 'vram_mb': None}]))
        self.assertTrue(got['kit_version'].startswith('0.4[2J.0v'))
        self.assertEqual(len(got['kit_version']), 40)
        self.assertEqual(got['hardware']['gpus'][0]['name'], 'gpufake line')

    def test_wrong_types_and_ranges_are_refused_without_quoting_the_reply(self):
        for over in ({'lock__cpu_free': True}, {'lock__cpu_free': -1}, {'lock__cpu_free': 1.0},
                     {'lock__running': 10 ** 10}, {'lock__pending': 'no'}, {'lock__unreadable': ['x']},
                     {'lock__gpu_free': 1}, {'hardware__gpus': [{}] * 17}, {'hardware__gpus': ['SECRET']},
                     {'hardware__memory_mb': 'SECRET'}, {'kit_version': 7}, {'ok': 'SECRET'},
                     {'protocol': True}, {'lock': None}):
            with self.subTest(over=over):
                self.assertNotIn('SECRET', self.bad(good(**over)))

    def test_not_one_json_object_line_is_refused(self):
        for text in ('', 'SECRET', '[1]', good() + '\n' + good(), '{"protocol": 1}'):
            with self.subTest(text=text[:30]):
                self.assertNotIn('SECRET', self.bad(text))

    def test_verbs_and_jobs_read_and_an_old_node_has_neither(self):
        old = reply.parse(good())
        self.assertEqual((old['verbs'], old['jobs']), ([], None))
        got = reply.parse(good(verbs=['status', 'start', 'follow', 'start'], jobs=2))
        self.assertEqual((got['verbs'], got['jobs']), (['follow', 'start', 'status'], 2))
        built = reply.build(snapshot(), '1', verbs=('status', 'start'), jobs=0)
        self.assertEqual(reply.parse(reply.dumps(built))['verbs'], ['start', 'status'])

    def test_bad_verbs_and_jobs_are_refused_without_quoting_the_reply(self):
        for over in ({'verbs': 'SECRET'}, {'verbs': ['SECRET']}, {'verbs': ['a\x1b']},
                     {'verbs': [7]}, {'verbs': ['stat'] * 17}, {'jobs': -1}, {'jobs': 'SECRET'},
                     {'jobs': True}):
            with self.subTest(over=over):
                self.assertNotIn('SECRET', self.bad(good(**over)))

    def test_a_refusal_reads_with_its_error_cleaned(self):
        got = reply.parse(json.dumps({'protocol': 1, 'ok': False, 'error': 'no\x07pe' + 'e' * 300}))
        self.assertEqual((got['ok'], got['error'][:4], len(got['error'])), (False, 'nope', 200))
        self.assertIn('error', self.bad(json.dumps({'protocol': 1, 'ok': False})))


class StatusLines(unittest.TestCase):

    def test_ok_and_failed_nodes_with_the_registry_caps(self):
        nodes = [registry.Node('box1', '10.0.0.1', 'pm', KEY, 1, 12288, 22, None),
                 registry.Node('box2', '10.0.0.2', 'pm', KEY, 2, 4096, 22, None)]
        ok = reply.parse(good(lock__pending=True, kit_version='0.3.9'))
        results = [remote.Result('box1', remote.OK, ok, ''),
                   remote.Result('box2', remote.UNREACHABLE, None, 'Connection refused')]
        lines = reply.status_lines(results, nodes, '0.4.0')
        self.assertTrue(lines[0].startswith('NODE'))
        self.assertIn('3/4', lines[1])
        self.assertIn('8.9/15.5 GB', lines[1])
        self.assertIn('1 slot, 12.0 GB', lines[1])
        self.assertIn('0.3.9 (here 0.4.0)', lines[1])
        self.assertIn('unreachable', lines[2])
        self.assertIn('2 slots, 4.0 GB', lines[2])
        self.assertIn('box2: unreachable: Connection refused', lines)
        self.assertIn('box1: its machine lock has a hardware change waiting to be adopted', lines)


class KitVersion(unittest.TestCase):

    def test_the_version_or_unknown(self):
        d = tempfile.mkdtemp(prefix='kit-loadnode-')
        self.addCleanup(gitfixture.rmtree, d)
        self.assertEqual(reply.kit_version(d), 'unknown')
        with open(os.path.join(d, 'kit.json'), 'w', encoding='utf-8') as f:
            f.write('{"version": "9.9.9"}')
        self.assertEqual(reply.kit_version(d), '9.9.9')


if __name__ == '__main__':
    unittest.main()
