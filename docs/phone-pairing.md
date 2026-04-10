# Phone ↔ VPS Syncthing pairing

This has to be done manually via the phone Syncthing app — there is no remote API for accepting pending devices on the phone side.

Throughout this doc I use `$VPS` as a shell variable for your VPS SSH target (e.g. `VPS=youruser@vps.example.com`). Set it once before starting:

```bash
VPS=youruser@vps.example.com
```

## One-time setup

1. **Grab the VPS device ID**:
   ```bash
   ssh $VPS 'syncthing --device-id'
   ```
   Copy the long string that prints.

2. **On your phone** (Syncthing-Fork or whichever Syncthing Android app you use):
   - Open the app → **Devices** tab → **+** (add device)
   - Paste the VPS device ID
   - **Device name**: `vps` (or whatever you like)
   - **Addresses**: `tcp://<your-vps-host>:22000` — **do not** leave this on the default `dynamic`. The VPS sits in a datacenter, so LAN discovery won't find it; you need the static address so the phone knows exactly where to dial.
   - **Save**

3. **Wait ~30 seconds** for the VPS to see the incoming connection attempt. Then on the VPS:
   ```bash
   ssh $VPS 'syncthing cli show pending devices'
   ```
   You'll see the phone's device ID as a pending accept. Accept it (and give it a friendly name):
   ```bash
   PHONE_DEVICE_ID=...paste-from-pending-list...
   ssh $VPS "syncthing cli config devices add --device-id $PHONE_DEVICE_ID"
   ssh $VPS "syncthing cli config devices $PHONE_DEVICE_ID name set phone"
   ```

4. **Share the `obsidian-vault` folder from VPS with the phone**:
   ```bash
   ssh $VPS "syncthing cli config folders obsidian-vault \
     devices add --device-id $PHONE_DEVICE_ID"
   ```

5. **On your phone**, Syncthing will now prompt you to accept the incoming folder share. Since your phone probably already has the same folder ID paired with the workstation, the app should offer to just **add the VPS as another sharing device for the existing folder** instead of creating a duplicate. Accept the share and you're done — the phone will start syncing via the VPS.

   If the app instead offers to create a brand-new folder, decline and go to **Folders → Obsidian Vault → Edit → Sharing** and tick the new VPS device checkbox manually. This shares the existing folder (and its existing contents) with the VPS without duplication.

## Topology

After pairing you'll have a full mesh:

```
             workstation (home LAN)
                /        \
               /          \
              /            \
          phone <--------> vps (internet, always-on)
```

Any two devices can sync when both are reachable. When you're out of the house:

- phone ↔ vps works (over internet, including cellular)
- workstation ↔ vps works (whenever the workstation is on)
- phone ↔ workstation syncs when you come home on the LAN

The VPS is always reachable, so effectively everything routes through it when you're mobile — which is the whole point of this setup.

## Samsung (and aggressive-OEM) gotchas — required settings

This is the single biggest headache on Samsung phones running One UI. The standard "Run on mobile data + unrestricted battery" advice from the Syncthing docs is not enough — Samsung's background-process killer is more aggressive than stock Android and will **kill Syncthing within seconds of the screen turning off or WiFi disconnecting** unless you also do these Samsung-specific steps. Xiaomi (MIUI) and Huawei (EMUI) have similar killer behavior and analogous settings.

### Samsung Settings → Battery → Background usage limits

1. **Never sleeping apps** → **Add apps** → select Syncthing → Add. **This is the setting that actually matters.** Without it, "Unrestricted battery" is meaningless — Samsung will still put the app into deep sleep no matter what battery profile you picked.
2. Verify Syncthing is **NOT** in the **Sleeping apps** or **Deep sleeping apps** lists. Remove if present.
3. **Put unused apps to sleep** → off, or at least make sure Syncthing is exempt.

### Samsung Settings → Apps → Syncthing → Battery

4. **Unrestricted** (not "Optimized", not "Restricted")
5. **Allow background activity** → on

### Samsung Settings → Apps → Syncthing → Mobile data

6. **Allow background data usage** → on
7. **Allow data usage while Data saver is on** → on

### Samsung Settings → Device care → Memory → Exclusions

8. Add Syncthing to **Exclusions from RAM cleanup** so the nightly auto-optimiser doesn't kill it.

### Inside the Syncthing-Fork app

9. **Settings → Run conditions → Run on mobile data** → on
10. **Settings → Run conditions → Always run in background** → on
11. **Settings → Run conditions → Respect Android power saving** → off

If any of 1–11 is missed, the symptom is identical: the app shows "disconnected" the moment WiFi is turned off, and the VPS logs show zero connection attempts from the phone's device ID over the following minutes. You can verify whether the daemon is alive at all by opening `http://localhost:8384` in a browser on the phone — if the Syncthing web GUI loads, the daemon is running (even if the UI elsewhere says disconnected); if you get "connection refused", the OS killed it.

### Other Android OEMs

- **Xiaomi (MIUI)**: Settings → Battery → App battery saver → Syncthing → **No restrictions**; Settings → Apps → Permissions → **Autostart** enable for Syncthing.
- **Huawei (EMUI)**: Settings → Battery → App launch → Syncthing → turn OFF "Manage automatically", then manually enable all three sub-toggles (Auto-launch, Secondary launch, Run in background).
- **OnePlus (OxygenOS)**: Settings → Battery → Battery optimization → Syncthing → **Don't optimize**.
- **Stock Android / Pixel**: Settings → Apps → Syncthing → Battery → **Unrestricted** is usually enough, stock Android is the most well-behaved.

## Cellular sync uses the Syncthing relay pool — expected and fine

When the phone is on cellular, it sits behind **carrier-grade NAT (CGNAT)** which blocks incoming connections and often drops long-lived outbound TCP sessions. Direct phone ↔ VPS over cellular **rarely works** on any major mobile carrier. Instead, Syncthing automatically falls back to its **public relay pool** — roughly 1,700 community-run relay servers that proxy encrypted blobs between peers.

From the VPS logs you will typically see incoming connections to your phone device ID from IPs like:

- `149.7.162.131` (Cogent, US) — a common Syncthing relay
- `185.35.202.206` (EU hosting) — another relay
- `51.x.x.x`, `95.x.x.x`, etc. — various community relays

These are **not attackers or misconfigurations**. They are the Syncthing relay pool doing its job. Relay traffic is still end-to-end TLS encrypted between phone and VPS — the relay only sees opaque bytes, not your file contents. Latency is slightly higher (small files ~1–3 seconds via relay vs. <1 second direct LAN) and data usage is slightly higher (~2× overhead because the encrypted payload traverses two network legs instead of one), but for a notes-only vault with small markdown files the difference is imperceptible.

If you want to verify direct cellular connection is possible from your carrier: look for a log entry like `Established secure connection to <PHONE_ID> at <your-vps-ip>:22000-<carrier-mobile-ip>:<port>/tcp-server`. If you only see relay IPs from Cogent/EU hosting ranges, direct is blocked on your carrier (normal for most EU/US mobile networks) and you don't need to fix anything.

## Verifying it works

Drop a test file into `notes/ask/` from anywhere (phone, workstation, tablet):

```bash
echo "What's the square root of 144?" > ~/sync/notes/ask/test.md
```

Within 60 seconds (one responder-timer cycle):

1. Syncthing pushes it to the VPS
2. VPS responder picks it up, queries Groq (primary) or Ollama (fallback)
3. Answer is appended in-place below the question with a `🤖 groq` or `🤖 ollama` marker
4. Syncthing pushes the updated file back to every paired device

Check the responder log for the trace:

```bash
ssh $VPS 'journalctl --user -u oso-responder.service -n 20'
```

You should see lines like:

```
found 1 file(s) in /home/<user>/sync/notes/ask
processing test.md
updated test.md via groq
updated 1/1
```
