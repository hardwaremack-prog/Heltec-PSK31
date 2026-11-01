"""Drive the Heard Me tab with a pretend PSKReporter. Usage: test_heard.py <shot.png>"""
import io, os, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
os.environ["HOME"] = os.path.join(HERE, "home_heard"); os.makedirs(os.environ["HOME"], exist_ok=True)
import urllib.request, heardme
heardme.LOG_PATH = os.path.join(os.environ["HOME"], ".lopsk_heard.csv")
if os.path.exists(heardme.LOG_PATH): os.remove(heardme.LOG_PATH)
now = int(time.time())
STATIONS = [("W1AW", "FN31pr", "United States", -3, 600), ("VE3XYZ", "FN03", "Canada", 5, 60),
            ("N8DET", "EN82", "United States", 18, 200), ("K9CHI", "EN61", "United States", -9, 3000),
            ("W3PGH", "FN00", "United States", 1, 9000)]
XML = '<?xml version="1.0"?><receptionReports currentSeconds="%d">' % now + "".join(
    f'<receptionReport receiverCallsign="{c}" receiverLocator="{g}" senderCallsign="K8ABC" senderLocator="EN82fm" '
    f'frequency="{432101500 - i * 7}" flowStartSeconds="{now - ago}" mode="PSK31" receiverDXCC="{d}" sNR="{snr}"/>'
    for i, (c, g, d, snr, ago) in enumerate(STATIONS)) + "</receptionReports>"
calls = []
class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass
def fake_urlopen(req, timeout=30):
    calls.append(req.full_url); return Resp(XML.encode())
urllib.request.urlopen = fake_urlopen

import tkinter as tk, lopsk_app
lopsk_app.heardme.LOG_PATH = heardme.LOG_PATH
root = tk.Tk(); app = lopsk_app.LoPSKApp(root)
app.reporter.log_path = heardme.LOG_PATH
out = {}
def step(i):
    if i == 2:
        app.nb.select(1); app.vars["callsign"].set("K8ABC"); app.vars["my_grid"].set("en82fm"); app.heard_check()
    if i == 20:
        out["rows"] = [app.heard_tree.item(k, "values") for k in app.heard_tree.get_children()]
        out["sum"] = app.heard_sum.cget("text")
        app.heard_check()                 # second check within 5 min: same query -> cache, no network
    if i == 30:
        out["status2"] = app.heard_lbl.cget("text")
        app.vars["heard_window"].set("Last hour"); app.heard_check()   # different query too soon -> polite refusal
    if i == 40:
        out["status3"] = app.heard_lbl.cget("text")
        app.heard_show_log(); root.update()
        subprocess.run(["import", "-window", "root", sys.argv[1]])
        out["log"] = len(app.heard_tree.get_children())
        root.after(300, root.destroy); return
    root.after(100, step, i + 1)
root.after(600, step, 0); root.mainloop()
for r in out["rows"]: print(r)
print("SUM:", out["sum"]); print("2:", out["status2"][:90]); print("3:", out["status3"][:110])
print("network calls:", len(calls), calls[0])
print("log rows:", out["log"])
