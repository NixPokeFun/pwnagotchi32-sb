from PIL import Image, ImageOps
from textwrap import TextWrapper


def _fit_to_max_x(value, value_x, max_x, font):
    if max_x is None or font is None or not value:
        return value
    budget = max_x - value_x
    if budget <= 0:
        return ''
    if font.getlength(value) <= budget:
        return value

    has_bracket = value.startswith('[') and value.endswith(']')
    ellipsis = '…]' if has_bracket else '…'
    trimmed = value[:-1] if has_bracket else value

    while trimmed and font.getlength(trimmed + ellipsis) > budget:
        trimmed = trimmed[:-1]

    if not trimmed:
        return ellipsis
    return trimmed + ellipsis


class Widget(object):
    def __init__(self, xy, color=0):
        self.xy = xy
        self.color = color

    def draw(self, canvas, drawer):
        raise Exception("not implemented")


class Bitmap(Widget):
    def __init__(self, path, xy, color=0):
        super().__init__(xy, color)
        self.image = Image.open(path)

    def draw(self, canvas, drawer):
        canvas.paste(self.image, self.xy)


class Line(Widget):
    def __init__(self, xy, color=0, width=1):
        super().__init__(xy, color)
        self.width = width

    def draw(self, canvas, drawer):
        drawer.line(self.xy, fill=self.color, width=self.width)


class Rect(Widget):
    def draw(self, canvas, drawer):
        drawer.rectangle(self.xy, outline=self.color)


class FilledRect(Widget):
    def draw(self, canvas, drawer):
        drawer.rectangle(self.xy, fill=self.color)


class Text(Widget):
    def __init__(self, value="", position=(0, 0), font=None, color=0, wrap=False,
                 max_length=0, png=False, right_edge=None, max_x=None):
        super().__init__(position, color)
        self.value = value
        self.font = font
        self.wrap = wrap
        self.max_length = max_length
        self.wrapper = TextWrapper(width=self.max_length, replace_whitespace=False) if wrap else None
        self.png = png
        self.right_edge = right_edge
        self.max_x = max_x

    def draw(self, canvas, drawer):
        if self.value is not None:
            if not self.png:
                if self.wrap:
                    text = '\n'.join(self.wrapper.wrap(self.value))
                else:
                    text = self.value
                if self.right_edge is not None:
                    drawer.text((self.right_edge, self.xy[1]), text, font=self.font,
                                fill=self.color, anchor="ra")
                else:
                    text = _fit_to_max_x(text, self.xy[0], self.max_x, self.font)
                    drawer.text(self.xy, text, font=self.font, fill=self.color)
            else:
                self.image = Image.open(self.value)
                self.image = self.image.convert('RGBA')
                self.pixels = self.image.load()
                for y in range(self.image.size[1]):
                    for x in range(self.image.size[0]):
                        if self.pixels[x, y][3] < 255:    # check alpha
                            self.pixels[x, y] = (255, 255, 255, 255)
                if self.color == 255:
                    self._image = ImageOps.colorize(self.image.convert('L'), black="white", white="black")
                else:
                    self._image = self.image
                self.image = self._image.convert('1')
                canvas.paste(self.image, self.xy)


class LabeledValue(Widget):
    def __init__(self, label, value="", position=(0, 0), label_font=None, text_font=None, color=0, label_spacing=5):
        super().__init__(position, color)
        self.label = label
        self.value = value
        self.label_font = label_font
        self.text_font = text_font
        self.label_spacing = label_spacing

    def draw(self, canvas, drawer):
        if self.label is None:
            drawer.text(self.xy, self.value, font=self.label_font, fill=self.color)
        else:
            pos = self.xy
            drawer.text(pos, self.label, font=self.label_font, fill=self.color)
            drawer.text((pos[0] + self.label_spacing + 5 * len(self.label), pos[1]), self.value, font=self.text_font, fill=self.color)
