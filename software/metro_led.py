"""
Metrónomo visual con dos LEDs.
    LED_ACCENT (pin 23) → beat 1 del compás
    LED_BEAT   (pin 24) → beats 2..N
"""

import threading
import time
from gpiozero import LED, Device
from gpiozero.pins.lgpio import LGPIOFactory

Device.pin_factory = LGPIOFactory()

PIN_ACCENT     = 23
PIN_BEAT       = 24
FLASH_DURATION = 0.04   # 40 ms de destello


class MetroLed:

    def __init__(self, bpm=120, beats_per_bar=4):
        self.bpm           = bpm
        self.beats_per_bar = beats_per_bar

        self._led_accent = LED(PIN_ACCENT)
        self._led_beat   = LED(PIN_BEAT)

        self._running = False
        self._thread  = None
        self._anchor  = None   # time.perf_counter() del beat 1 de arranque

    # ── API pública ───────────────────────────────────────────────────────

    @property
    def running(self):
        return self._running

    def start(self):
        if self._running:
            return
        self._running = True
        self._anchor  = time.perf_counter()
        self._thread  = threading.Thread(target=self._run, daemon=True, name='metro-led')
        self._thread.start()

    def stop(self):
        self._running = False
        self._led_accent.off()
        self._led_beat.off()

    def next_bar_time(self):
        """Tiempo absoluto (perf_counter) del inicio del siguiente compás."""
        if self._anchor is None:
            return time.perf_counter()
        bar_duration = (60.0 / self.bpm) * self.beats_per_bar
        elapsed      = time.perf_counter() - self._anchor
        next_bar     = (int(elapsed / bar_duration) + 1) * bar_duration
        return self._anchor + next_bar

    def close(self):
        self.stop()
        try:
            self._led_accent.close()
            self._led_beat.close()
        except Exception:
            pass

    # ── Thread interno ────────────────────────────────────────────────────

    def _wait_until(self, target):
        remaining = target - time.perf_counter()
        if remaining > 0.003:
            time.sleep(remaining - 0.002)
        while time.perf_counter() < target:
            pass

    def _run(self):
        interval = 60.0 / self.bpm
        bpb      = self.beats_per_bar
        i        = 0

        while self._running:
            beat_time   = self._anchor + i * interval
            beat_in_bar = (i % bpb) + 1

            if beat_in_bar == 1:
                self._led_accent.on()
                self._led_beat.off()
            else:
                self._led_accent.off()
                self._led_beat.on()

            self._wait_until(beat_time + FLASH_DURATION)
            self._led_accent.off()
            self._led_beat.off()

            self._wait_until(beat_time + interval)
            i += 1
