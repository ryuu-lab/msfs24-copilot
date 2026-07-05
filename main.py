"""
FS2024 Smart Copilot
Uses DeepSeek-compatible OpenAI client for voice lines is not required at runtime;
This app drives keyboard macros + TTS callouts, optionally reading live telemetry
from MSFS via SimConnect (airspeed, radio altitude, gear state) instead of pure
fixed-delay timers.
"""

import tkinter as tk
from tkinter import ttk
import os
import json
import time
import logging
import threading
import queue

from dotenv import load_dotenv
import keyboard
import pyttsx3

# ============================================
# LOGGING (replaces bare print/except-pass)
# ============================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("copilot")

# ============================================
# SIMCONNECT (real telemetry, optional)
# ============================================
try:
    from SimConnect import SimConnect, AircraftRequests
    SIMCONNECT_AVAILABLE = True
except ImportError:
    SIMCONNECT_AVAILABLE = False
    logger.warning("python-SimConnect not installed - falling back to timing-based callouts")

load_dotenv()
api_key = os.getenv("DEEPSEEK_API_KEY")
if not api_key:
    logger.warning("DEEPSEEK_API_KEY not set - AI features requiring it will fail")

# ============================================
# SETTINGS (loaded early, validated)
# ============================================
SETTINGS_FILE = "copilot_settings.json"
DEFAULT_SETTINGS = {
    "key_bindings": {
        "GEAR_TOGGLE": "g",
        "FLAPS_INCR": "b",
        "FLAPS_DECR": "v",
        "AP_MASTER": "z",
        "PARKING_BRAKES": "space",
        "TOGGLE_MASTER_BATTERY": "shift+b",
        "TOGGLE_AVIONICS_MASTER": "shift+a",
        "PITOT_HEAT_ON": "shift+h",
        "TOGGLE_NAV_LIGHTS": "ctrl+l",
        "TOGGLE_LANDING_LIGHTS": "ctrl+shift+l",
        "TOGGLE_STROBES": "ctrl+shift+s",
        "TOGGLE_TAXI_LIGHTS": "ctrl+shift+t",
    },
    "callouts": {
        "80_knots": True,
        "v1": True,
        "v1_speed": 140,
        "rotate": True,
        "rotate_speed": 155,
        "positive_rate": True,
        "gear_up_call": True,
    },
    "voice_enabled": True,
    "voice_rate": 170,
    "action_delay": 2.0,
    "ui_always_on_top": True,
    "use_live_telemetry": True,   # NEW: prefer SimConnect over timers when available
    "telemetry_poll_hz": 5,       # NEW: how often to sample the sim
}


def _deep_merge_defaults(loaded, defaults):
    """Merge loaded settings into defaults, recursing into nested dicts and
    validating value types so a corrupt/partial settings file can't crash
    the app or silently disable nested sections."""
    merged = dict(defaults)
    if not isinstance(loaded, dict):
        return merged
    for key, default_val in defaults.items():
        if key not in loaded:
            continue
        loaded_val = loaded[key]
        if isinstance(default_val, dict) and isinstance(loaded_val, dict):
            merged[key] = _deep_merge_defaults(loaded_val, default_val)
        elif isinstance(default_val, bool):
            merged[key] = bool(loaded_val)
        elif isinstance(default_val, (int, float)) and isinstance(loaded_val, (int, float)):
            merged[key] = loaded_val
        elif isinstance(default_val, str) and isinstance(loaded_val, str):
            merged[key] = loaded_val
        # else: type mismatch -> keep default, don't crash
    return merged


def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r") as f:
            loaded = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Could not read {SETTINGS_FILE} ({e}); using defaults")
        return dict(DEFAULT_SETTINGS)
    return _deep_merge_defaults(loaded, DEFAULT_SETTINGS)


def save_settings():
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
    except OSError as e:
        logger.error(f"Could not save {SETTINGS_FILE}: {e}")


settings = load_settings()
KEY_MAP = settings["key_bindings"]

# ============================================
# TEXT-TO-SPEECH
# ============================================
voice_queue = queue.Queue()


def voice_worker():
    """Background thread - speaks items one at a time without interruption."""
    try:
        local_engine = pyttsx3.init()
    except Exception as e:
        logger.error(f"TTS engine failed to initialize: {e}")
        return

    local_engine.setProperty("rate", settings.get("voice_rate", 170))
    local_engine.setProperty("volume", 0.9)

    try:
        voices = local_engine.getProperty("voices")
        for voice in voices:
            if "british" in voice.name.lower() or "english" in voice.name.lower():
                local_engine.setProperty("voice", voice.id)
                break
    except Exception as e:
        logger.debug(f"Voice selection skipped: {e}")

    while True:
        try:
            text = voice_queue.get(timeout=1)
        except queue.Empty:
            continue
        if not text:
            continue
        try:
            logger.info(f"Speaking: {text}")
            local_engine.say(text)
            local_engine.runAndWait()
        except Exception as e:
            logger.error(f"Voice error: {e}")


def speak(text):
    if settings.get("voice_enabled", True) and text:
        voice_queue.put(text)


def speak_async(text):
    speak(text)


# ============================================
# THREAD-SAFE UI HELPERS
# ============================================
# Tkinter is not thread-safe: widgets must only be touched from the main
# thread. Background threads (voice worker, telemetry poller, action
# sequences) post work into this queue; the GUI drains it on a timer via
# root.after(), which always runs on the main thread.
ui_queue = queue.Queue()


def ui_post(fn, *args, **kwargs):
    """Schedule fn(*args, **kwargs) to run on the main/UI thread."""
    ui_queue.put((fn, args, kwargs))


def _drain_ui_queue():
    while True:
        try:
            fn, args, kwargs = ui_queue.get_nowait()
        except queue.Empty:
            break
        try:
            fn(*args, **kwargs)
        except Exception as e:
            logger.error(f"UI update failed: {e}")
    root.after(33, _drain_ui_queue)  # ~30Hz drain


# ============================================
# LIVE TELEMETRY (SimConnect)
# ============================================
class TelemetryMonitor:
    """Polls MSFS via SimConnect for real airspeed/altitude/gear state.
    Falls back cleanly to None readings if the sim isn't running or
    SimConnect isn't installed, so callers must handle missing data."""

    def __init__(self):
        self.sm = None
        self.aq = None
        self.connected = False
        self._stop = threading.Event()
        self._thread = None
        self.latest = {
            "airspeed": None,
            "radio_alt": None,
            "vertical_speed": None,
            "gear_position": None,
        }

    def try_connect(self):
        if not SIMCONNECT_AVAILABLE:
            return False
        try:
            self.sm = SimConnect()
            self.aq = AircraftRequests(self.sm, _time=200)
            self.connected = True
            logger.info("Connected to MSFS via SimConnect")
            return True
        except Exception as e:
            logger.warning(f"SimConnect connect failed (is MSFS running?): {e}")
            self.connected = False
            return False

    def start(self):
        if not settings.get("use_live_telemetry", True):
            return
        if not self.try_connect():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self.sm:
            try:
                self.sm.exit()
            except Exception:
                pass

    def _poll_loop(self):
        hz = max(1, int(settings.get("telemetry_poll_hz", 5)))
        period = 1.0 / hz
        consecutive_failures = 0
        while not self._stop.is_set():
            try:
                airspeed = self.aq.get("AIRSPEED_INDICATED")
                radio_alt = self.aq.get("PLANE_ALT_ABOVE_GROUND")
                vs = self.aq.get("VERTICAL_SPEED")
                gear = self.aq.get("GEAR_HANDLE_POSITION")
                if airspeed is not None:
                    self.latest["airspeed"] = float(airspeed)
                if radio_alt is not None:
                    self.latest["radio_alt"] = float(radio_alt)
                if vs is not None:
                    self.latest["vertical_speed"] = float(vs)
                if gear is not None:
                    self.latest["gear_position"] = gear
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                logger.debug(f"Telemetry read failed: {e}")
                if consecutive_failures > 20:
                    logger.warning("Lost SimConnect connection; stopping telemetry poll")
                    self.connected = False
                    return
            time.sleep(period)

    def get(self, key, default=None):
        return self.latest.get(key, default)


telemetry = TelemetryMonitor()

# ============================================
# CALLOUT MONITOR
# ============================================
class CalloutMonitor:
    """Triggers takeoff callouts. Uses live SimConnect telemetry (airspeed
    crossing thresholds, positive climb rate) when available; otherwise
    falls back to the original fixed-delay timeline."""

    def __init__(self):
        self.running = False
        self.takeoff_phase = False
        self.callouts_done = set()

    def start(self):
        self.running = True
        logger.info("Callout monitor ready")

    def stop(self):
        self.running = False
        self.takeoff_phase = False

    def trigger_takeoff_sequence(self):
        self.takeoff_phase = True
        self.callouts_done.clear()

        if telemetry.connected and settings.get("use_live_telemetry", True):
            threading.Thread(target=self._telemetry_sequence, daemon=True).start()
        else:
            threading.Thread(target=self._timer_sequence, daemon=True).start()
        return True

    def _telemetry_sequence(self):
        """Drive callouts off real airspeed/vertical speed instead of
        assuming a fixed acceleration profile."""
        callouts = settings["callouts"]
        v1_speed = callouts.get("v1_speed", 140)
        rotate_speed = callouts.get("rotate_speed", 155)
        gear_pressed = False
        timeout_at = time.time() + 120  # safety cutoff: 2 min max

        while self.takeoff_phase and time.time() < timeout_at:
            airspeed = telemetry.get("airspeed")
            vs = telemetry.get("vertical_speed")
            radio_alt = telemetry.get("radio_alt")

            if airspeed is None:
                # Telemetry dropped mid-sequence; hand off to timer fallback
                logger.warning("Telemetry unavailable mid-sequence, falling back to timers")
                self._timer_sequence()
                return

            if callouts.get("80_knots", True) and "80_knots" not in self.callouts_done and airspeed >= 80:
                self.callouts_done.add("80_knots")
                copilot_say("80 knots.", True)

            if callouts.get("v1", True) and "v1" not in self.callouts_done and airspeed >= v1_speed:
                self.callouts_done.add("v1")
                copilot_say("V1.", True)

            if "rotate" not in self.callouts_done and airspeed >= rotate_speed:
                if callouts.get("rotate", True):
                    self.callouts_done.add("rotate")
                    copilot_say("Rotate.", True)

            # Positive rate: airborne (radio alt climbing) and climbing
            airborne = radio_alt is not None and radio_alt > 15
            climbing = vs is not None and vs > 200  # ft/min threshold
            if callouts.get("positive_rate", True) and "positive_rate" not in self.callouts_done and airborne and climbing:
                self.callouts_done.add("positive_rate")
                copilot_say("Positive rate.", True)

            if (
                callouts.get("gear_up_call", True)
                and "positive_rate" in self.callouts_done
                and not gear_pressed
            ):
                self.callouts_done.add("gear_up_call")
                copilot_say("Gear up.", True)
                press_key(KEY_MAP.get("GEAR_TOGGLE", "g"))
                gear_pressed = True
                break  # sequence complete

            time.sleep(1.0 / max(1, int(settings.get("telemetry_poll_hz", 5))))

    def _timer_sequence(self):
        """Original fixed-delay fallback for when SimConnect is unavailable."""
        callouts = settings["callouts"]
        timeline = [
            (3.0, "80_knots", "80 knots."),
            (5.5, "v1", "V1."),
            (7.5, "rotate", "Rotate."),
            (9.5, "positive_rate", "Positive rate."),
            (10.5, "gear_up_call", "Gear up."),
        ]
        for delay, callout_key, phrase in timeline:
            if not self.takeoff_phase:
                break
            time.sleep(delay)
            if callout_key in self.callouts_done:
                continue
            if callouts.get(callout_key, True):
                self.callouts_done.add(callout_key)
                copilot_say(phrase, True)
                if callout_key == "gear_up_call":
                    press_key(KEY_MAP.get("GEAR_TOGGLE", "g"))


callout_monitor = CalloutMonitor()

# ============================================
# DARK THEME
# ============================================
THEME = {
    "bg": "#0d1117",
    "bg_secondary": "#161b22",
    "bg_button": "#21262d",
    "text": "#c9d1d9",
    "text_dim": "#8b949e",
    "text_bright": "#f0f6fc",
    "accent_green": "#238636",
    "accent_blue": "#1f6feb",
    "accent_orange": "#d2991d",
    "accent_purple": "#8957e5",
    "accent_red": "#da3633",
    "accent_cyan": "#39d2c0",
    "log_bg": "#0d1117",
    "log_text": "#7ee787",
}

# ============================================
# CORE FUNCTIONS
# ============================================
def press_key(key):
    """Press a single key. Returns True on success."""
    try:
        keyboard.press_and_release(key)
        time.sleep(0.05)
        return True
    except Exception as e:
        logger.error(f"Failed to press key '{key}': {e}")
        return False


def log(text):
    """Add text to the log box. Safe to call from any thread."""
    def _do():
        log_box.insert(tk.END, text + "\n")
        log_box.see(tk.END)
    ui_post(_do)


def copilot_log(text):
    """Update status label and log. Safe to call from any thread."""
    def _do():
        log_box.insert(tk.END, f"🗣 {text}\n")
        log_box.see(tk.END)
        lbl_status.config(text=text)
    ui_post(_do)


def copilot_say(text, speak_aloud=True):
    copilot_log(text)
    if speak_aloud:
        speak(text)
    time.sleep(0.3)


def captain_log(text):
    log(f"👨‍✈️ {text}")


# ============================================
# DELAYED ACTION EXECUTOR
# ============================================
def execute_with_delay(actions_list):
    """Execute actions with countdown for user to focus MSFS."""
    delay = settings.get("action_delay", 2.0)

    def do_actions():
        for i in range(int(delay), 0, -1):
            ui_post(lbl_status.config, text=f"⚠ Click MSFS! Executing in {i}s...")
            time.sleep(1)
        for say, key, speak_aloud in actions_list:
            if say:
                copilot_say(say, speak_aloud)
                time.sleep(0.5)
            if key:
                press_key(key)
                time.sleep(0.15)

    threading.Thread(target=do_actions, daemon=True).start()


def execute_instant(actions_list):
    for say, key, speak_aloud in actions_list:
        if say:
            copilot_say(say, speak_aloud)
            time.sleep(0.5)
        if key:
            press_key(key)
            time.sleep(0.15)


# ============================================
# QUICK ACTIONS (WITH DELAY)
# ============================================
def quick_gear():
    captain_log("Gear toggle")
    execute_with_delay([("Gear.", KEY_MAP.get("GEAR_TOGGLE", "g"), True)])


def quick_flaps_up():
    captain_log("Flaps up")
    execute_with_delay([("Flaps up.", KEY_MAP.get("FLAPS_DECR", "v"), True)])


def quick_flaps_down():
    captain_log("Flaps down")
    execute_with_delay([("Flaps down.", KEY_MAP.get("FLAPS_INCR", "b"), True)])


def quick_parking_brake():
    captain_log("Parking brake")
    execute_with_delay([("Parking brake.", KEY_MAP.get("PARKING_BRAKES", "space"), True)])


# ============================================
# SMART FLOWS
# ============================================
def flow_takeoff():
    captain_log("Takeoff Flow")
    execute_with_delay([
        ("Setting takeoff configuration.", None, True),
        ("Flaps 1.", "b", True),
        ("Landing lights on.", "ctrl+shift+l", True),
        ("Strobes on.", "ctrl+shift+s", True),
        ("Takeoff configuration set. Ready.", None, True),
    ])


def flow_go_around():
    captain_log("Go Around!")
    execute_with_delay([
        ("Go around, TOGA power.", None, True),
        ("Flaps one notch up.", "v", True),
        ("Positive rate.", None, True),
        ("Gear up.", "g", True),
        ("Go-around procedure complete.", None, True),
    ])


def flow_approach():
    captain_log("Approach Flow")
    execute_with_delay([
        ("Setting approach configuration.", None, True),
        ("Landing lights on.", "ctrl+shift+l", True),
        ("Flaps 1.", "b", True),
        ("Gear down.", "g", True),
        ("Approach configuration set.", None, True),
    ])


# ============================================
# CHECKLISTS
# ============================================
def checklist_before_start():
    captain_log("Before Start Checklist")
    log("═" * 35)
    execute_with_delay([
        ("Parking brake — Set.", "space", True),
        ("Battery master — On.", "shift+b", True),
        ("Avionics master — On.", "shift+a", True),
        ("Nav lights — On.", "ctrl+l", True),
        ("Pitot heat — On.", "shift+h", True),
        ("Flaps — Set for departure.", None, True),
        ("Flight controls — Free and correct.", None, True),
        ("Before start checklist complete.", None, True),
    ])
    log("═" * 35 + "\n")


def checklist_before_takeoff():
    captain_log("Before Takeoff Checklist")
    log("═" * 35)
    execute_with_delay([
        ("Flaps — Set.", "b", True),
        ("Landing lights — On.", "ctrl+shift+l", True),
        ("Strobes — On.", "ctrl+shift+s", True),
        ("Transponder — TA/RA.", None, True),
        ("Takeoff briefing — Complete.", None, True),
        ("Before takeoff checklist complete. Ready for departure.", None, True),
    ])
    log("═" * 35 + "\n")


def checklist_after_landing():
    captain_log("After Landing Checklist")
    log("═" * 35)
    execute_with_delay([
        ("Landing lights — Off.", "ctrl+shift+l", True),
        ("Strobes — Off.", "ctrl+shift+s", True),
        ("Taxi lights — On.", "ctrl+shift+t", True),
        ("Flaps — Up.", "v", True),
        ("Spoilers — Down.", None, True),
        ("Transponder — Standby.", None, True),
        ("After landing checklist complete.", None, True),
    ])
    log("═" * 35 + "\n")


# ============================================
# TAKEOFF CALLOUT TRIGGER
# ============================================
def trigger_takeoff_callouts():
    captain_log("Starting takeoff roll")
    mode = "live telemetry" if (telemetry.connected and settings.get("use_live_telemetry", True)) else "timed fallback"
    logger.info(f"Takeoff callout sequence starting ({mode})")
    copilot_say("Takeoff thrust set.", True)
    time.sleep(0.5)
    callout_monitor.trigger_takeoff_sequence()


# ============================================
# SETTINGS WINDOW
# ============================================
def open_settings_window():
    settings_win = tk.Toplevel(root)
    settings_win.title("Copilot Settings")
    settings_win.geometry("480x610")
    settings_win.configure(bg=THEME["bg"])
    settings_win.attributes("-topmost", True)

    tk.Label(settings_win, text="⚙ SETTINGS", font=("Arial", 14, "bold"),
             bg=THEME["bg"], fg=THEME["text_bright"]).pack(pady=10)

    notebook = ttk.Notebook(settings_win)
    notebook.pack(fill="both", expand=True, padx=10, pady=10)

    # === Voice Tab ===
    voice_tab = tk.Frame(notebook, bg=THEME["bg_secondary"])
    notebook.add(voice_tab, text="🎙 Voice")

    voice_var = tk.BooleanVar(value=settings.get("voice_enabled", True))
    tk.Checkbutton(voice_tab, text="Enable copilot voice", variable=voice_var,
                   bg=THEME["bg_secondary"], fg=THEME["text"],
                   selectcolor=THEME["bg_button"], font=("Arial", 10)).pack(pady=15, anchor="w", padx=20)

    tk.Label(voice_tab, text="Voice Speed:", bg=THEME["bg_secondary"],
             fg=THEME["text"]).pack(anchor="w", padx=20)
    rate_var = tk.IntVar(value=settings.get("voice_rate", 170))
    tk.Scale(voice_tab, from_=100, to=250, variable=rate_var, orient="horizontal",
              bg=THEME["bg_secondary"], fg=THEME["text"], troughcolor=THEME["bg_button"],
              length=350).pack(padx=20)
    tk.Label(voice_tab, text="100 = Slow | 170 = Normal | 250 = Fast",
             bg=THEME["bg_secondary"], fg=THEME["text_dim"], font=("Arial", 8)).pack()

    # === Timing Tab ===
    timing_tab = tk.Frame(notebook, bg=THEME["bg_secondary"])
    notebook.add(timing_tab, text="⏱ Timing")

    tk.Label(timing_tab, text="Action Delay (seconds):", bg=THEME["bg_secondary"],
             fg=THEME["text"], font=("Arial", 10)).pack(anchor="w", padx=20, pady=(15, 5))
    tk.Label(timing_tab, text="Time to click MSFS before action executes",
             bg=THEME["bg_secondary"], fg=THEME["text_dim"], font=("Arial", 8)).pack(anchor="w", padx=20)
    delay_var = tk.DoubleVar(value=settings.get("action_delay", 2.0))
    tk.Scale(timing_tab, from_=0.5, to=5.0, variable=delay_var, orient="horizontal",
              resolution=0.5, bg=THEME["bg_secondary"], fg=THEME["text"],
              troughcolor=THEME["bg_button"], length=350).pack(padx=20)

    # === Telemetry Tab (NEW) ===
    telemetry_tab = tk.Frame(notebook, bg=THEME["bg_secondary"])
    notebook.add(telemetry_tab, text="📡 Telemetry")

    status_text = "🟢 Connected" if telemetry.connected else "🔴 Not connected"
    tk.Label(telemetry_tab, text=f"SimConnect status: {status_text}",
             bg=THEME["bg_secondary"], fg=THEME["text_bright"],
             font=("Arial", 10, "bold")).pack(anchor="w", padx=20, pady=(15, 5))

    live_var = tk.BooleanVar(value=settings.get("use_live_telemetry", True))
    tk.Checkbutton(telemetry_tab, text="Use live sim data for takeoff callouts\n(falls back to timers if unavailable)",
                   variable=live_var, justify="left",
                   bg=THEME["bg_secondary"], fg=THEME["text"],
                   selectcolor=THEME["bg_button"]).pack(anchor="w", padx=20, pady=10)

    def reconnect():
        ok = telemetry.try_connect()
        msg = "Connected!" if ok else "Could not connect - is MSFS running?"
        ui_post(lbl_status.config, text=msg)

    tk.Button(telemetry_tab, text="🔄 Reconnect to MSFS", bg=THEME["accent_blue"], fg="white",
              command=reconnect, cursor="hand2").pack(anchor="w", padx=20, pady=10)

    # === Callouts Tab ===
    callouts_tab = tk.Frame(notebook, bg=THEME["bg_secondary"])
    notebook.add(callouts_tab, text="📢 Callouts")

    tk.Label(callouts_tab, text="Takeoff Callouts:", bg=THEME["bg_secondary"],
             fg=THEME["text_bright"], font=("Arial", 10, "bold")).pack(anchor="w", padx=20, pady=10)

    callout_vars = {}
    var80 = tk.BooleanVar(value=settings["callouts"].get("80_knots", True))
    callout_vars["80_knots"] = var80
    tk.Checkbutton(callouts_tab, text="80 Knots", variable=var80,
                   bg=THEME["bg_secondary"], fg=THEME["text"],
                   selectcolor=THEME["bg_button"]).pack(anchor="w", padx=30, pady=3)

    v1_frame = tk.Frame(callouts_tab, bg=THEME["bg_secondary"])
    v1_frame.pack(anchor="w", padx=30, pady=3, fill="x")
    varV1 = tk.BooleanVar(value=settings["callouts"].get("v1", True))
    callout_vars["v1"] = varV1
    tk.Checkbutton(v1_frame, text="V1", variable=varV1,
                   bg=THEME["bg_secondary"], fg=THEME["text"],
                   selectcolor=THEME["bg_button"]).pack(side="left")
    tk.Label(v1_frame, text="Speed:", bg=THEME["bg_secondary"], fg=THEME["text_dim"]).pack(side="left", padx=(20, 5))
    v1_speed_var = tk.IntVar(value=settings["callouts"].get("v1_speed", 140))
    callout_vars["v1_speed"] = v1_speed_var
    tk.Entry(v1_frame, textvariable=v1_speed_var, width=5, bg=THEME["bg_button"],
             fg=THEME["text_bright"]).pack(side="left")

    rot_frame = tk.Frame(callouts_tab, bg=THEME["bg_secondary"])
    rot_frame.pack(anchor="w", padx=30, pady=3, fill="x")
    varRot = tk.BooleanVar(value=settings["callouts"].get("rotate", True))
    callout_vars["rotate"] = varRot
    tk.Checkbutton(rot_frame, text="Rotate", variable=varRot,
                   bg=THEME["bg_secondary"], fg=THEME["text"],
                   selectcolor=THEME["bg_button"]).pack(side="left")
    tk.Label(rot_frame, text="Speed:", bg=THEME["bg_secondary"], fg=THEME["text_dim"]).pack(side="left", padx=(20, 5))
    rot_speed_var = tk.IntVar(value=settings["callouts"].get("rotate_speed", 155))
    callout_vars["rotate_speed"] = rot_speed_var
    tk.Entry(rot_frame, textvariable=rot_speed_var, width=5, bg=THEME["bg_button"],
             fg=THEME["text_bright"]).pack(side="left")

    varPR = tk.BooleanVar(value=settings["callouts"].get("positive_rate", True))
    callout_vars["positive_rate"] = varPR
    tk.Checkbutton(callouts_tab, text="Positive Rate", variable=varPR,
                   bg=THEME["bg_secondary"], fg=THEME["text"],
                   selectcolor=THEME["bg_button"]).pack(anchor="w", padx=30, pady=3)

    varGU = tk.BooleanVar(value=settings["callouts"].get("gear_up_call", True))
    callout_vars["gear_up_call"] = varGU
    tk.Checkbutton(callouts_tab, text="Gear Up (auto-retracts gear)", variable=varGU,
                   bg=THEME["bg_secondary"], fg=THEME["text"],
                   selectcolor=THEME["bg_button"]).pack(anchor="w", padx=30, pady=3)

    def save_all():
        settings["voice_enabled"] = voice_var.get()
        settings["voice_rate"] = rate_var.get()
        settings["action_delay"] = delay_var.get()
        settings["use_live_telemetry"] = live_var.get()
        for key, var in callout_vars.items():
            settings["callouts"][key] = var.get()
        save_settings()
        global KEY_MAP
        KEY_MAP = settings["key_bindings"]
        copilot_say("Settings saved.", True)
        settings_win.destroy()

    tk.Button(settings_win, text="💾 SAVE SETTINGS", bg=THEME["accent_green"], fg="white",
              font=("Arial", 12, "bold"), command=save_all, padx=30, pady=12).pack(pady=20)


# ============================================
# GUI
# ============================================
root = tk.Tk()
root.title("FS2024 Smart Copilot")
root.geometry("540x780")
root.configure(bg=THEME["bg"])

if settings.get("ui_always_on_top", True):
    root.attributes("-topmost", True)

title_frame = tk.Frame(root, bg=THEME["bg_secondary"], height=42)
title_frame.pack(fill="x")
title_frame.pack_propagate(False)
tk.Label(title_frame, text="✈ FS2024 SMART COPILOT", font=("Arial", 13, "bold"),
         bg=THEME["bg_secondary"], fg=THEME["text_bright"]).pack(side="left", padx=15, pady=8)
tk.Button(title_frame, text="⚙", bg=THEME["bg_secondary"], fg=THEME["text_dim"],
          font=("Arial", 13), bd=0, command=open_settings_window,
          activebackground=THEME["bg_button"], cursor="hand2").pack(side="right", padx=12, pady=5)

main = tk.Frame(root, bg=THEME["bg"])
main.pack(fill="both", expand=True, padx=10, pady=5)

sect_style = {"font": ("Arial", 10, "bold"), "bg": THEME["bg"], "fg": THEME["accent_cyan"]}
btn_sm = {"width": 10, "height": 2, "font": ("Arial", 9)}
btn_lg = {"width": 15, "height": 2, "font": ("Arial", 9, "bold")}

# --- TELEMETRY STATUS (NEW) ---
telemetry_status_var = tk.StringVar(value="📡 Telemetry: connecting...")
tk.Label(main, textvariable=telemetry_status_var, bg=THEME["bg"], fg=THEME["text_dim"],
         font=("Arial", 8)).pack(pady=(5, 0))

# --- QUICK ACTIONS ---
tk.Label(main, text="⚡ QUICK ACTIONS", **sect_style).pack(pady=(10, 5))
tk.Label(main, text=f"⏱ {settings.get('action_delay', 2)}s delay — click MSFS after pressing",
         bg=THEME["bg"], fg=THEME["text_dim"], font=("Arial", 8)).pack()

qf = tk.Frame(main, bg=THEME["bg"])
qf.pack(pady=5)
tk.Button(qf, text="Gear", bg=THEME["accent_blue"], fg="white", **btn_sm,
          command=quick_gear, cursor="hand2").pack(side="left", padx=3)
tk.Button(qf, text="Flaps Up", bg=THEME["accent_green"], fg="white", **btn_sm,
          command=quick_flaps_up, cursor="hand2").pack(side="left", padx=3)
tk.Button(qf, text="Flaps Dn", bg=THEME["accent_green"], fg="white", **btn_sm,
          command=quick_flaps_down, cursor="hand2").pack(side="left", padx=3)
tk.Button(qf, text="Park Brk", bg=THEME["accent_orange"], fg="black", **btn_sm,
          command=quick_parking_brake, cursor="hand2").pack(side="left", padx=3)

# --- TAKEOFF CALLOUTS ---
tk.Label(main, text="📢 TAKEOFF CALLOUTS", **sect_style).pack(pady=(15, 5))
tk.Label(main, text="Press when starting takeoff roll — auto-sequences callouts",
         bg=THEME["bg"], fg=THEME["text_dim"], font=("Arial", 8)).pack()
tk.Button(main, text="🚀 START TAKEOFF CALLOUTS", bg=THEME["accent_red"], fg="white",
          font=("Arial", 12, "bold"), width=25, height=2,
          command=trigger_takeoff_callouts, cursor="hand2").pack(pady=8)
tk.Label(main, text="80 knots → V1 → Rotate → Positive rate → Gear up",
         bg=THEME["bg"], fg=THEME["text_dim"], font=("Arial", 7)).pack()

# --- SMART FLOWS ---
tk.Label(main, text="🔄 SMART FLOWS", **sect_style).pack(pady=(15, 5))
ff = tk.Frame(main, bg=THEME["bg"])
ff.pack(pady=5)
tk.Button(ff, text="🛫 TAKEOFF\nFlow", bg=THEME["accent_green"], fg="white", **btn_lg,
          command=lambda: threading.Thread(target=flow_takeoff, daemon=True).start(),
          cursor="hand2").pack(side="left", padx=4)
tk.Button(ff, text="🔄 GO AROUND\nFlow", bg=THEME["accent_orange"], fg="black", **btn_lg,
          command=lambda: threading.Thread(target=flow_go_around, daemon=True).start(),
          cursor="hand2").pack(side="left", padx=4)
tk.Button(ff, text="🛬 APPROACH\nFlow", bg=THEME["accent_blue"], fg="white", **btn_lg,
          command=lambda: threading.Thread(target=flow_approach, daemon=True).start(),
          cursor="hand2").pack(side="left", padx=4)

# --- CHECKLISTS ---
tk.Label(main, text="✅ CHECKLISTS", **sect_style).pack(pady=(15, 5))
cf = tk.Frame(main, bg=THEME["bg"])
cf.pack(pady=5)
tk.Button(cf, text="BEFORE\nSTART", bg=THEME["accent_purple"], fg="white", **btn_lg,
          command=lambda: threading.Thread(target=checklist_before_start, daemon=True).start(),
          cursor="hand2").pack(side="left", padx=4)
tk.Button(cf, text="BEFORE\nTAKEOFF", bg=THEME["accent_purple"], fg="white", **btn_lg,
          command=lambda: threading.Thread(target=checklist_before_takeoff, daemon=True).start(),
          cursor="hand2").pack(side="left", padx=4)
tk.Button(cf, text="AFTER\nLANDING", bg=THEME["accent_purple"], fg="white", **btn_lg,
          command=lambda: threading.Thread(target=checklist_after_landing, daemon=True).start(),
          cursor="hand2").pack(side="left", padx=4)

# --- STATUS ---
tk.Label(main, text="📡 CURRENT STATUS", **sect_style).pack(pady=(15, 5))
lbl_status = tk.Label(main, text="Ready.", wraplength=480,
                       bg=THEME["bg_secondary"], fg=THEME["text_bright"],
                       font=("Arial", 10), anchor="w", justify="left",
                       relief="flat", padx=10, pady=8)
lbl_status.pack(pady=5, fill="x")

# --- LOG ---
tk.Label(main, text="📋 COMMS LOG", **sect_style).pack(pady=(10, 5))
log_frame = tk.Frame(main, bg=THEME["log_bg"], relief="flat", bd=1)
log_frame.pack(fill="both", expand=True, pady=5)
log_scroll = tk.Scrollbar(log_frame)
log_scroll.pack(side="right", fill="y")
log_box = tk.Text(log_frame, height=10, font=("Consolas", 9),
                   yscrollcommand=log_scroll.set, bg=THEME["log_bg"], fg=THEME["log_text"],
                   relief="flat", padx=8, pady=8, insertbackground=THEME["log_text"],
                   state="normal")
log_box.pack(fill="both", expand=True)
log_scroll.config(command=log_box.yview)

bottom = tk.Frame(root, bg=THEME["bg_secondary"], height=24)
bottom.pack(fill="x", side="bottom")
bottom.pack_propagate(False)
tk.Label(bottom, text="💡 Quick buttons have delay | Checkboxes/Flows execute after countdown",
         bg=THEME["bg_secondary"], fg=THEME["text_dim"], font=("Arial", 7)).pack(pady=4)


def _update_telemetry_status_label():
    if telemetry.connected:
        airspeed = telemetry.get("airspeed")
        txt = f"📡 Telemetry: 🟢 live (IAS {airspeed:.0f}kt)" if airspeed is not None else "📡 Telemetry: 🟢 connected"
    elif SIMCONNECT_AVAILABLE:
        txt = "📡 Telemetry: 🔴 not connected (using timers)"
    else:
        txt = "📡 Telemetry: ⚪ SimConnect not installed (using timers)"
    telemetry_status_var.set(txt)
    root.after(1000, _update_telemetry_status_label)


# ============================================
# STARTUP
# ============================================
threading.Thread(target=voice_worker, daemon=True).start()
root.after(0, _drain_ui_queue)
logger.info("Voice worker started")

callout_monitor.start()
telemetry.start()
root.after(500, _update_telemetry_status_label)

log("✈ FS2024 Smart Copilot Ready")
log("─" * 35)
log(f"⏱ Action delay: {settings.get('action_delay', 2)}s")
log(f"🎙 Voice: {'ON' if settings.get('voice_enabled') else 'OFF'}")
log(f"📢 Takeoff callouts: Ready ({'live telemetry' if SIMCONNECT_AVAILABLE else 'timer-based'})")
log("─" * 35 + "\n")

logger.info("=" * 50)
logger.info(" FS2024 SMART COPILOT - READY")
logger.info(f" Voice: {'ENABLED' if settings.get('voice_enabled') else 'DISABLED'}")
logger.info(f" Action delay: {settings.get('action_delay', 2)} seconds")
logger.info(f" SimConnect available: {SIMCONNECT_AVAILABLE}")
logger.info("=" * 50)

speak("Copilot online and ready.")

try:
    root.mainloop()
finally:
    callout_monitor.stop()
    telemetry.stop()
    logger.info("Copilot shut down.")