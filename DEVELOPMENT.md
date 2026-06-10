# Development

This repo is the LinuxCNC client for [Smooth](https://github.com/loobric/smooth-core).
Everything lives in **one file**, `smooth_linuxcnc.py`, on purpose — see the
README's design constraints (old control-box distros, stdlib only, cron-safe).

## Architecture

Smooth keeps a strict core/client split: `smooth-core` is the application-agnostic
REST API and database; clients like this one speak the **public facade API only**
(`ToolRecord`, `Machine`, `ToolTableEntry` — see smooth-core's
`docs/UBIQUITOUS_LANGUAGE.md`). Deep-schema endpoints are private and off-limits.

The client maps each `.tbl` row to a `ToolTableEntry` upsert:

| .tbl | ToolTableEntry |
|------|----------------|
| `T` | `tool_number` (upsert key with the machine) |
| `P` | `pocket` |
| `D`, `X`, `Y`, `Z` | `offsets` (with per-key units) |
| `;comment` | `description` |
| everything, verbatim | `extra.linuxcnc.raw` + `.params` (lossless) |

Offset fields are stamped `"machine"` in `provenance`. Entries push **unbound**
(`tool_record_id` absent): binding is the server inbox's job, or the user's —
never a client-side guess.

## Rules of the file

1. **Stdlib only, Python 3.6 floor.** No f-strings beyond 3.6 compatibility, no
   dataclasses, no walrus, no `list[...]` generics. CI enforces via 3.6 container.
2. **One HTTP seam.** All traffic goes through `http_json()`; tests stub exactly
   that function.
3. **Exit codes are the contract with cron**: 0 = pushed or benign failure
   (server down), 2 = config/usage error. Nothing the server does may make the
   script block or crash.
4. Tests are stdlib `unittest`: `python3 -m unittest discover -s tests -v`.

## Manual end-to-end

```bash
# in smooth-core:
SMOOTH_SOLO=1 uvicorn smooth.main:app --port 8000
# here:
SMOOTH_API_URL=http://localhost:8000 MACHINE_NAME=mill01 \
  TOOL_TABLE=tests/fixtures/sample.tbl ./smooth_linuxcnc.py push
```

`examples/` holds a LinuxCNC sim configuration for testing against a real
LinuxCNC instance.

## Contributing

DCO sign-off, tests first, keep the file ancient-Python-compatible. Pull-path
work (server → `.tbl`, the closed loop) is tracked in
[smooth-linuxcnc#2](https://github.com/loobric/smooth-linuxcnc/issues/2).
