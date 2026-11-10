#!/usr/bin/env bash
# gl-late-load-test.sh — PROVE the EGL/NOT_INITIALIZED diagnosis before rebooting.
#
# THE HYPOTHESIS
#   nvidia_drm is being loaded too early at boot, before nvgpu/nvmap are up, and
#   that leaves EGL unable to initialise. Evidence: the fix file was named
#   "60-nvidia-drm.conf", which sorts BEFORE the vendor "nv.conf", so
#   systemd-modules-load loads nvidia_drm before nvgpu. The kernel log agrees:
#
#     [drm] Initialized nvidia-drm ... on minor 0     <- too early
#     nvmap_page_pool_init: ... 243433 pages          <- nvmap heap, only now
#     nvgpu: 17000000.gpu ... syncpt_unit_base ...    <- nvgpu, only now
#
# THE TEST
#   Load nvidia_drm the way a LATE (i.e. correct) load would: with nvgpu/nvmap
#   already up. If weston's GL renderer initialises now, the diagnosis is right
#   and the reboot will work once the module list is renamed to sort last.
#   Before the fix this same test fails with EGL_NOT_INITIALIZED.
#
# This changes nothing permanent: it unloads and reloads nvidia_drm in the
# running kernel. Reboot to undo.
#
# RUN AS ROOT:  sudo bash /tmp/gl-late-load-test.sh

set -u

if [ "$(id -u)" -ne 0 ]; then
    echo "run me as root:  sudo bash $0" >&2
    exit 1
fi

RT=/run/user/1000
UID_JETSON=$(id -u jetson)

echo "== 1. stop the kiosk so nothing holds card0 =="
systemctl stop face-kiosk.service 2>/dev/null || true
systemctl stop getty@tty1.service   2>/dev/null || true
sleep 2
echo "   nvidia_drm in use by: $(lsmod | awk '$1=="nvidia_drm"{print $3}') process(es)"

echo
echo "== 2. unload and reload nvidia_drm LATE (i.e. after nvgpu/nvmap) =="
if ! modprobe -r nvidia_drm; then
    echo "   !! could not unload nvidia_drm — something still holds it."
    echo "      Reboot and re-run instead; the conclusion is unchanged."
    exit 1
fi
sleep 2
modprobe nvidia_drm modeset=1
sleep 3

echo
echo "== 3. where did the cards land? =="
for c in /sys/class/drm/card[0-9]; do
    [ -e "$c" ] || continue
    drv=$(basename "$(readlink -f "$c/device/driver" 2>/dev/null)")
    conn=$(basename "$(dirname "$(ls -d "$c"/card[0-9]*-*/status 2>/dev/null | head -1)")")
    st=$(cat "$c"/card[0-9]*-*/status 2>/dev/null | head -1)
    printf '   %s -> %-11s %s=%s\n' "$(basename "$c")" "$drv" "$conn" "$st"
done

# Find the card that actually carries a connected output.
CARD=""
for s in /sys/class/drm/card[0-9]*-*/status; do
    [ -e "$s" ] || continue
    if [ "$(cat "$s")" = connected ]; then
        CARD="$(basename "$(dirname "$s")")"; CARD="${CARD%%-*}"
        break
    fi
done
echo "   will use: ${CARD:-<none>}"
[ -n "$CARD" ] || { echo "   !! no connected output"; exit 1; }

echo
echo "== 4. THE TEST: weston GL renderer on ${CARD} =="
rm -f "$RT/gltest"
timeout 12 setpriv --reuid="$UID_JETSON" --regid="$UID_JETSON" --init-groups \
    env XDG_RUNTIME_DIR="$RT" HOME=/home/jetson \
    weston --backend=drm-backend.so --drm-device="$CARD" --renderer=gl \
           --socket=gltest --log=/tmp/gltest.log >/dev/null 2>&1
rc=$?

echo "   weston exit=$rc   socket created: $([ -S "$RT/gltest" ] && echo YES || echo NO)"
echo "   --- renderer lines ---"
grep -E 'EGL vendor|GL version|GL renderer|EGL error|failed to initialize|fatal' \
    /tmp/gltest.log 2>/dev/null | sed 's/^/     /' || echo "     (no log)"

echo
if grep -q 'EGL vendor: NVIDIA' /tmp/gltest.log 2>/dev/null; then
    echo "==> VERDICT: EGL INITIALISES on a LATE load."
    echo "    The diagnosis is confirmed. The reboot will work once"
    echo "    zz-nvidia-drm.conf is installed in /etc/modules-load.d/ and the"
    echo "    old 60-nvidia-drm.conf is removed."
else
    echo "==> VERDICT: EGL still fails on a late load."
    echo "    The load-order theory does NOT explain this. Do not reboot yet —"
    echo "    report back, the fallback to the pixman renderer still gives a"
    echo "    working face and we need to look again."
fi

echo
echo "== 5. restoring =="
rm -f "$RT/gltest"
systemctl start getty@tty1.service 2>/dev/null || true
systemctl start face-kiosk.service 2>/dev/null || true
echo "   kiosk: $(systemctl is-active face-kiosk.service) (pixman fallback still applies)"
