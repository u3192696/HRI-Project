#!/usr/bin/env bash
# drm-order-diagnostic.sh — capture exactly what changes when nvidia_drm reloads,
# and whether that is what makes weston's GL renderer work.
#
# THE QUESTION
#   On this board weston+GL works when nvidia_drm is loaded late/manually, and
#   fails with EGL_NOT_INITIALIZED when it is loaded at sysinit from
#   /etc/modules-load.d - no matter how long you wait afterwards.
#
#   Two candidate explanations, and the fix is different for each:
#
#   (A) MINOR/ORDER: the two DRM drivers get swapped minor numbers depending on
#       load order, and weston's gl-renderer.so resolves /dev/dri/renderD129
#       without regard to the card it was given. If the mapping FLIPS across the
#       reload, the fix is a load-order declaration:
#           /etc/modprobe.d/zz-nvidia-drm-order.conf
#       with:  softdep nvidia_drm pre: tegra_drm
#
#   (B) MODULE INIT STATE: the mapping does NOT change, and a boot-loaded
#       nvidia_drm is simply unusable for EGL until it is re-initialised. The
#       fix is then to reload it once at boot from a unit.
#
#   Run this and the BEFORE/AFTER tables say which one it is.
#
# RUN AS ROOT:  sudo bash /tmp/drm-order-diagnostic.sh

set -u
[ "$(id -u)" -eq 0 ] || { echo "run me as root:  sudo bash $0" >&2; exit 1; }

snap() {
    echo "  cards:"
    for c in /sys/class/drm/card[0-9]; do
        [ -e "$c" ] || continue
        printf '    %-6s dev=%-7s driver=%s\n' "$(basename "$c")" \
            "$(cat "$c/dev")" "$(basename "$(readlink -f "$c/device/driver" 2>/dev/null)")"
    done
    echo "  render nodes:"
    for r in /sys/class/drm/renderD*; do
        [ -e "$r" ] || continue
        printf '    %-11s dev=%-7s driver=%s\n' "$(basename "$r")" \
            "$(cat "$r/dev")" "$(basename "$(readlink -f "$r/device/driver" 2>/dev/null)")"
    done
    echo "  by-path (which driver owns which card):"
    for p in /dev/dri/by-path/*-card; do
        [ -e "$p" ] || continue
        printf '    %-42s -> %s\n' "$(basename "$p")" "$(basename "$(readlink -f "$p")")"
    done
    echo "  module refcounts:"
    lsmod | awk '$1=="nvidia_drm"||$1=="tegra_drm"{printf "    %-12s refcount=%s\n",$1,$3}'
}

renderer_of_service() {
    journalctl -u face-kiosk.service --since '-60 sec' --no-pager 2>/dev/null \
        | grep -oE 'compositor up on [a-z0-9]+ using the [a-z]+ renderer' | tail -1
}

echo "=============================================================="
echo " STEP 1 - BEFORE, on the sysinit-loaded module (GL fails here)"
echo "=============================================================="
snap

echo
echo "=============================================================="
echo " STEP 2 - reload nvidia_drm, exactly as the working manual case"
echo "=============================================================="
systemctl stop face-kiosk.service 2>/dev/null || true
sleep 2
if ! modprobe -r nvidia_drm; then
    echo "  !! could not unload nvidia_drm (something still holds it)."
    echo "     Stop anything using the display and re-run."
    exit 1
fi
echo "  unloaded nvidia_drm"
sleep 2
modprobe nvidia_drm modeset=1
echo "  loaded nvidia_drm"
sleep 3

echo
echo "=============================================================="
echo " STEP 3 - AFTER the reload (GL works here)"
echo "=============================================================="
snap

echo
echo "=============================================================="
echo " STEP 4 - does the SERVICE now get GL?"
echo "=============================================================="
systemctl reset-failed face-kiosk.service 2>/dev/null || true
systemctl start face-kiosk.service
sleep 22
printf '  state:    %s / %s\n' \
    "$(systemctl show face-kiosk.service -p ActiveState --value)" \
    "$(systemctl show face-kiosk.service -p SubState --value)"
printf '  renderer: %s\n' "$(renderer_of_service)"

echo
echo "=============================================================="
echo " VERDICT - read the STEP 1 vs STEP 3 tables"
echo "=============================================================="
echo "  If the render-node / card assignments are THE SAME in both,"
echo "  it is case (B): module init state. The softdep fix will NOT"
echo "  help; we need a one-shot reload at boot instead."
echo
echo "  If they are SWAPPED, it is case (A): install"
echo "  /etc/modprobe.d/zz-nvidia-drm-order.conf"
echo "  containing:  softdep nvidia_drm pre: tegra_drm"
echo "  and the boot will then match the working state."
