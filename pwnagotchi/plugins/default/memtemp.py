# memtemp shows memory infos, cpu temperature and cpu frequency
#
# mem usage, cpu load, cpu temp, cpu frequency
#
###############################################################
from pwnagotchi.ui.components import LabeledValue, Text
from pwnagotchi.ui.view import BLACK
import pwnagotchi.ui.fonts as fonts
import pwnagotchi.plugins as plugins
import pwnagotchi
import logging


class MemTemp(plugins.Plugin):
    __author__ = 'https://github.com/xenDE'
    __editor__ = 'flippedgannet'
    __version__ = '1.0.3'
    __license__ = 'GPL3'
    __description__ = 'A plugin that displays memory/cpu usage, temperature and frequency'

    ALLOWED_FIELDS = {
        'mem': 'mem_usage',
        'cpu': 'cpu_load',
        'temp': 'cpu_temp',
        'freq': 'cpu_freq'
    }
    DEFAULT_FIELDS = ['mem', 'cpu', 'temp', 'freq']
    LINE_SPACING = 10
    LABEL_SPACING = 0
    FIELD_WIDTH = 4

    def on_loaded(self):
        logging.info("[memtemp] Plugin loaded.")

    def mem_usage(self):
        return f"{int(pwnagotchi.mem_usage() * 100)}%"

    def cpu_load(self):
        return f"{int(pwnagotchi.cpu_load() * 100)}%"

    def cpu_temp(self):
        scale = self.options.get('scale', 'celsius').lower()
        if scale == "fahrenheit":
            temp = (pwnagotchi.temperature() * 9 / 5) + 32
            symbol = "F"
        elif scale == "kelvin":
            temp = pwnagotchi.temperature() + 273.15
            symbol = "K"
        else:
            temp = pwnagotchi.temperature()
            symbol = "C"
        return f"{int(temp)}{symbol}"

    def cpu_freq(self):
        try:
            with open('/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq', 'rt') as fp:
                return f"{round(float(fp.readline()) / 1000000, 1)}G"
        except Exception as e:
            logging.debug("[memtemp] Failed to read CPU frequency: %s", e)
            return "0G"

    def pad_text(self, data):
        return " " * (self.FIELD_WIDTH - len(data)) + data

    def on_ui_setup(self, ui):
        if 'fields' in self.options and self.options['fields']:
            fields_raw = self.options['fields'].split(',')
            self.fields = [x.strip() for x in fields_raw if x.strip() in self.ALLOWED_FIELDS]
            if not self.fields:
                logging.warning("[memtemp] No valid fields in config, falling back to defaults: %s",
                                ",".join(self.DEFAULT_FIELDS))
                self.fields = self.DEFAULT_FIELDS
        else:
            self.fields = self.DEFAULT_FIELDS

        line_spacing = int(self.options.get('linespacing', self.LINE_SPACING))

        # Position must be provided via config. No hardcoded per-display defaults.
        h_pos = (153, 84)
        v_pos = (200, 64)
        if 'position' in self.options and self.options['position']:
            try:
                pos = [int(x.strip()) for x in self.options['position'].split(',')]
                h_pos = (pos[0], pos[1])
                v_pos = (pos[0], pos[1])
            except Exception as e:
                logging.warning("[memtemp] Invalid position option: %s", e)
        else:
            logging.warning("[memtemp] No 'position' set in config, using plugin defaults. "
                            "Set it explicitly for your display.")

        orientation = self.options.get('orientation', 'horizontal').lower()

        if orientation == "vertical":
            for idx, field in enumerate(self.fields):
                v_pos_x = v_pos[0]
                v_pos_y = v_pos[1] + ((len(self.fields) - 4) * -1 * line_spacing)
                ui.add_element(
                    f"memtemp_{field}",
                    LabeledValue(
                        color=BLACK,
                        label=f"{self.pad_text(field)}:",
                        value="-",
                        position=(v_pos_x, v_pos_y + (idx * line_spacing)),
                        label_font=fonts.Small,
                        text_font=fonts.Small,
                        label_spacing=self.LABEL_SPACING,
                    )
                )
        else:
            h_pos_x = h_pos[0] + ((len(self.fields) - 4) * -1 * 25)
            h_pos_y = h_pos[1]
            ui.add_element(
                'memtemp_header',
                Text(
                    color=BLACK,
                    value=" ".join([self.pad_text(x) for x in self.fields]),
                    position=(h_pos_x, h_pos_y),
                    font=fonts.Small,
                )
            )
            ui.add_element(
                'memtemp_data',
                Text(
                    color=BLACK,
                    value=" ".join([self.pad_text("-") for x in self.fields]),
                    position=(h_pos_x, h_pos_y + line_spacing),
                    font=fonts.Small,
                )
            )

    def on_unload(self, ui):
        with ui._lock:
            orientation = self.options.get('orientation', 'horizontal').lower()
            if orientation == "vertical":
                for field in self.fields:
                    ui.remove_element(f"memtemp_{field}")
            else:
                ui.remove_element('memtemp_header')
                ui.remove_element('memtemp_data')

    def on_ui_update(self, ui):
        orientation = self.options.get('orientation', 'horizontal').lower()
        if orientation == "vertical":
            for field in self.fields:
                ui.set(f"memtemp_{field}", getattr(self, self.ALLOWED_FIELDS[field])())
        else:
            data = " ".join([self.pad_text(getattr(self, self.ALLOWED_FIELDS[x])()) for x in self.fields])
            ui.set('memtemp_data', data)
