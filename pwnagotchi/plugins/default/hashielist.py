import os
import logging
import shutil
import subprocess
import time

import pwnagotchi
import pwnagotchi.plugins as plugins


class Hashielist(plugins.Plugin):
    """
    Unified handshake pipeline for pwnagotchi.

    One plugin, one internal pipeline, two independently gated stages:

        1. VAULT (gated by `vault`, default off)
           Every *.pcap dropped into the handshake folder is run through
           hcxpcapngtool. A valid handshake lands as a *.22000 in the
           sibling `hashes` folder. pcaps that yield nothing are recorded
           in a hidden noshake cache so we never re-run the tool on them.

        2. LIST (gated by `whitelist`, default on)
           Every *.22000 in `hashes` is parsed for the BSSID encoded in
           its filename. That MAC is appended to the whitelist file and
           pushed into the live runtime whitelist so pwnagotchi stops
           attacking an AP we already own.

    Ordering matters: LIST consumes VAULT's output, so on the same wake
    cycle a fresh pcap can be vaulted and its MAC whitelisted without
    waiting for the next epoch.

    Configuration under [main.plugins.hashielist] in config.toml:

        whitelist = true   # master switch for the LIST stage
        vault     = false  # master switch for the VAULT stage

    With both flags false the plugin is inert apart from keeping the
    whitelist file present and mirroring its contents into the runtime
    whitelist.
    """

    __author__ = 'ex18a - original hashvault & whitelist plugins'
    __editor__ = 'flippedgannet'
    __version__ = '1.1.1'
    __description__ = (
        'Unified handshake vault and whitelist manager. Validates pcaps '
        'into hashes, then harvests BSSIDs into the runtime whitelist. '
        'Both stages are independently gated by config.'
    )

    # Hidden bookkeeping file remembering pcaps that were already run
    # through hcxpcapngtool and produced no handshake. Kept out of the
    # user-facing `hashes` folder on purpose -- one dotfile beats a pile
    # of 0-byte *.noshake markers nobody wants to look at. Matches the
    # existing /root/.pwnagotchi-* convention used elsewhere.
    NOSHAKE_CACHE_PATH = '/root/.hashvault-noshake-cache'

    # Minimum mtime age before we consider a pcap safe to feed to
    # hcxpcapngtool. Without this we race the writer and occasionally
    # chew on half-flushed files.
    SETTLE_SECONDS = 10

    # Well-known locations to probe when hcxpcapngtool is not on PATH.
    # hcxtools' Makefile installs into /usr/local/bin by default, so a
    # stock pwnagotchi image that followed upstream build instructions
    # will have the binary there rather than in /usr/bin.
    HCXPCAPNGTOOL_FALLBACKS = (
        '/usr/local/bin/hcxpcapngtool',
        '/usr/bin/hcxpcapngtool',
        '/bin/hcxpcapngtool',
    )

    def __init__(self):
        # Paths are resolved in on_loaded() against the live config.
        self.whitelist_path = '/root/.whitelist'
        self.handshake_dir = None
        self.hash_dir = None

        # Absolute path to hcxpcapngtool, resolved once at load time.
        # None means "not found" and forces the VAULT stage off even if
        # the config flag says otherwise -- better than hammering a
        # nonexistent path on every sweep.
        self._hcxpcapngtool = None

        # LIST stage state.
        #   _known_entries         in-memory mirror of the whitelist file
        #   _whitelist_mtime       last seen mtime, for lazy reload
        #   _processed_hash_files  filenames already harvested this process
        #   _hash_dir_mtime        last seen mtime of the hashes folder,
        #                          used to skip listdir on idle epochs
        self._known_entries = set()
        self._whitelist_mtime = None
        self._processed_hash_files = set()
        self._hash_dir_mtime = None

        # VAULT stage state.
        #   _last_checked_mtime  per-pcap mtime we last ran the tool at
        #   _noshake_cache       mirror of the hidden noshake file
        self._last_checked_mtime = {}
        self._noshake_cache = set()

        # Feature gates, set in on_loaded(). Both start off so the
        # plugin never writes anything before config says it may.
        self._list_enabled = False
        self._vault_enabled = False

    # ==================================================================
    # Lifecycle
    # ==================================================================

    def on_loaded(self):
        """
        Resolve config, prepare on-disk state, then run one full pass so
        we immediately catch up on whatever landed while the plugin was
        disabled.
        """
        config = pwnagotchi.config

        # Plugin options live under [main.plugins.hashielist] in
        # config.toml, i.e. config['main']['plugins']['hashielist'].
        # Some pwnagotchi loaders also inject the same dict as
        # self.options before calling on_loaded, so prefer that when
        # present and fall back to a manual lookup otherwise.
        options = (
            getattr(self, 'options', None)
            or (config.get('main', {}).get('plugins', {}) or {}).get('hashielist', {})
            or {}
        )

        # `whitelist` doubles as the plugin's core toggle: with the
        # option missing we default to on so the whitelist file at least
        # exists and gets consumed. `vault` defaults off because writing
        # into the hashes folder is the more invasive behaviour.
        self._list_enabled = bool(options.get('whitelist', True))
        self._vault_enabled = bool(options.get('vault', False))

        self.handshake_dir = config.get('bettercap', {}).get(
            'handshakes', '/root/handshakes'
        )
        parent_dir = os.path.dirname(os.path.normpath(self.handshake_dir))
        self.hash_dir = os.path.join(parent_dir, 'hashes')

        # Resolve hcxpcapngtool once. hcxtools' Makefile installs into
        # /usr/local/bin by default, not /usr/bin, so a hardcoded path
        # is wrong on any image that followed upstream build steps.
        self._hcxpcapngtool = self._resolve_hcxpcapngtool()
        if self._vault_enabled:
            if self._hcxpcapngtool:
                logging.info(
                    "[hashielist] using hcxpcapngtool at %s",
                    self._hcxpcapngtool,
                )
            else:
                logging.error(
                    "[hashielist] hcxpcapngtool not found on PATH or in %s; "
                    "vault stage disabled",
                    ', '.join(self.HCXPCAPNGTOOL_FALLBACKS),
                )
                self._vault_enabled = False

        logging.info(
            "[hashielist] loaded: list=%s vault=%s handshakes=%s hashes=%s",
            self._list_enabled, self._vault_enabled,
            self.handshake_dir, self.hash_dir,
        )

        # Always make sure the whitelist file exists, even with the LIST
        # stage off. An existing whitelist must keep working, and a user
        # enabling the plugin just to *consume* a prepared file shouldn't
        # have to touch it by hand.
        self._ensure_whitelist_file()

        # One-shot migration of any config.toml whitelist entries into
        # the file, then blank the config list so pwnagotchi doesn't hold
        # a stale copy in memory next to our file-driven source of truth.
        legacy_entries = config['main'].get('whitelist', [])
        if legacy_entries:
            logging.info(
                "[hashielist] migrating %d legacy config.toml whitelist entry(ies) into %s",
                len(legacy_entries), self.whitelist_path,
            )
            self._migrate_config_entries(legacy_entries)
        config['main']['whitelist'] = []

        # Prime the in-memory mirror. A failed initial read leaves the
        # set empty and does NOT advance _whitelist_mtime, so the first
        # pipeline pass will retry.
        entries, ok = self._read_whitelist_file()
        if ok:
            self._known_entries = entries
            logging.info(
                "[hashielist] whitelist primed with %d entries",
                len(self._known_entries),
            )
        else:
            logging.warning(
                "[hashielist] initial whitelist read failed, starting empty"
            )

        if self._vault_enabled:
            if not os.path.isdir(self.hash_dir):
                os.makedirs(self.hash_dir, exist_ok=True)
                logging.info("[hashielist] created hashes folder: %s", self.hash_dir)
            self._load_noshake_cache()
            logging.info(
                "[hashielist] noshake cache loaded: %d remembered miss(es)",
                len(self._noshake_cache),
            )
            logging.info("[hashielist] vault stage armed, scanning backlog")
            self._vault_sweep_backlog()
        else:
            logging.info("[hashielist] vault stage disabled, hashes folder untouched")

        logging.info("[hashielist] list stage armed, running initial sync")
        self._run_pipeline()

    def on_epoch(self, agent, epoch, epoch_data):
        """
        Once per training epoch. Only the LIST half makes sense here --
        the VAULT stage is time-gated and pcaps are rarely flushed
        mid-epoch, so we skip the expensive conversion and just re-sync
        the whitelist.
        """
        self._run_pipeline()

    def on_sleep(self, agent, t):
        """
        Pwnagotchi going to sleep is the safest moment to touch pcaps:
        no captures are being written. Run the full pipeline here (vault
        then list) rather than on_epoch.
        """
        if self._vault_enabled:
            self._vault_sweep_recent()
        self._run_pipeline()

    def on_unload(self, ui):
        """
        Runtime disable. Blank the runtime whitelist so a subsequent
        reload starts from a clean, file-driven state rather than
        inheriting whatever we injected this session.
        """
        logging.info("[hashielist] unloading, blanking runtime whitelist")
        pwnagotchi.config['main']['whitelist'] = []

    # ==================================================================
    # Pipeline orchestration
    # ==================================================================

    def _run_pipeline(self):
        """
        Read-side entry point: reload the whitelist if it changed on
        disk, harvest any new MACs, then mirror everything into the
        runtime config.

        Each helper returns whether it actually did something; when none
        of them changed state we emit a single debug line so an operator
        can distinguish "plugin is idle but alive" from "on_epoch blew up
        and swallowed the exception".
        """
        reloaded = self._reload_whitelist_if_changed()
        harvested = self._harvest_macs_from_hashes()
        injected = self._inject_into_runtime()

        if not (reloaded or harvested or injected):
            logging.debug("[hashielist] pipeline pass: no changes")

    # ==================================================================
    # LIST stage: whitelist file management
    # ==================================================================

    def _ensure_whitelist_file(self):
        """
        Create the whitelist file if missing and make it world-writable
        so pwnagotchi and a human editor agree on permissions. Cheap and
        safe to call from anywhere that touches the file.
        """
        try:
            if not os.path.exists(self.whitelist_path):
                with open(self.whitelist_path, 'a'):
                    pass
                os.chmod(self.whitelist_path, 0o666)
                logging.info("[hashielist] created whitelist file: %s", self.whitelist_path)
        except Exception as e:
            logging.error("[hashielist] could not create whitelist file: %s", e)

    @staticmethod
    def _normalize_entry(raw):
        """
        Strip comments/whitespace and lowercase anything that looks like
        a MAC so case-only duplicates don't slip through.
        """
        clean = raw.split('#', 1)[0].strip()
        if ':' in clean:
            clean = clean.lower()
        return clean

    def _migrate_config_entries(self, config_entries):
        """
        One-shot migration: append legacy config.toml whitelist entries
        into the physical file, tagged with their origin so a human
        reading the file later can tell them apart from auto-harvested
        MACs.
        """
        self._ensure_whitelist_file()
        try:
            with open(self.whitelist_path, 'r') as f:
                existing = {
                    self._normalize_entry(line)
                    for line in f
                    if line.strip()
                }
            existing.discard('')

            added = 0
            with open(self.whitelist_path, 'a') as f:
                for entry in config_entries:
                    clean = self._normalize_entry(str(entry))
                    if clean and clean not in existing:
                        f.write(f"{clean} # Manual_Config_Entry\n")
                        existing.add(clean)
                        added += 1

            if added:
                logging.info(
                    "[hashielist] migrated %d config entry(ies) into whitelist file",
                    added,
                )
            else:
                logging.debug("[hashielist] no new config entries to migrate")
        except Exception as e:
            logging.error("[hashielist] config entry migration failed: %s", e)

    def _read_whitelist_file(self):
        """
        Parse the whitelist file into a set of entries. Lines may carry
        a trailing `# comment`; blanks and comment-only lines are
        dropped.

        Returns (entries, ok). On failure returns (set(), False) and
        deliberately leaves _whitelist_mtime untouched, so the next
        pipeline pass treats the file as still-dirty and retries instead
        of silently latching an empty in-memory set forever.
        """
        self._ensure_whitelist_file()
        try:
            with open(self.whitelist_path, 'r') as f:
                entries = {
                    self._normalize_entry(line)
                    for line in f
                    if line.strip() and not line.lstrip().startswith('#')
                }
            entries.discard('')
            # Only advance the mtime after a successful read; a half-read
            # or failed open must not convince us we're up to date.
            self._whitelist_mtime = os.path.getmtime(self.whitelist_path)
            return entries, True
        except Exception as e:
            logging.error("[hashielist] whitelist read failed: %s", e)
            return set(), False

    def _reload_whitelist_if_changed(self):
        """
        mtime check so a user hand-editing the whitelist while the
        plugin runs gets picked up on the next pass without us polling
        file contents every epoch. Returns True if the in-memory mirror
        was actually replaced.
        """
        try:
            mtime = os.path.getmtime(self.whitelist_path)
        except OSError:
            return False
        if mtime == self._whitelist_mtime:
            return False

        logging.info("[hashielist] whitelist changed on disk, reloading")
        entries, ok = self._read_whitelist_file()
        if not ok:
            # _whitelist_mtime stayed put, so we'll try again next pass.
            logging.warning(
                "[hashielist] reload failed, keeping %d in-memory entry(ies)",
                len(self._known_entries),
            )
            return False

        self._known_entries = entries
        logging.info(
            "[hashielist] whitelist reloaded: %d entries",
            len(self._known_entries),
        )
        return True

    def _append_whitelist_entry(self, entry, comment):
        """
        Append one entry to the file and keep the in-memory mirror and
        mtime in sync so the next pass doesn't misread this as an
        external change.
        """
        if not entry:
            return False
        self._ensure_whitelist_file()
        try:
            with open(self.whitelist_path, 'a') as f:
                f.write(f"{entry} # {comment}\n")
            self._known_entries.add(entry)
            self._whitelist_mtime = os.path.getmtime(self.whitelist_path)
            return True
        except Exception as e:
            logging.error(
                "[hashielist] failed to append whitelist entry %s: %s",
                entry, e,
            )
            return False

    # ==================================================================
    # LIST stage: harvesting MACs out of vaulted hashes
    # ==================================================================

    @staticmethod
    def _pcap_to_hash_name(pcap_basename):
        return os.path.splitext(pcap_basename)[0] + '.22000'

    @staticmethod
    def _hash_to_pcap_name(hash_basename):
        return os.path.splitext(hash_basename)[0] + '.pcap'

    def _harvest_macs_from_hashes(self):
        """
        Walk the hashes folder and, for every *.22000 whose source pcap
        is still present, extract the BSSID from the filename and record
        it in the whitelist. Filenames already handled this process
        lifetime are skipped.

        Returns the number of new MACs actually written. The hashes
        folder's own mtime gates the listdir: on epochs where nothing
        was vaulted we short-circuit before touching the directory
        listing, which keeps idle passes cheap even with hundreds of
        hashes on disk.
        """
        if not self._list_enabled:
            return 0
        if not os.path.isdir(self.hash_dir):
            return 0

        try:
            dir_mtime = os.path.getmtime(self.hash_dir)
        except OSError:
            return 0

        if dir_mtime == self._hash_dir_mtime:
            logging.debug(
                "[hashielist] hashes folder unchanged, skipping scan"
            )
            return 0
        self._hash_dir_mtime = dir_mtime

        harvested = 0
        for filename in os.listdir(self.hash_dir):
            if not filename.endswith('.22000'):
                continue
            if filename in self._processed_hash_files:
                continue

            pcap_path = os.path.join(
                self.handshake_dir, self._hash_to_pcap_name(filename)
            )
            hash_path = os.path.join(self.hash_dir, filename)

            # Only trust a hash if its source pcap is still around. An
            # orphaned *.22000 is either stale or accounted for
            # elsewhere; either way, not our business.
            if not (os.path.exists(pcap_path) and os.path.exists(hash_path)):
                logging.debug(
                    "[hashielist] skipping orphaned hash %s (pcap present=%s)",
                    filename, os.path.exists(pcap_path),
                )
                self._processed_hash_files.add(filename)
                continue

            if self._harvest_one_hash(filename):
                harvested += 1
            self._processed_hash_files.add(filename)

        if harvested:
            logging.info(
                "[hashielist] harvest pass complete: %d new MAC(s) whitelisted",
                harvested,
            )
        return harvested

    def _harvest_one_hash(self, hash_filename):
        """
        Parse a `<ssid>_<bssid>.22000` filename, normalise the BSSID
        into colon form, and append it to the whitelist unless already
        known. Returns True if a new entry was actually written.
        """
        try:
            stem = os.path.splitext(hash_filename)[0]
            # SSIDs can contain underscores, so peel off only the final
            # underscore-delimited token -- that's the BSSID.
            ssid, sep, raw_mac = stem.rpartition('_')
            if not sep:
                logging.warning("[hashielist] unparseable hash filename: %s", hash_filename)
                return False

            raw_mac = raw_mac.lower()
            if len(raw_mac) != 12:
                logging.warning(
                    "[hashielist] unexpected BSSID length in %s: %r",
                    hash_filename, raw_mac,
                )
                return False

            formatted_mac = ':'.join(raw_mac[i:i + 2] for i in range(0, 12, 2))
            if formatted_mac in self._known_entries:
                logging.debug(
                    "[hashielist] MAC already whitelisted: %s (%s)",
                    formatted_mac, ssid,
                )
                return False

            if self._append_whitelist_entry(formatted_mac, ssid or 'auto'):
                logging.info(
                    "[hashielist] whitelisted MAC %s from %s",
                    formatted_mac, ssid or '<no-ssid>',
                )
                return True
            return False
        except Exception as e:
            logging.error(
                "[hashielist] MAC extraction failed for %s: %s",
                hash_filename, e,
            )
            return False

    def _inject_into_runtime(self):
        """
        Mirror the in-memory whitelist into pwnagotchi's live config so
        bettercap stops attacking those BSSIDs immediately, no restart
        required. This is a full replace -- removals from the file
        propagate too, since we're the sole writer here.

        Returns True if the runtime list actually changed.
        """
        try:
            runtime = pwnagotchi.config['main'].setdefault('whitelist', [])
            before = set(runtime)
            desired = sorted(self._known_entries)
            if runtime == desired:
                return False
            runtime[:] = desired
            added = len(set(desired) - before)
            removed = len(before - set(desired))
            logging.info(
                "[hashielist] runtime whitelist synced (+%d -%d, total=%d)",
                added, removed, len(runtime),
            )
            return True
        except Exception as e:
            logging.error("[hashielist] runtime injection failed: %s", e)
            return False

    # ==================================================================
    # VAULT stage: pcap -> 22000 conversion and bookkeeping
    # ==================================================================

    @classmethod
    def _resolve_hcxpcapngtool(cls):
        """
        Locate the hcxpcapngtool binary. Try PATH first so a
        user-installed copy wins, then fall back to the standard
        locations. Returns an absolute path or None.

        Why not just hardcode /usr/bin: hcxtools' Makefile installs
        into /usr/local/bin by default, so a stock pwnagotchi image that
        followed upstream build instructions has the binary there. A
        hardcoded /usr/bin path fails on every such image.
        """
        found = shutil.which('hcxpcapngtool')
        if found:
            return found
        for candidate in cls.HCXPCAPNGTOOL_FALLBACKS:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def _load_noshake_cache(self):
        """
        Load the hidden noshake file. Missing is normal on first boot;
        anything else is logged and non-fatal.
        """
        try:
            with open(self.NOSHAKE_CACHE_PATH, 'rt') as fp:
                self._noshake_cache = {line.strip() for line in fp if line.strip()}
        except FileNotFoundError:
            logging.debug("[hashielist] noshake cache absent, starting fresh")
        except Exception as e:
            logging.error("[hashielist] noshake cache read failed: %s", e)

    def _mark_noshake(self, hash_filename):
        """
        Remember that a pcap was already converted and yielded no
        handshake. Append to the hidden file and update the in-memory
        set so later passes in this process short-circuit without
        touching disk again.
        """
        self._noshake_cache.add(hash_filename)
        try:
            with open(self.NOSHAKE_CACHE_PATH, 'at') as fp:
                fp.write(hash_filename + '\n')
        except Exception as e:
            logging.error("[hashielist] noshake cache write failed: %s", e)

    def _vault_sweep_backlog(self):
        """
        One-shot pass over every pcap already present at load time --
        catches up on captures made while the plugin was off. Same
        settle guard as the recent sweep, because a fresh capture can
        still be in flight when we come up mid-epoch.
        """
        if not self.handshake_dir or not os.path.isdir(self.handshake_dir):
            return
        count = 0
        now = time.time()
        for filename in os.listdir(self.handshake_dir):
            if not filename.endswith('.pcap'):
                continue
            fullpath = os.path.join(self.handshake_dir, filename)
            try:
                mtime = os.path.getmtime(fullpath)
            except OSError:
                continue
            if now - mtime <= self.SETTLE_SECONDS:
                continue
            self._vault_one_pcap(fullpath, mtime)
            count += 1
        logging.info("[hashielist] backlog sweep complete: %d pcap(s) examined", count)

    def _vault_sweep_recent(self):
        """
        Periodic pass over settled pcaps whose mtime moved since we
        last looked at them. Runs on every sleep event to pick up fresh
        captures.
        """
        if not self.handshake_dir or not os.path.isdir(self.handshake_dir):
            return

        # Prune bookkeeping for pcaps that vanished; nothing else keeps
        # this dict bounded on long uptimes.
        alive = set()
        now = time.time()
        for filename in os.listdir(self.handshake_dir):
            if not filename.endswith('.pcap'):
                continue
            fullpath = os.path.join(self.handshake_dir, filename)
            alive.add(fullpath)

            try:
                mtime = os.path.getmtime(fullpath)
            except OSError:
                # File vanished between listdir and stat; skip.
                continue

            if now - mtime <= self.SETTLE_SECONDS:
                continue

            last = self._last_checked_mtime.get(fullpath, 0)
            if mtime > last:
                self._vault_one_pcap(fullpath, mtime)

        for stale in list(self._last_checked_mtime):
            if stale not in alive:
                self._last_checked_mtime.pop(stale, None)

    def _vault_one_pcap(self, pcap_path, current_mtime=None):
        """
        Convert one pcap to *.22000 via hcxpcapngtool. Outcomes:
          - hash already exists        -> nothing to do
          - pcap known to have no shake -> nothing to do
          - conversion succeeds         -> log, record mtime
          - conversion fails            -> remember in noshake cache
        """
        filename = os.path.basename(pcap_path)
        hash_filename = self._pcap_to_hash_name(filename)
        output_hash = os.path.join(self.hash_dir, hash_filename)

        # Short-circuit: hash already produced, or we already know this
        # pcap has no handshake. The noshake cache is why we don't
        # re-run hcxpcapngtool on every empty capture on every boot.
        if os.path.exists(output_hash):
            logging.debug("[hashielist] hash already vaulted: %s", hash_filename)
            return
        if hash_filename in self._noshake_cache:
            logging.debug("[hashielist] known empty capture, skipping: %s", filename)
            return

        # _hcxpcapngtool is resolved in on_loaded() and cleared if the
        # binary was missing at that point. If it's still None here the
        # VAULT stage shouldn't have been armed; bail defensively.
        if not self._hcxpcapngtool:
            logging.error(
                "[hashielist] no hcxpcapngtool path resolved, skipping %s",
                filename,
            )
            return

        logging.info("[hashielist] converting capture: %s", filename)
        try:
            subprocess.run(
                [self._hcxpcapngtool, '-o', output_hash, pcap_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except FileNotFoundError:
            # Binary vanished between load-time resolve and now (package
            # removed, path masked). Log once, disable the stage, and
            # let the next reload re-resolve.
            logging.error(
                "[hashielist] hcxpcapngtool disappeared from %s; "
                "disabling vault stage until reload",
                self._hcxpcapngtool,
            )
            self._vault_enabled = False
            self._hcxpcapngtool = None
            return
        except Exception as e:
            logging.error("[hashielist] hcxpcapngtool failed on %s: %s", filename, e)
            return

        if os.path.exists(output_hash):
            logging.info("[hashielist] vaulted: %s", hash_filename)
            # A brand new .22000 just landed in the hashes folder; clear
            # the cached dir mtime so the next LIST pass re-scans even if
            # the OS didn't bump the directory mtime the way we expect.
            self._hash_dir_mtime = None
        else:
            logging.info("[hashielist] no handshake in capture, remembering: %s", filename)
            self._mark_noshake(hash_filename)

        # Record the mtime we processed at, so a later modification
        # (rare, but possible if a capture is appended to) triggers a
        # re-check.
        if current_mtime is None:
            try:
                current_mtime = os.path.getmtime(pcap_path)
            except OSError:
                current_mtime = time.time()
        self._last_checked_mtime[pcap_path] = current_mtime
