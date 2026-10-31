"""A pretend LoPSK board on a pseudo-terminal, for testing the app without
hardware. Speaks the firmware's serial protocol in real time and saves every
bit it 'transmits' to fake_board_bits.txt."""
import os, pty, sys, time, select, tty

master, slave = pty.openpty()
tty.setraw(slave)
print(os.ttyname(slave), flush=True)
out_path = sys.argv[1] if len(sys.argv) > 1 else "fake_board_bits.txt"

def say(s):
    os.write(master, (s + "\n").encode())

mode, queue, baud, sent, ending, tail = "idle", [], 31.25, [], False, 0
next_bit = 0.0; last_q = 0.0; buf = b""
say("LOPSK 1.0 FakeBoard SX1262")
while True:
    r, _, _ = select.select([master], [], [], 0.005)
    if r:
        buf += os.read(master, 4096)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            p = line.decode().strip().split()
            if not p: continue
            v = p[0].upper()
            if v == "HELLO": say("LOPSK 1.0 FakeBoard SX1262")
            elif v == "PWR": say(f"OK PWR {p[1]}")
            elif v == "STEER": say(f"OK STEER {p[1]}")
            elif v == "PSK":
                mode, queue, sent, ending, tail = "psk", [], [], False, 32
                baud = float(p[2]); next_bit = time.time(); say("PSKON")
                sys.stderr.write(f"PSK on {p[1]} Hz {baud} baud\n")
            elif v == "B": queue += list(p[1]); sys.stderr.write(f"B {len(p[1])}\n")
            elif v == "END": ending = True; say("OK END")
            elif v == "STOP":
                mode = "idle"; say("STOPPED"); open(out_path, "w").write("".join(sent))
            elif v == "STATUS": say(f"PSK {len(queue)}" if mode == "psk" else "IDLE")
            elif v == "TUNE": say("TUNEON")
            else: say("ERR unknown command")
    if mode == "psk":
        now = time.time()
        while now >= next_bit and mode == "psk":
            next_bit += 1 / baud
            if queue: sent.append(queue.pop(0))
            elif ending:
                if tail: tail -= 1; sent.append("1")
                else:
                    mode = "idle"; say("PSKDONE 0"); sys.stderr.write(f"done {len(sent)} bits\n"); open(out_path, "w").write("".join(sent))
            else: sent.append("0")
        if now - last_q > 0.25 and mode == "psk":
            last_q = now; say(f"Q {len(queue)}")
