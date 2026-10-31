"""
PSK31 / PSK63 receiver for LoPSK (pure Python + numpy).

  Channel(freq_hz, baud)        one decoder on one audio frequency, with
                                automatic frequency tracking (AFC), symbol
                                timing recovery and a squelch
  find_signals(spectrum, ...)   picks out PSK signals on a waterfall row
  decode_audio(audio, ...)      whole-file helper (WAV decode, tests)

Audio is 8000 samples/s mono float. The demodulator is the classic design:
mix the signal down to 0 Hz, filter to 16 samples per symbol, matched-filter
with a 2-symbol cosine pulse, sample at the amplitude peak, and compare each
symbol's phase with the previous one: same phase = 1, reversal = 0.
"""
import math

import numpy as np

import psk31

SR = 8000


def lowpass_taps(cutoff, ntaps):
    """Windowed-sinc lowpass; cutoff as a fraction of the sample rate."""
    n = np.arange(ntaps) - (ntaps - 1) / 2
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.blackman(ntaps)
    return h / h.sum()


def resample(x, rate_in, rate_out):
    """Plain linear-interpolation resampler (for odd sound card rates)."""
    if rate_in == rate_out or len(x) == 0:
        return np.asarray(x, dtype=np.float64)
    n_out = int(len(x) * rate_out / rate_in)
    t = np.arange(n_out) * rate_in / rate_out
    return np.interp(t, np.arange(len(x)), x)


class Channel:
    """One PSK decoder locked to one signal."""

    def __init__(self, freq_hz, baud=31.25, afc=True, squelch=0.35):
        self.freq = float(freq_hz)        # current carrier estimate (Hz, audio)
        self.home = float(freq_hz)        # where it was tuned; AFC stays near it
        self.baud = baud
        self.afc = afc
        self.squelch = squelch
        self.decim = int(round(SR / (16 * baud)))      # 16 samples per symbol after this
        self.rate2 = SR / self.decim
        self.sps = 16
        self.taps1 = lowpass_taps(3.2 * baud / SR, 8 * self.decim + 1).astype(np.complex128)
        self.hist1 = np.zeros(len(self.taps1) - 1, dtype=np.complex128)
        self.dphase1 = 0                                   # decimation phase
        self.nco = 0.0                                     # mixer phase, cycles
        mf = np.hanning(2 * self.sps + 2)[1:-1]            # 2-symbol cosine pulse
        self.mf = mf / mf.sum()
        self.mfbuf = np.zeros(len(self.mf), dtype=np.complex128)
        self.amp = np.zeros(self.sps)                      # amplitude by sample position
        self.n = 0                                         # decimated sample counter
        self.next_sym = self.sps
        self.prev = 0j
        self.quality = 0.0                                 # 0..1, how clean the phase is
        self.level = 0.0
        self.vari = psk31.VaricodeDecoder()
        self.text_total = 0
        self.last_char_time = 0.0
        self.time = 0.0

    # ---------------------------------------------------------- processing
    def process(self, audio):
        """Feed audio (8 kHz). Returns decoded text (may be empty)."""
        audio = np.asarray(audio, dtype=np.float64)
        if len(audio) == 0:
            return ""
        self.time += len(audio) / SR
        idx = np.arange(len(audio))
        ph = self.nco + self.freq * idx / SR
        self.nco = (self.nco + self.freq * len(audio) / SR) % 1.0
        x = audio * np.exp(-2j * np.pi * ph)
        xx = np.concatenate([self.hist1, x])
        y = np.convolve(xx, self.taps1, mode="valid")
        self.hist1 = xx[-(len(self.taps1) - 1):]
        d = y[self.dphase1::self.decim]
        self.dphase1 = (self.dphase1 - len(y)) % self.decim
        out = []
        for s in d:
            c = self._sample(s)
            if c:
                out.append(c)
        return "".join(out)

    def _sample(self, s):
        # matched filter (running 2-symbol cosine window)
        self.mfbuf[:-1] = self.mfbuf[1:]
        self.mfbuf[-1] = s
        z = complex(np.dot(self.mfbuf, self.mf))
        a = abs(z)
        pos = self.n % self.sps
        self.amp[pos] = 0.97 * self.amp[pos] + 0.03 * a
        self.level = 0.995 * self.level + 0.005 * a
        self.n += 1
        if self.n < self.next_sym:
            return None
        # ---- symbol decision time
        self.next_sym += self.sps
        # timing: steer the sampling point toward the amplitude peak
        ang = np.angle(np.sum(self.amp * np.exp(2j * np.pi * np.arange(self.sps) / self.sps)))
        peak = (ang / (2 * np.pi) * self.sps) % self.sps
        here = (self.n - 1) % self.sps
        err = ((peak - here + self.sps / 2) % self.sps) - self.sps / 2
        if err > 0.6:
            self.next_sym += 1
        elif err < -0.6:
            self.next_sym -= 1
        # differential phase
        dz = z * self.prev.conjugate()
        self.prev = z
        if dz == 0:
            return None
        bit = 1 if dz.real > 0 else 0
        dphi = math.atan2(dz.imag, dz.real)
        q = math.cos(2 * dphi)                     # 1 = perfect 0/180 deg, ~0 = noise
        self.quality = 0.9 * self.quality + 0.1 * q
        if self.afc:
            # phase drift per symbol (mod 180 deg) = frequency error; pull toward it,
            # harder when locked, gently when searching, never far from where tuned
            e = 0.5 * math.atan2(math.sin(2 * dphi), math.cos(2 * dphi))
            g = 0.08 if self.quality > 0.3 else 0.04
            self.freq += g * e / (2 * math.pi) * self.baud
            lim = 0.8 * self.baud
            self.freq = min(max(self.freq, self.home - lim), self.home + lim)
        ch = self.vari.push(bit)
        if ch is not None and self.quality >= self.squelch:
            self.text_total += 1
            self.last_char_time = self.time
            return ch
        return None


# -------------------------------------------------------------- signal finding

def find_signals(power_db, bin_hz, baud=31.25, min_snr_db=6.0, fmin=200, fmax=2900,
                 spacing=None):
    """Return [(freq_hz, snr_db)] for PSK-looking bumps in a spectrum row (dB)."""
    p = 10 ** (np.asarray(power_db, dtype=np.float64) / 10)
    width = max(3, int(round(1.4 * baud / bin_hz)))
    k = np.hanning(width + 2)[1:-1]
    sm = np.convolve(p, k / k.sum(), mode="same")
    band = sm[int(fmin / bin_hz):int(fmax / bin_hz)]
    # noise floor, but never more than 35 dB under the strongest signal (so a
    # clean recording's filter leakage isn't mistaken for more stations)
    floor = max(np.median(band), band.max() * 10 ** -3.5) + 1e-20
    spacing = spacing or 2.2 * baud
    gap = int(spacing / bin_hz)
    out = []
    order = np.argsort(sm)[::-1]
    taken = np.zeros(len(sm), dtype=bool)
    for i in order:
        f = i * bin_hz
        if f < fmin or f > fmax or taken[i]:
            continue
        snr = 10 * math.log10(sm[i] / floor)
        if snr < min_snr_db:
            break
        lo, hi = max(0, i - gap), min(len(sm), i + gap + 1)
        if sm[i] < sm[lo:hi].max():
            continue
        taken[lo:hi] = True
        out.append((f, snr))
        if len(out) >= 12:
            break
    return out


def spectrum_db(audio, n=None):
    """Long FFT for signal finding: ~1 Hz bins."""
    n = n or min(len(audio), 8192)
    seg = np.asarray(audio[-n:], dtype=np.float64)
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
    return 10 * np.log10(spec + 1e-20), SR / len(seg)


def decode_audio(audio, freq=None, baud=31.25, squelch=0.0):
    """Decode a whole recording. With no freq, decodes every signal found.
    Returns [(freq_hz, text)]."""
    audio = np.asarray(audio, dtype=np.float64)
    if freq is None:
        db, bin_hz = spectrum_db(audio, min(len(audio), 32768))
        freqs = [f for f, _ in find_signals(db, bin_hz, baud)]
    else:
        freqs = [freq]
    out = []
    for f in freqs:
        ch = Channel(f, baud, squelch=squelch)
        txt = []
        for i in range(0, len(audio), 800):
            txt.append(ch.process(audio[i:i + 800]))
        out.append((ch.freq, "".join(txt)))
    return out
