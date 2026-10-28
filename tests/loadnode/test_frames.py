"""kitlib.loadnode.frames: a frame is handed on only when all of it came, so a dropped connection
never yields part of one, and a stream that is not frames is refused."""
import unittest

from kitlib.loadnode import frames


class Frames(unittest.TestCase):

    def test_frames_come_back_whatever_the_pieces(self):
        data = (frames.encode('L', b'line one\n\x00\xff') + frames.encode('K')
                + frames.encode('R', b'{"exit": 0}') + frames.encode('E'))
        want = [('L', b'line one\n\x00\xff'), ('K', b''), ('R', b'{"exit": 0}'), ('E', b'')]
        for size in (1, 2, 3, 7, len(data)):
            with self.subTest(size=size):
                d, got = frames.Decoder(), []
                for i in range(0, len(data), size):
                    got += d.feed(data[i:i + size])
                self.assertEqual((got, d.pending), (want, 0))

    def test_a_frame_cut_short_is_never_handed_on(self):
        d = frames.Decoder()
        whole = frames.encode('L', b'abc')
        got = d.feed(whole + frames.encode('L', b'0123456789')[:-1])
        self.assertEqual(got, [('L', b'abc')])
        self.assertEqual(d.pending, len(frames.encode('L', b'0123456789')) - 1)

    def test_a_bad_header_or_an_oversized_frame_is_refused(self):
        for data in (b'Q 1\nx', b'L x\n', b'L -1\n', b'L 1 2\n', b'L1\n', b'x' * 40,
                     b'L 999999999\n'):
            with self.subTest(data=data[:12]):
                with self.assertRaises(frames.FrameError):
                    frames.Decoder().feed(data)
        with self.assertRaises(frames.FrameError):
            frames.Decoder(max_frame=4).feed(b'L 5\n12345')

    def test_encode_refuses_an_unknown_kind_or_too_much(self):
        with self.assertRaises(ValueError):
            frames.encode('Q', b'')
        with self.assertRaises(ValueError):
            frames.encode('L', b'x' * (frames.MAX_FRAME + 1))


if __name__ == '__main__':
    unittest.main()
