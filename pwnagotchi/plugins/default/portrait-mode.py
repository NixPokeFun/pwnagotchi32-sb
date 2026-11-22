import logging
import threading

import tomlkit

import pwnagotchi
import pwnagotchi.plugins as plugins
import pwnagotchi.ui.hw as hw


class PortraitMode(plugins.Plugin):
    __author__ = 'flippedgannet (ported from ex18a)'
    __version__ = '1.0.0'
    __license__ = 'GPL3'
    __description__ = 'Shows the Waveshare 2.13" e-paper (V1-V4) upright (122x250) instead of landscape.'

    CONFIG_PATH = '/etc/pwnagotchi/config.toml'

    # landscape display type -> portrait display type
    PORTRAIT_FOR = {
        'waveshare_1': 'waveshare_1_portrait',
        'waveshare_2': 'waveshare_2_portrait',
        'waveshare_3': 'waveshare_3_portrait',
        'waveshare_4': 'waveshare_4_portrait',
    }
    LANDSCAPE_FOR = {v: k for k, v in PORTRAIT_FOR.items()}

    # display type -> driver class exported by pwnagotchi.ui.hw
    IMPL_CLASS = {
        'waveshare_1': 'WaveshareV1',
        'waveshare_2': 'WaveshareV2',
        'waveshare_3': 'WaveshareV3',
        'waveshare_4': 'WaveshareV4',
        'waveshare_1_portrait': 'WaveshareV1Portrait',
        'waveshare_2_portrait': 'WaveshareV2Portrait',
        'waveshare_3_portrait': 'WaveshareV3Portrait',
        'waveshare_4_portrait': 'WaveshareV4Portrait',
    }

    # Elements that other plugins add at positions meant for the 250x122
    # landscape canvas: some are off the 122px wide portrait canvas, some would
    # sit on top of the face. They are moved into the free band between the
    # peer line and the CH/APS row (y=154..208). Best-effort defaults, override
    # or extend them in the config, e.g.
    #   main.plugins.portrait-mode.positions.control_ip = "0,172"
    DEFAULT_POSITIONS = {
        'control_iface': (4, 154),
        'control_blinds': (64, 154),
        'control_ip': (0, 166),
        'memtemp_header': (0, 184),
        'memtemp_data': (0, 194),
        'latitude': (0, 178),
        'longitude': (0, 188),
        'altitude': (0, 198),
        'pass': (0, 198),
        'datetime': (0, 198),
        'bat': (76, 0),
    }

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = False
        self.ready = False
        # element key -> position it had before this plugin moved it
        self._moved = {}
        self._positions = dict(self.DEFAULT_POSITIONS)

    # ---------------------------------------------------------------- helpers

    def _persist(self):
        # write the display type back to the config so the next boot comes up
        # in the right orientation right away, without a landscape flash first
        return bool(self.options.get('persist', True))

    @staticmethod
    def _parse_position(value):
        if isinstance(value, str):
            value = value.split(',')
        x, y = value
        return int(x), int(y)

    def _load_positions(self):
        self._positions = dict(self.DEFAULT_POSITIONS)
        custom = self.options.get('positions', {})
        if not isinstance(custom, dict):
            return
        for key, value in custom.items():
            try:
                self._positions[key] = self._parse_position(value)
            except Exception:
                logging.warning("[portrait-mode] ignoring invalid position for '%s': %r", key, value)

    def _write_display_type(self, display_type):
        if not self._persist():
            return
        try:
            with open(self.CONFIG_PATH, 'r') as fp:
                doc = tomlkit.parse(fp.read())
            try:
                doc['ui']['display']['type'] = display_type
            except (KeyError, TypeError):
                # keep a flat dotted key so it cannot swallow other lines
                doc.append(tomlkit.key(['ui', 'display', 'type']), tomlkit.item(display_type))
            with open(self.CONFIG_PATH, 'w') as fp:
                fp.write(tomlkit.dumps(doc))
            logging.info("[portrait-mode] wrote display type '%s' to the config", display_type)
        except Exception as err:
            logging.error("[portrait-mode] could not write the config: %s", err)

    def _make_impl(self, display_type):
        return getattr(hw, self.IMPL_CLASS[display_type])(pwnagotchi.config)

    @staticmethod
    def _display_ready(ui):
        # Display builds its panel and render thread right after View.__init__
        # has fired ui_setup, so wait until that has finished. Anything that
        # is not a Display has no render thread and is always ready.
        if hasattr(type(ui), '_render_thread'):
            return getattr(ui, '_render_thread_instance', None) is not None
        return True

    # ---------------------------------------------------------- orientation

    def _enter_portrait(self, ui):
        with self._lock:
            current = ui._implementation.name
            if current in self.LANDSCAPE_FOR:
                # booted straight into a portrait driver, nothing to swap
                self.ready = True
                logging.info("[portrait-mode] portrait driver '%s' is already active", current)
                return
            portrait_type = self.PORTRAIT_FOR.get(current)
            if portrait_type is None:
                logging.error("[portrait-mode] no portrait driver for display '%s'", current)
                return
            try:
                ui.apply_implementation(self._make_impl(portrait_type))
            except Exception:
                logging.exception("[portrait-mode] switching to %s failed", portrait_type)
                return
            self.ready = True
            self._write_display_type(portrait_type)
            logging.info("[portrait-mode] switched to %s", portrait_type)

    def _leave_portrait(self, ui):
        with self._lock:
            current = ui._implementation.name
            landscape_type = self.LANDSCAPE_FOR.get(current)
            if landscape_type is None:
                return
            try:
                ui.apply_implementation(self._make_impl(landscape_type))
            except Exception:
                logging.exception("[portrait-mode] switching back to %s failed", landscape_type)
                return
            self._write_display_type(landscape_type)
            logging.info("[portrait-mode] switched back to %s", landscape_type)

    def _place_elements(self, ui):
        elems = ui._state._state
        with self._lock:
            if not self.ready:
                return
            for key, target in self._positions.items():
                elem = elems.get(key)
                if elem is None or tuple(elem.xy) == target:
                    continue
                self._moved.setdefault(key, tuple(elem.xy))
                elem.xy = target

    def _restore_elements(self, ui):
        elems = ui._state._state
        with self._lock:
            # also stops a ui_update still queued from re-moving the elements
            self.ready = False
            for key, original in self._moved.items():
                elem = elems.get(key)
                if elem is not None:
                    elem.xy = original
            self._moved.clear()

    # ----------------------------------------------------------- plugin API

    def on_loaded(self):
        logging.info("[portrait-mode] plugin loaded")

    def on_ui_setup(self, ui):
        display_type = ui._implementation.name
        if display_type not in self.PORTRAIT_FOR and display_type not in self.LANDSCAPE_FOR:
            logging.error("[portrait-mode] unsupported display '%s', only the Waveshare 2.13\" V1-V4 "
                          "(%s) can be switched. Plugin stays inactive.",
                          display_type, ', '.join(self.PORTRAIT_FOR))
            return

        rotation = getattr(ui, '_rotation', 0)
        if rotation not in (0, 180):
            logging.warning("[portrait-mode] ui.display.rotation = %s is not usable in portrait, "
                            "use 0 or 180", rotation)

        self._load_positions()

        if self._display_ready(ui):
            # enabled at runtime, the panel is already up
            self._enter_portrait(ui)
        else:
            # enabled at boot, wait for the first ui_update
            self._pending = True

    def on_ui_update(self, ui):
        if self._pending and self._display_ready(ui):
            self._pending = False
            self._enter_portrait(ui)
        if self.ready:
            self._place_elements(ui)

    def on_unload(self, ui):
        self._pending = False
        if not self.ready:
            return
        try:
            self._restore_elements(ui)
            self._leave_portrait(ui)
        except Exception:
            logging.exception("[portrait-mode] failed to revert to landscape")
        self.ready = False
