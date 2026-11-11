"""
control: iface / blind epochs / WAN status / IP on the pwnagotchi screen.

Config (all optional):
    [main.plugins.control]
    enabled = true
    iface = true            # show wifi interface
    iface_pos = "5,32"
    blinds = true           # show watchdog blind epochs
    blinds_pos = "50,32"
    wan = true              # show WAN status (shows BT while bt-pan is connected)
    wan_pos = "100,0"
    ip = true               # show current IP (bt-pan IP while bt-pan is connected)
    ip_pos = "0,96"
    net_check_interval = 30
"""
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
    __author__ = 'flippedgannet'
    __version__ = '0.2.0'
    __license__ = 'GPL3'
    __description__ = (
        'Displays current Wi-Fi interface, watchdog blind epochs counter, '
        'network status and IP address (bt-pan aware)'
    )

    # Two independent probes: one resolves a hostname, the other opens a
    # raw TCP session to a fixed IP. A captive portal that answers DNS
    # but drops traffic therefore cannot fake a healthy state.
    _DNS_PROBE = 'one.one.one.one'
    _RAW_PROBE = ('1.1.1.1', 53)
    _RAW_TIMEOUT = 2.0

    _BT_PLUGIN = 'bt-pan'

    # Default positions, overridable per element via "<x>,<y>" in config.
    _DEFAULTS = {
        'iface_pos':  '5,32',
        'blinds_pos': '50,32',
        'wan_pos':    '100,0',
        'ip_pos':     '0,96',
    }

    def __init__(self):
        self.iface = 'mon0'
        self.agent = None
        self._net_status = '--'
        self._net_lock = threading.Lock()
        self._net_stop = threading.Event()
        self._net_wake = threading.Event()  # set -> re-probe now
        self._net_thread = None
        self._net_interval = 30

        # Element toggles and positions, filled in on_loaded().
        self._show_iface = True
        self._show_blinds = True
        self._show_wan = True
        self._show_ip = True
        self._pos_iface = (5, 32)
        self._pos_blinds = (60, 32)
        self._pos_wan = (80, 0)
        self._pos_ip = (0, 96)

        # Track which widgets actually exist so on_ui_update / on_unload
        # never touch a missing element.
        self._has_iface = False
        self._has_blinds = False
        self._has_wan = False
        self._has_ip = False

    def on_loaded(self):
        # Retrieve primary wireless interface from global configuration
        main_iface = pwnagotchi.config.get('main', {}).get('iface')
        self.iface = main_iface or self.options.get('interface', 'mon0')

        self._show_iface = bool(self.options.get('iface', True))
        self._show_blinds = bool(self.options.get('blinds', True))
        self._show_wan = bool(self.options.get('wan', True))
        self._show_ip = bool(self.options.get('ip', True))

        self._pos_iface = self._parse_pos('iface_pos')
        self._pos_blinds = self._parse_pos('blinds_pos')
        self._pos_wan = self._parse_pos('wan_pos')
        self._pos_ip = self._parse_pos('ip_pos')

        try:
            self._net_interval = max(5, int(self.options.get('net_check_interval', 30)))
        except (TypeError, ValueError):
            self._net_interval = 30

        self._net_thread = threading.Thread(
            target=self._net_loop, daemon=True, name='control-net',
        )
        self._net_thread.start()
        logging.info(
            "[control] Plugin loaded. Interface=%s (iface=%s blinds=%s wan=%s ip=%s)",
            self.iface, self._show_iface, self._show_blinds,
            self._show_wan, self._show_ip,
        )

    def on_ready(self, agent):
        self.agent = agent

    # --- bt-pan events: re-probe right away so WAN does not lag ----------

    def on_bt_pan_connected(self, *args, **kwargs):
        self._net_wake.set()

    def on_bt_pan_disconnected(self, *args, **kwargs):
        self._net_wake.set()

    # --- UI ---------------------------------------------------------------

    def on_ui_setup(self, ui):
        if self._show_iface:
            self._has_iface = True
            ui.add_element(
                'control_iface',
                Text(
                    color=view.BLACK,
                    value=self.iface,
                    position=self._pos_iface,
                    font=fonts.Small,
                ),
            )

        if self._show_blinds:
            self._has_blinds = True
            ui.add_element(
                'control_blinds',
                Text(
                    color=view.BLACK,
                    value='b:0',
                    position=self._pos_blinds,
                    font=fonts.Small,
                ),
            )

        if self._show_wan:
            self._has_wan = True
            ui.add_element(
                'control_wan',
                LabeledValue(
                    color=view.BLACK,
                    label='WAN:',
                    value='--',
                    position=self._pos_wan,
                    label_font=fonts.Bold,
                    text_font=fonts.Medium,
                ),
            )

        if self._show_ip:
            self._has_ip = True
            ui.add_element(
                'control_ip',
                LabeledValue(
                    color=view.BLACK,
                    label='IP:',
                    value='--',
                    position=self._pos_ip,
                    label_font=fonts.Bold,
                    text_font=fonts.Medium,
                ),
            )

    def on_ui_update(self, ui):
        if self._has_iface:
            ui.set('control_iface', self.iface)

        if self._has_blinds:
            blind_count = 0
            if self.agent:
                blind_count = getattr(getattr(self.agent, '_epoch', None), 'blind_for', 0) or 0
            ui.set('control_blinds', f"b:{blind_count}")

        bt = self._bt_state() if (self._has_wan or self._has_ip) else None

        if self._has_wan:
            if bt:
                status = 'BT'
            else:
                with self._net_lock:
                    status = self._net_status
            ui.set('control_wan', status)

        if self._has_ip:
            ip = (bt or {}).get('ip') or self._local_ip()
            ui.set('control_ip', ip or '--')

    def on_unload(self, ui):
        self._net_stop.set()
        self._net_wake.set()
        if self._net_thread is not None and self._net_thread.is_alive():
            self._net_thread.join(timeout=self._RAW_TIMEOUT + 1)

        with ui._lock:
            if self._has_iface:
                ui.remove_element('control_iface')
            if self._has_blinds:
                ui.remove_element('control_blinds')
            if self._has_wan:
                ui.remove_element('control_wan')
            if self._has_ip:
                ui.remove_element('control_ip')
        logging.info("[control] Plugin unloaded")

    # --- Config helpers ----------------------------------------------------

    def _parse_pos(self, key):
        """Return an (x, y) tuple for ``key``, falling back to the default.

        Accepts either a "x,y" string or a two-element list, and quietly
        ignores malformed values so a typo in config.toml cannot prevent
        the plugin from loading.
        """
        raw = self.options.get(key, self._DEFAULTS[key])

        if isinstance(raw, str):
            parts = [p.strip() for p in raw.split(',')]
        elif isinstance(raw, (list, tuple)):
            parts = list(raw)
        else:
            parts = []

        if len(parts) != 2:
            logging.warning(
                "[control] invalid %s=%r, using default %s",
                key, raw, self._DEFAULTS[key],
            )
            parts = [p.strip() for p in self._DEFAULTS[key].split(',')]

        try:
            return int(parts[0]), int(parts[1])
        except (TypeError, ValueError):
            logging.warning(
                "[control] non-numeric %s=%r, using default %s",
                key, raw, self._DEFAULTS[key],
            )
            dx, dy = self._DEFAULTS[key].split(',')
            return int(dx), int(dy)

    # --- bt-pan / IP -------------------------------------------------------

    def _bt_state(self):
        """Return bt-pan state dict while it is connected, else None."""
        plugin = plugins.loaded.get(self._BT_PLUGIN)
        if plugin is None:
            return None
        try:
            st = plugin.get_state()
        except Exception:
            return None
        return st if st.get('state') == 'connected' else None

    @staticmethod
    def _iface_ipv4(iface):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            r = fcntl.ioctl(
                s.fileno(), 0x8915, struct.pack('256s', iface[:15].encode()),
            )  # SIOCGIFADDR
            ip = socket.inet_ntoa(r[20:24])
            return None if ip.startswith('169.254.') else ip
        except OSError:
            return None
        finally:
            s.close()

    def _local_ip(self):
        """Primary IPv4, no subprocess and no traffic (safe in on_ui_update)."""
        # Source address the kernel would pick for the default route.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(self._RAW_PROBE)  # UDP connect sends nothing
            ip = s.getsockname()[0]
            if ip and not ip.startswith('169.254.'):
                return ip
        except OSError:
            pass  # no default route
        finally:
            s.close()

        # Fallback: first non-loopback interface that has an IPv4.
        try:
            for name in sorted(os.listdir('/sys/class/net')):
                if name == 'lo':
                    continue
                ip = self._iface_ipv4(name)
                if ip:
                    return ip
        except OSError:
            pass
        return None

    # --- Network probing ---------------------------------------------------

    def _net_loop(self):
        while not self._net_stop.is_set():
            try:
                status = self._probe_network()
            except Exception:
                logging.exception("[control] network probe failed")
                status = '??'
            with self._net_lock:
                self._net_status = status
            self._net_wake.wait(self._net_interval)
            self._net_wake.clear()

    def _probe_network(self):
        dns_ok = self._dns_ok()
        raw_ok = self._raw_ok()

        if dns_ok and raw_ok:
            return 'OK'    # full internet: DNS resolves and IPs are reachable
        if raw_ok:
            return 'DNS'   # can reach an IP, but names do not resolve
        if dns_ok:
            return '??'    # names resolve, but nothing is reachable
        return 'NO'        # no internet at all

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
