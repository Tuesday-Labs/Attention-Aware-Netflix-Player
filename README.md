

## 📝 Project Overview

The **Netflix Attention Controller** is a productivity and accessibility tool designed to make streaming more seamless. By leveraging Google's MediaPipe Face Landmarker, it monitors your attention levels in real-time. 

![macOS](https://img.shields.io/badge/os-macOS-brightgreen?logo=apple) ![Windows](https://img.shields.io/badge/os-Windows-blue?logo=windows)

Unlike simple "face detection" tools, this project computes precise **3D head pose (Yaw and Pitch)** to determine if you are actually looking at the screen. It also features a "Seek-Back" mechanism: if you leave the camera's view (e.g., to grab a snack), the script tracks your absence and automatically rewinds the video when you return, ensuring you never miss a beat.

---

## ✨ Features

| Feature | Description |
|---|---|
| **Head-Pose Tracking** | Uses MediaPipe Face Landmarker (478-point mesh) to compute yaw & pitch of your head in real time |
| **Auto Pause / Resume** | Netflix pauses when you look away for >1.5 s, resumes when you look back |
| **Seek-Back on Return** | Leave the room? Netflix keeps playing, but rewinds by exactly how long you were gone when you come back |
| **2-Person Mode** | Supports two viewers — Netflix pauses if *either* person looks away, resumes only when *both* are watching |
| **Auto Calibration** | Learns your neutral head position in the first 30 frames — no manual setup needed |
| **Digital Zoom** | Crops and upscales the centre of the camera frame so MediaPipe can detect faces even at ~2 m distance |
| **CLI Playback Control** | Standalone script to play, pause, toggle, seek, and query Netflix from the terminal |
| **Live Overlay** | Real-time HUD showing gaze ray, confidence bar, attention stats, and Netflix state |

---

## 📁 Project Structure

```
.
├── README.md
├── LICENSE
├── requirements.txt
├── .gitignore
├── netflix_seek_test.py          # CLI: play / pause / seek / toggle Netflix
└── head/
    ├── face_landmarker.task      # MediaPipe model weights (~3.6 MB)
    ├── netflix_attention.py      # Single-viewer attention controller
    └── netflix_attention_2p.py   # Two-viewer attention controller
```

---

## 🖥️ Requirements

- **macOS** (uses AppleScript) or **Windows** (uses Chrome DevTools Protocol)
- **Google Chrome** with a Netflix tab open and a video loaded
- **Python 3.10+**
- A webcam (built-in or external)

---

## 🚀 Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/Tuesday-Labs/Attention-Aware-Netflix-Player.git
cd Attention-Aware-Netflix-Player
```

### 2. Create a virtual environment (recommended)

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. System-Specific Setup

**For macOS Users:**
The first time you run the app, macOS will ask for:
- **Camera access** — for the webcam feed
- **Accessibility / Automation** — so the script can send JavaScript to Chrome via AppleScript

Go to **System Settings → Privacy & Security** and allow both.

**For Windows Users:**
The script requires access to Chrome's debugging mechanics to inject playback behavior. You **must** launch Chrome from the command line with remote debugging enabled:
1. Completely close all running instances of Chrome.
2. Open Command Prompt or PowerShell and launch Chrome:
   ```cmd
   start chrome.exe --remote-debugging-port=9222
   ```
3. Open Netflix in that new Chrome window and start your video.

---

## 🎮 Usage

### Attention Controller — Single Viewer

```bash
python head/netflix_attention.py
```

- Look at the screen → Netflix plays
- Turn your head away → Netflix pauses after 1.5 s
- Leave the camera frame → Netflix keeps playing; seeks back when you return
- Press **`q`** to quit

### Attention Controller — Two Viewers

```bash
python head/netflix_attention_2p.py
```

- Both face the screen → Netflix plays
- Either person looks away → Netflix pauses
- Either person leaves the frame → seeks back on return
- **P1** = leftmost face, **P2** = rightmost face (auto-assigned each frame)

### CLI Playback Control

```bash
# Play / Pause / Toggle
python netflix_seek_test.py --play
python netflix_seek_test.py --pause
python netflix_seek_test.py --toggle

# Get current position
python netflix_seek_test.py --get-time

# Seek to a specific time
python netflix_seek_test.py --minutes 18 --seconds 30
python netflix_seek_test.py --time 1091243   # milliseconds

# Check if the Netflix player API is reachable
python netflix_seek_test.py --check
```

---

## ⚙️ Configuration

Key constants live at the top of each attention script and can be tuned to your setup:

| Constant | Default | Description |
|---|---|---|
| `CAMERA_INDEX` | `0` | OpenCV camera index |
| `YAW_THRESHOLD_DEG` | `25` | How far you can turn left/right before "away" triggers |
| `PITCH_THRESHOLD_DEG` | `20` | How far you can tilt up/down before "away" triggers |
| `DIGITAL_ZOOM` | `2.0` | Centre-crop zoom factor (increase if you sit far away) |
| `AWAY_GRACE_SEC` | `1.5` | Seconds of looking away before Netflix pauses |
| `BACK_GRACE_SEC` | `0.8` | Seconds of looking back before Netflix resumes |
| `MIN_ABSENT_FOR_SEEK_SEC` | `2.0` | Minimum absence to trigger a seek-back |
| `AUTO_CALIB_FRAMES` | `30` | Frames used for auto-calibration |
| `FILTER_LENGTH` | `10` | Smoothing window for head-pose estimation |

---

## 🏗️ How It Works

### Netflix Control

Because Chrome's isolated-world sandbox prevents direct access to the `netflix` global, the scripts seamlessly inject JavaScript into your browser:
- **macOS:** AppleScript drives Google Chrome's `execute javascript` command to directly evaluate code.
- **Windows:** Communicates locally via WebSockets using the Chrome DevTools Protocol (`--remote-debugging-port=9222`).
- **Inject a `<script>` tag** into the page's main world (used by `netflix_seek_test.py`), or
- **Fall back to the HTML5 `<video>` element** when the Netflix API isn't available (used by the attention scripts)

This approach requires **no browser extensions** — just Chrome and a Netflix tab.

### Head-Pose Estimation

1. **Digital zoom** — the centre of the camera frame is cropped and upscaled so MediaPipe can reliably detect faces at distance.
2. **478-point face mesh** — MediaPipe's Face Landmarker returns 3D landmarks.
3. **Yaw & Pitch** — computed from cross-product of ear-to-ear and top-to-bottom vectors, smoothed over a rolling window.
4. **Auto-calibration** — the first 30 frames establish a neutral baseline; subsequent angles are measured relative to it.

### Behaviour Logic

```
Face visible + head oriented at screen  →  WATCHING   →  Netflix plays
Face visible + head turned away         →  AWAY       →  Netflix pauses (after grace)
Face absent (left the room)             →  ABSENT     →  Netflix keeps playing
Face returns after absence              →  SEEK-BACK  →  Netflix rewinds by absence duration
```

---

## 🔧 Troubleshooting

| Problem | Solution |
|---|---|
| `No Netflix tab found in Chrome` | Open Chrome, navigate to Netflix, and start playing a video |
| `AppleScript error` | Grant Automation permission: System Settings → Privacy & Security → Automation → allow your terminal |
| `Could not connect to Chrome (Windows)` | Ensure all Chrome windows were closed before launching with the `--remote-debugging-port` flag |
| `ConnectionRefusedError` | Check if another app is using port 9222 or if your firewall is blocking local websocket connections |
| Face not detected | Increase `DIGITAL_ZOOM`, improve lighting, or sit closer to the camera |
| Rapid pause/resume flickering | Increase `AWAY_GRACE_SEC` or `BACK_GRACE_SEC` |
| `Model not found` | Make sure `face_landmarker.task` is in the `head/` directory |

---

## 📜 Credits & Acknowledgements

- **Netflix JS API** — sourced from [this Stack Overflow answer](https://stackoverflow.com/a/61988153) by Zarbi4734, licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)
- **MediaPipe Face Landmarker** — [Google MediaPipe](https://developers.google.com/mediapipe)
- **OpenCV** — [opencv.org](https://opencv.org/)

---

## 📄 License

This project is licensed under the MIT License — see [LICENSE](LICENSE) for details.
