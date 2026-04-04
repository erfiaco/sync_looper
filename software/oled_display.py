from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306
from PIL import Image, ImageDraw, ImageFont


class OledDisplay:
    """
    Pantalla principal:
    ┌────────────────────────────┐
    │ 120BPM 4/4 4bars     MUTE │  ← pequeño (params)
    │────────────────────────────│
    │ ● REC                      │  ← grande (estado)
    │ Beat 3/4  Bar 2/4          │  ← progreso (solo en REC/COUNT)
    └────────────────────────────┘

    Pantalla config:
    ┌────────────────────────────┐
    │ ─── CONFIG ───             │
    │ > TEMPO:  120 BPM          │
    │   COMPAS: 4/4              │
    │   BARS:   4                │
    └────────────────────────────┘
    """

    STATE_LABELS = {
        'IDLE':      '■  IDLE',
        'COUNTDOWN': '◎  COUNT',
        'RECORDING': '●  REC',
        'PLAYING':   '▶  PLAY',
        'STOPPING':  '◑  ENDING...',
    }

    def __init__(self, port=1, address=0x3C, width=128, height=64):
        self.serial = i2c(port=port, address=address)
        self.device = ssd1306(self.serial, width=width, height=height)
        self.W, self.H = self.device.size

        try:
            self.font_big   = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
            self.font_med   = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
            self.font_small = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 9)
        except Exception:
            self.font_big = self.font_med = self.font_small = ImageFont.load_default()

    def clear(self):
        self.device.display(Image.new("1", (self.W, self.H)))

    def show_main(self, state, bpm, beats_per_bar, total_bars,
                  muted=False, beat_in_bar=None, bar=None):
        img = Image.new("1", (self.W, self.H))
        d   = ImageDraw.Draw(img)

        # Línea 1: parámetros + mute (fuente pequeña)
        params = f"{bpm}BPM {beats_per_bar}/4 {total_bars}bars"
        d.text((0, 0), params, font=self.font_small, fill=255)
        if muted:
            d.text((96, 0), "MUTE", font=self.font_small, fill=255)

        # Separador
        d.line([(0, 11), (self.W, 11)], fill=255)

        # Estado grande
        label = self.STATE_LABELS.get(state, state)
        d.text((0, 15), label, font=self.font_big, fill=255)

        # Progreso de beat/bar (solo durante COUNTDOWN y RECORDING)
        if beat_in_bar is not None and state in ('COUNTDOWN', 'RECORDING'):
            if state == 'COUNTDOWN':
                progress = f"Count {beat_in_bar}/{beats_per_bar}"
            else:
                progress = f"Beat {beat_in_bar}/{beats_per_bar}  Bar {bar}/{total_bars}"
            d.text((0, 50), progress, font=self.font_small, fill=255)

        self.device.display(img)

    def show_config(self, params, current_idx):
        """
        params: {'bpm': int, 'beats_per_bar': int, 'total_bars': int}
        current_idx: 0=TEMPO, 1=COMPAS, 2=BARS
        """
        img = Image.new("1", (self.W, self.H))
        d   = ImageDraw.Draw(img)

        d.text((0, 0), "--- CONFIG ---", font=self.font_med, fill=255)

        items = [
            ("TEMPO",  f"{params['bpm']} BPM"),
            ("COMPAS", f"{params['beats_per_bar']}/4"),
            ("BARS",   str(params['total_bars'])),
            ("CLICK",  "ON" if params['click'] else "OFF"),
        ]

        for i, (name, value) in enumerate(items):
            y      = 14 + i * 12          # ← paso de 16 a 12px, caben los 4
            prefix = ">" if i == current_idx else " "
            font   = self.font_small      # ← todos en small, no hay espacio para med
            # El item seleccionado lleva un rectángulo de fondo para destacar
            if i == current_idx:
                d.rectangle([(0, y - 1), (127, y + 10)], fill=255)
                d.text((0, y), f"{prefix} {name}: {value}", font=font, fill=0)
            else:
                d.text((0, y), f"{prefix} {name}: {value}", font=font, fill=255)
        
        self.device.display(img)
