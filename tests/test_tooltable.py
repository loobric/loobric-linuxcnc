# MIT License
# Copyright (c) 2025 sliptonic
# SPDX-License-Identifier: MIT

"""Tool table parse/generate round-trip tests (stdlib unittest only).

Assumptions:
- smooth_linuxcnc.py is a single stdlib-only file importable from repo root
- parse_tool_table/generate_tool_table round-trip losslessly
- Parse errors carry line numbers; duplicate tool numbers are errors
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import smooth_linuxcnc as sl

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sample.tbl")


class TestParse(unittest.TestCase):

    def test_parse_fixture(self):
        with open(FIXTURE) as f:
            tools = sl.parse_tool_table(f.read())
        self.assertEqual([t["tool_number"] for t in tools], [1, 3, 10, 22])
        t3 = tools[1]
        self.assertEqual(t3["pocket"], 3)
        self.assertAlmostEqual(t3["diameter"], 6.35)
        self.assertAlmostEqual(t3["z_offset"], -48.25)
        self.assertAlmostEqual(t3["x_offset"], 1.5)
        self.assertEqual(t3["comment"], "1/4 downcut")
        t22 = tools[3]
        self.assertAlmostEqual(t22["u_offset"], 0.1)
        self.assertAlmostEqual(t22["front_angle"], 45.0)

    def test_round_trip_is_lossless(self):
        with open(FIXTURE) as f:
            original = f.read()
        tools = sl.parse_tool_table(original)
        regenerated = sl.generate_tool_table(tools)
        self.assertEqual(sl.parse_tool_table(regenerated), tools)
        # And generation is stable (idempotent on its own output)
        self.assertEqual(sl.generate_tool_table(sl.parse_tool_table(regenerated)), regenerated)

    def test_missing_tool_number_is_error(self):
        with self.assertRaises(sl.ToolTableError):
            sl.parse_tool_table("P1 D+5.0 Z-1.0 ;no tool number")

    def test_duplicate_tool_number_is_error(self):
        with self.assertRaises(sl.ToolTableError):
            sl.parse_tool_table("T1 P1 Z-1.0\nT1 P2 Z-2.0")

    def test_blank_and_comment_lines_skipped(self):
        tools = sl.parse_tool_table("\n;just a comment\n\nT7 P7 Z-1.000000\n")
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["tool_number"], 7)


class TestSlotMapping(unittest.TestCase):
    """Mapping a parsed tool to a /sync `slots` entry (v2 sectioned schema).

    Assumptions (mirrors docs/TOOL_SCHEMA.md):
    - a slot carries only what a machine may OBSERVE: tool_number + plain
      offsets (z/x/y/diameter) with per-key `<key>_unit`; the server stamps
      provenance observed:linuxcnc@<machine>, so the slot sends no `source`
    - the slot NEVER sends internal/canonical keys, nor a bound_instance_id
      (binding is the server/inbox's job)
    - `data` is the opaque linuxcnc payload: the raw line + ALL parsed params
      (lossless), and becomes clients.linuxcnc.data on the server
    - `client_item_id` is the slot's stable handle, "<machine>:T<n>"
    """

    def test_slot_payload_shape(self):
        line = "T3 P3 D+6.350000 Z-48.250000 X+1.500000 ;1/4 downcut"
        tool = sl.parse_tool_table(line)[0]
        slot = sl.tool_to_slot(tool, "millstone", units="mm")

        self.assertEqual(slot["tool_number"], 3)
        self.assertAlmostEqual(slot["offsets"]["z"], -48.25)
        self.assertEqual(slot["offsets"]["z_unit"], "mm")
        self.assertAlmostEqual(slot["offsets"]["diameter"], 6.35)
        self.assertEqual(slot["client_item_id"], "millstone:T3")
        # opaque linuxcnc payload is lossless
        self.assertIn("raw", slot["data"])
        self.assertEqual(slot["data"]["params"]["x_offset"], 1.5)
        # in our lane: no canonical/internal/provenance, no premature binding
        self.assertNotIn("canonical", slot)
        self.assertNotIn("internal", slot)
        self.assertNotIn("provenance", slot)
        self.assertNotIn("bound_instance_id", slot)
        self.assertNotIn("source", slot["offsets"])

    def test_lathe_params_preserved_in_data(self):
        line = "T22 P22 Z-60.1 U+0.1 I+45.0 Q3 ;lathe"
        tool = sl.parse_tool_table(line)[0]
        slot = sl.tool_to_slot(tool, "millstone", units="mm")
        params = slot["data"]["params"]
        self.assertAlmostEqual(params["u_offset"], 0.1)
        self.assertAlmostEqual(params["front_angle"], 45.0)
        self.assertEqual(params["orientation"], 3)


class TestConfig(unittest.TestCase):

    def test_parse_shell_style_config(self):
        text = '\n'.join([
            '# comment',
            'SMOOTH_API_URL="http://nas.local:8000"',
            "SMOOTH_API_KEY='secret'",
            'MACHINE_NAME=mill01',
            '',
        ])
        cfg = sl.parse_config(text)
        self.assertEqual(cfg["SMOOTH_API_URL"], "http://nas.local:8000")
        self.assertEqual(cfg["SMOOTH_API_KEY"], "secret")
        self.assertEqual(cfg["MACHINE_NAME"], "mill01")

    def test_find_tool_table_from_ini(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ini = os.path.join(d, "mill.ini")
            with open(ini, "w") as f:
                f.write("[EMCIO]\nTOOL_TABLE = tool.tbl\n")
            with open(os.path.join(d, "tool.tbl"), "w") as f:
                f.write("T1 P1 Z-1.0\n")
            path = sl.find_tool_table(ini)
            self.assertEqual(path, os.path.join(d, "tool.tbl"))


class TestBackup(unittest.TestCase):

    def test_backup_creates_timestamped_copy(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "tool.tbl")
            with open(src, "w") as f:
                f.write("T1 P1 Z-1.0\n")
            backup = sl.backup_tool_table(src, d)
            self.assertTrue(os.path.exists(backup))
            self.assertNotEqual(backup, src)
            with open(backup) as f:
                self.assertEqual(f.read(), "T1 P1 Z-1.0\n")


if __name__ == "__main__":
    unittest.main()
