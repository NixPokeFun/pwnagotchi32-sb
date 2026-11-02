import logging
from PIL import Image

import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.hw.base import DisplayImpl


class WaveshareV4(DisplayImpl):
    def __init__(self, config):
        super(WaveshareV4, self).__init__(config, 'waveshare_4')
        self._display = None

    def layout(self):
        fonts.setup(10, 8, 10, 35, 25, 9)
        self._layout['width'] = 250
        self._layout['height'] = 122
        self._layout['face'] = (0, 40)
        self._layout['name'] = (5, 20)
        self._layout['channel'] = (0, 0)
        self._layout['aps'] = (28, 0)
        self._layout['uptime'] = (185, 0)
        self._layout['line1'] = [0, 14, 250, 14]
        self._layout['line2'] = [0, 108, 250, 108]
        self._layout['friend_face'] = (0, 92)
        self._layout['friend_name'] = (40, 94)
        self._layout['shakes'] = (0, 109)
        
        self._layout['mode_right_edge'] = (249, 109)
        widest = max(fonts.Bold.getlength(m) for m in ('AUTO', 'MANU', 'WAIT', 'EVAL', 'LEARN'))
        self._layout['mode'] = (int(249 - widest), 109)
        self._layout['last_pwnd_max_x'] = int(249 - widest - 6)

        self._layout['status'] = {
            'pos': (125, 20),
            'font': fonts.status_font(fonts.Medium),
            'max': 20
        }
        return self._layout

    def initialize(self):
        logging.info("initializing waveshare v4 display")
        from pwnagotchi.ui.hw.libs.waveshare.v4.epd2in13_V4 import EPD
        self._display = EPD()
        self._display.init()
        self._display.Clear(0xFF)

        try:
            # Set initial base image in RAM to reduce ghosting on partial updates
            blank_image = Image.new('1', (self._display.height, self._display.width), 255)
            buf = self._display.getbuffer(blank_image)
            self._display.displayPartBaseImage(buf)
        except Exception as err:
            logging.error(f"error initializing base image for waveshare v4: {err}")

    def render(self, canvas):
        buf = self._display.getbuffer(canvas)
        self._display.displayPartial(buf)

    def clear(self):
        self._display.Clear(0xFF)
