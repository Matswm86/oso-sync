# Syncthing device + folder ledger (template)

This file is a template for tracking which devices are in your OsO Sync mesh and which folders they share. Fill it in with your own values. The full device IDs are deliberately *not* included here — keeping a human-readable ledger with just the first 8 characters is enough for day-to-day reference, and the authoritative source is whichever node you query with `syncthing cli config devices list`.

## Devices

| Name | ID (first 8) | Role | OS | Paired with |
|---|---|---|---|---|
| `workstation` | `XXXXXXX-` | always-on when at home | Linux / macOS / etc. | phone, vps |
| `phone` | `YYYYYYY-` | always-with-user | Android (Syncthing-Fork) | workstation, vps *(see `docs/phone-pairing.md`)* |
| `vps` | `ZZZZZZZ-` | always-on, public internet | any modern Linux | workstation, phone |

Add more rows for tablets, secondary laptops, etc. Any two devices that share a folder need to both list each other as remote devices.

## Folders

| Folder ID | Label | Type | Workstation path | VPS path | Phone path |
|---|---|---|---|---|---|
| `obsidian-vault` | Obsidian Vault | sendreceive | `~/notes` (or wherever your vault lives) | `~/sync/notes` | the Obsidian vault directory on the phone |

The folder ID must match on all devices. The path can be different per device — Syncthing tracks the folder by ID, not path.

## Ports

| Port | Protocol | Purpose | Where to expose |
|---|---|---|---|
| 22000 | tcp | BEP sync protocol | VPS UFW allow-rule required; LAN-only on workstation |
| 21027 | udp | LAN discovery | LAN only (never open on VPS — VPS uses global discovery instead) |
| 8384 | tcp | Web GUI | Always bind to `127.0.0.1` only. Use an SSH tunnel for remote access: `ssh -L 8384:127.0.0.1:8384 <user@host>` |

## Audit checklist

Use this when setting up a new device or periodically reviewing the mesh:

- [ ] Each device has a friendly name set (`syncthing cli config devices <ID> name set <name>`)
- [ ] The VPS device has an explicit static address (`tcp://<vps-host>:22000`) on both the workstation and the phone — not just the default `dynamic`, because the VPS is not on your LAN so LAN discovery won't find it
- [ ] The VPS Syncthing web GUI is bound to `127.0.0.1` only (`netstat -tlnp | grep 8384` should show only `127.0.0.1:8384`)
- [ ] The shared folder has all expected devices listed on every device
- [ ] Phone ↔ VPS pairing completed and verified (see `docs/phone-pairing.md`)
- [ ] UFW on the VPS only opens `22000/tcp`, not `21027/udp` or `8384`
- [ ] `.env` and `secrets/` are in `.gitignore` (they are by default in this repo)
