# MIT License
# Copyright (c) 2025 sliptonic
# SPDX-License-Identifier: MIT

"""Tests for `init` config scaffolding, including multi-config handling.

Assumptions:
- _discover_inis() returns a sorted list of candidate INIs (possibly empty)
- cmd_init() never clobbers without force, writes mode 0600, and renders every
  discovered INI into the file (one active, the rest commented) so a multi-
  machine box can switch by un/commenting instead of retyping a path
- a non-interactive run (no tty) must never block on a prompt
"""
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import smooth_linuxcnc as sl


class InitConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "linuxcnc.conf")

    def _read(self):
        with open(self.path) as f:
            return f.read()

    def test_refuses_existing_without_force(self):
        with open(self.path, "w") as f:
            f.write("KEEP_ME=1\n")
        with mock.patch.object(sl, "_discover_inis", return_value=[]):
            self.assertEqual(sl.cmd_init(self.path), 2)
        self.assertEqual(self._read(), "KEEP_ME=1\n")  # untouched

    def test_force_overwrites_and_sets_0600(self):
        with open(self.path, "w") as f:
            f.write("OLD=1\n")
        os.chmod(self.path, 0o644)
        with mock.patch.object(sl, "_discover_inis", return_value=[]):
            self.assertEqual(sl.cmd_init(self.path, force=True), 0)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertIn("SMOOTH_API_URL", self._read())

    def test_no_configs_uses_placeholder(self):
        with mock.patch.object(sl, "_discover_inis", return_value=[]):
            self.assertEqual(sl.cmd_init(self.path), 0)
        body = self._read()
        self.assertIn('LINUXCNC_INI="%s"' % sl.INI_PLACEHOLDER, body)

    def test_single_config_is_activated(self):
        only = "/home/u/linuxcnc/configs/mill/mill.ini"
        with mock.patch.object(sl, "_discover_inis", return_value=[only]):
            self.assertEqual(sl.cmd_init(self.path), 0)
        body = self._read()
        self.assertIn('LINUXCNC_INI="%s"' % only, body)
        self.assertNotIn("# LINUXCNC_INI=", body)  # no alternatives

    def test_multiple_configs_noninteractive_lists_alternatives(self):
        inis = [
            "/home/u/linuxcnc/configs/lathe/lathe.ini",
            "/home/u/linuxcnc/configs/mill/mill.ini",
        ]
        # Force the non-interactive path: no tty -> first wins, rest commented.
        with mock.patch.object(sl, "_discover_inis", return_value=inis), \
                mock.patch.object(sys.stdin, "isatty", return_value=False):
            self.assertEqual(sl.cmd_init(self.path), 0)
        body = self._read()
        self.assertIn('LINUXCNC_INI="%s"' % inis[0], body)        # active
        self.assertIn('# LINUXCNC_INI="%s"' % inis[1], body)      # alternative

    def test_interactive_choice_selects_that_config(self):
        inis = [
            "/home/u/linuxcnc/configs/lathe/lathe.ini",
            "/home/u/linuxcnc/configs/mill/mill.ini",
        ]
        with mock.patch.object(sl, "_discover_inis", return_value=inis), \
                mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(sys.stdout, "isatty", return_value=True), \
                mock.patch("builtins.input", return_value="2"):
            self.assertEqual(sl.cmd_init(self.path), 0)
        body = self._read()
        self.assertIn('LINUXCNC_INI="%s"' % inis[1], body)        # chose #2
        self.assertIn('# LINUXCNC_INI="%s"' % inis[0], body)      # #1 commented

    def test_explicit_ini_overrides_discovery(self):
        inis = ["/home/u/linuxcnc/configs/mill/mill.ini"]
        explicit = "/opt/custom/my.ini"
        # --ini must win without prompting, even when discovery finds something.
        with mock.patch.object(sl, "_discover_inis", return_value=inis), \
                mock.patch("builtins.input",
                           side_effect=AssertionError("must not prompt")):
            self.assertEqual(sl.cmd_init(self.path, ini=explicit), 0)
        body = self._read()
        self.assertIn('LINUXCNC_INI="%s"' % explicit, body)       # active
        self.assertIn('# LINUXCNC_INI="%s"' % inis[0], body)      # discovered, commented

    def test_generated_config_is_parseable(self):
        inis = ["/a/x.ini", "/b/y.ini"]
        with mock.patch.object(sl, "_discover_inis", return_value=inis), \
                mock.patch.object(sys.stdin, "isatty", return_value=False):
            sl.cmd_init(self.path)
        # The active line wins; the commented alternative must be ignored.
        cfg = sl.parse_config(self._read())
        self.assertEqual(cfg["LINUXCNC_INI"], inis[0])


if __name__ == "__main__":
    unittest.main()
