"""
Netflix Seek Test
=================
Controls Netflix playback (seek, play, pause, toggle) by injecting
JavaScript into the Netflix Chrome tab via AppleScript.

Source for the Netflix JS API:
  https://stackoverflow.com/a/61988153
  Posted by Zarbi4734
  Retrieved 2026-03-11, License - CC BY-SA 4.0

Usage:
  python netflix_seek_test.py --play
  python netflix_seek_test.py --pause
  python netflix_seek_test.py --toggle
  python netflix_seek_test.py --get-time
  python netflix_seek_test.py --minutes 18 --seconds 30
  python netflix_seek_test.py --time 1091243
  python netflix_seek_test.py --check

No extra pip packages needed — stdlib only.
Chrome must have a Netflix tab open with a video loaded.
"""

import subprocess
import argparse
import sys
import time


# ──────────────────────────────────────────────────────────────── #
#  WHY script-tag injection?
#
#  AppleScript's "execute tab X javascript" runs in Chrome's ISOLATED
#  WORLD (like a content-script) — it can see the DOM but NOT page
#  globals like `netflix`.  Injecting a <script> tag always runs in
#  the page's MAIN WORLD, where `netflix` IS defined.  The result is
#  written to a body data-attribute, which the isolated world reads back.
#
#  OPTIMISATION — 1 subprocess call per operation (was 3):
#  A single AppleScript block finds the Netflix tab, injects the
#  <script>, AND reads back the result attribute, all in one shot.
# ──────────────────────────────────────────────────────────────── #

_ATTR = "data-nf-result"    # body attribute used to ferry results out


# ── Inner JS builder ─────────────────────────────────────────── #

def _nf_js(action: str) -> str:
    """
    Build a self-contained JS snippet that:
      - Gets the Netflix player (needs main-world access).
      - Runs `action`, which may call _set(value) to return a result,
        or reference `pl` (the player object) directly.
    Written to be injected via a <script> tag.
    """
    return (
        f"var _set=function(v){{document.body.setAttribute('{_ATTR}',v);}};"
        f"try{{"
        f"  var vp=netflix.appContext.state.playerApp.getAPI().videoPlayer;"
        f"  var pl=vp.getVideoPlayerBySessionId(vp.getAllPlayerSessionIds()[0]);"
        f"  {action}"
        f"}}catch(e){{_set('ERROR:'+e.message);}}"
    )


# ── Pre-built inner scripts ───────────────────────────────────── #
# Each is a one-liner thanks to _nf_js().

_JS_CHECK  = _nf_js("_set('OK:'+pl.getCurrentTime());")
_JS_TIME   = _nf_js("_set('TIME:'+pl.getCurrentTime());")
_JS_PLAY   = _nf_js("pl.play();  _set('PLAYING');")
_JS_PAUSE  = _nf_js("pl.pause(); _set('PAUSED');")
_JS_TOGGLE = _nf_js(
    "if(pl.isPaused()){pl.play(); _set('TOGGLED:PLAYING');}"
    "else{pl.pause(); _set('TOGGLED:PAUSED');}"
)

def _js_seek(ms: int) -> str:
    return _nf_js(f"pl.seek({ms}); _set('SEEKED_TO:{ms}');")


# ──────────────────────────────────────────────────────────────── #
#  Core executor — single AppleScript call per operation
#  Finds the Netflix tab + injects <script> + reads result = 1 call.
# ──────────────────────────────────────────────────────────────── #

def _inject_and_read_mac(inner_js: str) -> str:
    """
    Inject inner_js into the Netflix tab's main world via a <script>
    tag, then return the value it wrote to document.body[_ATTR].
    All three steps (find tab / inject / read) happen in one osascript
    subprocess call.
    """
    def esc(js: str) -> str:
        """Escape JS for embedding inside an AppleScript double-quoted string."""
        return js.replace("\\", "\\\\").replace('"', '\\"')

    # Step A — inject:  wraps inner_js in a self-removing <script> tag
    inject_js = (
        "(function(){"
        "var s=document.createElement('script');"
        "s.textContent=" + repr(inner_js) + ";"
        "document.body.appendChild(s);s.remove();"
        "})();"
    )
    # Step B — read:    pull the result attribute back (DOM is shared)
    read_js = f"document.body.getAttribute('{_ATTR}');"

    script = f'''
    tell application "Google Chrome"
        repeat with w in windows
            repeat with t in tabs of w
                if URL of t contains "netflix.com" then
                    execute t javascript "{esc(inject_js)}"
                    set r to execute t javascript "{esc(read_js)}"
                    return r as string
                end if
            end repeat
        end repeat
    end tell
    return "NO_TAB"
    '''
    result = subprocess.run(["osascript", "-e", script],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    output = result.stdout.strip()
    if output == "NO_TAB":
        raise RuntimeError(
            "No Netflix tab found in Chrome — open Netflix first!")
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
        
        # Inject JavaScript using Chrome DevTools Protocol
        inject_js = (
            "(function(){"
            "var s=document.createElement('script');"
            "s.textContent=" + json.dumps(inner_js) + ";"
            "document.body.appendChild(s);s.remove();"
            f"return document.body.getAttribute('{_ATTR}');"
            "})();"
        )
        
        payload = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": inject_js,
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


def inject_and_read(inner_js: str) -> str:
    import platform
    if platform.system() == "Windows":
        return _inject_and_read_windows(inner_js)
    else:
        return _inject_and_read_mac(inner_js)


# ──────────────────────────────────────────────────────────────── #
#  High-level player actions
# ──────────────────────────────────────────────────────────────── #

def _ms_to_mmss(ms_str: str) -> str:
    sec = int(float(ms_str)) // 1000
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"


def check_player() -> bool:
    """Verify the Netflix player API is reachable. Returns True if OK."""
    print("[*] Checking Netflix player API…")
    r = inject_and_read(_JS_CHECK)
    if r.startswith("OK:"):
        ms = r[3:]
        print(f"    [OK]  Player found!  Position: {_ms_to_mmss(ms)}  ({ms} ms)")
        return True
    print(f"    [FAIL]  Player check failed: {r}")
    return False


def get_current_time():
    """Print the current playback position."""
    print("[*] Fetching current position…")
    r = inject_and_read(_JS_TIME)
    if r.startswith("TIME:"):
        ms = r[5:]
        print(f"    {_ms_to_mmss(ms)}  ({ms} ms)")
    else:
        print(f"    [FAIL]  {r}")


def play_video():
    print("[*] Sending play…")
    r = inject_and_read(_JS_PLAY)
    if r == "PLAYING":
        print("    Playing.")
    else:
        print(f"    [FAIL]  Play failed: {r}"); sys.exit(1)


def pause_video():
    print("[*] Sending pause…")
    r = inject_and_read(_JS_PAUSE)
    if r == "PAUSED":
        print("    Paused.")
    else:
        print(f"    [FAIL]  Pause failed: {r}"); sys.exit(1)


def toggle_playback():
    print("[*] Toggling play/pause…")
    r = inject_and_read(_JS_TOGGLE)
    if r == "TOGGLED:PLAYING":
        print("    Was paused -> now playing.")
    elif r == "TOGGLED:PAUSED":
        print("    Was playing -> now paused.")
    else:
        print(f"    [FAIL]  Toggle failed: {r}"); sys.exit(1)


def seek_to(ms: int):
    print(f"[*] Seeking to {_ms_to_mmss(str(ms * 1000))}  ({ms} ms)…")
    r = inject_and_read(_js_seek(ms))
    if r.startswith("SEEKED_TO:"):
        print("    Seek sent.")
    else:
        print(f"    [FAIL]  Seek failed: {r}"); sys.exit(1)


# ──────────────────────────────────────────────────────────────── #
#  CLI
# ──────────────────────────────────────────────────────────────── #

def parse_args():
    p = argparse.ArgumentParser(
        description="Control Netflix playback via JS injection (no extra libs).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python netflix_seek_test.py --play
  python netflix_seek_test.py --pause
  python netflix_seek_test.py --toggle
  python netflix_seek_test.py --get-time
  python netflix_seek_test.py --minutes 18 --seconds 30
  python netflix_seek_test.py --time 1091243
  python netflix_seek_test.py --check
        """
    )
    p.add_argument("--play",       action="store_true", help="Resume playback")
    p.add_argument("--pause",      action="store_true", help="Pause playback")
    p.add_argument("--toggle",     action="store_true", help="Toggle play/pause")
    p.add_argument("--get-time",   action="store_true", help="Print current position")
    p.add_argument("--check",      action="store_true", help="Check player API accessibility")
    p.add_argument("--time",       type=int, metavar="MS",  help="Seek to millisecond offset")
    p.add_argument("--minutes",    type=int, default=0, metavar="MIN")
    p.add_argument("--seconds",    type=int, default=0, metavar="SEC")
    return p.parse_args()


def main():
    args = parse_args()

    print("=" * 55)
    print("  Netflix Player Control")
    print("=" * 55)

    try:
        if args.check:
            sys.exit(0 if check_player() else 1)

        if args.get_time:
            get_current_time(); sys.exit(0)

        if args.play:
            play_video(); sys.exit(0)

        if args.pause:
            pause_video(); sys.exit(0)

        if args.toggle:
            toggle_playback(); sys.exit(0)

        # ── Seek ────────────────────────────────────────────────
        if args.time is not None:
            target_ms = args.time
        elif args.minutes or args.seconds:
            target_ms = (args.minutes * 60 + args.seconds) * 1000
        else:
            # Interactive fallback
            print("\nNo target provided.")
            print("  m = MM:SS   t = raw milliseconds")
            choice = input("Choice [m/t]: ").strip().lower()
            if choice == "m":
                ts = input("MM:SS : ").strip().split(":")
                if len(ts) != 2:
                    print("Bad format."); sys.exit(1)
                target_ms = (int(ts[0]) * 60 + int(ts[1])) * 1000
            else:
                target_ms = int(input("Milliseconds: ").strip())

        if not check_player():
            print("\n[!] Make sure Chrome has a Netflix video open and loaded.")
            sys.exit(1)

        seek_to(target_ms)
        time.sleep(0.8)
        get_current_time()
        print("\n[Done]")

    except RuntimeError as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
