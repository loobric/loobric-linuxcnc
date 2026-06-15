# MIT License
# Copyright (c) 2025 sliptonic
# SPDX-License-Identifier: MIT

"""Push-flow tests with a stubbed HTTP transport (stdlib unittest + mock).

Assumptions (v2 sectioned schema):
- All server traffic goes through smooth_linuxcnc.http_json(), so tests
  stub exactly one seam
- push_tool_table() creates+names a MachineRecord on first run (POST
  /machine-records then POST .../assert), PERSISTS the returned internal.id
  in the state file, and reuses it on later runs
- the whole table is pushed in ONE /sync call (mode=snapshot), which returns
  {"items": [...], "removed_tool_numbers": [...]}
- Benign failure: an unreachable server logs and returns exit code 0
  (cron must never block or spam); usage/config errors return 2
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import smooth_linuxcnc as sl

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sample.tbl")


def sectioned(slot, machine_id="m-1", machine="mill01"):
    """Build a server sectioned entry from a pushed slot."""
    src = "observed:linuxcnc@%s" % machine
    offsets = {}
    for key in ("z", "x", "y", "diameter"):
        if slot["offsets"].get(key) is not None:
            offsets[key] = {"value": slot["offsets"][key],
                            "unit": slot["offsets"].get(key + "_unit"),
                            "source": src}
    return {
        "internal": {"id": "e-%d" % slot["tool_number"], "machine_id": machine_id,
                     "version": 1, "created_at": "t", "updated_at": "t"},
        "canonical": {
            "tool_number": {"value": slot["tool_number"], "source": src},
            "bound_instance_id": {"value": None, "source": "unknown"},
            "offsets": offsets,
        },
        "clients": {"linuxcnc": {
            "client_version": sl.CLIENT_VERSION,
            "client_item_id": slot.get("client_item_id"),
            "created_at": "t", "updated_at": "t",
            "data": slot.get("data", {}),
        }},
    }


class TestPushFlow(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = {
            "SMOOTH_API_URL": "http://nas.local:8000",
            "SMOOTH_API_KEY": "k",
            "MACHINE_NAME": "mill01",
            "TOOL_TABLE": FIXTURE,
            "STATE_DIR": self.dir,
        }

    def test_push_creates_names_machine_then_syncs_table(self):
        calls = []

        def fake_http(method, url, api_key, body=None, timeout=10):
            calls.append((method, url, body))
            if method == "POST" and url.endswith("/api/v1/machine-records"):
                return {"internal": {"id": "m-1", "version": 1},
                        "canonical": {}, "clients": {}}
            if method == "POST" and url.endswith("/api/v1/machine-records/m-1/assert"):
                return {"internal": {"id": "m-1"},
                        "canonical": {"name": {"value": body["value"],
                                               "source": "asserted:linuxcnc"}},
                        "clients": {}}
            if method == "GET" and "/api/v1/machine-records/" in url:
                if url.rstrip("/").endswith("m-1"):
                    return {"internal": {"id": "m-1", "version": 1},
                            "canonical": {}, "clients": {}}
                raise sl.ServerError(404, "not found")
            if method == "POST" and url.endswith("/api/v1/tool-table-entry-records/sync"):
                self.assertEqual(body["mode"], "snapshot")
                self.assertEqual(body["machine_id"], "m-1")
                return {"items": [sectioned(s) for s in body["slots"]],
                        "removed_tool_numbers": []}
            raise AssertionError("unexpected call: %s %s" % (method, url))

        with mock.patch.object(sl, "http_json", fake_http):
            code = sl.push_tool_table(self.cfg)

        self.assertEqual(code, 0)
        methods = [(c[0], c[1].rsplit("/api/v1/", 1)[-1]) for c in calls]
        self.assertEqual(methods, [
            ("POST", "machine-records"),
            ("POST", "machine-records/m-1/assert"),
            ("POST", "tool-table-entry-records/sync"),
        ])
        # the name assert carries the actor + path/value the API expects
        assert_body = calls[1][2]
        self.assertEqual(assert_body, {"path": "name", "value": "mill01",
                                       "actor": "linuxcnc"})
        sync_body = calls[2][2]
        self.assertEqual(len(sync_body["slots"]), 4)
        self.assertEqual(sync_body["slots"][1]["tool_number"], 3)
        # slots push in our lane: no canonical/bound identity is asserted
        self.assertNotIn("bound_instance_id", sync_body["slots"][1])
        self.assertNotIn("canonical", sync_body["slots"][1])

    def test_push_persists_and_reuses_machine_id(self):
        # first push creates + names the machine, persisting its id
        def first(method, url, api_key, body=None, timeout=10):
            if url.endswith("/machine-records"):
                return {"internal": {"id": "m-7"}, "canonical": {}, "clients": {}}
            if url.endswith("/assert"):
                return {"internal": {"id": "m-7"}, "canonical": {}, "clients": {}}
            if url.endswith("/sync"):
                return {"items": [], "removed_tool_numbers": []}
            raise AssertionError(url)

        with mock.patch.object(sl, "http_json", first):
            self.assertEqual(sl.push_tool_table(self.cfg), 0)

        # second push must NOT create/name again - it reuses the stored id
        calls = []

        def second(method, url, api_key, body=None, timeout=10):
            calls.append((method, url))
            if url.endswith("/machine-records") or url.endswith("/assert"):
                raise AssertionError("should not re-create machine: %s" % url)
            if method == "GET" and "/machine-records/" in url:   # verify it still exists
                return {"internal": {"id": "m-7"}, "canonical": {}, "clients": {}}
            if url.endswith("/sync"):
                self.assertEqual(body["machine_id"], "m-7")
                return {"items": [], "removed_tool_numbers": []}
            raise AssertionError(url)

        with mock.patch.object(sl, "http_json", second):
            self.assertEqual(sl.push_tool_table(self.cfg), 0)
        self.assertEqual([c[0] for c in calls], ["GET", "POST"])  # verify, then /sync

    def test_recreates_machine_when_stored_id_is_gone(self):
        """The stored machine was deleted (UI) or the DB reset: a stale id would
        push slots into a ghost machine the UI can't show. The client detects the
        404 and re-registers, so the table lands under a LIVE machine."""
        state_file = sl._state_path(self.cfg, self.cfg["MACHINE_NAME"])
        sl._save_state(state_file, {"machine_id": "m-dead", "tools": {}})
        calls = []

        def http(method, url, api_key, body=None, timeout=10):
            calls.append((method, url))
            if method == "GET" and url.endswith("/machine-records/m-dead"):
                raise sl.ServerError(404, "not found")           # the ghost machine
            if method == "POST" and url.endswith("/machine-records"):
                return {"internal": {"id": "m-new"}, "canonical": {}, "clients": {}}
            if url.endswith("/assert"):
                return {"internal": {"id": "m-new"}, "canonical": {}, "clients": {}}
            if url.endswith("/sync"):
                self.assertEqual(body["machine_id"], "m-new")    # synced under the LIVE machine
                return {"items": [], "removed_tool_numbers": []}
            raise AssertionError(url)

        with mock.patch.object(sl, "http_json", http):
            self.assertEqual(sl.push_tool_table(self.cfg), 0)
        self.assertTrue(calls[0][1].endswith("/machine-records/m-dead"))   # verified the ghost
        self.assertTrue(any(u.endswith("/machine-records") for _, u in calls))  # re-created

    def test_snapshot_reconcile_removes_are_logged(self):
        logs = []

        def fake_http(method, url, api_key, body=None, timeout=10):
            if url.endswith("/machine-records"):
                return {"internal": {"id": "m-1"}, "canonical": {}, "clients": {}}
            if url.endswith("/assert"):
                return {"internal": {"id": "m-1"}, "canonical": {}, "clients": {}}
            if url.endswith("/sync"):
                return {"items": [sectioned(s) for s in body["slots"]],
                        "removed_tool_numbers": [99]}
            raise AssertionError(url)

        with mock.patch.object(sl, "http_json", fake_http), \
             mock.patch.object(sl, "log", logs.append):
            self.assertEqual(sl.push_tool_table(self.cfg), 0)
        self.assertTrue(any("Reconciled" in m and "T99" in m for m in logs))

    def test_unreachable_server_is_benign(self):
        def fake_http(method, url, api_key, body=None, timeout=10):
            raise sl.ServerUnreachable("connection refused")

        with mock.patch.object(sl, "http_json", fake_http):
            code = sl.push_tool_table(self.cfg)
        self.assertEqual(code, 0)

    def test_missing_config_is_usage_error(self):
        code = sl.push_tool_table({"SMOOTH_API_URL": "http://x"})  # no machine/table
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
