# Kiosk boot fix — run these on `orin`

Everything below is to be run **by you** on the Jetson (it needs root). Only the
one `scp` touches your laptop.

---

## Step 0 — optional but recommended: prove the diagnosis first

This unloads and reloads `nvidia_drm` in the running kernel, which is exactly
what the fixed boot order will do. It takes about 20 seconds and **tells you
whether the reboot will work before you spend one**.

```bash
sudo bash /tmp/gl-late-load-test.sh
```

Read the last few lines. If it says

```
==> VERDICT: EGL INITIALISES on a LATE load.
```

then the diagnosis is confirmed and step 3 will work. If it says EGL still
fails, **stop and tell me** — do not reboot, and don't worry, the pixman
fallback still gives you a working face.

---

## Step 1 — copy the new script into place

Already done by me (`/home/jetson/robot/face/kiosk-start.sh`). Nothing to do.

---

## Step 2 — install the corrected module list

Remove the old one (it sorts **before** the vendor list, which is the whole bug),
install the new one, and make sure the other two files are in place:

```bash
sudo rm -f /etc/modules-load.d/60-nvidia-drm.conf
sudo install -o root -g root -m 0644 /tmp/zz-nvidia-drm.conf /etc/modules-load.d/zz-nvidia-drm.conf
sudo install -o root -g root -m 0644 /tmp/face-kiosk.service /etc/systemd/system/face-kiosk.service
sudo systemctl daemon-reload
```

Verify the ordering is what we expect — `zz-nvidia-drm.conf` must come **last**,
after `nv.conf`:

```bash
ls /usr/lib/modules-load.d/*.conf /etc/modules-load.d/*.conf | xargs -n1 basename | sort
```

Expected order:

```
fwupd-i2c.conf
modules.conf
nv.conf
zz-nvidia-drm.conf      <-- last
```

---

## Step 3 — remove the `/run/user/1000` race

```bash
sudo loginctl enable-linger jetson
```

This makes logind create `/run/user/1000` at boot and keep it up, instead of
depending on a service that can be torn down at the wrong moment (`/run/user/1000`
was the cause of one real failure during this session).

Verify:

```bash
loginctl show-user jetson -p Linger
# want: Linger=yes
```

---

## Step 4 — sanity-check the unit before rebooting

```bash
systemd-analyze verify /etc/systemd/system/face-kiosk.service
sudo systemctl reset-failed face-kiosk.service
sudo systemctl restart face-kiosk.service
sleep 20
systemctl status face-kiosk --no-pager | head -12
journalctl -u face-kiosk --since '-60 sec' --no-pager | grep -E 'kiosk-start|renderer'
```

**What you want to see** in that last command:

```
kiosk-start: compositor up on card0 using the gl renderer
```

`the gl renderer` — not `pixman`. If it says `pixman`, GL is still broken; the
face will still be on screen, but tell me, because it is running at ~2.2× the
CPU for no reason.

---

## Step 5 — the reboot

```bash
sudo reboot
```

After it comes up, the monitor should show the **face**, not a `tty1` login
prompt. Then, from your laptop:

```bash
ssh jetson@orin.home 'journalctl -u face-kiosk -b --no-pager | grep -E "renderer|kiosk-start" | tail -5'
```

Want: `ActiveState=active` and `using the gl renderer`.

---

## If it still fails

Come back with the output of:

```bash
systemctl status face-kiosk --no-pager | head -20
journalctl -u face-kiosk -b --no-pager | tail -40
ls -1 /etc/modules-load.d/
cat /sys/class/drm/card*/card*-DP-1/status
```

The face will still come up on the pixman renderer in that case — you will have a
working display, just a slower one — so this is never a dead end.

---

## What was actually wrong (short version)

Three separate faults, stacked:

1. **Nothing loaded `nvidia-drm` at boot.** Removing gdm removed the thing that
   had been loading the display driver incidentally. Fixed with a file in
   `/etc/modules-load.d/`.

2. **That fix was named so it sorted too early.** `60-nvidia-drm.conf` sorts
   before the vendor `nv.conf`, so `nvidia_drm` came up *before* `nvgpu`/`nvmap`.
   Nothing failed at load time, but EGL was then broken for the whole boot, so
   the compositor died with `EGL_NOT_INITIALIZED`. The `zz-` prefix fixes it.

3. **Card numbering moved when the load order changed** — `nvidia-drm` is now
   `card0`, not `card1` — and the start script had a hardcoded card path. It now
   discovers whichever card has a connected output.

Plus a smaller one: `/run/user/1000` could vanish mid-restart because
`user-runtime-dir@1000.service` is `StopWhenUnneeded=yes`; linger plus
`Requires=` settles it.
