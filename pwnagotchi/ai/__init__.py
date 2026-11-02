import os
import time
import logging


def load(config, agent, epoch, from_disk=True):
    config = config['ai']
    if not config['enabled']:
        logging.info("ai disabled")
        return False

    try:
        begin = time.time()
        logging.info("[ai] bootstrapping PyTorch dependencies ...")

        start = time.time()
        from stable_baselines3 import A2C
        logging.debug("[ai] A2C imported in %.2fs" % (time.time() - start))

        start = time.time()
        from stable_baselines3.common.vec_env import DummyVecEnv
        logging.debug("[ai] DummyVecEnv imported in %.2fs" % (time.time() - start))

        start = time.time()
        import pwnagotchi.ai.gym as wrappers
        logging.debug("[ai] gym wrapper imported in %.2fs" % (time.time() - start))

        env = wrappers.Environment(agent, epoch)
        env = DummyVecEnv([lambda: env])

        logging.info("[ai] creating model ...")
        start = time.time()
        a2c = A2C("MlpPolicy", env, **config['params'])
        logging.debug("[ai] A2C created in %.2fs" % (time.time() - start))

        if from_disk and os.path.exists(config['path']):
            logging.info("[ai] loading %s ..." % config['path'])
            start = time.time()
            try:
                a2c = A2C.load(config['path'], env=env)
                logging.debug("[ai] A2C loaded in %.2fs" % (time.time() - start))

                # a clean load proves compatibility right now, so any prior
                # mismatch marker was transient after all -- clear it.
                pending_path = config['path'] + '.brain-mismatch-flag'
                if os.path.exists(pending_path):
                    try:
                        os.remove(pending_path)
                    except Exception:
                        pass

            except ValueError as e:
                if "do not match" not in str(e):
                    raise

                # SB3's check_for_correct_spaces() raises this ValueError when
                # the env's action/observation space doesn't match the saved
                # model's. two causes look identical:
                #
                # 1. a genuine, permanent structural change (a Parameter
                #    range changed, or one was added/removed) -- retrying
                #    the same load will fail identically every time.
                #
                # 2. a transient boot-time race: supported_channels() came
                #    back empty because mon0 hadn't finished coming up, so
                #    the action space is missing its channel dimensions.
                #
                # only agent.supported_channels() being empty right now can
                # tell these apart. if it's empty, defer judgment and let
                # AsyncTrainer's retry loop handle it.
                if not agent.supported_channels():
                    logging.warning("[ai] %s -- supported_channels() is currently empty (mon0 likely still "
                                    "coming up), deferring judgment" % e)
                    return False

                # channels are known, so this looks permanent -- but require
                # it to reproduce across a restart before discarding a
                # trained model. a persistent marker records the first
                # occurrence; only the second one discards.
                pending_path = config['path'] + '.brain-mismatch-flag'
                if not os.path.exists(pending_path):
                    try:
                        with open(pending_path, 'w') as fp:
                            fp.write(str(e))
                    except Exception as marker_err:
                        logging.error("[ai] failed to write mismatch marker: %s" % marker_err)
                    logging.warning("[ai] %s -- channels are known but the saved model still doesn't "
                                    "match; noting this and waiting to see if it reproduces after a "
                                    "restart before touching brain.nn" % e)
                    return False

                logging.warning("[ai] %s -- saved model is incompatible (confirmed again after a restart), "
                                "starting a fresh one instead" % e)

                backup_dir = os.path.join(os.path.dirname(config['path']), 'brain-backups', 'obsolete')
                try:
                    os.makedirs(backup_dir, exist_ok=True)
                except Exception as mkdir_err:
                    logging.error("[ai] failed to create obsolete-backup dir: %s" % mkdir_err)

                backup_path = os.path.join(backup_dir, os.path.basename(config['path']) + ".obsolete")
                try:
                    os.replace(config['path'], backup_path)
                    logging.info("[ai] backed up incompatible model to %s" % backup_path)
                except Exception as backup_err:
                    logging.error("[ai] failed to back up incompatible model: %s" % backup_err)

                json_path = os.path.splitext(config['path'])[0] + '.json'
                if os.path.exists(json_path):
                    json_backup_path = os.path.join(backup_dir, os.path.basename(json_path) + ".obsolete")
                    try:
                        os.replace(json_path, json_backup_path)
                        logging.info("[ai] backed up incompatible stats to %s" % json_backup_path)
                    except Exception as json_backup_err:
                        logging.error("[ai] failed to back up incompatible stats: %s" % json_backup_err)

                try:
                    os.remove(pending_path)
                except Exception:
                    pass
        else:
            logging.info("[ai] new model created:")
            for key, value in config['params'].items():
                logging.info("      %s: %s" % (key, value))

        logging.debug("[ai] total loading time is %.2fs" % (time.time() - begin))
        return a2c
    except Exception as e:
        logging.exception("error while starting AI (%s)", e)

    logging.warning("[ai] AI not loaded!")
    return False
