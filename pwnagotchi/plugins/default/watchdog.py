"""
watchdog: hardware monitor and interface auto-recovery for pwnagotchi.

Escalation ladder, evaluated once per epoch while the agent is blind:
    1. Monitor interface vanished from sysfs  -> recreate it, else reboot
    2. Bettercap REST API unresponsive        -> reboot
    3. Blind epochs reached the ceiling       -> reboot
    4. Otherwise                              -> restart a dead wifi.recon

Config (optional):
    [main.plugins.watchdog]
    enabled = true
    max_blind_epochs = 15
"""
import logging
import os
import threading
import time
from datetime import datetime

import pwnagotchi
import pwnagotchi.plugins as plugins

try:
    from pwnagotchi import _real_subprocess_run
except ImportError:  # framework without the patched subprocess
    from subprocess import run as _real_subprocess_run

DEFAULT_IFACE = 'wlan0mon'
DEFAULT_MON_START = '/usr/bin/monstart'
CRASH_LOG = '/var/log/pwnagotchi_crashes.log'
PWNAGOTCHI_LOG = '/var/log/pwnagotchi.log'

ACT_AT_BLIND = 2          # blind epochs before interface/API faults escalate
DEFAULT_MAX_BLIND = 15    # absolute blind ceiling
CRASH_TAIL_LINES = 30     # log lines copied into a crash report
DISPLAY_SETTLE = 1        # seconds the screen gets to show the status
RECOVERY_TIMEOUT = 10     # per recovery command

API_TIMEOUT = 5           # bettercap must answer within this
REBOOT_GRACE = 30         # time the framework reboot gets before we force it
SYNC_TIMEOUT = 10
FORCE_TIMEOUT = 10
CRASH_TAIL_BYTES = 16 * 1024
CRASH_LOG_MAX = 1024 * 1024


def _call_bounded(func, timeout):
    """Run func() in a daemon thread. Returns (finished, value, error).

    Whatever func blocks on (a REST call without timeout, a child stuck in
    uninterruptible I/O on a wedged wifi driver), the caller gets control back.
    """
    box = {}

    def target():
        try:
            box['value'] = func()
        except Exception as e:
            box['error'] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    return not thread.is_alive(), box.get('value'), box.get('error')


def _run_bounded(cmd, timeout):
    """Run cmd through the unpatched subprocess.run. Returns exit code or None."""
    finished, proc, error = _call_bounded(
        lambda: _real_subprocess_run(cmd, timeout=timeout, check=False, capture_output=True),
        timeout + 2,
    )
    if not finished:
        logging.warning(f"[watchdog] Command {cmd} did not finish in {timeout}s")
    elif error:
        logging.warning(f"[watchdog] Command {cmd} failed: {error}")
    return proc.returncode if proc is not None else None


class Watchdog(plugins.Plugin):
    __author__ = 'flippedgannet'
    __version__ = '1.4.0'
    __license__ = 'GPL3'
    __description__ = 'Hardware monitor and interface auto-recovery engine'

    def __init__(self):
        self.iface = DEFAULT_IFACE
        self.mon_start_cmd = DEFAULT_MON_START
        self.max_blind = DEFAULT_MAX_BLIND
        self.crash_log = CRASH_LOG
        self.is_rebooting = False
        self._worker = None
        self._lock = threading.Lock()

    def on_loaded(self):
        main = pwnagotchi.config.get('main', {})
        self.iface = main.get('iface') or self.options.get('interface', DEFAULT_IFACE)

        mon_start = main.get('mon_start_cmd')
        self.mon_start_cmd = DEFAULT_MON_START if mon_start is None else mon_start

        try:
            self.max_blind = int(self.options.get('max_blind_epochs', DEFAULT_MAX_BLIND))
        except (TypeError, ValueError):
            logging.warning(f"[watchdog] Invalid max_blind_epochs, using {DEFAULT_MAX_BLIND}")
            self.max_blind = DEFAULT_MAX_BLIND

        logging.info(f"[watchdog] Engine active. Monitoring interface: {self.iface}")

    # ---------------------------------------------------------------- epochs

    def on_epoch(self, agent, epoch, epoch_data):
        if self._busy():
            return

        blind = getattr(getattr(agent, '_epoch', None), 'blind_for', 0) or 0
        if blind == 0:
            return

        # 1. Zero-cost hardware check via sysfs
        if not self._iface_present():
            logging.warning(f"[watchdog] Wireless interface {self.iface} missing from sysfs! (blind={blind})")
            if blind >= ACT_AT_BLIND:
                self._dispatch(self._recover_or_reboot, agent)
            return

        # 2. Bettercap REST API reachability
        session = self._fetch_session(agent)
        if session is None:
            logging.warning(f"[watchdog] Bettercap REST API unresponsive (blind={blind})")
            if blind >= ACT_AT_BLIND:
                self._dispatch(self._reboot, agent, "Bettercap REST API operational breakdown")
            return

        # 3. Maximum permissible blind runtime
        if blind >= self.max_blind:
            logging.warning(
                f"[watchdog] Absolute blind ceiling breached ({blind}/{self.max_blind}). Triggering reboot."
            )
            self._dispatch(self._reboot, agent, f"Exceeded maximum blind threshold of {self.max_blind}")
            return

        # 4. In-place healing of the wifi capture loop
        self._heal_wifi_module(agent, session)

    # ------------------------------------------------------------ checks

    def _iface_present(self):
        return os.path.exists(f"/sys/class/net/{self.iface}")

    @staticmethod
    def _fetch_session(agent):
        """Bettercap session dict, or None if the API is down or hangs."""
        finished, session, error = _call_bounded(agent.session, API_TIMEOUT)
        if not finished:
            logging.warning(f"[watchdog] Bettercap API gave no answer within {API_TIMEOUT}s")
        elif error:
            logging.warning(f"[watchdog] Bettercap API error: {error}")
        return session if finished and error is None else None

    # ---------------------------------------------------------- recovery

    def _recover_or_reboot(self, agent):
        if self._recover_interface(agent):
            logging.info("[watchdog] Driver subsystem resurrected. Skipping hard reboot cycle.")
            return
        self._reboot(agent, f"Interface {self.iface} permanently detached")

    def _recover_interface(self, agent):
        """Recreate the monitor interface (`ifconfig up` cannot create a missing one)."""
        logging.info(f"[watchdog] Initiating monitor interface recovery for {self.iface}...")

        if self.mon_start_cmd:
            rc = _run_bounded(['/bin/sh', '-c', self.mon_start_cmd], RECOVERY_TIMEOUT)
            if rc != 0:
                logging.warning(f"[watchdog] mon_start_cmd finished with exit code {rc}")
        _run_bounded(['ip', 'link', 'set', self.iface, 'up'], RECOVERY_TIMEOUT)

        if not self._iface_present():
            logging.error(f"[watchdog] In-place interface recovery failed: {self.iface} is still missing")
            return False

        # Bettercap lost the old interface, so its recon has to be rebound.
        self._restart_recon(agent, stop_first=True)
        return True

    def _heal_wifi_module(self, agent, session):
        """Restart a dead wifi.recon thread."""
        wifi_active = any(
            m.get('name') == 'wifi' and m.get('running') for m in session.get('modules', [])
        )
        if not wifi_active:
            logging.info("[watchdog] wifi.recon processing thread halted. Forcing state reset...")
            self._restart_recon(agent, stop_first=False)

    @staticmethod
    def _restart_recon(agent, stop_first):
        def restart():
            agent._reset_wifi_settings()
            if stop_first and hasattr(agent, 'stop_module'):
                agent.stop_module('wifi.recon')
            agent.start_module('wifi.recon')

        finished, _, error = _call_bounded(restart, RECOVERY_TIMEOUT)
        if not finished:
            logging.warning(f"[watchdog] wifi.recon restart did not finish in {RECOVERY_TIMEOUT}s")
        elif error:
            logging.warning(f"[watchdog] wifi.recon restart failed: {error}")

    # ------------------------------------------------------------ reboot

    def _reboot(self, agent, reason):
        """Dump diagnostics, show the reason, reboot. Returns only if every reboot path failed."""
        self.is_rebooting = True
        logging.critical(f"[watchdog] CRITICAL STATE: {reason}. Initiating graceful sequence.")

        self._dump_crash_info(reason)
        view, original_update = self._freeze_display(agent, reason)

        finished, _, error = _call_bounded(agent._save_recovery_data, RECOVERY_TIMEOUT)
        if not finished or error:
            logging.warning(f"[watchdog] Session state synchronization failed: {error or 'timeout'}")

        time.sleep(DISPLAY_SETTLE)
        self._reboot_system()

        # Still alive after every attempt: resume normal operation instead of hanging.
        logging.critical("[watchdog] Every reboot path failed. Resuming monitoring.")
        if view is not None:
            view.update = original_update
        self.is_rebooting = False

    @staticmethod
    def _freeze_display(agent, reason):
        """Show the status and stop further redraws. Returns (view, original update)."""
        try:
            view = agent.view()
            original_update = view.update
            view.set('status', f"Fixing: {reason}")
            view.update(force=True)
            view.update = lambda *args, **kwargs: None
            return view, original_update
        except Exception as e:
            logging.warning(f"[watchdog] Failed to detach display rendering pipeline: {e}")
            return None, None

    def _reboot_system(self):
        """Ask the framework to reboot; force it if the system is still up after the grace period."""
        deadline = time.monotonic() + REBOOT_GRACE
        finished, _, error = _call_bounded(pwnagotchi.reboot, REBOOT_GRACE)

        if error:
            logging.error(f"[watchdog] Core reboot failed: {error}. Cascading to raw shell backup.")
        else:
            # reboot() may return as soon as the shutdown is merely requested.
            time.sleep(max(0, deadline - time.monotonic()))
            state = "returned" if finished else "has not returned"
            logging.error(
                f"[watchdog] System still up {REBOOT_GRACE}s after the core reboot request ({state}). Forcing reboot."
            )

        self._force_reboot()

    @staticmethod
    def _force_reboot():
        _call_bounded(os.sync, SYNC_TIMEOUT)
        _run_bounded(['reboot', '-f'], FORCE_TIMEOUT)

        # reboot -f did not take effect either: ask the kernel directly.
        try:
            with open('/proc/sysrq-trigger', 'w') as f:
                f.write('b')
        except OSError as e:
            logging.critical(f"[watchdog] Complete hardware kernel panic state. Force drop: {e}")

    # ------------------------------------------------------- diagnostics

    def _dump_crash_info(self, reason):
        """Append a crash report (reason + log tail) and make sure it reaches the disk."""
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            entry = f"=== WATCHDOG CRASH REPORT [{now}] :: Reason: {reason} ===\n"
            entry += self._tail(PWNAGOTCHI_LOG, CRASH_TAIL_LINES)

            self._rotate_crash_log()
            with open(self.crash_log, 'a', encoding='utf-8') as f:
                f.write(entry + "\n")
                f.flush()
                os.fsync(f.fileno())  # the reboot may follow immediately
        except Exception as e:
            logging.error(f"[watchdog] Diagnostics context dump processing error: {e}")

    @staticmethod
    def _tail(path, lines):
        """Last `lines` lines of a file, reading only its end."""
        try:
            with open(path, 'rb') as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - CRASH_TAIL_BYTES))
                data = f.read().decode('utf-8', errors='ignore')
        except OSError:
            return ''
        return "\n".join(data.splitlines()[-lines:]) + "\n"

    def _rotate_crash_log(self):
        try:
            if os.path.getsize(self.crash_log) > CRASH_LOG_MAX:
                os.replace(self.crash_log, self.crash_log + '.old')
        except OSError:
            pass

    # ----------------------------------------------------------- workers

    def _busy(self):
        return self._worker is not None and self._worker.is_alive()

    def _dispatch(self, task, *args):
        """Run slow recovery/reboot work off the epoch callback, one task at a time."""
        with self._lock:
            if self._busy():
                return
            self._worker = threading.Thread(
                target=self._run_task, args=(task, *args), daemon=True, name='watchdog-worker',
            )
            self._worker.start()

    def _run_task(self, task, *args):
        try:
            task(*args)
        except Exception:
            logging.exception("[watchdog] Recovery task crashed")
        finally:
            self.is_rebooting = False
