"""kitlib.gate.land Lander.push: a push that failed before it connected to the remote (git's
`fatal: unable to access '<url>': <curl's connect error>`, exit 128, and nothing else on stderr)
cannot have landed, so the landing is NOT LANDED (FAILED) and the lock is released, as for a
refused push, whatever ls-remote then reads (in an outage it fails the same way). Every failure
after the connection, and any stderr that also holds a later-phase line, stays STUCK.

The strings are what git 2.55.0.windows.5 (libcurl 8.21.0) printed on this machine under LC_ALL=C
for `git ls-remote` (the same remote-curl.c discover_refs call a push makes first) against an
unresolvable host, a refused port and a black-hole address, and the earlier curl forms of the same
errors (`port 443`, an OS reason, no `after <n> ms`), and curl's timeout lines for the resolving
and connecting states (lib/multi.c). The push is replaced by a fake git result; the ls-remote that
follows reads the scratch remote unless faked. Scratch repositories with a bare remote in temp and
the scratch lock store only."""
import unittest

from kitlib import gate as G
from kitlib.gate import land
from kitlib.git import cmd
from tests.gate.test_land import BRANCH, PushCase
from tests.gate import test_push_fail_at_base as at_base

URL = "fatal: unable to access 'https://github.com/owner/repo.git/': "
# connect phase: no connection was made, so no byte of the push reached the remote
RESOLVE = URL + 'Could not resolve host: github.com\n'
RESOLVE_PROXY = URL + 'Could not resolve proxy: proxy.example\n'
REFUSED_8 = URL + 'Failed to connect to github.com:443 after 2098 ms: Could not connect to server\n'
TIMED_OUT_8 = URL + 'Failed to connect to github.com:443 after 21102 ms: Could not connect to server\n'
REFUSED_PORT = URL + "Failed to connect to github.com port 443 after 21 ms: Couldn't connect to server\n"
REFUSED_OLD = URL + 'Failed to connect to github.com port 443: Connection refused\n'
TIMED_OUT_OLD = URL + 'Failed to connect to github.com port 443: Timed out\n'
IPV6 = "fatal: unable to access 'https://[::1]:1/o/r.git/': Failed to connect to ::1:1 after 2051 ms: Could not connect to server\n"
CONNECT_TIMEOUT = URL + 'Connection timed out after 300001 milliseconds\n'
RESOLVE_TIMEOUT = URL + 'Resolving timed out after 10000 milliseconds\n'
CONNECT_PHASE = (RESOLVE, RESOLVE_PROXY, REFUSED_8, TIMED_OUT_8, REFUSED_PORT, REFUSED_OLD,
                 TIMED_OUT_OLD, IPV6, CONNECT_TIMEOUT, RESOLVE_TIMEOUT)

# after the connection: the upload may have reached the remote
RESET = ('error: RPC failed; curl 35 Recv failure: Connection was reset\n'
         'send-pack: unexpected disconnect while reading sideband packet\n'
         'fatal: the remote end hung up unexpectedly\n'
         "error: failed to push some refs to 'https://github.com/owner/repo.git'\n")
HUNG_UP = 'fatal: the remote end hung up unexpectedly\n'
DISCOVERY_RESET = URL + 'Recv failure: Connection was reset\n'
OPERATION_TIMEOUT = URL + 'Operation timed out after 300000 milliseconds with 0 out of 0 bytes received\n'
TLS = URL + 'schannel: failed to receive handshake, SSL/TLS connection failed\n'
RPC_CONNECT = ("error: RPC failed; curl 7 Failed to connect to github.com:443 after 21102 ms: "
               "Could not connect to server\n" + HUNG_UP)
MIXED = RESOLVE + HUNG_UP
PROXY_CONNECT = URL + ('Failed to connect to github.com:443 over proxy proxy.example after 2045 ms: '
                       'Could not connect to server\n')
LATER_PHASE = (RESET, HUNG_UP, DISCOVERY_RESET, OPERATION_TIMEOUT, TLS, RPC_CONNECT, MIXED,
               PROXY_CONNECT, URL + 'Could not resolve host: github.com; then more\n', '')


class NeverConnected(unittest.TestCase):
    """land.push_never_connected alone."""

    def test_each_connect_phase_failure_is_told(self):
        for err in CONNECT_PHASE:
            with self.subTest(err=err):
                self.assertTrue(land.push_never_connected(128, err))
                self.assertTrue(land.push_never_connected(128, err.replace('\n', '\r\n')))

    def test_each_later_failure_is_not(self):
        for err in LATER_PHASE:
            with self.subTest(err=err):
                self.assertFalse(land.push_never_connected(128, err))

    def test_only_a_git_that_died_with_it_counts(self):
        # exit 128 is git's die(); a timed-out or killed git, or any other exit, is not told
        for rc in (None, 0, 1, 2, 255):
            with self.subTest(rc=rc):
                self.assertFalse(land.push_never_connected(rc, RESOLVE))


class PushNeverConnected(PushCase):
    land_with_push = at_base.PushFailsAtBase.land_with_push

    def assert_not_landed(self, out, err):
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(out.lines[-1].startswith('NOT LANDED: '), out.lines)
        self.assertIn('never connected', out.lines[-1])
        self.assertIn(err.strip().split("': ", 1)[1], out.lines[-1])
        self.assertFalse([l for l in out.lines if l.startswith(('STUCK', 'GATE OK'))], out.lines)
        self.assertTrue(out.merge)
        self.assertIsNone(self.lock().read())           # released: the next landing may start
        self.assertEqual(self.remote_main(), self.main)
        self.assertEqual(self.main_now(), self.main)

    def land_fails(self, err, ls=None):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.land_with_push(tip, cmd.Result(128, b'', err), ls=ls)
        self.assert_not_landed(out, err)
        return out, tip

    def test_could_not_resolve_host_with_the_remote_unreadable_is_not_landed(self):
        # tonight's outage: the ls-remote after the push fails the same way
        out, tip = self.land_fails(RESOLVE, ls=(None, 'Could not resolve host: github.com'))
        self.assertIn('unreadable (Could not resolve host: github.com)', out.lines[-1])
        again = self.lander(BRANCH, self.main, tip).run()   # and it lands, with no hand step
        self.assertEqual(again.code, G.OK, again.lines)
        self.assertEqual(self.remote_main(), again.merge)

    def test_could_not_resolve_host_at_the_base_is_not_landed(self):
        out, _ = self.land_fails(RESOLVE)
        self.assertIn('still the base %s' % self.main[:10], out.lines[-1])

    def test_could_not_resolve_proxy_is_not_landed(self):
        self.land_fails(RESOLVE_PROXY, ls=(None, 'network down'))

    def test_failed_to_connect_refused_curl_8_is_not_landed(self):
        self.land_fails(REFUSED_8, ls=(None, 'network down'))

    def test_failed_to_connect_timed_out_curl_8_is_not_landed(self):
        self.land_fails(TIMED_OUT_8)

    def test_failed_to_connect_port_form_is_not_landed(self):
        self.land_fails(REFUSED_PORT, ls=(None, 'network down'))

    def test_failed_to_connect_connection_refused_is_not_landed(self):
        self.land_fails(REFUSED_OLD)

    def test_failed_to_connect_timed_out_is_not_landed(self):
        self.land_fails(TIMED_OUT_OLD, ls=(None, 'network down'))

    def test_connection_timed_out_before_connect_is_not_landed(self):
        self.land_fails(CONNECT_TIMEOUT, ls=(None, 'network down'))

    def test_resolving_timed_out_is_not_landed(self):
        self.land_fails(RESOLVE_TIMEOUT, ls=(None, 'network down'))

    def test_a_remote_main_this_checkout_cannot_place_is_not_landed(self):
        # the push never connected, so the merge cannot be in whatever the remote's main is
        out, _ = self.land_fails(REFUSED_8, ls=('f' * 40, None))
        self.assertIn('cannot place', out.lines[-1])


class LaterFailureStaysStuck(PushCase):
    land_with_push = at_base.PushFailsAtBase.land_with_push

    def stuck(self, err, rc=128, ls=None):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.land_with_push(tip, cmd.Result(rc, b'', err), ls=ls)
        self.assert_stuck(out, tip)
        return out

    def test_a_connection_reset_after_connecting_is_stuck(self):
        self.stuck(RESET)

    def test_a_reset_while_reading_the_remote_refs_is_stuck(self):
        self.stuck(DISCOVERY_RESET, ls=(None, 'network down'))

    def test_an_operation_timeout_is_stuck(self):
        self.stuck(OPERATION_TIMEOUT, ls=(None, 'network down'))

    def test_a_tls_failure_is_stuck(self):
        self.stuck(TLS, ls=(None, 'network down'))

    def test_a_connect_failure_on_the_pack_post_is_stuck(self):
        # `RPC failed`: the ref discovery had connected, and git then hung up
        self.stuck(RPC_CONNECT, ls=(None, 'network down'))

    def test_a_connect_form_with_a_later_line_is_stuck(self):
        self.stuck(MIXED, ls=(None, 'network down'))

    def test_a_connect_form_from_a_git_that_did_not_die_is_stuck(self):
        self.stuck(RESOLVE, rc=None, ls=(None, 'network down'))


if __name__ == '__main__':
    unittest.main()
