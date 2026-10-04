# Protocol & REST API

One JSON envelope serves both transports:

| Transport | How | Auth | Job state |
| --- | --- | --- | --- |
| **SSH RPC** | `ssh host 'rodm-agent rpc'` — one request on stdin, one response on stdout | SSH login | per-call process (stateless) |
| **REST** | `POST https://host:8443/v1/rpc` (+ event streams, file/image endpoints) | `Authorization: Bearer <token>` | persistent (the `serve` process) |

The client prefers REST and falls back to SSH for connection-class errors;
job-producing operations require REST (they only make sense in the
long-lived process). Response bodies are identical either way.

---

## Envelope

Request:

```json
{"v":1,"id":"0f8c1a2b3c4d5e6f","op":"drive_status","params":{"device":"/dev/sr0"},"ts":1717000000.12}
```

| Field | Type | Notes |
| --- | --- | --- |
| `v` | int | `PROTOCOL_VERSION` = 1 |
| `id` | string | 16 hex chars, echoed back (use it to correlate) |
| `op` | string | must be in the whitelist below |
| `params` | object | all values validated, never interpolated into a shell |
| `ts` | number | client timestamp, informational |

Success:

```json
{"v":1,"id":"0f8c…","ok":true,"result":{ … }}
```

Failure (HTTP status is derived from the code: 400/401/404/409, else 200):

```json
{"v":1,"id":"0f8c…","ok":false,"error":{"code":"validation_error","message":"path must be a string"}}
```

### Error codes

| Code | Raised when |
| --- | --- |
| `invalid_request` | malformed JSON / envelope, missing fields |
| `unknown_op` | `op` not in `KNOWN_OPS` |
| `validation_error` | parameter fails validation (type, range, name) |
| `invalid_device` | not an optical device, or on the blocklist |
| `no_media` | drive is empty |
| `no_filesystem` | disc is blank, unformatted, or an audio CD |
| `not_mounted` | `browse … "mount": false` on an unmounted disc |
| `tool_missing` | required utility (e.g. `wodim`) not installed |
| `tool_failed` | the utility exited non-zero (message carries stderr/hint) |
| `job_not_found` | unknown/expired `job_id` |
| `job_cancelled` | operation cancelled via `job_cancel` |
| `unauthorized` | REST: missing/invalid bearer token (HTTP 401) |
| `conflict` | e.g. file exists (no `overwrite`) |
| `internal_error` | unexpected exception (logged server-side) |

Client-local code (never sent by the agent): `connection_error` — transport
unavailable (REST down, SSH failed, job op without REST).

---

## Simple operations

Answered immediately; dispatch is synchronous.

| `op` | `params` | `result` |
| --- | --- | --- |
| `ping` | — | `{pong:true, time, uptime}` (liveness/RPC test) |
| `version` | — | `{name, version, protocol, python, tools{…}, data_root}` |
| `list_drives` | — | `{drives:[DriveInfo…], count}` |
| `drive_status` | `device` | DriveInfo fields **plus** `capacity_bytes`, `mount_root` |
| `mount` | `device` | `{device, mountpoint, mounted:true}` (idempotent) |
| `unmount` | `device` | `{device, mounted:false}` (idempotent) |
| `browse` | `device`, `path` (default `"/"`), `mount` (default `true`) | `{device, mountpoint, path, parent, entries:[DirEntry…]}` |
| `eject` | `device` | `{device, action}` (unmounts first if needed) |
| `close_tray` | `device` | `{device, action}` |
| `stage_list` | `path` (default `"stage"`) | `{path, entries:[DirEntry…]}` |
| `job_status` | `job_id` | JobSnapshot (below) |
| `job_events` | `job_id`, `after` (default 0) | `{job: JobSnapshot, events:[JobEvent…], next_seq}` |
| `job_cancel` | `job_id` | cancels a pending/running job → JobSnapshot; a job that already finished raises `validation_error` |
| `job_list` | — | `{jobs:[JobSnapshot…]}` (in-memory, this process) |

**DriveInfo** fields: `device, model, vendor, size, writable, hotpluggable,
mountpoint, mounted, media_present, label, fstype, disc_type, read_only`.

**DirEntry** fields: `name, path` (relative to the disc root or data root),
`is_dir, size, mtime`.

`browse` paths are *relative to the disc root* (`"/"`, `"/docs/readme.txt"`)
— there is no way to reference host paths. `mount` (and `browse` with
`"mount": true`) raise `no_filesystem` with a human hint on blank/audio
media.

## Job operations

Return `{"job_id": "…"}` immediately; poll or stream for progress.

| `op` | `params` | Notes |
| --- | --- | --- |
| `create_iso_from_disc` | `device`, `name`, `overwrite` (false) | streaming `dd` → `images/<name>.iso` (`.iso` appended if missing) |
| `create_iso_from_files` | `source` (path inside data root, usually `stage/…`), `name`, `label` (""), `overwrite` (false) | `genisoimage` → `images/<name>.iso` |
| `burn_iso` | `device`, `iso` (path inside data root, e.g. `images/x.iso`), `speed` (`auto`/null or 1–999), `overburn`, `eject_after`, `simulate` (all false) | `wodim`; job result: `{device, image, image_name, image_bytes, speed, simulated, ejected}` |
| `verify_disc` | `device`, `iso` | streams the disc back, MD5 of the source vs disc at both ISO offsets; result: `{…, bytes_compared, source_md5, disc_md5, match_offset, verified:true}` |

**JobSnapshot**: `job_id, op, state` (`pending` / `running` / `done` /
`failed` / `cancelled`), `progress` (0.0–1.0, `-1` = unknown), `message`,
`created, updated, result, error, error_code`.

**JobEvent**: `seq` (monotonic per job), `ts`, `kind`
(`progress` | `log` | `status` | `done` | `error`), `data`.

Jobs are **in-memory**: restarting the agent cancels them (`job_not_found`
afterwards). `job_list` shows what this process knows — and over SSH every
call is a *fresh* process, so job queries only make sense against the
long-lived REST service.

---

## REST endpoints (port 8443, TLS)

All except `/v1/health` require `Authorization: Bearer <token>`
(401 `unauthorized` otherwise; comparison is constant-time).

| Method & path | Body / query | Response |
| --- | --- | --- |
| `GET /v1/health` | — | `{ok, service:"rodm-agent", version, time}` (no auth) |
| `POST /v1/rpc` | JSON request envelope (≤ 16 MiB) | JSON response envelope |
| `GET /v1/jobs/<id>/events?after=<seq>` | — | `application/x-ndjson` stream (below) |
| `GET /v1/file?device=…&path=…` | — | raw bytes of a file on the **mounted** disc; `Content-Length`, `Content-Disposition`, guessed MIME type |
| `GET /v1/images` | — | `{images:[{name, path, size, mtime}]}` from `images/` (`*.iso`, `*.img`) |
| `POST /v1/upload?path=<relative>` | raw body + `Content-Length` | `{path, size}` — streamed into the data root via `.part` + rename; 413 if > `max_iso_bytes` (12 GiB), 400 if truncated |

### Job event stream

`GET /v1/jobs/<id>/events?after=0` (`Connection: close`), one JSON object
per line:

```json
{"type":"event","event":{"seq":3,"ts":…,"kind":"progress","data":{"progress":0.42,"message":"42%"}}}
{"type":"status","job":{ …JobSnapshot… }}
```

The stream ends when the job reaches `done`/`failed`/`cancelled` or after
25 s of silence (`IDLE_STREAM_TIMEOUT`); the client then resumes polling.

### Example session

```bash
TOKEN=$(ssh drive-host 'cat ~/.local/share/rodm/agent.token')
CURL="curl -sf --cacert ~/.config/rodm/certs/drive-host-8443.pem"
AUTH="Authorization: Bearer $TOKEN"

$CURL -H "$AUTH" -X POST https://drive-host:8443/v1/rpc \
  -d '{"v":1,"id":"1","op":"list_drives","params":{}}'
$CURL -H "$AUTH" -X POST https://drive-host:8443/v1/rpc \
  -d '{"v":1,"id":"2","op":"burn_iso","params":{"device":"/dev/sr0","iso":"images/x.iso","speed":8}}'
$CURL -H "$AUTH" "https://drive-host:8443/v1/jobs/<job_id>/events?after=0"
```

## Validation & safety rules

- `device` must match `/dev/(sr[0-9]+|cdrom[0-9]*|scd[0-9]+|optical[0-9]*)`
  and must not be a system disk (`/dev/sda*`, `/dev/nvme*`, …)
- every file path is resolved inside the data root (`resolve_in_root`);
  disc paths inside the mountpoint (`resolve_media_path`); `..`, absolute
  escapes and symlinks pointing out are rejected
- booleans, integers (speed 1–999 or `auto`, offsets, limits), names (`validate_iso_name`),
  job ids and lengths are range-checked before anything runs
- subprocesses are argument lists — no shell string is ever built
- file writes stream to `.part` and rename atomically; directories are
  created `0700`
