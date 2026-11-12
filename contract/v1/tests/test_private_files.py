"""Owner-only artifact files on every OS (POSIX 0600, Windows owner-only ACL)."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from contract.v1.privatefs import WELL_KNOWN_BROAD_SIDS, acl_sids, current_user_sid, make_owner_only, write_private  # noqa: E402


class PrivateFiles(unittest.TestCase):
    def test_write_private_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "att_x.result.json"
            write_private(p, b'{"ok":true}')
            self.assertEqual(p.read_bytes(), b'{"ok":true}')
            self.assertFalse(Path(str(p) + ".tmp").exists())
            if os.name == "nt":
                sids = acl_sids(p)
                self.assertEqual(set(sids), {current_user_sid()})
            else:
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_overwrite_keeps_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "pins.json"
            write_private(p, b"1")
            write_private(p, b"22")
            self.assertEqual(p.read_bytes(), b"22")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            else:
                self.assertEqual(set(acl_sids(p)), {current_user_sid()})

    @unittest.skipUnless(os.name == "nt", "Windows ACL check")
    def test_windows_other_users_and_groups_cannot_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "open.json"
            p.write_text("x", encoding="utf-8")
            before = set(acl_sids(p))
            # A normal temp file inherits more than the owner (at least SYSTEM/Administrators).
            self.assertTrue(before - {current_user_sid()}, "test precondition: inherited ACEs expected")
            make_owner_only(p)
            after = acl_sids(p)
            self.assertEqual(set(after), {current_user_sid()})
            for sid, name in WELL_KNOWN_BROAD_SIDS.items():
                self.assertNotIn(sid, after, name)

    @unittest.skipIf(os.name == "nt", "POSIX mode check")
    def test_posix_group_and_other_bits_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "open.json"
            p.write_text("x", encoding="utf-8")
            os.chmod(p, 0o666)
            make_owner_only(p)
            self.assertEqual(stat.S_IMODE(p.stat().st_mode) & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
