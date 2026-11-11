"""
bt-pan: lightweight Bluetooth PAN (NAP) auto-connect for pwnagotchi.

Requirements:
    sudo apt-get install -y bluez python3-dbus iproute2 dhcpcd5   # or isc-dhcp-client

One-time pairing (phone: enable Bluetooth tethering first):
    sudo bluetoothctl
    > power on
    > agent on
    > default-agent
    > scan on
    > pair XX:XX:XX:XX:XX:XX
    > trust XX:XX:XX:XX:XX:XX
    > exit

After that the plugin picks the trusted device that exposes NAP by itself.

Config (config.toml):
    [main.plugins.bt-pan]
    enabled = true
    dhcp = true        # request an IP from the phone automatically
    interval = 60      # seconds between connection checks/attempts (min 20)

For other plugins (the control plugin uses these):
    plugins.loaded["bt-pan"].get_state()   -> state, device, mac, iface, ip, error
        state: starting | no-device | connecting | connected | disconnected | error
        get_state() is cheap and safe to call from on_ui_update.
    Events (optional hooks, used by control to refresh WAN immediately):
        on_bt_pan_connected(self, _, data)     data: mac, device, iface, ip
        on_bt_pan_disconnected(self, _, data)  data: mac, device
"""

import fcntl
import ipaddress
import logging
import os
import shutil
import socket
import struct
import subprocess
import threading
import time

from pwnagotchi import plugins
from pwnagotchi.plugins import Plugin

try:
    import dbus

    DBUS_OK = True
except ImportError:
    DBUS_OK = False

BLUEZ = "org.bluez"
NAP_UUID = "00001116-0000-1000-8000-00805f9b34fb"

DEFAULT_INTERVAL = 60
MIN_INTERVAL = 20  # lower values would steal radio time from handshake capture
START_DELAY = 10  # let pwnagotchi/wifi settle before the first attempt
NAP_TIMEOUT = 12  # was 20 + up to 3 retries in bt-tether
PAN_WAIT = 4  # wait for bnepX after NAP connects
DHCP_TIMEOUT = 15  # was 30
LEASE_WAIT = 30  # poll for the lease (exits the moment it arrives; a busy Zero W can be slow)
MAX_BACKOFF = 5  # failed attempts back off up to interval * MAX_BACKOFF
WEDGE_RESTART = 4  # NoReply/busy streak before restarting bluetoothd once


def _run(cmd, timeout=3):
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        ).returncode
    except Exception:
        return None


class BTPan(Plugin):
    __author__ = "flippedgannet"
    __version__ = "1.0.0"
    __license__ = "GPL3"
    __description__ = "Lightweight Bluetooth PAN auto-connect (pair via bluetoothctl)"

    # ------------------------------------------------------------------ setup
    def on_loaded(self):
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._state = {
            "state": "starting",
            "device": None,
            "mac": None,
            "iface": None,
            "ip": None,
            "error": None,
        }
        self._up = False
        self._fails = 0
        self._wedge = 0
        self._last_err = None

        self.dhcp = bool(self.options.get("dhcp", True))
        try:
            interval = int(self.options.get("interval", DEFAULT_INTERVAL))
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL
        self.interval = max(MIN_INTERVAL, interval)

        if not DBUS_OK:
            logging.error("[bt-pan] Missing python3-dbus: sudo apt install python3-dbus")
            self._set(state="error", error="Missing python3-dbus")
            return

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logging.info(f"[bt-pan] Loaded (dhcp={self.dhcp}, interval={self.interval}s)")

    def on_unload(self, ui):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        try:
            iface = self._pan_iface()
            if iface and self.dhcp:
                self._dhcp_release(iface)
        except Exception as e:
            logging.debug(f"[bt-pan] Unload cleanup: {e}")
        logging.info("[bt-pan] Unloaded")

    # ----------------------------------------------------------- public state
    def get_state(self):
        """Snapshot for other plugins (e.g. control). Cheap: no subprocess.

        The monitor only refreshes every `interval` seconds, so a dropped link
        would otherwise keep reporting "connected" for up to a minute. The PAN
        interface disappearing is an instant, free signal, so check it here.
        """
        with self._lock:
            st = dict(self._state)
        if st["state"] == "connected" and not os.path.isdir(
            f"/sys/class/net/{st['iface']}"
        ):
            st.update(state="disconnected", iface=None, ip=None)
        return st

    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)

    def _emit(self, name, data):
        try:
            plugins.on(name, None, data)
        except Exception as e:
            logging.debug(f"[bt-pan] Emit {name} failed: {e}")

    # ------------------------------------------------------------------- loop
    def _loop(self):
        if self._stop.wait(START_DELAY):
            return
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                self._on_error(e)
            self._stop.wait(self._next_delay())

    def _next_delay(self):
        if self._up or not self._fails:
            return self.interval
        return self.interval * min(1 + self._fails, MAX_BACKOFF)

    def _tick(self):
        adapter, devices = self._snapshot()
        if adapter is None:
            raise RuntimeError("No bluetooth adapter")
        if not adapter[1]:
            self._power_on(adapter[0])

        if not devices:
            self._mark_down()
            self._set(state="no-device", device=None, mac=None, iface=None, ip=None)
            return

        dev = devices[0]
        iface = self._pan_iface()
        ip = self._addr(iface) if iface else None

        if iface and dev["connected"] and (ip or not self.dhcp):
            self._fails = self._wedge = 0
            self._last_err = None
            self._mark_up(dev, iface, ip)
            return

        self._mark_down()
        self._set(
            state="connecting", device=dev["name"], mac=dev["mac"], iface=None, ip=None
        )
        self._attempt(dev, iface)

    def _attempt(self, dev, iface):
        first = not self._fails  # announce a cycle once, not on every retry
        if not iface:
            if first:
                logging.info(f"[bt-pan] Connecting to {dev['name']}...")
            # Half-open ACL (connected, no PAN) makes ConnectProfile hang: clear it
            # now, while nothing is pending, so it can't wedge BlueZ.
            if dev["connected"] and self._fails >= 2:
                self._bluez_call(dev["path"], "Disconnect")
                self._stop.wait(1)
            self._connect_nap(dev["path"])
            iface = self._wait_iface()
            if not iface:
                raise RuntimeError("NAP connected but no PAN interface appeared")

        ip = None
        if self.dhcp:
            if first:
                logging.info(f"[bt-pan] Link up on {iface}, waiting for IP from {dev['name']}...")
            ip = self._dhcp(iface)

        self._fails = self._wedge = 0
        self._last_err = None
        self._mark_up(dev, iface, ip)

    # ------------------------------------------------------- state transitions
    def _mark_up(self, dev, iface, ip):
        self._set(
            state="connected",
            device=dev["name"],
            mac=dev["mac"],
            iface=iface,
            ip=ip,
            error=None,
        )
        if not self._up:
            self._up = True
            logging.info(f"[bt-pan] Connected: {dev['name']} via {iface} ip={ip}")
            self._emit(
                "bt_pan_connected",
                {"mac": dev["mac"], "device": dev["name"], "iface": iface, "ip": ip},
            )

    def _mark_down(self):
        if self._up:
            self._up = False
            st = self.get_state()
            logging.warning(f"[bt-pan] Connection to {st['device']} lost")
            self._emit(
                "bt_pan_disconnected", {"mac": st["mac"], "device": st["device"]}
            )
            self._set(state="disconnected", iface=None, ip=None)

    def _on_error(self, e):
        msg = str(e)
        self._fails += 1
        self._mark_down()

        if any(
            k in msg
            for k in ("NoReply", "InProgress", "br-connection-busy", "NotReady", "not powered")
        ):
            self._wedge += 1
        else:
            self._wedge = 0

        if "create-socket" in msg or "profile-unavailable" in msg:
            hint = "Tethering is off on the phone"
        elif "page-timeout" in msg or "Host is down" in msg:
            hint = "Phone out of range or Bluetooth off"
        elif "Authentication" in msg or "refused" in msg.lower():
            hint = "Pairing rejected, re-pair with bluetoothctl"
        else:
            hint = msg.strip().replace("\n", " ")[:100]
            hint = hint[:1].upper() + hint[1:]

        if hint != self._last_err:  # log once per distinct problem, not every cycle
            logging.warning(f"[bt-pan] {hint}")
            self._last_err = hint
        else:
            logging.debug(f"[bt-pan] {hint}")
        self._set(state="error", error=hint)

        if self._wedge >= WEDGE_RESTART:
            self._restart_bluetooth()

    # ------------------------------------------------------------------ BlueZ
    def _snapshot(self):
        """One D-Bus call -> (adapter(path, powered) | None, usable devices)."""
        bus = dbus.SystemBus()
        mgr = dbus.Interface(
            bus.get_object(BLUEZ, "/"), "org.freedesktop.DBus.ObjectManager"
        )
        adapter, devices = None, []
        for path, ifs in mgr.GetManagedObjects().items():
            a = ifs.get("org.bluez.Adapter1")
            if a is not None and adapter is None:
                adapter = (str(path), bool(a.get("Powered", False)))
            d = ifs.get("org.bluez.Device1")
            if d is None or not (d.get("Paired") and d.get("Trusted")):
                continue
            if NAP_UUID not in [str(u).lower() for u in d.get("UUIDs", [])]:
                continue
            mac = str(d.get("Address", "")).upper()
            devices.append(
                {
                    "path": str(path),
                    "mac": mac,
                    "name": str(d.get("Alias") or d.get("Name") or mac),
                    "connected": bool(d.get("Connected", False)),
                }
            )
        devices.sort(key=lambda x: (not x["connected"], x["mac"]))
        return adapter, devices

    def _power_on(self, adapter_path):
        bus = dbus.SystemBus()
        props = dbus.Interface(
            bus.get_object(BLUEZ, adapter_path), "org.freedesktop.DBus.Properties"
        )
        try:
            props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))
        except dbus.exceptions.DBusException:
            _run(["rfkill", "unblock", "bluetooth"])
            props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))

    def _bluez_call(self, path, method):
        try:
            bus = dbus.SystemBus()
            dev = dbus.Interface(bus.get_object(BLUEZ, path), "org.bluez.Device1")
            getattr(dev, method)(timeout=5)
        except Exception as e:
            logging.debug(f"[bt-pan] {method} failed: {e}")

    def _connect_nap(self, path):
        """Blocking, bounded by NAP_TIMEOUT (runs in our own thread, not the agent's)."""
        bus = dbus.SystemBus()
        dev = dbus.Interface(bus.get_object(BLUEZ, path), "org.bluez.Device1")
        dev.ConnectProfile(NAP_UUID, timeout=NAP_TIMEOUT)

    def _restart_bluetooth(self):
        logging.warning("[bt-pan] Bluetooth looks wedged, restarting bluetoothd once")
        _run(["systemctl", "restart", "bluetooth"], timeout=20)
        self._wedge = 0
        for _ in range(8):
            if self._stop.wait(1):
                return
            try:
                adapter, _ = self._snapshot()
                if adapter and adapter[1]:
                    return
            except Exception:
                pass

    # ---------------------------------------------------------------- network
    @staticmethod
    def _pan_iface():
        try:
            for name in sorted(os.listdir("/sys/class/net")):
                if name.startswith(("bnep", "bt-pan")):
                    return name
        except OSError:
            pass
        return None

    def _wait_iface(self):
        deadline = time.monotonic() + PAN_WAIT
        while time.monotonic() < deadline and not self._stop.is_set():
            iface = self._pan_iface()
            if iface:
                return iface
            self._stop.wait(0.25)
        return self._pan_iface()

    @staticmethod
    def _ipv4(iface):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            r = fcntl.ioctl(
                s.fileno(), 0x8915, struct.pack("256s", iface[:15].encode())
            )  # SIOCGIFADDR
            ip = socket.inet_ntoa(r[20:24])
            return None if ip.startswith("169.254.") else ip
        except OSError:
            return None
        finally:
            s.close()

    @staticmethod
    def _ipv6(iface):
        """Global IPv6 (IPv6-only tethering via SLAAC) from /proc, no subprocess."""
        try:
            with open("/proc/net/if_inet6") as f:
                for line in f:
                    p = line.split()
                    if len(p) >= 6 and p[5] == iface and p[3] == "00":
                        return str(ipaddress.IPv6Address(int(p[0], 16)))
        except OSError:
            pass
        return None

    def _addr(self, iface):
        return self._ipv4(iface) or self._ipv6(iface)

    def _wait_addr(self, iface, timeout):
        deadline = time.monotonic() + timeout
        while True:
            ip = self._addr(iface)
            if (
                ip
                or time.monotonic() >= deadline
                or self._stop.is_set()
                or not os.path.isdir(f"/sys/class/net/{iface}")
            ):
                return ip
            self._stop.wait(0.5)

    def _dhcp(self, iface):
        _run(["ip", "link", "set", iface, "up"])
        if self._addr(iface):
            return self._addr(iface)

        if shutil.which("dhcpcd"):
            conf = "/tmp/bt-pan-dhcpcd.conf"
            with open(conf, "w") as f:
                f.write("noarp\n")  # no ARP probe on a point-to-point link
            cmds = [
                ["dhcpcd", "-4", "-f", conf, "-n", iface],
                ["dhcpcd", "-4", "-n", iface],
            ]
        elif shutil.which("dhclient"):
            conf = "/tmp/bt-pan-dhclient.conf"
            with open(conf, "w") as f:
                f.write("initial-interval 1;\nbackoff-cutoff 3;\ntimeout 12;\n")
            _run(["pkill", "-f", f"dhclient.* {iface}$"])
            cmds = [["dhclient", "-4", "-1", "-cf", conf, iface]]
        else:
            raise RuntimeError("No DHCP client (install dhcpcd5 or isc-dhcp-client)")

        for cmd in cmds:
            if _run(cmd, timeout=DHCP_TIMEOUT) == 0 or self._stop.is_set():
                break

        # Wait regardless of the exit code: the client keeps negotiating in the
        # background, and a lease that is merely slow is not a failure.
        ip = self._wait_addr(iface, LEASE_WAIT)
        if not ip:
            raise RuntimeError("No IP from phone (is Bluetooth tethering on?)")
        return ip

    def _dhcp_release(self, iface):
        if shutil.which("dhcpcd"):
            _run(["dhcpcd", "-k", iface])
        _run(["pkill", "-f", f"dhclient.* {iface}$"])
