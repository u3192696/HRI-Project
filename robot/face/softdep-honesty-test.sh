#!/usr/bin/env bash
# softdep-honesty-test.sh — will the "load tegra_drm before nvidia_drm" fix
# actually work? Answer it here, without rebooting, BEFORE installing it.
#
# WHY THIS EXISTS
#   Three cases were observed on this board:
#     nvidia_drm initialised BEFORE tegra_drm  -> GL dead  (modules-load.d boot)
#     nvidia_drm initialised AFTER  tegra_drm  -> GL works (gdm, manual reload)
#   which suggests forcing the order might fix it. But that correlation has a
#   confound: in every working case nvidia_drm was loaded into a system that was
#   ALREADY FULLY UP, not merely "after tegra_drm". If the thing that matters is
#   "the display stack is fully up", then a softdep that merely swaps two
#   modules at sysinit will NOT fix anything - both would still load during the
#   same early burst.
#
# WHAT THIS DOES
#   Reproduces the boot order as closely as possible on the running system:
#   unload BOTH tegra_drm and nvidia_drm, then load them back in the NEW order
#   (tegra_drm first, nvidia_drm second), then immediately try weston with GL.
#
#   If GL initialises -> the ordering is what matters; the softdep fix is sound.
#   If GL fails      -> ordering is not enough; go to the fallback (reload
#                       nvidia_drm at boot from a unit, or accept pixman).
#
#   This is not a perfect boot replica, but it is far cheaper than a reboot and
#   it is the decisive question.
#
# RUN AS ROOT:  sudo bash /tmp/softdep-honesty-test.sh

set -u
[ "$(id -u)" -eq 0 ] || { echo "run me as root:  sudo bash $0" >&2; exit 1; }

echo "== stop anything holding DRM =="
systemctl stop face-kiosk.service 2>/dev/null || true
sleep 3

echo
echo "== unload both DRM drivers (tegra_drm first, then nvidia_drm) =="
# Order matters for unloading too: nvidia_drm is the one with a user normally.
modprobe -r nvidia_drm 2>&1 | sed 's/^/   /' || echo "   (nvidia_drm unload failed)"
sleep 1
if ! modprobe -r tegra_drm 2>&1 | sed 's/^/   /'; then
    echo "   !! tegra_drm is in use and cannot be unloaded."
    echo "      This test cannot run. Do not take this as a result either way."
    modprobe nvidia_drm modeset=1 2>/dev/null
    systemctl start face-kiosk.service 2>/dev/null
    exit 1
fi
sleep 2
echo "   remaining: $(lsmod | awk '$1=="tegra_drm"||$1=="nvidia_drm"{printf "%s=%s ",$1,$3}') (expect empty)"

echo
echo "== reload in the NEW order: tegra_drm FIRST, then nvidia_drm =="
modprobe tegra_drm
sleep 2
modprobe nvidia_drm modeset=1
sleep 3

echo "   kernel log ordering for this sequence:"
journalctl -k --since '-20 sec' --no-pager 2>/dev/null \
    | grep -E 'Initialized tegra|Initialized nvidia-drm' \
    | sed 's/^/     /'
echo "   (want tegra above nvidia-drm)"

echo
echo "== THE TEST: weston GL renderer, immediately after that load =="
rm -f /run/user/1000/honest
timeout 12 setpriv --reuid=1000 --regid=1000 --clear-groups env \
    XDG_RUNTIME_DIR=/run/user/1000 HOME=/home/jetson \
    weston --backend=drm-backend.so --drm-device=card0 --renderer=gl \
           --socket=honest --log=/tmp/honest.log >/dev/null 2>&1
echo "   exit=$?"
grep -E 'EGL vendor|GL version|Using GL renderer|EGL error state|failed to initialize|fatal' \
    /tmp/honest.log 2>/dev/null | sed 's/^/     /' || echo "     (no log written)"

echo
if grep -q 'EGL vendor: NVIDIA' /tmp/honest.log 2>/dev/null; then
    echo "==> VERDICT: GL WORKS with tegra_drm loaded first."
    echo "    The softdep fix is sound. Install zz-nvidia-drm-order.conf and reboot."
else
    echo "==> VERDICT: GL still FAILS with tegra_drm loaded first."
    echo "    Ordering alone is not the cause. The softdep fix will NOT work —"
    echo "    do not install it. Go to the fallback: reload nvidia_drm once at"
    echo "    boot from a unit, after the display stack is up."
fi

echo
echo "== restoring the normal stack =="
rm -f /run/user/1000/honest
systemctl reset-failed face-kiosk.service 2>/dev/null || true
systemctl start face-kiosk.service 2>/dev/null || true
sleep 18
echo "   kiosk: $(systemctl is-active face-kiosk.service)"
echo "   renderer: $(journalctl -u face-kiosk.service --since '-30 sec' --no-pager 2>/dev/null | grep -o 'using the [a-z]* renderer' | tail -1)"
