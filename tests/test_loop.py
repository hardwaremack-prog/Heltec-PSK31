"""Encode -> (board or textbook) audio -> noise -> decode, and score it."""
import difflib, sys, os
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import psk31, psk31rx

MSG = "CQ CQ CQ de K8ABC K8ABC pse k. The quick brown fox jumps over the lazy dog 0123456789 73!"

def run(kind, snr_db, baud=31.25, tune_err=6.0, f=1200.0, seed=1, delay=0.0, search=False):
    bits = psk31.frame_bits(MSG)
    if kind == "board":
        a = psk31.board_audio(bits, f, baud, tick_delay_s=delay)
    else:
        a = psk31.standard_audio(bits, f, baud)
    a = np.concatenate([np.zeros(4000), a, np.zeros(4000)])
    rng = np.random.default_rng(seed)
    sig_p = np.mean(a[a != 0] ** 2)
    # SNR in 2500 Hz: noise power over 4000 Hz Nyquist band
    noise_p = sig_p / 10 ** (snr_db / 10) * (4000 / 2500)
    a = a + rng.normal(0, np.sqrt(noise_p), len(a))
    res = psk31rx.decode_audio(a, None if search else f + tune_err, baud)
    txt = max(res, key=lambda r: len(r[1]))[1] if res else ""
    m = difflib.SequenceMatcher(None, MSG, txt)
    ok = sum(b.size for b in m.get_matching_blocks())
    return ok / len(MSG), txt, res

if __name__ == "__main__":
    for baud in (31.25, 62.5):
        for kind in ("textbook", "board"):
            row = []
            for snr in (20, 0, -6, -9, -12):
                acc = np.mean([run(kind, snr, baud, seed=s)[0] for s in range(3)])
                row.append(f"{snr:>4} dB {acc*100:5.1f}%")
            print(f"PSK{int(baud*2/2.0)} {kind:9}", " | ".join(row))
    r = run("board", 0, search=True)
    print("auto-find:", [(round(f,1), t[:30]) for f, t in r[2]])
    print("sample @ -6 dB:", repr(run("board", -6)[1]))
