# MIT License
# Copyright (c) 2025 sliptonic
# SPDX-License-Identifier: MIT

"""Push-flow tests with a stubbed HTTP transport (stdlib unittest + mock).

Assumptions:
- All server traffic goes through smooth_linuxcnc.http_json(), so tests
  stub exactly one seam
- push_tool_table() ensures the machine exists (find by name, create if
  missing), then PUTs the table
- Benign failure: an unreachable server logs and returns exit code 0
  (cron must never block or spam); usage/config errors return 2
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import smooth_linuxcnc as sl

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sample.tbl")

CFG = {
    "SMOOTH_API_URL": "http://nas.local:8000",
    "SMOOTH_API_KEY": "k",
    "MACHINE_NAME": "mill01",
    "TOOL_TABLE": FIXTURE,
}


class TestPushFlow(unittest.TestCase):

    def test_push_creates_machine_then_puts_table(self):
        calls = []

        def fake_http(method, url, api_key, body=None, timeout=10):
            calls.append((method, url, body))
            if method == "GET" and url.endswith("/api/v1/machines"):
                return {"items": []}
            if method == "POST" and url.endswith("/api/v1/machines"):
                return {"success_count": 1, "errors": [],
                        "items": [{"id": "m-1", "name": "mill01"}]}
            if method == "PUT" and url.endswith("/api/v1/machines/m-1/tool-table"):
                return {"success_count": len(body["items"]), "errors": [], "items": body["items"]}
            raise AssertionError("unexpected call: %s %s" % (method, url))

        with mock.patch.object(sl, "http_json", fake_http):
            code = sl.push_tool_table(CFG)

        self.assertEqual(code, 0)
        methods = [c[0] for c in calls]
        self.assertEqual(methods, ["GET", "POST", "PUT"])
        put_body = calls[2][2]
        self.assertEqual(len(put_body["items"]), 4)
        self.assertEqual(put_body["items"][1]["tool_number"], 3)
        # entries are pushed unbound; binding is the server/inbox's job
        self.assertNotIn("tool_record_id", put_body["items"][1])

    def test_push_reuses_existing_machine(self):
        calls = []

        def fake_http(method, url, api_key, body=None, timeout=10):
            calls.append((method, url))
            if method == "GET" and url.endswith("/api/v1/machines"):
                return {"items": [{"id": "m-9", "name": "mill01"}]}
            if method == "PUT" and url.endswith("/api/v1/machines/m-9/tool-table"):
                return {"success_count": 4, "errors": [], "items": []}
            raise AssertionError("unexpected call: %s %s" % (method, url))

        with mock.patch.object(sl, "http_json", fake_http):
            self.assertEqual(sl.push_tool_table(CFG), 0)
        self.assertEqual([c[0] for c in calls], ["GET", "PUT"])

    def test_unreachable_server_is_benign(self):
        def fake_http(method, url, api_key, body=None, timeout=10):
            raise sl.ServerUnreachable("connection refused")

        with mock.patch.object(sl, "http_json", fake_http):
            code = sl.push_tool_table(CFG)
        self.assertEqual(code, 0)

    def test_missing_config_is_usage_error(self):
        code = sl.push_tool_table({"SMOOTH_API_URL": "http://x"})  # no key/machine/table
        self.assertEqual(code, 2)

    def test_server_side_item_errors_are_logged_not_fatal(self):
        def fake_http(method, url, api_key, body=None, timeout=10):
            if method == "GET":
                return {"items": [{"id": "m-1", "name": "mill01"}]}
            return {"success_count": 3,
                    "errors": [{"index": 0, "message": "boom"}], "items": []}

        with mock.patch.object(sl, "http_json", fake_http):
            self.assertEqual(sl.push_tool_table(CFG), 0)


if __name__ == "__main__":
    unittest.main()
