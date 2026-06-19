# Changelog

All notable changes to **smooth-linuxcnc** (the LinuxCNC controller-side client
for Smooth) are recorded here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [0.3.0] — 2026-06-19

The v2 client, aligned to the sectioned tool schema.

### Added
- Pushes the machine's tool table to the server via `/sync`: each entry carries
  `tool_number` and observable offsets (z/x/y/diameter), plus the opaque,
  client-owned `clients.linuxcnc.data` payload (the raw canonical line and all
  parsed params) so nothing is lost on round trip.
- A stable `client_item_id` per entry as the server's re-adoption fallback.

### Changed
- Speaks the **sectioned schema**: this client only ever writes its own
  `clients.linuxcnc` section plus the few canonical fields a machine may
  **observe** (tool number, offsets) — it never sends `internal`/`canonical`
  keys; the server stamps provenance `observed:linuxcnc@<machine>` itself.
- The `/sync` wire field is **`entries`** (was `slots`); binding is the
  server/inbox's job — this client never sends a `bound_instance_id`.

### Removed
- The undocumented "slot" term, project-wide → **entry** / `ToolTableEntry`.

[0.3.0]: https://github.com/loobric/smooth-linuxcnc/releases/tag/v0.3.0
