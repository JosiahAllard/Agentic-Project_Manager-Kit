"""kitlib.ident: the bounded id, digest and integer checks the bridge's records use."""
import hashlib
import unittest

from kitlib.ident import IdentError, MAX_INTEGER, digest, identifier, integer


class Identifier(unittest.TestCase):
    def test_accepts_an_opaque_id_and_returns_it(self):
        for value in ('a', 'Z9', 'chat-1.2:3_x', 'a' * 128):
            self.assertIs(identifier(value), value)

    def test_refuses_anything_else_with_a_code_and_not_the_value(self):
        for value in ('', '-a', '.a', 'a' * 129, 'a b', 'a/b', 'é', 'a\n', None, 1, b'a'):
            with self.subTest(value=value), self.assertRaises(IdentError) as caught:
                identifier(value)
            self.assertEqual(caught.exception.code, 'invalid-identifier')
            self.assertNotIn(repr(value), str(caught.exception))

    def test_the_error_is_a_value_error(self):
        self.assertTrue(issubclass(IdentError, ValueError))


class Digest(unittest.TestCase):
    def test_accepts_a_lowercase_sha256(self):
        value = hashlib.sha256(b'x').hexdigest()
        self.assertIs(digest(value), value)

    def test_refuses_upper_case_short_long_and_non_text(self):
        value = hashlib.sha256(b'x').hexdigest()
        for bad in (value.upper(), value[:-1], value + '0', 'g' * 64, None, value.encode()):
            with self.subTest(bad=bad), self.assertRaisesRegex(IdentError, 'invalid-digest'):
                digest(bad)


class Integer(unittest.TestCase):
    def test_accepts_one_to_the_maximum(self):
        for value in (1, 42, MAX_INTEGER):
            self.assertIs(integer(value), value)
        self.assertEqual(integer(7, maximum=7), 7)

    def test_refuses_zero_negative_too_large_bool_and_float(self):
        for value in (0, -1, MAX_INTEGER + 1, True, 1.0, '1', None):
            with self.subTest(value=value), self.assertRaisesRegex(IdentError, 'invalid-integer'):
                integer(value)
        with self.assertRaisesRegex(IdentError, 'invalid-integer'):
            integer(8, maximum=7)


if __name__ == '__main__':
    unittest.main()
