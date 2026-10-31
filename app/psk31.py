"""
PSK31 / PSK63 encoder for LoPSK.

  varicode_bits(text)  -> "0"/"1" string ready to send (characters joined by "00")
  standard_audio(...)  -> textbook BPSK31 audio (cosine-shaped reversals), used for
                          "Play on speakers", Save WAV and testing
  board_audio(...)     -> exactly what the LoPSK board puts on the air: a constant-
                          power carrier whose phase is steered 180 degrees by tiny
                          frequency nudges, computed tick-for-tick the same way the
                          firmware does it (see PhaseSteer below)

Run directly to print the bits for a message:  python3 psk31.py "CQ CQ de K8ABC"
"""
import math
import sys

import numpy as np

# ---------------------------------------------------------------- Varicode
# G3PLX's PSK31 Varicode (public spec, 1998). Index = ASCII code 0..127.
# No code contains "00", so two zeros in a row always mark a character gap.
VARICODE = [
    "1010101011", "1011011011", "1011101101", "1101110111", "1011101011", "1101011111",
    "1011101111", "1011111101", "1011111111", "11101111", "11101", "1101101111",
    "1011011101", "11111", "1101110101", "1110101011", "1011110111", "1011110101",
    "1110101101", "1110101111", "1101011011", "1101101011", "1101101101", "1101010111",
    "1101111011", "1101111101", "1110110111", "1101010101", "1101011101", "1110111011",
    "1011111011", "1101111111",
    # 32 space ! " # $ % & ' ( ) * + , - . /
    "1", "111111111", "101011111", "111110101", "111011011", "1011010101", "1010111011",
    "101111111", "11111011", "11110111", "101101111", "111011111", "1110101", "110101",
    "1010111", "110101111",
    # 48 0-9
    "10110111", "10111101", "11101101", "11111111", "101110111", "101011011", "101101011",
    "110101101", "110101011", "110110111",
    # 58 : ; < = > ? @
    "11110101", "110111101", "111101101", "1010101", "111010111", "1010101111", "1010111101",
    # 65 A-Z
    "1111101", "11101011", "10101101", "10110101", "1110111", "11011011", "11111101",
    "101010101", "1111111", "111111101", "101111101", "11010111", "10111011", "11011101",
    "10101011", "11010101", "111011101", "10101111", "1101111", "1101101", "101010111",
    "110110101", "101011101", "101110101", "101111011", "1010101101",
    # 91 [ \ ] ^ _ `
    "111110111", "111101111", "111111011", "1010111111", "101101101", "1011011111",
    # 97 a-z
    "1011", "1011111", "101111", "101101", "11", "111101", "1011011", "101011", "1101",
    "111101011", "10111111", "11011", "111011", "1111", "111", "111111", "110111111",
    "10101", "10111", "101", "110111", "1111011", "1101011", "11011111", "1011101",
    "111010101",
    # 123 { | } ~ DEL
    "1010110111", "110111011", "1010110101", "1011010111", "1110110101",
]
assert len(VARICODE) == 128
DECODE = {code: chr(i) for i, code in enumerate(VARICODE)}


def varicode_bits(text):
    """Text -> bit string. Unknown characters become '?'. Newlines go out as CR LF."""
    out = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            out.append(VARICODE[13] + "00" + VARICODE[10] + "00")
            continue
        c = ord(ch)
        if c > 127:
            c = ord("?")
        out.append(VARICODE[c] + "00")
    return "".join(out)


class VaricodeDecoder:
    """Feed bits one at a time; returns a character when one completes."""

    def __init__(self):
        self.reg = ""
        self.zeros = 0

    def push(self, bit):
        if bit:
            if self.zeros == 1 and self.reg:
                self.reg += "0"                 # a single zero is part of the code
            self.zeros = 0
            self.reg += "1"
            if len(self.reg) > 12:              # too long to be a character: noise
                self.reg = ""
            return None
        self.zeros += 1
        if self.zeros == 2 and self.reg:        # "00" ends the character
            code, self.reg = self.reg, ""
            return DECODE.get(code)
        return None

    def push_bits(self, bits):
        out = []
        for b in bits:
            c = self.push(1 if b in (1, "1", True) else 0)
            if c is not None:
                out.append(c)
        return "".join(out)


# ---------------------------------------------------------------- framing

PREAMBLE_BITS = 32       # 1 s of reversals ("idle"): lets receivers lock on
POSTAMBLE_BITS = 32      # 1 s of steady carrier: standard PSK31 sign-off


def frame_bits(text):
    """Whole transmission: preamble reversals, text, steady-carrier tail."""
    return "0" * PREAMBLE_BITS + varicode_bits(text) + "1" * POSTAMBLE_BITS


# ---------------------------------------------------------------- textbook audio

def standard_audio(bits, audio_hz=1000.0, baud=31.25, sr=8000, amp=0.5):
    """Classic BPSK31: a 0 bit = phase reversal with a cosine amplitude dip."""
    sps = int(round(sr / baud))
    n = len(bits) * sps
    t = np.arange(n) / sr
    env = np.ones(n)
    sign = np.ones(n)
    s = 1.0
    for i, b in enumerate(bits):
        a, z = i * sps, (i + 1) * sps
        if b == "0":
            # amplitude goes s -> 0 -> -s across the symbol (cosine)
            env[a:z] = np.cos(np.pi * np.arange(sps) / sps)
            sign[a:z] = s
            s = -s
        else:
            sign[a:z] = s
    return (amp * env * sign * np.cos(2 * np.pi * audio_hz * t)).astype(np.float32)


# ---------------------------------------------------------------- board signal

STEP_HZ = 32000000.0 / 33554432.0   # SX1262 synthesizer step, ~0.9537 Hz


class PhaseSteer:
    """
    The SX1262 can't flip its carrier phase directly, but its synthesizer can move
    in 0.95 Hz steps without breaking phase. Running 1 Hz high for 1 s adds one
    full cycle of phase, so a short, smooth nudge that adds exactly half a cycle
    is a 180 degree phase reversal.

    Every tick (1 ms) we pick the synthesizer offset, in whole PLL steps, that
    brings the accumulated phase closest to a raised-cosine path from the old
    phase to the new one over half a symbol, centred on the symbol boundary.
    Rounding error is carried forward, so the phase lands within 0.001 cycle.
    Reversals alternate up/down so the spectrum stays symmetric, like normal PSK31.

    This class is the reference: the firmware runs the same arithmetic in C.
    """

    def __init__(self, baud=31.25, tick_us=1000):
        self.tick_us = tick_us
        self.ticks_per_sym = int(round(1e6 / baud / tick_us))      # 32 for PSK31, 16 for PSK63
        self.trans = self.ticks_per_sym // 2                       # transition length in ticks
        self.phase = 0.0          # cycles actually accumulated (offset part only)
        self.target = 0.0         # settled phase, cycles
        self.direction = 1

    def boundary_offsets(self, reverse):
        """Offsets for the `trans` ticks around one symbol boundary."""
        dt = self.tick_us * 1e-6
        start = self.target
        if reverse:
            self.target = start + 0.5 * self.direction
            self.direction = -self.direction
        out = []
        for k in range(1, self.trans + 1):
            u = k / self.trans
            want = start + (self.target - start) * 0.5 * (1 - math.cos(math.pi * u))
            steps = int(round((want - self.phase) / (STEP_HZ * dt)))
            self.phase += steps * STEP_HZ * dt
            out.append(steps)
        return out

    def ticks_for_bits(self, bits):
        """Whole transmission -> list of per-tick offsets (PLL steps).
        Each bit starts with its boundary move (a reversal for 0, nothing for 1)
        lasting half a symbol, then holds steady for the other half."""
        ticks = []
        for b in bits:
            ticks += self.boundary_offsets(b == "0")
            ticks += [0] * (self.ticks_per_sym - self.trans)
        return ticks


def board_audio(bits, audio_hz=1000.0, baud=31.25, sr=8000, amp=0.5, tick_us=1000,
                tick_delay_s=0.0):
    """Audio a USB receiver hears from the LoPSK board (offsets applied per tick,
    phase-continuous, quantized to whole PLL steps exactly like the firmware)."""
    ps = PhaseSteer(baud, tick_us)
    ticks = ps.ticks_for_bits(bits)
    dt = tick_us * 1e-6
    spt = sr * dt
    n = int(len(ticks) * spt)
    t = np.arange(n) / sr
    tick_idx = np.minimum(((t - tick_delay_s) / dt).astype(int), len(ticks) - 1)
    tick_idx = np.maximum(tick_idx, 0)
    freq = audio_hz + np.asarray(ticks, dtype=np.float64)[tick_idx] * STEP_HZ
    phase = 2 * np.pi * np.cumsum(freq) / sr
    return (amp * np.cos(phase)).astype(np.float32)


if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]) or "CQ CQ de K8ABC K8ABC pse k"
    b = varicode_bits(msg)
    print(b)
    print(f"{len(b)} bits, {len(b) / 31.25:.1f} s at PSK31")
