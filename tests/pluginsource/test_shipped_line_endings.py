"""What a plugin export ships for a cmd script: the blob's own bytes (kitlib.pluginsource.export
reads blobs with one cat-file batch and no eol filter), and kitlib.machinesetup copies them byte
for byte into a new repository. cmd.exe misreads goto labels in a file with LF line endings, so the
kit stores its .cmd and .bat files as-is (`-text` in .gitattributes), CRLF in the blob."""
import unittest

from kitlib import pluginsource as P
from kitlib.git import batch


class ExportedCmdScriptsKeepCrlf(unittest.TestCase):
    def test_every_tracked_cmd_and_bat_blob_at_head_has_crlf_line_endings(self):
        kit = P.kit_root()
        blobs = [e for e in P._tree(kit, 'HEAD')
                 if e[1] == 'blob' and e[3].lower().endswith(('.cmd', '.bat'))]
        paths = [e[3] for e in blobs]
        self.assertIn('templates/stacks/godot/tools/godot.cmd', paths)
        data, why = batch.cat_file(sorted({e[2] for e in blobs}), kit)
        self.assertIsNotNone(data, why)
        for _mode, _typ, oid, path in blobs:
            body = data[oid][1]
            lf, crlf = body.count(b'\n'), body.count(b'\r\n')
            self.assertGreater(lf, 0, path)
            self.assertEqual(crlf, lf, '%s: %d of %d line endings are CRLF in the blob' % (path, crlf, lf))


if __name__ == '__main__':
    unittest.main()
