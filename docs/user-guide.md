# User guide

The client window is a left/right split: **drive list + details** on the
left, the working **tabs** on the right, a toolbar on top and a status bar
(current job) below.

```
[ Host: ▾ ] [Connect] [Profiles…] [Refresh drives] [Eject]   ● connected
┌ Optical drives ───────┐  ┌ Browse | Image | Burn | Queue | Log ──────────┐
│ /dev/sr0  DVD±RW      │  │                                              │
│   MEDIA CD-ROM …      │  │              tab content                     │
│   LABEL RESOURCECD    │  │                                              │
│ details…              │  │                                              │
└───────────────────────┘  └──────────────────────────────────────────────┘
 Ready                                                                     Burning 42%
```

---

## 1. Profiles (connection settings)

**Profiles…** opens the profile dialog:

| Field | Meaning |
| --- | --- |
| Name | label in the toolbar host combo |
| Host / IP | drive host, e.g. `192.168.1.50` |
| SSH user, SSH port, SSH key | key path left empty = default keys (`~/.ssh/id_*`) |
| Agent command | remote path to the wrapper, default `~/rodm/bin/rodm-agent` |
| Use the REST API | enables HTTPS transport (leave on for job progress) |
| Scheme / REST port | `https` / `8443` |
| Bearer token | stored in `profiles.json`; **Fetch via SSH** reads it from the agent for you |
| **Test connection** | connects for real (SSH + REST), reports agent version and detected drives |
| **Forget certificate** | deletes the pinned TLS fingerprint for this host:port — use after reinstalling the agent (new certificate) |

Saving writes `~/.config/rodm/profiles.json` (mode `0600`).

## 2. Connect

**Connect** in the toolbar:

1. opens the SSH transport and runs `rodm-agent rpc ping`
2. fetches the REST token over SSH if the profile has none
3. opens REST (`https://host:8443`); on first contact you get
   **trust-on-first-use** certificate pinning (`~/.config/rodm/certs/`)
4. refreshes the drive list

The connection indicator in the toolbar shows the primary transport and the
agent version (`spider: SSH (agent 0.1.0)`), or `offline` when disconnected.
Failed transports are reported in the Log tab as warnings.

## 3. Browse tab — walk a disc

1. Select a drive on the left, press **Mount** (or just switch to the tab —
   mounting happens automatically when needed)
2. Type a path or double-click directories; **Up** moves one level
3. **Download selected…** copies files to a local directory — streamed via
   SFTP (or the REST file endpoint as fallback)
4. **Unmount** / **Eject** when finished

Notes:

- Only *mounted data discs* are browsable. Blank media and audio CDs raise
  `no_filesystem` ("the disc is blank, unformatted or an audio CD") and
  cannot be mounted — audio CDs are not supported by this tool at all.
- Each request re-mounts only if needed (`mount=true` default in the API).

## 4. Image tab — rip and build ISOs

**Rip a physical disc on the remote drive**

1. ISO name (extension `.iso` is added automatically)
2. optional *Overwrite if the ISO already exists*
3. **Rip disc to ISO** — a job streams `/dev/sr0` into
   `~/.local/share/rodm/images/` with live progress; **Cancel job** works

**Create an ISO from local files**

1. **Choose files…** / **Choose folder…** (every file is first uploaded to
   the agent's `stage/` directory — SFTP, with REST as fallback — and you
   watch the progress per file)
2. volume label (≤ 32 chars) and **Build ISO from files**
3. `genisoimage` runs on the agent; its output streams line by line into
   the progress box

**ISO images stored on the agent** lists everything in `images/` with size
(`*.iso`/`*.img`); **Refresh list** rescans, **Use in Burn tab** jumps over
with the image preselected (or double-click a row).

## 5. Burn tab — write an ISO

1. **Refresh** then pick the ISO in the combo
2. Options:
   - **Speed** — `auto`, `1x … 48x`
   - **Simulate** — trial run: `wodim` rehearses the write without
     touching the dye
   - **Allow overburning** — write past the official capacity
   - **Eject when finished**
   - **Verify against the source ISO** — after the burn, a second job
     streams the disc back (both ISO window offsets, 0 and 32 KiB) and
     compares its MD5 against the source file without writing to disk
3. **Start burn** → progress bar, `wodim` log lines in the status area;
   **Cancel** aborts the job (SIGTERM to `wodim` after a grace period —
   the disc ends up unusable)

An empty drive fails immediately with `no_media` ("no disc is inserted") —
insert media and close the tray first. A read-only drive or an image larger
than the medium is rejected before writing (the size check is skipped when
**Allow overburning** is set); blank media itself is enforced by `wodim`.

## 6. Queue tab — burn several discs

1. **Add to queue** appends the currently selected ISO + drive (repeat for
   several ISOs; **Remove selected** / **Clear** to edit)
2. **Start queue** burns one entry after another. Before each entry the
   queue checks the drive: if it is empty you get an **Insert next disc**
   dialog (press OK once the disc is seated; 15-minute timeout). The queue
   does not eject by itself — press **Eject** in the toolbar after each
   finished disc so the next check sees an empty drive
3. First failure stops the queue — fix the cause, then **Start queue**
   again (finished entries are already marked)

The queue is client-side: it simply submits one `burn_iso` job after the
previous one finished.

## 7. Log tab

Live log of client and agent operations with a level filter
(DEBUG/INFO/WARNING/ERROR). Everything is also written to
`~/.local/state/rodm/logs/rodm.log`; the agent logs to
`~/.local/state/rodm/logs/agent.log` on its host.

## 8. Status bar

- left: last user-facing message
- right: current job ("Burning 42%") — persists across tab switches

## Typical workflows

**Back up a data DVD**

```
Insert disc → Browse (check contents) → Image → "Rip disc to ISO"
→ done. ISO is now in images/ on the agent.
```

**Make a bootable/any ISO and burn it**

```
Image → choose files/folder → Build ISO → Burn tab → Refresh →
pick ISO → speed auto → Verify ✓ → Start burn → swap disc if a queue.
```

**Read a disc from another machine with no optical drive**

```
Browse → Mount → double-click dirs → Download selected… → local disk.
```

## Where things land

| What | Where |
| --- | --- |
| Ripped/built ISOs | `<agent data root>/images/` on the drive host |
| Upload staging | `<agent data root>/stage/` |
| Mountpoints | `<agent data root>/mnt/<device>_<label>/` (e.g. `mnt/sr0_RESOURCECD/`) |
| Profiles / pinned certs | `~/.config/rodm/` (client) |
| Logs | `~/.local/state/rodm/logs/` (both sides) |

See also: [api.md](api.md) (protocol), [troubleshooting.md](troubleshooting.md).
