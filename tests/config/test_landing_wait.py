"""The default "landing".wait_s (kitlib/config/schema.py): a submitted landing queued behind a
chain of landings that took more than an hour must still get its turn. With the old default,
3600 s, two landings queued serially on 2026-10-17 came back NOT RUN with "waited 3600 s (its
--max-wait)"."""

import unittest

from kitlib.config import schema


class LandingWait(unittest.TestCase):

    def test_the_default_outlasts_a_queue_of_more_than_an_hour(self):
        wait_s = schema.defaults('landing')['wait_s']
        self.assertEqual(wait_s, 21600)
        self.assertGreater(wait_s, 3600)


if __name__ == '__main__':
    unittest.main()
