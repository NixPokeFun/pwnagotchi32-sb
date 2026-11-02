import json
import logging
import socket
import subprocess
import threading
import time
import requests

import pwnagotchi

# pwngrid-peer runs on port 8666
API_ADDRESS = 'http://127.0.0.1:8666/api/v1'

_RESTART_COOLDOWN = 180
_last_restart_time = 0.0
_restart_lock = threading.Lock()

_LIST_ENDPOINTS = ('peers', 'memory', 'inbox')


def _safe_default(path):
  return [] if any(marker in path for marker in _LIST_ENDPOINTS) else {}


# Single connectivity probe. requests' own timeout= does NOT cover
# getaddrinfo() -- with no working DNS, socket.getaddrinfo() blocks for
# 40+ seconds (5s * 2 attempts * search domains) before requests ever
# gets a chance to apply its connect/read timeout. is_connected() used to
# be called every epoch from bin/pwnagotchi, which made that 40s show up
# directly as dead air between epochs. Resolve manually with an explicit
# timeout first so we bail out in ~8s instead.
#
# 8s is generous on purpose: on a slow but working link (weak WiFi, LTE
# with poor signal, captive portal round-trip), 5s was observed to time
# out even with a fully functional connection. The DNS probe usually
# resolves or fails much faster than that; the TCP+TLS probe is the one
# that occasionally needs the headroom.
_CONNECTIVITY_HOST = 'api.opwngrid.xyz'
_CONNECTIVITY_URL = 'https://%s/api/v1/uptime' % _CONNECTIVITY_HOST
_CONNECTIVITY_CHECK_TIMEOUT = 8.0
_CONNECTIVITY_DNS_TIMEOUT = 8.0

def is_connected():
  old = socket.getdefaulttimeout()
  socket.setdefaulttimeout(_CONNECTIVITY_DNS_TIMEOUT)
  try:
    socket.gethostbyname(_CONNECTIVITY_HOST)
  except Exception as e:
    logging.debug('[grid] is_connected: DNS lookup failed (%s)', e)
    return False
  finally:
    socket.setdefaulttimeout(old)

  try:
    requests.get(_CONNECTIVITY_URL, timeout=_CONNECTIVITY_CHECK_TIMEOUT)
    return True
  except Exception as e:
    logging.debug('[grid] is_connected: %s is unreachable (%s)',
                  _CONNECTIVITY_URL, e)
    return False


def _auto_start_grid():
  global _last_restart_time
  with _restart_lock:
    now = time.time()
    if now - _last_restart_time < _RESTART_COOLDOWN:
      return False
    _last_restart_time = now

  def start_task():
    try:
      logging.warning(
          '[grid] Connection lost! Restarting pwngrid-peer...'
      )
      subprocess.run(['sudo', 'systemctl', 'restart', 'pwngrid-peer'])
      logging.info(
          '[grid] pwngrid-peer restarted successfully.'
      )
    except Exception as e:
      logging.error('[grid] Restart error: %s', e)

  start_thread = threading.Thread(target=start_task)
  start_thread.daemon = True
  start_thread.start()
  return True


def _handle_request_error(e, context):
  """Shared network-error handler + pwngrid-peer autostart trigger."""
  if isinstance(e, requests.exceptions.ConnectionError):
    if 'ReadTimeoutError' in str(e) or 'ConnectTimeoutError' in str(e):
      logging.debug('[grid] %s connection error (timeout): %s', context, e)
    elif _auto_start_grid():
      logging.error(
          '[grid] %s connection error, triggering autostart: %s', context, e
      )
    else:
      logging.error('[grid] %s connection error: %s', context, e)
  elif isinstance(e, requests.exceptions.Timeout):
    logging.debug('[grid] %s timeout: %s', context, e)
  else:
    logging.error('[grid] %s communication error: %s', context, e)


def call(path, obj=None):
  url = '{}{}'.format(API_ADDRESS, path)
  try:
    if obj is None:
      logging.debug('[grid] call GET %s', url)
      r = requests.get(url, timeout=(2.0, 5.0))
    else:
      logging.debug('[grid] call POST %s with data', url)
      if isinstance(obj, bytes):
        r = requests.post(url, data=obj, timeout=(2.0, 5.0))
      else:
        r = requests.post(url, json=obj, timeout=(2.0, 5.0))

    if r.status_code == 200:
      return r.json()

    logging.error(
        '[grid] call unexpected status %d for %s: %s', r.status_code, url, r.text
    )

  except Exception as e:
    _handle_request_error(e, 'call')

  return _safe_default(path)


def advertise(enabled=True):
  return call('/mesh/%s' % ('true' if enabled else 'false'))


def set_advertisement_data(data):
  return call('/mesh/data', obj=data)


def get_advertisement_data():
  return call('/mesh/data')


def memory():
  return call('/mesh/memory')


def peers():
  return call('/mesh/peers')


def closest_peer():
  all_peers = peers()
  return all_peers[0] if len(all_peers) else None


def update_data(last_session):
  brain = {}
  try:
    with open('/root/brain.json') as fp:
      brain = json.load(fp)
  except Exception:
    pass

  plugins_enabled = []
  language = 'en'
  try:
    if hasattr(pwnagotchi, 'config') and 'main' in pwnagotchi.config:
      plugins_cfg = pwnagotchi.config['main'].get('plugins', {})
      plugins_enabled = [
          name
          for name, options in plugins_cfg.items()
          if options.get('enabled', False)
      ]
      language = pwnagotchi.config['main'].get('lang', 'en')
  except Exception:
    pass

  data = {
      'session': {
          'duration': getattr(last_session, 'duration', 0),
          'epochs': getattr(last_session, 'epochs', 0),
          'train_epochs': getattr(last_session, 'train_epochs', 0),
          'avg_reward': getattr(last_session, 'avg_reward', 0.0),
          'min_reward': getattr(last_session, 'min_reward', 0.0),
          'max_reward': getattr(last_session, 'max_reward', 0.0),
          'deauthed': getattr(last_session, 'deauthed', 0),
          'associated': getattr(last_session, 'associated', 0),
          'handshakes': getattr(last_session, 'handshakes', 0),
          'peers': getattr(last_session, 'peers', 0),
      },
      'uname': subprocess.getoutput('uname -a'),
      'brain': brain,
      'version': getattr(pwnagotchi, '__version__'),
      'build': 'pwnagotchi-ai32 by flippedgannet',
      'plugins': plugins_enabled,
      'language': language,
  }

  logging.debug('[grid] Updating grid data: %s', data)
  call('/data', data)


def report_ap(essid, bssid):
  """Report an AP to opwngrid. Returns True only on success.

  IMPORTANT: returns False on any failure (network error, non-200 status,
  timeout) so the grid plugin does not mark a handshake as 'reported'
  when the peer is actually unreachable.
  """
  url = '{}/report/ap'.format(API_ADDRESS)
  try:
    logging.debug('[grid] report_ap POST %s for %s(%s)', url, essid, bssid)
    r = requests.post(
        url,
        json={'essid': essid, 'bssid': bssid},
        timeout=(2.0, 5.0),
    )
    if r.status_code == 200:
      return True
    logging.error(
        '[grid] report_ap unexpected status %d for %s: %s',
        r.status_code, url, r.text,
    )
  except Exception as e:
    _handle_request_error(e, 'report_ap')
  return False


def inbox(page=1, with_pager=False):
  obj = call('/inbox?p=%d' % page)

  if isinstance(obj, list) or not isinstance(obj, dict):
    obj = {'pages': 1, 'messages': []}

  if 'pages' not in obj:
    obj['pages'] = 1
  if 'messages' not in obj:
    obj['messages'] = []

  return obj.get('messages', []) if not with_pager else obj


def inbox_message(id):
  return call('/inbox/%d' % int(id))


def mark_message(id, mark):
  return call('/inbox/%d/%s' % (int(id), str(mark)))


def send_message(to, message):
  return call('/unit/%s/inbox' % to, message.encode('utf-8'))
