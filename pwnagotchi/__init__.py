import os
import logging
import subprocess
import time
import re
from pathlib import Path

from pwnagotchi._version import __version__

_name = None
config = None
_cpu_stats = {}

# Strict anti-bootloop constraints tracker
_BOOT_POLICY = {
    "file": "/root/.pwn_boot_rate",
    "window": 2000,  # Time window in seconds (approx 33 mins)
    "max": 2         # Max permitted reboots within the window
}

# Keep a reference to the real subprocess.run to use when reboots are allowed
_real_subprocess_run = subprocess.run


def _guarded_subprocess_run(*args, **kwargs):
    """
    A wrapper around subprocess.run that intercepts direct OS shutdown/reboot
    commands and evaluates them against the core bootloop policy.
    """
    cmd = args[0] if args else kwargs.get("args", [])

    # Check if the process is trying to issue a direct reboot/shutdown command
    is_reboot_cmd = any(isinstance(cmd, list) and token in ["shutdown", "reboot", "halt"] for token in cmd) or \
                    (isinstance(cmd, str) and any(token in cmd for token in ["shutdown", "reboot", "halt"]))

    if is_reboot_cmd:
        logging.info("[core] Intercepted direct OS reboot command from a subprocess call.")
        # Evaluate policy. If can_reboot() returns False, we drop the command entirely.
        if not can_reboot():
            logging.error("[core] Subprocess reboot intercepted and blocked by bootloop policy.")
            return subprocess.CompletedProcess(args=cmd, returncode=0)

    # Ensure execution flows to the real runner if reboot is allowed or if it is a standard command
    return _real_subprocess_run(*args, **kwargs)


# Apply the monkey patch immediately upon core initialization.
# This forces the watchdog's subprocess.run() calls to go through our policy engine.
subprocess.run = _guarded_subprocess_run


def can_reboot() -> bool:
    """
    Evaluates system safety policy to prevent destructive infinite reboot loops.
    Returns True if a reboot is allowed, False if the threshold is breached.
    """
    now = int(time.time())
    count, last_time = 0, 0

    if os.path.exists(_BOOT_POLICY["file"]):
        try:
            with open(_BOOT_POLICY["file"], 'r') as f:
                count, last_time = map(int, f.read().split())
        except (ValueError, OSError):
            pass

    # If the window has collapsed, clear historical penalties
    if now - last_time > _BOOT_POLICY["window"]:
        count = 0

    if count >= _BOOT_POLICY["max"]:
        logging.critical("[core] Anti-bootloop triggered! Reboot blocked to protect SD card.")
        return False

    try:
        with open(_BOOT_POLICY["file"], 'w') as f:
            f.write(f"{count + 1} {now}")
    except OSError as e:
        logging.error("[core] Failed to update boot rate database: %s", e)

    return True


def set_name(new_name):
    if new_name is None:
        return

    new_name = new_name.strip()
    if new_name == '':
        return

    if not re.match(r'^[a-zA-Z0-9\-]{2,25}$', new_name):
        logging.warning("name '%s' is invalid: min length is 2, max length 25, only a-zA-Z0-9- allowed", new_name)
        return

    current = name()
    if new_name != current:
        global _name

        logging.info("setting unit hostname '%s' -> '%s'", current, new_name)
        try:
            with open('/etc/hostname', 'wt') as fp:
                fp.write(new_name)

            with open('/etc/hosts', 'rt') as fp:
                prev = fp.read()
            logging.debug("old hosts:\n%s\n", prev)

            with open('/etc/hosts', 'wt') as fp:
                patched = prev.replace(current, new_name, -1)
                logging.debug("new hosts:\n%s\n", patched)
                fp.write(patched)

            _real_subprocess_run(["hostname", new_name], check=True)
            reboot(force=True)
        except Exception as e:
            logging.error("Failed to update hostname configuration: %s", e)


def name():
    global _name
    if _name is None:
        try:
            with open('/etc/hostname', 'rt') as fp:
                _name = fp.read().strip()
        except OSError:
            _name = "pwnagotchi"
    return _name


def uptime():
    try:
        with open('/proc/uptime', 'rt') as fp:
            # split('.')[0] gets the whole seconds string before the decimal point
            return int(fp.read().split('.')[0])
    except (OSError, ValueError, IndexError):
        return 0


def mem_usage():
    kb_mem_total = 1
    kb_mem_free = 0
    kb_main_buffers = 0
    kb_main_cached = 0

    try:
        with open('/proc/meminfo', 'rt') as fp:
            for line in fp:
                line = line.strip()
                # Split line into list: ['MemTotal:', '383484', 'kB']
                parts = line.split()
                if len(parts) < 2:
                    continue
                
                if line.startswith("MemTotal:"):
                    kb_mem_total = int(parts[1])
                elif line.startswith("MemFree:"):
                    kb_mem_free = int(parts[1])
                elif line.startswith("Buffers:"):
                    kb_main_buffers = int(parts[1])
                elif line.startswith("Cached:"):
                    kb_main_cached = int(parts[1])

        kb_mem_used = kb_mem_total - kb_mem_free - kb_main_cached - kb_main_buffers
        # Avoid returning negative values if statistics transiently fluctuate
        return max(0.0, round(kb_mem_used / kb_mem_total, 1))
    except (OSError, IndexError, ValueError, ZeroDivisionError):
        return 0.0


def _cpu_stat():
    """
    Returns the split first line of the /proc/stat file
    """
    try:
        with open('/proc/stat', 'rt') as fp:
            return list(map(int, fp.readline().split()[1:]))
    except (OSError, ValueError):
        return [0] * 10


def cpu_load(tag=None):
    """
    Returns the current cpuload
    """
    if tag and tag in _cpu_stats.keys():
        parts0 = _cpu_stats[tag]
    else:
        parts0 = _cpu_stat()
        time.sleep(0.1)
    parts1 = _cpu_stat()
    if tag:
        _cpu_stats[tag] = parts1

    try:
        parts_diff = [p1 - p0 for (p0, p1) in zip(parts0, parts1)]
        user, nice, sys, idle, iowait, irq, softirq, steal, _guest, _guest_nice = parts_diff
        idle_sum = idle + iowait
        non_idle_sum = user + nice + sys + irq + softirq + steal
        total = idle_sum + non_idle_sum
        return non_idle_sum / total if total > 0 else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def temperature(celsius=True):
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'rt') as fp:
            temp = int(fp.read().strip())
        c = int(temp / 1000)
        return c if celsius else ((c * (9 / 5)) + 32)
    except (OSError, ValueError):
        return 0


def shutdown():
    logging.warning("shutting down ...")

    try:
        from pwnagotchi.ui import view
        if view.ROOT:
            view.ROOT.on_shutdown()
            time.sleep(10)
    except ImportError:
        pass

    logging.warning("syncing...")

    try:
        from pwnagotchi import fs
        for m in fs.mounts:
            m.sync()
    except ImportError:
        pass

    _real_subprocess_run(["sync"], check=True)
    try:
        _real_subprocess_run(["halt"], check=True)
    except subprocess.CalledProcessError as e:
        logging.critical("halt command failed: %s", e)


def restart(mode, restart_bettercap=True):
    logging.warning("restarting in %s mode ...", mode)
    mode = mode.upper()

    if mode == 'AUTO':
        Path("/root/.pwnagotchi-auto").touch()
    else:
        Path("/root/.pwnagotchi-manual").touch()

    if restart_bettercap:
        try:
            logging.info("Restarting bettercap service...")
            _real_subprocess_run(["service", "bettercap", "restart"], timeout=30, check=True)
            time.sleep(2)
        except subprocess.TimeoutExpired:
            logging.error("service bettercap restart timed out after 30s")
        except subprocess.CalledProcessError as e:
            logging.error("service bettercap restart failed: %s", e)

    try:
        logging.info("Restarting pwnagotchi service...")
        _real_subprocess_run(["service", "pwnagotchi", "restart"], timeout=30, check=True)
    except subprocess.TimeoutExpired:
        logging.error("service pwnagotchi restart timed out after 30s")
    except subprocess.CalledProcessError as e:
        logging.error("service pwnagotchi restart failed: %s", e)


def reboot(mode=None, force=False):
    """
    Executes a guarded reboot sequence. Defends the system against loop states
    unless explicitly bypassed via the force flag.
    """
    if not force and not can_reboot():
        logging.error("[core] Execution cancelled: hardware limits reached.")
        return

    if mode is not None:
        mode = mode.upper()
        logging.warning("rebooting in %s mode ...", mode)
    else:
        logging.warning("rebooting ...")

    try:
        from pwnagotchi.ui import view
        if view.ROOT:
            view.ROOT.on_rebooting()
            time.sleep(10)
    except ImportError:
        pass

    if mode == 'AUTO':
        Path("/root/.pwnagotchi-auto").touch()
    elif mode == 'MANU':
        Path("/root/.pwnagotchi-manual").touch()

    logging.warning("syncing...")

    try:
        from pwnagotchi import fs
        for m in fs.mounts:
            m.sync()
    except ImportError:
        pass

    _real_subprocess_run(["sync"], check=True)
    try:
        _real_subprocess_run(["shutdown", "-r", "now"], check=True)
    except subprocess.CalledProcessError as e:
        logging.critical("reboot command failed: %s", e)
