import logging

import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.hw.base import DisplayImpl

# Native resolution of every Waveshare 2.13" e-paper panel (V1-V4) when it is
# used upright. The vendor libraries take an image of exactly this size as is,
# while the landscape drivers hand them a 250x122 canvas that the library has
# to rotate first.
PORTRAIT_WIDTH = 122
PORTRAIT_HEIGHT = 250


class _Waveshare213Portrait(DisplayImpl):
    """Shared portrait layout and render path for the 2.13" panels V1-V4.

    Subclasses only differ in how the vendor EPD class is created and
    refreshed, mirroring the matching landscape driver
    (waveshare2in13*.py) one to one so the refresh behaviour stays the same.
    """

    def __init__(self, config, name):
        super(_Waveshare213Portrait, self).__init__(config, name)
        self._display = None

    def layout(self):
        # a 35px face is up to 126px wide, which does not fit the 122px
        # portrait canvas, so the face font is smaller than in landscape
        fonts.setup(10, 8, 10, 30, 25, 9)
        width = PORTRAIT_WIDTH
        height = PORTRAIT_HEIGHT

        self._layout['width'] = width
        self._layout['height'] = height

        # top: uptime, a rule, the name and the status text under it
        self._layout['uptime'] = (0, 0)
        self._layout['line1'] = [0, 14, width - 1, 14]
        self._layout['name'] = (4, 22)
        self._layout['status'] = {
            'pos': (4, 42),
            'font': fonts.status_font(fonts.Medium),
            'max': 19,     # characters per line that fit in 'width' at 6px each
            'lines': 3,    # the face starts at y=84, only ~40px below the status
            'width': 114   # status is centered in this column
        }

        # middle: the face, centered on the full width, and the closest peer
        self._layout['face'] = (0, 84)
        self._layout['face_width'] = width
        self._layout['friend_face'] = (0, 128)
        self._layout['friend_name'] = (0, 141)
        self._layout['friend_name_max_x'] = width

        # y=154..208 is intentionally left free for plugin elements
        # (control, memtemp, gps, ...), see the portrait-mode plugin

        # bottom: channel and APs, a rule, handshakes, mode, last handshake
        self._layout['channel'] = (0, 210)
        self._layout['aps'] = (40, 210)
        self._layout['line2'] = [0, 224, width - 1, 224]
        self._layout['shakes'] = (0, 226)

        right_edge = width - 1
        self._layout['mode_right_edge'] = (right_edge, 226)
        widest = max(fonts.Bold.getlength(m) for m in ('AUTO', 'MANU', 'WAIT', 'EVAL', 'LEARN'))
        self._layout['mode'] = (int(right_edge - widest), 226)

        # there is no room to the right of PWND for the last handshake name
        # (the mode sits there), so it gets its own row below
        self._layout['last_pwnd_name'] = (0, 236)
        self._layout['last_pwnd_max_x'] = right_edge
        return self._layout

    def _refresh(self, buf):
        raise NotImplementedError

    def render(self, canvas):
        # a frame queued for the previous (landscape) driver can still arrive
        # right after a runtime orientation switch; drop it instead of letting
        # the vendor library rotate it into a wrong image
        if canvas.size != (PORTRAIT_WIDTH, PORTRAIT_HEIGHT):
            logging.debug("%s: skipping %dx%d frame, expected %dx%d",
                          self.name, canvas.size[0], canvas.size[1], PORTRAIT_WIDTH, PORTRAIT_HEIGHT)
            return
        self._refresh(self._display.getbuffer(canvas))

    def clear(self):
        self._display.Clear(0xFF)


class WaveshareV1Portrait(_Waveshare213Portrait):
    def __init__(self, config):
        super(WaveshareV1Portrait, self).__init__(config, 'waveshare_1_portrait')

    def initialize(self):
        logging.info("initializing waveshare v2in13_V1 portrait display in monochromatic mode")
        from pwnagotchi.ui.hw.libs.waveshare.v2in13_V1.epd2in13 import EPD
        self._display = EPD()
        self._display.init(self._display.lut_full_update)
        self._display.Clear(0xFF)
        self._display.init(self._display.lut_partial_update)

    def _refresh(self, buf):
        self._display.display(buf)


class WaveshareV2Portrait(_Waveshare213Portrait):
    def __init__(self, config):
        super(WaveshareV2Portrait, self).__init__(config, 'waveshare_2_portrait')

    def initialize(self):
        logging.info("initializing waveshare v2in13_V2 portrait display")
        from pwnagotchi.ui.hw.libs.waveshare.v2in13_V2.epd2in13_V2 import EPD
        self._display = EPD()
        self._display.init(self._display.FULL_UPDATE)
        self._display.Clear(0xFF)
        self._display.init(self._display.PART_UPDATE)

    def _refresh(self, buf):
        self._display.displayPartial(buf)


class WaveshareV3Portrait(_Waveshare213Portrait):
    def __init__(self, config):
        super(WaveshareV3Portrait, self).__init__(config, 'waveshare_3_portrait')

    def initialize(self):
        logging.info("initializing waveshare v2in13_V3 portrait display")
        from pwnagotchi.ui.hw.libs.waveshare.v2in13_V3.epd2in13_V3 import EPD
        self._display = EPD()
        self._display.init()
        self._display.Clear(0xFF)

    def _refresh(self, buf):
        self._display.displayPartial(buf)


class WaveshareV4Portrait(_Waveshare213Portrait):
    def __init__(self, config):
        super(WaveshareV4Portrait, self).__init__(config, 'waveshare_4_portrait')

    def initialize(self):
        logging.info("initializing waveshare v4 portrait display")
        from pwnagotchi.ui.hw.libs.waveshare.v4.epd2in13_V4 import EPD
        self._display = EPD()
        self._display.init()
        self._display.Clear(0xFF)

        try:
            # Set initial base image in RAM to reduce ghosting on partial updates
            from PIL import Image
            blank_image = Image.new('1', (self._display.width, self._display.height), 255)
            buf = self._display.getbuffer(blank_image)
            self._display.displayPartBaseImage(buf)
        except Exception as err:
            logging.error(f"error initializing base image for waveshare v4 portrait: {err}")

    def _refresh(self, buf):
        self._display.displayPartial(buf)
