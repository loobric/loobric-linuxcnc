# Changelog

All notable changes to **smooth-linuxcnc** (the LinuxCNC controller-side client
for Smooth) are recorded here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [0.5.0] — 2026-06-28

Less startup friction, same single-file promise.

### Added
- **`init`** is an interactive setup wizard: it prompts for the server URL
  (defaulting to the `api.loobric.com` sandbox, with a not-for-production
  warning), an API key (blank allowed, with in-file notes on creating one later),
  a machine name (defaulting to the hostname), and — when several
  `~/linuxcnc/configs/*/*.ini` exist — which machine this is (writing the rest as
  commented alternatives). It then offers to run `doctor`. The result is written
  to `~/.config/smooth/linuxcnc.conf` (mode 600 — it holds an API key), never
  clobbering an existing config without `--force`. Non-interactive runs take every
  default without blocking; `--ini PATH` names a config directly for scripted
  installs.
- **`doctor`** validates config, resolves and parses the tool table, and confirms
  the server is reachable and the API key works — a green/red checklist so setup
  problems surface there instead of in a cron log. Any HTTP response counts as
  reachable; only a network failure does not.
- **pip-installable**: `pip install smooth-linuxcnc` puts a `smooth-linuxcnc`
  command on your PATH. Still zero third-party dependencies, so the single file
  stays copy-and-run on old control boxes — you never have to choose.

### Changed
- Real argparse CLI: `-h`/`--help` on every command, `--version`, and global
  `--config` / `--url` overrides. The `push`/`sync` exit-code contract (0 on
  success *or* unreachable server, 2 on usage/config error) is unchanged, and a
  positional machine name plus the `SMOOTH_*` env overrides still work.

## [0.4.0] — 2026-06-23

### Added
- The controller surfaces tools the operator must still mount. A member of the
  machine's bound tool set with no entry yet is reported as **requested** — named
  by both its human name and its full instance id (the id disambiguates tools
  that share a name), with a target pocket when one is preferred — and as
  **pending bind** once mounted but not yet confirmed. These fold into the sync
  summary, so an outstanding request never reads as "nothing to do".

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

[0.5.0]: https://github.com/loobric/smooth-linuxcnc/releases/tag/v0.5.0
[0.4.0]: https://github.com/loobric/smooth-linuxcnc/releases/tag/v0.4.0
[0.3.0]: https://github.com/loobric/smooth-linuxcnc/releases/tag/v0.3.0
