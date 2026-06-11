# MIT License
# Copyright (c) 2025 sliptonic
# SPDX-License-Identifier: MIT

"""
Tests for the full sync cycle (smooth-linuxcnc#2) — the pull path that
closes the wear-offset loop.

The contract under test (stdlib unittest, one stubbed HTTP seam):
- 3-way per tool via the state file: local-only change pushes,
  server-only change on a BOUND entry writes back to the .tbl
  (timestamped backup first), both-changed is a reported conflict that
  touches NEITHER side
- unbound entries never write back; comments/blank lines in the .tbl
  survive verbatim (line-surgical writes)
- idempotent: a second sync with no changes makes no HTTP write and no
  file write (change-detection short-circuit)
- benign failure: unreachable server logs and exits 0
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import smooth_linuxcnc as sl

TBL = """;millstone tool table - hands off the comments
T3 P3 D+6.350000 Z-48.250000 ;1/4 downcut

T5 P5 D+5.000000 Z-50.000000 ;5mm drill
"""


class FakeServer:
    """In-memory machine + tool-table honoring the facade contract."""

    def __init__(self):
        self.machine = {"id": "m-1", "name": "mill01"}
        self.entries = {}  # tool_number -> entry
        self.puts = 0

    def entry(self, tool_number):
        return self.entries[tool_number]

    def bind(self, tool_number, record_id="rec-1"):
        self.entries[tool_number]["tool_record_id"] = record_id

    def set_offset(self, tool_number, key, value):
        e = self.entries[tool_number]
        e["offsets"] = dict(e["offsets"], **{key: value})
        e["version"] += 1

    def http(self, method, url, api_key, body=None, timeout=10):
        if method == "GET" and url.endswith("/api/v1/machines"):
            return {"items": [self.machine]}
        if method == "GET" and url.endswith("/api/v1/machines/m-1/tool-table"):
            return {"items": list(self.entries.values())}
        if method == "PUT" and url.endswith("/api/v1/machines/m-1/tool-table"):
            self.puts += 1
            out = []
            for item in body["items"]:
                n = item["tool_number"]
                if n in self.entries:
                    current = self.entries[n]
                    keep_binding = current.get("tool_record_id")
                    current.update(item)
                    if item.get("tool_record_id") is None and keep_binding:
                        current["tool_record_id"] = keep_binding
                    current["version"] += 1
                else:
                    self.entries[n] = {**item, "id": "e-%d" % n,
                                       "version": 1, "machine_id": "m-1",
                                       "tool_record_id": item.get("tool_record_id")}
                out.append(self.entries[n])
            return {"success_count": len(out), "errors": [], "items": out}
        raise AssertionError("unexpected: %s %s" % (method, url))


class SyncCycleTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.tbl = os.path.join(self.dir, "tool.tbl")
        with open(self.tbl, "w") as f:
            f.write(TBL)
        self.server = FakeServer()
        self.cfg = {
            "SMOOTH_API_URL": "http://x", "MACHINE_NAME": "mill01",
            "TOOL_TABLE": self.tbl, "STATE_DIR": self.dir,
            "LOG_DIR": "",
        }

    def run_sync(self):
        with mock.patch.object(sl, "http_json", self.server.http):
            return sl.sync_tool_table(self.cfg)

    def read_tbl(self):
        with open(self.tbl) as f:
            return f.read()

    def test_first_sync_pushes_all_and_writes_state(self):
        code = self.run_sync()
        self.assertEqual(code, 0)
        self.assertEqual(sorted(self.server.entries), [3, 5])
        self.assertEqual(self.read_tbl(), TBL)  # pull changed nothing
        state_files = [n for n in os.listdir(self.dir) if "state" in n]
        self.assertEqual(len(state_files), 1)

    def test_idempotent_no_changes_no_writes(self):
        self.run_sync()
        puts_before = self.server.puts
        self.run_sync()
        self.assertEqual(self.server.puts, puts_before)  # short-circuit
        self.assertEqual(self.read_tbl(), TBL)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".bak")], [])

    def test_local_edit_pushes(self):
        self.run_sync()
        with open(self.tbl, "w") as f:
            f.write(TBL.replace("Z-48.250000", "Z-48.100000"))
        self.run_sync()
        self.assertAlmostEqual(self.server.entry(3)["offsets"]["z"], -48.1)

    def test_server_edit_on_bound_entry_writes_back_with_backup(self):
        self.run_sync()
        self.server.bind(3)
        self.server.set_offset(3, "z", -48.007)
        code = self.run_sync()
        self.assertEqual(code, 0)
        content = self.read_tbl()
        self.assertIn("Z-48.007000", content)
        self.assertIn(";millstone tool table - hands off the comments", content)
        self.assertIn("T5 P5 D+5.000000 Z-50.000000", content)  # untouched line
        backups = [n for n in os.listdir(self.dir) if ".bak" in n]
        self.assertEqual(len(backups), 1)
        # and the cycle converges: next sync is a no-op
        puts = self.server.puts
        self.run_sync()
        self.assertEqual(self.server.puts, puts)

    def test_server_edit_on_unbound_entry_never_writes_back(self):
        self.run_sync()
        self.server.set_offset(3, "z", -47.0)  # NOT bound
        self.run_sync()
        self.assertIn("Z-48.250000", self.read_tbl())  # local untouched

    def test_both_changed_is_conflict_touching_neither(self):
        self.run_sync()
        self.server.bind(3)
        self.server.set_offset(3, "z", -48.007)          # server change
        with open(self.tbl, "w") as f:                    # local change
            f.write(TBL.replace("Z-48.250000", "Z-48.300000"))
        logs = []
        with mock.patch.object(sl, "http_json", self.server.http), \
             mock.patch.object(sl, "log", logs.append):
            code = sl.sync_tool_table(self.cfg)
        self.assertEqual(code, 0)
        self.assertIn("Z-48.300000", self.read_tbl())     # local kept
        self.assertAlmostEqual(self.server.entry(3)["offsets"]["z"], -48.007)  # server kept
        self.assertTrue(any("CONFLICT" in m and "T3" in m for m in logs))
        # T5 unaffected: still syncs normally
        self.assertIn(5, self.server.entries)

    def test_unreachable_server_is_benign(self):
        def dead(*a, **k):
            raise sl.ServerUnreachable("down")
        with mock.patch.object(sl, "http_json", dead):
            self.assertEqual(sl.sync_tool_table(self.cfg), 0)


if __name__ == "__main__":
    unittest.main()


class BindingThenEditTest(SyncCycleTest):
    """Regression (found live): confirming a binding bumps entry.version;
    that must NOT make the next local touch-off a false conflict."""

    def test_bind_between_syncs_then_local_edit_pushes_cleanly(self):
        self.run_sync()
        self.server.bind(3)  # inbox confirm: version bumps, data unchanged
        self.server.entries[3]["version"] += 1
        with open(self.tbl, "w") as f:
            f.write(TBL.replace("Z-48.250000", "Z-48.137000"))
        logs = []
        with mock.patch.object(sl, "http_json", self.server.http), \
             mock.patch.object(sl, "log", logs.append):
            sl.sync_tool_table(self.cfg)
        self.assertFalse(any("CONFLICT" in m for m in logs), logs)
        self.assertAlmostEqual(self.server.entry(3)["offsets"]["z"], -48.137)
