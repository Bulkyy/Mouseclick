"""
Macro Recorder - a small ReMouse-style tool for Windows 10.

Records mouse movement, clicks, scrolling (and optionally keyboard), then plays it
back with adjustable speed, looping, delays and more. Everything is controllable by
buttons or by global hotkeys that you can rebind.

Requires: pip install pynput
"""

import ctypes
import json
import os
import queue
import random
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Make coordinates match real pixels on scaled (125%/150%) displays.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from pynput import keyboard, mouse  # noqa: E402

APP_NAME = "Macro Recorder"
BASE_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "macro_recorder_settings.json")

DEFAULT_HOTKEYS = {"record": "f6", "play": "f7", "stop": "f8"}
HOTKEY_LABELS = {
    "record": "Start / stop recording",
    "play": "Start / stop playback",
    "stop": "Stop everything (emergency)",
}


# ---------------------------------------------------------------- key helpers
def key_name(k):
    """Stable string for a pynput key, used for hotkey matching."""
    if isinstance(k, keyboard.Key):
        return k.name
    if isinstance(k, keyboard.KeyCode):
        if k.char:
            return k.char.lower()
        return f"vk{k.vk}"
    return str(k)


def ser_key(k):
    if isinstance(k, keyboard.Key):
        return {"s": k.name}
    if isinstance(k, keyboard.KeyCode):
        if k.char is not None:
            return {"c": k.char}
        return {"v": k.vk}
    return None


def de_key(d):
    try:
        if "s" in d:
            return keyboard.Key[d["s"]]
        if "c" in d:
            return d["c"]
        return keyboard.KeyCode.from_vk(d["v"])
    except Exception:
        return None


# ------------------------------------------------------------------------ app
class MacroApp:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.resizable(False, False)

        self.ui_queue = queue.Queue()
        self.events = []
        self.recording = False
        self.playing = False
        self.rec_start = 0.0
        self.rec_lock = threading.Lock()
        self.mouse_listener = None
        self.stop_evt = threading.Event()
        self.play_thread = None
        self.binding = None  # which hotkey is waiting for a key press
        self.hotkeys = dict(DEFAULT_HOTKEYS)

        # settings variables
        self.v_speed = tk.StringVar(value="1.0")
        self.v_loops = tk.StringVar(value="1")
        self.v_infinite = tk.BooleanVar(value=False)
        self.v_loop_delay = tk.StringVar(value="0.0")
        self.v_start_delay = tk.StringVar(value="1.0")
        self.v_jitter = tk.StringVar(value="0")
        self.v_relative = tk.BooleanVar(value=False)
        self.v_rec_moves = tk.BooleanVar(value=True)
        self.v_rec_keys = tk.BooleanVar(value=False)
        self.v_topmost = tk.BooleanVar(value=False)
        self.v_minimize = tk.BooleanVar(value=False)

        self.load_config()
        self.build_ui()
        self.refresh_hotkey_labels()
        self.apply_topmost()

        self.kb_listener = keyboard.Listener(on_press=self.on_key_press, on_release=self.on_key_release)
        self.kb_listener.daemon = True
        self.kb_listener.start()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(40, self.poll_queue)

    # ------------------------------------------------------------------ UI
    def build_ui(self):
        pad = {"padx": 8, "pady": 4}
        main = ttk.Frame(self.root, padding=10)
        main.grid(row=0, column=0)

        # transport buttons
        bar = ttk.Frame(main)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.btn_record = ttk.Button(bar, text="Record", width=12, command=lambda: self.toggle_record(True))
        self.btn_play = ttk.Button(bar, text="Play", width=12, command=self.toggle_play)
        self.btn_stop = ttk.Button(bar, text="Stop all", width=12, command=self.stop_all)
        for i, b in enumerate((self.btn_record, self.btn_play, self.btn_stop)):
            b.grid(row=0, column=i, **pad)

        self.status = tk.StringVar(value="Idle")
        ttk.Label(main, textvariable=self.status, font=("Segoe UI", 10, "bold")).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 0))
        self.info = tk.StringVar(value="No recording")
        ttk.Label(main, textvariable=self.info).grid(row=2, column=0, columnspan=2, sticky="w", padx=8)

        # playback settings
        pb = ttk.LabelFrame(main, text="Playback", padding=8)
        pb.grid(row=3, column=0, sticky="nsew", padx=(0, 6), pady=8)

        def row(parent, r, text, widget):
            ttk.Label(parent, text=text).grid(row=r, column=0, sticky="w", pady=2)
            widget.grid(row=r, column=1, sticky="w", padx=6, pady=2)

        row(pb, 0, "Speed (x)", ttk.Spinbox(pb, from_=0.1, to=50, increment=0.25, width=7, textvariable=self.v_speed))
        row(pb, 1, "Loop count", ttk.Spinbox(pb, from_=1, to=999999, increment=1, width=7, textvariable=self.v_loops))
        ttk.Checkbutton(pb, text="Loop forever", variable=self.v_infinite).grid(
            row=2, column=0, columnspan=2, sticky="w")
        row(pb, 3, "Delay between loops (s)",
            ttk.Spinbox(pb, from_=0, to=3600, increment=0.5, width=7, textvariable=self.v_loop_delay))
        row(pb, 4, "Start delay (s)",
            ttk.Spinbox(pb, from_=0, to=60, increment=0.5, width=7, textvariable=self.v_start_delay))
        row(pb, 5, "Timing jitter (%)",
            ttk.Spinbox(pb, from_=0, to=100, increment=5, width=7, textvariable=self.v_jitter))
        ttk.Checkbutton(pb, text="Relative to cursor position", variable=self.v_relative).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # recording + options
        right = ttk.Frame(main)
        right.grid(row=3, column=1, sticky="nsew", pady=8)

        rc = ttk.LabelFrame(right, text="Recording / window", padding=8)
        rc.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(rc, text="Record mouse movement", variable=self.v_rec_moves).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(rc, text="Record keyboard too", variable=self.v_rec_keys).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(rc, text="Always on top", variable=self.v_topmost, command=self.apply_topmost).grid(
            row=2, column=0, sticky="w")
        ttk.Checkbutton(rc, text="Minimize while playing", variable=self.v_minimize).grid(row=3, column=0, sticky="w")

        hk = ttk.LabelFrame(right, text="Hotkeys (click Bind, then press a key)", padding=8)
        hk.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.hk_labels = {}
        self.hk_buttons = {}
        for r, action in enumerate(("record", "play", "stop")):
            ttk.Label(hk, text=HOTKEY_LABELS[action]).grid(row=r, column=0, sticky="w", pady=2)
            lbl = ttk.Label(hk, text="", width=8, anchor="center", relief="sunken")
            lbl.grid(row=r, column=1, padx=6)
            btn = ttk.Button(hk, text="Bind", width=6, command=lambda a=action: self.start_bind(a))
            btn.grid(row=r, column=2)
            self.hk_labels[action] = lbl
            self.hk_buttons[action] = btn

        # file buttons
        files = ttk.Frame(main)
        files.grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Button(files, text="Save recording...", command=self.save_recording).grid(row=0, column=0, **{"padx": 8})
        ttk.Button(files, text="Load recording...", command=self.load_recording).grid(row=0, column=1, padx=8)
        ttk.Button(files, text="Clear", command=self.clear_recording).grid(row=0, column=2, padx=8)

    def apply_topmost(self):
        self.root.attributes("-topmost", bool(self.v_topmost.get()))

    def refresh_hotkey_labels(self):
        for action, lbl in self.hk_labels.items():
            lbl.config(text=self.hotkeys[action].upper())
            self.hk_buttons[action].config(text="Bind")

    def set_status(self, text):
        self.status.set(text)

    def update_info(self):
        with self.rec_lock:
            n = len(self.events)
            dur = self.events[-1]["t"] if n else 0.0
        self.info.set(f"{n} events, {dur:.2f}s long" if n else "No recording")

    # ------------------------------------------------------- thread-safe UI
    def post(self, fn):
        self.ui_queue.put(fn)

    def poll_queue(self):
        try:
            while True:
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(40, self.poll_queue)

    # ------------------------------------------------------- number helpers
    @staticmethod
    def num(var, default, lo=None, hi=None, cast=float):
        try:
            val = cast(float(str(var.get()).strip()))
        except Exception:
            val = default
        if lo is not None:
            val = max(lo, val)
        if hi is not None:
            val = min(hi, val)
        return val

    # ---------------------------------------------------------- hotkeys
    def start_bind(self, action):
        if self.binding == action:
            self.binding = None
            self.refresh_hotkey_labels()
            return
        self.binding = action
        self.refresh_hotkey_labels()
        self.hk_buttons[action].config(text="Press...")
        self.hk_labels[action].config(text="...")

    def on_key_press(self, key):
        name = key_name(key)
        if self.binding:
            action = self.binding
            self.binding = None
            # remove duplicates so one key never triggers two actions
            for other, val in self.hotkeys.items():
                if val == name and other != action:
                    self.hotkeys[other] = DEFAULT_HOTKEYS[other] if DEFAULT_HOTKEYS[other] != name else "f12"
            self.hotkeys[action] = name
            self.post(self.refresh_hotkey_labels)
            self.post(self.save_config)
            return
        for action, val in self.hotkeys.items():
            if val == name:
                self.post({"record": self.toggle_record, "play": self.toggle_play, "stop": self.stop_all}[action])
                return
        if self.recording and self.v_rec_keys.get():
            self.add_event({"type": "key", "key": ser_key(key), "pressed": True})

    def on_key_release(self, key):
        name = key_name(key)
        if name in self.hotkeys.values():
            return
        if self.recording and self.v_rec_keys.get():
            self.add_event({"type": "key", "key": ser_key(key), "pressed": False})

    # ---------------------------------------------------------- recording
    def add_event(self, ev):
        if not self.recording:
            return
        ev["t"] = round(time.perf_counter() - self.rec_start, 5)
        with self.rec_lock:
            self.events.append(ev)

    def toggle_record(self, from_button=False):
        if self.recording:
            self.stop_recording(trim_click=from_button)
        else:
            self.start_recording()

    def start_recording(self):
        if self.playing:
            self.stop_playback()
        with self.rec_lock:
            self.events = []
        self.rec_start = time.perf_counter()
        self.recording = True
        self.btn_record.config(text="Stop recording")
        self.set_status("RECORDING...")
        self.info.set("Recording...")

        record_moves = self.v_rec_moves.get()

        def on_move(x, y):
            if record_moves:
                self.add_event({"type": "move", "x": x, "y": y})

        def on_click(x, y, button, pressed):
            self.add_event({"type": "click", "x": x, "y": y, "button": button.name, "pressed": pressed})

        def on_scroll(x, y, dx, dy):
            self.add_event({"type": "scroll", "x": x, "y": y, "dx": dx, "dy": dy})

        self.mouse_listener = mouse.Listener(on_move=on_move, on_click=on_click, on_scroll=on_scroll)
        self.mouse_listener.daemon = True
        self.mouse_listener.start()

    def stop_recording(self, trim_click=False):
        if not self.recording:
            return
        self.recording = False
        if self.mouse_listener:
            self.mouse_listener.stop()
            self.mouse_listener = None
        self.btn_record.config(text="Record")
        self.set_status("Idle")
        if trim_click:
            # Recording was stopped with the on-screen button, so drop that final click
            # (and the mouse movement toward it) instead of replaying it.
            with self.rec_lock:
                idx = None
                for i in range(len(self.events) - 1, -1, -1):
                    e = self.events[i]
                    if e["type"] == "click" and e["pressed"]:
                        idx = i
                        break
                if idx is not None and self.events[-1]["t"] - self.events[idx]["t"] < 0.6:
                    self.events = self.events[:idx]
        self.update_info()

    def clear_recording(self):
        if self.recording or self.playing:
            return
        with self.rec_lock:
            self.events = []
        self.update_info()

    # ----------------------------------------------------------- playback
    def toggle_play(self):
        if self.playing:
            self.stop_playback()
        else:
            self.start_playback()

    def start_playback(self):
        if self.recording:
            self.stop_recording()
        with self.rec_lock:
            events = list(self.events)
        if not events:
            self.set_status("Nothing to play - record something first")
            return

        speed = self.num(self.v_speed, 1.0, 0.05, 100)
        loops = 0 if self.v_infinite.get() else self.num(self.v_loops, 1, 1, cast=int)
        loop_delay = self.num(self.v_loop_delay, 0.0, 0)
        start_delay = self.num(self.v_start_delay, 0.0, 0)
        jitter = self.num(self.v_jitter, 0.0, 0, 100) / 100.0
        relative = self.v_relative.get()

        self.stop_evt = threading.Event()
        self.playing = True
        self.btn_play.config(text="Stop playback")
        if self.v_minimize.get():
            self.root.iconify()

        self.play_thread = threading.Thread(
            target=self.play_worker,
            args=(events, speed, loops, loop_delay, start_delay, jitter, relative, self.stop_evt),
            daemon=True,
        )
        self.play_thread.start()

    def stop_playback(self):
        self.stop_evt.set()

    def stop_all(self):
        if self.recording:
            self.stop_recording()
        self.stop_playback()

    @staticmethod
    def wait_until(stop_evt, start, target):
        """Sleep until perf_counter() reaches start+target. Returns False if stopped."""
        while True:
            if stop_evt.is_set():
                return False
            rem = start + target - time.perf_counter()
            if rem <= 0:
                return True
            if rem > 0.03:
                if stop_evt.wait(rem - 0.02):
                    return False
            else:
                time.sleep(0)  # short spin for accuracy

    def play_worker(self, events, speed, loops, loop_delay, start_delay, jitter, relative, stop_evt):
        mc = mouse.Controller()
        kc = keyboard.Controller()
        held_buttons = set()
        held_keys = []
        try:
            if start_delay > 0:
                self.post(lambda: self.set_status(f"Starting in {start_delay:g}s..."))
                if stop_evt.wait(start_delay):
                    return

            first_pos = next(((e["x"], e["y"]) for e in events if "x" in e), None)
            n = 0
            while not stop_evt.is_set() and (loops == 0 or n < loops):
                n += 1
                label = f"Playing loop {n}" + ("" if loops == 0 else f" / {loops}") + f"  ({speed:g}x)"
                self.post(lambda s=label: self.set_status(s))

                ox = oy = 0
                if relative and first_pos:
                    cx, cy = mc.position
                    ox, oy = cx - first_pos[0], cy - first_pos[1]

                start = time.perf_counter()
                cum = 0.0
                prev_t = 0.0
                for ev in events:
                    gap = (ev["t"] - prev_t) / speed
                    prev_t = ev["t"]
                    if jitter:
                        gap *= 1 + random.uniform(-jitter, jitter)
                    cum += max(gap, 0)
                    if not self.wait_until(stop_evt, start, cum):
                        return

                    kind = ev["type"]
                    if kind == "move":
                        mc.position = (ev["x"] + ox, ev["y"] + oy)
                    elif kind == "click":
                        mc.position = (ev["x"] + ox, ev["y"] + oy)
                        btn = mouse.Button[ev["button"]]
                        if ev["pressed"]:
                            mc.press(btn)
                            held_buttons.add(btn)
                        else:
                            mc.release(btn)
                            held_buttons.discard(btn)
                    elif kind == "scroll":
                        mc.position = (ev["x"] + ox, ev["y"] + oy)
                        mc.scroll(ev["dx"], ev["dy"])
                    elif kind == "key":
                        k = de_key(ev["key"])
                        if k is None:
                            continue
                        if ev["pressed"]:
                            kc.press(k)
                            held_keys.append(k)
                        else:
                            kc.release(k)
                            if k in held_keys:
                                held_keys.remove(k)

                if loops == 0 or n < loops:
                    if loop_delay > 0:
                        self.post(lambda: self.set_status("Waiting between loops..."))
                        if stop_evt.wait(loop_delay):
                            return
        except Exception as exc:  # keep the UI alive on any playback error
            self.post(lambda e=exc: self.set_status(f"Playback error: {e}"))
        finally:
            for b in held_buttons:
                try:
                    mc.release(b)
                except Exception:
                    pass
            for k in held_keys:
                try:
                    kc.release(k)
                except Exception:
                    pass
            self.post(self.playback_finished)

    def playback_finished(self):
        self.playing = False
        self.btn_play.config(text="Play")
        if not self.recording and not self.status.get().startswith("Playback error"):
            self.set_status("Idle")
        if self.v_minimize.get():
            self.root.deiconify()

    # ------------------------------------------------------------ file I/O
    def save_recording(self):
        with self.rec_lock:
            events = list(self.events)
        if not events:
            messagebox.showinfo(APP_NAME, "Nothing to save yet.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("Macro files", "*.json")], initialdir=BASE_DIR)
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "events": events}, f)

    def load_recording(self):
        path = filedialog.askopenfilename(filetypes=[("Macro files", "*.json")], initialdir=BASE_DIR)
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            events = data["events"]
            assert isinstance(events, list)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not load file:\n{exc}")
            return
        with self.rec_lock:
            self.events = events
        self.update_info()

    # -------------------------------------------------------------- config
    def load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            return
        self.hotkeys.update({k: v for k, v in cfg.get("hotkeys", {}).items() if k in DEFAULT_HOTKEYS})
        for name, var in (
            ("speed", self.v_speed), ("loops", self.v_loops), ("loop_delay", self.v_loop_delay),
            ("start_delay", self.v_start_delay), ("jitter", self.v_jitter),
        ):
            if name in cfg:
                var.set(str(cfg[name]))
        for name, var in (
            ("infinite", self.v_infinite), ("relative", self.v_relative), ("rec_moves", self.v_rec_moves),
            ("rec_keys", self.v_rec_keys), ("topmost", self.v_topmost), ("minimize", self.v_minimize),
        ):
            if name in cfg:
                var.set(bool(cfg[name]))

    def save_config(self):
        cfg = {
            "hotkeys": self.hotkeys,
            "speed": self.v_speed.get(), "loops": self.v_loops.get(),
            "loop_delay": self.v_loop_delay.get(), "start_delay": self.v_start_delay.get(),
            "jitter": self.v_jitter.get(),
            "infinite": self.v_infinite.get(), "relative": self.v_relative.get(),
            "rec_moves": self.v_rec_moves.get(), "rec_keys": self.v_rec_keys.get(),
            "topmost": self.v_topmost.get(), "minimize": self.v_minimize.get(),
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass

    def on_close(self):
        self.stop_all()
        self.save_config()
        try:
            self.kb_listener.stop()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    MacroApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
