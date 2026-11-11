import logging
import time
import smbus
import threading
from collections import deque
from flask import abort, render_template_string

import pwnagotchi
import pwnagotchi.plugins as plugins
import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.components import LabeledValue
from pwnagotchi.ui.view import BLACK


PiSugar_addresses = {
    "PiSugar2": 0x75,      # PiSugar2/2Plus
    "PiSugar3": 0x57,      # PiSugar3/3Plus
    "PiSugar2 RTC": 0x32   # PiSugar2/2Plus RTC
}

# Battery level curve for 5312 (PiSugar3 / PiSugar2Plus)
curve5312 = [
    (4.10, 100.0),
    (4.05, 95.0),
    (3.90, 88.0),
    (3.80, 77.0),
    (3.70, 65.0),
    (3.62, 55.0),
    (3.58, 49.0),
    (3.49, 25.6),
    (3.32, 4.5),
    (3.1, 0.0),
]

# Battery level curve for 5209 (PiSugar2)
curve5209 = [
    (4.16, 100.0),
    (4.05, 95.0),
    (4.00, 80.0),
    (3.92, 65.0),
    (3.86, 40.0),
    (3.79, 25.5),
    (3.66, 10.0),
    (3.52, 6.5),
    (3.49, 3.2),
    (3.1, 0.0),
]


class PiSugarServer:
    def __init__(self):
        """
        Initialize connection state and thread for PiSugar communication.
        """
        self._bus = smbus.SMBus(1)
        self.ready = False
        self.model = None
        self.i2creg = []
        self.address = 0
        self.battery_voltage = 0.00
        self.voltage_history = deque(maxlen=10)
        self.battery_level = 0
        self.battery_charging = 0
        self.temperature = 0
        self.power_plugged = False
        self.allow_charging = True
        self.lowpower_shutdown = False
        self.lowpower_shutdown_level = 10
        self.shutdown_delay = 45
        self.max_charge_voltage_protection = False
        self.max_protection_level = 80

        # Start device auto-detection thread
        self.connection_thread = threading.Thread(
            target=self._connect_device, daemon=True
        )
        self.connection_thread.start()

    def _connect_device(self):
        """
        Attempt to connect to the PiSugar device in a background loop.
        """
        while self.model is None:
            if self.check_device(PiSugar_addresses["PiSugar2"]) is not None:
                self.address = PiSugar_addresses["PiSugar2"]
                if self.check_device(PiSugar_addresses["PiSugar2"], 0xC2) != 0:
                    self.model = "PiSugar2Plus"
                else:
                    self.model = "PiSugar2"
                self.device_init()
            elif self.check_device(PiSugar_addresses["PiSugar3"]) is not None:
                self.model = "PiSugar3"
                self.address = PiSugar_addresses["PiSugar3"]
                self.device_init()
            else:
                self.model = None
                logging.info(
                    "[PiSugarX] No PiSugar device found. Retrying in 5 seconds..."
                )
                time.sleep(5)

        logging.info(f"[PiSugarX] {self.model} connected successfully.")
        self.start_timer()

        while len(self.i2creg) < 256:
            time.sleep(1)

        self.ready = True
        logging.info(f"[PiSugarX] {self.model} initialized and ready.")

    def start_timer(self):
        """
        Start the telemetry polling thread.
        """
        timer_thread = threading.Thread(target=self.update_value, daemon=True)
        timer_thread.start()

    def update_value(self):
        """
        Periodically read registers and update status telemetry.
        """
        while True:
            try:
                if self.model in ("PiSugar2", "PiSugar2Plus"):
                    self.set_battery_notallow_charging()
                    time.sleep(0.05)

                self.i2creg = []
                for i in range(0, 256, 32):
                    current_register = i
                    current_length = min(32, 256 - i)
                    chunk = self._bus.read_i2c_block_data(
                        self.address, current_register, current_length
                    )
                    self.i2creg.extend(chunk)
                    time.sleep(0.1)

                if self.model == "PiSugar3":
                    low = self.i2creg[0x23]
                    high = self.i2creg[0x22]
                    self.battery_voltage = ((high << 8) + low) / 1000.0
                    self.temperature = self.i2creg[0x04] - 40
                    ctr1 = self.i2creg[0x02]
                    self.power_plugged = (ctr1 & (1 << 7)) != 0
                    self.allow_charging = (ctr1 & (1 << 6)) != 0

                    if self.max_charge_voltage_protection:
                        self._bus.write_byte_data(self.address, 0x0B, 0x29)
                        self._bus.write_byte_data(
                            self.address,
                            0x20,
                            self._bus.read_byte_data(self.address, 0x20) | 0b10000000,
                        )
                        self._bus.write_byte_data(self.address, 0x0B, 0x00)
                    else:
                        self._bus.write_byte_data(self.address, 0x0B, 0x29)
                        self._bus.write_byte_data(
                            self.address,
                            0x20,
                            self._bus.read_byte_data(self.address, 0x20) & 0b01111111,
                        )
                        self._bus.write_byte_data(self.address, 0x0B, 0x00)

                elif self.model == "PiSugar2":
                    high = self.i2creg[0xA3]
                    low = self.i2creg[0xA2]
                    if high & 0x20:
                        self.battery_voltage = (
                            2600.0 - (((high | 0b11000000) << 8) + low) * 0.26855
                        ) / 1000.0
                    else:
                        self.battery_voltage = (
                            2600.0 + (((high & 0x1F) << 8) + low) * 0.26855
                        ) / 1000.0

                    self.power_plugged = (self.i2creg[0x55] & 0b00010000) != 0

                    if self.max_charge_voltage_protection:
                        self.voltage_history.append(self.battery_voltage)
                        self.battery_level = self.convert_battery_voltage_to_level()
                        if self.battery_level > self.max_protection_level:
                            self.set_battery_notallow_charging()
                        else:
                            self.set_battery_allow_charging()
                    else:
                        self.set_battery_allow_charging()

                elif self.model == "PiSugar2Plus":
                    low = self.i2creg[0xD0]
                    high = self.i2creg[0xD1]
                    self.battery_voltage = (
                        (((high & 0b00111111) << 8) + low) * 0.26855 + 2600.0
                    ) / 1000.0
                    self.power_plugged = self.i2creg[0xDD] == 0x1F

                    if self.max_charge_voltage_protection:
                        self.voltage_history.append(self.battery_voltage)
                        self.battery_level = self.convert_battery_voltage_to_level()
                        if self.battery_level > self.max_protection_level:
                            self.set_battery_notallow_charging()
                        else:
                            self.set_battery_allow_charging()
                    else:
                        self.set_battery_allow_charging()

                self.voltage_history.append(self.battery_voltage)

                if self.model == "PiSugar3":
                    self.battery_level = self.i2creg[0x2A]
                else:
                    self.battery_level = self.convert_battery_voltage_to_level()

                if self.lowpower_shutdown and (self.battery_level < self.lowpower_shutdown_level):
                    logging.info("[PiSugarX] Low power threshold reached. Triggering shutdown.")
                    self.shutdown()
                    pwnagotchi.shutdown()

            except Exception as e:
                logging.error(f"[PiSugarX] Register read error: {e}")

            time.sleep(3)

    def shutdown(self):
        """
        Trigger hardware shutdown countdown via I2C for supported models.
        """
        if self.model == "PiSugar3":
            self._bus.write_byte_data(self.address, 0x0B, 0x29)
            self._bus.write_byte_data(self.address, 0x09, self.shutdown_delay)
            self._bus.write_byte_data(
                self.address,
                0x02,
                self._bus.read_byte_data(self.address, 0x02) & 0b11011111,
            )
            self._bus.write_byte_data(self.address, 0x0B, 0x00)
            logging.info(f"[PiSugarX] PiSugar scheduled hardware cutoff in {self.shutdown_delay}s.")

    def check_device(self, address, reg=0):
        """
        Safely check if an I2C device exists at a specified address.
        """
        try:
            return self._bus.read_byte_data(address, reg)
        except OSError:
            return None

    def device_init(self):
        """
        Configure hardware registers for specific PiSugar revisions.
        """
        if self.model == "PiSugar2Plus":
            self._bus.write_byte_data(
                self.address, 0x52, self._bus.read_byte_data(self.address, 0x52) | 0b00000010
            )
            self._bus.write_byte_data(
                self.address, 0x54, self._bus.read_byte_data(self.address, 0x54) | 0b00000010
            )
            self._bus.write_byte_data(
                self.address, 0x52, self._bus.read_byte_data(self.address, 0x52) | 0b00000100
            )
            self._bus.write_byte_data(
                self.address, 0x29, self._bus.read_byte_data(self.address, 0x29) & 0b10111111
            )
            self._bus.write_byte_data(
                self.address,
                0x52,
                (self._bus.read_byte_data(self.address, 0x52) & 0b10011111) | 0b01000000,
            )
            self._bus.write_byte_data(
                self.address, 0xC2, self._bus.read_byte_data(self.address, 0xC2) | 0b00010000
            )
            self._bus.write_byte_data(
                self.address,
                0x30,
                (self._bus.read_byte_data(self.address, 0x30) & 0b11000000) | 0x3F,
            )

        elif self.model == "PiSugar2":
            self._bus.write_byte_data(
                self.address,
                0x51,
                (self._bus.read_byte_data(self.address, 0x51) & 0b11110011) | 0b00000100,
            )
            self._bus.write_byte_data(
                self.address, 0x53, self._bus.read_byte_data(self.address, 0x53) | 0b00000010
            )
            self._bus.write_byte_data(
                self.address,
                0x51,
                (self._bus.read_byte_data(self.address, 0x51) & 0b11001111) | 0b00010000,
            )
            self._bus.write_byte_data(
                self.address, 0x26, self._bus.read_byte_data(self.address, 0x26) & 0b10110000
            )
            self._bus.write_byte_data(
                self.address,
                0x52,
                (self._bus.read_byte_data(self.address, 0x52) & 0b11110011) | 0b00000100,
            )
            self._bus.write_byte_data(
                self.address,
                0x53,
                (self._bus.read_byte_data(self.address, 0x53) & 0b11101111) | 0b00010000,
            )

    def convert_battery_voltage_to_level(self):
        """
        Convert average battery voltage to percentage based on hardware curves.
        """
        if self.model in ("PiSugar2Plus", "PiSugar3Plus", "PiSugar3"):
            curve = curve5312
        else:
            curve = curve5209

        if len(self.voltage_history) < 5:
            avg_voltage = sum(self.voltage_history) / len(self.voltage_history)
        else:
            sorted_history = sorted(self.voltage_history)
            trimmed_history = sorted_history[2:-2]
            avg_voltage = sum(trimmed_history) / len(trimmed_history)

        for (v1, p1), (v2, p2) in zip(curve, curve[1:]):
            if v2 <= avg_voltage <= v1:
                return p2 + (p1 - p2) * (avg_voltage - v2) / (v1 - v2)

        return curve[-1][1] if avg_voltage < curve[-1][0] else curve[0][1]

    def get_version(self):
        if self.model == "PiSugar3":
            try:
                return bytes(self.i2creg[0xE2:0xEE]).decode("ascii")
            except Exception:
                return None
        return None

    def get_model(self):
        return self.model

    def get_battery_level(self):
        return self.battery_level

    def get_battery_voltage(self):
        return self.battery_voltage

    def get_battery_allow_charging(self):
        return self.allow_charging

    def set_battery_allow_charging(self):
        if self.model == "PiSugar2":
            self._bus.write_byte_data(
                self.address, 0x54, self._bus.read_byte_data(self.address, 0x54) & 0b11111011
            )
            self._bus.write_byte_data(
                self.address, 0x55, self._bus.read_byte_data(self.address, 0x55) & 0b11111011
            )
            self._bus.write_byte_data(
                self.address, 0x54, self._bus.read_byte_data(self.address, 0x54) | 0b00000100
            )
        elif self.model == "PiSugar2Plus":
            self._bus.write_byte_data(
                self.address, 0x56, self._bus.read_byte_data(self.address, 0x56) & 0b11111011
            )
            self._bus.write_byte_data(
                self.address, 0x58, self._bus.read_byte_data(self.address, 0x58) & 0b11111011
            )
            self._bus.write_byte_data(
                self.address, 0x56, self._bus.read_byte_data(self.address, 0x56) | 0b00000100
            )

    def set_battery_notallow_charging(self):
        if self.model == "PiSugar2":
            self._bus.write_byte_data(
                self.address, 0x54, self._bus.read_byte_data(self.address, 0x54) & 0b11111011
            )
            self._bus.write_byte_data(
                self.address, 0x55, self._bus.read_byte_data(self.address, 0x55) | 0b00000100
            )
            self._bus.write_byte_data(
                self.address, 0x54, self._bus.read_byte_data(self.address, 0x54) | 0b00000100
            )
        elif self.model == "PiSugar2Plus":
            self._bus.write_byte_data(
                self.address, 0x56, self._bus.read_byte_data(self.address, 0x56) & 0b11111011
            )
            self._bus.write_byte_data(
                self.address, 0x58, self._bus.read_byte_data(self.address, 0x58) | 0b00000100
            )
            self._bus.write_byte_data(
                self.address, 0x56, self._bus.read_byte_data(self.address, 0x56) | 0b00000100
            )

    def get_temperature(self):
        return self.temperature

    def get_battery_power_plugged(self):
        return self.power_plugged


class PiSugar(plugins.Plugin):
    __author__ = "jayofelony & wsvdmeer"
    __version__ = "2.0.0"
    __license__ = "GPL3"
    __description__ = (
        "Combined PiSugar hardware manager and OS RTC datetime display for Pwnagotchi."
    )

    DEFAULT_RTC_POSITION = (0, 92)
    DEFAULT_RTC_FORMAT = "%H:%M %d-%m"

    def __init__(self):
        self._agent = None
        self.ps = None
        self.ready = False

        # PiSugar display rotation settings
        self.drot = 0
        self.nextDChg = 0
        self.rotation_enabled = True
        self.default_display = "voltage"

        # Integrated RTC datetime settings
        self.datetime_enabled = True
        self.datetime_position = self.DEFAULT_RTC_POSITION
        self.datetime_format = self.DEFAULT_RTC_FORMAT

        try:
            self.ps = PiSugarServer()
        except Exception as e:
            logging.debug("[PiSugarX] Unable to instantiate server: %s", repr(e))

    def safe_get(self, func, default=None):
        """
        Safely invoke server functions preventing unhandled runtime exceptions.
        """
        if self.ps is None:
            return default
        try:
            return func()
        except Exception as e:
            logging.debug("[PiSugarX] Safe execution fallback [%s]: %s", func.__name__, e)
            return default

    def on_loaded(self):
        logging.info("[PiSugarX] Plugin loading...")
        cfg = pwnagotchi.config.get("main", {}).get("plugins", {}).get("pisugarx", {})

        # Parse PiSugar battery options
        self.rotation_enabled = cfg.get("rotation", True)
        self.default_display = cfg.get("default_display", "voltage").lower()

        valid_displays = ["voltage", "percentage", "temp"]
        if self.default_display not in valid_displays:
            logging.warning(
                f"[PiSugarX] Invalid default_display '{self.default_display}'. Defaulting to 'voltage'."
            )
            self.default_display = "voltage"

        if self.ps:
            self.ps.lowpower_shutdown = cfg.get("lowpower_shutdown", True)
            self.ps.lowpower_shutdown_level = cfg.get("lowpower_shutdown_level", 10)
            self.ps.max_charge_voltage_protection = cfg.get("max_charge_voltage_protection", True)
            self.ps.shutdown_delay = int(cfg.get("shutdown_delay", 45))

        # Parse RTC datetime integration options
        self.datetime_enabled = cfg.get("datetime_enabled", True)

        position = cfg.get("datetime_position", self.DEFAULT_RTC_POSITION)
        if isinstance(position, (list, tuple)) and len(position) == 2:
            self.datetime_position = tuple(position)
        else:
            logging.warning(
                f"[PiSugarX] Invalid datetime_position {position!r}. Using default {self.DEFAULT_RTC_POSITION}."
            )

        fmt = cfg.get("datetime_format", self.DEFAULT_RTC_FORMAT)
        if isinstance(fmt, str) and fmt:
            self.datetime_format = fmt
        else:
            logging.warning(
                f"[PiSugarX] Invalid datetime_format {fmt!r}. Using default '{self.DEFAULT_RTC_FORMAT}'."
            )

        logging.info("[PiSugarX] Plugin successfully loaded.")

    def on_ready(self, agent):
        if self.ps:
            self.ready = self.ps.ready

    def on_ui_setup(self, ui):
        # 1. Add Battery UI element
        ui.add_element(
            "bat",
            LabeledValue(
                color=BLACK,
                label="BAT",
                value="0%",
                position=(ui.width() / 2 + 15, 0),
                label_font=fonts.Bold,
                text_font=fonts.Medium,
            ),
        )

        # 2. Add RTC Datetime UI element if enabled
        if self.datetime_enabled:
            ui.add_element(
                "datetime",
                LabeledValue(
                    color=BLACK,
                    label="",
                    value="--:--",
                    position=self.datetime_position,
                    label_font=fonts.Small,
                    text_font=fonts.Small,
                ),
            )

    def on_unload(self, ui):
        with ui._lock:
            if "bat" in ui._state._state:
                ui.remove_element("bat")
            if "datetime" in ui._state._state:
                ui.remove_element("datetime")

    def on_ui_update(self, ui):
        # Update Battery telemetry on UI
        if "bat" in ui._state._state:
            if self.ps and self.ps.ready:
                capacity = self.safe_get(self.ps.get_battery_level, default=0)
                voltage = self.safe_get(self.ps.get_battery_voltage, default=0.00)
                temp = self.safe_get(self.ps.get_temperature, default=0)
            else:
                capacity = 0
                voltage = 0.00
                temp = 0

            battery_plugged = self.safe_get(self.ps.get_battery_power_plugged, default=False) if self.ps else False
            ui._state._state["bat"].label = "CHG" if battery_plugged else "BAT"

            if self.rotation_enabled:
                if time.time() > self.nextDChg:
                    self.drot = (self.drot + 1) % 3
                    self.nextDChg = time.time() + 5

                if self.drot == 0:
                    ui.set("bat", f"{voltage:.2f}V")
                elif self.drot == 1:
                    ui.set("bat", f"{capacity:.0f}%")
                else:
                    ui.set("bat", f"{temp}°C")
            else:
                if self.default_display == "voltage":
                    ui.set("bat", f"{voltage:.2f}V")
                elif self.default_display in ["percentage", "percent"]:
                    ui.set("bat", f"{capacity:.0f}%")
                elif self.default_display == "temp":
                    ui.set("bat", f"{temp}°C")

        # Update RTC Datetime on UI
        if self.datetime_enabled and "datetime" in ui._state._state:
            try:
                current_time = time.strftime(self.datetime_format)
            except (ValueError, TypeError):
                logging.warning(
                    f"[PiSugarX] Invalid format '{self.datetime_format}'. Falling back to default."
                )
                self.datetime_format = self.DEFAULT_RTC_FORMAT
                current_time = time.strftime(self.datetime_format)

            ui.set("datetime", current_time)

    def on_webhook(self, path, request):
        if not self.ready or self.ps is None:
            return render_template_string(
                "<html><head><title>PiSugarX</title></head><body><h1>PiSugarX not ready</h1></body></html>"
            )

        try:
            if request.method == "GET":
                if path == "/" or not path:
                    version = self.safe_get(self.ps.get_version, default="Unknown")
                    model = self.safe_get(self.ps.get_model, default="Unknown")
                    battery_level = self.safe_get(self.ps.get_battery_level, default="N/A")
                    battery_voltage = self.safe_get(self.ps.get_battery_voltage, default="N/A")
                    temperature = self.safe_get(self.ps.get_temperature, default="N/A")

                    ret = f"""
                    <!DOCTYPE html>
                    <html lang="en">
                    <head>
                        <meta charset="UTF-8">
                        <title>PiSugarX Status</title>
                        <style>
                            body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f9; }}
                            h1 {{ color: #333; }}
                            table {{ border-collapse: collapse; width: 300px; }}
                            th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; }}
                            th {{ background-color: #eee; }}
                        </style>
                    </head>
                    <body>
                        <h1>PiSugarX Overview</h1>
                        <table>
                            <tr><th>Property</th><th>Value</th></tr>
                            <tr><td>Model</td><td>{model}</td></tr>
                            <tr><td>Firmware</td><td>{version}</td></tr>
                            <tr><td>Battery Level</td><td>{battery_level}%</td></tr>
                            <tr><td>Voltage</td><td>{battery_voltage}V</td></tr>
                            <tr><td>Temperature</td><td>{temperature} °C</td></tr>
                        </table>
                    </body>
                    </html>
                    """
                    return render_template_string(ret)
                abort(404)
        except Exception as e:
            return render_template_string(f"<html><body><h1>Error: {repr(e)}</h1></body></html>"), 500
