# Smooth LinuxCNC Client

> Push your LinuxCNC tool table to a [Smooth](https://github.com/loobric/smooth-core) server. One file, standard library only, cron-safe.

## What it does

`smooth_linuxcnc.py sync` keeps your machine's tool table (`.tbl`) and a Smooth
server in step, both directions:

- **Machine → server:** tool numbers, pockets, offsets, comments — raw table
  lines preserved losslessly. A touch-off at the machine reaches your CAM-side
  tool record on the next sync, with provenance.
- **Server → machine:** changes to **bound** entries are written back into the
  table — line-surgically (your comments survive), with a timestamped backup
  first. Unbound entries never write back.
- **Never a guess:** entries pair with CAM tool records on the server (review
  inbox), and a tool changed on *both* sides between syncs is reported as a
  conflict touching neither — resolve by re-editing one side.

## Design constraints (why this is one file)

LinuxCNC control boxes are often image-built on old distributions. This client:

- is a **single file** — copy it anywhere, no install step
- uses **only the Python 3 standard library** — no pip, ever (CI tests back to Python 3.6)
- **never blocks the machine** — an unreachable server logs one line and exits 0,
  so a cron job can fire forever without consequences
- **never runs a server on the control box** — the Smooth server belongs on a
  NAS/LAN box; this script is just a small messenger

## Quick start

### 1. Get the file onto the control box

```bash
wget https://raw.githubusercontent.com/loobric/smooth-linuxcnc/main/smooth_linuxcnc.py
chmod +x smooth_linuxcnc.py
```

### 2. Configure

Create `~/.config/smooth/linuxcnc.conf`:

```bash
SMOOTH_API_URL="http://nas.local:8000"
SMOOTH_API_KEY="your-api-key"        # omit against a solo-mode server
MACHINE_NAME="mill01"
LINUXCNC_INI="/home/user/linuxcnc/configs/mill/mill.ini"   # tool table found via INI
# or point at the table directly:
# TOOL_TABLE="/home/user/linuxcnc/configs/mill/tool.tbl"
# UNITS="mm"                         # offset units, default mm
# LOG_DIR="/tmp/smooth-sync"         # optional log files
```

Environment variables with the same names override the file.

### 3. Sync

```bash
./smooth_linuxcnc.py sync            # full cycle: push + pull (use this)
./smooth_linuxcnc.py push            # one-way, table -> server only
```

```
[2026-06-09 12:00:00] Pushing 4 tools from /home/user/linuxcnc/configs/mill/tool.tbl as machine 'mill01'
[2026-06-09 12:00:00] Registered machine 'mill01' on server
[2026-06-09 12:00:01] Pushed 4 entries
```

The machine is created on the server on first contact.

### 4. Automate (cron)

```bash
crontab -e
# every 5 minutes; safe even when the server is down
*/5 * * * * /home/user/smooth_linuxcnc.py sync >> /tmp/smooth-sync.log 2>&1
```

## Tool table format

Parses and regenerates the standard LinuxCNC format losslessly, including
lathe parameters:

```
T<num> P<pocket> [D<dia>] [X Y Z U V W offsets] [A B C angles] [Q<orient>] [I<front> J<back>] ;comment
```

The raw line and every parsed parameter travel to the server in the entry's
`extra.linuxcnc` field, so nothing your table says is ever lost in translation.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Pushed — or server unreachable (benign, retry next sync) |
| 2 | Usage or configuration error (missing settings, unreadable table) |

## Development

```bash
python3 -m unittest discover -s tests -v
```

Tests are stdlib-only too (`unittest`); CI runs them on Python 3.6 through 3.12.
The `examples/` directory contains a LinuxCNC sim configuration for testing.

## License

MIT — see [LICENSE](LICENSE). Contributions welcome under DCO sign-off.
