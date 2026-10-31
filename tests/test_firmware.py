"""Turn the firmware simulator's frequency log into receiver audio and decode it
with LoPSK's decoder and (optionally) csdr, the decoder OpenWebRX uses."""
import difflib, os, subprocess, sys
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
import psk31, psk31rx

MSG = "CQ CQ CQ de K8ABC K8ABC pse k. The quick brown fox jumps over the lazy dog 0123456789 73!"
SR, F = 8000, 1000.0

def log_to_audio(path):
    ev, on, base = [], None, None
    for line in open(path):
        p = line.split()
        t = float(p[0]) / 1000
        if p[1] == "F":
            frf = int(p[2]); base = base or frf
            ev.append((t, frf))
        elif p[1] == "RF" and p[2] == "CW" and on is None:
            on = t
        elif p[1] == "RF" and p[2] == "OFF":
            off = t
    n = int((off - on) * SR)
    t = on + np.arange(n) / SR
    times = np.array([e[0] for e in ev]); frfs = np.array([e[1] for e in ev])
    cur = frfs[np.maximum(np.searchsorted(times, t, side="right") - 1, 0)]
    freq = F + (cur - base) * psk31.STEP_HZ
    return np.cos(2 * np.pi * np.cumsum(freq) / SR)

def score(txt):
    m = difflib.SequenceMatcher(None, MSG, txt)
    return sum(b.size for b in m.get_matching_blocks()) / len(MSG)

def csdr(audio, exe):
    n = np.arange(len(audio)); bb = audio * np.exp(-2j * np.pi * F * n / SR)
    raw = np.empty(2 * len(bb), np.float32); raw[0::2] = bb.real; raw[1::2] = bb.imag
    b = 31.25 / SR
    cmd = (f"{exe} bandpass_fir_fft_cc -{b} {b} {b} | {exe} simple_agc_cc 0.001 0.5 | "
           f"{exe} timing_recovery_cc GARDNER 256 0.5 2 --add_q | {exe} dbpsk_decoder_c_u8 | "
           f"{exe} psk31_varicode_decoder_u8_u8")
    r = subprocess.run(cmd, shell=True, input=raw.tobytes() + bytes(8 * 16000), capture_output=True, timeout=60)
    return r.stdout.decode("latin-1")

if __name__ == "__main__":
    a = log_to_audio(sys.argv[1])
    exe = sys.argv[2] if len(sys.argv) > 2 else None
    print(f"{len(a)/SR:.1f} s of audio from the firmware log")
    for snr in (30, 0, -6, -9):
        rng = np.random.default_rng(5)
        x = np.concatenate([np.zeros(SR), a, np.zeros(SR)])
        x = x + rng.normal(0, np.sqrt(0.5 / 10 ** (snr / 10) * 4000 / 2500), len(x))
        mine = psk31rx.decode_audio(x, F + 4.0)[0][1]
        line = f"{snr:>4} dB  LoPSK decoder {score(mine)*100:5.1f}%"
        if exe:
            c = csdr(x, exe)
            line += f" | csdr {score(c)*100:5.1f}%  {c.strip()[:50]!r}"
        print(line, "" if exe else repr(mine.strip()[:50]))
