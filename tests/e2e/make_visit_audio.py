"""Synthesize a spoken two-voice doctor conversation as a WAV file, using the
espeak-ng library bundled in the espeakng-loader package (no system install).
Used to exercise the Conversations flow where no human recording is at hand.

  python tests/e2e/make_visit_audio.py /tmp/visit.wav
"""
from __future__ import annotations

import ctypes
import os
import sys
import wave

LINES = [
    ("en-us+m3", "Good morning. I looked at the blood pressure readings you brought. They are still high, around one fifty over ninety."),
    ("en-us+f3", "We measure it every morning as you asked, doctor."),
    ("en-us+m3", "Good. I will start amlodipine five milligrams once daily in the morning, and keep the metformin as it is. "
                 "Please repeat the kidney test and the potassium in four weeks and come back to see me with the results."),
    ("en-us+f3", "Should we stop the aspirin?"),
    ("en-us+m3", "No, continue the aspirin seventy five milligrams. We will review it after the test results."),
]


def synthesize(path: str) -> float:
    import espeakng_loader

    base = os.path.dirname(espeakng_loader.__file__)
    lib = ctypes.CDLL(os.path.join(base, "libespeak-ng.so"))
    data = os.path.join(base, "espeak-ng-data")
    rate = lib.espeak_Initialize(2, 0, data.encode(), 0)  # synchronous output
    if rate <= 0:
        raise RuntimeError(f"espeak_Initialize returned {rate}")
    samples = bytearray()
    cb_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)

    def cb(wav, n, events):
        if n > 0 and wav:
            samples.extend(ctypes.string_at(wav, n * 2))
        return 0

    cb_ref = cb_type(cb)
    lib.espeak_SetSynthCallback(cb_ref)
    lib.espeak_SetVoiceByName.argtypes = [ctypes.c_char_p]
    lib.espeak_Synth.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
                                 ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    for voice, text in LINES:
        lib.espeak_SetVoiceByName(voice.encode())
        lib.espeak_SetParameter(1, 150, 0)
        raw = text.encode("utf-8")
        lib.espeak_Synth(raw, len(raw) + 1, 0, 0, 0, 1, None, None)
        samples.extend(b"\x00\x00" * int(rate * 0.6))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(samples))
    return len(samples) / 2 / rate


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "visit.wav"
    seconds = synthesize(out)
    print(f"{out}: {seconds:.1f} seconds")
