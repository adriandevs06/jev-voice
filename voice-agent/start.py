"""start.py - one command to talk to the laptop.

    python start.py          live: says it, does it
    python start.py dry      logs what it WOULD do, touches nothing

Does the three things that used to be a checklist, then starts server.py:

  1. The brain's login. Only the multi-step half ("go to gmail, find X, forward
     it") needs it; opening apps, searching, typing and clicking do not. The
     desktop app signs in only the sessions it hosts, so the standalone CLI
     the brain runs has no login until `claude auth login` has run once.
     `python start.py login` runs it here; a normal start just says so.
  2. The Jev key (TYPESAFE_API_KEY or AI_GATEWAY_API_KEY) -- reported, never printed.
  3. The mic, picked by name (stream_ears.pick_mic), so a shifted device index
     cannot land on the silent SplitCam device again.

The listening bar at the top of the screen is the feedback; the console at
http://127.0.0.1:8420 (full transcript, typed commands) is there if wanted.
"""
import ctypes
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
sys.path.insert(0, str(HERE))


def free_ram_mb() -> int:
    class MEM(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MEM()
    m.dwLength = ctypes.sizeof(MEM)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return int(m.ullAvailPhys / 2**20)


def ensure_login(launcher, prompt: bool = False) -> bool:
    import hands
    st = hands.auth_status(launcher)
    if st.get("loggedIn"):
        print(f"  brain   : signed in ({st.get('authMethod')})")
        return True
    if not prompt:
        print("  brain   : not signed in -- simple commands work; multi-step needs"
              " `python start.py login` once")
        return False
    print("\n  brain   : NOT signed in -- one-time step.")
    print("            A browser tab opens; approve it there, then come back here.\n")
    subprocess.call([*launcher, "auth", "login"], env=hands.brain_env())
    st = hands.auth_status(launcher)
    if st.get("loggedIn"):
        print("  brain   : signed in")
        return True
    print("  brain   : still not signed in. Opening apps / searching / clicking works;")
    print("            multi-step requests will not until `claude auth login` succeeds.")
    return False


def main():
    args = [a.lower() for a in sys.argv[1:]]
    dry = any(a in ("dry", "--dry", "dry-run") for a in args)
    if dry:
        os.environ["VOICE_DRY_RUN"] = "1"
    print(f"\nvoice agent -- {'DRY RUN (logs only)' if dry else 'LIVE'}\n")

    import server                                  # heavy imports: numpy, whisper, fastapi
    from jev import credentials
    from stream_ears import pick_mic

    try:
        ensure_login(server.Brain.launcher(), prompt="login" in args)
    except RuntimeError:
        # No Claude Code on this machine: the instant commands still work;
        # anything that needs Claude says so on the bar instead of crashing.
        print("  brain   : Claude Code not installed -- instant commands only "
              "(install it from claude.com/claude-code, then run `claude` once to log in)")

    key, base, _ = credentials()
    print(f"  jev     : {'key found -> ' + base if key else 'no key -- rules only, fires on the pause'}")

    import sounddevice as sd
    mic = pick_mic(os.environ.get("VOICE_DEVICE"))
    print(f"  mic     : {sd.query_devices(mic)['name'] if mic is not None else 'system default'}")

    ram = free_ram_mb()
    if ram < 1500:
        print(f"  ram     : {ram} MB free -- startup will be slow. Closing heavy apps helps.")
    print("\n  when it's ready: DOUBLE-TAP ALT to talk (rising beep), double-tap again to stop.")
    print("  a bar at the top of the screen shows it listening, what it heard and what it did.")
    print(f"  full log: http://127.0.0.1:{server.PORT}\n")

    import uvicorn
    uvicorn.run(server.app, host="127.0.0.1", port=server.PORT, log_level="warning")


if __name__ == "__main__":
    main()
