import logging

import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.hw.base import DisplayImpl


class Waveshareoledlcd(DisplayImpl):
    def __init__(self, config):
        super(Waveshareoledlcd, self).__init__(config, 'waveshareoledlcd')

    def layout(self):
        fonts.setup(12, 10, 12, 70, 25, 9)
        self._layout['width'] = 320
        self._layout['height'] = 240
        self._layout['face'] = (0, 36)
        self._layout['name'] = (150, 36)
        self._layout['channel'] = (0, 0)
        self._layout['aps'] = (40, 0)
        self._layout['uptime'] = (240, 0)
        self._layout['line1'] = [0, 14, 320, 14]
        self._layout['line2'] = [0, 220, 320, 220]
        self._layout['friend_face'] = (0, 130)
        self._layout['friend_name'] = (40, 135)
        self._layout['shakes'] = (0, 220)

        right_edge = 319
        self._layout['mode_right_edge'] = (right_edge, 220)
        widest = max(fonts.Bold.getlength(m) for m in ('AUTO', 'MANU', 'WAIT', 'EVAL', 'LEARN'))
        self._layout['mode'] = (int(right_edge - widest), 220)
        self._layout['last_pwnd_max_x'] = int(right_edge - widest - 6)

        self._layout['status'] = {
            'pos': (150, 48),
            'font': fonts.status_font(fonts.Medium),
            'max': 20
        }

        return self._layout

    def initialize(self):
        logging.info("initializing Waveshare OLED/LCD hat")
        from pwnagotchi.ui.hw.libs.waveshare.oledlcd.ST7789 import ST7789
        self._display = ST7789(0,0,22,18)

    def render(self, canvas):
        self._display.display(canvas)

    def clear(self):
        self._display.clear()
