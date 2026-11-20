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
        self._record_array   = None   # buffer pre-alocado para la grabación
        self._record_pos     = 0      # frames escritos hasta ahora
        self._grabando       = False
        self.muted           = False

        self._overdub_array    = None  # buffer overdub pre-alocado
        self._overdub_pos      = 0
        self._overdub_pending  = False  # esperando inicio de ciclo
        self._is_overdubbing   = False  # grabando overdub ahora mismo
        self._next_overdub     = False  # encolar otro overdub al terminar

        self._record_start_at  = None  # sync con metro: anchor del beat 1

        self._click_audio = None  # tono a mezclar en el output
        self._click_pos   = 0

        self._input_stream = None
        self._output_stream    = None
        self._playback_audio   = None
        self._playback_pos     = [0]
        self._is_playing_audio = False
        self._loop_stop        = False

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

        # OutputStream permanente — siempre corriendo, evita latencia de init
        self._output_stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            callback=self._output_callback,
            blocksize=256,
            latency='low',
            dtype='float32'
        )
        self._output_stream.start()
        print(f"[SyncEngine] Input  latency: {self._input_stream.latency*1000:.1f} ms")
        print(f"[SyncEngine] Output latency: {self._output_stream.latency*1000:.1f} ms")

    def start_recording(self, start_at=None):
        """Para lo que haya en curso y lanza countdown → recording.
        start_at: tiempo absoluto (perf_counter) para sincronizar con metro LED."""
        self._stop_and_join()
        print(f"[SyncEngine] start_recording: stop_event={self._stop_event.is_set()} antes de clear")
        self._stop_event.clear()
        print(f"[SyncEngine] start_recording: stop_event={self._stop_event.is_set()} después de clear")
        self._recorded_audio  = None
        self._record_start_at = start_at
        self._thread = threading.Thread(
            target=self._run_countdown_then_record,
            daemon=True, name='sync-rec'
        )
        self._thread.start()

    def start_playing(self):
        print(f"[SyncEngine] start_playing: state={self._state}, recorded={'None' if self._recorded_audio is None else len(self._recorded_audio)}")
        if self._recorded_audio is None:
            return
        with self._state_lock:
            if self._state == 'IDLE':
                print("[SyncEngine] start_playing abortado: stop_all ya activo")
                return
        self._playback_audio      = self._recorded_audio
        self._playback_pos[0]     = 0
        self._loop_stop           = False
        self._is_playing_audio    = True
        self._set_state('PLAYING')

    def schedule_overdub(self):
        """Programa un overdub al inicio del siguiente ciclo de reproducción.
        Si ya hay un overdub en curso, encola uno más."""
        with self._state_lock:
            state = self._state
        if state == 'PLAYING':
            self._overdub_array   = np.zeros_like(self._recorded_audio)
            self._overdub_pos     = 0
            self._overdub_pending = True
        elif state == 'OVERDUBBING':
            self._next_overdub = True

    def schedule_stop(self):
        with self._state_lock:
            if self._state == 'PLAYING':
                self._state = 'STOPPING'
                self._loop_stop = True
        if self.on_state_change:
            self.on_state_change('STOPPING')

    def stop_all(self):
        self._grabando         = False
        self._is_playing_audio = False
        self._loop_stop        = False
        self._overdub_pending  = False
        self._is_overdubbing   = False
        self._next_overdub     = False
        self._overdub_array    = None
        self._stop_event.set()
        self._set_state('IDLE')  # inmediato, sin bloquear

    def schedule_click(self, is_accent=True):
        """Mezcla un click en el siguiente bloque del output. Thread-safe."""
        self._click_audio = self._accent if is_accent else self._beat
        self._click_pos   = 0

    def toggle_mute(self):
        self.muted = not self.muted

    def close(self):
        self.stop_all()
        for stream in (self._input_stream, self._output_stream):
            if stream:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
        self._input_stream  = None
        self._output_stream = None

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
        if self._grabando and self._record_array is not None:
            end = self._record_pos + frames
            if end <= len(self._record_array):
                if self.muted:
                    self._record_array[self._record_pos:end] = 0
                else:
                    self._record_array[self._record_pos:end] = indata[:frames]
                self._record_pos = end
        elif self._is_overdubbing and self._overdub_array is not None:
            end = self._overdub_pos + frames
            if end <= len(self._overdub_array):
                if self.muted:
                    self._overdub_array[self._overdub_pos:end] = 0
                else:
                    self._overdub_array[self._overdub_pos:end] = indata[:frames]
                self._overdub_pos = end
    
    
    def _output_callback(self, outdata, frames, time_info, status):
        outdata[:] = 0

        # — Playback / loop —
        if self._is_playing_audio and self._playback_audio is not None:
            audio        = self._playback_audio
            total_frames = len(audio)
            pos          = self._playback_pos[0]
            available    = total_frames - pos

            if available >= frames:
                outdata[:] = audio[pos:pos + frames]
                self._playback_pos[0] += frames
            else:
                outdata[:available] = audio[pos:]
                remaining = frames - available

                if self._loop_stop:
                    self._is_playing_audio = False
                    self._loop_stop = False
                    threading.Thread(
                        target=self._set_state, args=('IDLE',), daemon=True
                    ).start()
                else:
                    outdata[available:] = audio[:remaining]
                    self._playback_pos[0] = remaining

                    if self._is_overdubbing:
                        self._is_overdubbing = False
                        threading.Thread(target=self._finish_overdub, daemon=True).start()
                    elif self._overdub_pending:
                        self._overdub_pending = False
                        self._is_overdubbing  = True
                        threading.Thread(
                            target=self._set_state, args=('OVERDUBBING',), daemon=True
                        ).start()

        # — Click (mezcla encima, funciona en cualquier estado) —
        if self._click_audio is not None:
            click     = self._click_audio
            available = len(click) - self._click_pos
            mix_len   = min(frames, available)
            outdata[:mix_len] += click[self._click_pos:self._click_pos + mix_len]
            self._click_pos += mix_len
            if self._click_pos >= len(click):
                self._click_audio = None
                self._click_pos   = 0

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
                    return

                beat_in_bar = i + 1
                self.schedule_click(beat_in_bar == 1)

                #self._fire_beat(beat_in_bar, 0) no se actualizara OLED
                self._wait_until(anchor + (i + 1) * interval)

            if self._stop_event.is_set():
                return

        # === RECORDING ===
        # Pre-alojar buffer durante la espera para no perder tiempo al inicio
        total_samples = int(self.total_bars * bpb * interval * SAMPLE_RATE) + SAMPLE_RATE
        self._record_array = np.zeros((total_samples, CHANNELS), dtype=np.float32)
        self._record_pos   = 0

        if self._record_start_at is not None:
            # Esperar al beat 1 del metro LED
            self._set_state('COUNTDOWN')
            self._wait_until(self._record_start_at)
            anchor = self._record_start_at
            self._record_start_at = None
            if self._stop_event.is_set():
                self._record_array = None
                self._set_state('IDLE')
                return
        else:
            anchor = time.perf_counter()

        # Número exacto de muestras que debe tener el loop
        expected_samples = int(self.total_bars * bpb * interval * SAMPLE_RATE)

        # _grabando = True ANTES del set_state para que el OLED write no recorte el inicio
        self._grabando = True
        self._set_state('RECORDING')

        total_beats = self.total_bars * bpb

        for i in range(total_beats):
            if self._stop_event.is_set():
                break

            beat_in_bar = (i % bpb) + 1
            bar_num     = (i // bpb) + 1

            # Click durante grabación: solo si click_enabled
            if self.click_enabled:
                self.schedule_click(beat_in_bar == 1)

            #self._fire_beat(beat_in_bar, bar_num) Intentamos que no haya conflicto con i2C ya no se actualizara OLED
            self._wait_until(anchor + (i + 1) * interval)

        self._grabando = False
        print(f"[SyncEngine] grabación terminada: record_pos={self._record_pos}, stop={self._stop_event.is_set()}")

        if self._stop_event.is_set():
            self._record_array = None
            return

        # Tomar exactamente expected_samples del buffer (sin compensación de latencia):
        # la latencia del input ya está "dentro" del anchor — si grabamos desde anchor
        # el buffer tiene justo lo que necesitamos sin recortar.
        raw = self._record_array[:self._record_pos]
        self._record_array = None
        if len(raw) >= expected_samples:
            self._recorded_audio = raw[:expected_samples].copy()
        else:
            self._recorded_audio = raw.copy()

        # Notificar → main.py arrancará el playback
        self._set_state('RECORDED')

    def _finish_overdub(self):
        # Mezclar overdub en el clip grabado
        od_len  = self._overdub_pos
        rec     = self._recorded_audio
        mixed   = rec.copy()
        mix_len = min(od_len, len(mixed))
        mixed[:mix_len] += self._overdub_array[:mix_len]
        np.clip(mixed, -1.0, 1.0, out=mixed)

        self._recorded_audio = mixed
        self._playback_audio = mixed  # el output_callback ve el nuevo clip sin interrupción

        if self._next_overdub:
            self._next_overdub  = False
            self._overdub_array = np.zeros_like(self._recorded_audio)
            self._overdub_pos   = 0
            self._is_overdubbing = True
            self._set_state('OVERDUBBING')
        else:
            self._overdub_array = None
            self._set_state('PLAYING')

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


