from gpiozero import Button, Device
from gpiozero.pins.lgpio import LGPIOFactory
import threading
import time

Device.pin_factory = LGPIOFactory()

HOLD_INITIAL_DELAY = 0.30
HOLD_REPEAT_MS     = 0.15
HOLD_FAST_THRESH   = 2.0
DELTA_SLOW         = 1
DELTA_FAST         = 10


class ButtonsManager:

    def __init__(self,
                 on_rec, on_mute, on_play, on_stop, on_exit,
                 on_config, on_save, on_down, on_up, on_prev, on_next):

        self.btn_rec    = Button(6,  pull_up=True, bounce_time=0.03)
        self.btn_mute   = Button(26, pull_up=True, bounce_time=0.03)
        self.btn_play   = Button(13, pull_up=True, bounce_time=0.03)
        self.btn_stop   = Button(5,  pull_up=True, bounce_time=0.03, hold_time=3.0)
        self.btn_config = Button(25, pull_up=True, bounce_time=0.03, hold_time=2.0)
        self.btn_down   = Button(9,  pull_up=True, bounce_time=0.03)
        self.btn_up     = Button(22, pull_up=True, bounce_time=0.03)
        self.btn_prev   = Button(27, pull_up=True, bounce_time=0.03)
        self.btn_next   = Button(17, pull_up=True, bounce_time=0.03)

        self._on_down = on_down
        self._on_up   = on_up
        self._dn_held = False
        self._up_held = False

        self.btn_rec.when_pressed    = on_rec
        self.btn_mute.when_pressed   = on_mute
        self.btn_play.when_pressed   = on_play
        self.btn_stop.when_pressed   = on_stop
        self.btn_stop.when_held      = on_exit
        self.btn_config.when_pressed = on_config
        self.btn_config.when_held    = on_save
        self.btn_down.when_pressed   = self._on_dn_press
        self.btn_down.when_released  = self._on_dn_release
        self.btn_up.when_pressed     = self._on_up_press
        self.btn_up.when_released    = self._on_up_release
        self.btn_prev.when_pressed   = on_prev
        self.btn_next.when_pressed   = on_next

    def _call(self, fn, *args):
        if fn:
            try:
                fn(*args)
            except Exception as e:
                print(f"[Buttons] error: {e}")

    def _on_dn_press(self):
        self._call(self._on_down)
        self._dn_held = True
        threading.Thread(target=self._hold_worker,
                         args=(lambda: self._dn_held, -1),
                         daemon=True).start()

    def _on_dn_release(self):
        self._dn_held = False

    def _on_up_press(self):
        self._call(self._on_up)
        self._up_held = True
        threading.Thread(target=self._hold_worker,
                         args=(lambda: self._up_held, +1),
                         daemon=True).start()

    def _on_up_release(self):
        self._up_held = False

    def _hold_worker(self, is_held_fn, direction):
        t_start = time.monotonic()
        end_time = t_start + HOLD_INITIAL_DELAY
        while time.monotonic() < end_time:
            if not is_held_fn():
                return
            time.sleep(0.01)
        while is_held_fn():
            elapsed = time.monotonic() - t_start
            if elapsed >= HOLD_FAST_THRESH:
                for _ in range(DELTA_FAST):
                    if direction < 0:
                        self._call(self._on_down)
                    else:
                        self._call(self._on_up)
            else:
                if direction < 0:
                    self._call(self._on_down)
                else:
                    self._call(self._on_up)
            time.sleep(HOLD_REPEAT_MS)

    def close(self):
        self._dn_held = False
        self._up_held = False
        for btn in (self.btn_rec, self.btn_mute, self.btn_play, self.btn_stop,
                    self.btn_config, self.btn_down, self.btn_up,
                    self.btn_prev, self.btn_next):
            try:
                btn.close()
            except Exception:
                pass
