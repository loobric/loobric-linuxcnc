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
- **Never a guess:** entries pair with CAM tool records on the server (the
  Inbox), and a tool changed on *both* sides between syncs is reported as a
  conflict touching neither — resolve by re-editing one side.
- **Tells you what to load:** when a tool set bound to this machine asks for a
  tool the table doesn't have yet, sync reports it as **requested** — named by
  both its human name and its full instance id, with a target pocket when the set
  states one — so the operator knows exactly what to mount. Once it's mounted the
  next sync reads **pending bind** until the binding is confirmed on the server,
  then folds back into "in sync". An outstanding request never reads as
  "nothing to do".

## Design constraints (why this is one file)

LinuxCNC control boxes are often image-built on old distributions. This client:

- is a **single file** — copy it anywhere, no install step required
- has **no third-party dependencies** — Python 3 standard library only, so there
  are never any pip-resolved packages to install, on any Python 3.6+ (CI tests
  back to Python 3.6)
- **never blocks the machine** — an unreachable server logs one line and exits 0,
  so a cron job can fire forever without consequences
- **never runs a server on the control box** — the Smooth server belongs on a
  NAS/LAN box; this script is just a small messenger

Because there are no dependencies, the same file is **pip-installable on a modern
box** (giving you a `smooth-linuxcnc` command on your PATH) *and* **copy-and-run
on an old one** — you never have to choose.

## Quick start

### 1. Get it onto the control box

Either install it (modern box, gives you a `smooth-linuxcnc` command):

```bash
pip install smooth-linuxcnc
```

…or just grab the single file (old box, no pip):

```bash
wget https://raw.githubusercontent.com/loobric/smooth-linuxcnc/master/smooth_linuxcnc.py
chmod +x smooth_linuxcnc.py
```

Every command below works either way — as `smooth-linuxcnc <cmd>` (installed) or
`./smooth_linuxcnc.py <cmd>` (single file).

### 2. Run the setup wizard

```bash
smooth-linuxcnc init
```

`init` walks you through it, prompting for:

- **Server URL** — defaults to the hosted `https://api.loobric.com` **sandbox**
  (a shared playground — keep nothing real there; point at your own NAS/LAN
  server for production).
- **API key** — leave it blank if you don't have one yet. Create an account and
  key through the web UI or the Python client (`pip install loobric-smooth`;
  `smooth register` then `smooth create-key` — see
  [loobric-smooth/docs/SANDBOX.md](https://github.com/loobric/loobric-smooth/blob/master/docs/SANDBOX.md)),
  then paste it into the config later. Blank is also correct for a solo-mode server.
- **Machine name** — defaults to this box's hostname.
- **LinuxCNC config** — auto-discovered from `~/linuxcnc/configs/`. If you have
  **several**, it asks which machine this is and writes the rest as commented
  alternatives, so you can switch later by un/commenting a line.

It writes `~/.config/smooth/linuxcnc.conf` (mode 600 — it holds your API key) and
offers to run `doctor` right away. You can re-open the file any time to change a
value; environment variables of the same name override it, as do `--url` and a
positional machine name on the command line.

Non-interactive (no terminal)? `init` takes every default without prompting. For
a scripted install, name the INI explicitly: `smooth-linuxcnc init --ini PATH`.

### 3. Check your setup

```bash
smooth-linuxcnc doctor
```

One command validates the config, finds and parses your tool table, and confirms
the server is reachable and your key works — so setup problems surface here
instead of in a cron log:

```
[ OK ] Config file - /home/user/.config/smooth/linuxcnc.conf
[ OK ] Server URL - http://nas.local:8000
[ OK ] Machine name - mill01
[ OK ] Tool table - /home/user/linuxcnc/configs/mill/tool.tbl (5 tools)
[ OK ] Server reachable - http://nas.local:8000 (server v0.2.0)
[ OK ] Authentication - API key accepted
```

### 4. Sync

```bash
smooth-linuxcnc sync            # full cycle: push + pull (use this)
smooth-linuxcnc push            # one-way, table -> server only
```

```
[2026-06-09 12:00:00] Pushing 4 tools from /home/user/linuxcnc/configs/mill/tool.tbl as machine 'mill01'
[2026-06-09 12:00:00] Registered machine 'mill01' on server
[2026-06-09 12:00:01] Pushed 4 entries
```

The machine is created on the server on first contact.

Once the table is pushed, a `sync` still tells you what the bench owes — an
open load request is folded into the in-sync summary, not hidden behind
"nothing to do":

```
[2026-06-09 12:05:00] 5 tools in sync, 1 tool requested: "1/4 downcut" (inst-7f3a91) - mount it and assign pocket 5
```

### 5. Automate (cron)

```bash
crontab -e
# every 5 minutes; safe even when the server is down
*/5 * * * * smooth-linuxcnc sync >> /tmp/smooth-sync.log 2>&1
# or, single-file install:
# */5 * * * * /home/user/smooth_linuxcnc.py sync >> /tmp/smooth-sync.log 2>&1
```

## Tool table format

Parses and regenerates the standard LinuxCNC format losslessly, including
lathe parameters:

```
T<num> P<pocket> [D<dia>] [X Y Z U V W offsets] [A B C angles] [Q<orient>] [I<front> J<back>] ;comment
```

The raw line and every parsed parameter travel to the server in the entry's
`clients.linuxcnc.data` field, so nothing your table says is ever lost in translation.

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
