"""Offline byte-stream faults; decoding never attests or routes a frame."""
import unittest

from optional.bridge.core.jsonwire import (
    MAX_CHUNK_BYTES, MAX_FRAME_BYTES, MAX_FRAMES_PER_FEED, JsonLineDecoder,
    WireError, encode_frame,
)


class Framing(unittest.TestCase):
    def test_split_utf8_and_crlf_roundtrip(self):
        value = {'message': 'snowman \u2603 and emoji \U0001f680', 'id': 1}
        wire = encode_frame(value)[:-1] + b'\r\n'
        decoder = JsonLineDecoder()
        frames = []
        for byte in wire:
            frames.extend(decoder.feed(bytes([byte])))
        self.assertEqual(frames, [value])
        self.assertEqual(decoder.pending_bytes, 0)
        decoder.finish()

    def test_coalesced_frames_and_pending_tail(self):
        decoder = JsonLineDecoder()
        self.assertEqual(decoder.feed(b'{"id":1}\n{"id":2}\n{"id":'),
                         ({'id': 1}, {'id': 2}))
        self.assertGreater(decoder.pending_bytes, 0)
        self.assertEqual(decoder.feed(b'3}\n'), ({'id': 3},))
        decoder.finish()

    def test_codex_and_peer_style_objects_are_only_opaque_data(self):
        values = (
            {'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'fixture', 'version': '0'}}},
            {'type': 'auth', 'token': 'fixture-not-a-credential'},
            {'msgV': 1, 'msg_id': 'fixture-id', 'type': 'user',
             'message': {'role': 'user', 'content': '<cross-session-message>fixture</cross-session-message>'}},
        )
        decoder = JsonLineDecoder()
        self.assertEqual(decoder.feed(b''.join(encode_frame(value) for value in values)), values)
        decoder.finish()

    def test_duplicate_keys_invalid_utf8_and_non_finite_numbers_end_decoder(self):
        cases = ((b'{"id":1,"id":2}\n', 'duplicate-key'),
                 (b'{"secret":"\xff"}\n', 'invalid-json'),
                 (b'{"secret":NaN}\n', 'non-finite-number'),
                 (b'{"secret":1e9999}\n', 'non-finite-number'))
        for wire, code in cases:
            with self.subTest(code=code):
                decoder = JsonLineDecoder()
                with self.assertRaises(WireError) as caught:
                    decoder.feed(wire)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn('secret', str(caught.exception))
                self.assertEqual(decoder.pending_bytes, 0)
                with self.assertRaisesRegex(WireError, 'decoder-failed'):
                    decoder.feed(b'{}\n')

    def test_empty_or_non_object_frames_are_rejected(self):
        for wire, code in ((b'\n', 'empty-frame'), (b'[]\n', 'object-required'),
                           (b'null\n', 'object-required')):
            with self.subTest(code=code):
                with self.assertRaises(WireError) as caught:
                    JsonLineDecoder().feed(wire)
                self.assertEqual(caught.exception.code, code)

    def test_oversized_pending_frame_is_rejected_before_retaining_it(self):
        decoder = JsonLineDecoder(8)
        self.assertEqual(decoder.feed(b'{"key":'), ())
        with self.assertRaisesRegex(WireError, 'frame-size'):
            decoder.feed(b'"more"}\n')
        self.assertEqual(decoder.pending_bytes, 0)

    def test_chunk_and_frame_count_are_bounded(self):
        with self.assertRaisesRegex(WireError, 'chunk-size'):
            JsonLineDecoder().feed(b'x' * (MAX_CHUNK_BYTES + 1))
        with self.assertRaisesRegex(WireError, 'frame-count'):
            JsonLineDecoder().feed(b'{}\n' * (MAX_FRAMES_PER_FEED + 1))

    def test_eof_requires_a_complete_frame_and_prevents_further_feed(self):
        decoder = JsonLineDecoder()
        decoder.feed(b'{"id":1}')
        with self.assertRaisesRegex(WireError, 'truncated-frame'):
            decoder.finish()
        clean = JsonLineDecoder()
        clean.finish()
        with self.assertRaisesRegex(WireError, 'decoder-finished'):
            clean.feed(b'{}\n')

    def test_invalid_outgoing_objects_limits_and_unicode_are_rejected(self):
        cases = ((['not-an-object'], 'object-required'),
                 ({1: 'non-string-key'}, 'non-string-key'),
                 ({'value': float('inf')}, 'non-finite-number'),
                 ({'value': '\ud800'}, 'invalid-unicode'),
                 ({'value': object()}, 'value-type'))
        for value, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(WireError) as caught:
                    encode_frame(value)
                self.assertEqual(caught.exception.code, code)
        for limit in (0, True, MAX_FRAME_BYTES + 1):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(WireError, 'invalid-frame-limit'):
                    JsonLineDecoder(limit)

    def test_encoding_size_uses_actual_utf8_bytes(self):
        with self.assertRaisesRegex(WireError, 'frame-size'):
            encode_frame({'text': '\U0001f680' * 4}, max_frame_bytes=16)

    def test_deep_or_cyclic_outgoing_values_are_bounded(self):
        cyclic = {}
        cyclic['loop'] = cyclic
        with self.assertRaisesRegex(WireError, 'object-depth'):
            encode_frame(cyclic)

    def test_incoming_depth_and_node_count_are_bounded(self):
        deep = b'{"value":' * 70 + b'null' + b'}' * 70 + b'\n'
        with self.assertRaisesRegex(WireError, 'object-depth'):
            JsonLineDecoder().feed(deep)
        wide = b'{"values":[' + b','.join([b'0'] * 20001) + b']}\n'
        with self.assertRaisesRegex(WireError, 'node-limit'):
            JsonLineDecoder().feed(wide)

    def test_failed_chunk_does_not_return_partial_success(self):
        decoder = JsonLineDecoder()
        with self.assertRaisesRegex(WireError, 'invalid-json'):
            decoder.feed(b'{"id":1}\nnot-json\n')
        with self.assertRaisesRegex(WireError, 'decoder-failed'):
            decoder.finish()
