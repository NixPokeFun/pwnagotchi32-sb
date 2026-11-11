import json
import logging
import os

import pwnagotchi.plugins as plugins
import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.components import LabeledValue
from pwnagotchi.ui.view import BLACK


class GPS(plugins.Plugin):
    __author__ = "evilsocket@gmail.com"
    __editor__ = "flippedgannet"
    __version__ = "1.1.0"
    __license__ = "GPL3"
    __description__ = "Save GPS coordinates whenever an handshake is captured."

    LINE_SPACING = 10
    LABEL_SPACING = 0

    def __init__(self):
        self.running = False
        self.coordinates = None
        self.options = dict()

    def on_loaded(self):
        logging.info(f"[gps] Plugin loaded for {self.options['device']}")

    def on_ready(self, agent):
        if os.path.exists(self.options["device"]) or ":" in self.options["device"]:
            logging.info(
                f"[gps] Enabling bettercap's gps module for {self.options['device']}"
            )
            try:
                agent.run("gps off")
            except Exception:
                logging.info(f"[gps] Bettercap gps module was already off")
                pass

            agent.run(f"set gps.device {self.options['device']}")
            agent.run(f"set gps.baudrate {self.options['speed']}")
            agent.run("gps on")
            logging.info(f"[gps] Bettercap gps module enabled on {self.options['device']}")
            self.running = True
        else:
            logging.warning("[gps] No GPS detected")

    def on_handshake(self, agent, filename, access_point, client_station):
        if self.running:
            info = agent.session()
            self.coordinates = info["gps"]
            gps_filename = filename.replace(".pcap", ".gps.json")

            if self.coordinates and all([
                # Ignore zeroed or incomplete telemetry coordinates
                self.coordinates["Latitude"], self.coordinates["Longitude"]
            ]):
                logging.info(f"[gps] Saving GPS to {gps_filename} ({self.coordinates})")
                with open(gps_filename, "w+t") as fp:
                    json.dump(self.coordinates, fp)
            else:
                logging.info("[gps] Not saving GPS. Couldn't find location.")

    def on_ui_setup(self, ui):
        # Default positioning layout for v4 e-ink 2.13
        lat_pos = (132, 75)
        lon_pos = (128, 85)
        alt_pos = (132, 95)

        # Check if user explicitly wants custom layout configuration
        use_custom = self.options.get('custom_pos', False)

        if use_custom:
            try:
                # Try parsing individual element positions from config file
                if 'lat_pos' in self.options:
                    lat_pos = [int(x.strip()) for x in self.options['lat_pos'].split(',')]
                if 'lon_pos' in self.options:
                    lon_pos = [int(x.strip()) for x in self.options['lon_pos'].split(',')]
                if 'alt_pos' in self.options:
                    alt_pos = [int(x.strip()) for x in self.options['alt_pos'].split(',')]

                logging.info("[gps] Using user-defined custom layout positions.")
            except Exception as e:
                # Fallback on parsing error
                logging.error(f"[gps] Error parsing custom GPS coordinates: {e}. Reverting to v4 defaults.")
                lat_pos = (132, 75)
                lon_pos = (128, 85)
                alt_pos = (132, 95)
        else:
            # Fallback message when custom positions are disabled or missing
            logging.warning("[gps] Custom layout disabled or not specified. Applying default layout for v4 e-ink 2.13.")

        # Inject UI elements into the display layout canvas
        ui.add_element(
            "latitude",
            LabeledValue(
                color=BLACK,
                label="lat:",
                value="-",
                position=lat_pos,
                label_font=fonts.Small,
                text_font=fonts.Small,
                label_spacing=self.LABEL_SPACING,
            ),
        )
        ui.add_element(
            "longitude",
            LabeledValue(
                color=BLACK,
                label="long:",
                value="-",
                position=lon_pos,
                label_font=fonts.Small,
                text_font=fonts.Small,
                label_spacing=self.LABEL_SPACING,
            ),
        )
        ui.add_element(
            "altitude",
            LabeledValue(
                color=BLACK,
                label="alt:",
                value="-",
                position=alt_pos,
                label_font=fonts.Small,
                text_font=fonts.Small,
                label_spacing=self.LABEL_SPACING,
            ),
        )

    def on_unload(self, ui):
        # Safely remove elements during plugin deconstruction or reload phase
        with ui._lock:
            ui.remove_element('latitude')
            ui.remove_element('longitude')
            ui.remove_element('altitude')

    def on_ui_update(self, ui):
        with ui._lock:
            if self.coordinates and all([
                self.coordinates["Latitude"], self.coordinates["Longitude"]
            ]):
                # Formatting telemetry values to clear trailing artifact rendering issues
                ui.set("latitude", f"{self.coordinates['Latitude']:.4f} ")
                ui.set("longitude", f"{self.coordinates['Longitude']:.4f} ")
                ui.set("altitude", f"{self.coordinates['Altitude']:.1f}m ")
