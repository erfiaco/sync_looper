"""
Motor de grabación sincronizada al tempo.

Estados internos:
    IDLE → COUNTDOWN → RECORDING → RECORDED → PLAYING
    PLAYING → STOPPING → IDLE
    Cualquier estado → IDLE  (stop_all)

Timing por tiempo absoluto (sin drift acumulativo).
"""

import numpy as np
import sounddevice as sd
import threading
import time

SAMPLE_RATE    = 44100
CHANNELS       = 2
CLICK_DURATION = 0.025   # 25 ms
ACCENT_FREQ    = 880     # tiempo 1
BEAT_FREQ      = 440     # tiempos 2..N


def _generate_tone(freq, duration, sample_rate, amplitude=0.7):
    n    = int(sample_rate * duration)
    t    = np.linspace(0, duration, n, False)
    mono = amplitude * np.sin(2 * np.pi * freq * t).astype(np.float32)
    fade = n // 2
    mono[n - fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    return np.column_stack([mono, mono])   # stereo


class SyncEngine:

    def __init__(self, bpm=120, beats_per_bar=4, total_bars=4, click_enabled=False):
        self.bpm           = bpm
        self.beats_per_bar = beats_per_bar
        self.total_bars    = total_bars
        self.click_enabled = click_enabled

        self._accent = _generate_tone(ACCENT_FREQ, CLICK_DURATION, SAMPLE_RATE)
        self._beat   = _generate_tone(BEAT_FREQ,   CLICK_DURATION, SAMPLE_RATE)

        self._state      = 'IDLE'
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread     = None

        self._recorded_audio = None   # np.ndarray (samples, 2)
        self._record_buffer  = []
        self._grabando       = False
        self.muted           = False

        self._input_stream = None

        # Callbacks (asignados desde main.py)
        self.on_state_change = None   # fn(state: str)
        self.on_beat         = None   # fn(beat_in_bar, beats_per_bar, bar, total_bars)
                                      #    bar=0 durante countdown

    # ── Propiedades ───────────────────────────────────────────────────────

    @property
    def state(self):
        return self._state

    @property
    def beat_duration(self):
        return 60.0 / self.bpm

    @property
    def total_duration(self):
        return self.total_bars * self.beats_per_bar * self.beat_duration

    def has_clip(self):
        return self._recorded_audio is not None

    # ── API pública ───────────────────────────────────────────────────────

    def start_listening(self):
        """Stream de entrada siempre activo (listening permanente)."""
        if self._input_stream is not None:
            return
        self._input_stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            callback=self._input_callback,
            blocksize=256,
            latency='low',
            dtype='float32'
        )
        self._input_stream.start()

    def start_recording(self):
        """Para lo que haya en curso y lanza countdown → recording."""
        self._stop_and_join()
        self._stop_event.clear()
        self._recorded_audio = None
        self._thread = threading.Thread(
            target=self._run_countdown_then_record,
            daemon=True, name='sync-rec'
        )
        self._thread.start()

    def start_playing(self):
        """Lanza el loop de playback del clip grabado."""
        if self._recorded_audio is None:
            return
        self._stop_and_join()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_playback,
            daemon=True, name='sync-play'
        )
        self._thread.start()

    def schedule_stop(self):
        """Programa parada al final del ciclo actual (solo desde PLAYING)."""
        with self._state_lock:
            if self._state == 'PLAYING':
                self._state = 'STOPPING'
        if self.on_state_change:
            self.on_state_change('STOPPING')

    def stop_all(self):
        """Para todo inmediatamente → IDLE."""
        self._grabando = False
        self._stop_event.set()
        try:
            sd.stop()
        except Exception:
            pass
        self._stop_and_join()
        self._set_state('IDLE')

    def toggle_mute(self):
        self.muted = not self.muted

    def close(self):
        self.stop_all()
        if self._input_stream:
            try:
                self._input_stream.stop()
                self._input_stream.close()
            except Exception:
                pass
            self._input_stream = None

    # ── Helpers internos ──────────────────────────────────────────────────

    def _set_state(self, state):
        with self._state_lock:
            self._state = state
        if self.on_state_change:
            try:
                self.on_state_change(state)
            except Exception as e:
                print(f"[SyncEngine] on_state_change error: {e}")

    def _stop_and_join(self):
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            self._thread.join(timeout=3.0)

    def _wait_until(self, target):
        """Sleep + busy-wait final para precisión de ~1 ms."""
        remaining = target - time.perf_counter()
        if remaining > 0.003:
            self._stop_event.wait(timeout=remaining - 0.002)
        while time.perf_counter() < target:
            pass

    def _fire_beat(self, beat_in_bar, bar):
        if self.on_beat:
            try:
                self.on_beat(beat_in_bar, self.beats_per_bar, bar, self.total_bars)
            except Exception as e:
                print(f"[SyncEngine] on_beat error: {e}")

    # ── Callback de audio input ───────────────────────────────────────────

    def _input_callback(self, indata, frames, time_info, status):
        data = np.zeros_like(indata, dtype=np.float32) if self.muted \
               else indata.copy().astype(np.float32)
        if self._grabando:
            self._record_buffer.append(data)

    # ── Thread: countdown + recording ────────────────────────────────────

    def _run_countdown_then_record(self):
        interval = self.beat_duration
        bpb      = self.beats_per_bar

        # === COUNTDOWN: solo si click_enabled ===
        if self.click_enabled:
            self._set_state('COUNTDOWN')
            anchor = time.perf_counter()

            for i in range(bpb):
                if self._stop_event.is_set():
                    self._set_state('IDLE')
                    return

                beat_in_bar = i + 1
                sound = self._accent if beat_in_bar == 1 else self._beat
                try:
                    sd.play(sound, SAMPLE_RATE)
                except Exception as e:
                    print(f"[SyncEngine] click error: {e}")

                #self._fire_beat(beat_in_bar, 0) Intentamos que no haya conflicto con i2C ya no se actualizara OLED 
                self._wait_until(anchor + (i + 1) * interval)

            if self._stop_event.is_set():
                self._set_state('IDLE')
                return

        # === RECORDING ===
        self._set_state('RECORDING')
        self._record_buffer = []
        self._grabando = True

        total_beats = self.total_bars * bpb
        anchor = time.perf_counter()

        for i in range(total_beats):
            if self._stop_event.is_set():
                break

            beat_in_bar = (i % bpb) + 1
            bar_num     = (i // bpb) + 1

            # Click durante grabación: solo si click_enabled
            if self.click_enabled:
                sound = self._accent if beat_in_bar == 1 else self._beat
                try:
                    sd.play(sound, SAMPLE_RATE)
                except Exception as e:
                    print(f"[SyncEngine] click error: {e}")

            #self._fire_beat(beat_in_bar, bar_num) Intentamos que no haya conflicto con i2C ya no se actualizara OLED 
            self._wait_until(anchor + (i + 1) * interval)

        self._grabando = False

        if self._stop_event.is_set():
            self._record_buffer = []
            self._set_state('IDLE')
            return

        # Consolidar buffer
        if self._record_buffer:
            raw = np.concatenate(self._record_buffer)
            # Recortar la latencia del input stream para alinear con el anchor
            try:
                latency_samples = int(self._input_stream.latency * SAMPLE_RATE)
                print(f"[SyncEngine] Recortando {latency_samples} samples de latencia")
                self._recorded_audio = raw[latency_samples:]
            except Exception:
                self._recorded_audio = raw
        else:
            self._recorded_audio = None
        
        self._record_buffer = []

        # Notificar → main.py arrancará el playback
        self._set_state('RECORDED')

    def save_clip(self, path):
        """Guarda el clip grabado en disco como WAV. Devuelve True si ok."""
        import soundfile as sf
        if self._recorded_audio is None:
            print("[SyncEngine] No hay clip para guardar.")
            return False
        try:
            sf.write(path, self._recorded_audio, SAMPLE_RATE)
            print(f"[SyncEngine] Clip guardado: {path}")
            return True
        except Exception as e:
            print(f"[SyncEngine] Error guardando: {e}")
            return False


    # ── Thread: playback ─────────────────────────────────────────────────


    def _run_playback(self):
        audio        = self._recorded_audio
        total_frames = len(audio)
        position     = [0]

        self._set_state('PLAYING')

        def _callback(outdata, frames, time_info, status):
            available = total_frames - position[0]

            if available >= frames:
                outdata[:] = audio[position[0]:position[0] + frames]
                position[0] += frames
            else:
                # Final del ciclo — copiar lo que queda
                outdata[:available] = audio[position[0]:]
                remaining = frames - available

                with self._state_lock:
                    should_stop = (self._state == 'STOPPING')

                if should_stop:
                    outdata[available:] = 0
                    raise sd.CallbackStop()
                else:
                    # Loop sin gap: continuar desde sample 0
                    outdata[available:] = audio[:remaining]
                    position[0] = remaining

        try:
            with sd.OutputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                callback=_callback,
                blocksize=256,
                latency='low',
                dtype='float32'
            ) as stream:
                while not self._stop_event.is_set() and stream.active:
                    time.sleep(0.05)
        except Exception as e:
            print(f"[SyncEngine] playback error: {e}")
    
        try:
            sd.stop()
        except Exception:
            pass

        self._set_state('IDLE')
