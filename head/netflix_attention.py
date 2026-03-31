"""
Netflix Attention Controller
============================
Two separate behaviours based on what the camera sees:

  HEAD TURNED AWAY (face visible, head not pointing at screen)
    → Netflix PAUSES after AWAY_GRACE_SEC seconds.
    → Netflix RESUMES when you look back.

  FACE ABSENT (you leave the camera frame entirely)
    → Netflix KEEPS PLAYING — you might have gone to get water, etc.
    → A timer records how long your face was gone.
    → When your face returns the video SEEKS BACK by that duration,
      so you don't miss anything.

Controls:
  q  - Quit
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
FILTER_LENGTH       = 10
YAW_THRESHOLD_DEG   = 25
PITCH_THRESHOLD_DEG = 20
AUTO_CALIB_FRAMES   = 30

# Digital zoom factor for MediaPipe detection.
# Crops the CENTER (1/DIGITAL_ZOOM) portion of the frame and stretches it
# to full frame size before detection — the face appears proportionally
# larger, solving the minimum-face-size requirement for distant users.
# Display window always shows the original unzoomed frame.
# 1.0 = no zoom | 1.5 = mild | 2.0 = good for ~2m | 3.0 = very far
DIGITAL_ZOOM        = 2.0

# Grace period: user must be away/back for this long before we act.
# This prevents rapid flickering from unstable detections.
AWAY_GRACE_SEC      = 1.5
BACK_GRACE_SEC      = 0.8

# A face absence shorter than this is ignored for seek-back purposes
# (e.g., a blink / brief detection dropout).
MIN_ABSENT_FOR_SEEK_SEC = 2.0

MODEL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "face_landmarker.task"
)

# REWINDING display: show the flag for a short period after seek-back fires
REWIND_DISPLAY_SEC = 2.0

# ── Head pose landmark indices ──────────────────────────────── #
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


def draw_overlay(frame, is_looking, head_ok,
                 yaw_off, pitch_off, head_conf,
                 session_elapsed, looking_s, away_s,
                 face_visible, calib_done):
    """Draw a compact info panel for the viewer."""
    h, w = frame.shape[:2]
    pw, ph = 240, 195
    px, py = w - pw - 15, 15
    ov = frame.copy()
    cv2.rectangle(ov, (px, py), (px + pw, py + ph), (15, 15, 25), -1)
    cv2.addWeighted(ov, 0.78, frame, 0.22, 0, frame)

    if not face_visible:
        border = (180, 140, 0)
    elif is_looking:
        border = (50, 220, 80)
    else:
        border = (50, 80, 230)
    cv2.rectangle(frame, (px, py), (px + pw, py + ph), border, 2)

    fx, fy = px + 15, py + 25
    cv2.putText(frame, "VIEWER", (fx, fy),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (210, 210, 210), 2, cv2.LINE_AA)
    fy += 26

    if not face_visible:
        cv2.putText(frame, "NOT IN FRAME",
                    (fx, fy), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (30, 170, 220), 1, cv2.LINE_AA)
        fy += 40
    else:
        if not calib_done:
            cv2.putText(frame, "CALIBRATING…",
                        (fx, fy), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (200, 200, 50), 1, cv2.LINE_AA)
        else:
            txt = "LOOKING" if is_looking else "AWAY"
            col = (50, 230, 80) if is_looking else (50, 80, 230)
            cv2.putText(frame, txt, (fx, fy),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, col, 2, cv2.LINE_AA)
        fy += 22

        # Confidence & Bar
        bw   = pw - 30
        fill = int(bw * head_conf)
        bc   = (50, 200, 80) if head_conf > 0.6 else (50, 120, 200) if head_conf > 0.3 else (60, 60, 210)
        
        cv2.putText(frame, f"Confidence: {int(head_conf * 100)}%",
                    (fx, fy), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (180, 180, 180), 1, cv2.LINE_AA)
        fy += 12
        cv2.rectangle(frame, (fx, fy), (fx + bw, fy + 10), (40, 40, 60), -1)
        cv2.rectangle(frame, (fx, fy), (fx + fill, fy + 10), bc, -1)
        fy += 26
        
        cv2.putText(frame, f"Yaw: {yaw_off:+.1f}  Pitch: {pitch_off:+.1f}",
                    (fx, fy), cv2.FONT_HERSHEY_SIMPLEX, 0.34, (140, 180, 240), 1, cv2.LINE_AA)
        fy += 22

    # Stats
    total = max(looking_s + away_s, 1)
    pct   = int(looking_s / total * 100)
    cv2.putText(frame, f"Look {fmt_duration(looking_s)} ({pct}%) "
                       f"Away {fmt_duration(away_s)}",
                (fx, fy), cv2.FONT_HERSHEY_SIMPLEX, 0.33, (180, 170, 120), 1, cv2.LINE_AA)


def draw_status_bar(frame, netflix_paused, is_looking, seek_back_secs, session_elapsed):
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
    if is_looking:
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
    calib_yaw = calib_pitch = 0.0
    ray_origins    = deque(maxlen=FILTER_LENGTH)
    ray_directions = deque(maxlen=FILTER_LENGTH)

    auto_calib_done    = False
    auto_calib_yaws    = []
    auto_calib_pitches = []

    session_start   = time.time()
    looking_seconds = 0.0
    away_seconds    = 0.0
    last_tick       = time.time()

    # ── "looking away" pause state ─────────────────────────────
    netflix_paused    = False
    away_since        = None   # time when head first turned away (face still visible)
    back_since        = None   # time when head first came back to screen

    # ── "face absent" seek-back state ──────────────────────────
    face_visible      = True   # is face currently in the camera frame?
    face_absent_since = None   # wall-clock time when face first disappeared
    rewinding_until   = 0.0    # wall-clock time until which the rewinding UI shows

    # ── Model setup ─────────────────────────────────────────────
    if not os.path.exists(MODEL_PATH):
        print(f"[ERROR] Model not found: {MODEL_PATH}")
        print("  Expecting it at:", MODEL_PATH)
        sys.exit(1)

    base_opts = mp_python.BaseOptions(model_asset_path=MODEL_PATH)
    opts = FaceLandmarkerOptions(
        base_options=base_opts,
        output_face_blendshapes=False,
        output_facial_transformation_matrixes=False,
        num_faces=1,
        # Lowered thresholds help detect smaller/distant faces that score
        # lower confidence. Pair with DETECTION_UPSCALE for best results.
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

    print("=" * 55)
    print("  Netflix Attention Controller")
    print("  Auto-calibrating on first face detection…")
    print("  Make sure Chrome has Netflix open with a video playing.")
    print("  LOOK AWAY  → pauses | LEAVE FRAME → seeks back on return")
    print("  q = quit")
    print("=" * 55)

    frame_idx = 0
    raw_yaw = raw_pitch = 180.0

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        frame = cv2.flip(frame, 1)
        fh, fw = frame.shape[:2]

        # Digital zoom: crop the centre region and resize to full frame.
        # This makes the face appear proportionally larger to MediaPipe
        # (unlike a full-frame upscale which keeps face/frame ratio the same).
        if DIGITAL_ZOOM != 1.0:
            cx, cy   = fw // 2, fh // 2
            crop_w   = int(fw / DIGITAL_ZOOM)
            crop_h   = int(fh / DIGITAL_ZOOM)
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

        is_looking = head_ok = False
        yaw_off = pitch_off = 0.0
        head_conf = 0.0

        # ── Face detected this frame? ─────────────────────────
        face_in_frame = bool(result.face_landmarks)

        # ── Transition: face came BACK after being absent ─────
        if face_in_frame and not face_visible:
            face_visible = True
            if face_absent_since is not None:
                absent_duration = now - face_absent_since
                if absent_duration >= MIN_ABSENT_FOR_SEEK_SEC:
                    print(f"[Seek-Back] Face was absent for {absent_duration:.1f}s — seeking back…")
                    current_ms = _netflix_get_time_ms()
                    if current_ms >= 0:
                        seek_target = max(0, current_ms - absent_duration * 1000)
                        _netflix_seek_ms(int(seek_target))
                        rewinding_until = now + REWIND_DISPLAY_SEC
                else:
                    print(f"[Seek-Back] Face absence ({absent_duration:.1f}s) too short — skipping seek.")
                face_absent_since = None
            # Reset look-away timers since face context changed
            away_since = back_since = None

        # ── Transition: face DISAPPEARED ─────────────────────
        elif not face_in_frame and face_visible:
            face_visible = False
            face_absent_since = now
            print(f"[Absent] Face left frame — timer started, Netflix keeps playing.")
            # If Netflix was paused due to looking away, resume it so
            # the "absent timer" concept works correctly (video plays while you're gone).
            if netflix_paused:
                _netflix_play()
                netflix_paused = False
            away_since = back_since = None

        # ── Head pose (only when face is in frame) ────────────
        if face_in_frame:
            lms = result.face_landmarks[0]

            def lm_np(idx):
                lm = lms[idx]
                # Landmarks are normalised to the cropped+resized det_frame.
                # Map back to original frame pixel coords using the crop box.
                crop_w = x2 - x1
                crop_h = y2 - y1
                ox = x1 + lm.x * crop_w
                oy = y1 + lm.y * crop_h
                oz = lm.z * crop_w   # z is a depth value, scale by width
                return np.array([ox, oy, oz])

            left   = lm_np(HEAD_LANDMARKS["left"])
            right  = lm_np(HEAD_LANDMARKS["right"])
            top    = lm_np(HEAD_LANDMARKS["top"])
            bottom = lm_np(HEAD_LANDMARKS["bottom"])
            front  = lm_np(HEAD_LANDMARKS["front"])

            r_ax = right - left;  r_ax /= np.linalg.norm(r_ax)
            u_ax = top - bottom;  u_ax /= np.linalg.norm(u_ax)
            fwd  = np.cross(r_ax, u_ax); fwd /= np.linalg.norm(fwd); fwd = -fwd

            center = (left + right + top + bottom + front) / 5.0
            ray_origins.append(center)
            ray_directions.append(fwd)

            avg_dir = np.mean(ray_directions, axis=0)
            avg_dir /= np.linalg.norm(avg_dir)
            avg_origin = np.mean(ray_origins, axis=0)

            xz = np.array([avg_dir[0], 0.0, avg_dir[2]])
            if np.linalg.norm(xz) > 1e-6: xz /= np.linalg.norm(xz)
            yaw_rad = math.acos(np.clip(np.dot([0.0, 0.0, -1.0], xz), -1.0, 1.0))
            if avg_dir[0] < 0: yaw_rad = -yaw_rad
            yaw_deg = np.degrees(yaw_rad)
            yaw_deg = abs(yaw_deg) if yaw_deg < 0 else (360 - yaw_deg if yaw_deg < 180 else yaw_deg)
            raw_yaw = yaw_deg

            yz = np.array([0.0, avg_dir[1], avg_dir[2]])
            if np.linalg.norm(yz) > 1e-6: yz /= np.linalg.norm(yz)
            pitch_rad = math.acos(np.clip(np.dot([0.0, 0.0, -1.0], yz), -1.0, 1.0))
            if avg_dir[1] > 0: pitch_rad = -pitch_rad
            pitch_deg = np.degrees(pitch_rad)
            pitch_deg = 360 + pitch_deg if pitch_deg < 0 else pitch_deg
            raw_pitch = pitch_deg

            # ── Auto-calibration ─────────────────────────────
            if not auto_calib_done:
                auto_calib_yaws.append(raw_yaw)
                auto_calib_pitches.append(raw_pitch)
                n = len(auto_calib_yaws)
                bw_bar = int(fw * (n / AUTO_CALIB_FRAMES))
                cv2.rectangle(frame, (0, fh - 8), (bw_bar, fh), (50, 200, 255), -1)
                cv2.putText(frame, f"Auto-calibrating… {n}/{AUTO_CALIB_FRAMES}",
                            (10, fh - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (50, 200, 255), 1, cv2.LINE_AA)
                if n >= AUTO_CALIB_FRAMES:
                    calib_yaw   = 180.0 - float(np.mean(auto_calib_yaws))
                    calib_pitch = 180.0 - float(np.mean(auto_calib_pitches))
                    auto_calib_done = True
                    print(f"[Auto-Calibrated] Yaw offset={calib_yaw:.1f}  Pitch offset={calib_pitch:.1f}")

            yaw_off   = (raw_yaw   + calib_yaw)   - 180.0
            pitch_off = (raw_pitch + calib_pitch)  - 180.0
            head_conf = max(0.0, min(1.0, compute_head_confidence(yaw_off, pitch_off)))
            head_ok   = (auto_calib_done and
                         abs(yaw_off)   <= YAW_THRESHOLD_DEG and
                         abs(pitch_off) <= PITCH_THRESHOLD_DEG)
            is_looking = head_ok

            # Draw gaze ray
            half_w  = np.linalg.norm(right - left) / 2
            ray_end = avg_origin - avg_dir * (2.5 * half_w)
            ray_col = (50, 230, 80) if head_ok else (50, 80, 230)
            cv2.line(frame,
                     (int(avg_origin[0]), int(avg_origin[1])),
                     (int(ray_end[0]),    int(ray_end[1])),
                     ray_col, 3)
            for lm in lms:
                # Same crop-box mapping as lm_np() so dots sit correctly
                # on the face in the original (non-zoomed) display frame.
                dot_x = int(x1 + lm.x * (x2 - x1))
                dot_y = int(y1 + lm.y * (y2 - y1))
                cv2.circle(frame, (dot_x, dot_y), 2, (50, 130, 50), -1)

            # ── Pupil Visualization (Technical Overlay) ──
            for iris_idx in [468, 473]: # Left and Right iris centers
                ilm = lms[iris_idx]
                ix, iy = int(x1 + ilm.x * (x2 - x1)), int(y1 + ilm.y * (y2 - y1))
                cv2.circle(frame, (ix, iy), 3, (255, 255, 255), -1) # White core
                cv2.circle(frame, (ix, iy), 6, (0, 255, 255), 1)   # Tech ring

        # ── Time tracking ──────────────────────────────────────
        if is_looking:
            looking_seconds += dt
        else:
            away_seconds += dt

        # ── Netflix pause / play (only when face IS in frame) ──
        # Face absent → we don't pause; the seek-back logic handles that case.
        if auto_calib_done and face_visible:
            if not is_looking:
                # User is looking away (head turned) — start away timer
                back_since = None
                if away_since is None:
                    away_since = now
                elif (now - away_since) >= AWAY_GRACE_SEC and not netflix_paused:
                    _netflix_pause()
                    netflix_paused = True
            else:
                # User is looking at screen — start back timer
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

        # ── Overlay ────────────────────────────────────────────
        absent_secs = (now - face_absent_since) if face_absent_since else 0.0
        draw_overlay(frame, is_looking, head_ok,
                     yaw_off, pitch_off, head_conf,
                     session_elapsed, looking_seconds, away_seconds,
                     face_visible, auto_calib_done)

        draw_status_bar(frame, netflix_paused, is_looking, absent_secs, session_elapsed)

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

        cv2.imshow("Netflix Attention Controller", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break

    detector.close()
    cap.release()
    cv2.destroyAllWindows()

    total = looking_seconds + away_seconds
    pct   = int(looking_seconds / total * 100) if total > 0 else 0
    print("\n── Session Summary ──────────────────────────")
    print(f"  Total   : {fmt_duration(total)}")
    print(f"  Looking : {fmt_duration(looking_seconds)}  ({pct}%)")
    print(f"  Away    : {fmt_duration(away_seconds)}  ({100 - pct}%)")
    print("─────────────────────────────────────────────\n")


if __name__ == "__main__":
    main()