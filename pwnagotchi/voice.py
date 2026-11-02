import gettext
import os
import random
import re


class Voice:
    def __init__(self, lang):
        localedir = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'locale')
        translation = gettext.translation(
            'voice', localedir,
            languages=[lang],
            fallback=True,
        )
        translation.install()
        self._ = translation.gettext
        self._n = translation.ngettext

    def _safe_format(self, template, **kwargs):
        """Safely format string avoiding KeyError or crash if PO file has missing/invalid keys."""
        try:
            return template.format(**kwargs)
        except (KeyError, ValueError, IndexError):
            def replace(match):
                key = match.group(1)
                return str(kwargs[key]) if key in kwargs else match.group(0)
            return re.sub(r'\{([a-zA-Z0-9_]+)\}', replace, str(template))

    def custom(self, s):
        return s

    def default(self):
        return self._('ZzzzZZzzzzZzzz')

    def on_starting(self):
        return random.choice([
            self._("Hi, I'm Pwnagotchi! Starting ..."),
            self._("New day, new hunt, new pwns!"),
            self._("Hack the Planet!"),
            self._("No more mister Wi-Fi!!"),
            self._("Pretty fly 4 a Wi-Fi!"),
            self._("Sniff. Deauth. Repeat."),
            self._("Good Pwning!"),
            self._("Ensign, Engage!"),
            self._("Free your Wi-Fi!"),
            self._("Chevron Seven, locked."),
            self._("May the Wi-fi be with you"),
        ])

    def on_ai_ready(self):
        return random.choice([
            self._('AI ready.'),
            self._('The neural network is ready.')
        ])

    def on_keys_generation(self):
        return random.choice([
            self._('Generating keys, do not turn off ...'),
            self._('Are you the keymaster?'),
            self._('I am the keymaster!'),
        ])

    def on_normal(self):
        return random.choice(['', '...'])

    def on_free_channel(self, channel):
        return self._safe_format(
            self._('Hey, channel {channel} is free! Your AP will say thanks.'),
            channel=channel
        )

    def on_reading_logs(self, lines_so_far=0):
        if lines_so_far == 0:
            return self._('Reading last session logs ...')
        return self._safe_format(
            self._('Read {lines_so_far} log lines so far ...'),
            lines_so_far=lines_so_far
        )

    def on_bored(self):
        return random.choice([
            self._("I'm bored ..."),
            self._("Let's go for a walk!")
        ])

    def on_blind(self, blind_for):
        return random.choice([
            self._safe_format(self._("I'm BLIND! ({blind_for})"), blind_for=blind_for),
            self._safe_format(self._("I can't see anything! ({blind_for})"), blind_for=blind_for),
            self._safe_format(self._("Where did the Wi-Fi go? ({blind_for})"), blind_for=blind_for)
        ])

    def on_motivated(self, reward):
        return random.choice([
            self._('This is the best day of my life!'),
            self._('All your base are belong to us'),
            self._('Fascinating!'),
        ])

    def on_demotivated(self, reward):
        return self._('Shitty day :/')

    def on_sad(self):
        return random.choice([
            self._("I'm extremely bored ..."),
            self._("I'm very sad ..."),
            self._("I'm sad"),
            self._("I'm so happy ..."),
            self._("Life? Don't talk to me about life."),
            '...'
        ])

    def on_angry(self):
        return random.choice([
            '...',
            self._('Leave me alone ...'),
            self._("I'm mad at you!")
        ])

    def on_excited(self):
        return random.choice([
            self._("I'm living the life!"),
            self._('I pwn therefore I am.'),
            self._('So many networks!!!'),
            self._("I'm having so much fun!"),
            self._("It's a Wi-Fi system! I know this!"),
            self._('My crime is that of curiosity ...')
        ])

    def on_new_peer(self, peer):
        if peer.first_encounter():
            return self._safe_format(
                self._('Hello {name}! Nice to meet you.'),
                name=peer.name()
            )
        return random.choice([
            self._safe_format(self._('Yo {name}! Sup?'), name=peer.name()),
            self._safe_format(self._('Hey {name} how are you doing?'), name=peer.name()),
            self._safe_format(self._('Unit {name} is nearby!'), name=peer.name())
        ])

    def on_lost_peer(self, peer):
        return random.choice([
            self._safe_format(self._('Uhm ... goodbye {name}'), name=peer.name()),
            self._safe_format(self._('{name} is gone ...'), name=peer.name())
        ])

    def on_miss(self, who):
        return random.choice([
            self._safe_format(self._('Whoops ... {name} is gone.'), name=who),
            self._safe_format(self._('{name} missed!'), name=who),
            self._('Missed!')
        ])

    def on_grateful(self):
        return random.choice([
            self._('Good friends are a blessing!'),
            self._('I love my friends!')
        ])

    def on_lonely(self):
        return random.choice([
            self._('Nobody wants to play with me ...'),
            self._('I feel so alone ...'),
            self._("Let's find friends"),
            self._("Where's everybody?!")
        ])

    def on_napping(self, secs):
        return random.choice([
            self._safe_format(self._('Napping for {secs}s ...'), secs=secs),
            self._('Zzzzz'),
            self._('Snoring ...'),
            self._safe_format(self._('ZzzZzzz ({secs}s)'), secs=secs)
        ])

    def on_shutdown(self):
        return random.choice([
            self._('Good night.'),
            self._('Zzz')
        ])

    def on_awakening(self):
        return random.choice([
            '...',
            '!',
            'Hello World!',
            self._('I dreamed of electric sheep')
        ])

    def on_waiting(self, secs):
        return random.choice([
            '...',
            self._safe_format(self._('Waiting for {secs}s ...'), secs=secs),
            self._safe_format(self._('Scanning for {secs}s ...'), secs=secs),
            self._safe_format(self._('Looking around ({secs}s)'), secs=secs)
        ])

    def on_assoc(self, ap):
        ssid, bssid = ap.get('hostname', ''), ap.get('mac', '')
        what = ssid if ssid and ssid != '<hidden>' else bssid
        return random.choice([
            self._safe_format(self._("Hey {what} let's be friends!"), what=what),
            self._safe_format(self._('Associating to {what}'), what=what),
            self._safe_format(self._('Yo {what}!'), what=what),
            self._safe_format(self._('Hello there, {what}'), what=what),
            self._safe_format(self._('Mind if I join, {what}?'), what=what),
            self._safe_format(self._('Rise and Shine Mr. {what}!'), what=what)
        ])

    def on_deauth(self, sta):
        mac = sta.get('mac', 'station')
        return random.choice([
            self._safe_format(self._('Just decided that {mac} needs no Wi-Fi!'), mac=mac),
            self._safe_format(self._('Deauthenticating {mac}'), mac=mac),
            self._safe_format(self._('No more Wi-Fi for {mac}'), mac=mac),
            self._safe_format(self._("It's a trap! {mac}"), mac=mac),
            self._safe_format(self._('Consider yourself unplugged, {mac}'), mac=mac),
            self._safe_format(self._('Hasta la vista, {mac}'), mac=mac),
            self._safe_format(self._('You shall not pass, {mac}'), mac=mac),
            self._safe_format(self._('Kickbanning {mac}!'), mac=mac)
        ])

    def on_handshakes(self, new_shakes):
        msg = self._n(
            'Cool, we got {num} new handshake!',
            'Cool, we got {num} new handshakes!',
            new_shakes
        )
        return self._safe_format(msg, num=new_shakes)

    def on_unread_messages(self, count, total):
        msg = self._n(
            'You have {count} new message!',
            'You have {count} new messages!',
            count
        )
        return self._safe_format(msg, count=count)

    def on_rebooting(self):
        return random.choice([
            self._("Oops, something went wrong ... Rebooting ..."),
            self._("Well, this is awkward."),
            self._("Tell my packets I love them."),
            self._("Have you tried turning it off and on again?"),
            self._("I'm afraid Dave"),
            self._("I'm dead, Jim!"),
            self._("I have a bad feeling about this"),
            self._("You did this.")
        ])

    def on_uploading(self, to):
        return random.choice([
            self._safe_format(self._("Uploading data to {to} ..."), to=to),
            self._safe_format(self._("Beam me up to {to}"), to=to),
            self._safe_format(self._("Engage warp drive, {to}"), to=to),
            self._safe_format(self._("Gift-wrapping data for {to}"), to=to),
            self._safe_format(self._("Please wait, magic happening at {to}"), to=to)
        ])

    def on_downloading(self, name):
        return self._safe_format(self._("Downloading from {name} ..."), name=name)

    # OTA Update Methods
    def on_update_available(self, version):
        return self._safe_format(self._("Update available: {version}!"), version=version)

    def on_update_cleaning(self):
        return self._("Cleaning up tmp ...")

    def on_update_installing(self, version):
        return random.choice([
            self._safe_format(self._("Installing {version} ..."), version=version),
            self._safe_format(self._("Upgrading myself to {version} ..."), version=version)
        ])

    def on_update_downloading(self, version):
        return random.choice([
            self._safe_format(self._("Downloading {version} ..."), version=version),
            self._safe_format(self._("Fetching {version} from the mothership ..."), version=version)
        ])

    def on_update_extracting(self, version):
        return self._safe_format(self._("Extracting {version} ..."), version=version)

    def on_update_checking_deps(self):
        return self._("Checking sys deps ...")

    def on_update_installing_deps(self):
        return self._("Installing sys deps ...")

    def on_update_installing_core(self):
        return self._("Installing Python core ...")

    def on_update_verifying_deps(self):
        return self._("Verifying package installations ...")

    def on_update_installed(self, version):
        return random.choice([
            self._safe_format(self._("Installed {version}, restarting service ..."), version=version),
            self._safe_format(self._("All done! Restarting on {version} ..."), version=version)
        ])

    def on_update_restarting(self):
        return self._("Restarting to apply update ...")

    def on_update_failed(self, version):
        return random.choice([
            self._safe_format(self._("Install failed, staying on {version}"), version=version),
            self._safe_format(self._("Update failed! Staying on {version} for now."), version=version)
        ])

    def on_last_session_data(self, last_session):
        status = self._safe_format(self._('Kicked {num} stations\n'), num=last_session.deauthed)
        if last_session.associated > 999:
            status += self._('Made >999 new friends\n')
        else:
            status += self._safe_format(self._('Made {num} new friends\n'), num=last_session.associated)

        status += self._safe_format(self._('Got {num} handshakes\n'), num=last_session.handshakes)

        if last_session.peers > 0:
            msg = self._n('Met {num} peer', 'Met {num} peers', last_session.peers)
            status += self._safe_format(msg, num=last_session.peers)
        return status

    def on_last_session_tweet(self, last_session):
        return self._safe_format(
            self._("I've been pwning for {duration} and kicked {deauthed} clients! "
                   "I've also met {associated} new friends and ate {handshakes} handshakes! "
                   "#pwnagotchi #pwnlog #pwnlife #hacktheplanet #skynet"),
            duration=last_session.duration_human,
            deauthed=last_session.deauthed,
            associated=last_session.associated,
            handshakes=last_session.handshakes
        )

    def hhmmss(self, count, fmt):
        if fmt == 'h':
            return self._n("hour", "hours", count)
        elif fmt == 'm':
            return self._n("minute", "minutes", count)
        elif fmt == 's':
            return self._n("second", "seconds", count)
        return fmt
