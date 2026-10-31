"""Drive the real LoPSK app under a virtual display with a pretend board and
pretend receiver audio. Usage: python3 test_app.py <pty> <bits_out> <shot.png>"""
import os, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
os.environ["HOME"] = os.path.join(HERE, "home")            # fresh config
os.makedirs(os.environ["HOME"], exist_ok=True)
import numpy as np
import tkinter as tk
import lopsk_app, psk31

PTY, BITS_OUT, SHOT = sys.argv[1:4]
root = tk.Tk()
app = lopsk_app.LoPSKApp(root)
app.port_cb["values"] = [PTY]; app.port_cb.set(PTY)

RX1 = "CQ CQ de W8XYZ W8XYZ pse k. Testing from Lansing, name Bob. "
RX2 = "K8ABC de N8QRP 599 tnx fer call 73 "
rng = np.random.default_rng(0)
a1 = psk31.board_audio(psk31.frame_bits(RX1 * 2), 1500.0)
a2 = psk31.standard_audio(psk31.frame_bits(RX2 * 3), 2200.0)
n = max(len(a1), len(a2))
mix = np.zeros(n); mix[:len(a1)] += a1 * 0.5; mix[:len(a2)] += a2 * 0.3
mix += rng.normal(0, 0.15, n)
feed = {"pos": 0}
results = {}

def step(i):
    t = i * 0.1
    if i == 5: app.connect()
    if i == 30:
        app.vars["callsign"].set("k8abc"); app.vars["their_call"].set("W8XYZ"); app.vars["their_name"].set("Bob")
        app.vars["my_name"].set("Mack"); app.vars["qth"].set("Clinton Twp"); app.vars["licensed"].set(True)
        app.rx_mode = "sound"                     # pretend a sound card is running
        app.on_wf_click(type("E", (), {"x": int(1495 / 3000 * app.wf.winfo_width())})())
    if 30 <= i:
        # 0.1 s of audio per 0.1 s tick, but 3x speed so the test is shorter
        for _ in range(4):
            p = feed["pos"]
            if p < n:
                app.on_audio(mix[p:p + 800]); feed["pos"] = p + 800
    if i == 140:
        app.insert_macro(lopsk_app.MACROS[1][1])   # Answer
        app.send()
    if i == 150:
        root.update(); subprocess.run(["import", "-window", "root", SHOT])
    if i >= 300 and app.tx is None or i > 1200:
        results["rx"] = app.rx_txt.get("1.0", "end")
        results["browser"] = [app.tree.item(k, "values") for k in app.tree.get_children()]
        results["txbox"] = app.tx_txt.get("1.0", "end")
        results["txlbl"] = app.tx_lbl.cget("text"); results["st"] = app.rx_lbl.cget("text")
        root.after(800, root.destroy)
        return
    root.after(100, step, i + 1)

root.after(500, step, 0)
root.mainloop()
print("RX TEXT:\n" + results["rx"])
print("BROWSER:", results["browser"]); print("TXLBL:", results["txlbl"], "|", results["st"])
time.sleep(0.5)
bits = open(BITS_OUT).read() if os.path.exists(BITS_OUT) else ""
from psk31 import VaricodeDecoder
sent = VaricodeDecoder().push_bits(bits)
print("BOARD SENT:", repr(sent))
want = app.macro_text(lopsk_app.MACROS[1][1])
print("EXPECTED  :", repr(want.replace("\n", "\r\n")))
print("PASS TX" if sent.strip() == want.replace("\n", "\r\n").strip() else "FAIL TX")
print("PASS RX main" if "W8XYZ pse k" in results["rx"] else "FAIL RX main")
print("PASS browser" if any("N8QRP" in str(b) for b in results["browser"]) else "FAIL browser")
