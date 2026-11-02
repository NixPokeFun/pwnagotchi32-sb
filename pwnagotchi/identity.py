import base64
import hashlib
import logging
import os
import subprocess

from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_PSS

DefaultPath = "/etc/pwnagotchi/"


class KeyPair(object):
    def __init__(self, path=DefaultPath, view=None):
        self.path = path
        self.priv_path = os.path.join(path, "id_rsa")
        self.priv_key = None
        self.pub_path = "%s.pub" % self.priv_path
        self.pub_key = None
        self.fingerprint_path = os.path.join(path, "fingerprint")
        self._view = view

        if not os.path.exists(self.path):
            os.makedirs(self.path)

        while True:
            # Generate new RSA keys if at least one key file is missing
            if not os.path.exists(self.priv_path) or not os.path.exists(self.pub_path):
                if self._view and hasattr(self._view, 'on_keys_generation'):
                    self._view.on_keys_generation()

                logging.info("Generating new RSA keys at %s ...", self.priv_path)
                try:
                    subprocess.run(
                        ["pwngrid", "-generate", "-keys", self.path],
                        check=True,
                        capture_output=True,
                        text=True
                    )
                except subprocess.CalledProcessError as cpe:
                    logging.error("Failed to generate keys via pwngrid: %s", cpe.stderr)
                    # Clean up partial key files and retry generation
                    self._cleanup_corrupted_keys()
                    continue

            # Load and validate existing keys
            try:
                with open(self.priv_path, "r", encoding="utf-8") as fp:
                    self.priv_key = RSA.importKey(fp.read())

                with open(self.pub_path, "r", encoding="utf-8") as fp:
                    self.pub_key = RSA.importKey(fp.read())

                self.pub_key_pem = self.pub_key.exportKey('PEM').decode("ascii")

                # Ensure compatibility with Go-based pwngrid PEM parser
                if 'RSA PUBLIC KEY' not in self.pub_key_pem:
                    self.pub_key_pem = self.pub_key_pem.replace('PUBLIC KEY', 'RSA PUBLIC KEY')

                pem_ascii = self.pub_key_pem.encode("ascii")
                self.pub_key_pem_b64 = base64.b64encode(pem_ascii).decode("ascii")
                self.fingerprint = hashlib.sha256(pem_ascii).hexdigest()

                # Atomic write for fingerprint file to prevent corruption on sudden power loss
                tmp_fingerprint_path = f"{self.fingerprint_path}.tmp"
                with open(tmp_fingerprint_path, "w", encoding="utf-8") as fp:
                    fp.write(self.fingerprint)
                os.replace(tmp_fingerprint_path, self.fingerprint_path)

            except (ValueError, TypeError, OSError) as e:
                # Catch invalid key structures (ValueError/TypeError) or I/O errors (OSError)
                logging.exception("Corrupted key or I/O error (%s), cleaning up and retrying...", e)
                self._cleanup_corrupted_keys()
                continue

            # Keys loaded and verified successfully
            break

        # Trigger UI update safely outside the key generation/validation loop
        if self._view and hasattr(self._view, 'on_starting'):
            try:
                self._view.on_starting()
            except Exception as e:
                logging.error("Error updating view state on startup: %s", e)

    def _cleanup_corrupted_keys(self):
        """Safely remove corrupted key pair files, fingerprint, and temp files."""
        for p in (self.priv_path, self.pub_path, self.fingerprint_path, f"{self.fingerprint_path}.tmp"):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError as e:
                logging.warning("Could not remove file %s: %s", p, e)

    def sign(self, message):
        """Sign a message using the private RSA key with PKCS1_PSS padding."""
        hasher = SHA256.new(message.encode("ascii"))
        signer = PKCS1_PSS.new(self.priv_key, saltLen=16)
        signature = signer.sign(hasher)
        signature_b64 = base64.b64encode(signature).decode("ascii")
        return signature, signature_b64
