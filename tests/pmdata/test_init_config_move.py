"""`tools/pm_data.py init` on a machine whose config is still in the old folder
(docs/kit-json.md "Moving the machine config folder"): refused before any write, so the new
folder never gets a config file holding pm_data alone."""
import json
import os

from kitlib.config import migration, user
from kitlib.pmdata import init as pminit
from tests.pmdata.test_init import _Base


class OldFolderInit(_Base):

    def test_an_old_only_machine_config_is_refused_before_any_write(self):
        old = os.path.join(self.home, migration.OLD_DIR_NAME, user.FILE_NAME)
        os.makedirs(os.path.dirname(old))
        with open(old, 'w', encoding='utf-8') as f:
            json.dump({'ledger_path': os.path.join(self.home, 'rulings.md')}, f)
        refs, cfg = self.refs(), self.config_text()
        out = self.init()
        self.assertEqual(out.code, pminit.CONFIG, out.lines)
        self.assertTrue(any(old in l for l in out.lines), out.lines)
        self.assertEqual((self.refs(), self.config_text()), (refs, cfg))
        self.assertFalse(os.path.exists(os.path.join(self.home, user.DIR_NAME, user.FILE_NAME)))
        self.assertFalse(os.path.exists(self.data))
