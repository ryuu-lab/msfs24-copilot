import tkinter as tk
from tkinter import ttk
import os
import json
import time
import threading
import queue
from openai import OpenAI
from dotenv import load_dotenv
import keyboard
import pyttsx3

# Try SimConnect
try:
    import SimConnect
    SIMCONNECT_AVAILABLE = True
except:
    SIMCONNECT_AVAILABLE = False
    print("⚠ SimConnect not available - using timing-based callouts")

load_dotenv()
api_key = os.getenv("DEEPSEEK_API_KEY")

client = OpenAI(
    api_key=api_key,
    base_url="https://api.deepseek.com"
)

# ============================================
# SETTINGS (loaded early)
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
}

def load_settings():
    try:
        with open(SETTINGS_FILE, 'r') as f:
            loaded = json.load(f)
            merged = DEFAULT_SETTINGS.copy()
            merged.update(loaded)
            return merged
    except:
        return DEFAULT_SETTINGS.copy()

def save_settings():
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f, indent=2)
    except:
        pass

settings = load_settings()
KEY_MAP = settings["key_bindings"]

# ============================================
# TEXT-TO-SPEECH (loaded after settings)
# ============================================
voice_queue = queue.Queue()

def voice_worker():
    """Background thread — speaks items one at a time without interruption"""
    local_engine = pyttsx3.init()
    local_engine.setProperty('rate', settings.get("voice_rate", 170))
    local_engine.setProperty('volume', 0.9)
    
    # Try to find a good voice
    voices = local_engine.getProperty('voices')
    for voice in voices:
        if 'british' in voice.name.lower() or 'english' in voice.name.lower():
            local_engine.setProperty('voice', voice.id)
            break
    
    while True:
        try:
            text = voice_queue.get(timeout=1)
            if text:
                try:
                    print(f"  🔊 Speaking: {text}")
                    local_engine.say(text)
                    local_engine.runAndWait()
                except Exception as e:
                    print(f"  ✗ Voice error: {e}")
        except queue.Empty:
            pass

def speak(text):
    """Add to voice queue — won't interrupt current speech"""
    if settings.get("voice_enabled", True) and text:
        voice_queue.put(text)

def speak_async(text):
    speak(text)

def update_voice_rate(rate):
    pass  # Voice engine is in worker thread, updated on restart

# ============================================
# CALLOUT MONITOR
# ============================================
class CalloutMonitor:
    """Monitors and triggers takeoff callouts with timing"""
    def __init__(self):
        self.running = False
        self.takeoff_phase = False
        self.callouts_done = set()
    
    def start(self):
        self.running = True
        print("✓ Callout monitor ready")
    
    def stop(self):
        self.running = False
        self.takeoff_phase = False
    
    def trigger_takeoff_sequence(self):
        """Manually trigger takeoff callout sequence with realistic timing"""
        self.takeoff_phase = True
        self.callouts_done.clear()
        
        def sequence():
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
        
        threading.Thread(target=sequence, daemon=True).start()
        return True

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
    """Press a single key"""
    try:
        keyboard.press_and_release(key)
        time.sleep(0.05)
        return True
    except:
        return False

def log(text):
    """Add text to log box"""
    log_box.insert(tk.END, text + "\n")
    log_box.see(tk.END)

def copilot_log(text):
    """Update status and log"""
    log(f"🗣 {text}")
    lbl_status.config(text=text)
    root.update_idletasks()

def copilot_say(text, speak_aloud=True):
    """Log and optionally speak a copilot message"""
    copilot_log(text)
    if speak_aloud:
        speak(text)
        time.sleep(0.3)  # Small delay to ensure voice queues properly

def captain_log(text):
    """Log captain action"""
    log(f"👨‍✈️ {text}")

# ============================================
# DELAYED ACTION EXECUTOR
# ============================================
def execute_with_delay(actions_list):
    """Execute actions with countdown for user to focus MSFS"""
    delay = settings.get("action_delay", 2.0)
    
    def do_actions():
        # Countdown
        for i in range(int(delay), 0, -1):
            lbl_status.config(text=f"⚠ Click MSFS! Executing in {i}s...")
            root.update_idletasks()
            time.sleep(1)
        
        # Execute all actions with delays between voice calls
        for say, key, speak_aloud in actions_list:
            if say:
                copilot_say(say, speak_aloud)
                time.sleep(0.5)  # Give voice time to process
            if key:
                press_key(key)
                time.sleep(0.15)
    
    threading.Thread(target=do_actions, daemon=True).start()

def execute_instant(actions_list):
    """Execute actions immediately"""
    for say, key, speak_aloud in actions_list:
        if say:
            copilot_say(say, speak_aloud)
            time.sleep(0.5)  # Give voice time to process
        if key:
            press_key(key)
            time.sleep(0.15)

# ============================================
# QUICK ACTIONS (WITH DELAY)
# ============================================
def quick_gear():
    captain_log("Gear toggle")
    execute_with_delay([
        ("Gear.", KEY_MAP.get("GEAR_TOGGLE", "g"), True),
    ])

def quick_flaps_up():
    captain_log("Flaps up")
    execute_with_delay([
        ("Flaps up.", KEY_MAP.get("FLAPS_DECR", "v"), True),
    ])

def quick_flaps_down():
    captain_log("Flaps down")
    execute_with_delay([
        ("Flaps down.", KEY_MAP.get("FLAPS_INCR", "b"), True),
    ])

def quick_parking_brake():
    captain_log("Parking brake")
    execute_with_delay([
        ("Parking brake.", KEY_MAP.get("PARKING_BRAKES", "space"), True),
    ])

# ============================================
# SMART FLOWS
# ============================================
def flow_takeoff():
    captain_log("Takeoff Flow")
    actions = [
        ("Setting takeoff configuration.", None, True),
        ("Flaps 1.", "b", True),
        ("Landing lights on.", "ctrl+shift+l", True),
        ("Strobes on.", "ctrl+shift+s", True),
        ("Takeoff configuration set. Ready.", None, True),
    ]
    execute_with_delay(actions)

def flow_go_around():
    captain_log("Go Around!")
    actions = [
        ("Go around, TOGA power.", None, True),
        ("Flaps one notch up.", "v", True),
        ("Positive rate.", None, True),
        ("Gear up.", "g", True),
        ("Go-around procedure complete.", None, True),
    ]
    execute_with_delay(actions)

def flow_approach():
    captain_log("Approach Flow")
    actions = [
        ("Setting approach configuration.", None, True),
        ("Landing lights on.", "ctrl+shift+l", True),
        ("Flaps 1.", "b", True),
        ("Gear down.", "g", True),
        ("Approach configuration set.", None, True),
    ]
    execute_with_delay(actions)

# ============================================
# CHECKLISTS
# ============================================
def checklist_before_start():
    captain_log("Before Start Checklist")
    log("═" * 35)
    actions = [
        ("Parking brake — Set.", "space", True),
        ("Battery master — On.", "shift+b", True),
        ("Avionics master — On.", "shift+a", True),
        ("Nav lights — On.", "ctrl+l", True),
        ("Pitot heat — On.", "shift+h", True),
        ("Flaps — Set for departure.", None, True),
        ("Flight controls — Free and correct.", None, True),
        ("Before start checklist complete.", None, True),
    ]
    execute_with_delay(actions)
    log("═" * 35 + "\n")

def checklist_before_takeoff():
    captain_log("Before Takeoff Checklist")
    log("═" * 35)
    actions = [
        ("Flaps — Set.", "b", True),
        ("Landing lights — On.", "ctrl+shift+l", True),
        ("Strobes — On.", "ctrl+shift+s", True),
        ("Transponder — TA/RA.", None, True),
        ("Takeoff briefing — Complete.", None, True),
        ("Before takeoff checklist complete. Ready for departure.", None, True),
    ]
    execute_with_delay(actions)
    log("═" * 35 + "\n")

def checklist_after_landing():
    captain_log("After Landing Checklist")
    log("═" * 35)
    actions = [
        ("Landing lights — Off.", "ctrl+shift+l", True),
        ("Strobes — Off.", "ctrl+shift+s", True),
        ("Taxi lights — On.", "ctrl+shift+t", True),
        ("Flaps — Up.", "v", True),
        ("Spoilers — Down.", None, True),
        ("Transponder — Standby.", None, True),
        ("After landing checklist complete.", None, True),
    ]
    execute_with_delay(actions)
    log("═" * 35 + "\n")

# ============================================
# TAKEOFF CALLOUT TRIGGER
# ============================================
def trigger_takeoff_callouts():
    """Start the takeoff callout sequence"""
    captain_log("Starting takeoff roll")
    copilot_say("Takeoff thrust set.", True)
    time.sleep(0.5)
    callout_monitor.trigger_takeoff_sequence()

# ============================================
# SETTINGS WINDOW
# ============================================
def open_settings_window():
    settings_win = tk.Toplevel(root)
    settings_win.title("Copilot Settings")
    settings_win.geometry("480x550")
    settings_win.configure(bg=THEME["bg"])
    settings_win.attributes('-topmost', True)
    
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
             fg=THEME["text"], font=("Arial", 10)).pack(anchor="w", padx=20, pady=(15,5))
    tk.Label(timing_tab, text="Time to click MSFS before action executes", 
             bg=THEME["bg_secondary"], fg=THEME["text_dim"], font=("Arial", 8)).pack(anchor="w", padx=20)
    delay_var = tk.DoubleVar(value=settings.get("action_delay", 2.0))
    tk.Scale(timing_tab, from_=0.5, to=5.0, variable=delay_var, orient="horizontal",
             resolution=0.5, bg=THEME["bg_secondary"], fg=THEME["text"],
             troughcolor=THEME["bg_button"], length=350).pack(padx=20)
    
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
    tk.Label(v1_frame, text="Speed:", bg=THEME["bg_secondary"], fg=THEME["text_dim"]).pack(side="left", padx=(20,5))
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
    tk.Label(rot_frame, text="Speed:", bg=THEME["bg_secondary"], fg=THEME["text_dim"]).pack(side="left", padx=(20,5))
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
root.geometry("540x750")
root.configure(bg=THEME["bg"])

if settings.get("ui_always_on_top", True):
    root.attributes('-topmost', True)

# Title bar
title_frame = tk.Frame(root, bg=THEME["bg_secondary"], height=42)
title_frame.pack(fill="x")
title_frame.pack_propagate(False)
tk.Label(title_frame, text="✈ FS2024 SMART COPILOT", font=("Arial", 13, "bold"),
         bg=THEME["bg_secondary"], fg=THEME["text_bright"]).pack(side="left", padx=15, pady=8)
tk.Button(title_frame, text="⚙", bg=THEME["bg_secondary"], fg=THEME["text_dim"],
          font=("Arial", 13), bd=0, command=open_settings_window,
          activebackground=THEME["bg_button"], cursor="hand2").pack(side="right", padx=12, pady=5)

# Main content area
main = tk.Frame(root, bg=THEME["bg"])
main.pack(fill="both", expand=True, padx=10, pady=5)

sect_style = {"font": ("Arial", 10, "bold"), "bg": THEME["bg"], "fg": THEME["accent_cyan"]}
btn_sm = {"width": 10, "height": 2, "font": ("Arial", 9)}
btn_lg = {"width": 15, "height": 2, "font": ("Arial", 9, "bold")}

# --- QUICK ACTIONS ---
tk.Label(main, text="⚡ QUICK ACTIONS", **sect_style).pack(pady=(10,5))
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
tk.Label(main, text="📢 TAKEOFF CALLOUTS", **sect_style).pack(pady=(15,5))
tk.Label(main, text="Press when starting takeoff roll — auto-sequences callouts", 
         bg=THEME["bg"], fg=THEME["text_dim"], font=("Arial", 8)).pack()
tk.Button(main, text="🚀 START TAKEOFF CALLOUTS", bg=THEME["accent_red"], fg="white",
          font=("Arial", 12, "bold"), width=25, height=2,
          command=trigger_takeoff_callouts, cursor="hand2").pack(pady=8)
tk.Label(main, text="80 knots → V1 → Rotate → Positive rate → Gear up", 
         bg=THEME["bg"], fg=THEME["text_dim"], font=("Arial", 7)).pack()

# --- SMART FLOWS ---
tk.Label(main, text="🔄 SMART FLOWS", **sect_style).pack(pady=(15,5))
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
tk.Label(main, text="✅ CHECKLISTS", **sect_style).pack(pady=(15,5))
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
tk.Label(main, text="📡 CURRENT STATUS", **sect_style).pack(pady=(15,5))
lbl_status = tk.Label(main, text="Ready.", wraplength=480,
                       bg=THEME["bg_secondary"], fg=THEME["text_bright"],
                       font=("Arial", 10), anchor="w", justify="left",
                       relief="flat", padx=10, pady=8)
lbl_status.pack(pady=5, fill="x")

# --- LOG ---
tk.Label(main, text="📋 COMMS LOG", **sect_style).pack(pady=(10,5))
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

# Bottom bar
bottom = tk.Frame(root, bg=THEME["bg_secondary"], height=24)
bottom.pack(fill="x", side="bottom")
bottom.pack_propagate(False)
tk.Label(bottom, text="💡 Quick buttons have delay | Checkboxes/Flows execute after countdown", 
         bg=THEME["bg_secondary"], fg=THEME["text_dim"], font=("Arial", 7)).pack(pady=4)

# ============================================
# STARTUP
# ============================================

# Start voice worker thread
threading.Thread(target=voice_worker, daemon=True).start()
time.sleep(0.5)  # Give worker time to initialize
print("✓ Voice worker started")

callout_monitor.start()

log("✈ FS2024 Smart Copilot Ready")
log("─" * 35)
log(f"⏱ Action delay: {settings.get('action_delay', 2)}s")
log(f"🎙 Voice: {'ON' if settings.get('voice_enabled') else 'OFF'}")
log(f"📢 Takeoff callouts: Ready")
log("─" * 35 + "\n")

print("\n" + "=" * 50)
print("  FS2024 SMART COPILOT - READY")
print("=" * 50)
print(f"  🎙 Voice: {'ENABLED' if settings.get('voice_enabled') else 'DISABLED'}")
print(f"  ⏱ Action delay: {settings.get('action_delay', 2)} seconds")
print(f"  📢 Callouts: 80kts → V1 → Rotate → Positive → Gear Up")
print(f"  ⚙ Settings: Click gear icon in app")
print("=" * 50 + "\n")

# Quick voice test
speak("Copilot online and ready.")

root.mainloop()

# Cleanup
callout_monitor.stop()
print("Copilot shut down.")