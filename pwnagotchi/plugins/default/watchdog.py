import logging, os, re, subprocess, threading, time
from collections import deque
import pwnagotchi
from pwnagotchi import plugins
import pwnagotchi.ui.faces as faces

class Watchdog(plugins.Plugin):
    __author__ = "flippedgannet"
    __version__ = "2.0.0"
    __license__ = "GPL3"
    __description__ = "Combined Wi-Fi, Broadcom and Bettercap watchdog"

    KERNEL_PATTERN = re.compile(r"brcmf_cfg80211_nexmon_set_channel.*?Set Channel failed", re.I)
    HOP_PATTERN = re.compile(r"wifi error while hopping to channel", re.I)
    BUSY_PATTERN = re.compile(r"wifi error while hopping to channel.*busy", re.I)

    def __init__(self):
        self.agent = None
        self.interface = self.monitor_interface = "mon0"
        self.brcmfmac = False
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.worker = None
        self.pending = None
        self.busy = False
        self.status, self.detail = "INIT", "initializing"
        self.resets = self.blind_epochs = 0
        self.last_check = self.last_scan = self.last_recovery = 0.0
        self.bettercap_down_since = None
        self.reboot_times = deque()
        self.session_probe_lock = threading.Lock()
        self.session_probe_thread = None
        self.session_probe_event = None
        self.session_probe_result = None

    def on_loaded(self):
        self.options = dict(self.options or {})
        main = getattr(pwnagotchi, "config", {}).get("main", {})
        self.interface = str(self.options.get("interface") or main.get("iface") or "mon0")
        self.monitor_interface = str(self.options.get("monitor_interface") or self.interface)
        self.check_interval = self._integer("check_interval", 1, 1, 60)
        self.journal_interval = self._integer("journal_interval", 5, 1, 60)
        self.cooldown = self._integer("recovery_cooldown", 180, 30, 3600)
        self.blind_threshold = self._integer("blind_threshold", 2, 1, 20)
        self.bettercap_grace = self._integer("bettercap_grace", 180, 30, 900)
        self.reboot_window = self._integer("reboot_window", 1800, 60, 86400)
        self.reboot_max = self._integer("reboot_max", 3, 0, 20)
        self.reboot_on_failure = bool(self.options.get("reboot_on_failure", True))
        self.marker = self.options.get("maintenance_marker",
            "/run/pwnagotchi-automatic-updates-maintenance")
        self.crash_log = self.options.get("crash_log", "/var/log/pwnagotchi_crashes.log")
        self.brcmfmac = self._detect_brcmfmac()
        self.status, self.detail = "READY", ("brcmfmac" if self.brcmfmac else "generic adapter")
        self.worker = threading.Thread(target=self._worker_loop, daemon=True, name="watchdog-worker")
        self.worker.start()
        logging.info("[watchdog] loaded iface=%s monitor=%s brcmfmac=%s",
                     self.interface, self.monitor_interface, self.brcmfmac)

    def _integer(self, key, default, low, high):
        try: return max(low, min(high, int(self.options.get(key, default))))
        except (TypeError, ValueError): return default

    def _run(self, args, timeout=8):
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)

    def _detect_brcmfmac(self):
        path = "/sys/class/net/wlan0/device/driver"
        try:
            if os.path.exists(path):
                return os.path.basename(os.readlink(path)) == "brcmfmac"
        except OSError:
            pass
        try:
            result = self._run(["lsmod"], 3)
            return result.returncode == 0 and any(
                line.split() and line.split()[0] == "brcmfmac"
                for line in result.stdout.splitlines())
        except Exception:
            return False

    def on_ready(self, agent):
        self.agent = agent
        self._queue("watchdog", "startup check")

    def on_epoch(self, agent, epoch, epoch_data):
        if self.stop.is_set() or os.path.exists(self.marker): return
        self.agent = agent
        now = time.time()
        if now - self.last_check < self.check_interval: return
        self.last_check = now
        self.blind_epochs = int(getattr(getattr(agent, "_epoch", None), "blind_for", 0) or 0)
        if self.blind_epochs >= self.blind_threshold:
            self._queue("watchdog", "blind epoch threshold reached")
            return
        if now - self.last_scan < self.journal_interval or now - self.last_recovery < self.cooldown:
            return
        self.last_scan = now
        if self.brcmfmac:
            kernel = self._journal(kernel=True)
            matches = len(self.KERNEL_PATTERN.findall(kernel))
            if matches >= 3:
                logging.warning("[watchdog] FixBRCMF signature: %d matches in last 10 kernel lines", matches)
                self._queue("brcmf", "Nexmon Set Channel failure signature")
                return
        journal = self._journal(kernel=False)
        if len(self.HOP_PATTERN.findall(journal)) >= 5:
            self._queue("recon", "channel hopping errors >= 5 in journal tail")

    def on_bcap_sys_log(self, agent, event):
        if os.path.exists(self.marker): return
        data = event.get("data") if isinstance(event, dict) else None
        message = data.get("Message") if isinstance(data, dict) else None
        if (isinstance(message, str) and self.BUSY_PATTERN.search(message)
                and time.time() - self.last_recovery >= self.cooldown):
            self._queue("recon", "Bettercap reported busy channel hopping")

    def get_status(self):
        with self.lock:
            return {"status": self.status, "detail": self.detail,
                    "resets": self.resets, "busy": self.busy,
                    "blind_epochs": self.blind_epochs}

    def _set(self, status, detail):
        with self.lock:
            self.status, self.detail = status, str(detail)[:80]

    def _queue(self, action, reason):
        with self.lock:
            if self.busy or self.pending: return
            self.pending = (action, reason)
            self.status, self.detail = "WORK", reason[:80]
            self.wake.set()

    def _worker_loop(self):
        while not self.stop.is_set():
            self.wake.wait(1)
            self.wake.clear()
            if self.stop.is_set(): break
            with self.lock:
                job, self.pending = self.pending, None
                if not job: continue
                self.busy = True
            try:
                if os.path.exists(self.marker):
                    self._set("PAUSE", "maintenance marker present")
                elif job[0] == "brcmf": self._brcmf_recovery(job[1])
                elif job[0] == "recon": self._recon_flip(job[1])
                else: self._watchdog_check(job[1])
            except Exception:
                logging.exception("[watchdog] worker action failed")
                self._set("ERR", "worker exception")
            finally:
                with self.lock: self.busy = False

    def _journal(self, kernel):
        args = ["journalctl", "-n10"]
        if kernel: args.append("-k")
        args.append("--no-pager")
        try:
            result = self._run(args, 4)
            return result.stdout[-12000:] if result.returncode == 0 else ""
        except Exception as exc:
            logging.warning("[watchdog] journal read failed: %s", exc)
            return ""

    def _exists(self):
        return os.path.exists("/sys/class/net/" + self.monitor_interface)

    def _up(self):
        try:
            r = self._run(["ip", "-brief", "link", "show", self.monitor_interface], 3)
            return r.returncode == 0 and "UP" in r.stdout
        except Exception: return False

    def _bettercap(self, command):
        if self.agent is None: raise RuntimeError("Bettercap agent unavailable")
        result = self.agent.run(command)
        if not isinstance(result, dict) or not result.get("success", False):
            raise RuntimeError("Bettercap command failed: %r" % (result,))
        return result

    def _watchdog_check(self, reason):
        if os.path.exists(self.marker):
            self._set("PAUSE", "maintenance in progress"); return
        now = time.time()
        try:
            active = self._run(["systemctl", "is-active", "--quiet", "bettercap"], 3).returncode == 0
        except Exception: active = False
        if not active:
            if self.bettercap_down_since is None: self.bettercap_down_since = now
            if now - self.bettercap_down_since < self.bettercap_grace:
                self._set("CHK", "waiting for Bettercap grace period"); return
            self._diagnostics("Bettercap service inactive")
            self._restart_bettercap(); return
        self.bettercap_down_since = None
        if self.brcmfmac and not self._exists():
            self._brcmf_recovery("monitor interface missing"); return
        if self.blind_epochs >= self.blind_threshold:
            if not self._up() and self.brcmfmac:
                self._brcmf_recovery("monitor interface down")
            else:
                self._recon_flip("blind epoch threshold reached")
            return
        probe = self._probe_bettercap_session(timeout=2.0)
        if probe is not True:
            logging.warning("[watchdog] Bettercap API probe failed or timed out")
            self._restart_bettercap()
            return
        self._set("OK", "monitoring healthy")

    def _probe_bettercap_session(self, timeout=2.0):
        """Run potentially blocking session() outside the watchdog worker."""
        with self.session_probe_lock:
            if self.session_probe_thread and self.session_probe_thread.is_alive():
                return False
            event = threading.Event()
            self.session_probe_event = event
            self.session_probe_result = None

            def probe():
                try:
                    self.session_probe_result = (
                        self.agent is not None and self.agent.session() is not None
                    )
                except Exception as exc:
                    logging.debug("[watchdog] session probe exception: %s", exc)
                    self.session_probe_result = False
                finally:
                    event.set()

            self.session_probe_thread = threading.Thread(
                target=probe, daemon=True, name="watchdog-session-probe"
            )
            self.session_probe_thread.start()
        if not event.wait(timeout):
            return False
        return self.session_probe_result is True

    def _recon_flip(self, reason):
        self.last_recovery = time.time()
        self._set("FIX", "restarting wifi.recon")
        try:
            self._bettercap("wifi.recon off; wifi.recon on")
            self._set("OK", "wifi.recon restarted")
        except Exception as exc:
            logging.warning("[watchdog] recon flip failed (%s): %s", reason, exc)
            if self.brcmfmac: self._brcmf_recovery("recon restart failed")
            else: self._restart_bettercap()

    def _find_phy(self):
        result = self._run(["iw", "phy"], 5)
        match = re.search(r"^Wiphy\s+(\S+)", result.stdout, re.M)
        return match.group(1) if match else None

    def _brcmf_recovery(self, reason):
        if not self.brcmfmac:
            self._set("SKIP", "Broadcom recovery disabled for external adapter"); return
        if time.time() - self.last_recovery < self.cooldown:
            self._set("WAIT", "recovery cooldown"); return
        self.last_recovery = time.time()
        self._set("FIX", "recovering Broadcom monitor interface")
        logging.warning("[watchdog] Broadcom recovery: %s", reason)
        try:
            try: self._bettercap("wifi.recon off")
            except Exception as exc: logging.debug("[watchdog] recon-off: %s", exc)
            if self._exists():
                self._run(["ip", "link", "set", self.monitor_interface, "down"], 4)
                self._run(["iw", "dev", self.monitor_interface, "del"], 4)
            if self.monitor_interface == "wlan0mon":
                self._run(["monstop"], 15)
                started = self._run(["monstart"], 30)
                if started.returncode: raise RuntimeError("monstart failed: " + started.stderr[-400:])
            else:
                unloaded = self._run(["modprobe", "-r", "brcmfmac"], 12)
                if unloaded.returncode:
                    logging.warning("[watchdog] modprobe -r: %s", unloaded.stderr.strip())
                loaded = self._run(["modprobe", "brcmfmac"], 15)
                if loaded.returncode: raise RuntimeError("modprobe brcmfmac failed: " + loaded.stderr[-400:])
                time.sleep(5)
                phy = self._find_phy()
                if not phy: raise RuntimeError("wireless PHY not found")
                add = self._run(["iw", "phy", phy, "interface", "add",
                                 self.monitor_interface, "type", "monitor"], 8)
                if add.returncode and not self._exists():
                    raise RuntimeError("monitor interface creation failed: " + add.stderr[-400:])
                up = self._run(["ip", "link", "set", self.monitor_interface, "up"], 5)
                if up.returncode: raise RuntimeError("monitor interface could not be brought up")
            if not self._exists(): raise RuntimeError("monitor interface still missing")
            self._bettercap("set wifi.interface %s" % self.monitor_interface)
            time.sleep(2)
            self._bettercap("wifi.clear; wifi.recon on")
            self.resets += 1
            self._set("OK", "Broadcom recovery complete")
        except Exception as exc:
            logging.exception("[watchdog] Broadcom recovery failed")
            self._diagnostics("Broadcom recovery failed: %s" % exc)
            if self.reboot_on_failure: self._reboot("Broadcom recovery failed")
            else: self._set("ERR", "Broadcom recovery failed")

    def _restart_bettercap(self):
        self._set("FIX", "restarting Bettercap service")
        try:
            result = self._run(["systemctl", "restart", "bettercap"], 20)
            if result.returncode == 0:
                self.bettercap_down_since = None
                self._set("OK", "Bettercap service restarted"); return
            logging.error("[watchdog] Bettercap restart: %s", result.stderr[-400:])
        except Exception: logging.exception("[watchdog] Bettercap restart failed")
        self._diagnostics("Bettercap restart failed")
        if self.reboot_on_failure: self._reboot("Bettercap restart failed")
        else: self._set("ERR", "Bettercap restart failed")

    def _diagnostics(self, reason):
        try:
            chunks = ["\n===== %s | %s =====\n" % (time.strftime("%F %T"), reason)]
            for path in ("/etc/pwnagotchi/log/pwnagotchi.log", "/etc/pwnagotchi/log/bettercap.log"):
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        chunks.append("".join(deque(f, maxlen=40)))
                except OSError: chunks.append("[missing %s]\n" % path)
            for args in (["dmesg", "--ctime"], ["journalctl", "-u", "bettercap", "-n40", "--no-pager"]):
                try: chunks.append(self._run(args, 5).stdout[-10000:])
                except Exception as exc: chunks.append("diagnostic failed: %s\n" % exc)
            os.makedirs(os.path.dirname(self.crash_log), exist_ok=True)
            with open(self.crash_log, "a", encoding="utf-8") as f: f.write("\n".join(chunks))
        except Exception: logging.exception("[watchdog] diagnostic write failed")

    def _reboot(self, reason):
        now = time.time()
        while self.reboot_times and now - self.reboot_times[0] > self.reboot_window:
            self.reboot_times.popleft()
        if len(self.reboot_times) >= self.reboot_max:
            self._set("LOCK", "reboot budget exhausted")
            logging.critical("[watchdog] reboot suppressed: %s", reason); return
        self.reboot_times.append(now)
        self._set("BOOT", reason)
        self._diagnostics(reason)
        try:
            if self.agent and hasattr(self.agent, "view"):
                ui = self.agent.view()
                if ui: ui.update(force=True, new_data={
                    "status": "Watchdog recovery failed. Rebooting.", "face": faces.BROKEN})
        except Exception: pass
        logging.critical("[watchdog] rebooting: %s", reason)
        pwnagotchi.reboot()

    def on_unload(self, ui):
        self.stop.set()
        self.wake.set()
        if self.worker and self.worker.is_alive(): self.worker.join(timeout=2)
        logging.info("[watchdog] unloaded")
