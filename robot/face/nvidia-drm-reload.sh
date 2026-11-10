#!/usr/bin/env bash
# nvidia-drm-reload.sh — reload nvidia_drm once, after the Tegra DRM stack is up.
#
# INSTALL TO: /usr/local/sbin/nvidia-drm-reload.sh  (root:root, mode 0755)
#   It MUST NOT live somewhere the unprivileged `jetson` user can write, because
#   it is executed by root via nvidia-drm-reload.service.
#
# WHY THIS EXISTS
#   On this board (Orin Nano Super, L4T R36.5.2) weston's GL renderer fails with
#
#       failed to initialize display
#       EGL error state: EGL_NOT_INITIALIZED (0x3001)
#
#   whenever nvidia_drm is initialised BEFORE the Tegra DRM stack comes up -
#   which is what happens when it is loaded from /etc/modules-load.d at sysinit.
#
#   Reloading the module once on a booted system always restores EGL. Verified
#   repeatedly by hand, and by the drm-order diagnostic:
#
#     boot (sysinit load)            -> EGL_NOT_INITIALIZED
#     after `modprobe -r` + `modprobe` -> EGL vendor: NVIDIA, "Using GL renderer"
#
#   Waiting does NOT help: 3.5 minutes of uptime with the sysinit-loaded module
#   still fails. Only an actual re-initialisation fixes it. So this script does
#   exactly that, at boot, before the compositor starts.
#
#   Cost: roughly 1-2 seconds of extra boot time. Benefit: GL instead of the
#   pixman fallback, which is worth ~3x the face's CPU (162% -> 51% of one core)
#   and leaves that CPU for the dialogue model.
#
# SAFETY
#   * Waits for tegra_drm to be present before reloading, so the ordering the
#     kernel ends up with is the one that works.
#   * Will not fight a module that is genuinely in use; it reports and exits 0
#     rather than forcing an unload.
#   * Always exits 0 unless the module cannot be restored at all, so a problem
#     here can never wedge the boot. face-kiosk has Wants= (not Requires=) on
#     the reload unit, and its own pixman fallback if we fail.

set -u

log() { echo "nvidia-drm-reload: $*"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "nvidia-drm-reload: not root; this script is meant to run from a root unit" >&2
    exit 1
fi

# --- 1. wait for the Tegra DRM stack -------------------------------------
# This is the condition that appears to matter. tegra_drm is not in any
# modules-load.d list; the kernel/udev probes it as a platform driver, which can
# land after systemd-modules-load has already run. Wait for it rather than
# relying on unit ordering.
_waited=0
for _ in $(seq 1 300); do            # up to 30s
    if lsmod | awk '{print $1}' | grep -qx tegra_drm; then
        break
    fi
    sleep 0.1
    _waited=$((_waited + 1))
done
if lsmod | awk '{print $1}' | grep -qx tegra_drm; then
    log "tegra_drm present after ~$((_waited / 10))s"
else
    log "tegra_drm did not appear within 30s; reloading nvidia_drm anyway"
fi

# --- 2. reload nvidia_drm ------------------------------------------------
if ! lsmod | awk '{print $1}' | grep -qx nvidia_drm; then
    log "nvidia_drm not loaded at all; loading it"
    modprobe nvidia_drm modeset=1
    exit $?
fi

_refs=$(lsmod | awk '$1=="nvidia_drm"{print $3}')
if [ "${_refs:-1}" != "0" ]; then
    # Something is holding it. Do not force: that risks a worse failure than the
    # one we are working around. Report and let the kiosk fall back to pixman.
    log "nvidia_drm is in use (refcount ${_refs}); not reloading."
    log "face-kiosk will fall back to the pixman renderer if GL cannot initialise."
    exit 0
fi

log "unloading nvidia_drm"
if ! modprobe -r nvidia_drm; then
    log "could not unload nvidia_drm; leaving it alone"
    exit 0
fi

log "loading nvidia_drm"
if ! modprobe nvidia_drm modeset=1; then
    log "FAILED to reload nvidia_drm; retrying in case the unload left it half-down"
    sleep 1
    modprobe nvidia_drm modeset=1 || {
        log "still failing - the display may be unavailable this boot"
        exit 1
    }
fi

# --- 3. wait for the display node to come back --------------------------
# face-kiosk discovers the card by attribute rather than by number, but it still
# needs the node to exist before it scans.
for _ in $(seq 1 200); do            # up to 20s
    if ls /sys/class/drm/card[0-9]*-*/status >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done

if ls /sys/class/drm/card[0-9]*-*/status >/dev/null 2>&1; then
    log "display nodes present after reload"
else
    log "no DRM connector nodes appeared after the reload"
fi

exit 0
