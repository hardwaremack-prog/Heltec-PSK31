"""
Receive-side audio sources for LoPSK. Each delivers 8 kHz mono audio blocks
(about 0.1 s each) to a callback.

  SoundCardSource  any audio input: a radio in USB mode, or an SDR program
                   (SDR++, SDR#, SDRangel) through a virtual audio cable.
  RtlSdrSource     an RTL-SDR USB dongle driven directly: tunes the dial
                   frequency and demodulates upper sideband, no other software.
"""
import threading
import time

import numpy as np

from psk31rx import SR, lowpass_taps, resample


class SoundCardSource:
    name = "Sound card"

    def __init__(self, on_audio, device=None):
        import sounddevice as sd             # imported here so the app runs without it
        self.sd = sd
        self.on_audio = on_audio
        self.device = device
        self.stream = None

    @staticmethod
    def list_devices():
        try:
            import sounddevice as sd
        except Exception:
            return []
        out = []
        try:
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0:
                    out.append((i, d["name"]))
        except Exception:
            pass
        return out

    def start(self):
        info = self.sd.query_devices(self.device, "input")
        rate = int(info["default_samplerate"])
        for r in (48000, 8000, 16000, 24000, 32000, 44100, rate):
            try:
                self.sd.check_input_settings(device=self.device, samplerate=r, channels=1)
                rate = r
                break
            except Exception:
                continue
        self.rate = rate
        d = rate // SR if rate % SR == 0 else 0
        if d > 1:
            self._taps = lowpass_taps(0.45 / d, 24 * d + 1)
            self._hist = np.zeros(len(self._taps) - 1)
            self._phase = 0
        self._d = d

        def cb(indata, frames, t, status):
            self.on_audio(self._to8k(indata[:, 0].astype(np.float64)))

        self.stream = self.sd.InputStream(device=self.device, channels=1, samplerate=rate,
                                          blocksize=int(rate * 0.1), callback=cb)
        self.stream.start()

    def _to8k(self, x):
        if self.rate == SR:
            return x
        if self._d > 1:                       # stateful decimation, keeps filter history
            xx = np.concatenate([self._hist, x])
            y = np.convolve(xx, self._taps, mode="valid")
            self._hist = xx[-(len(self._taps) - 1):]
            out = y[self._phase::self._d]
            self._phase = (self._phase - len(y)) % self._d
            return out
        return resample(x, self.rate, SR)

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None


class _Fir:
    """Stateful complex FIR filter + decimator."""

    def __init__(self, taps, decim):
        self.taps = taps.astype(np.complex128)
        self.decim = decim
        self.hist = np.zeros(len(taps) - 1, dtype=np.complex128)
        self.phase = 0

    def process(self, x):
        xx = np.concatenate([self.hist, x])
        y = np.convolve(xx, self.taps, mode="valid")
        self.hist = xx[-(len(self.taps) - 1):]
        out = y[self.phase::self.decim]
        self.phase = (self.phase - len(y)) % self.decim
        return out


class UsbDemod:
    """Complex IQ at fs (dial frequency at +offset_hz) -> 8 kHz USB audio (0-3 kHz)."""

    def __init__(self, fs=240000, offset_hz=60000):
        self.fs = fs
        self.decim = fs // SR
        self.offset = offset_hz
        self.n = 0
        self.n2 = 0
        self.stage1 = _Fir(lowpass_taps(3000 / fs, 8 * self.decim + 1), self.decim)
        self.stage2 = _Fir(lowpass_taps(1450 / SR, 161), 1)

    def process(self, iq):
        iq = np.asarray(iq, dtype=np.complex128)
        # put the middle of the 0-3 kHz audio passband (dial + 1500 Hz) at 0 Hz,
        # so one symmetric lowpass keeps just the upper sideband
        idx = self.n + np.arange(len(iq))
        self.n = (self.n + len(iq)) % (self.fs * 1000)
        lo = np.exp(-2j * np.pi * (self.offset + 1500.0) * idx / self.fs)
        y = self.stage1.process(iq * lo)
        y = self.stage2.process(y)
        idx2 = self.n2 + np.arange(len(y))
        self.n2 = (self.n2 + len(y)) % (SR * 1000)
        return np.real(y * np.exp(2j * np.pi * 1500.0 * idx2 / SR))


class RtlSdrSource:
    name = "RTL-SDR"

    def __init__(self, on_audio, dial_hz, ppm=0, gain="auto"):
        from rtlsdr import RtlSdr                # pyrtlsdr; needs librtlsdr installed
        self.RtlSdr = RtlSdr
        self.on_audio = on_audio
        self.dial_hz = dial_hz
        self.ppm = int(ppm)
        self.gain = gain
        self.sdr = None
        self.running = False
        self.error = None
        self.demod = UsbDemod()

    def start(self):
        sdr = self.RtlSdr()
        sdr.sample_rate = self.demod.fs
        if self.ppm:
            sdr.freq_correction = self.ppm
        sdr.center_freq = self.dial_hz - self.demod.offset
        sdr.gain = self.gain
        self.sdr = sdr
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        n = 24 * 1024                            # ~0.1 s per read
        while self.running:
            try:
                iq = self.sdr.read_samples(n)
            except Exception as e:
                self.error = str(e)
                self.running = False
                break
            self.on_audio(self.demod.process(iq) * 4.0)

    def stop(self):
        self.running = False
        time.sleep(0.2)
        if self.sdr:
            try:
                self.sdr.close()
            except Exception:
                pass
            self.sdr = None


def rtl_available():
    try:
        from rtlsdr import RtlSdr  # noqa: F401
        return True, ""
    except Exception as e:
        return False, str(e)


def read_wav(path):
    """WAV file -> 8 kHz mono float."""
    import wave
    with wave.open(path, "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float64) / 32768
    elif width == 1:
        x = (np.frombuffer(raw, np.uint8).astype(np.float64) - 128) / 128
    elif width == 4:
        x = np.frombuffer(raw, "<i4").astype(np.float64) / 2 ** 31
    else:
        raise ValueError("unsupported WAV sample size")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if rate != SR:
        if rate % SR == 0:
            d = rate // SR
            x = np.convolve(x, lowpass_taps(0.45 / d, 24 * d + 1), mode="same")[::d]
        else:
            x = resample(x, rate, SR)
    return x


def write_wav(path, audio, rate=SR):
    import wave
    a = np.clip(np.asarray(audio) * 32767, -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(a.tobytes())
