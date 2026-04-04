from gpiozero import Button, Device
from gpiozero.pins.lgpio import LGPIOFactory

Device.pin_factory = LGPIOFactory()


class ButtonsManager:
    """
    GPIO 5  → REC
    GPIO 26 → MUTE
    GPIO 6  → PLAY  (stop al final del ciclo)
    GPIO 13 → STOP  / hold 3s = salir al boot menu
    GPIO 25 → CONFIG (toggle entrar/confirmar)
    GPIO 9  → DOWN  (bajar valor en config)
    GPIO 22 → UP    (subir valor en config)
    GPIO 27 → PREV  (parámetro anterior en config)
    GPIO 17 → NEXT  (parámetro siguiente en config)
    """

    def __init__(self,
                 on_rec, on_mute, on_play, on_stop, on_exit,
                 on_config, on_save, on_down, on_up, on_prev, on_next):

        self.btn_rec    = Button(6,  pull_up=True, bounce_time=0.03)
        self.btn_mute   = Button(26, pull_up=True, bounce_time=0.03)
        self.btn_play   = Button(13,  pull_up=True, bounce_time=0.03)
        self.btn_stop   = Button(5, pull_up=True, bounce_time=0.03, hold_time=3.0)
        self.btn_config = Button(25, pull_up=True, bounce_time=0.03, hold_time=2.0)
        self.btn_down   = Button(9,  pull_up=True, bounce_time=0.03)
        self.btn_up     = Button(22, pull_up=True, bounce_time=0.03)
        self.btn_prev   = Button(27, pull_up=True, bounce_time=0.03)
        self.btn_next   = Button(17, pull_up=True, bounce_time=0.03)

        self.btn_rec.when_pressed    = on_rec
        self.btn_mute.when_pressed   = on_mute
        self.btn_play.when_pressed   = on_play
        self.btn_stop.when_pressed   = on_stop
        self.btn_stop.when_held      = on_exit
        self.btn_config.when_pressed = on_config
        self.btn_config.when_held    = on_save
        self.btn_down.when_pressed   = on_down
        self.btn_up.when_pressed     = on_up
        self.btn_prev.when_pressed   = on_prev
        self.btn_next.when_pressed   = on_next

    def close(self):
        for btn in (self.btn_rec, self.btn_mute, self.btn_play, self.btn_stop,
                    self.btn_config, self.btn_down, self.btn_up,
                    self.btn_prev, self.btn_next):
            try:
                btn.close()
            except Exception:
                pass
