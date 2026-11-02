import random

_DEFAULTS = {
    'LOOK_R': '( ⚆_⚆)',
    'LOOK_L': '(☉_☉ )',
    'LOOK_R_HAPPY': '( ◕‿◕)',
    'LOOK_L_HAPPY': '(◕‿◕ )',
    'SLEEP': '(⇀‿‿↼)',
    'SLEEP2': '(≖‿‿≖)',
    'AWAKE': '(◕‿‿◕)',
    'BORED': '(-__-)',
    'INTENSE': '(°▃▃°)',
    'COOL': '(⌐■_■)',
    'HAPPY': '(•‿‿•)',
    'GRATEFUL': '(^‿‿^)',
    'EXCITED': '(ᵔ◡◡ᵔ)',
    'MOTIVATED': '(☼‿‿☼)',
    'DEMOTIVATED': '(≖__≖)',
    'SMART': '(✜‿‿✜)',
    'LONELY': '(ب__ب)',
    'SAD': '(╥☁╥ )',
    'ANGRY': "(-_-')",
    'FRIEND': '(♥‿‿♥)',
    'BROKEN': '(☓‿‿☓)',
    'BLIND': '(☓‿‿☓)',
    'DEBUG': '(#__#)',
    'UPLOAD': '(1__0)',
    'UPLOAD1': '(1__1)',
    'UPLOAD2': '(0__1)'
}

_CONFIG_FACES = {}

def load_from_config(config):
    """Loads your config.toml face settings into the override dictionary"""
    for face_name, face_value in config.items():
        _CONFIG_FACES[face_name.upper()] = face_value

def __getattr__(name):
    val = _CONFIG_FACES.get(name, _DEFAULTS.get(name))

    if val is not None:
        if isinstance(val, list):
            return random.choice(val)
        return val

    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
