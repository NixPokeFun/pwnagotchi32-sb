import _thread
import threading
import time
import random
import os
import json
import logging

import pwnagotchi.plugins as plugins
import pwnagotchi.ai as ai


class Stats(object):
    def __init__(self, path, events_receiver):
        self._lock = threading.Lock()
        self._receiver = events_receiver

        self.path = path
        self.born_at = time.time()
        self.epochs_lived = 0
        self.epochs_trained = 0

        self.worst_reward = 0.0
        self.best_reward = 0.0

        self.load()

    def on_epoch(self, data, training):
        best_r = False
        worst_r = False
        with self._lock:
            reward = data['reward']
            if reward < self.worst_reward:
                self.worst_reward = reward
                worst_r = True

            elif reward > self.best_reward:
                best_r = True
                self.best_reward = reward

            self.epochs_lived += 1
            if training:
                self.epochs_trained += 1

        self.save()

        if best_r:
            self._receiver.on_ai_best_reward(reward)
        elif worst_r:
            self._receiver.on_ai_worst_reward(reward)

    def load(self):
        with self._lock:
            if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
                logging.info("[AI] loading %s" % self.path)
                with open(self.path, 'rt') as fp:
                    obj = json.load(fp)

                self.born_at = obj['born_at']
                self.epochs_lived, self.epochs_trained = obj['epochs_lived'], obj['epochs_trained']
                self.best_reward, self.worst_reward = obj['rewards']['best'], obj['rewards']['worst']

    def save(self):
        with self._lock:
            logging.info("[AI] saving %s" % self.path)

            data = json.dumps({
                'born_at': self.born_at,
                'epochs_lived': self.epochs_lived,
                'epochs_trained': self.epochs_trained,
                'rewards': {
                    'best': self.best_reward,
                    'worst': self.worst_reward
                }
            })

            temp = "%s.tmp" % self.path
            back = "%s.bak" % self.path
            with open(temp, 'wt') as fp:
                fp.write(data)

            if os.path.isfile(self.path):
                os.replace(self.path, back)
            os.replace(temp, self.path)


class AsyncTrainer(object):
    CHANNEL_CAP = 6

    def __init__(self, config):
        self._config = config
        self._model = None
        self._is_training = False
        self._training_epochs = 0
        self._nn_path = self._config['ai']['path']
        self._stats = Stats("%s.json" % os.path.splitext(self._nn_path)[0], self)
        self._ai_hold = threading.Event()

    def set_training(self, training, for_epochs=0):
        self._is_training = training
        self._training_epochs = for_epochs

        if training:
            self._view.set('mode', 'LEARN')
            plugins.on('ai_training_start', self, for_epochs)
        else:
            self._view.set('mode', 'AUTO' if self._ai_hold.is_set() else 'EVAL')
            plugins.on('ai_training_end', self)

    def is_training(self):
        return self._is_training

    def training_epochs(self):
        return self._training_epochs

    def start_ai(self):
        self._view.set('mode', 'AUTO')
        self._ai_hold.set()
        _thread.start_new_thread(self._ai_worker, ())

    def hold_ai(self, reason=None):
        if not self._ai_hold.is_set():
            logging.info("[AI] stopping engine (standby) ...")
            self._ai_hold.set()

    def release_ai(self, reason=None):
        if self._ai_hold.is_set():
            logging.info("[AI] starting engine ...")
            self._ai_hold.clear()

    def is_ai_held(self):
        return self._ai_hold.is_set()

    def _save_ai(self):
        logging.info("[AI] saving model to %s ...", self._nn_path)
        temp = "%s.tmp" % self._nn_path
        self._model.save(temp)
        os.replace(temp, self._nn_path)

    def _render_env_safe(self):
        try:
            if hasattr(self._model.env, 'envs'):
                self._model.env.envs[0].render()
            else:
                self._model.env.render()
        except Exception:
            pass

    def _limit_channels(self, channels):
        if not channels:
            return channels
        if len(channels) <= self.CHANNEL_CAP:
            return channels
        preferred = [c for c in (1, 6, 11) if c in channels]
        rest = [c for c in sorted(channels) if c not in preferred]
        return (preferred + rest)[:self.CHANNEL_CAP]

    def on_ai_step(self):
        self._render_env_safe()

        if self._is_training:
            self._save_ai()

        self._stats.on_epoch(self._epoch.data(), self._is_training)

    def on_ai_training_step(self, _locals, _globals):
        self._render_env_safe()
        plugins.on('ai_training_step', self, _locals, _globals)
        return True

    def on_ai_policy(self, new_params):
        if 'channels' in new_params:
            new_params['channels'] = self._limit_channels(new_params['channels'])

        plugins.on('ai_policy', self, new_params)
        logging.info("[AI] setting new policy:")
        for name, value in new_params.items():
            if name in self._config['personality']:
                curr_value = self._config['personality'][name]
                if curr_value != value:
                    logging.info("[AI] ! %s: %s -> %s" % (name, curr_value, value))
                    self._config['personality'][name] = value
            else:
                logging.error("[AI] param %s not in personality configuration!" % name)

        self.run('set wifi.ap.ttl %d' % self._config['personality']['ap_ttl'])
        self.run('set wifi.sta.ttl %d' % self._config['personality']['sta_ttl'])
        self.run('set wifi.rssi.min %d' % self._config['personality']['min_rssi'])

    def on_ai_ready(self):
        plugins.on('ai_ready', self)

    def on_ai_best_reward(self, r):
        logging.info("[AI] best reward so far: %s" % r)
        self._view.on_motivated(r)
        plugins.on('ai_best_reward', self, r)

    def on_ai_worst_reward(self, r):
        logging.info("[AI] worst reward so far: %s" % r)
        self._view.on_demotivated(r)
        plugins.on('ai_worst_reward', self, r)

    def _ai_worker(self):
        self._model = ai.load(self._config, self, self._epoch)

        if self._model:
            self.on_ai_ready()

            epochs_per_episode = self._config['ai']['epochs_per_episode']

            obs = None
            was_held = False
            while True:
                if self._ai_hold.is_set():
                    if not was_held:
                        self._view.set('mode', 'AUTO')
                        was_held = True
                    time.sleep(1)
                    continue
                was_held = False

                self._render_env_safe()

                try:
                    if random.random() > self._config['ai']['laziness']:
                        logging.info("[AI] learning for %d epochs ..." % epochs_per_episode)
                        try:
                            self.set_training(True, epochs_per_episode)
                            if os.path.isfile(self._nn_path):
                                back = "%s.bak" % self._nn_path
                                os.replace(self._nn_path, back)
                            self._model.learn(total_timesteps=epochs_per_episode, callback=self.on_ai_training_step)
                        except Exception as e:
                            logging.exception("[AI] error while training (%s)", e)
                        finally:
                            self.set_training(False)
                            obs = self._model.env.reset()
                    elif obs is None:
                        obs = self._model.env.reset()

                    if self._ai_hold.is_set():
                        continue

                    action, _ = self._model.predict(obs)
                    obs, _, _, _ = self._model.env.step(action)
                except Exception as e:
                    logging.exception("[AI] error during AI step (%s)", e)
                    obs = None
                    time.sleep(1)
        else:
            self._view.set('mode', 'AUTO')
