# MIT License
# Copyright (c) 2025 sliptonic
# SPDX-License-Identifier: MIT

"""
Tests for the full sync cycle (smooth-linuxcnc#2) - the pull path that
closes the wear-offset loop, retargeted to the v2 sectioned schema.

The contract under test (stdlib unittest, one stubbed HTTP seam):
- the client creates+names a MachineRecord on first run and persists the
  server internal.id in the state file (reused thereafter)
- 3-way per tool via the state file: local-only change pushes (mode=merge),
  server-only change on a BOUND entry (canonical.bound_instance_id not null)
  writes back to the .tbl (timestamped backup first), both-changed is a
  reported conflict that touches NEITHER side
- unbound entries never write back; comments/blank lines in the .tbl survive
  verbatim (line-surgical writes)
- a local deletion propagates as a snapshot /sync that omits the removed
  entry, so the server reconciles it away (removed_tool_numbers); a
  server-side-only addition is never reconciled
- idempotent: a second sync with no changes makes no HTTP write and no file
  write (change-detection short-circuit)
- benign failure: unreachable server logs and exits 0
"""
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
    """In-memory MachineRecord + ToolTableEntryRecords in the v2 sectioned
    shape. Models exactly the three endpoints the client uses:
    machine create + name-assert, the /sync push (snapshot|merge) returning
    items + removed_tool_numbers, and the GET returning sectioned entries.
    """

    def __init__(self):
        self.machine_id = "m-1"
        self.machine_name = "mill01"
        self.entries = {}  # tool_number -> sectioned entry
        self.syncs = 0
        self.tool_set = None  # the set bound to this machine, or None

    # --- helpers the tests drive -------------------------------------------

    def _src(self):
        return "observed:linuxcnc@%s" % self.machine_name

    def _make_entry(self, entry, bound_id=None):
        n = entry["tool_number"]
        offsets = {}
        for key in ("z", "x", "y", "diameter"):
            if entry["offsets"].get(key) is not None:
                offsets[key] = {"value": entry["offsets"][key],
                                "unit": entry["offsets"].get(key + "_unit"),
                                "source": self._src()}
        bound = ({"value": bound_id, "source": "asserted:human@inbox"}
                 if bound_id else {"value": None, "source": "unknown"})
        return {
            "internal": {"id": "e-%d" % n, "machine_id": self.machine_id,
                         "version": 1, "created_at": "t", "updated_at": "t"},
            "canonical": {
                "tool_number": {"value": n, "source": self._src()},
                "bound_instance_id": bound,
                "offsets": offsets,
            },
            "clients": {"linuxcnc": {
                "client_version": entry.get("client_version", sl.CLIENT_VERSION),
                "client_item_id": entry.get("client_item_id"),
                "created_at": "t", "updated_at": "t",
                "data": entry.get("data", {}),
            }},
        }

    def server_only_entry(self, n, z=None):
        """An entry present on the server with NO linuxcnc client section
        (e.g. created by another client) - the client must never delete it."""
        offsets = {}
        if z is not None:
            offsets["z"] = {"value": z, "unit": "mm", "source": self._src()}
        return {
            "internal": {"id": "e-%d" % n, "machine_id": self.machine_id,
                         "version": 1, "created_at": "t", "updated_at": "t"},
            "canonical": {
                "tool_number": {"value": n, "source": self._src()},
                "bound_instance_id": {"value": None, "source": "unknown"},
                "offsets": offsets,
            },
            "clients": {},
        }

    def entry(self, n):
        return self.entries[n]

    def offset(self, n, key):
        return self.entries[n]["canonical"]["offsets"][key]["value"]

    def is_bound(self, n):
        return self.entries[n]["canonical"]["bound_instance_id"]["value"] is not None

    def bind(self, n, instance_id="inst-1"):
        e = self.entries[n]
        e["canonical"]["bound_instance_id"] = {"value": instance_id,
                                               "source": "asserted:human@inbox"}

    def set_offset(self, n, key, value):
        e = self.entries[n]
        e["canonical"]["offsets"][key] = {"value": value, "unit": "mm",
                                          "source": self._src()}
        e["internal"]["version"] += 1

    def make_set(self, members, name="millstone"):
        """Bind a tool set to this machine. `members` is a list of
        (tool_record_id, number, state) - number/state may be None.  A None
        number is an unknown preference; an int is an asserted preference."""
        out = []
        for spec in members:
            tool_record_id, number, state = (list(spec) + [None, None])[:3]
            num = ({"value": number, "source": "asserted:freecad@bob"}
                   if number is not None else {"value": None, "source": "unknown"})
            member = {"tool_record_id": tool_record_id, "number": num}
            if state is not None:
                member["state"] = state
            out.append(member)
        self.tool_set = {
            "internal": {"id": "set-1", "version": 1,
                         "created_at": "t", "updated_at": "t"},
            "canonical": {
                "name": {"value": name, "source": "asserted:freecad@bob"},
                "machine_id": {"value": self.machine_id,
                               "source": "asserted:freecad@bob"},
                "members": out,
            },
            "clients": {},
        }

    # --- the wire ----------------------------------------------------------

    def http(self, method, url, api_key, body=None, timeout=10):
        if method == "POST" and url.endswith("/api/v1/machine-records"):
            return {"internal": {"id": self.machine_id, "version": 1},
                    "canonical": {}, "clients": {}}
        if method == "POST" and url.endswith("/assert"):
            self.machine_name = body["value"]
            return {"internal": {"id": self.machine_id},
                    "canonical": {"name": {"value": body["value"],
                                           "source": "asserted:linuxcnc"}},
                    "clients": {}}
        if method == "GET" and "/api/v1/machine-records/" in url:
            if url.rstrip("/").endswith(self.machine_id):
                return {"internal": {"id": self.machine_id, "version": 1},
                        "canonical": {}, "clients": {}}
            raise sl.ServerError(404, "not found")
        if method == "GET" and "/api/v1/tool-set-records" in url:
            return {"items": [self.tool_set] if self.tool_set else []}
        if method == "GET" and "/api/v1/tool-table-entry-records" in url:
            return {"items": list(self.entries.values())}
        if method == "POST" and url.endswith("/tool-table-entry-records/sync"):
            self.syncs += 1
            mode = body.get("mode", "merge")
            seen = set()
            out = []
            for entry in body["entries"]:
                n = entry["tool_number"]
                seen.add(n)
                if n in self.entries:
                    prev = self.entries[n]
                    bound_id = prev["canonical"]["bound_instance_id"]["value"]
                    new = self._make_entry(entry, bound_id=bound_id)
                    new["internal"]["version"] = prev["internal"]["version"] + 1
                    self.entries[n] = new
                else:
                    self.entries[n] = self._make_entry(entry)
                out.append(self.entries[n])
            removed = []
            if mode == "snapshot":
                # reconcile away only THIS client's entries that we omitted;
                # server-side-only entries (no linuxcnc section) are untouched
                for n in list(self.entries):
                    if n in seen:
                        continue
                    if "linuxcnc" in self.entries[n].get("clients", {}):
                        del self.entries[n]
                        removed.append(n)
            return {"items": out, "removed_tool_numbers": sorted(removed)}
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
        # the machine_id is persisted for reuse next run
        import json
        with open(os.path.join(self.dir, state_files[0])) as f:
            self.assertEqual(json.load(f)["machine_id"], "m-1")

    def test_idempotent_no_changes_no_writes(self):
        self.run_sync()
        syncs_before = self.server.syncs
        self.run_sync()
        self.assertEqual(self.server.syncs, syncs_before)  # short-circuit
        self.assertEqual(self.read_tbl(), TBL)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".bak")], [])

    def test_local_edit_pushes(self):
        self.run_sync()
        with open(self.tbl, "w") as f:
            f.write(TBL.replace("Z-48.250000", "Z-48.100000"))
        self.run_sync()
        self.assertAlmostEqual(self.server.offset(3, "z"), -48.1)

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
        syncs = self.server.syncs
        self.run_sync()
        self.assertEqual(self.server.syncs, syncs)

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
        self.assertAlmostEqual(self.server.offset(3, "z"), -48.007)  # server kept
        self.assertTrue(any("CONFLICT" in m and "T3" in m for m in logs))
        # T5 unaffected: still syncs normally
        self.assertIn(5, self.server.entries)

    def test_unreachable_server_is_benign(self):
        def dead(*a, **k):
            raise sl.ServerUnreachable("down")
        with mock.patch.object(sl, "http_json", dead):
            self.assertEqual(sl.sync_tool_table(self.cfg), 0)

    def seed_server_from_local(self, bind=False):
        """Simulate the server already holding this table (an earlier push),
        optionally with every entry bound - but with NO local sync state."""
        for t in sl.parse_tool_table(self.read_tbl()):
            entry = sl.tool_to_entry(t, "mill01", units="mm")
            n = entry["tool_number"]
            self.server.entries[n] = self.server._make_entry(
                entry, bound_id=("inst-%d" % n) if bind else None)

    def test_first_sync_when_server_already_has_bound_entries_is_in_sync(self):
        """Controller pushed earlier (no sync state), records were created and
        bound on the server. The FIRST sync must NOT flag every row as a
        conflict - local and server agree, so it's a clean no-op."""
        self.seed_server_from_local(bind=True)
        logs = []
        with mock.patch.object(sl, "http_json", self.server.http), \
             mock.patch.object(sl, "log", logs.append):
            code = sl.sync_tool_table(self.cfg)
        self.assertEqual(code, 0)
        self.assertFalse(any("CONFLICT" in m for m in logs), logs)
        self.assertTrue(any("In sync" in m for m in logs))
        self.assertEqual(self.server.syncs, 0)       # nothing pushed
        self.assertEqual(self.read_tbl(), TBL)       # nothing written
        # the bindings survive (we never touched the entries)
        self.assertTrue(all(self.server.is_bound(n) for n in self.server.entries))

    def test_first_contact_genuine_divergence_is_still_a_conflict(self):
        """No baseline AND the fields actually differ: that we cannot
        arbitrate, so it stays a conflict - but only for the row that differs."""
        self.seed_server_from_local(bind=True)
        self.server.set_offset(3, "z", -47.0)  # server's T3 edited before first sync
        logs = []
        with mock.patch.object(sl, "http_json", self.server.http), \
             mock.patch.object(sl, "log", logs.append):
            sl.sync_tool_table(self.cfg)
        self.assertTrue(any("CONFLICT" in m and "T3" in m for m in logs))
        self.assertFalse(any("CONFLICT" in m and "T5" in m for m in logs))  # T5 agreed

    def test_local_delete_removes_entry_on_server(self):
        """The operator removed a tool from tool.tbl: the next sync deletes it
        on the server (snapshot reconcile) instead of leaving a phantom."""
        self.run_sync()
        self.assertEqual(sorted(self.server.entries), [3, 5])
        # operator deletes T3 from the controller's table
        with open(self.tbl, "w") as f:
            f.write("T5 P5 D+5.000000 Z-50.000000 ;5mm drill\n")
        logs = []
        with mock.patch.object(sl, "http_json", self.server.http), \
             mock.patch.object(sl, "log", logs.append):
            code = sl.sync_tool_table(self.cfg)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(self.server.entries), [5])  # T3 gone
        self.assertTrue(any("Removed" in m and "T3" in m for m in logs))
        # converges: a second sync is a clean no-op (state forgot T3)
        with mock.patch.object(sl, "http_json", self.server.http), \
             mock.patch.object(sl, "log", logs.append):
            sl.sync_tool_table(self.cfg)
        self.assertEqual(sorted(self.server.entries), [5])

    def test_local_delete_vs_server_edit_is_conflict(self):
        """A tool deleted locally but edited on the server is a real conflict:
        touch neither side (never silently clobber the server edit)."""
        self.run_sync()
        self.server.set_offset(3, "z", -48.009)  # server edited T3
        with open(self.tbl, "w") as f:            # operator deleted T3 locally
            f.write("T5 P5 D+5.000000 Z-50.000000 ;5mm drill\n")
        logs = []
        with mock.patch.object(sl, "http_json", self.server.http), \
             mock.patch.object(sl, "log", logs.append):
            sl.sync_tool_table(self.cfg)
        self.assertIn(3, self.server.entries)  # NOT deleted
        self.assertTrue(any("CONFLICT" in m and "T3" in m for m in logs))

    def test_server_only_entry_never_synced_is_not_deleted(self):
        """A tool present on the server but never in our local table/baseline
        is a server-side addition, not a local deletion - left untouched."""
        self.run_sync()  # baseline now knows T3, T5
        # A third tool appears on the server that we never had locally.
        self.server.entries[9] = self.server.server_only_entry(9, z=-1.0)
        logs = []
        with mock.patch.object(sl, "http_json", self.server.http), \
             mock.patch.object(sl, "log", logs.append):
            sl.sync_tool_table(self.cfg)
        self.assertIn(9, self.server.entries)  # not deleted
        self.assertTrue(any("not added automatically" in m and "T9" in m for m in logs))


class BindingThenEditTest(SyncCycleTest):
    """Regression (found live): confirming a binding bumps the record version;
    that must NOT make the next local touch-off a false conflict (we detect
    change on canonical offsets, never on version)."""

    def test_bind_between_syncs_then_local_edit_pushes_cleanly(self):
        self.run_sync()
        self.server.bind(3)  # inbox confirm: version bumps, offsets unchanged
        self.server.entries[3]["internal"]["version"] += 1
        with open(self.tbl, "w") as f:
            f.write(TBL.replace("Z-48.250000", "Z-48.137000"))
        logs = []
        with mock.patch.object(sl, "http_json", self.server.http), \
             mock.patch.object(sl, "log", logs.append):
            sl.sync_tool_table(self.cfg)
        self.assertFalse(any("CONFLICT" in m for m in logs), logs)
        self.assertAlmostEqual(self.server.offset(3, "z"), -48.137)


class RequestedMembersTest(SyncCycleTest):
    """smooth-linuxcnc#3: the controller surfaces set members the operator
    must still mount (requested) and members mounted-but-not-yet-confirmed
    (pending bind), folding them into the in-sync summary. It NEVER edits the
    .tbl for a requested tool (fulfilment is the operator's mount + the
    existing merge push)."""

    def sync_with_logs(self):
        logs = []
        with mock.patch.object(sl, "http_json", self.server.http), \
             mock.patch.object(sl, "log", logs.append):
            code = sl.sync_tool_table(self.cfg)
        return code, logs

    def bound_for_local(self):
        """Bind every local entry to an instance and return the id map."""
        ids = {}
        for n in self.server.entries:
            iid = "inst-%d" % n
            self.server.bind(n, iid)
            ids[n] = iid
        return ids

    def test_requested_member_is_reported_with_pocket(self):
        """A set member whose instance is not bound to any entry is requested;
        with an asserted preferred number, the report names the pocket."""
        self.run_sync()                       # server now has T3, T5
        ids = self.bound_for_local()
        # an 18th tool the machine doesn't have yet, preferred pocket 8
        self.server.make_set([(ids[3], None, None), (ids[5], None, None),
                              ("inst-new", 8, None)])
        code, logs = self.sync_with_logs()
        self.assertEqual(code, 0)
        summary = [m for m in logs if "requested" in m]
        self.assertTrue(summary, logs)
        self.assertIn("1 tool requested", summary[0])
        self.assertIn('"inst-new"', summary[0])
        self.assertIn("assign pocket 8", summary[0])
        # NOT reported as "nothing to do" and the .tbl is untouched
        self.assertFalse(any("nothing to do" in m for m in logs))
        self.assertEqual(self.read_tbl(), TBL)

    def test_pocket_clause_omitted_when_number_unknown(self):
        """A requested member with no asserted preferred number names no
        pocket - the operator chooses one."""
        self.run_sync()
        ids = self.bound_for_local()
        self.server.make_set([(ids[3], None, None), (ids[5], None, None),
                              ("inst-new", None, None)])
        code, logs = self.sync_with_logs()
        summary = [m for m in logs if "requested" in m][0]
        self.assertIn('"inst-new"', summary)
        self.assertIn("assign a pocket", summary)
        self.assertNotIn("pocket 8", summary)
        self.assertNotIn("assign pocket ", summary)

    def test_all_members_bound_reports_no_request(self):
        """When every member's instance is bound to a machine entry, there is
        no request - the sync reads as a clean in-sync no-op."""
        self.run_sync()
        ids = self.bound_for_local()
        self.server.make_set([(ids[3], None, None), (ids[5], None, None)])
        code, logs = self.sync_with_logs()
        self.assertEqual(code, 0)
        self.assertFalse(any("requested" in m for m in logs), logs)
        self.assertTrue(any("In sync" in m for m in logs))

    def test_in_sync_summary_accounts_for_request(self):
        """An outstanding request must not read as 'nothing to do'; the summary
        counts the in-sync tools alongside the request."""
        self.run_sync()
        ids = self.bound_for_local()
        self.server.make_set([(ids[3], None, None), (ids[5], None, None),
                              ("inst-new", 8, None)])
        code, logs = self.sync_with_logs()
        self.assertFalse(any("nothing to do" in m for m in logs), logs)
        self.assertTrue(any("2 tools in sync" in m and "requested" in m
                            for m in logs), logs)

    def test_pending_bind_reported(self):
        """A member the server marks 'pending bind' (mounted, binding not yet
        confirmed) is reported as pending bind, not as a request."""
        self.run_sync()
        ids = self.bound_for_local()
        self.server.make_set([(ids[3], None, None), (ids[5], None, None),
                              ("inst-new", 8, "pending bind")])
        code, logs = self.sync_with_logs()
        self.assertTrue(any("pending bind" in m for m in logs), logs)
        self.assertFalse(any("requested" in m for m in logs), logs)

    def test_no_bound_set_behaves_as_before(self):
        """A machine with no bound set: no set fetch result, no request line."""
        self.run_sync()
        code, logs = self.sync_with_logs()
        self.assertEqual(code, 0)
        self.assertFalse(any("requested" in m for m in logs))
        self.assertTrue(any("In sync" in m for m in logs))

    def test_set_endpoint_404_is_swallowed(self):
        """A 404 from the set endpoint (no set / unsupported filter) does not
        abort the sync; it behaves as a setless machine."""
        self.run_sync()
        real_http = self.server.http

        def http(method, url, api_key, body=None, timeout=10):
            if method == "GET" and "/api/v1/tool-set-records" in url:
                raise sl.ServerError(404, "not found")
            return real_http(method, url, api_key, body, timeout)
        logs = []
        with mock.patch.object(sl, "http_json", http), \
             mock.patch.object(sl, "log", logs.append):
            code = sl.sync_tool_table(self.cfg)
        self.assertEqual(code, 0)
        self.assertTrue(any("In sync" in m for m in logs))


if __name__ == "__main__":
    unittest.main()
