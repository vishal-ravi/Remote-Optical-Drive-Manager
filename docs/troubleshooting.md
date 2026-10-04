# Troubleshooting

Start with the two doctor commands — they cover 80 % of the cases:

```bash
ssh user@host '~/rodm/bin/rodm-agent status --tools'   # drives + media + tool paths
ssh user@host '~/rodm/bin/rodm-agent doctor'           # paths, token, write access
systemctl status rodm-agent --no-pager                 # service state (root mode)
journalctl -u rodm-agent -n 50 --no-pager               # agent log
```

Client side: **Log tab** (levels DEBUG…ERROR) and
`~/.local/state/rodm/logs/rodm.log`.

---

## Connection

| Symptom | Cause & fix |
| --- | --- |
| `Permission denied (publickey,password)` | no key installed → `ssh-copy-id -i ~/.ssh/id_ed25519.pub user@host`; check profile *SSH key* path |
| `ssh: connect to host … port 22: Connection refused` | sshd not running on the drive host, wrong IP/port |
| `Connection refused` / `connection_error` on REST | agent not running → `sudo systemctl start rodm-agent` (or start `serve` manually); check the port: `ss -ltnp \| grep 8443`; check firewall |
| Client stuck on "connecting" after upgrading the agent | restart the client so it picks up new code: close it and run `./scripts/run_client.sh` again |
| `command not found: ~/rodm/bin/rodm-agent` (SSH RPC) | wrapper missing/wrong path → re-run `install_agent.sh`, or set *Agent command* in the profile |
| REST answers but SSH RPC fails (or vice versa) | the GUI keeps whichever transport works; only job ops strictly need REST |

## Authentication / TLS

| Symptom | Cause & fix |
| --- | --- |
| `unauthorized` / HTTP 401 | missing/wrong bearer token → profile → **Fetch via SSH**; on the host `rodm-agent token` should print the same value; `RODM_TOKEN` overrides the file |
| `TLS fingerprint mismatch`, `certificate verify failed` | the server certificate changed (agent reinstalled, host IP changed) → **Profiles… → Forget certificate → Test connection → Save** |
| `SSL: CERTIFICATE_VERIFY_FAILED` from scripts | pass the pinned file: `curl --cacert ~/.config/rodm/certs/<host>-<port>.pem …` |
| "certificate will not be trusted" warnings on first connect | normal — trust-on-first-use; confirm the host, then continue |
| Want to test without TLS | `--insecure` only takes effect when no certificate exists yet → use a throwaway data root: `rodm-agent serve --insecure --data-root /tmp/rodm-test --host 127.0.0.1`, then `curl http://127.0.0.1:8443/v1/health` (loopback only) |

## Drives & media

| Symptom (code) | Cause & fix |
| --- | --- |
| `no optical drives found` | no `/dev/sr*`: check `ls -l /dev/sr*`, cables/VM passthrough; in a VM the drive must be passed through |
| `no_media` / "disc not present" | insert a disc; try `eject` then re-insert (tray closed = `close_tray` first) |
| `no_filesystem` / "the disc is blank, unformatted or an audio CD" | blank or audio media cannot be browsed — only data discs; rip raw images with `create_iso_from_disc` if you know what you are doing (audio CDs are unsupported) |
| `invalid_device` / "not an optical drive" | only `/dev/sr*`, `/dev/cdrom*`, `/dev/scd*`, `/dev/optical*` are accepted (system disks are structurally rejected) |
| browse shows an empty directory | disc is data but you are at `/`; use **Up**/path, or the disc's content really is there — refresh |
| `not mounted` | call with `mount:false`; press **Mount** in the Browse tab |

## Mount / permissions

| Symptom (code) | Cause & fix |
| --- | --- |
| `must be superuser to unmount/mount`, `permission denied` | agent lacks privilege → reinstall with `install_agent.sh … --root`, **or** grant passwordless sudo for the exact commands (the error message prints a ready-made `sudoers.d` line), **or** ensure `udisksctl` + polkit allow your user |
| `check that udisks2/polkit allow this user to mount` | `udisksctl mount -b /dev/sr0` fails interactively → install/enable `udisks2`, or use the `--root` agent |
| `close any programs still using the disc` (unmount) | something holds the mount → `lsof +f -- /path/to/mnt` or `fuser -vm /dev/sr0`, close it, retry |
| `is the tray locked?` (eject) | physical lock switch on the drive, or a stuck tray → power-cycle the drive |
| mounts land in `~/.local/share/rodm/mnt/` | by design (no host pollution); *Eject* unmounts first |

## ISO, burning, verification

| Symptom (code) | Cause & fix |
| --- | --- |
| `tool_missing: 'wodim'/'genisoimage'` | `install_agent.sh user@host --install-packages`, or `sudo apt install wodim genisoimage`; confirm with `status --tools` |
| `conflict: … already exists (use overwrite)` | tick **Overwrite if the ISO already exists** (or delete `images/<name>.iso`) |
| burn fails immediately: "no media" / "not writable" | blank media required; `-R/-RW/+R/+RW` supported by `wodim`, not pressed DVD-ROMs |
| burn fails: medium error / power calibration | clean the drive lens, try a lower speed (`4x`), another disc brand, or **Simulate** first |
| `does not fit on the medium … enable overburn` | image larger than the disc → free space/use a bigger disc, or enable **Allow overburning** (only helps on `-R`/`+R` media) |
| `job_not_found` | the agent restarted (jobs live in memory) or you queried a different process → re-submit the operation |
| `connection_error: … needs the REST API` | job operations only exist in the long-lived service → start `rodm-agent serve` (REST is down) |
| verification fails | mismatch of the read-back MD5 → lower burn speed, different media, clean drive; **Simulate** burns are not on disc |
| ISO bigger than free space | images live in the data root → check `df -h ~`; raise nothing, just free space |

## System / OS

| Symptom | Cause & fix |
| --- | --- |
| `E: Could not get lock /var/lib/dpkg/lock-frontend` | another apt/dpkg is running (possibly stuck for days) → `ps aux \| grep -E 'apt\|dpkg\|unattended'`; if wedged: `sudo kill <pid>`, then `sudo dpkg --configure -a` and verify `dpkg -l \| grep -c ^iU` → 0 |
| `sudo: a terminal is required` / password prompts in logs | the user service tried `sudo -n` without a sudoers rule → use `--root` or add the rule from the error hint |
| Qt: `Cannot load library … libxcb-cursor.so.0` | `sudo apt install libxcb-cursor0` **on the client** |
| Qt: `could not find or load the Qt platform plugin "xcb"` | missing X libs (`libxcb-xinerama0 libxkbcommon-x11-0`), or no `$DISPLAY` (SSH without `-X`) |
| GUI dies with DBus/xkb errors | it ran as root → `run_client.sh` refuses on purpose; start it as your normal user |
| `QThread: Destroyed while thread …` / abort | old client build → restart `./scripts/run_client.sh` (all task threads are now owned and joined on close) |
| `systemctl start rodm-agent` → `start-limit-hit` | crash loop → `sudo systemctl reset-failed rodm-agent` and inspect `journalctl -u rodm-agent -n 100` |
| port already in use | another process holds 8443 → `ss -ltnp \| grep 8443`, or `serve --port 9443` (update the profile) |
| agent runs but as wrong data root | `HOME` differs (root service without `Environment=HOME=…`) → reinstall with `--root` which sets it; check `status`/`doctor` output paths |

## Diagnostic snippets

```bash
# is the service healthy end-to-end?
ssh user@host '~/rodm/bin/rodm-agent doctor; ~/rodm/bin/rodm-agent status --tools'

# raw REST check (pinned cert + token)
TOKEN=$(ssh user@host 'cat ~/.local/share/rodm/agent.token')
curl -sf --cacert ~/.config/rodm/certs/192.168.1.50-8443.pem \
     -H "Authorization: Bearer $TOKEN" -X POST https://192.168.1.50:8443/v1/rpc \
     -d '{"v":1,"id":"t","op":"version","params":{}}'

# one-off SSH RPC (no token needed)
ssh user@host '~/rodm/bin/rodm-agent rpc' <<'EOF'
{"v":1,"id":"t","op":"list_drives","params":{}}
EOF

# watch a burn live
ssh user@host 'journalctl -u rodm-agent -f'
```

Still stuck: capture the client Log tab (DEBUG), `journalctl -u rodm-agent`
around the failure, and the exact error code from `api.md`.
