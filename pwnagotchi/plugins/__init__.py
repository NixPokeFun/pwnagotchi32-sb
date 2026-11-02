import ast
import glob
import importlib.util
import logging
import os
import queue
import threading

try:
    import prctl
except ImportError:  # pragma: no cover - optional dependency
    prctl = None


default_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "default")

loaded = {}
database = {}
locks = {}
plugin_event_queues = {}


def _set_thread_name(name):
    if prctl is not None:
        try:
            prctl.set_name(name)
        except Exception:
            logging.debug("could not set thread name to %s", name)


class PluginEventQueue(threading.Thread):
    """Dedicated worker thread and event queue for one plugin.

    Running each plugin on its own thread/queue keeps a slow or blocking
    callback (notably ``on_load`` used as a main loop by some legacy plugins)
    from starving every other plugin in the system.
    """

    def __init__(self, plugin_name):
        super().__init__(daemon=True)
        self.plugin_name = plugin_name
        self.work_queue = queue.Queue()
        self.keep_going = True
        self.load_handler = None
        logging.debug("plugin event queue for %s starting", plugin_name)
        self.start()

    def stop(self):
        self.keep_going = False

    def add_work(self, event_name, *args, **kwargs):
        if event_name == "loaded":
            # Some plugins implement ``on_load`` as a blocking main loop. Run
            # it on a dedicated thread so the queue keeps serving other events
            # for the same plugin.
            callback = getattr(loaded.get(self.plugin_name), 'on_loaded', None)
            if callable(callback):
                self.load_handler = threading.Thread(
                    target=self._handle_event,
                    args=(event_name, *args),
                    kwargs=kwargs,
                    daemon=True,
                    name="R1_%s_%s" % (self.plugin_name, event_name),
                )
                self.load_handler.start()
            else:
                self.load_handler = None
        else:
            self.work_queue.put((event_name, args, kwargs))

    def run(self):
        _set_thread_name("PLG %s" % self.plugin_name)
        logging.debug("worker thread starting for %s", self.plugin_name)
        while self.keep_going:
            try:
                event_name, args, kwargs = self.work_queue.get(timeout=2)
            except queue.Empty:
                continue
            self._handle_event(event_name, *args, **kwargs)
        logging.info("worker thread exiting for %s", self.plugin_name)

    def _handle_event(self, event_name, *args, **kwargs):
        _set_thread_name("R1_%s_%s" % (self.plugin_name, event_name))
        callback = getattr(loaded.get(self.plugin_name), 'on_%s' % event_name, None)
        if not callable(callback):
            return
        try:
            callback(*args, **kwargs)
        except Exception:
            # Exceptions raised inside a fire-and-forget worker are otherwise
            # swallowed by the threading machinery and the plugin silently
            # stops working; log them explicitly with a full traceback.
            logging.exception(
                "plugin %s failed while handling %s (args=%r kwargs=%r)",
                self.plugin_name, event_name, args, kwargs,
            )


class Plugin:
    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        plugin_name = cls.__module__.split('.')[0]
        plugin_instance = cls()
        logging.debug("loaded plugin %s as %s", plugin_name, plugin_instance)
        loaded[plugin_name] = plugin_instance

        for attr_name in plugin_instance.__dir__():
            if attr_name.startswith('on_'):
                cb = getattr(plugin_instance, attr_name, None)
                if cb is not None and callable(cb):
                    locks["%s::%s" % (plugin_name, attr_name)] = threading.Lock()


def _queue_for(plugin_name):
    if plugin_name not in plugin_event_queues:
        plugin_event_queues[plugin_name] = PluginEventQueue(plugin_name)
    return plugin_event_queues[plugin_name]


def _persist_enabled_flag(name, enable, config, config_path='/etc/pwnagotchi/config.toml'):
    """Persist ``main.plugins.<name>.enabled`` to ``config.toml``.

    When the file already exists we only touch that single key so user
    comments, formatting and unrelated settings (including flat dotted keys
    like ``personality.max_interactions``) survive untouched -- appending a
    brand new nested table via ``tomlkit.table()`` can silently absorb the
    flat dotted lines that follow it, so a missing key is added as its own
    flat dotted line instead.

    When the file does not exist yet we fall back to ``save_config`` so the
    default template is materialised on disk, matching the behaviour users
    expect when enabling a plugin from the web UI on a fresh install.
    """
    if not os.path.exists(config_path):
        from pwnagotchi.utils import save_config
        try:
            save_config(config, config_path)
        except Exception:
            logging.exception("could not create %s for plugin %s", config_path, name)
        return

    try:
        import tomlkit

        with open(config_path, 'r') as fp:
            doc = tomlkit.parse(fp.read())

        try:
            doc['main']['plugins'][name]['enabled'] = enable
        except (KeyError, TypeError):
            doc.append(
                tomlkit.key(['main', 'plugins', name, 'enabled']),
                tomlkit.item(enable),
            )

        with open(config_path, 'w') as fp:
            fp.write(tomlkit.dumps(doc))
    except Exception:
        logging.exception(
            "targeted write failed for plugin %s, falling back to save_config", name
        )
        try:
            from pwnagotchi.utils import save_config
            save_config(config, config_path)
        except Exception:
            logging.exception("fallback save_config also failed for plugin %s", name)


def toggle_plugin(name, enable=True):
    """Load or unload a plugin at runtime.

    Returns True if the plugin's runtime state changed, otherwise False.
    """
    import pwnagotchi
    from pwnagotchi.ui import view

    if pwnagotchi.config:
        plugins_cfg = pwnagotchi.config['main'].setdefault('plugins', {})
        plugins_cfg.setdefault(name, {})['enabled'] = enable

    if not enable and name in loaded:
        unload_cb = getattr(loaded[name], 'on_unload', None)
        if callable(unload_cb):
            try:
                unload_cb(view.ROOT)
            except Exception:
                logging.exception("on_unload failed for plugin %s", name)
        del loaded[name]
        queue_obj = plugin_event_queues.pop(name, None)
        if queue_obj is not None:
            queue_obj.stop()
        if pwnagotchi.config:
            _persist_enabled_flag(name, False, pwnagotchi.config)
        return True

    if enable and name in database and name not in loaded:
        try:
            load_from_file(database[name])
        except Exception:
            logging.exception("could not load plugin %s", name)
            return False

        if name in loaded and pwnagotchi.config:
            loaded[name].options = pwnagotchi.config['main']['plugins'].get(name, {})

        one(name, 'loaded')
        if pwnagotchi.config:
            one(name, 'config_changed', pwnagotchi.config)
        one(name, 'ui_setup', view.ROOT)
        one(name, 'ready', view.ROOT._agent)
        if pwnagotchi.config:
            _persist_enabled_flag(name, True, pwnagotchi.config)
        return True

    return False

def on(event_name, *args, **kwargs):
    for plugin_name in list(loaded.keys()):
        one(plugin_name, event_name, *args, **kwargs)


def one(plugin_name, event_name, *args, **kwargs):
    if plugin_name not in loaded:
        return
    callback = getattr(loaded[plugin_name], 'on_%s' % event_name, None)
    if callable(callback):
        _queue_for(plugin_name).add_work(event_name, *args, **kwargs)


def load_from_file(filename):
    logging.debug("loading %s", filename)
    plugin_name = os.path.basename(filename.replace(".py", ""))
    spec = importlib.util.spec_from_file_location(plugin_name, filename)
    instance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(instance)
    _queue_for(plugin_name)
    return plugin_name, instance


def load_from_path(path, enabled=()):
    logging.debug("loading plugins from %s - enabled: %s", path, enabled)
    for filename in glob.glob(os.path.join(path, "*.py")):
        plugin_name = os.path.basename(filename.replace(".py", ""))
        database[plugin_name] = filename
        if plugin_name in enabled:
            try:
                load_from_file(filename)
            except Exception as e:
                logging.warning("error while loading %s: %s", filename, e)
                logging.debug(e, exc_info=True)
    return loaded


def get_plugin_metadata(filepath):
    """Statically parse plugin metadata without importing the module.

    Extracts ``__description__``, ``__author__`` and ``__version__`` class
    attributes from a plugin source file. The plugin is never executed, so
    this is safe for untrusted or broken files.
    """
    metadata = {'__description__': None, '__author__': None, '__version__': None}
    try:
        with open(filepath, 'rt', errors='ignore') as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if not isinstance(stmt, ast.Assign):
                    continue
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and target.id in metadata:
                        value = stmt.value
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            metadata[target.id] = value.value
    except Exception as e:
        logging.debug("could not statically parse plugin metadata from %s: %s", filepath, e)
    return metadata


def load(config):
    try:
        enabled = [name for name, options in config['main']['plugins'].items()
                   if options.get('enabled')]

        load_from_path(default_path, enabled=enabled)

        custom_path = config['main'].get('custom_plugins')
        if custom_path:
            load_from_path(custom_path, enabled=enabled)

        for name, plugin in loaded.items():
            plugin.options = config['main']['plugins'].get(name, {})

        on('loaded')
        on('config_changed', config)
    except Exception:
        logging.exception("failed to load plugins")
