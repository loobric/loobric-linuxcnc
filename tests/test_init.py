# MIT License
# Copyright (c) 2025 sliptonic
# SPDX-License-Identifier: MIT

"""Tests for the `init` setup wizard, including multi-config handling.

Assumptions:
- _discover_inis() returns a sorted list of candidate INIs (possibly empty)
- cmd_init() never clobbers without force, writes mode 0600, and renders every
  discovered INI into the file (one active, the rest commented) so a multi-
  machine box can switch by un/commenting instead of retyping a path
- interactively it prompts for server URL (default sandbox), API key (blank
  allowed), machine name (default hostname), the INI when several exist, and
  whether to run doctor; a non-interactive run (no tty) takes every default
  without ever blocking on a prompt
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
        # Default to non-interactive so the wizard never blocks on input in CI
        # or a dev terminal. Interactive tests re-patch these to True locally.
        for stream in (sys.stdin, sys.stdout):
            p = mock.patch.object(stream, "isatty", return_value=False)
            p.start()
            self.addCleanup(p.stop)

    def _read(self):
        with open(self.path) as f:
            return f.read()

    # --- file safety -------------------------------------------------------

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

    # --- INI discovery -----------------------------------------------------

    def test_no_configs_uses_placeholder(self):
        with mock.patch.object(sl, "_discover_inis", return_value=[]):
            self.assertEqual(sl.cmd_init(self.path), 0)
        self.assertIn('LINUXCNC_INI="%s"' % sl.INI_PLACEHOLDER, self._read())

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
        with mock.patch.object(sl, "_discover_inis", return_value=inis):
            self.assertEqual(sl.cmd_init(self.path), 0)  # non-interactive: first wins
        body = self._read()
        self.assertIn('LINUXCNC_INI="%s"' % inis[0], body)        # active
        self.assertIn('# LINUXCNC_INI="%s"' % inis[1], body)      # alternative

    def test_explicit_ini_overrides_discovery(self):
        inis = ["/home/u/linuxcnc/configs/mill/mill.ini"]
        explicit = "/opt/custom/my.ini"
        with mock.patch.object(sl, "_discover_inis", return_value=inis), \
                mock.patch("builtins.input",
                           side_effect=AssertionError("must not prompt")):
            self.assertEqual(sl.cmd_init(self.path, ini=explicit), 0)
        body = self._read()
        self.assertIn('LINUXCNC_INI="%s"' % explicit, body)       # active
        self.assertIn('# LINUXCNC_INI="%s"' % inis[0], body)      # discovered, commented

    # --- non-interactive defaults -----------------------------------------

    def test_noninteractive_defaults(self):
        with mock.patch.object(sl, "_discover_inis", return_value=[]), \
                mock.patch.object(sl, "_default_machine_name", return_value="box7"), \
                mock.patch("builtins.input",
                           side_effect=AssertionError("must not prompt")):
            self.assertEqual(sl.cmd_init(self.path), 0)
        cfg = sl.parse_config(self._read())
        self.assertEqual(cfg["SMOOTH_API_URL"], sl.SANDBOX_URL)  # sandbox default
        self.assertEqual(cfg["SMOOTH_API_KEY"], "")              # blank
        self.assertEqual(cfg["MACHINE_NAME"], "box7")            # hostname default
        self.assertIn("SANDBOX", self._read())                  # warning comment present

    # --- interactive wizard ------------------------------------------------

    def _interactive(self):
        """Context managers that make cmd_init think it has a terminal."""
        return (
            mock.patch.object(sys.stdin, "isatty", return_value=True),
            mock.patch.object(sys.stdout, "isatty", return_value=True),
        )

    def test_wizard_fills_answered_values(self):
        # Answers: url, key, machine, run-doctor? -> no
        answers = ["http://nas.local:8000", "secret-key", "mill42", "n"]
        i1, i2 = self._interactive()
        with mock.patch.object(sl, "_discover_inis", return_value=[]), i1, i2, \
                mock.patch("builtins.input", side_effect=answers):
            self.assertEqual(sl.cmd_init(self.path), 0)
        cfg = sl.parse_config(self._read())
        self.assertEqual(cfg["SMOOTH_API_URL"], "http://nas.local:8000")
        self.assertEqual(cfg["SMOOTH_API_KEY"], "secret-key")
        self.assertEqual(cfg["MACHINE_NAME"], "mill42")
        self.assertNotIn("SANDBOX", self._read())  # not the sandbox, no warning

    def test_wizard_blank_answers_take_defaults(self):
        # Empty url + empty key + empty machine + empty doctor-answer.
        answers = ["", "", "", ""]
        i1, i2 = self._interactive()
        with mock.patch.object(sl, "_discover_inis", return_value=[]), i1, i2, \
                mock.patch.object(sl, "_default_machine_name", return_value="lathe9"), \
                mock.patch.object(sl, "cmd_doctor", return_value=2) as doc, \
                mock.patch("builtins.input", side_effect=answers):
            self.assertEqual(sl.cmd_init(self.path), 0)  # doctor's code doesn't sink init
        cfg = sl.parse_config(self._read())
        self.assertEqual(cfg["SMOOTH_API_URL"], sl.SANDBOX_URL)
        self.assertEqual(cfg["SMOOTH_API_KEY"], "")
        self.assertEqual(cfg["MACHINE_NAME"], "lathe9")
        doc.assert_called_once()  # blank == default "Y" -> doctor offered & run

    def test_wizard_offers_doctor_and_runs_on_yes(self):
        answers = ["http://x", "", "m", "y"]
        i1, i2 = self._interactive()
        with mock.patch.object(sl, "_discover_inis", return_value=[]), i1, i2, \
                mock.patch.object(sl, "cmd_doctor", return_value=0) as doc, \
                mock.patch("builtins.input", side_effect=answers):
            self.assertEqual(sl.cmd_init(self.path), 0)
        doc.assert_called_once()

    def test_wizard_skips_doctor_on_no(self):
        answers = ["http://x", "", "m", "n"]
        i1, i2 = self._interactive()
        with mock.patch.object(sl, "_discover_inis", return_value=[]), i1, i2, \
                mock.patch.object(sl, "cmd_doctor") as doc, \
                mock.patch("builtins.input", side_effect=answers):
            self.assertEqual(sl.cmd_init(self.path), 0)
        doc.assert_not_called()

    def test_wizard_interactive_ini_choice(self):
        inis = [
            "/home/u/linuxcnc/configs/lathe/lathe.ini",
            "/home/u/linuxcnc/configs/mill/mill.ini",
        ]
        # url, key, machine, ini-choice "2", doctor "n"
        answers = ["http://x", "", "m", "2", "n"]
        i1, i2 = self._interactive()
        with mock.patch.object(sl, "_discover_inis", return_value=inis), i1, i2, \
                mock.patch("builtins.input", side_effect=answers):
            self.assertEqual(sl.cmd_init(self.path), 0)
        body = self._read()
        self.assertIn('LINUXCNC_INI="%s"' % inis[1], body)        # chose #2
        self.assertIn('# LINUXCNC_INI="%s"' % inis[0], body)      # #1 commented


if __name__ == "__main__":
    unittest.main()
