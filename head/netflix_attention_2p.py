"""
Netflix Attention Controller — 2-Person Edition
================================================
Two separate behaviours based on what the camera sees:

  BOTH LOOKING AT SCREEN
    → Netflix PLAYS (or resumes if it was paused).

  EITHER / BOTH LOOKING AWAY (face still visible, head turned)
    → Netflix PAUSES after AWAY_GRACE_SEC seconds.
    → Netflix RESUMES only when BOTH are looking back.

  EITHER / BOTH LEAVE FRAME ENTIRELY
    → Netflix KEEPS PLAYING — treat absence like "got water".
    → A seek-back timer accumulates while at least one person is gone.
    → When all absent viewers return the video SEEKS BACK so they
      don't miss anything.

Person identity (P1 / P2):
    Faces are sorted left-to-right each frame by their nose-bridge
    X coordinate.  P1 = leftmost face, P2 = rightmost face.
    No persistent tracker needed — identity is purely positional.

Calibration:
    Each person gets their own 30-frame auto-calibration that runs
    the first time their face is stable in the frame.  Until both
    are calibrated the look-away logic is inactive for that person.

Controls:
    q  — Quit
"""

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision
from mediapipe.tasks.python.vision import FaceLandmarkerOptions, FaceLandmarker
import numpy as np
import math
import time
import os
import subprocess
import sys
from collections import deque

# ─────────────────────────── CONFIG ─────────────────────────── #
CAMERA_INDEX        = 0
FILTER_LENGTH       = 10          # smoothing window (frames)
YAW_THRESHOLD_DEG   = 25
PITCH_THRESHOLD_DEG = 20
AUTO_CALIB_FRAMES   = 30          # frames gathered per person

# Digital zoom: crops the centre (1/DIGITAL_ZOOM) of the frame and
# stretches to full size so distant faces are large enough for MediaPipe.
# Display always shows the original unzoomed frame.
# 1.0 = no zoom | 2.0 = good for ~2 m | 3.0 = very far
DIGITAL_ZOOM        = 2.0

# Grace periods (seconds)
AWAY_GRACE_SEC      = 1.5    # head turned away → pause after this long
BACK_GRACE_SEC      = 0.8    # both heads back   → resume after this long

# Face absences shorter than this are ignored for seek-back purposes
# (e.g. blinks / brief detection dropouts).
MIN_ABSENT_FOR_SEEK_SEC = 2.0

MODEL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "face_landmarker.task"
)

# Head pose landmark indices (MediaPipe 478-point topology)
HEAD_LANDMARKS = {"left": 234, "right": 454, "top": 10, "bottom": 152, "front": 1}

# ─────────────────────── NETFLIX CONTROL ────────────────────── #


def _nf_js(action: str) -> str:
    # Wrap the action in a function that returns the result directly to AppleScript
    return (
        f"(function(){{"
        f"  try {{"
        f"    var v = document.querySelector('video');"
        f"    var n = window.netflix || window.__netflix || (typeof netflix !== 'undefined' ? netflix : null);"
        f"    var pl = null;"
        f"    if (n) {{"
        f"      try {{"
        f"        var vp = n.appContext.state.playerApp.getAPI().videoPlayer;"
        f"        var ids = vp.getAllPlayerSessionIds();"
        f"        if (ids.length > 0) pl = vp.getVideoPlayerBySessionId(ids[0]);"
        f"      }} catch(e) {{}}"
        f"    }}"
        f"    if (!pl && !v) return 'ERROR:No video or Netflix API found';"
        f"    "
        f"    var _play = function() {{ if(pl) pl.play(); else v.play(); }};"
        f"    var _pause = function() {{ if(pl) pl.pause(); else v.pause(); }};"
        f"    var _getTime = function() {{ return pl ? pl.getCurrentTime() : (v.currentTime * 1000); }};"
        f"    var _seek = function(m) {{ if(pl) pl.seek(m); else v.currentTime = m/1000.0; }};"
        f"    "
        f"    {action.replace('pl.play()', '_play()').replace('pl.pause()', '_pause()').replace('pl.getCurrentTime()', '_getTime()').replace('pl.seek', '_seek')}"
        f"  }} catch (e) {{ return 'ERROR:' + e.message; }}"
        f"}})();"
    )


_JS_PLAY     = _nf_js("pl.play();  return 'PLAYING';")
_JS_PAUSE    = _nf_js("pl.pause(); return 'PAUSED';")
_JS_GET_TIME = _nf_js("return 'TIME:' + pl.getCurrentTime();")


def _inject_and_read_mac(inner_js: str) -> str:
    def esc(js: str) -> str:
        return js.replace("\\", "\\\\").replace('"', '\\"')

    script = f'''
    tell application "Google Chrome"
        set foundTab to false
        set resultVal to "NO_TAB"
        repeat with w in windows
            repeat with t in tabs of w
                try
                    set theUrl to (URL of t) as string
                    if (theUrl contains "netflix.com") or (theUrl contains "Netflix.com") then
                        set resultVal to execute t javascript "{esc(inner_js)}"
                        set foundTab to true
                        exit repeat
                    end if
                end try
            end repeat
            if foundTab then exit repeat
        end repeat
        return resultVal as string
    end tell
    '''
    result = subprocess.run(["osascript", "-e", script],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    output = result.stdout.strip()
    if output == "NO_TAB":
        raise RuntimeError("No Netflix tab found in Chrome — open Netflix first!")
    return output


def _inject_and_read_windows(inner_js: str) -> str:
    import urllib.request
    import json
    try:
        import websocket
    except ImportError:
        raise RuntimeError("Please install dependencies: pip install -r requirements.txt")

    try:
        req = urllib.request.Request("http://127.0.0.1:9222/json")
        with urllib.request.urlopen(req) as response:
            tabs = json.loads(response.read().decode())
        
        n_tab = next((t for t in tabs if 'url' in t and 'netflix.com' in t['url'].lower()), None)
        if not n_tab:
            raise RuntimeError("No Netflix tab found in Chrome — open Netflix first!")
            
        ws_url = n_tab.get('webSocketDebuggerUrl')
        if not ws_url:
            raise RuntimeError("Netflix tab found, but no WebSocket URL (Chrome not debuggable?)")
            
        ws = websocket.create_connection(ws_url)
        
        payload = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": inner_js,
                "returnByValue": True
            }
        }
        ws.send(json.dumps(payload))
        result = json.loads(ws.recv())
        ws.close()
        
        if 'result' in result and 'result' in result['result']:
            val = result['result']['result'].get('value')
            if val is not None:
                return str(val)
        return "ERROR: CDP Evaluation failed"
        
    except urllib.error.URLError:
        raise RuntimeError("Could not connect to Chrome. Did you start it with --remote-debugging-port=9222?")


def _inject_and_read(inner_js: str) -> str:
    import platform
    if platform.system() == "Windows":
        return _inject_and_read_windows(inner_js)
    else:
        return _inject_and_read_mac(inner_js)


def _netflix_play():
    try:
        r = _inject_and_read(_JS_PLAY)
        print(f"[Netflix] ▶  Play  → {r}")
    except Exception as e:
        print(f"[Netflix] Play failed: {e}")


def _netflix_pause():
    try:
        r = _inject_and_read(_JS_PAUSE)
        print(f"[Netflix] ⏸  Pause → {r}")
    except Exception as e:
        print(f"[Netflix] Pause failed: {e}")


def _netflix_get_time_ms() -> float:
    """Return current Netflix playback position in milliseconds."""
    try:
        r = _inject_and_read(_JS_GET_TIME)
        if r.startswith("TIME:"):
            return float(r[5:])
        raise RuntimeError(r)
    except Exception as e:
        print(f"[Netflix] Get-time failed: {e}")
        return -1.0


def _netflix_seek_ms(ms: int):
    """Seek Netflix to the given millisecond position."""
    try:
        js = _nf_js(f"pl.seek({int(ms)}); return 'SEEKED_TO:{int(ms)}';")
        r = _inject_and_read(js)
        print(f"[Netflix] ⏩ Seek  → {r}")
    except Exception as e:
        print(f"[Netflix] Seek failed: {e}")


# ──────────────────────────── HELPERS ───────────────────────── #
def fmt_duration(sec):
    return f"{int(sec) // 60:02d}:{int(sec) % 60:02d}"


def compute_head_confidence(yaw_off, pitch_off):
    yw = max(0.0, 1.0 - abs(yaw_off)   / YAW_THRESHOLD_DEG)
    pt = max(0.0, 1.0 - abs(pitch_off) / PITCH_THRESHOLD_DEG)
    return (yw + pt) / 2.0


# ──────────────────── PER-PERSON STATE ──────────────────────── #
class PersonState:
    """All mutable tracking state for one viewer."""

    def __init__(self, label: str):
        self.label = label                     # "P1" or "P2"

        # Calibration
        self.calib_yaw   = 0.0
        self.calib_pitch = 0.0
        self.calib_done  = False
        self.calib_yaws:   list[float] = []
        self.calib_pitches: list[float] = []

        # Per-person smoothing queues
        self.ray_origins    = deque(maxlen=FILTER_LENGTH)
        self.ray_directions = deque(maxlen=FILTER_LENGTH)

        # Current-frame derived values
        self.is_looking  = False
        self.head_ok     = False
        self.yaw_off     = 0.0
        self.pitch_off   = 0.0
        self.head_conf   = 0.0
        self.raw_yaw     = 180.0
        self.raw_pitch   = 180.0

        # Face visibility
        self.in_frame        = False
        self.absent_since: float | None = None   # wall-clock when they left

        # Session stats
        self.looking_seconds = 0.0
        self.away_seconds    = 0.0


# ───────────────────── HEAD-POSE COMPUTATION ────────────────── #
def compute_head_pose(lms, x1, y1, x2, y2, state: PersonState):
    """
    Extract yaw / pitch from the face landmark set, update the
    smoothing queues and calibration data for `state`.  Modifies
    `state` in-place; returns (avg_origin, avg_dir) for the gaze ray.
    """
    def lm_np(idx):
        lm = lms[idx]
        crop_w = x2 - x1
        crop_h = y2 - y1
        ox = x1 + lm.x * crop_w
        oy = y1 + lm.y * crop_h
        oz = lm.z * crop_w
        return np.array([ox, oy, oz])

    left_pt   = lm_np(HEAD_LANDMARKS["left"])
    right_pt  = lm_np(HEAD_LANDMARKS["right"])
    top_pt    = lm_np(HEAD_LANDMARKS["top"])
    bottom_pt = lm_np(HEAD_LANDMARKS["bottom"])
    front_pt  = lm_np(HEAD_LANDMARKS["front"])

    r_ax = right_pt - left_pt;  r_ax /= np.linalg.norm(r_ax)
    u_ax = top_pt - bottom_pt;  u_ax /= np.linalg.norm(u_ax)
    fwd  = np.cross(r_ax, u_ax); fwd /= np.linalg.norm(fwd); fwd = -fwd

    center = (left_pt + right_pt + top_pt + bottom_pt + front_pt) / 5.0
    state.ray_origins.append(center)
    state.ray_directions.append(fwd)

    avg_dir = np.mean(state.ray_directions, axis=0)
    avg_dir /= np.linalg.norm(avg_dir)
    avg_origin = np.mean(state.ray_origins, axis=0)

    # Yaw
    xz = np.array([avg_dir[0], 0.0, avg_dir[2]])
    if np.linalg.norm(xz) > 1e-6: xz /= np.linalg.norm(xz)
    yaw_rad = math.acos(np.clip(np.dot([0.0, 0.0, -1.0], xz), -1.0, 1.0))
    if avg_dir[0] < 0: yaw_rad = -yaw_rad
    yaw_deg = np.degrees(yaw_rad)
    yaw_deg = abs(yaw_deg) if yaw_deg < 0 else (360 - yaw_deg if yaw_deg < 180 else yaw_deg)
    state.raw_yaw = yaw_deg

    # Pitch
    yz = np.array([0.0, avg_dir[1], avg_dir[2]])
    if np.linalg.norm(yz) > 1e-6: yz /= np.linalg.norm(yz)
    pitch_rad = math.acos(np.clip(np.dot([0.0, 0.0, -1.0], yz), -1.0, 1.0))
    if avg_dir[1] > 0: pitch_rad = -pitch_rad
    pitch_deg = np.degrees(pitch_rad)
    pitch_deg = 360 + pitch_deg if pitch_deg < 0 else pitch_deg
    state.raw_pitch = pitch_deg

    # Calibrated offsets
    state.yaw_off   = (state.raw_yaw   + state.calib_yaw)   - 180.0
    state.pitch_off = (state.raw_pitch + state.calib_pitch)  - 180.0
    state.head_conf = max(0.0, min(1.0,
                          compute_head_confidence(state.yaw_off, state.pitch_off)))
    state.head_ok   = (state.calib_done and
                       abs(state.yaw_off)   <= YAW_THRESHOLD_DEG and
                       abs(state.pitch_off) <= PITCH_THRESHOLD_DEG)
    state.is_looking = state.head_ok

    return avg_origin, avg_dir, left_pt, right_pt


def run_calibration(state: PersonState, frame, fw, fh):
    """
    Accumulate calibration samples for `state`.  Draws a progress bar
    on `frame` while collecting.  Returns True once done.
    """
    if state.calib_done:
        return True

    state.calib_yaws.append(state.raw_yaw)
    state.calib_pitches.append(state.raw_pitch)
    n = len(state.calib_yaws)

    # Progress bar (half-width, positioned by person index)
    p_idx = 0 if state.label == "P1" else 1
    bar_x  = p_idx * (fw // 2)
    bar_w  = fw // 2
    fill_w = int(bar_w * (n / AUTO_CALIB_FRAMES))
    bar_y  = fh - 8 - (p_idx * 14)
    cv2.rectangle(frame, (bar_x, bar_y), (bar_x + fill_w, bar_y + 10),
                  (50, 200, 255) if p_idx == 0 else (255, 150, 50), -1)
    cv2.putText(frame, f"Calibrating {state.label}… {n}/{AUTO_CALIB_FRAMES}",
                (bar_x + 4, bar_y - 2),
                cv2.FONT_HERSHEY_SIMPLEX, 0.40, (220, 220, 220), 1, cv2.LINE_AA)

    if n >= AUTO_CALIB_FRAMES:
        state.calib_yaw   = 180.0 - float(np.mean(state.calib_yaws))
        state.calib_pitch = 180.0 - float(np.mean(state.calib_pitches))
        state.calib_done  = True
        print(f"[{state.label}] Auto-calibrated — "
              f"Yaw offset={state.calib_yaw:.1f}  Pitch offset={state.calib_pitch:.1f}")
        return True
    return False


# ─────────────────────────── OVERLAY ────────────────────────── #
def draw_person_panel(frame, state: PersonState, panel_x, panel_y):
    """Draw a compact info panel for one person at (panel_x, panel_y)."""
    pw, ph = 240, 195
    ov = frame.copy()
    cv2.rectangle(ov, (panel_x, panel_y),
                  (panel_x + pw, panel_y + ph), (15, 15, 25), -1)
    cv2.addWeighted(ov, 0.78, frame, 0.22, 0, frame)

    if not state.in_frame:
        border = (180, 140, 0)
    elif state.is_looking:
        border = (50, 220, 80)
    else:
        border = (50, 80, 230)
    cv2.rectangle(frame, (panel_x, panel_y),
                  (panel_x + pw, panel_y + ph), border, 2)

    fx, fy = panel_x + 10, panel_y + 20
    cv2.putText(frame, state.label, (fx, fy),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (210, 210, 210), 2, cv2.LINE_AA)
    fy += 26

    if not state.in_frame:
        absent = (time.time() - state.absent_since) if state.absent_since else 0.0
        cv2.putText(frame, "NOT IN FRAME",
                    (fx, fy), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (30, 170, 220), 1, cv2.LINE_AA)
        fy += 18
        if absent > 0:
            cv2.putText(frame, f"Gone: {fmt_duration(absent)}",
                        (fx, fy), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (30, 200, 255), 1, cv2.LINE_AA)
            fy += 16
    else:
        if not state.calib_done:
            cv2.putText(frame, "CALIBRATING…",
                        (fx, fy), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (200, 200, 50), 1, cv2.LINE_AA)
        else:
            txt = "LOOKING" if state.is_looking else "AWAY"
            col = (50, 230, 80) if state.is_looking else (50, 80, 230)
            cv2.putText(frame, txt, (fx, fy),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, col, 2, cv2.LINE_AA)
        fy += 22

        # Confidence & Bar
        bw   = pw - 20
        fill = int(bw * state.head_conf)
        bc   = (50, 200, 80) if state.head_conf > 0.6 else (50, 120, 200) if state.head_conf > 0.3 else (60, 60, 210)
        
        cv2.putText(frame, f"Confidence: {int(state.head_conf * 100)}%",
                    (fx, fy), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (180, 180, 180), 1, cv2.LINE_AA)
        fy += 12
        cv2.rectangle(frame, (fx, fy), (fx + bw, fy + 10), (40, 40, 60), -1)
        cv2.rectangle(frame, (fx, fy), (fx + fill, fy + 10), bc, -1)
        fy += 26
        
        cv2.putText(frame, f"Yaw: {state.yaw_off:+.1f}  Pitch: {state.pitch_off:+.1f}",
                    (fx, fy), cv2.FONT_HERSHEY_SIMPLEX, 0.34, (140, 180, 240), 1, cv2.LINE_AA)
        fy += 22

    # Stats
    total = max(state.looking_seconds + state.away_seconds, 1)
    pct   = int(state.looking_seconds / total * 100)
    cv2.putText(frame, f"Look {fmt_duration(state.looking_seconds)} ({pct}%) "
                       f"Away {fmt_duration(state.away_seconds)}",
                (fx, fy), cv2.FONT_HERSHEY_SIMPLEX, 0.33, (180, 170, 120), 1, cv2.LINE_AA)


def draw_status_bar(frame, netflix_paused, both_looking, seek_back_secs,
                    p1: PersonState, p2: PersonState, session_elapsed,
                    is_rewinding=False):
    """Draw the bottom status strip spanning the full frame width."""
    h, w = frame.shape[:2]
    ov = frame.copy()
    cv2.rectangle(ov, (0, h - 42), (w, h), (15, 15, 25), -1)
    cv2.addWeighted(ov, 0.82, frame, 0.18, 0, frame)

    # Netflix state
    nf_txt = "Netflix: PAUSED" if netflix_paused else "Netflix: PLAYING"
    nf_col = (80, 80, 230) if netflix_paused else (50, 230, 80)
    cv2.putText(frame, nf_txt, (12, h - 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, nf_col, 1, cv2.LINE_AA)


    # Overall attention badge
    if both_looking:
        badge, badge_col = "WATCHING", (50, 230, 80)
    else:
        badge, badge_col = "ATTENTION LOST", (50, 80, 230)
    cv2.putText(frame, badge, (w // 2 - 65, h - 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, badge_col, 1, cv2.LINE_AA)

    # Seek-back timer (right side)
    if seek_back_secs > 0:
        cv2.putText(frame, f"↩ Seek-back: {fmt_duration(seek_back_secs)}",
                    (w - 220, h - 24), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (30, 200, 255), 1, cv2.LINE_AA)

    # Session timer (far right)
    cv2.putText(frame, f"Session {fmt_duration(session_elapsed)}  [q]=quit",
                (12, h - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (120, 120, 120), 1, cv2.LINE_AA)


# ─────────────────────────────── MAIN ───────────────────────── #
def main():
    # Two person states — indices 0 (P1/left) and 1 (P2/right)
    persons = [PersonState("P1"), PersonState("P2")]

    session_start = time.time()

    # ── Netflix play/pause state ───────────────────────────────
    netflix_paused = False
    away_since     = None    # when *at least one* person first looked away
    back_since     = None    # when *both* people first looked back

    # ── Seek-back state ────────────────────────────────────────
    # We define "seek period" as any time at least one person is absent.
    # seek_start is None when nobody is absent; set to wall-clock when
    # the first person leaves.  When everyone is back we accumulate
    # the elapsed duration into seek_back_pool and seek by that amount.
    seek_start:   float | None = None
    seek_back_pool: float = 0.0   # accumulated seconds to seek back

    # REWINDING display: show the flag for a short period after seek-back fires
    REWIND_DISPLAY_SEC = 2.0
    rewinding_until: float = 0.0   # wall-clock time until which the flag shows

    last_tick = time.time()

    # ── MediaPipe model ─────────────────────────────────────────
    if not os.path.exists(MODEL_PATH):
        print(f"[ERROR] Model not found: {MODEL_PATH}")
        sys.exit(1)

    base_opts = mp_python.BaseOptions(model_asset_path=MODEL_PATH)
    opts = FaceLandmarkerOptions(
        base_options=base_opts,
        output_face_blendshapes=False,
        output_facial_transformation_matrixes=False,
        num_faces=2,                          # ← detect up to 2 faces
        min_face_detection_confidence=0.3,
        min_face_presence_confidence=0.3,
        min_tracking_confidence=0.3,
        running_mode=mp_vision.RunningMode.VIDEO,
    )
    detector = FaceLandmarker.create_from_options(opts)

    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        print(f"[ERROR] Cannot open camera {CAMERA_INDEX}.")
        sys.exit(1)

    print("=" * 60)
    print("  Netflix Attention Controller — 2-Person Edition")
    print("  P1 = leftmost face  |  P2 = rightmost face")
    print("  Both looking → Netflix plays.")
    print("  Either looks away → pauses after grace period.")
    print("  Either leaves frame → keeps playing; seeks back on return.")
    print("  Auto-calibration runs per-person (30 frames each).")
    print("  q = quit")
    print("=" * 60)

    frame_idx = 0

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        frame = cv2.flip(frame, 1)
        fh, fw = frame.shape[:2]

        # ── Digital zoom for detection ────────────────────────
        if DIGITAL_ZOOM != 1.0:
            cx, cy = fw // 2, fh // 2
            crop_w = int(fw / DIGITAL_ZOOM)
            crop_h = int(fh / DIGITAL_ZOOM)
            x1 = max(cx - crop_w // 2, 0)
            y1 = max(cy - crop_h // 2, 0)
            x2 = min(x1 + crop_w, fw)
            y2 = min(y1 + crop_h, fh)
            det_frame = cv2.resize(frame[y1:y2, x1:x2], (fw, fh),
                                   interpolation=cv2.INTER_LINEAR)
        else:
            x1 = y1 = 0
            x2, y2 = fw, fh
            det_frame = frame

        mp_img = mp.Image(image_format=mp.ImageFormat.SRGB,
                          data=cv2.cvtColor(det_frame, cv2.COLOR_BGR2RGB))
        ts_ms  = int(cap.get(cv2.CAP_PROP_POS_MSEC)) or (frame_idx * 33)
        result = detector.detect_for_video(mp_img, ts_ms)
        frame_idx += 1

        now       = time.time()
        dt        = now - last_tick
        last_tick = now
        session_elapsed = now - session_start

        # ── Sort detected faces left-to-right by nose X coord ──
        # (landmark 1 = nose bridge — same as HEAD_LANDMARKS["front"])
        detected_faces = result.face_landmarks  # list of landmark lists
        if len(detected_faces) > 1:
            detected_faces = sorted(detected_faces,
                                    key=lambda lms: lms[HEAD_LANDMARKS["front"]].x)

        n_faces = len(detected_faces)

        # ── Update each person's in-frame status ──────────────
        for i, ps in enumerate(persons):
            was_in_frame = ps.in_frame
            ps.in_frame  = (i < n_faces)

            if ps.in_frame and not was_in_frame:
                # Person just came back into frame
                if ps.absent_since is not None:
                    absent_dur = now - ps.absent_since
                    print(f"[{ps.label}] Back in frame after {absent_dur:.1f}s.")
                ps.absent_since = None

            elif not ps.in_frame and was_in_frame:
                # Person just left frame
                ps.absent_since = now
                print(f"[{ps.label}] Left frame — timer started.")
                ps.is_looking = False
                # Clear smoothing queues so stale data doesn't linger
                ps.ray_origins.clear()
                ps.ray_directions.clear()

        # ── Head pose per visible person ───────────────────────
        for i, ps in enumerate(persons):
            if not ps.in_frame:
                ps.is_looking = False
                ps.head_ok    = False
                ps.head_conf  = 0.0
                continue

            lms = detected_faces[i]

            avg_origin, avg_dir, left_pt, right_pt = compute_head_pose(
                lms, x1, y1, x2, y2, ps
            )

            # Calibration
            run_calibration(ps, frame, fw, fh)

            # Gaze ray drawn on frame
            half_w  = np.linalg.norm(np.array([right_pt[0] - left_pt[0],
                                                right_pt[1] - left_pt[1]])) / 2
            ray_end = avg_origin - avg_dir * (2.5 * half_w)
            ray_col = (50, 230, 80) if ps.head_ok else (50, 80, 230)
            cv2.line(frame,
                     (int(avg_origin[0]), int(avg_origin[1])),
                     (int(ray_end[0]),    int(ray_end[1])),
                     ray_col, 3)
            # Landmark dots
            for lm in lms:
                dot_x = int(x1 + lm.x * (x2 - x1))
                dot_y = int(y1 + lm.y * (y2 - y1))
                cv2.circle(frame, (dot_x, dot_y), 2, (50, 130, 50), -1)

            # ── Pupil Visualization (Technical Overlay) ──
            for iris_idx in [468, 473]: # Left and Right iris centers
                ilm = lms[iris_idx]
                ix, iy = int(x1 + ilm.x * (x2 - x1)), int(y1 + ilm.y * (y2 - y1))
                cv2.circle(frame, (ix, iy), 3, (255, 255, 255), -1) # White core
                cv2.circle(frame, (ix, iy), 6, (0, 255, 255), 1)   # Tech ring

        # ── Time tracking per person ───────────────────────────
        for ps in persons:
            if ps.is_looking:
                ps.looking_seconds += dt
            elif ps.in_frame:  # in frame but not looking
                ps.away_seconds += dt

        # ── Seek-back timer ────────────────────────────────────────
        # Runs whenever at least one person is absent from frame.
        # Netflix is left in whatever state play/pause logic sets —
        # we do NOT forcibly resume here, so the remaining person's
        # look-away state keeps controlling playback while one is gone.
        anyone_absent = any(not ps.in_frame for ps in persons)

        if anyone_absent:
            if seek_start is None:
                seek_start = now   # first person just left

        else:
            # All persons back in frame
            if seek_start is not None:
                elapsed = now - seek_start
                seek_back_pool += elapsed
                seek_start = None
                print(f"[Seek-Back] All back — adding {elapsed:.1f}s "
                      f"(pool={seek_back_pool:.1f}s)")

                if seek_back_pool >= MIN_ABSENT_FOR_SEEK_SEC:
                    print(f"[Seek-Back] Seeking back {seek_back_pool:.1f}s…")
                    current_ms = _netflix_get_time_ms()
                    if current_ms >= 0:
                        target_ms = max(0, current_ms - seek_back_pool * 1000)
                        _netflix_seek_ms(int(target_ms))
                    rewinding_until = now + REWIND_DISPLAY_SEC
                    seek_back_pool = 0.0
                else:
                    print(f"[Seek-Back] Absence too short ({seek_back_pool:.1f}s) — skipping.")
                    seek_back_pool = 0.0

                # Reset look-away timers — context changed
                away_since = back_since = None

        # ── Play / Pause logic ──────────────────────────────────────
        # Only consider people who are IN FRAME and CALIBRATED.
        # An absent person is ignored here — the remaining person's
        # attention still controls playback while their partner is away.
        present_calibrated = [ps for ps in persons
                              if ps.in_frame and ps.calib_done]

        if present_calibrated:
            all_present_looking = all(ps.is_looking for ps in present_calibrated)
            if not all_present_looking:
                # At least one present person is looking away
                back_since = None
                if away_since is None:
                    away_since = now
                elif (now - away_since) >= AWAY_GRACE_SEC and not netflix_paused:
                    _netflix_pause()
                    netflix_paused = True
            else:
                # All present persons are looking at screen
                away_since = None
                if back_since is None:
                    back_since = now
                elif (now - back_since) >= BACK_GRACE_SEC and netflix_paused:
                    _netflix_play()
                    netflix_paused = False

        # ── Greyscale effect while rewinding ─────────────────────
        is_rewinding = (now < rewinding_until)
        if is_rewinding:
            grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            frame = cv2.cvtColor(grey, cv2.COLOR_GRAY2BGR)

        # ── Overlay panels ─────────────────────────────────────
        # P1 panel — top-right; P2 panel — top-right below P1
        draw_person_panel(frame, persons[0], fw - 255, 10)
        draw_person_panel(frame, persons[1], fw - 255, 215)

        seek_secs = (now - seek_start) + seek_back_pool if seek_start else seek_back_pool
        # For the badge: "all present" looking means nobody in-frame is away
        present_looking = (bool(present_calibrated) and
                           all(ps.is_looking for ps in present_calibrated))
        draw_status_bar(frame, netflix_paused, present_looking,
                        seek_secs, persons[0], persons[1], session_elapsed,
                        is_rewinding=is_rewinding)

        # ── Centered REWINDING label ───────────────────────────
        if is_rewinding:
            rw_text = "REWINDING"
            rw_scale = 1.8
            rw_thick = 4
            (tw, th), _ = cv2.getTextSize(rw_text, cv2.FONT_HERSHEY_SIMPLEX,
                                          rw_scale, rw_thick)
            rx = (fw - tw) // 2
            ry = (fh + th) // 2
            # Dark backdrop for readability
            cv2.rectangle(frame, (rx - 16, ry - th - 12),
                          (rx + tw + 16, ry + 12), (0, 0, 0), -1)
            cv2.putText(frame, rw_text, (rx, ry),
                        cv2.FONT_HERSHEY_SIMPLEX, rw_scale,
                        (0, 0, 255), rw_thick, cv2.LINE_AA)

        cv2.imshow("Netflix Attention — 2 Person", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break

    detector.close()
    cap.release()
    cv2.destroyAllWindows()

    # ── Session summary ────────────────────────────────────────
    print("\n── Session Summary ──────────────────────────────────────")
    for ps in persons:
        total = ps.looking_seconds + ps.away_seconds
        pct   = int(ps.looking_seconds / total * 100) if total > 0 else 0
        print(f"  {ps.label}  Total {fmt_duration(total)} | "
              f"Looking {fmt_duration(ps.looking_seconds)} ({pct}%) | "
              f"Away {fmt_duration(ps.away_seconds)} ({100 - pct}%)")
    print("─────────────────────────────────────────────────────────\n")


if __name__ == "__main__":
    main()
