# Boot fix, take 2 — reload `nvidia_drm` at boot (proven approach)

Run on `orin` as your normal user; the `sudo` prompts are yours.
Everything is staged in `/tmp/`. Nothing here touches the network.

---

## What changed, and why it's different this time

Take 1 (rename the module list to `zz-`) **did not work** — your reboot proved the
file order was right and GL still failed. That theory is dead.

Take 2 does not guess. It automates **the one thing that has never failed**:
reloading `nvidia_drm` once on a booted system. That single action has restored
EGL every time it has been tried — by hand, and in the diagnostic you just ran.

So this removes the diagnosis problem entirely: there is nothing left to
understand about *why* the first load is bad. We just repeat the step that works,
automatically, at boot.

**Cost:** ~1–2 seconds of boot time. **Benefit:** GL instead of pixman, worth
~3× the face's CPU (162% → 51% of one core), which is CPU the dialogue model
then gets to use.

---

## Step 1 — install the pieces

```bash
sudo install -o root -g root -m 0755 /tmp/nvidia-drm-reload.sh      /usr/local/sbin/nvidia-drm-reload.sh
sudo install -o root -g root -m 0644 /tmp/nvidia-drm-reload.service /etc/systemd/system/nvidia-drm-reload.service
sudo install -o root -g root -m 0644 /tmp/face-kiosk.service        /etc/systemd/system/face-kiosk.service
sudo systemctl daemon-reload
sudo systemctl enable nvidia-drm-reload.service
```

Note the script deliberately goes to `/usr/local/sbin/` and **not** to
`/home/jetson/...` — it runs as root, so it must not sit anywhere the `jetson`
user can write.

---

## Step 2 — check the unit is sane before trusting it

```bash
systemd-analyze verify /etc/systemd/system/nvidia-drm-reload.service
systemctl list-dependencies --after nvidia-drm-reload.service | head
```

Any *ordering cycle* warning here matters — tell me and stop.

---

## Step 3 — test it NOW, without rebooting

This is the whole payoff of take 2: it can be exercised immediately.

```bash
# Stop the kiosk so nvidia_drm is free to unload.
sudo systemctl stop face-kiosk.service

# Run the new unit by hand. This is exactly what boot will do.
sudo systemctl start nvidia-drm-reload.service
journalctl -u nvidia-drm-reload.service -n 20 --no-pager

# Now start the kiosk and see which renderer it gets.
sudo systemctl reset-failed face-kiosk.service
sudo systemctl start face-kiosk.service
sleep 20
journalctl -u face-kiosk.service --since '-60 sec' --no-pager \
  | grep -o 'using the [a-z]* renderer' | tail -1
```

**Success = `using the gl renderer`.**

If it says `pixman`, the reload did not achieve anything new — tell me, and we go
with your fallback idea below, which I've made easier.

---

## Step 4 — reboot, then verify

```bash
sudo reboot
```

From your laptop afterwards:

```bash
ssh jetson@orin.home 'systemctl is-active face-kiosk; \
  journalctl -u face-kiosk -b --no-pager | grep -o "using the [a-z]* renderer" | tail -1; \
  journalctl -u nvidia-drm-reload -b --no-pager | tail -5'
```

Want: `active` + `using the gl renderer`.

---

## Your fallback, if this doesn't stick

You asked for "boot to a login prompt, SSH in, restart the face". That is
available and it is one command — but note it is already *almost* automatic:

**The face already starts by itself on every boot.** The only difference is
*which renderer* it gets. If GL still fails on a cold boot you will still see the
face on the monitor; it will just be the pixman (slower) one. So the fallback
isn't "restart the face" — it's "upgrade the renderer", one command:

```bash
ssh jetson@orin.home 'sudo systemctl restart nvidia-drm-reload face-kiosk'
```

If that is what you'd rather live with for now, say so and I will stop here —
the current state is already a working, self-starting kiosk. The 3× CPU is a
real cost but it is not a blocker, and there are many phases of this project
ahead where it matters less than getting on with them.

---

## If you want me to stop digging

Say the word. Current state is genuinely fine:

- Face starts on its own at every boot, no login needed on the device
- Monitor shows the face, not a prompt
- Fully manageable over SSH
- The only outstanding item is GL vs pixman (CPU cost, not a functional failure)

The remaining rabbit hole is a known NVIDIA-side quirk on Orin + JetPack 6; the
forum thread on it is still open with no posted fix from NVIDIA.
