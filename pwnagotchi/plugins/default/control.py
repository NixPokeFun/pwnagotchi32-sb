import fcntl
import logging
import os
import socket
import struct
import threading

from pwnagotchi.ui.components import LabeledValue, Text
import pwnagotchi.ui.view as view
import pwnagotchi.ui.fonts as fonts
import pwnagotchi.plugins as plugins
import pwnagotchi


class Control(plugins.Plugin):
    __author__ = "flippedgannet"
    __version__ = "0.3.0"
    __license__ = "GPL3"
    __description__ = "Displays interface, blind epochs, Watchdog health, WAN and IP"

    _DNS_PROBE = "one.one.one.one"
    _RAW_PROBE = ("1.1.1.1", 53)
    _RAW_TIMEOUT = 2.0
    _BT_PLUGIN = "bt-pan"
    _DEFAULTS = {
        "iface_pos": "5,32", "blinds_pos": "50,32",
        "health_pos": "80,32", "wan_pos": "100,0", "ip_pos": "0,96",
    }

    def __init__(self):
        self.iface = "mon0"
        self.agent = None
        self._net_status = "--"
        self._net_lock = threading.Lock()
        self._net_stop = threading.Event()
        self._net_wake = threading.Event()
        self._net_thread = None
        self._net_interval = 30
        self._show = {}
        self._positions = {}
        self._has = set()

    def on_loaded(self):
        self.iface = pwnagotchi.config.get("main", {}).get("iface") or self.options.get("interface", "mon0")
        for key in ("iface", "blinds", "health", "wan", "ip"):
            self._show[key] = bool(self.options.get(key, True))
        for key, default in self._DEFAULTS.items():
            self._positions[key] = self._parse_pos(key, default)
        try:
            self._net_interval = max(5, int(self.options.get("net_check_interval", 30)))
        except (TypeError, ValueError):
            self._net_interval = 30
        self._net_thread = threading.Thread(target=self._net_loop, daemon=True, name="control-net")
        self._net_thread.start()
        logging.info("[control] loaded; iface=%s", self.iface)

    def on_ready(self, agent):
        self.agent = agent

    def on_bt_pan_connected(self, *args, **kwargs):
        self._net_wake.set()

    def on_bt_pan_disconnected(self, *args, **kwargs):
        self._net_wake.set()

    def _add(self, ui, name, element):
        ui.add_element(name, element)
        self._has.add(name)

    def on_ui_setup(self, ui):
        if self._show.get("iface"):
            self._add(ui, "control_iface", Text(color=view.BLACK, value=self.iface,
                position=self._positions["iface_pos"], font=fonts.Small))
        if self._show.get("blinds"):
            self._add(ui, "control_blinds", Text(color=view.BLACK, value="b:0",
                position=self._positions["blinds_pos"], font=fonts.Small))
        if self._show.get("health"):
            self._add(ui, "control_health", Text(color=view.BLACK, value="dog:--",
                position=self._positions["health_pos"], font=fonts.Small))
        if self._show.get("wan"):
            self._add(ui, "control_wan", LabeledValue(color=view.BLACK, label="WAN:", value="--",
                position=self._positions["wan_pos"], label_font=fonts.Bold, text_font=fonts.Medium))
        if self._show.get("ip"):
            self._add(ui, "control_ip", LabeledValue(color=view.BLACK, label="IP:", value="--",
                position=self._positions["ip_pos"], label_font=fonts.Bold, text_font=fonts.Medium))

    def on_ui_update(self, ui):
        if "control_iface" in self._has:
            ui.set("control_iface", self.iface)
        if "control_blinds" in self._has:
            blind = getattr(getattr(self.agent, "_epoch", None), "blind_for", 0) or 0
            ui.set("control_blinds", "b:%s" % blind)
        if "control_health" in self._has:
            ui.set("control_health", "dog:%s" % self._watchdog_state())
        bt = self._bt_state() if ({"control_wan", "control_ip"} & self._has) else None
        if "control_wan" in self._has:
            if bt:
                status = "BT"
            else:
                with self._net_lock:
                    status = self._net_status
            ui.set("control_wan", status)
        if "control_ip" in self._has:
            ui.set("control_ip", (bt or {}).get("ip") or self._local_ip() or "--")

    def _watchdog_state(self):
        plugin = plugins.loaded.get("watchdog")
        if plugin is None:
            return "--"
        try:
            return plugin.get_status().get("status", "??")
        except Exception:
            return "??"

    def on_unload(self, ui):
        self._net_stop.set()
        self._net_wake.set()
        if self._net_thread and self._net_thread.is_alive():
            self._net_thread.join(timeout=self._RAW_TIMEOUT + 1)
        with ui._lock:
            for name in self._has:
                ui.remove_element(name)
        logging.info("[control] unloaded")

    def _parse_pos(self, key, default):
        raw = self.options.get(key, default)
        parts = [x.strip() for x in raw.split(",")] if isinstance(raw, str) else list(raw) if isinstance(raw, (list, tuple)) else []
        if len(parts) != 2:
            parts = default.split(",")
        try:
            return int(parts[0]), int(parts[1])
        except (TypeError, ValueError):
            return tuple(int(x) for x in default.split(","))

    def _bt_state(self):
        plugin = plugins.loaded.get(self._BT_PLUGIN)
        if plugin is None:
            return None
        try:
            state = plugin.get_state()
            return state if state.get("state") == "connected" else None
        except Exception:
            return None

    @staticmethod
    def _iface_ipv4(iface):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            result = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", iface[:15].encode()))
            ip = socket.inet_ntoa(result[20:24])
            return None if ip.startswith("169.254.") else ip
        except OSError:
            return None
        finally:
            sock.close()

    def _local_ip(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(self._RAW_PROBE)
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("169.254."):
                return ip
        except OSError:
            pass
        finally:
            sock.close()
        try:
            for name in sorted(os.listdir("/sys/class/net")):
                if name != "lo":
                    ip = self._iface_ipv4(name)
                    if ip:
                        return ip
        except OSError:
            pass
        return None

    def _net_loop(self):
        while not self._net_stop.is_set():
            try:
                status = self._probe_network()
            except Exception:
                logging.exception("[control] network probe failed")
                status = "??"
            with self._net_lock:
                self._net_status = status
            self._net_wake.wait(self._net_interval)
            self._net_wake.clear()

    def _probe_network(self):
        dns_ok = self._dns_ok()
        raw_ok = self._raw_ok()
        if dns_ok and raw_ok: return "OK"
        if raw_ok: return "DNS"
        if dns_ok: return "??"
        return "NO"

    def _dns_ok(self):
        try:
            socket.gethostbyname(self._DNS_PROBE)
            return True
        except (socket.gaierror, OSError):
            return False

    def _raw_ok(self):
        try:
            with socket.create_connection(self._RAW_PROBE, timeout=self._RAW_TIMEOUT):
                return True
        except OSError:
            return False
