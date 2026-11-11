import logging
import os
import re
import sqlite3
from datetime import datetime
from enum import Enum
from threading import Lock

import requests
from pwnagotchi import plugins
import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.components import LabeledValue
from pwnagotchi.ui.view import BLACK
from pwnagotchi.utils import remove_whitelisted


class WpaSec(plugins.Plugin):
    __author__ = '33197631+dadav@users.noreply.github.com'
    __editor__ = 'flippedgannet'
    __version__ = '2.2.0'
    __license__ = 'GPL3'
    __description__ = 'Automatically uploads handshakes to https://wpa-sec.stanev.org and displays results.'

    class Status(Enum):
        TOUPLOAD = 0
        INVALID = 1
        SUCCESSFUL = 2

    def __init__(self):
        self.ready = False
        self.lock = Lock()
        self.options = dict()
        self.skip_until_reload = set()

        # UI & caching variables
        self._last_pwd_result = ""
        self._last_potfile_mtime = 0
        self._handshake_dir = None
        self._db_path = '/etc/pwnagotchi/.wpa_sec_db'

    def _init_db(self):
        """Initialize SQLite DB for tracking uploaded handshakes."""
        try:
            db_conn = sqlite3.connect(self._db_path)
            db_conn.execute('pragma journal_mode=wal')
            with db_conn:
                db_conn.execute('''
                    CREATE TABLE IF NOT EXISTS handshakes (
                        path TEXT PRIMARY KEY,
                        status INTEGER
                    )
                ''')
                db_conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_handshakes_status
                    ON handshakes (status)
                ''')
            db_conn.close()
        except Exception as e:
            logging.error("WPA_SEC: Failed to initialize SQLite database: %s", e)

    def on_loaded(self):
        """Gets called when the plugin gets loaded."""
        if not self.options.get('api_key'):
            logging.error("WPA_SEC: API-KEY isn't set. Can't upload.")
            return

        if not self.options.get('api_url'):
            logging.error("WPA_SEC: API-URL isn't set. Can't upload.")
            return

        self._init_db()
        self.ready = True
        logging.info("WPA_SEC: plugin loaded successfully.")

    def on_handshake(self, agent, filename, access_point, client_station):
        """Record new handshakes into database as TOUPLOAD."""
        config = agent.config()
        self._handshake_dir = config['bettercap']['handshakes']

        if not remove_whitelisted([filename], config['main'].get('whitelist', [])):
            return

        try:
            db_conn = sqlite3.connect(self._db_path)
            with db_conn:
                db_conn.execute('''
                    INSERT INTO handshakes (path, status)
                    VALUES (?, ?)
                    ON CONFLICT(path) DO UPDATE SET status = excluded.status
                    WHERE handshakes.status = ?
                ''', (filename, self.Status.TOUPLOAD.value, self.Status.INVALID.value))
            db_conn.close()
        except Exception as e:
            logging.error("WPA_SEC: Error recording handshake %s: %s", filename, e)

    def on_internet_available(self, agent):
        """Upload pending handshakes and download cracked results when connected to internet."""
        if not self.ready or self.lock.locked():
            return

        with self.lock:
            config = agent.config()
            display = agent.view()
            self._handshake_dir = config['bettercap']['handshakes']

            # --- UPLOAD SECTION ---
            try:
                db_conn = sqlite3.connect(self._db_path)
                cursor = db_conn.cursor()

                cursor.execute('SELECT path FROM handshakes WHERE status = ?', (self.Status.TOUPLOAD.value,))
                handshakes_toupload = [row[0] for row in cursor.fetchall()]
                handshakes_toupload = set(handshakes_toupload) - self.skip_until_reload

                if handshakes_toupload:
                    logging.info("WPA_SEC: Internet connectivity detected. Uploading handshakes...")
                    for idx, handshake in enumerate(handshakes_toupload):
                        display.on_uploading(f"WPA-SEC ({idx + 1}/{len(handshakes_toupload)})")
                        logging.info("WPA_SEC: Uploading %s...", handshake)

                        try:
                            upload_response = self._upload_to_wpasec(handshake)

                            if upload_response.startswith("hcxpcapngtool"):
                                logging.info("WPA_SEC: %s successfully uploaded.", handshake)
                                new_status = self.Status.SUCCESSFUL.value
                            else:
                                logging.info("WPA_SEC: %s uploaded, but was marked invalid by server.", handshake)
                                new_status = self.Status.INVALID.value

                            cursor.execute('''
                                INSERT INTO handshakes (path, status)
                                VALUES (?, ?)
                                ON CONFLICT(path) DO UPDATE SET status = excluded.status
                            ''', (handshake, new_status))
                            db_conn.commit()

                        except requests.exceptions.RequestException:
                            logging.exception("WPA_SEC: Network error uploading %s, skipping until reload.", handshake)
                            self.skip_until_reload.add(handshake)
                        except OSError:
                            logging.exception("WPA_SEC: File access error uploading %s, removing from DB.", handshake)
                            cursor.execute('DELETE FROM handshakes WHERE path = ?', (handshake,))
                            db_conn.commit()
                        except Exception:
                            logging.exception("WPA_SEC: Unexpected exception uploading %s.", handshake)

                    display.on_normal()

                cursor.close()
                db_conn.close()
            except Exception:
                logging.exception("WPA_SEC: Exception during upload cycle.")

            # --- DOWNLOAD SECTION ---
            try:
                if self.options.get('download_results', False):
                    cracked_file_path = os.path.join(self._handshake_dir, 'wpa-sec.cracked.potfile')

                    if os.path.exists(cracked_file_path):
                        last_check = datetime.fromtimestamp(os.path.getmtime(cracked_file_path))
                        download_interval = int(self.options.get('download_interval', 3600))
                        if (datetime.now() - last_check).total_seconds() < download_interval:
                            return

                    self._download_from_wpasec(cracked_file_path)
                    if self.options.get('single_files', False):
                        self._write_cracked_single_files(cracked_file_path, self._handshake_dir)
            except Exception:
                logging.exception("WPA_SEC: Exception downloading results.")

    def _upload_to_wpasec(self, path, timeout=30):
        with open(path, 'rb') as file_to_upload:
            cookie = {'key': self.options['api_key']}
            payload = {'file': file_to_upload}
            headers = {"User-Agent": "Pwnagotchi/WpaSec-Plugin"}

            result = requests.post(
                self.options['api_url'],
                cookies=cookie,
                files=payload,
                headers=headers,
                timeout=timeout
            )
            result.raise_for_status()

            response = result.text.partition('\n')[0]
            logging.debug("WPA_SEC: Response uploading %s: %s", path, response)
            return response

    def _download_from_wpasec(self, output, timeout=30):
        api_url = self.options['api_url'].rstrip('/') + '/?api&dl=1'
        cookie = {'key': self.options['api_key']}
        headers = {"User-Agent": "Pwnagotchi/WpaSec-Plugin"}

        logging.info("WPA_SEC: Downloading cracked passwords...")

        result = requests.get(api_url, cookies=cookie, headers=headers, timeout=timeout)
        result.raise_for_status()

        # Write downloaded passwords safely
        tmp_output = f"{output}.tmp"
        with open(tmp_output, 'wb') as output_file:
            output_file.write(result.content)
        os.replace(tmp_output, output)

        logging.info("WPA_SEC: Downloaded cracked passwords to %s", output)

    def _write_cracked_single_files(self, cracked_file_path, handshake_dir):
        """Splits download results into individual .cracked files."""
        logging.info("WPA_SEC: Writing individual .cracked files...")

        try:
            with open(cracked_file_path, 'r', encoding='utf-8', errors='ignore') as cracked_file:
                for line in cracked_file:
                    try:
                        parts = line.strip().split(':')
                        if len(parts) >= 4:
                            bssid, station_mac, ssid, password = parts[0], parts[1], parts[2], parts[3]
                            if password:
                                clean_ssid = re.sub(r'[^a-zA-Z0-9]', '', ssid)
                                base_filename = f"{clean_ssid}_{bssid}"

                                # Check for .pcap or .pcapng targets
                                for ext in ('.pcapng', '.pcap'):
                                    pcap_path = os.path.join(handshake_dir, base_filename + ext)
                                    pcap_cracked_path = pcap_path + '.cracked'

                                    if os.path.exists(pcap_path) and not os.path.exists(pcap_cracked_path):
                                        tmp_cracked = f"{pcap_cracked_path}.tmp"
                                        with open(tmp_cracked, 'w', encoding='utf-8') as f:
                                            f.write(password)
                                        os.replace(tmp_cracked, pcap_cracked_path)
                    except Exception:
                        logging.exception("WPA_SEC: Error parsing potfile line: %s", line)
        except Exception as e:
            logging.error("WPA_SEC: Could not read potfile for single files splitting: %s", e)

    def on_webhook(self, path, request):
        from flask import make_response

        html_content = f'''
            <html>
                <body>
                    <form id="postForm" action="{self.options['api_url']}" method="POST">
                        <input type="hidden" name="key" value="{self.options['api_key']}">
                    </form>
                    <script type="text/javascript">
                        document.getElementById('postForm').submit();
                    </script>
                </body>
            </html>
        '''
        return make_response(html_content)

    def on_ui_setup(self, ui):
        if self.options.get('show_pwd', False) and self.options.get('download_results', False):
            # Display password at bottom or customized coordinates
            x_pos = self.options.get('x_coord', 0)
            y_pos = self.options.get('y_coord', 95)

            ui.add_element('pass', LabeledValue(
                color=BLACK,
                label='',
                value='',
                position=(x_pos, y_pos),
                label_font=fonts.Bold,
                text_font=fonts.Small
            ))

    def on_unload(self, ui):
        with ui._lock:
            try:
                ui.remove_element('pass')
            except Exception:
                pass

    def on_ui_update(self, ui):
        if not (self.options.get('show_pwd', False) and self.options.get('download_results', False)):
            return

        if not self._handshake_dir:
            return

        file_path = os.path.join(self._handshake_dir, 'wpa-sec.cracked.potfile')

        # Performance Optimization: Read file ONLY if mtime changed
        try:
            if not os.path.exists(file_path):
                ui.set('pass', '')
                return

            current_mtime = os.path.getmtime(file_path)
            if current_mtime != self._last_potfile_mtime:
                self._last_potfile_mtime = current_mtime

                with open(file_path, 'r', encoding='utf-8', errors='ignore') as file:
                    lines = [line.strip() for line in file if line.strip()]
                    if lines:
                        last_line = lines[-1]
                        parts = last_line.split(':')
                        if len(parts) >= 4:
                            self._last_pwd_result = f"{parts[2]} - {parts[3]}"
                        else:
                            self._last_pwd_result = "Malformed line"
                    else:
                        self._last_pwd_result = ""

            ui.set('pass', self._last_pwd_result)

        except Exception as e:
            logging.debug("WPA_SEC: Error reading potfile for UI: %s", e)
