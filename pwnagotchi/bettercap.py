import json
import logging
import random
import asyncio
import base64
import time
import requests
import websockets
from time import sleep
from requests.auth import HTTPBasicAuth

# Requests configuration
#
# DEFAULT_RETRIES / API_TIMEOUT were reduced from 5/30 to 2/15. With the
# old values, a single hung HTTP POST to bettercap (e.g. a heavy
# wifi.recon.channel call across 10+ channels while bettercap is busy
# processing a deauth burst) could block the caller for up to
# API_TIMEOUT * (DEFAULT_RETRIES + 1) = 180 seconds. run() is synchronous
# and called from the main loop, so that entire window shows up as dead
# air between epochs. 2 retries * 15s caps the worst case at ~45s, and
# recon commands can opt out of retries entirely (see run(retries=...)).
DEFAULT_RETRIES = 2
API_TIMEOUT = 15

# WebSocket configuration
PING_TIMEOUT = 180
PING_INTERVAL = 15
MAX_QUEUE = 10000

MIN_SLEEP = 0.5
MAX_SLEEP = 5.0

# Log a warning when a single run() call takes longer than this. Useful for
# spotting which bettercap commands are actually slow on-device rather than
# guessing.
SLOW_COMMAND_THRESHOLD = 3.0


def decode(r, verbose_errors=True):
    try:
        return r.json()
    except Exception as e:
        if r.status_code == 200:
            logging.error("Error decoding JSON: error='%s' resp='%s'", e, r.text)
            return r.text
        else:
            err = "Error %d: %s" % (r.status_code, r.text.strip())
            if verbose_errors:
                logging.info(err)
            raise Exception(err)


class Client(object):
    def __init__(self, hostname='localhost', scheme='http', port=8081, username='user', password='pass'):
        self.hostname = hostname
        self.scheme = scheme
        self.port = port
        self.username = username
        self.password = password
        self.url = "%s://%s:%d/api" % (scheme, hostname, port)

        # Secure WS endpoint without plain-text credentials in the URI
        self.websocket = "ws://%s:%d/api" % (hostname, port)
        auth_str = base64.b64encode(("%s:%s" % (username, password)).encode('utf-8')).decode('utf-8')
        self.ws_headers = {"Authorization": "Basic %s" % auth_str}

        self.auth = HTTPBasicAuth(username, password)

        # Connection pooling for HTTP requests (Keep-Alive optimization for 32-bit Pi)
        self._session = requests.Session()
        self._session.auth = self.auth
        adapter = requests.adapters.HTTPAdapter(max_retries=DEFAULT_RETRIES)
        self._session.mount('http://', adapter)
        self._session.mount('https://', adapter)

    def session(self, sess="session"):
        r = self._session.get("%s/%s" % (self.url, sess), timeout=API_TIMEOUT)
        return decode(r)

    async def start_websocket(self, consumer):
        endpoint = "%s/events" % self.websocket
        while True:
            try:
                async with websockets.connect(
                    endpoint,
                    extra_headers=self.ws_headers,
                    ping_interval=PING_INTERVAL,
                    ping_timeout=PING_TIMEOUT,
                    max_queue=MAX_QUEUE
                ) as ws:
                    async for msg in ws:
                        try:
                            await consumer(msg)
                        except asyncio.CancelledError:
                            raise  # Re-raise cancellation to propagate shutdown
                        except Exception as ex:
                            logging.debug("Error while processing event (%s)", ex)
            except asyncio.CancelledError:
                break  # Gracefully break the reconnect loop on cancellation
            except (ConnectionRefusedError, OSError, websockets.exceptions.WebSocketException) as e:
                sleep_time = MIN_SLEEP + MAX_SLEEP * random.random()
                logging.debug("WebSocket connection error (%s), retrying in %.2f s", type(e).__name__, sleep_time)
                await asyncio.sleep(sleep_time)

    def run(self, command, verbose_errors=True, retries=None):
        """
        Execute a bettercap command via the HTTP API.

        retries -- override DEFAULT_RETRIES for this call. Pass 0 for
        fire-and-forget recon commands (wifi.recon.channel, wifi.recon
        clear) where waiting through a retry storm on a busy bettercap is
        worse than simply skipping the epoch.
        """
        if retries is None:
            retries = DEFAULT_RETRIES

        t0 = time.time()
        attempt = 0
        while True:
            try:
                r = self._session.post(
                    "%s/session" % self.url,
                    json={'cmd': command},
                    timeout=API_TIMEOUT
                )
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                attempt += 1
                if attempt > retries:
                    raise
                sleep_time = MIN_SLEEP + MAX_SLEEP * random.random()
                logging.warning("Command execution failed (attempt %d/%d, %s), retrying in %.2f s...",
                                attempt, retries, type(e).__name__, sleep_time)
                sleep(sleep_time)

        elapsed = time.time() - t0
        if elapsed > SLOW_COMMAND_THRESHOLD:
            logging.warning("[bettercap] slow command (%.1fs): %s", elapsed, command[:100])

        return decode(r, verbose_errors=verbose_errors)
