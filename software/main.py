#!/usr/bin/env python3
"""
Sync Looper — state machine principal.

Flujo normal:
    IDLE → (REC) → COUNTDOWN → RECORDING → PLAYING → (PLAY) → STOPPING → IDLE
    Cualquier estado → (STOP) → IDLE
    IDLE/PLAYING + (CONFIG) → CONFIG → (CONFIG) → IDLE/PLAYING
"""

import signal
import time
import os
import sounddevice as sd
from threading import Event

from .sync_engine    import SyncEngine
from .oled_display   import OledDisplay
from .buttons_manager import ButtonsManager

sd.default.device = 0   # Forzar AudioInjector

BPM_MIN,    BPM_MAX    = 20,  300
COMPAS_MIN, COMPAS_MAX = 1,   12
BARS_MIN,   BARS_MAX   = 1,   64


class SyncLooper:

    def __init__(self):
        # Parámetros configurables
        self.bpm           = 120
        self.beats_per_bar = 4
        self.total_bars    = 4
        self.click_enabled = False   # por defecto sin click

        # Estado de la app (CONFIG es propio de main; el resto los maneja el engine)
        self._app_state  = 'IDLE'
        self._config_idx = 0        # 0=TEMPO 1=COMPAS 2=BARS
        self._last_beat  = None     # (beat_in_bar, bar) — para el OLED
        self._muted      = False
        self._loops_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "loops"
        )
        os.makedirs(self._loops_dir, exist_ok=True)
#        self._loops_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "loops")
#        os.makedirs(self._loops_dir, exist_ok=True)
        self._pre_config_state = 'IDLE'  # estado previo al entrar en CONFIG

        self.engine  = SyncEngine(self.bpm, self.beats_per_bar, self.total_bars)
        self.display = OledDisplay()

        self.engine.on_state_change = self._on_engine_state
        #self.engine.on_beat         = self._on_beat Intentamos que no haya conflicto con i2C ya no se actualizara OLED 
        self.engine.start_listening()

        self.exit_event = Event()
        signal.signal(signal.SIGINT,  self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Delay antes de inicializar GPIO (boot menu necesita liberarlos)
        time.sleep(1.2)

        
        self.buttons = ButtonsManager(
            on_rec=self._on_rec,
            on_mute=self._on_mute,
            on_play=self._on_play,
            on_stop=self._on_stop,
            on_exit=self._on_exit,
            on_config=self._on_config,
            on_save=self._on_save,
            on_down=self._on_down,
            on_up=self._on_up,
            on_prev=self._on_prev,
            on_next=self._on_next,
        )

        self._update_display()

    # ── Callbacks del engine ──────────────────────────────────────────────

    def _on_engine_state(self, state):
        print(f"[Main] Engine → {state}")

        if state == 'RECORDED':
            # Clip listo: arrancar playback automáticamente
            import threading
            threading.Thread(target=self.engine.start_playing, daemon=True).start()
            return

        self._app_state = state
        self._update_display()

#    def _on_beat(self, beat_in_bar, beats_per_bar, bar, total_bars): Intentamos que no haya conflicto con i2C ya no se actualizara OLED 
#        self._last_beat = (beat_in_bar, bar)
#        self._update_display()
        
    def _on_save(self):
        """GPIO 25 hold 2s → guardar clip en disco."""
        if self._app_state == 'CONFIG':
            return
        if not self.engine.has_clip():
            print("[Main] Nada que guardar.")
            return

        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename  = f"loop_{timestamp}.wav"
        path      = os.path.join(self._loops_dir, filename)

        ok = self.engine.save_clip(path)

        # Feedback visual breve en el OLED
        try:
            msg = f"SAVED!" if ok else "ERROR!"
            img = __import__('PIL.Image', fromlist=['Image']).new("1", (128, 64))
            from PIL import ImageDraw
            d = ImageDraw.Draw(img)
            d.text((20, 22), msg,     font=self.display.font_big,   fill=255)
            d.text((5,  44), filename, font=self.display.font_small, fill=255)
            self.display.device.display(img)
            import time; time.sleep(1.5)
        except Exception:
            pass

        self._update_display()

    # ── Botones ───────────────────────────────────────────────────────────

    def _on_rec(self):
        if self._app_state == 'CONFIG':
            return
        print(f"[Main] REC (estado={self._app_state})")
        self._last_beat = None
        # Nueva grabación desde cualquier estado (borra el clip anterior)
        self.engine.start_recording()

    def _on_mute(self):
        if self._app_state == 'CONFIG':
            return
        self._muted = not self._muted
        self.engine.muted = self._muted
        self._update_display()

    def _on_play(self):
        # Solo tiene efecto en PLAYING: programa stop al final del ciclo
        if self._app_state == 'PLAYING':
            self.engine.schedule_stop()

    def _on_stop(self):
        if self._app_state == 'CONFIG':
            return
        self.engine.stop_all()   # → IDLE a través de _on_engine_state

    def _on_exit(self):
        """STOP mantenido 3s → salir al boot menu."""
        print("[Main] Saliendo al boot menu...")
        self._cleanup()

    def _on_config(self):
        """Toggle CONFIG. Solo disponible en IDLE o PLAYING."""
        if self._app_state == 'CONFIG':
            # Confirmar y salir de config
            self._apply_config()
            self._app_state = self._pre_config_state
            self._update_display()
        elif self._app_state in ('IDLE', 'PLAYING'):
            self._pre_config_state = self._app_state
            self._app_state  = 'CONFIG'
            self._config_idx = 0
            self._update_display()

    def _on_down(self):
        if self._app_state == 'CONFIG':
            self._adjust_param(-1)

    def _on_up(self):
        if self._app_state == 'CONFIG':
            self._adjust_param(+1)

    def _on_prev(self):
        if self._app_state == 'CONFIG':
            self._config_idx = (self._config_idx - 1) % 4 
            self._update_display()

    def _on_next(self):
        if self._app_state == 'CONFIG':
            self._config_idx = (self._config_idx + 1) % 4
            self._update_display()

    # ── Config helpers ────────────────────────────────────────────────────

    def _adjust_param(self, delta):
        if self._config_idx == 0:
            self.bpm = max(BPM_MIN, min(BPM_MAX, self.bpm + delta))
        elif self._config_idx == 1:
            self.beats_per_bar = max(COMPAS_MIN, min(COMPAS_MAX, self.beats_per_bar + delta))
        elif self._config_idx == 2:
            self.total_bars = max(BARS_MIN, min(BARS_MAX, self.total_bars + delta))
        elif self._config_idx == 3:   # ← nuevo
            self.click_enabled = not self.click_enabled   # toggle ON/OFF
        self._update_display()
        
    def _apply_config(self):
        self.engine.bpm           = self.bpm
        self.engine.beats_per_bar = self.beats_per_bar
        self.engine.total_bars    = self.total_bars
        self.engine.click_enabled = self.click_enabled
        print(f"[Main] Config → {self.bpm}BPM {self.beats_per_bar}/4 {self.total_bars}bars")

    # ── Display ───────────────────────────────────────────────────────────

    def _update_display(self):
        try:
            if self._app_state == 'CONFIG':
                self.display.show_config(
                    params={
                        'bpm':          self.bpm,
                        'beats_per_bar': self.beats_per_bar,
                        'total_bars':   self.total_bars,
                        'click':        self.click_enabled,
                    },
                    current_idx=self._config_idx
                )
            else:
                beat_in_bar, bar = self._last_beat if self._last_beat else (None, None)
                self.display.show_main(
                    state         = self._app_state,
                    bpm           = self.bpm,
                    beats_per_bar = self.beats_per_bar,
                    total_bars    = self.total_bars,
                    muted         = self._muted,
                    beat_in_bar   = beat_in_bar,
                    bar           = bar,
                )
        except Exception as e:
            print(f"[Main] Display error: {e}")

    # ── Run / Cleanup ─────────────────────────────────────────────────────

    def _signal_handler(self, signum, frame):
        print("\n[Main] Señal recibida, saliendo...")
        self.exit_event.set()

    def _cleanup(self):
        print("[Main] Limpiando...")
        self.engine.close()
        self.display.clear()
        self.buttons.close()
        import subprocess
        subprocess.Popen(["/usr/bin/python3",
                          "/home/Javo/Proyects/boot_menu/boot_menu.py"])
        os._exit(0)

    def run(self):
        print("=== Sync Looper ===")
        print(f"Defaults: {self.bpm}BPM  {self.beats_per_bar}/4  {self.total_bars} bars")
        try:
            while not self.exit_event.is_set():
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            self._cleanup()


if __name__ == "__main__":
    SyncLooper().run()
