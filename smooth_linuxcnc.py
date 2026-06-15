#!/usr/bin/env python3
# MIT License
# Copyright (c) 2025 sliptonic
# SPDX-License-Identifier: MIT

"""
Smooth LinuxCNC client - push your tool table to a Smooth server.

SINGLE FILE, STANDARD LIBRARY ONLY. LinuxCNC control boxes are often
image-built on old distributions; this script must run on any Python 3.6+
with no pip installs and must NEVER block or break the machine:

- Server unreachable / server error  -> log it, exit 0 (cron-safe)
- Bad usage or missing configuration -> log it, exit 2

Usage:
    smooth_linuxcnc.py sync [machine-name]   # full cycle: push + pull (cron this)
    smooth_linuxcnc.py push [machine-name]   # one-way: table -> server only

Configuration: ~/.config/smooth/linuxcnc.conf (shell-style KEY="value",
compatible with the v1 format), overridable via environment variables:

    SMOOTH_API_URL="http://nas.local:8000"
    SMOOTH_API_KEY="your-api-key"      # not needed against a solo-mode server
    MACHINE_NAME="mill01"              # or pass as CLI argument
    TOOL_TABLE="/path/to/tool.tbl"     # or LINUXCNC_INI to discover it
    LINUXCNC_INI="/path/to/mill.ini"
    UNITS="mm"                         # offsets unit, default mm
    LOG_DIR="/tmp/smooth-sync"         # optional log file location

Tool table reference: http://wiki.linuxcnc.org/cgi-bin/wiki.pl?ToolTable
Entries are pushed UNBOUND; binding tool-table rows to ToolInstanceRecords is
the server's job (review inbox) or the user's (explicit assert). This script
never guesses identity (v2 decision G2).

v2 sectioned schema (docs/TOOL_SCHEMA.md): this client only ever writes its
own `clients.linuxcnc` section plus the few canonical fields a machine may
OBSERVE (tool_number, offsets). It never sends `internal`/`canonical` keys;
the server stamps provenance `observed:linuxcnc@<machine>` itself. Canonical
offsets read back as `canonical.offsets.<key>.{value,unit,source}`, and a slot
is "bound" when `canonical.bound_instance_id.value` is not null.
"""

import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/smooth/linuxcnc.conf")
HTTP_TIMEOUT = 10  # seconds
CLIENT_NAME = "linuxcnc"
CLIENT_VERSION = "0.3.0"


class ToolTableError(Exception):
    """Error parsing or generating a LinuxCNC tool table."""


class ServerUnreachable(Exception):
    """The Smooth server could not be reached (benign for cron)."""


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_file = None


def log(message):
    """Timestamped log line to stdout (and LOG_DIR file when configured)."""
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    print(line)
    if _log_file:
        try:
            with open(_log_file, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass  # logging must never break the sync


def _init_log_file(config):
    global _log_file
    log_dir = config.get("LOG_DIR")
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            _log_file = os.path.join(
                log_dir, "sync-%s.log" % time.strftime("%Y%m%d")
            )
        except OSError:
            _log_file = None


# ---------------------------------------------------------------------------
# Tool table parse / generate (lossless round trip)
# ---------------------------------------------------------------------------

# (parameter letter, dict key, converter) in canonical output order
_PARAMS = [
    ("P", "pocket", int),
    ("D", "diameter", float),
    ("X", "x_offset", float),
    ("Y", "y_offset", float),
    ("Z", "z_offset", float),
    ("A", "a_angle", float),
    ("B", "b_angle", float),
    ("C", "c_angle", float),
    ("U", "u_offset", float),
    ("V", "v_offset", float),
    ("W", "w_offset", float),
    ("Q", "orientation", int),
    ("I", "front_angle", float),
    ("J", "back_angle", float),
]


def parse_tool_table_line(line):
    """Parse one tool table line into a dict, or None for blanks/comments.

    Raises:
        ToolTableError: missing/invalid tool number, negative diameter
    """
    line = line.strip()
    if not line or line.startswith(";"):
        return None

    parts = line.split(";", 1)
    data = parts[0].strip()
    comment = parts[1].strip() if len(parts) > 1 else ""

    if re.search(r"T[^\d\s]", data):
        raise ToolTableError("Invalid tool number in line: %s" % line)
    t_match = re.search(r"T(\d+)", data)
    if not t_match:
        raise ToolTableError("Missing tool number in line: %s" % line)

    tool = {"tool_number": int(t_match.group(1)), "comment": comment}
    for letter, key, conv in _PARAMS:
        if conv is int:
            match = re.search(r"%s(\d+)" % letter, data)
        else:
            match = re.search(r"%s([+-]?\d+\.?\d*)" % letter, data)
        tool[key] = conv(match.group(1)) if match else None

    if tool["diameter"] is not None and tool["diameter"] < 0:
        raise ToolTableError("Diameter must be positive in line: %s" % line)
    return tool


def parse_tool_table(content):
    """Parse a complete tool table file into a list of tool dicts."""
    tools = []
    seen = set()
    for line_num, line in enumerate(content.split("\n"), 1):
        try:
            tool = parse_tool_table_line(line)
        except ToolTableError as e:
            raise ToolTableError("Error at line %d: %s" % (line_num, e))
        if tool is None:
            continue
        if tool["tool_number"] in seen:
            raise ToolTableError(
                "Duplicate tool number T%d at line %d" % (tool["tool_number"], line_num)
            )
        seen.add(tool["tool_number"])
        tools.append(tool)
    return tools


def generate_tool_table_line(tool):
    """Render one tool dict as a canonical tool table line."""
    parts = ["T%d" % tool["tool_number"]]
    for letter, key, conv in _PARAMS:
        value = tool.get(key)
        if value is None:
            if key == "pocket":
                parts.append("P0")
            continue
        if conv is int:
            parts.append("%s%d" % (letter, value))
        else:
            parts.append("%s%+.6f" % (letter, value))
    line = " ".join(parts)
    if tool.get("comment"):
        line += " ;%s" % tool["comment"]
    return line


def generate_tool_table(tools):
    """Render a list of tool dicts as a tool table file, sorted by T number."""
    ordered = sorted(tools, key=lambda t: t["tool_number"])
    return "\n".join(generate_tool_table_line(t) for t in ordered)


# ---------------------------------------------------------------------------
# Mapping to the Smooth API (v2 sectioned schema, /sync slots)
# ---------------------------------------------------------------------------

# canonical offset key  <-  local tool dict key
_OFFSET_KEYS = [("z_offset", "z"), ("x_offset", "x"), ("y_offset", "y"),
                ("diameter", "diameter")]


def tool_to_slot(tool, machine_name, units="mm"):
    """Map a parsed tool to a `slots` entry for the /sync call.

    A slot carries ONLY what a machine may legitimately state:
    - `tool_number` and plain `offsets` (z/x/y/diameter + `<key>_unit`): the
      observable canonical values. The server stamps them with provenance
      `observed:linuxcnc@<machine>`; we never send `source`/`canonical`.
    - `data`: the opaque, client-owned linuxcnc payload (the raw canonical
      line + ALL parsed params) so nothing is lost on round trip. It becomes
      `clients.linuxcnc.data` on the server.
    - `client_item_id`: this client's stable handle for the slot, the server's
      re-adoption fallback (§6). Binding is the server/inbox's job; we never
      send a bound_instance_id.
    """
    offsets = {}
    for src, dst in _OFFSET_KEYS:
        if tool.get(src) is not None:
            offsets[dst] = tool[src]
            offsets[dst + "_unit"] = units

    params = {k: v for k, v in tool.items() if k != "comment" and v is not None}
    slot = {
        "tool_number": tool["tool_number"],
        "offsets": offsets,
        "data": {
            "raw": generate_tool_table_line(tool),
            "params": params,
        },
        "client_item_id": "%s:T%d" % (machine_name, tool["tool_number"]),
    }
    # The table comment is the operator's label for the slot — observed table
    # state. Surfacing it gives the tool a human-readable name (the server
    # stamps observed:linuxcnc@<machine>; on adopt it seeds the instance name).
    if tool.get("comment"):
        slot["description"] = tool["comment"]
    return slot


def _entry_tool_number(entry):
    """The slot number from a sectioned entry's canonical section."""
    return entry["canonical"]["tool_number"]["value"]


def _entry_bound(entry):
    """True when the slot has a confirmed physical tool bound to it.

    Replaces the old top-level `tool_record_id`: a slot is bound when
    `canonical.bound_instance_id.value` is not null (asserted on the server).
    Pull/write-back applies ONLY to bound slots.
    """
    field = (entry.get("canonical") or {}).get("bound_instance_id") or {}
    return field.get("value") is not None


def _entry_offsets(entry):
    """The server-owned offset values flowing back from a sectioned entry.

    Returns {canonical_key: value} (e.g. {"z": -48.25, "diameter": 6.35}),
    skipping `unknown`/null fields. This is the ONLY server-side data the
    table consumes, so it is also the change-detection key. Provenance/units
    and version numbers are deliberately ignored: a binding confirmation or
    other metadata write bumps the record version without touching offsets,
    and treating that as a change made a confirm look like a conflict (found
    live).
    """
    result = {}
    offsets = (entry.get("canonical") or {}).get("offsets") or {}
    for key, field in offsets.items():
        if field is not None and field.get("value") is not None:
            result[key] = field["value"]
    return result


def _local_offsets(tool):
    """The same {canonical_key: value} shape derived from a local tool dict,
    so a server entry and a local row can be compared field-for-field."""
    result = {}
    for src, dst in _OFFSET_KEYS:
        if tool.get(src) is not None:
            result[dst] = tool[src]
    return result


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def parse_config(text):
    """Parse shell-style KEY="value" config text into a dict."""
    config = {}
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        config[key.strip()] = value
    return config


def load_config(path=DEFAULT_CONFIG_PATH):
    """Load config file (if present) and apply environment overrides."""
    config = {}
    if os.path.exists(path):
        with open(path) as f:
            config.update(parse_config(f.read()))
    for key in ("SMOOTH_API_URL", "SMOOTH_API_KEY", "MACHINE_NAME",
                "TOOL_TABLE", "LINUXCNC_INI", "UNITS", "LOG_DIR"):
        if os.environ.get(key):
            config[key] = os.environ[key]
    return config


def find_tool_table(ini_path):
    """Discover the tool table path from a LinuxCNC INI file.

    Assumptions:
    - TOOL_TABLE appears as 'TOOL_TABLE = <path>' (section ignored);
      relative paths resolve against the INI's directory
    """
    ini_dir = os.path.dirname(os.path.abspath(ini_path))
    with open(ini_path) as f:
        for line in f:
            line = line.strip()
            if line.upper().startswith("TOOL_TABLE"):
                _, _, value = line.partition("=")
                value = value.strip()
                if value:
                    if not os.path.isabs(value):
                        value = os.path.join(ini_dir, value)
                    return value
    raise ToolTableError("No TOOL_TABLE setting found in %s" % ini_path)


def backup_tool_table(path, backup_dir):
    """Copy the tool table to a timestamped backup file; return its path.

    Called before ANY write to the tool table (the pull path). Push never
    writes, but the helper lives here so every future writer has it.
    """
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(
        backup_dir, "%s-%s.bak" % (os.path.basename(path), stamp)
    )
    shutil.copy2(path, backup)
    return backup


# ---------------------------------------------------------------------------
# HTTP (single seam for tests; urllib only)
# ---------------------------------------------------------------------------

def http_json(method, url, api_key, body=None, timeout=HTTP_TIMEOUT):
    """One JSON request. Raises ServerUnreachable on network failure."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if api_key:
        request.add_header("Authorization", "Bearer %s" % api_key)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise ServerUnreachable("HTTP %d from %s: %s" % (e.code, url, detail))
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise ServerUnreachable("cannot reach %s: %s" % (url, e))


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------

def _ensure_machine(base_url, api_key, name, state, state_file):
    """Return this machine's server id, creating+naming a MachineRecord on
    first contact and PERSISTING its id in the state file.

    v2 has no name lookup: the client owns the server->client back-reference
    (§6). First run creates an empty MachineRecord, asserts its name, and
    stores `internal.id` in the state file; every later run reuses that id.
    """
    machine_id = state.get("machine_id")
    if machine_id:
        return machine_id
    created = http_json("POST", base_url + "/api/v1/machine-records", api_key,
                        body={})
    machine_id = created["internal"]["id"]
    http_json("POST", "%s/api/v1/machine-records/%s/assert" % (base_url, machine_id),
              api_key,
              body={"path": "name", "value": name, "actor": CLIENT_NAME})
    state["machine_id"] = machine_id
    _save_state(state_file, state)
    log("Registered machine '%s' on server (id %s)" % (name, machine_id))
    return machine_id


def _sync_push(base_url, machine_id, machine_name, api_key, slots, mode):
    """One /sync call. `mode` is 'snapshot' (full table; reconciles away
    absent slots) or 'merge' (deltas; touches only the slots sent)."""
    return http_json(
        "POST", "%s/api/v1/tool-table-entry-records/sync" % base_url, api_key,
        body={
            "machine_id": machine_id,
            "client": CLIENT_NAME,
            "machine_name": machine_name,
            "client_version": CLIENT_VERSION,
            "mode": mode,
            "force": False,
            "slots": slots,
        })


def _resolve_inputs(config):
    """Resolve (base_url, machine_name, table_path); return (.., None) or
    (None, None, None, exit_code) on a usage/config error."""
    base_url = (config.get("SMOOTH_API_URL") or "").rstrip("/")
    machine_name = config.get("MACHINE_NAME")
    table_path = config.get("TOOL_TABLE")
    if not table_path and config.get("LINUXCNC_INI"):
        try:
            table_path = find_tool_table(config["LINUXCNC_INI"])
        except (OSError, ToolTableError) as e:
            log("ERROR: %s" % e)
            return None, None, None, 2
    if not base_url or not machine_name or not table_path:
        log("ERROR: SMOOTH_API_URL, MACHINE_NAME, and TOOL_TABLE (or "
            "LINUXCNC_INI) must be configured")
        return None, None, None, 2
    return base_url, machine_name, table_path, None


def push_tool_table(config):
    """Push the local tool table to the server as a full snapshot.

    Returns a process exit code:
    - 0: pushed, or benign failure (server unreachable) — cron-safe
    - 2: usage/config error (missing settings, unreadable table)
    """
    _init_log_file(config)

    base_url, machine_name, table_path, err = _resolve_inputs(config)
    if err is not None:
        return err

    try:
        with open(table_path) as f:
            tools = parse_tool_table(f.read())
    except OSError as e:
        log("ERROR: cannot read tool table %s: %s" % (table_path, e))
        return 2
    except ToolTableError as e:
        log("ERROR: %s" % e)
        return 2

    units = config.get("UNITS", "mm")
    slots = [tool_to_slot(t, machine_name, units=units) for t in tools]
    api_key = config.get("SMOOTH_API_KEY", "")
    state_file = _state_path(config, machine_name)
    state = _load_state(state_file)

    log("Pushing %d tools from %s as machine '%s'"
        % (len(slots), table_path, machine_name))
    try:
        machine_id = _ensure_machine(base_url, api_key, machine_name,
                                     state, state_file)
        # This reads the COMPLETE tool.tbl, so it is a full snapshot: the
        # server reconciles away entries the operator deleted locally. (The
        # two-way sync path sends deltas with mode 'merge' instead.)
        result = _sync_push(base_url, machine_id, machine_name, api_key,
                            slots, mode="snapshot")
    except ServerUnreachable as e:
        log("Server not reachable, will retry next sync: %s" % e)
        return 0  # benign: never block the machine

    log("Pushed %d entries" % len(result.get("items", [])))
    removed = result.get("removed_tool_numbers") or []
    if removed:
        log("Reconciled %d entr%s removed locally: T%s"
            % (len(removed), "ies" if len(removed) != 1 else "y",
               ", T".join(str(n) for n in removed)))
    for error in result.get("errors", []):
        log("Server rejected item %s: %s" % (error.get("index"), error.get("message")))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Full sync cycle (push + pull) - smooth-linuxcnc#2
# ---------------------------------------------------------------------------

def _state_path(config, machine_name):
    state_dir = config.get("STATE_DIR") or os.path.dirname(DEFAULT_CONFIG_PATH)
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        pass
    return os.path.join(state_dir, "state-%s.json" % machine_name)


def _load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"tools": {}}


def _save_state(path, state):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def _entry_to_local_overlay(tool, entry):
    """Overlay a server entry's canonical offsets onto a local tool dict.

    Only the observable canonical fields the server owns flow back: the
    offsets. Everything else (U/V/W, angles, orientation, the comment) keeps
    the machine's values. In the v2 schema offsets are the only canonical
    fields this client both observes and consumes.
    """
    merged = dict(tool)
    offsets = _entry_offsets(entry)
    for src, dst in (("z", "z_offset"), ("x", "x_offset"),
                     ("y", "y_offset"), ("diameter", "diameter")):
        if offsets.get(src) is not None:
            merged[dst] = offsets[src]
    return merged


def _surgical_write(table_path, replacements, backup_dir):
    """Replace only the lines of changed tools; comments survive verbatim.

    Args:
        replacements: tool_number -> new canonical line
    """
    backup_tool_table(table_path, backup_dir)
    with open(table_path) as f:
        lines = f.read().split("\n")
    out = []
    for line in lines:
        try:
            parsed = parse_tool_table_line(line)
        except ToolTableError:
            parsed = None
        if parsed and parsed["tool_number"] in replacements:
            out.append(replacements.pop(parsed["tool_number"]))
        else:
            out.append(line)
    with open(table_path + ".tmp", "w") as f:
        f.write("\n".join(out))
    os.replace(table_path + ".tmp", table_path)


def sync_tool_table(config):
    """Full cycle: 3-way per tool between local table, server, and the
    state recorded at last sync.

    Decision table per tool (G2/D5: never guess, never clobber):
    - neither changed              -> skip
    - local changed only           -> push
    - server changed only + BOUND  -> write back to .tbl (backup first)
    - server changed only, unbound -> leave the table alone
    - both changed                 -> CONFLICT: log loudly, touch neither
    - gone locally, was synced     -> delete on the server (the operator
                                      removed it from the controller's table)
    - gone locally + changed on
      the server                   -> CONFLICT: touch neither
    - on the server, never synced  -> server-side addition; left for the
                                      operator (pull direction, not automatic)
    First sync (no state) pushes everything and records state.
    Returns 0 on success/benign failure, 2 on config errors.

    NOTE: server-side frozen fields (#7) will be honored here once the
    server exposes them; until then conflicts are detected client-side.
    """
    _init_log_file(config)

    base_url, machine_name, table_path, err = _resolve_inputs(config)
    if err is not None:
        return err

    try:
        with open(table_path) as f:
            content = f.read()
        local_tools = {t["tool_number"]: t for t in parse_tool_table(content)}
    except (OSError, ToolTableError) as e:
        log("ERROR: cannot read tool table %s: %s" % (table_path, e))
        return 2
    local_lines = {t["tool_number"]: generate_tool_table_line(t)
                   for t in local_tools.values()}

    state_file = _state_path(config, machine_name)
    state = _load_state(state_file)
    known = state.get("tools", {})
    units = config.get("UNITS", "mm")
    api_key = config.get("SMOOTH_API_KEY", "")
    backup_dir = config.get("LOG_DIR") or os.path.dirname(os.path.abspath(table_path))

    try:
        machine_id = _ensure_machine(base_url, api_key, machine_name,
                                     state, state_file)
        server = {_entry_tool_number(e): e for e in http_json(
            "GET", "%s/api/v1/tool-table-entry-records?machine_id=%s"
            % (base_url, machine_id), api_key).get("items", [])}
    except ServerUnreachable as e:
        log("Server not reachable, will retry next sync: %s" % e)
        return 0

    to_push = []
    to_write = {}
    conflicts = []

    for n in sorted(local_tools):
        key = str(n)
        last = known.get(key, {})
        entry = server.get(n)

        if key not in known and entry is not None:
            # First contact with no baseline, but the server ALREADY has this
            # tool (e.g. created by an earlier push, then bound on the server).
            # A missing baseline is not evidence of a change: compare the
            # syncable fields. If they already agree we are in sync and simply
            # adopt the baseline; only a genuine content divergence — which we
            # have no history to arbitrate — is a conflict. (Without this, a
            # first sync after a push flags EVERY row as a false conflict.)
            agree = _entry_offsets(entry) == _local_offsets(local_tools[n])
            local_changed = not agree
            server_changed = not agree
        else:
            local_changed = local_lines[n] != last.get("local_line")
            server_changed = entry is not None and \
                _entry_offsets(entry) != last.get("server_fields")
            if entry is None:
                local_changed = True  # never seen by server: push
                server_changed = False

        if local_changed and server_changed:
            conflicts.append(n)
            log("CONFLICT: T%d changed locally AND on the server - "
                "touching neither. Resolve by re-editing one side." % n)
            continue
        if local_changed:
            to_push.append(tool_to_slot(local_tools[n], machine_name, units=units))
        elif server_changed:
            if _entry_bound(entry):
                merged = _entry_to_local_overlay(local_tools[n], entry)
                new_line = generate_tool_table_line(merged)
                if new_line != local_lines[n]:
                    to_write[n] = new_line
            else:
                log("T%d changed on server but is unbound - not written" % n)

    pushed = {}
    if to_push:
        try:
            # Deltas, NOT a snapshot: 'merge' touches only the slots we send,
            # so unchanged / unbound-server-changed / conflicted slots and any
            # server-side additions are left exactly as they are.
            result = _sync_push(base_url, machine_id, machine_name, api_key,
                                to_push, mode="merge")
        except ServerUnreachable as e:
            log("Server not reachable mid-sync, will retry: %s" % e)
            return 0
        for error in result.get("errors", []):
            log("Server rejected item %s: %s" % (error.get("index"),
                                                 error.get("message")))
        pushed = {_entry_tool_number(e): e for e in result.get("items", [])}
        log("Pushed %d changed tool(s)" % len(pushed))

    if to_write:
        _surgical_write(table_path, dict(to_write), backup_dir)
        log("Wrote %d server-side change(s) into %s (backup taken)"
            % (len(to_write), table_path))
        with open(table_path) as f:
            local_tools = {t["tool_number"]: t
                           for t in parse_tool_table(f.read())}
        local_lines = {t["tool_number"]: generate_tool_table_line(t)
                       for t in local_tools.values()}

    # Tools the server still has but that vanished from the local table.
    # The last-synced baseline tells a deletion apart from a server-side add:
    #   in `known`     -> operator deleted it locally  -> delete on the server
    #   not in `known` -> a server-side addition        -> pull direction,
    #                     never auto-written into the local table here
    to_delete = []
    for n in sorted(set(server) - set(local_tools)):
        key = str(n)
        if key not in known:
            log("Server has T%d for this machine but it is not in the local "
                "table - not added automatically" % n)
            continue
        # delete-vs-edit: if the server copy changed since our last sync, this
        # is a real conflict - never silently clobber a server-side edit.
        if _entry_offsets(server[n]) != known[key].get("server_fields"):
            conflicts.append(n)
            log("CONFLICT: T%d deleted locally but changed on the server - "
                "touching neither. Resolve by re-editing or re-deleting." % n)
            continue
        to_delete.append(n)

    if to_delete:
        # The v2 API has no dedicated delete endpoint, so a deletion is a
        # snapshot push of exactly the slots that should REMAIN: the server
        # reconciles away the client-managed (linuxcnc) slots we omitted -
        # which are precisely the locally-deleted ones - and reports them in
        # `removed_tool_numbers`. Server-side-only slots (no linuxcnc section)
        # are not the client's to reconcile and are left untouched.
        keep = [tool_to_slot(local_tools[n], machine_name, units=units)
                for n in sorted(local_tools) if n not in conflicts]
        try:
            result = _sync_push(base_url, machine_id, machine_name, api_key,
                                keep, mode="snapshot")
        except ServerUnreachable as e:
            log("Server not reachable mid-sync, will retry: %s" % e)
            return 0
        for error in result.get("errors", []):
            log("Server rejected delete %s: %s" % (error.get("index"),
                                                   error.get("message")))
        removed = result.get("removed_tool_numbers") or to_delete
        log("Removed %d tool(s) deleted locally: T%s"
            % (len(removed), ", T".join(str(n) for n in removed)))

    # record the new baseline (conflicted tools keep their OLD baseline so
    # they are re-detected next sync)
    new_tools = {}
    for n in sorted(local_tools):
        key = str(n)
        if n in conflicts:
            if key in known:
                new_tools[key] = known[key]
            continue
        entry = pushed.get(n) or server.get(n)
        new_tools[key] = {
            "local_line": local_lines[n],
            "server_fields": _entry_offsets(entry) if entry else None,
        }
    _save_state(state_file, {"machine_id": machine_id, "tools": new_tools})

    if not to_push and not to_write and not to_delete and not conflicts:
        log("In sync - nothing to do")
    return 0


def main(argv):
    if len(argv) < 2 or argv[1] not in ("push", "sync"):
        print(__doc__)
        return 2
    config = load_config()
    if len(argv) > 2:
        config["MACHINE_NAME"] = argv[2]
    if argv[1] == "sync":
        return sync_tool_table(config)
    return push_tool_table(config)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
