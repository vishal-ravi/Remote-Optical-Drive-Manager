# Development

## Repository layout

```
disc/
├── README.md               entry point, quick start
├── docs/                   this documentation set
├── pyproject.toml          project metadata, pytest + ruff config
├── requirements-client.txt PySide6, paramiko, requests
├── common/                 shared by both sides
│   ├── protocol.py         envelopes, ops, error codes, job/drive dataclasses
│   ├── validation.py       every parameter validator (device, paths, speeds…)
│   └── logging_setup.py    log formatting + file/console handlers
├── agent/                  remote agent (stdlib only, no pip deps)
│   ├── cli.py              rodm-agent subcommands
│   ├── config.py           data/state roots, token, TLS paths, limits
│   ├── core.py             AgentCore.dispatch → op handlers
│   ├── http_api.py         REST server, TLS pair generation, event streams
│   ├── ssh_rpc.py          one-request-per-call SSH frontend
│   ├── jobs.py             JobManager, JobContext, progress/cancel
│   ├── drives.py           lsblk/udevadm parsing, DriveInfo, media detection
│   ├── process.py          run_checked/stream_command + sudo escalation
│   ├── tools.py            whitelisted utility lookup (wodim, dd, …)
│   └── ops/                media.py (mount/browse/eject), images.py (rip/build),
│                           burn.py (wodim, verify)
├── client/                 desktop client (PySide6 + paramiko + requests)
│   ├── profiles.py         Profile/ProfileStore (~/.config/rodm)
│   ├── transport.py        ClientSession: REST-first, SSH fallback, SFTP
│   └── gui/                main_window, dialogs/profile_dialog,
│                           workers.py (TaskHost, job/queue/transfer tasks),
│                           tabs/{browse,image,burn,queue,log}_tab.py
├── scripts/
│   ├── run_client.sh       venv bootstrap + GUI (refuses to run as root)
│   ├── install_agent.sh    scp/tar install, --root/--service/--install-packages
│   └── rodm-agent.service  systemd template (root mode)
└── tests/                  pytest suite (no network, no GUI display needed)
```

## Setup

```bash
./scripts/run_client.sh        # creates .venv with dev tools already usable
```

Everything runs from the repo root; `tests/conftest.py` puts the root on
`sys.path`.

## Tests

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/     # 221 passed
```

| File | Focus |
| --- | --- |
| `test_protocol.py` | envelopes, request/response encoding, job/drive dataclasses |
| `test_validation.py` | every validator: devices, traversal, names, speeds, limits |
| `test_agent_core.py` | dispatch whitelist, error codes, job op gating, lsblk parsing |
| `test_jobs.py` | JobManager: states, events, cancel, concurrency |
| `test_process.py` | command wrappers, error surfacing, `sudo -n` escalation |
| `test_media_ops.py` | mount/browse/eject paths, blank-disc `no_filesystem` |
| `test_burn_ops.py` | fake `dd`/`wodim`: rip progress, burn command line, verify offsets |
| `test_ssh_rpc.py` | one-request stdin/stdout contract, error responses |
| `test_tls_cert.py` | SAN parsing, certificate refresh policy |
| `test_client_session.py` | REST-first ordering, fallback rules, job gate, token fetch |
| `test_integration_rest.py` | **real HTTP server** on loopback: rpc, upload→ISO job, auth, profile store |
| `test_gui_smoke.py` | offscreen GUI: tabs, status, TaskHost thread ownership |

Conventions visible in the tests:

- external binaries are faked (`monkeypatch` `tools.require`/`which`, fake
  `dd`/`wodim` scripts on `PATH`) — never burn in CI
- the REST integration test uses a real `ThreadingHTTPServer` on a free
  loopback port with `require_tls=False`; its ISO-build case runs the real
  `genisoimage`, so that package must be installed locally
- GUI tests need `QT_QPA_PLATFORM=offscreen` (set automatically in CI/local
  if unset)

## Lint

```bash
.venv/bin/ruff check --select E9,F common agent client tests   # gate (used in CI)
```

`pyproject.toml` sets `line-length = 100`, `target-version = "py310"`.

## Coding conventions

- **agent = stdlib only.** No third-party imports in `agent/` or `common/`.
- **Never build shell strings.** Call `run_checked([...])` /
  `stream_command([...])` with argument lists; privileges via
  `escalate=True`, not manual `sudo`.
- **Validate at the boundary.** New parameters get a validator in
  `common/validation.py` and are called in the op handler *before* any work.
- **Errors are `AgentError(message, code)`** with a code from
  `common/protocol.py` — never bare `Exception` across the wire.
- **Long work = job.** Anything slower than ~2 s runs through `JobContext`
  (`ctx.progress/log/status/check_cancelled`) so cancel/progress work for
  free; return a JSON-serializable `result`.
- **Qt threads are owned.** GUI background work goes through
  `TaskHost.start_task()` (keeps a reference, parents the thread, joins on
  close) — never start a bare `QThread`.
- type hints + `from __future__ import annotations`; docstrings say *why*,
  not *what*.
- update the matching doc page when behaviour changes (this is enforced
  socially: reviewers read `docs/` first).

## Adding a new operation

1. `common/protocol.py` — add `OP_*`, include it in `SIMPLE_OPS` or
   `JOB_OPS`
2. validators in `common/validation.py` (reuse `validate_device`, …)
3. `agent/core.py` — `_op_<name>` handler: validate → call `agent/ops/*` →
   return a plain dict (or `self.jobs.submit(...)` for jobs)
4. `agent/ops/*` — the real work, argument-list subprocesses only
5. client: call it via `ClientSession.call("<op>", {...})`; add a GUI entry
   if the user needs it (through `TaskHost`)
6. tests: dispatch/validation unit tests + at least one fake-binary test
   for the ops layer
7. document it in `docs/api.md` (params, result, errors)

## Release checklist

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/
.venv/bin/ruff check --select E9,F common agent client tests
./scripts/install_agent.sh user@host --root        # upgrade the agent
./scripts/run_client.sh                            # smoke the GUI
```
