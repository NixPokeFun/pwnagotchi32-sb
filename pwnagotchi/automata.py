import logging
import copy

import pwnagotchi
import pwnagotchi.plugins as plugins
from pwnagotchi.ai.epoch import Epoch


class Automata(object):
    def __init__(self, config, view):
        self._config = config
        self._view = view
        self._epoch = Epoch(config)

        self._ai_user_intent = config.get('ai', {}).get('enabled', False)
        self._personality_baseline = copy.deepcopy(config['personality'])

        self._away_from_home_epochs = config['personality'].get('home_absence_cooldown', 5)

        self._resume_after_active_epochs = config['personality'].get('resume_after_active_epochs', 2)

        self._frozen_bssid_set = set()

    def _on_miss(self, who):
        logging.info("it looks like %s is not in range anymore :/", who)
        self._epoch.track(miss=True)
        self._view.on_miss(who)

    def _on_error(self, who, e):
        if 'is an unknown BSSID' in str(e):
            self._on_miss(who)
        else:
            logging.error(e)

    def set_starting(self):
        self._view.on_starting()

    def set_ready(self):
        plugins.on('ready', self)

    def in_good_mood(self):
        return self._has_support_network_for(1.0)

    def _has_support_network_for(self, factor):
        bond_factor = self._config['personality']['bond_encounters_factor']
        total_encounters = sum(peer.encounters for _, peer in self._peers.items())
        support_factor = total_encounters / bond_factor
        return support_factor >= factor

    def set_grateful(self):
        self._view.on_grateful()
        plugins.on('grateful', self)

    def set_lonely(self):
        if not self._has_support_network_for(1.0):
            logging.info("unit is lonely")
            self._view.on_lonely()
            plugins.on('lonely', self)
        else:
            logging.info("unit is grateful instead of lonely")
            self.set_grateful()

    def set_bored(self):
        factor = self._epoch.inactive_for / self._config['personality']['bored_num_epochs']
        if not self._has_support_network_for(factor):
            logging.warning("%d epochs with no activity -> bored", self._epoch.inactive_for)
            self._view.on_bored()
            plugins.on('bored', self)
        else:
            logging.info("unit is grateful instead of bored")
            self.set_grateful()

    def set_sad(self):
        factor = self._epoch.inactive_for / self._config['personality']['sad_num_epochs']
        if not self._has_support_network_for(factor):
            logging.warning("%d epochs with no activity -> sad", self._epoch.inactive_for)
            self._view.on_sad()
            plugins.on('sad', self)
        else:
            logging.info("unit is grateful instead of sad")
            self.set_grateful()

    def set_angry(self, factor):
        if not self._has_support_network_for(factor):
            logging.warning("%d epochs with no activity -> angry", self._epoch.inactive_for)
            self._view.on_angry()
            plugins.on('angry', self)
        else:
            logging.info("unit is grateful instead of angry")
            self.set_grateful()

    def set_excited(self):
        logging.warning("%d epochs with activity -> excited", self._epoch.active_for)
        self._view.on_excited()
        plugins.on('excited', self)

    def set_rebooting(self):
        self._view.on_rebooting()
        plugins.on('rebooting', self)

    def wait_for(self, t, sleeping=True):
        plugins.on('sleep' if sleeping else 'wait', self, t)
        self._view.wait(t, sleeping)
        self._epoch.track(sleep=True, inc=t)

    def is_stale(self):
        return self._epoch.num_missed > self._config['personality']['max_misses_for_recon']

    def any_activity(self):
        return self._epoch.any_activity

    def _reset_personality_to_baseline(self):
        changed = {
            name: (self._config['personality'].get(name), value)
            for name, value in self._personality_baseline.items()
            if self._config['personality'].get(name) != value
        }

        self._config['personality'].update(self._personality_baseline)

        if changed:
            logging.info("[AUTO] restoring baseline personality:")
            for name, (old, new) in changed.items():
                logging.info("[AUTO] ! %s: %s -> %s" % (name, old, new))

        self.run('set wifi.ap.ttl %d' % self._config['personality']['ap_ttl'])
        self.run('set wifi.sta.ttl %d' % self._config['personality']['sta_ttl'])
        self.run('set wifi.rssi.min %d' % self._config['personality']['min_rssi'])

    def _capture_bssid_set(self):
        return {ap['mac'] for ap in self._access_points}

    def _relocated_since_pause(self):
        current = self._capture_bssid_set()

        if not self._frozen_bssid_set:
            return bool(current)

        still_present = self._frozen_bssid_set & current
        remaining_fraction = len(still_present) / len(self._frozen_bssid_set)
        threshold = self._config['personality'].get('relocation_bssid_drop_ratio', 0.5)
        return remaining_fraction <= (1 - threshold)

    def _hold_ai_with_snapshot(self):
        self._frozen_bssid_set = self._capture_bssid_set()
        self.mode = 'auto'
        self._view.set('mode', 'AUTO')
        self.hold_ai()

    def next_epoch(self):
        logging.debug("agent.next_epoch()")

        was_stale = self.is_stale()
        did_miss = self._epoch.num_missed

        self._epoch.next()

        if self._ai_user_intent:
            if self.mode == 'auto' and self.is_ai_held() and not self.is_training():
                self._reset_personality_to_baseline()

            home_networks = self._config['main'].get('home_networks', [])
            home_visible = bool(home_networks) and self.is_whitelisted_ap_visible()

            if home_networks:
                self._away_from_home_epochs = 0 if home_visible else self._away_from_home_epochs + 1

            home_cooldown = self._config['personality'].get('home_absence_cooldown', 5)
            home_on_cooldown = bool(home_networks) and self._away_from_home_epochs < home_cooldown

            if home_visible and not self.is_ai_held():
                logging.info("[AI HOLD] home network visible, switching to AUTO")
                self._hold_ai_with_snapshot()

            elif self.mode == 'ai' and (self._epoch.bored_for >= 1 or self._epoch.sad_for >= 1) \
                    and not self.is_training():
                logging.info("[AI HOLD] bored/sad, switching to AUTO")
                self._hold_ai_with_snapshot()

            elif self.mode == 'auto' and not self.is_training() \
                    and not home_visible and not home_on_cooldown \
                    and self._epoch.inactive_for == 0 \
                    and self._epoch.active_for >= self._resume_after_active_epochs \
                    and self._relocated_since_pause():
                logging.info("[AI] there is activity, launching AI ...")
                self.mode = 'ai'
                self._view.set('mode', 'EVAL')
                self.release_ai()

        if was_stale:
            factor = did_miss / self._config['personality']['max_misses_for_recon']
            if factor >= 2.0:
                self.set_angry(factor)
            else:
                logging.warning("agent missed %d interactions -> lonely", did_miss)
                self.set_lonely()
        elif self._epoch.sad_for:
            factor = self._epoch.inactive_for / self._config['personality']['sad_num_epochs']
            if factor >= 2.0:
                self.set_angry(factor)
            else:
                self.set_sad()
        elif self._epoch.bored_for:
            self.set_bored()
        elif self._epoch.active_for >= self._config['personality']['excited_num_epochs']:
            self.set_excited()
        elif self._epoch.active_for >= 5 and self._has_support_network_for(5.0):
            self.set_grateful()

        plugins.on('epoch', self, self._epoch.epoch - 1, self._epoch.data())
