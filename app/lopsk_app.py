#!/usr/bin/env python3
"""
LoPSK — PSK31 / PSK63 from a Meshtastic LoRa board (SX1262), plus a full
PSK31 decoder with waterfall, click-to-tune and a multi-signal browser.

Transmit: the app turns your text into Varicode bits and streams them to the
board's LoPSK firmware, which makes real BPSK by steering the SX1262's phase.
Receive: RTL-SDR dongle, any sound card input, or a WAV recording. (The LoRa
chip itself can only act as a signal-strength meter, not a PSK decoder.)

Works on Windows and Raspberry Pi OS. Needs: Python 3.9+, tkinter, numpy,
pyserial (esptool only for flashing; sounddevice / pyrtlsdr for receiving).
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np

import psk31
import psk31rx
import rx_sources

try:
    import serial
    import serial.tools.list_ports
except ImportError:                       # handled nicely in the UI
    serial = None

APP_NAME = "LoPSK"
VERSION = "1.0"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
FW_DIR = os.path.join(APP_DIR, "firmware")
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".lopsk.json")
SR = psk31rx.SR
WF_MAX_HZ = 3000

BOARDS = {
    "Heltec V3 (also Wireless Stick Lite V3 / Tracker)": ("esp32s3", "lopsk-heltec-v3.bin"),
    "LilyGO T-Beam v1.1/v1.2 (SX1262)": ("esp32", "lopsk-tbeam-sx1262.bin"),
    "LilyGO T3-S3 (SX1262)": ("esp32s3", "lopsk-t3s3-sx1262.bin"),
}

# There is no long-standing PSK31 "watering hole" on the bands the SX1262 can
# reach (150-960 MHz), so these are suggestions — agree a frequency with whoever
# is listening.
FREQ_PRESETS = {
    "70 cm weak-signal area — 432.100 MHz": 432.100,
    "33 cm — 903.100 MHz": 903.100,
    "Custom": None,
}
MODES = {"PSK31": 31.25, "PSK63": 62.5}

MACROS = [
    ("CQ", "CQ CQ CQ de {MY} {MY} {MY}\nCQ CQ CQ de {MY} {MY} {MY} pse k\n"),
    ("Answer", "{CALL} {CALL} de {MY} {MY} {MY} pse kn\n"),
    ("QSO", "{CALL} de {MY}\nThanks for the call {NAME}. Your signal is 599 here.\n"
            "My name is {MYNAME}, QTH {QTH}.\nHow copy? {CALL} de {MY} kn\n"),
    ("73", "{CALL} de {MY}\nThanks for the QSO {NAME}, 73 and hope to see you again!\n{CALL} de {MY} sk\n"),
    ("de MYCALL", "de {MY}\n"),
]

DEFAULTS = {
    "callsign": "", "their_call": "", "their_name": "", "my_name": "", "qth": "",
    "preset": list(FREQ_PRESETS)[0], "dial_mhz": "432.100", "mode": "PSK31",
    "tx_hz": "1000", "cal_hz": "0", "power": 10, "board": list(BOARDS)[0],
    "licensed": False, "rx_source": "", "rtl_ppm": "0", "rtl_gain": "auto",
    "tx_follows_rx": True, "squelch": 35, "afc": True, "steer": 0, "auto_end": True,
}

BG = "#11161c"; PANEL = "#1a222b"; FG = "#e6edf3"; MUTED = "#8b98a5"
ACCENT = "#ffb020"; GOOD = "#3fb950"; BAD = "#f85149"; TXRED = "#ff5a5a"; RXBLUE = "#58a6ff"
MONO = "Consolas" if sys.platform == "win32" else "DejaVu Sans Mono"


class LoPSKApp:
    def __init__(self, root):
        self.root = root
        self.cfg = dict(DEFAULTS)
        self.load_cfg()
        self.ser = None
        self.ui_q = queue.Queue()
        self.audio_q = queue.Queue(maxsize=50)
        self.fw_version = None

        # transmit state
        self.tx = None
        # receive state
        self.rx_src = None
        self.rx_mode = None
        self.rx_freq = float(self.cfg.get("tx_hz") or 1000)
        self.retune_to = None
        self.spec_rows = []
        self.lora_pts = []
        self.browser = {}            # id -> dict(freq, text, iid)
        self.browser_seq = 0
        self.wf_img = None
        self.wf_last = 0
        self.quality = 0.0

        root.title(f"{APP_NAME} — PSK31 from your LoRa radio")
        root.configure(bg=BG)
        root.geometry("1120x920")
        root.minsize(980, 820)
        self.build_style()
        self.build_ui()
        self.refresh_ports()
        threading.Thread(target=self.rx_worker, daemon=True).start()
        root.after(50, self.poll)
        root.after(100, self.tick)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------------------------------------------------- config
    def load_cfg(self):
        try:
            with open(CONFIG_PATH) as f:
                self.cfg.update(json.load(f))
        except Exception:
            pass

    def save_cfg(self):
        for k, v in self.vars.items():
            try:
                self.cfg[k] = v.get()
            except tk.TclError:
                pass
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump(self.cfg, f, indent=1)
        except Exception:
            pass

    # ---------------------------------------------------------- styling
    def build_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL,
                     bordercolor="#2d3843", lightcolor=PANEL, darkcolor=PANEL,
                     font=("Segoe UI", 10) if sys.platform == "win32" else ("DejaVu Sans", 10))
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("Panel.TLabel", background=PANEL, foreground=FG)
        st.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        st.configure("Head.TLabel", background=PANEL, foreground=ACCENT, font=("", 11, "bold"))
        st.configure("Title.TLabel", background=BG, foreground=FG, font=("", 18, "bold"))
        st.configure("Sub.TLabel", background=BG, foreground=MUTED)
        st.configure("TButton", background="#26313c", foreground=FG, padding=(10, 5))
        st.map("TButton", background=[("active", "#33414f"), ("disabled", "#1f272f")],
               foreground=[("disabled", "#56616c")])
        st.configure("Small.TButton", padding=(7, 3))
        st.configure("Accent.TButton", background=ACCENT, foreground="#111")
        st.map("Accent.TButton", background=[("active", "#ffc24d"), ("disabled", "#5c4a20")])
        st.configure("Stop.TButton", background="#7a2323", foreground=FG)
        st.map("Stop.TButton", background=[("active", "#963030")])
        st.configure("TCheckbutton", background=PANEL, foreground=FG)
        st.map("TCheckbutton", background=[("active", PANEL)])
        st.configure("TCombobox", fieldbackground=PANEL, background="#26313c", foreground=FG, arrowcolor=FG)
        st.map("TCombobox", fieldbackground=[("readonly", "#0d1217")], foreground=[("readonly", FG)],
               selectbackground=[("readonly", "#0d1217")], selectforeground=[("readonly", FG)])
        st.configure("TEntry", fieldbackground="#0d1217", foreground=FG, insertcolor=FG)
        st.configure("Horizontal.TScale", background=PANEL, troughcolor="#0d1217")
        st.configure("Q.Horizontal.TProgressbar", troughcolor="#0d1217", background=GOOD, bordercolor=PANEL)
        st.configure("Treeview", background="#0d1217", fieldbackground="#0d1217", foreground=FG,
                     rowheight=21, borderwidth=0, font=(MONO, 9))
        st.configure("Treeview.Heading", background="#26313c", foreground=MUTED, relief="flat")
        st.map("Treeview", background=[("selected", "#2b4a73")])
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)

    def panel(self, parent, title):
        f = ttk.Frame(parent, style="Panel.TFrame", padding=10)
        ttk.Label(f, text=title, style="Head.TLabel").grid(row=0, column=0, columnspan=8, sticky="w", pady=(0, 6))
        return f

    # ---------------------------------------------------------- layout
    def build_ui(self):
        c = self.cfg
        S, B, I = tk.StringVar, tk.BooleanVar, tk.IntVar
        self.vars = v = {
            "callsign": S(value=c["callsign"]), "their_call": S(value=c["their_call"]),
            "their_name": S(value=c["their_name"]), "my_name": S(value=c["my_name"]), "qth": S(value=c["qth"]),
            "preset": S(value=c["preset"]), "dial_mhz": S(value=c["dial_mhz"]), "mode": S(value=c["mode"]),
            "tx_hz": S(value=c["tx_hz"]), "cal_hz": S(value=c["cal_hz"]), "power": I(value=c["power"]),
            "board": S(value=c["board"]), "licensed": B(value=c["licensed"]),
            "rx_source": S(value=c["rx_source"]), "rtl_ppm": S(value=c["rtl_ppm"]), "rtl_gain": S(value=c["rtl_gain"]),
            "tx_follows_rx": B(value=c["tx_follows_rx"]), "squelch": I(value=c["squelch"]),
            "afc": B(value=c["afc"]), "steer": I(value=c["steer"]), "auto_end": B(value=c["auto_end"]),
        }
        if v["preset"].get() not in FREQ_PRESETS:
            v["preset"].set("Custom")

        top = ttk.Frame(self.root, padding=(14, 10, 14, 2))
        top.pack(fill="x")
        ttk.Label(top, text="LoPSK", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="   PSK31 for SX1262 Meshtastic boards", style="Sub.TLabel").pack(side="left", pady=(6, 0))
        self.state_lbl = tk.Label(top, text="RX", bg=BG, fg=GOOD, font=("", 14, "bold"))
        self.state_lbl.pack(side="right")

        body = ttk.Frame(self.root, padding=(14, 6))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1, uniform="c")
        body.columnconfigure(1, weight=1, uniform="c")
        body.rowconfigure(1, weight=3)
        body.rowconfigure(2, weight=2)

        # --- radio
        rp = self.panel(body, "Radio")
        rp.grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=(0, 8))
        ttk.Label(rp, text="Port", style="Panel.TLabel").grid(row=1, column=0, sticky="w")
        self.port_cb = ttk.Combobox(rp, width=20, state="readonly")
        self.port_cb.grid(row=1, column=1, columnspan=2, sticky="we", padx=4)
        ttk.Button(rp, text="↻", width=3, command=self.refresh_ports).grid(row=1, column=3)
        self.conn_btn = ttk.Button(rp, text="Connect", command=self.toggle_connect)
        self.conn_btn.grid(row=1, column=4, padx=(6, 0))
        self.radio_lbl = ttk.Label(rp, text="Not connected", style="Muted.TLabel")
        self.radio_lbl.grid(row=2, column=0, columnspan=5, sticky="w", pady=(3, 4))
        ttk.Label(rp, text="Power", style="Panel.TLabel").grid(row=3, column=0, sticky="w")
        pw = ttk.Scale(rp, from_=-9, to=22, variable=v["power"], orient="horizontal",
                       command=lambda e: self.pwr_lbl.config(text=f"{int(float(e))} dBm"))
        pw.grid(row=3, column=1, columnspan=3, sticky="we", padx=4)
        pw.bind("<ButtonRelease-1>", lambda e: self.send_power())
        self.pwr_lbl = ttk.Label(rp, text=f"{v['power'].get()} dBm", style="Panel.TLabel", width=7)
        self.pwr_lbl.grid(row=3, column=4, sticky="w")
        ttk.Label(rp, text="Firmware", style="Panel.TLabel").grid(row=4, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(rp, textvariable=v["board"], values=list(BOARDS), state="readonly", width=28)\
            .grid(row=4, column=1, columnspan=3, sticky="we", padx=4, pady=(6, 0))
        ttk.Button(rp, text="Flash", command=self.flash).grid(row=4, column=4, padx=(6, 0), pady=(6, 0))
        ttk.Checkbutton(rp, text="Compatibility phase mode (try this if nobody can decode you)",
                        variable=v["steer"], onvalue=1, offvalue=0,
                        command=lambda: self.cmd(f"STEER {self.vars['steer'].get()}"))\
            .grid(row=5, column=0, columnspan=5, sticky="w", pady=(6, 0))
        rp.columnconfigure(1, weight=1)

        # --- frequency
        fp = self.panel(body, "Frequency")
        fp.grid(row=0, column=1, sticky="nsew", padx=(5, 0), pady=(0, 8))
        ttk.Label(fp, text="Band", style="Panel.TLabel").grid(row=1, column=0, sticky="w")
        pc = ttk.Combobox(fp, textvariable=v["preset"], values=list(FREQ_PRESETS), state="readonly", width=30)
        pc.grid(row=1, column=1, columnspan=3, sticky="we", padx=4)
        pc.bind("<<ComboboxSelected>>", self.on_preset)
        ttk.Combobox(fp, textvariable=v["mode"], values=list(MODES), state="readonly", width=7)\
            .grid(row=1, column=4, sticky="e")
        v["mode"].trace_add("write", lambda *a: self.on_mode())
        ttk.Label(fp, text="Dial (MHz, USB)", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(fp, textvariable=v["dial_mhz"], width=11).grid(row=2, column=1, sticky="w", padx=4)
        ttk.Label(fp, text="TX audio (Hz)", style="Panel.TLabel").grid(row=2, column=2, sticky="e")
        ttk.Entry(fp, textvariable=v["tx_hz"], width=6).grid(row=2, column=3, sticky="w", padx=4)
        ttk.Label(fp, text="Calibration (Hz)", style="Panel.TLabel").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Entry(fp, textvariable=v["cal_hz"], width=11).grid(row=3, column=1, sticky="w", padx=4)
        ttk.Button(fp, text="Tune 10 s", command=self.tune).grid(row=3, column=2, columnspan=2, sticky="e", padx=4)
        self.freq_lbl = ttk.Label(fp, text="", style="Muted.TLabel", wraplength=470)
        self.freq_lbl.grid(row=4, column=0, columnspan=5, sticky="w", pady=(3, 0))
        fp.columnconfigure(1, weight=1)
        for key in ("dial_mhz", "tx_hz", "cal_hz"):
            v[key].trace_add("write", lambda *a: self.update_freq_label())

        # --- receive
        rxp = self.panel(body, "Receive")
        rxp.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(0, 8))
        self.build_rx(rxp)

        # --- transmit
        txp = self.panel(body, "Transmit")
        txp.grid(row=2, column=0, columnspan=2, sticky="nsew")
        self.build_tx(txp)

        self.update_freq_label()
        if serial is None:
            self.status("pyserial is not installed — run the installer script, or: pip install pyserial", BAD)

    def build_rx(self, p):
        v = self.vars
        row = ttk.Frame(p, style="Panel.TFrame")
        row.grid(row=1, column=0, columnspan=2, sticky="we")
        ttk.Label(row, text="Receiver", style="Panel.TLabel").pack(side="left")
        self.src_cb = ttk.Combobox(row, textvariable=v["rx_source"], state="readonly", width=46)
        self.src_cb.pack(side="left", padx=4)
        self.src_cb.bind("<<ComboboxSelected>>", lambda e: self.on_source_pick())
        ttk.Button(row, text="↻", width=3, command=self.refresh_sources).pack(side="left")
        self.rx_btn = ttk.Button(row, text="Start listening", style="Accent.TButton", command=self.toggle_rx)
        self.rx_btn.pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Decode WAV…", command=self.decode_wav).pack(side="left", padx=6)
        self.rtl_row = ttk.Frame(row, style="Panel.TFrame")
        self.rtl_row.pack(side="left", padx=(6, 0))
        ttk.Label(self.rtl_row, text="ppm", style="Panel.TLabel").pack(side="left", padx=(0, 3))
        ttk.Entry(self.rtl_row, textvariable=v["rtl_ppm"], width=4).pack(side="left")
        ttk.Label(self.rtl_row, text="gain", style="Panel.TLabel").pack(side="left", padx=(8, 3))
        ttk.Entry(self.rtl_row, textvariable=v["rtl_gain"], width=5).pack(side="left")
        self.rx_lbl = tk.Label(p, text="", bg=PANEL, fg=MUTED, anchor="w", justify="left", wraplength=1040)
        self.rx_lbl.grid(row=2, column=0, columnspan=2, sticky="we", pady=(4, 4))

        self.wf = tk.Canvas(p, height=110, bg="#05080b", highlightthickness=0, cursor="crosshair")
        self.wf.grid(row=3, column=0, columnspan=2, sticky="we")
        self.wf.bind("<Configure>", lambda e: self.redraw_waterfall(force=True))
        self.wf.bind("<Button-1>", self.on_wf_click)
        self.wf.bind("<Button-3>", self.on_wf_right)

        ctl = ttk.Frame(p, style="Panel.TFrame")
        ctl.grid(row=4, column=0, columnspan=2, sticky="we", pady=(6, 4))
        self.rxf_lbl = tk.Label(ctl, text="", bg=PANEL, fg=RXBLUE, font=(MONO, 11, "bold"))
        self.rxf_lbl.pack(side="left")
        ttk.Label(ctl, text="  signal", style="Muted.TLabel").pack(side="left")
        self.qbar = ttk.Progressbar(ctl, maximum=100, length=110, style="Q.Horizontal.TProgressbar")
        self.qbar.pack(side="left", padx=4)
        ttk.Checkbutton(ctl, text="AFC", variable=v["afc"]).pack(side="left", padx=(8, 0))
        ttk.Label(ctl, text="  squelch", style="Panel.TLabel").pack(side="left")
        ttk.Scale(ctl, from_=0, to=90, variable=v["squelch"], orient="horizontal", length=90)\
            .pack(side="left", padx=4)
        ttk.Checkbutton(ctl, text="TX on RX frequency", variable=v["tx_follows_rx"]).pack(side="left", padx=(10, 0))
        ttk.Button(ctl, text="Clear", style="Small.TButton",
                   command=lambda: self.rx_txt.delete("1.0", "end")).pack(side="right")

        split = ttk.Frame(p, style="Panel.TFrame")
        split.grid(row=5, column=0, columnspan=2, sticky="nsew")
        split.columnconfigure(0, weight=3)
        split.columnconfigure(1, weight=2)
        split.rowconfigure(0, weight=1)
        self.rx_txt = tk.Text(split, height=8, bg="#0d1217", fg=FG, relief="flat", wrap="word",
                              highlightthickness=0, font=(MONO, 11), insertbackground=FG)
        self.rx_txt.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.rx_txt.tag_config("tx", foreground=TXRED)
        self.rx_txt.tag_config("info", foreground=MUTED)
        self.rx_txt.tag_config("me", background="#5a1f22")
        cols = ("f", "t")
        self.tree = ttk.Treeview(split, columns=cols, show="headings", height=6)
        self.tree.heading("f", text="Hz", anchor="e")
        self.tree.heading("t", text="Signal browser — click to tune", anchor="w")
        self.tree.column("f", width=52, anchor="e", stretch=False)
        self.tree.column("t", width=330, anchor="w")
        self.tree.grid(row=0, column=1, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self.on_browser_pick)
        p.columnconfigure(0, weight=1)
        p.rowconfigure(5, weight=1)
        self.refresh_sources()

    def build_tx(self, p):
        v = self.vars
        row = ttk.Frame(p, style="Panel.TFrame")
        row.grid(row=1, column=0, columnspan=3, sticky="we")
        for i, (lbl, key, w, up) in enumerate((("My call", "callsign", 9, True), ("Name", "my_name", 9, False),
                                               ("QTH", "qth", 12, False), ("Their call", "their_call", 9, True),
                                               ("Their name", "their_name", 9, False))):
            ttk.Label(row, text=lbl, style="Panel.TLabel").pack(side="left", padx=(0 if i == 0 else 10, 3))
            ttk.Entry(row, textvariable=v[key], width=w).pack(side="left")
            if up:
                v[key].trace_add("write", lambda *a, k=key: self.force_upper(k))
        mr = ttk.Frame(p, style="Panel.TFrame")
        mr.grid(row=2, column=0, columnspan=3, sticky="we", pady=(8, 6))
        ttk.Label(mr, text="Macros", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        for name, text in MACROS:
            ttk.Button(mr, text=name, style="Small.TButton",
                       command=lambda t=text: self.insert_macro(t)).pack(side="left", padx=(0, 5))
        ttk.Checkbutton(mr, text="I hold an amateur license for this frequency",
                        variable=v["licensed"]).pack(side="right")

        self.tx_txt = tk.Text(p, height=4, bg="#0d1217", fg=FG, relief="flat", wrap="word",
                              highlightthickness=1, highlightbackground="#2d3843", highlightcolor=ACCENT,
                              font=(MONO, 11), insertbackground=FG, undo=True)
        self.tx_txt.grid(row=3, column=0, columnspan=3, sticky="nsew")
        self.tx_txt.tag_config("sent", foreground="#5d6873")
        self.tx_txt.tag_config("queued", foreground=TXRED)
        self.tx_txt.bind("<Control-Return>", lambda e: (self.send(), "break")[1])
        self.tx_txt.bind("<Return>", self.on_tx_enter)

        br = ttk.Frame(p, style="Panel.TFrame")
        br.grid(row=4, column=0, columnspan=3, sticky="we", pady=(8, 0))
        self.send_btn = ttk.Button(br, text="Send  (Ctrl+Enter)", style="Accent.TButton", command=self.send)
        self.send_btn.pack(side="left")
        ttk.Button(br, text="STOP", style="Stop.TButton", command=self.stop_tx).pack(side="left", padx=6)
        ttk.Checkbutton(br, text="Back to receive when the text is sent", variable=v["auto_end"])\
            .pack(side="left", padx=(6, 0))
        ttk.Button(br, text="Save WAV…", command=self.save_wav).pack(side="right")
        ttk.Button(br, text="▶ Play on speakers", command=self.play_preview).pack(side="right", padx=6)
        self.tx_lbl = tk.Label(p, text="", bg=PANEL, fg=MUTED, anchor="w")
        self.tx_lbl.grid(row=5, column=0, columnspan=3, sticky="we", pady=(6, 0))
        p.columnconfigure(0, weight=1)
        p.rowconfigure(3, weight=1)

    # ---------------------------------------------------------- small helpers
    def force_upper(self, key):
        var = self.vars[key]
        s = var.get()
        if s != s.upper():
            var.set(s.upper())

    def status(self, text, color=MUTED):
        self.rx_lbl.config(text=text, fg=color)

    def baud(self):
        return MODES.get(self.vars["mode"].get(), 31.25)

    def num(self, key, default=0.0):
        try:
            return float(self.vars[key].get())
        except (ValueError, tk.TclError):
            return default

    def on_preset(self, _=None):
        mhz = FREQ_PRESETS.get(self.vars["preset"].get())
        if mhz:
            self.vars["dial_mhz"].set(f"{mhz:.3f}")
        self.relisten_lora()

    def on_mode(self):
        self.retune_to = self.rx_freq
        self.update_freq_label()

    def freq_plan(self):
        dial = float(self.vars["dial_mhz"].get()) * 1e6
        audio = float(self.vars["tx_hz"].get())
        cal = float(self.vars["cal_hz"].get() or 0)
        if not 200 <= audio <= 2900:
            raise ValueError("TX audio frequency should be 200–2900 Hz.")
        rf = dial + audio + cal
        if not 150e6 <= rf <= 960e6:
            raise ValueError("The SX1262 can only transmit between 150 and 960 MHz.")
        return dial, audio, cal, rf

    def update_freq_label(self):
        try:
            dial, audio, cal, rf = self.freq_plan()
            self.freq_lbl.config(text=f"You transmit on {rf/1e6:.6f} MHz. Receivers in USB on {dial/1e6:.3f} MHz "
                                      f"see you at {audio:.0f} Hz on their waterfall.", foreground=MUTED)
        except ValueError as e:
            self.freq_lbl.config(text=str(e) if "SX1262" in str(e) or "TX audio" in str(e)
                                 else "Enter numbers for the dial, audio and calibration.", foreground=BAD)
        self.redraw_waterfall(force=True)

    # ---------------------------------------------------------- serial
    def refresh_ports(self):
        if serial is None:
            return
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_cb["values"] = ports
        if ports and self.port_cb.get() not in ports:
            pref = [p for p in ports if any(s in p for s in ("USB", "ACM", "COM"))]
            self.port_cb.set((pref or ports)[-1])

    def toggle_connect(self):
        self.disconnect() if self.ser else self.connect()

    def connect(self):
        if serial is None:
            messagebox.showerror(APP_NAME, "pyserial is missing. Install it with: pip install pyserial")
            return
        port = self.port_cb.get()
        if not port:
            messagebox.showinfo(APP_NAME, "Plug in the board and pick its port.")
            return
        try:
            self.ser = serial.Serial(port, 115200, timeout=0.2)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Couldn't open {port}:\n{e}")
            return
        self.conn_btn.config(text="Disconnect")
        self.radio_lbl.config(text=f"Opened {port}, waiting for LoPSK firmware…", foreground=MUTED)
        threading.Thread(target=self.reader, args=(self.ser,), daemon=True).start()
        self.root.after(1500, lambda: self.cmd("HELLO"))
        self.root.after(1700, self.send_power)
        self.root.after(1800, lambda: self.cmd(f"STEER {self.vars['steer'].get()}"))
        self.root.after(4000, self.check_hello)

    def check_hello(self):
        if self.ser and not self.fw_version:
            self.radio_lbl.config(text="No answer — flash LoPSK firmware to this board (Flash button).",
                                  foreground=ACCENT)

    def disconnect(self):
        s, self.ser = self.ser, None
        self.fw_version = None
        if s:
            try:
                s.write(b"STOP\n")
                s.close()
            except Exception:
                pass
        if self.tx:
            self.finish_tx("Disconnected")
        self.conn_btn.config(text="Connect")
        self.radio_lbl.config(text="Not connected", foreground=MUTED)

    def reader(self, s):
        buf = b""
        while self.ser is s:
            try:
                data = s.read(256)
            except Exception as e:
                self.ui_q.put(("serial_err", str(e)))
                return
            if not data:
                continue
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.ui_q.put(("line", line.decode(errors="replace").strip()))

    def cmd(self, text):
        if not self.ser:
            return False
        try:
            self.ser.write((text + "\n").encode())
            self.last_cmd = time.time()
            return True
        except Exception as e:
            self.status(f"Serial write failed: {e}", BAD)
            self.disconnect()
            return False

    def send_power(self):
        p = int(round(self.vars["power"].get()))
        self.vars["power"].set(p)
        self.pwr_lbl.config(text=f"{p} dBm")
        self.cmd(f"PWR {p}")

    # ---------------------------------------------------------- transmit
    def macro_text(self, t):
        v = self.vars
        my = v["callsign"].get().strip().upper() or "NOCALL"
        return (t.replace("{MY}", my).replace("{CALL}", v["their_call"].get().strip().upper() or "??")
                 .replace("{NAME}", v["their_name"].get().strip() or "OM")
                 .replace("{MYNAME}", v["my_name"].get().strip() or "?")
                 .replace("{QTH}", v["qth"].get().strip() or "?"))

    def insert_macro(self, t):
        self.tx_txt.insert("end", self.macro_text(t))
        self.tx_txt.see("end")
        self.tx_txt.focus_set()

    def unsent_text(self):
        start = self.tx_txt.index("sent_end") if "sent_end" in self.tx_txt.mark_names() else "1.0"
        return start, self.tx_txt.get(start, "end-1c")

    def ready_to_tx(self):
        v = self.vars
        if not self.ser:
            messagebox.showinfo(APP_NAME, "Connect to the radio first.")
            return False
        if not self.fw_version:
            messagebox.showinfo(APP_NAME, "The board hasn't answered as LoPSK firmware yet — flash it first.")
            return False
        if not v["licensed"].get():
            messagebox.showwarning(APP_NAME, "Transmitting PSK31 needs an amateur radio license.\n"
                                   "Tick the license box once you're sure you're allowed on this frequency.")
            return False
        if len(v["callsign"].get().strip()) < 3:
            messagebox.showwarning(APP_NAME, "Enter your callsign first (it identifies your station).")
            return False
        try:
            self.freq_plan()
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e))
            return False
        return True

    def on_tx_enter(self, event):
        # while on the air, Enter sends the line you just typed (live keyboard mode)
        if self.tx and not self.tx["ending"]:
            self.tx_txt.insert("insert", "\n")
            self.send()
            return "break"
        return None

    def send(self):
        start, text = self.unsent_text()
        if not text.strip() and not self.tx:
            messagebox.showinfo(APP_NAME, "Type something (or click a macro) first.")
            return
        if not self.tx:
            if not self.ready_to_tx():
                return
            my = self.vars["callsign"].get().strip().upper()
            if my not in text.upper():
                if not messagebox.askyesno(APP_NAME, "This text doesn't contain your callsign, so it doesn't "
                                           "identify your station. Send it anyway?"):
                    return
            if self.vars["tx_follows_rx"].get() and self.rx_mode in ("rtl", "sound"):
                # answer exactly where the station you're reading is (AFC-tracked)
                f = getattr(self, "cur_rx_freq", None)
                if f and 200 <= f <= 2900:
                    self.vars["tx_hz"].set(str(int(round(f))))
            self.save_cfg()
            self.start_tx()
        if text:
            self.queue_text(text)
            end = self.tx_txt.index("end-1c")
            self.tx_txt.tag_add("queued", start, end)
            self.tx_txt.mark_set("sent_end", end)
            self.tx_txt.mark_gravity("sent_end", "left")
        if self.vars["auto_end"].get():
            self.tx["ending"] = True

    def start_tx(self):
        dial, audio, cal, rf = self.freq_plan()
        b = self.baud()
        self.tx = {"pending": "", "sent": 0, "played": 0, "fw_q": 0, "chars": [], "echoed": 0,
                   "ending": False, "end_sent": False, "on": False, "rf": rf, "started": time.time(),
                   "total": 0}
        self.tx["pending"] = "0" * psk31.PREAMBLE_BITS
        self.tx["total"] = psk31.PREAMBLE_BITS
        if self.rx_mode == "lora":
            self.cmd("STOP")
        self.cmd(f"PSK {rf:.0f} {b}")
        self.rx_txt.insert("end", "\n", "tx")
        self.state_lbl.config(text="● TX", fg=TXRED)
        self.tx_lbl.config(text=f"Starting {self.vars['mode'].get()} on {rf/1e6:.6f} MHz…", fg=TXRED)

    def queue_text(self, text):
        t = self.tx
        for ch in text.replace("\r\n", "\n"):
            bits = psk31.varicode_bits(ch)
            t["pending"] += bits
            t["total"] += len(bits)
            t["chars"].append((t["total"], ch))

    def pump_tx(self):
        t = self.tx
        if not t or not t["on"]:
            return
        # keep ~1.5 s of bits in the board's queue
        room = int(1.5 * self.baud()) * 2 - (t["sent"] - t["played"])
        if t["pending"] and room > 60:
            chunk = t["pending"][:min(room, 380)]
            t["pending"] = t["pending"][len(chunk):]
            if self.cmd("B " + chunk):
                t["sent"] += len(chunk)
        elif t["ending"] and not t["pending"] and not t["end_sent"]:
            self.cmd("END")
            t["end_sent"] = True
        elif time.time() - getattr(self, "last_cmd", 0) > 2:
            self.cmd("STATUS")                     # keep-alive: the board drops TX if we go quiet
        # echo what has actually gone out
        while t["echoed"] < len(t["chars"]) and t["chars"][t["echoed"]][0] <= t["played"]:
            ch = t["chars"][t["echoed"]][1]
            if ch != "\r":
                self.rx_txt.insert("end", ch, "tx")
                self.rx_txt.see("end")
            t["echoed"] += 1
        left = len(t["chars"]) - t["echoed"]
        secs = (t["total"] - t["played"]) / self.baud()
        self.tx_lbl.config(text=f"● On the air  {t['rf']/1e6:.6f} MHz   {left} characters to go (~{secs:.0f} s)"
                           + ("" if left or t["ending"] else "   — idling, type more or press STOP"),
                           fg=TXRED)

    def stop_tx(self):
        if self.tx:
            self.cmd("STOP")
            self.finish_tx("Stopped")
        else:
            self.cmd("STOP")

    def finish_tx(self, why):
        t, self.tx = self.tx, None
        self.state_lbl.config(text="RX", fg=GOOD)
        if t:
            took = time.time() - t["started"]
            self.tx_lbl.config(text=f"{why} after {took:.0f} s.", fg=MUTED)
            self.rx_txt.insert("end", "\n", "tx")
        if "sent_end" in self.tx_txt.mark_names():
            self.tx_txt.tag_remove("queued", "1.0", "end")
            self.tx_txt.tag_add("sent", "1.0", "sent_end")
        self.relisten_lora()

    def tune(self):
        if not self.ready_to_tx():
            return
        dial, audio, cal, rf = self.freq_plan()
        self.cmd(f"TUNE {rf:.0f} 10")
        self.tx_lbl.config(text=f"Tune carrier on {rf/1e6:.6f} MHz for 10 s — on your receiver it should sit at "
                                f"{audio:.0f} Hz. If it shows at X Hz instead, set Calibration to {audio:.0f} − X.",
                           fg=ACCENT)

    def preview_audio(self, board=True):
        start, text = self.unsent_text()
        text = text or self.tx_txt.get("1.0", "end-1c")
        if not text.strip():
            return None
        bits = psk31.frame_bits(text)
        f = self.num("tx_hz", 1000)
        fn = psk31.board_audio if board else psk31.standard_audio
        return fn(bits, f, self.baud())

    def play_preview(self):
        a = self.preview_audio()
        if a is None:
            messagebox.showinfo(APP_NAME, "Type something in the transmit box first.")
            return
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "lopsk_play.wav")
        rx_sources.write_wav(path, a)
        try:
            if sys.platform == "win32":
                import winsound
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                import shutil
                player = shutil.which("paplay") or shutil.which("pw-play") or shutil.which("aplay")
                if not player:
                    raise RuntimeError("no audio player found (aplay/paplay)")
                subprocess.Popen([player, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.tx_lbl.config(text=f"Playing what the board will send ({len(a)/SR:.0f} s).", fg=GOOD)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Couldn't play audio: {e}")

    def save_wav(self):
        a = self.preview_audio()
        if a is None:
            messagebox.showinfo(APP_NAME, "Type something in the transmit box first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".wav", initialfile="lopsk_preview.wav",
                                            filetypes=[("WAV audio", "*.wav")])
        if path:
            rx_sources.write_wav(path, a)
            self.tx_lbl.config(text=f"Saved {os.path.basename(path)} — exactly what the board sends. "
                                    "Open it with Decode WAV, or in fldigi, to test.", fg=GOOD)

    # ---------------------------------------------------------- flashing
    def flash(self):
        board = self.vars["board"].get()
        chip, fname = BOARDS[board]
        path = os.path.join(FW_DIR, fname)
        port = self.port_cb.get()
        if not os.path.exists(path):
            messagebox.showerror(APP_NAME, f"Firmware file missing:\n{path}")
            return
        if not port:
            messagebox.showinfo(APP_NAME, "Plug in the board and pick its port first.")
            return
        if not messagebox.askyesno(APP_NAME, f"Flash LoPSK firmware for:\n{board}\n\non {port}?\n\n"
                                   "This replaces what's on the board now (Meshtastic or LoFT8). You can put "
                                   "Meshtastic back any time at flasher.meshtastic.org."):
            return
        self.disconnect()
        self.status(f"Flashing {fname} to {port}…", ACCENT)
        import importlib.util
        import shutil
        if importlib.util.find_spec("esptool"):
            tool = [sys.executable, "-m", "esptool"]
        else:
            exe = shutil.which("esptool") or shutil.which("esptool.py")
            tool = [exe] if exe else ["esptool-missing"]
        verb = "write_flash"
        try:
            import importlib.metadata
            if int(importlib.metadata.version("esptool").split(".")[0]) >= 5:
                verb = "write-flash"
        except Exception:
            pass
        args = tool + ["--chip", chip, "--port", port, "--baud", "460800", verb, "0x0", path]

        def run():
            try:
                p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for line in p.stdout:
                    line = line.strip()
                    if line:
                        self.ui_q.put(("flashlog", line))
                self.ui_q.put(("flashdone", p.wait()))
            except (FileNotFoundError, OSError):
                self.ui_q.put(("flashdone", -1))
        threading.Thread(target=run, daemon=True).start()

    # ---------------------------------------------------------- receive
    SRC_RTL = "RTL-SDR dongle (tunes the dial frequency itself)"
    SRC_LORA = "LoRa board — signal meter only (can't decode PSK)"

    def refresh_sources(self):
        items = [self.SRC_RTL]
        self.sound_devs = {}
        for idx, name in rx_sources.SoundCardSource.list_devices():
            label = f"Sound card: {name}"
            self.sound_devs[label] = idx
            items.append(label)
        if not self.sound_devs:
            items.append("Sound card (install the 'sounddevice' package to use)")
        items.append(self.SRC_LORA)
        self.src_cb["values"] = items
        if self.vars["rx_source"].get() not in items:
            self.vars["rx_source"].set(items[0])
        self.on_source_pick()

    def on_source_pick(self):
        src = self.vars["rx_source"].get()
        if src == self.SRC_RTL:
            self.rtl_row.pack(side="left", padx=(6, 0))
            ok, _ = rx_sources.rtl_available()
            self.status("RTL-SDR: plug it in with an antenna for the band. LoPSK tunes it to the Dial frequency "
                        "and demodulates USB itself." + ("" if ok else "  ⚠ RTL-SDR driver library not found — "
                                                         "see the README."), MUTED if ok else ACCENT)
        else:
            self.rtl_row.pack_forget()
            if src == self.SRC_LORA:
                self.status("Uses the board's own receiver as a signal-strength meter. The SX1262's narrowest "
                            "filter is ~4.8 kHz wide and it has no phase output, so it can show that a signal "
                            "is there — not what it says.", ACCENT)
            elif src.startswith("Sound card:"):
                self.status("Feed it USB-mode audio from a radio tuned to the Dial frequency, or from an SDR "
                            "program through a virtual audio cable.", MUTED)
            else:
                self.status("Sound card input needs: pip install sounddevice", ACCENT)

    def toggle_rx(self):
        self.stop_rx() if self.rx_mode else self.start_rx()

    def start_rx(self):
        self.save_cfg()
        src = self.vars["rx_source"].get()
        try:
            dial = float(self.vars["dial_mhz"].get()) * 1e6
        except ValueError:
            messagebox.showerror(APP_NAME, "Set a valid Dial frequency first.")
            return
        self.spec_rows = []
        try:
            if src == self.SRC_RTL:
                ok, err = rx_sources.rtl_available()
                if not ok:
                    raise RuntimeError("RTL-SDR library not available: " + err)
                gain = self.vars["rtl_gain"].get().strip().lower()
                gain = "auto" if gain in ("", "auto") else float(gain)
                self.rx_src = rx_sources.RtlSdrSource(self.on_audio, dial, ppm=int(self.num("rtl_ppm")), gain=gain)
                self.rx_src.start()
                self.rx_mode = "rtl"
                self.status(f"Listening on {dial/1e6:.3f} MHz USB with the RTL-SDR. Click a signal on the "
                            "waterfall to read it.", GOOD)
            elif src == self.SRC_LORA:
                if not self.ser:
                    messagebox.showinfo(APP_NAME, "Connect to the LoPSK board first (Radio panel).")
                    return
                self.rx_mode = "lora"
                self.lora_pts = []
                self.relisten_lora()
                self.status("LoRa board signal meter: the line jumps when someone transmits near your TX "
                            "frequency. Use an RTL-SDR or a radio to read what they send.", ACCENT)
            elif src in self.sound_devs:
                self.rx_src = rx_sources.SoundCardSource(self.on_audio, self.sound_devs[src])
                self.rx_src.start()
                self.rx_mode = "sound"
                self.status(f"Listening on {src[12:]} ({self.rx_src.rate} Hz). Click a signal on the waterfall "
                            "to read it.", GOOD)
            else:
                self.on_source_pick()
                return
        except Exception as e:
            self.rx_src = None
            self.rx_mode = None
            messagebox.showerror(APP_NAME, f"Couldn't start the receiver:\n{e}")
            return
        self.rx_btn.config(text="Stop listening")
        self.src_cb.state(["disabled"])

    def stop_rx(self):
        if self.rx_src:
            try:
                self.rx_src.stop()
            except Exception:
                pass
        if self.rx_mode == "lora" and self.ser and not self.tx:
            self.cmd("STOP")
        self.rx_src = None
        self.rx_mode = None
        self.rx_btn.config(text="Start listening")
        self.src_cb.state(["!disabled"])
        self.redraw_waterfall(force=True)

    def relisten_lora(self):
        if self.rx_mode == "lora" and self.ser and not self.tx:
            try:
                dial, audio, cal, rf = self.freq_plan()
            except ValueError:
                return
            self.cmd(f"LISTEN {rf:.0f}")

    def on_audio(self, block):
        """Called from the receiver's thread with 8 kHz audio."""
        try:
            self.audio_q.put_nowait(np.asarray(block, dtype=np.float64))
        except queue.Full:
            pass                                    # PC too busy: drop rather than lag

    def rx_worker(self):
        """Waterfall + main decoder + signal browser, off the UI thread."""
        main = None
        others = {}                  # id -> Channel
        hist = np.zeros(0)
        fft_n = 2048
        acc = np.zeros(0)
        last_scan = 0.0
        while True:
            try:
                block = self.audio_q.get(timeout=0.5)
            except queue.Empty:
                continue
            baud = self.baud()
            if self.retune_to is not None or main is None or main.baud != baud:
                f = self.retune_to if self.retune_to is not None else self.rx_freq
                self.retune_to = None
                main = psk31rx.Channel(f, baud)
                others = {}
                self.ui_q.put(("browser_reset", None))
            main.afc = self.vars_afc
            main.squelch = self.vars_sq
            txt = main.process(block)
            self.ui_q.put(("main", (txt, main.freq, main.quality)))
            for cid, ch in list(others.items()):
                ch.squelch = max(0.3, self.vars_sq)
                t = ch.process(block)
                if t:
                    self.ui_q.put(("btext", (cid, ch.freq, t)))
                if ch.time - ch.last_char_time > 45 and ch.time > 45:
                    del others[cid]
                    self.ui_q.put(("bdel", cid))
            # waterfall rows (~8 per second)
            acc = np.concatenate([acc, block])
            while len(acc) >= fft_n:
                seg = acc[:fft_n]
                acc = acc[fft_n // 2:]
                spec = np.abs(np.fft.rfft(seg * np.hanning(fft_n))) ** 2
                self.spec_rows.append(10 * np.log10(spec[: int(WF_MAX_HZ * fft_n / SR)] + 1e-12))
                if len(self.spec_rows) > 400:
                    del self.spec_rows[:100]
            # signal browser: look for PSK signals every 2 s
            hist = np.concatenate([hist, block])[-SR * 4:]
            if main.time - last_scan > 2 and len(hist) >= SR * 2:
                last_scan = main.time
                db, bin_hz = psk31rx.spectrum_db(hist, 16384 if len(hist) >= 16384 else len(hist))
                for f, snr in psk31rx.find_signals(db, bin_hz, baud, min_snr_db=5):
                    near = [c for c in others.values() if abs(c.freq - f) < 1.6 * baud]
                    if not near and abs(main.freq - f) > 1.6 * baud and len(others) < 10:
                        self.browser_seq += 1
                        others[self.browser_seq] = psk31rx.Channel(f, baud)
                        others[self.browser_seq].time = main.time
                        others[self.browser_seq].last_char_time = main.time

    # UI-side copies of settings the worker reads (tk vars aren't thread-safe)
    vars_afc = True
    vars_sq = 0.35

    def on_wf_click(self, event):
        w = max(self.wf.winfo_width(), 50)
        f = round(event.x / w * WF_MAX_HZ)
        self.set_rx_freq(f)

    def on_wf_right(self, event):
        w = max(self.wf.winfo_width(), 50)
        f = round(event.x / w * WF_MAX_HZ)
        self.vars["tx_hz"].set(str(int(f)))

    def set_rx_freq(self, f):
        f = max(100, min(WF_MAX_HZ - 50, f))
        self.rx_freq = f
        self.retune_to = f
        self.rx_txt.insert("end", f"\n[{f:.0f} Hz] ", "info")
        self.rx_txt.see("end")
        if self.vars["tx_follows_rx"].get() and not self.tx:
            self.vars["tx_hz"].set(str(int(round(f))))
        self.redraw_waterfall(force=True)

    def on_browser_pick(self, _=None):
        sel = self.tree.selection()
        if not sel:
            return
        try:
            f = float(self.tree.item(sel[0], "values")[0])
        except (ValueError, IndexError):
            return
        self.set_rx_freq(f)

    def decode_wav(self):
        path = filedialog.askopenfilename(title="PSK31 recording", filetypes=[("WAV audio", "*.wav")])
        if not path:
            return
        baud = self.baud()

        def run():
            try:
                audio = rx_sources.read_wav(path)
                res = psk31rx.decode_audio(audio, None, baud, squelch=0.3)
                self.ui_q.put(("wav", (os.path.basename(path), res, None)))
            except Exception as e:
                self.ui_q.put(("wav", (os.path.basename(path), [], str(e))))
        self.status(f"Decoding {os.path.basename(path)}…", MUTED)
        threading.Thread(target=run, daemon=True).start()

    def show_wav(self, name, res, err):
        if err:
            self.status(f"{name}: {err}", BAD)
            return
        self.status(f"{name}: {len(res)} signal(s) found.", GOOD if res else ACCENT)
        for f, text in res:
            self.rx_txt.insert("end", f"\n[{name} @ {f:.0f} Hz] ", "info")
            self.rx_txt.insert("end", text)
        self.rx_txt.see("end")

    # ---------------------------------------------------------- drawing
    def redraw_waterfall(self, force=False):
        c = self.wf
        w, h = max(c.winfo_width(), 50), int(c["height"])
        if self.rx_mode == "lora":
            self.draw_lora_meter(w, h)
            return
        rows = self.spec_rows[-h:]
        if not rows:
            if force:
                c.delete("all")
                c.create_text(w // 2, h // 2, text="waterfall appears while listening — click a signal to read it",
                              fill="#3a4651")
                self.draw_markers(w, h)
            return
        arr = np.array(rows)
        xs = np.linspace(0, arr.shape[1] - 1, w).astype(int)
        arr = arr[:, xs][::-1]
        floor = np.median(arr)
        norm = np.clip((arr - floor + 3) / 25.0, 0, 1)
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img[:norm.shape[0]] = waterfall_colors(norm)
        ppm = f"P6 {w} {h} 255\n".encode() + img.tobytes()
        self.wf_img = tk.PhotoImage(data=ppm, format="PPM")
        c.delete("all")
        c.create_image(0, 0, anchor="nw", image=self.wf_img)
        self.draw_markers(w, h)

    def draw_markers(self, w, h):
        c = self.wf
        bw = self.baud()
        x = self.rx_freq / WF_MAX_HZ * w
        half = bw / WF_MAX_HZ * w
        c.create_line(x - half, 0, x - half, h, fill=RXBLUE)
        c.create_line(x + half, 0, x + half, h, fill=RXBLUE)
        try:
            tx = float(self.vars["tx_hz"].get()) / WF_MAX_HZ * w
            c.create_line(tx, 0, tx, h, fill=TXRED, dash=(3, 3))
            c.create_text(tx + 3, 9, text="TX", anchor="w", fill=TXRED, font=("", 8, "bold"))
        except ValueError:
            pass
        for f in range(500, WF_MAX_HZ, 500):
            c.create_text(f / WF_MAX_HZ * w, h - 8, text=str(f), fill="#7d8a96", font=("", 8))

    def draw_lora_meter(self, w, h):
        c = self.wf
        c.delete("all")
        now = time.time()
        pts = [(t, v) for t, v in self.lora_pts if now - t < 30]
        self.lora_pts = pts
        lo, hi = -135.0, -60.0
        if pts:
            vals = [p[1] for p in pts]
            lo = min(lo, min(vals) - 2)
            hi = max(min(vals) + 40, max(vals) + 5)

        def y(val):
            return h - 4 - (val - lo) / (hi - lo) * (h - 8)
        for s in range(0, 31, 5):
            x = w - s / 30 * w
            c.create_line(x, 0, x, h, fill="#17212a")
            c.create_text(x - 14, h - 8, text=f"-{s}s", fill="#4b5661", font=("", 8))
        if len(pts) > 1:
            coords = []
            for t, val in pts[:: max(1, len(pts) // 800)]:
                coords += [w - (now - t) / 30 * w, y(val)]
            c.create_line(*coords, fill=ACCENT, width=1.5)
            floor = float(np.percentile([p[1] for p in pts], 20))
            c.create_line(0, y(floor), w, y(floor), fill="#3a4651", dash=(4, 3))
            c.create_text(w - 6, 10, text=f"{pts[-1][1]:.0f} dBm  ({pts[-1][1]-floor:+.0f} dB over noise)",
                          anchor="e", fill=FG, font=("", 10, "bold"))
        c.create_text(6, 10, text="LoRa board signal meter (last 30 s)", anchor="w", fill="#7d8a96", font=("", 9))

    # ---------------------------------------------------------- loops
    def poll(self):
        main_txt = []
        try:
            for _ in range(400):
                kind, val = self.ui_q.get_nowait()
                if kind == "line":
                    self.on_line(val)
                elif kind == "main":
                    txt, f, q = val
                    if txt:
                        main_txt.append(txt)
                    self.quality = q
                    self.cur_rx_freq = f
                elif kind == "btext":
                    self.browser_text(*val)
                elif kind == "bdel":
                    b = self.browser.pop(val, None)
                    if b and self.tree.exists(b["iid"]):
                        self.tree.delete(b["iid"])
                elif kind == "browser_reset":
                    self.tree.delete(*self.tree.get_children())
                    self.browser = {}
                elif kind == "wav":
                    self.show_wav(*val)
                elif kind == "serial_err":
                    self.status(f"Serial error: {val}", BAD)
                    self.disconnect()
                elif kind == "flashlog":
                    if "%" not in val or "100" in val:
                        self.status(val[:160], MUTED)
                elif kind == "flashdone":
                    if val == 0:
                        self.status("Flash complete. Press Connect.", GOOD)
                    elif val == -1:
                        self.status("esptool not found — run: pip install esptool", BAD)
                    else:
                        self.status("Flash failed. Hold the board's BOOT button while plugging it in, then retry.",
                                    BAD)
        except queue.Empty:
            pass
        if main_txt and not self.tx:              # like fldigi: receive text pauses while you transmit
            s = "".join(main_txt).replace("\r", "")
            my = self.vars["callsign"].get().strip().upper()
            self.rx_txt.insert("end", s)
            if my and len(my) >= 3:
                # highlight your callsign in received text
                start = self.rx_txt.index(f"end-{len(s) + 40}c")
                idx = start
                while True:
                    idx = self.rx_txt.search(my, idx, "end", nocase=True)
                    if not idx:
                        break
                    self.rx_txt.tag_add("me", idx, f"{idx}+{len(my)}c")
                    idx = f"{idx}+{len(my)}c"
            self.rx_txt.see("end")
            if int(self.rx_txt.index("end").split(".")[0]) > 3000:
                self.rx_txt.delete("1.0", "500.0")
        self.root.after(50, self.poll)

    def browser_text(self, cid, f, text):
        b = self.browser.get(cid)
        if not b:
            iid = self.tree.insert("", "end", values=(f"{f:.0f}", ""))
            b = self.browser[cid] = {"iid": iid, "text": ""}
        b["text"] = (b["text"] + text.replace("\r", "").replace("\n", " "))[-120:]
        if self.tree.exists(b["iid"]):
            self.tree.item(b["iid"], values=(f"{f:.0f}", b["text"][-60:]))

    def on_line(self, line):
        if not line:
            return
        t = self.tx
        if line.startswith("Q "):
            if t:
                try:
                    q = int(line[2:])
                    t["played"] = max(t["played"], t["sent"] - q)
                except ValueError:
                    pass
            return
        if line.startswith("R "):
            if self.rx_mode == "lora":
                try:
                    self.lora_pts.append((time.time(), int(line[2:]) / 10.0))
                except ValueError:
                    pass
            return
        if line.startswith("LOPSK"):
            ok = "NO-RADIO" not in line
            self.fw_version = line.split()[1] if len(line.split()) > 1 else "?"
            self.radio_lbl.config(text=("✓ " if ok else "⚠ ") + line.replace("LOPSK", "LoPSK firmware"),
                                  foreground=GOOD if ok else BAD)
            if not ok:
                self.status("Firmware runs but can't find the SX1262 — wrong board image?", BAD)
        elif line.startswith("LOFT8"):
            self.radio_lbl.config(text="This board has LoFT8 firmware — click Flash to load LoPSK.",
                                  foreground=ACCENT)
        elif line == "PSKON":
            if t:
                t["on"] = True
                t["started"] = time.time()
        elif line.startswith("PSKDONE"):
            if t:
                t["played"] = t["total"]
                self.pump_tx()
            self.finish_tx("Sent")
        elif line.startswith("STOPPED"):
            if t and "host" in line:
                self.finish_tx("Board stopped (lost contact with the app)")
        elif line.startswith("ERR"):
            self.status(f"Board: {line}", BAD)
            if t and "queue" not in line:
                self.finish_tx("Error")
        elif line.startswith("TUNEDONE"):
            self.tx_lbl.config(text="Tune finished.", fg=MUTED)
            self.relisten_lora()

    def tick(self):
        self.vars_afc = bool(self.vars["afc"].get())
        self.vars_sq = float(self.vars["squelch"].get()) / 100.0
        if self.tx:
            self.pump_tx()
            if not self.tx["on"] and time.time() - self.tx["started"] > 4:
                self.finish_tx("No answer from the board")
        if self.rx_mode and time.time() - self.wf_last > 0.15:
            self.wf_last = time.time()
            self.redraw_waterfall()
        if self.rx_mode in ("rtl", "sound"):
            q = max(0.0, self.quality)
            self.qbar["value"] = q * 100
            f = getattr(self, "cur_rx_freq", self.rx_freq)
            self.rxf_lbl.config(text=f"RX {f:7.1f} Hz")
            if self.rx_src is not None and getattr(self.rx_src, "error", None):
                self.status(f"Receiver stopped: {self.rx_src.error}", BAD)
                self.stop_rx()
        else:
            self.rxf_lbl.config(text=f"RX {self.rx_freq:7.1f} Hz")
            self.qbar["value"] = 0
        self.root.after(100, self.tick)

    def on_close(self):
        if self.tx:
            self.cmd("STOP")
        self.stop_rx()
        self.save_cfg()
        self.disconnect()
        self.root.destroy()


def waterfall_colors(norm):
    """0..1 array -> RGB uint8 (black, deep blue, cyan, yellow, white)."""
    stops = np.array([[5, 8, 11], [20, 40, 120], [0, 170, 200], [255, 210, 40], [255, 255, 255]], float)
    pos = norm * (len(stops) - 1)
    i = np.clip(pos.astype(int), 0, len(stops) - 2)
    f = (pos - i)[..., None]
    return (stops[i] * (1 - f) + stops[i + 1] * f).astype(np.uint8)


def main():
    root = tk.Tk()
    LoPSKApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
