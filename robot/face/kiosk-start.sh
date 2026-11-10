#!/usr/bin/env bash
# kiosk-start.sh — launch the face on the DisplayPort output, no login.
#
# Used by face-kiosk.service. Kept as a script rather than an inline ExecStart
# so the launch logic can be reasoned about and run by hand.
#
# WHY WESTON IS BACKGROUNDED HERE
#   Weston 13 REMOVED the `weston -- client` syntax (it now fails with
#   "fatal: unhandled option: --"). So we cannot hand the client to weston on
#   the command line. Instead we start the compositor, wait for its Wayland
#   socket to appear, then run the client against it.
#
# EXIT / RESTART CONTRACT
#   This script exits when EITHER process dies, and kills the other on the way
#   out, so the unit never leaves an orphaned compositor behind. systemd's
#   Restart=always then retries after RestartSec. That gives us:
#     * no display connected -> weston exits -> unit retries, no CPU spin
#     * face app crashes      -> unit restarts the whole stack cleanly
#   The delay lives in the unit (RestartSec), not in a loop here.

set -u

FACE_DIR="/home/jetson/robot/face"
FACE_APP="${FACE_DIR}/face.py"
WESTON_CONF="${FACE_DIR}/weston-kiosk.ini"
STATE_DIR="/run/face"
STATE_FILE="${STATE_DIR}/state.json"

export XDG_RUNTIME_DIR="/run/user/1000"
export XDG_CONFIG_HOME="/home/jetson/.config"
export XDG_SESSION_TYPE="wayland"
export WAYLAND_DISPLAY="wayland-face"

# --- runtime dir -----------------------------------------------------------
# weston must create its socket at $XDG_RUNTIME_DIR. At a cold boot of a SYSTEM
# service nothing logs in, so logind has not created /run/user/1000 yet. The
# unit handles this with Wants=user-runtime-dir@1000.service, which makes the
# directory regardless of any login (verified: it creates /run/user/<uid> from
# scratch).
#
# It CANNOT be fixed from inside this script: /run is mode 755 owned by root,
# so an unprivileged user cannot mkdir under it. But it can be WAITED for —
# user-runtime-dir@1000.service normally creates it within a second or two of
# being pulled in, and on a cold boot it is occasionally a beat behind us.
# Waiting avoids a spurious failure that would otherwise cost a whole restart
# cycle; if it never appears the diagnosis below is the right one.
if [ ! -d "${XDG_RUNTIME_DIR}" ]; then
    echo "kiosk-start: waiting for ${XDG_RUNTIME_DIR}" >&2
    for _ in $(seq 1 100); do      # up to ~10s
        [ -d "${XDG_RUNTIME_DIR}" ] && break
        sleep 0.1
    done
fi
if [ ! -d "${XDG_RUNTIME_DIR}" ]; then
    echo "kiosk-start: FATAL ${XDG_RUNTIME_DIR} does not exist." >&2
    echo "  This is a systemd-level problem, not a script bug. Check that" >&2
    echo "  face-kiosk.service declares Requires=user-runtime-dir@1000.service" >&2
    echo "  and After=user-runtime-dir@1000.service, then daemon-reload." >&2
    echo "  Also run once: sudo loginctl enable-linger jetson" >&2
    exit 1
fi
if [ ! -w "${XDG_RUNTIME_DIR}" ]; then
    echo "kiosk-start: FATAL ${XDG_RUNTIME_DIR} is not writable by $(id -un)." >&2
    echo "  weston needs it for its Wayland socket. Check for ProtectHome= or" >&2
    echo "  ProtectSystem=stretch in the unit (ProtectHome also covers /run/user)." >&2
    exit 1
fi

# --- state directory -------------------------------------------------------
# /run is root-owned, so an unprivileged run cannot mkdir here. When started by
# systemd the unit supplies it via RuntimeDirectory=face. For a manual run we
# fall back to the user's XDG runtime dir so the script is still testable
# outside systemd (state is shared with the vision/dialogue components either
# way, as long as FACE_STATE points at it).
if [ -d "${STATE_DIR}" ] && [ -w "${STATE_DIR}" ]; then
    :
elif mkdir -p "${STATE_DIR}" 2>/dev/null; then
    :
else
    STATE_DIR="${XDG_RUNTIME_DIR}/face"
    STATE_FILE="${STATE_DIR}/state.json"
    mkdir -p "${STATE_DIR}"
    echo "kiosk-start: /run/face not writable; using ${STATE_DIR}" >&2
fi
# face.py reads this to find the state file, so whichever directory we settled
# on, the app and the writers agree on it.
export FACE_STATE="${STATE_FILE}"
# Same reasoning for the two files that decide what the screen shows: the session
# conductor writes the session and the voice listener writes the prompt.
# Deliberately NOT seeded: an absent session file means standby, so the robot
# comes up waiting at power-on with nothing having to write anything.
export FACE_SESSION="${STATE_DIR}/session.json"
export FACE_PROMPT="${STATE_DIR}/prompt.json"
# The listening state the pill is drawn from. The voice listener owns this one, not
# the conductor, which is the point: any process can put something on screen by
# publishing a file, and this process is the only one that draws.
export FACE_MIC="${STATE_DIR}/mic.json"
# The tuning session's file, owned by robot/voice/calibrate-lead.sh while someone
# is tuning the lip-sync offset. Same reasoning again: the tool publishes a file,
# this process draws. At standby the screen is the word "Standby" and no face, and
# a tuning trial is judged by watching the projected mouth - so without this there
# is nothing to judge.
export FACE_CALIBRATION="${STATE_DIR}/calibration.json"

# Seed the state file so the face has something to read on first boot and so
# the vision/dialogue components have a well-known path to write to.
if [ ! -s "${STATE_FILE}" ]; then
    printf '%s\n' '{"expression":"neutral","gaze":{"x":0.0,"y":0.0}}' > "${STATE_FILE}"
    chmod 664 "${STATE_FILE}"
fi

# Wait for a display that is actually connected, then pin to it.
#
# DO NOT HARDCODE card1. The DRM card numbering is NOT stable on this board: it
# follows module load order. When gdm loaded nvidia-drm late, it came up as
# card1 (tegra took card0); loading it early from modules-load.d makes it card0
# instead. A hardcoded path silently reports "display not connected" and the
# kiosk never starts even though the monitor is plugged in and lit.
#
# WHY THIS WAITS INSTEAD OF EXITING AT ONCE
#   Exiting immediately means systemd restarts us every RestartSec (5s), which
#   works but writes a full unit-start + failure pair to the journal each time:
#   ~700 cycles/hour, ~2000 lines a day, which buries real faults and makes
#   `systemctl status` unreadable.
#
#   Waiting here is strictly better, and NOT at the cost of self-healing:
#   self-healing comes from Restart=always, not from the retry interval, so it
#   is untouched. Because this loop polls, a display plugged in while we are
#   waiting is picked up in ~1s rather than waiting out a 5s restart gap. So a
#   newly connected projector appears FASTER than before, and the log is ~13x
#   quieter.
#
#   The wait is deliberately BOUNDED. An unbounded wait would be quieter still,
#   but the unit would sit at `active (running)` with nothing on screen — a unit
#   that reports healthy while showing nothing, which is worse than a noisy one.
#   After the deadline we exit non-zero, systemd retries, and the state stays
#   honest.
#
# Override without editing this file:  KIOSK_DISPLAY_WAIT_SECS=0 to disable.
WAIT_SECS="${KIOSK_DISPLAY_WAIT_SECS:-60}"

find_connected_drm() {
    DRM_CARD=""
    DRM_CONNECTOR=""
    for _status in /sys/class/drm/card[0-9]*-*/status; do
        [ -e "${_status}" ] || continue
        if [ "$(cat "${_status}" 2>/dev/null)" = "connected" ]; then
            DRM_CONNECTOR="$(basename "$(dirname "${_status}")")"   # e.g. card0-DP-1
            DRM_CARD="${DRM_CONNECTOR%%-*}"                          # e.g. card0
            return 0
        fi
    done
    return 1
}

_waited=0
_announced=0
while :; do
    if find_connected_drm; then
        if [ "${_waited}" -gt 0 ]; then
            echo "kiosk-start: display appeared after ${_waited}s of waiting" >&2
        fi
        break
    fi

    if [ "${_announced}" -eq 0 ]; then
        if [ "${WAIT_SECS}" -gt 0 ]; then
            echo "kiosk-start: no connected DRM output yet; waiting up to ${WAIT_SECS}s for one" >&2
        fi
        _announced=1
    fi

    if [ "${_waited}" -ge "${WAIT_SECS}" ]; then
        echo "kiosk-start: still no connected DRM output after ${WAIT_SECS}s;" \
             "exiting so systemd retries." >&2
        echo "  scanned: $(ls -d /sys/class/drm/card[0-9]*-* 2>/dev/null | tr '\n' ' ')" >&2
        echo "  if the display IS plugged in and lit, check that it is in the" >&2
        echo "  DisplayPort socket (this board has no HDMI) and powered on." >&2
        exit 1
    fi

    sleep 1
    _waited=$((_waited + 1))
done
echo "kiosk-start: using ${DRM_CARD} (connected output ${DRM_CONNECTOR})" >&2

# --drm-device pins weston to the card we just verified has the display. Without
# it weston picks the first DRM device, which on this board may be the tegra_drm
# card, which has no connectors attached.
#
# The renderer is chosen below, once the trap is installed, so the retry loop can
# start weston more than once without leaking a process.
WESTON_PID=""

cleanup() {
    # Runs on EXIT, and on SIGTERM/SIGINT from systemd.
    #   * Forward the stop to weston and reap it.
    #   * If we were signalled (systemd stopping us), exit 0. A requested stop is
    #     success, not failure — otherwise the unit reports "failed" on every
    #     restart and, worse, could trip Restart=always during shutdown.
    #   * On a normal exit (face app died on its own) fall through so the real
    #     return code propagates and systemd restarts the stack.
    if [ -n "${WESTON_PID}" ]; then
        kill "${WESTON_PID}" 2>/dev/null || true
        wait "${WESTON_PID}" 2>/dev/null || true
    fi
    if [ -n "${SIGNALLED:-}" ]; then
        exit 0
    fi
}
on_term() {
    SIGNALLED=1
    exit 0
}
trap cleanup EXIT
trap on_term INT TERM

# Wait for the compositor's socket. Bounded: if it never appears, weston failed
# to start (no display, no DRM master, bad config) and we exit non-zero so
# systemd can retry.
#
# TWO RENDERERS, GL FIRST, PIXMAN AS THE FALLBACK
#   Why the fallback exists at all: on this image the GL renderer can fail with
#     failed to initialize display
#     EGL error state: EGL_NOT_INITIALIZED (0x3001)
#   depending on when nvidia-drm was loaded relative to the rest of the display
#   stack. The pixman (CPU) renderer initialises reliably in that state. A kiosk
#   showing a slower face beats a kiosk showing nothing, and the GL attempt
#   fails in roughly a second, so trying it first costs almost nothing.
#   Revisit if NVIDIA fixes EGL init ordering in a later L4T.
SOCK="${XDG_RUNTIME_DIR}/${WAYLAND_DISPLAY}"

RENDERER=""
for _r in gl pixman; do
    rm -f "${SOCK}"
    WLOG="${STATE_DIR}/weston-${_r}.log"
    weston \
        --backend=drm-backend.so \
        --drm-device="${DRM_CARD}" \
        --renderer="${_r}" \
        --config="${WESTON_CONF}" \
        --socket="${WAYLAND_DISPLAY}" \
        --log="${WLOG}" &
    WESTON_PID=$!

    _ok=0
    for _ in $(seq 1 100); do
        if [ -S "${SOCK}" ]; then
            _ok=1
            break
        fi
        if ! kill -0 "${WESTON_PID}" 2>/dev/null; then
            break   # weston died; no point waiting out the rest
        fi
        sleep 0.1
    done

    if [ "${_ok}" -eq 1 ]; then
        RENDERER="${_r}"
        echo "kiosk-start: compositor up on ${DRM_CARD} using the ${_r} renderer" >&2
        break
    fi

    echo "kiosk-start: the ${_r} renderer did not produce a compositor;" \
         "see ${WLOG}; trying the next renderer" >&2
    kill "${WESTON_PID}" 2>/dev/null || true
    wait "${WESTON_PID}" 2>/dev/null || true
    WESTON_PID=""
done

if [ -z "${RENDERER}" ]; then
    echo "kiosk-start: no renderer could start weston; exiting so systemd retries." >&2
    exit 1
fi

echo "kiosk-start: compositor up; launching face" >&2

# Run the face in the foreground. When it exits, the EXIT trap tears weston
# down and the unit restarts.
python3 "${FACE_APP}"
FACE_RC=$?

# Distinguish a deliberate stop from a real failure. If systemd is stopping us
# it sends SIGTERM; the trap then runs cleanup and we return. Exiting 0 on a
# requested stop keeps the unit's status honest ("inactive"), while any other
# exit propagates so Restart=always does its job.
echo "kiosk-start: face exited rc=${FACE_RC}" >&2
exit "${FACE_RC}"
