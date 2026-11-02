from pwnagotchi.ui.hw.inky import Inky
from pwnagotchi.ui.hw.papirus import Papirus
from pwnagotchi.ui.hw.oledhat import OledHat
from pwnagotchi.ui.hw.lcdhat import LcdHat
from pwnagotchi.ui.hw.dfrobot1 import DFRobotV1
from pwnagotchi.ui.hw.dfrobot2 import DFRobotV2
from pwnagotchi.ui.hw.waveshare2in13 import WaveshareV1
from pwnagotchi.ui.hw.waveshare2in13_V2 import WaveshareV2
from pwnagotchi.ui.hw.waveshare2in13_V3 import WaveshareV3
from pwnagotchi.ui.hw.waveshare2in13_V4 import WaveshareV4
from pwnagotchi.ui.hw.waveshare1in44lcd import Waveshare144lcd
from pwnagotchi.ui.hw.waveshare2in13bc import Waveshare213bc
from pwnagotchi.ui.hw.waveshare2in13d import Waveshare213d
from pwnagotchi.ui.hw.waveshare2in13g import Waveshare2in13g
from pwnagotchi.ui.hw.waveshare2in13b_V4 import Waveshare213bV4
from pwnagotchi.ui.hw.waveshare3in5lcd import Waveshare35lcd
from pwnagotchi.ui.hw.spotpear24inch import Spotpear24inch
from pwnagotchi.ui.hw.displayhatmini import DisplayHatMini
from pwnagotchi.ui.hw.waveshareoledlcd import Waveshareoledlcd
from pwnagotchi.ui.hw.waveshare1in54 import Waveshare154
from pwnagotchi.ui.hw.waveshare2in13b_V3 import Waveshare2in13bV3
from pwnagotchi.ui.hw.waveshare3in7 import Waveshare3in7
from pwnagotchi.ui.hw.waveshare3in52 import Waveshare3in52
from pwnagotchi.ui.hw.waveshare4in2 import Waveshare4in2
from pwnagotchi.ui.hw.waveshare4in2_V2 import Waveshare4in2V2
from pwnagotchi.ui.hw.waveshare7in5 import Waveshare7in5
from pwnagotchi.ui.hw.waveshare7in5_V2 import Waveshare7in5V2


def display_for(config):
    # config has been normalized already in utils.load_config
    if config['ui']['display']['type'] == 'inky':
        return Inky(config)

    elif config['ui']['display']['type'] == 'papirus':
        return Papirus(config)

    elif config['ui']['display']['type'] == 'oledhat':
        return OledHat(config)

    elif config['ui']['display']['type'] == 'lcdhat':
        return LcdHat(config)

    elif config['ui']['display']['type'] == 'dfrobot_1':
        return DFRobotV1(config)

    elif config['ui']['display']['type'] == 'dfrobot_2':
        return DFRobotV2(config)

    elif config['ui']['display']['type'] == 'waveshare144lcd':
        return Waveshare144lcd(config)

    elif config['ui']['display']['type'] == 'waveshare35lcd':
        return Waveshare35lcd(config)

    elif config['ui']['display']['type'] == 'spotpear24inch':
        return Spotpear24inch(config)

    elif config['ui']['display']['type'] == 'displayhatmini':
        return DisplayHatMini(config)

    elif config['ui']['display']['type'] == 'waveshareoledlcd':
        return Waveshareoledlcd(config)

    elif config['ui']['display']['type'] == 'waveshare1in54':
        return Waveshare154(config)

    elif config['ui']['display']['type'] == 'waveshare_1':
        return WaveshareV1(config)

    elif config['ui']['display']['type'] == 'waveshare_2':
        return WaveshareV2(config)

    elif config['ui']['display']['type'] == 'waveshare_3':
        return WaveshareV3(config)

    elif config['ui']['display']['type'] == 'waveshare_4':
        return WaveshareV4(config)

    elif config['ui']['display']['type'] == 'waveshare2in13bc':
        return Waveshare213bc(config)

    elif config['ui']['display']['type'] == 'waveshare2in13d':
        return Waveshare213d(config)

    elif config['ui']['display']['type'] == 'waveshare2in13b_v3':
        return Waveshare2in13bV3(config)

    elif config['ui']['display']['type'] == 'waveshare2in13b_v4':
        return Waveshare213bV4(config)

    elif config['ui']['display']['type'] == 'waveshare2in13g':
        return Waveshare2in13g(config)

    elif config['ui']['display']['type'] == 'waveshare3in7':
        return Waveshare3in7(config)

    elif config['ui']['display']['type'] == 'waveshare3in52':
        return Waveshare3in52(config)

    elif config['ui']['display']['type'] == 'waveshare4in2':
        return Waveshare4in2(config)

    elif config['ui']['display']['type'] == 'waveshare4in2_v2':
        return Waveshare4in2V2(config)

    elif config['ui']['display']['type'] == 'waveshare7in5':
        return Waveshare7in5(config)

    elif config['ui']['display']['type'] == 'waveshare7in5_v2':
        return Waveshare7in5V2(config)
