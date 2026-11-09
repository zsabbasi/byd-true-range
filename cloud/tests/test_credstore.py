"""Tests for credstore.py: the BYD sign-in encrypted at rest on THIS computer (AES-256-GCM on Linux/macOS, DPAPI on Windows)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import credstore  # noqa: E402
import db  # noqa: E402

CREDS = {"username": "sub.account@example.com", "password": "Sup3r-S3cret-pa$$word!", "base_url": "https://dilinkappoversea-sg.byd.auto",
         "country_code": "PK", "language": "en", "time_zone": "Asia/Karachi"}
POSIX = sys.platform != "win32"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        db.DB_PATH = self.root / "data" / "history.sqlite"
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")})
        self.env.start()
        for k in ("BYD_APP_KEY_FILE", "CREDENTIALS_DIRECTORY"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def key(self, name="k.key", content=b"0123456789abcdef0123456789abcdef", mode=0o600):
        p = self.root / name
        p.write_bytes(content)
        os.chmod(p, mode)
        return p


class AesStore(Base):
    """The Linux path. Forced with scheme='aes' so it is also tested on a Windows development machine."""

    def test_round_trip(self):
        credstore.save(CREDS, "aes")
        self.assertEqual(credstore.load(), CREDS)

    def test_nothing_stored(self):
        self.assertIsNone(credstore.load())
        self.assertFalse(credstore.exists())
        self.assertIsNone(credstore.version())

    def test_nothing_readable_is_in_the_file(self):
        credstore.save(CREDS, "aes")
        raw = credstore.path().read_bytes()
        for secret in (CREDS["password"], CREDS["username"], "sub.account", "Sup3r", "example.com", "Karachi", "dilinkappoversea"):
            self.assertNotIn(secret.encode(), raw, secret)
        self.assertEqual(raw[len(credstore.MAGIC): len(credstore.MAGIC) + 1], credstore.KIND_AES)

    def test_every_save_uses_a_fresh_salt_and_nonce(self):
        credstore.save(CREDS, "aes")
        a = credstore.path().read_bytes()
        credstore.save(CREDS, "aes")
        b = credstore.path().read_bytes()
        self.assertNotEqual(a, b)
        self.assertEqual(credstore.load(), CREDS)

    def test_the_default_key_is_created_on_save_outside_the_data_folder_and_not_on_load(self):
        self.assertIsNone(credstore.load())
        self.assertFalse(credstore.default_key_path().exists())             # reading never invents a key
        credstore.save(CREDS, "aes")
        kp = credstore.default_key_path()
        self.assertTrue(kp.exists())
        self.assertNotEqual(kp.parent, credstore.path().parent)              # separate from the data folder (backups!)
        self.assertGreaterEqual(len(kp.read_bytes().strip()), 32)
        if POSIX:
            self.assertEqual(oct(kp.stat().st_mode & 0o777), "0o600")
            self.assertEqual(oct(kp.parent.stat().st_mode & 0o777), "0o700")

    def test_the_default_key_is_not_overwritten_by_a_second_save(self):
        credstore.save(CREDS, "aes")
        before = credstore.default_key_path().read_bytes()
        credstore.save(dict(CREDS, username="x@y.z"), "aes")
        self.assertEqual(credstore.default_key_path().read_bytes(), before)
        self.assertEqual(credstore.load()["username"], "x@y.z")

    def test_a_missing_key_means_not_readable_and_says_why(self):
        credstore.save(CREDS, "aes")
        credstore.default_key_path().unlink()
        self.assertIsNone(credstore.load())
        d = credstore.describe()
        self.assertTrue(d["present"])
        self.assertFalse(d["readable"])
        self.assertIn("key file", d["reason"])
        self.assertFalse(credstore.default_key_path().exists())             # and it did not create a new one

    def test_a_wrong_key_cannot_decrypt(self):
        credstore.save(CREDS, "aes")
        credstore.default_key_path().write_bytes(b"a-completely-different-key-0123456789abcdef")
        self.assertIsNone(credstore.load())
        self.assertIn("decrypted", credstore.describe()["reason"])

    def test_tampering_with_the_file_is_detected(self):
        credstore.save(CREDS, "aes")
        p = credstore.path()
        raw = bytearray(p.read_bytes())
        for pos in (len(credstore.MAGIC) + 1 + 2, len(raw) // 2, len(raw) - 3):    # salt, ciphertext, tag
            tampered = bytearray(raw)
            tampered[pos] ^= 0x01
            p.write_bytes(bytes(tampered))
            self.assertIsNone(credstore.load(), pos)

    def test_the_format_header_is_authenticated(self):
        credstore.save(CREDS, "aes")
        p = credstore.path()
        raw = p.read_bytes()
        p.write_bytes(b"BYDCRED2" + raw[len(credstore.MAGIC):])
        self.assertIsNone(credstore.load())

    def test_truncated_or_garbage_files_are_none(self):
        p = credstore.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        credstore.save(CREDS, "aes")
        p.write_bytes(p.read_bytes()[:-8])
        self.assertIsNone(credstore.load())
        for junk in (b"", b"hello", credstore.MAGIC, credstore.MAGIC + b"?xxxx", credstore.MAGIC + credstore.KIND_AES + b"\x00" * 10,
                     credstore.MAGIC + credstore.KIND_AES + b"\x00" * 80, b"\xff" * 100, credstore.MAGIC + b"F" + b'{"a":1}'):
            p.write_bytes(junk)
            self.assertIsNone(credstore.load(), junk[:20])

    def test_key_source_priority_env_then_systemd_then_default(self):
        default = self.key("default.key", b"default-key-default-key-default-key")
        shutil_copy = credstore.default_key_path()
        shutil_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil_copy.write_bytes(default.read_bytes())
        os.chmod(shutil_copy, 0o600)
        self.assertEqual(credstore.key_source()[1], "default")
        cred_dir = self.root / "creds"
        cred_dir.mkdir()
        (cred_dir / "byd_credential_key").write_bytes(b"systemd-key-systemd-key-systemd-key!")
        os.chmod(cred_dir / "byd_credential_key", 0o600)
        with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(cred_dir)}):
            self.assertEqual(credstore.key_source()[1], "systemd")
            envkey = self.key("env.key", b"env-key-env-key-env-key-env-key-env")
            with mock.patch.dict(os.environ, {"BYD_APP_KEY_FILE": str(envkey)}):
                self.assertEqual(credstore.key_source()[1], "env")
                credstore.save(CREDS, "aes")
                self.assertEqual(credstore.load(), CREDS)
            self.assertIsNone(credstore.load())                              # saved with the env key; systemd key is different

    def test_a_systemd_credential_works_end_to_end(self):
        cred_dir = self.root / "creds"
        cred_dir.mkdir()
        (cred_dir / "byd_credential_key").write_bytes(b"systemd-supplied-secret-key-0123456789")
        os.chmod(cred_dir / "byd_credential_key", 0o600)
        with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(cred_dir)}):
            m = credstore.save(CREDS, "aes")
            self.assertIn("systemd", m)
            self.assertEqual(credstore.load(), CREDS)
            self.assertFalse(credstore.default_key_path().exists())          # nothing was written to the home folder

    def test_a_named_key_file_that_is_missing_is_an_error_not_a_silent_new_key(self):
        with mock.patch.dict(os.environ, {"BYD_APP_KEY_FILE": str(self.root / "nope.key")}):
            with self.assertRaises(FileNotFoundError):
                credstore.save(CREDS, "aes")
            self.assertFalse((self.root / "nope.key").exists())
        self.assertFalse(credstore.exists())

    def test_a_too_short_key_is_refused(self):
        with mock.patch.dict(os.environ, {"BYD_APP_KEY_FILE": str(self.key("short.key", b"abc"))}):
            with self.assertRaises(ValueError):
                credstore.save(CREDS, "aes")

    @unittest.skipUnless(POSIX, "permission bits are meaningful on POSIX only")
    def test_a_key_readable_by_others_is_refused(self):
        loose = self.key("loose.key", mode=0o644)
        with mock.patch.dict(os.environ, {"BYD_APP_KEY_FILE": str(loose)}):
            with self.assertRaises(PermissionError):
                credstore.save(CREDS, "aes")
        good = self.key("good.key", mode=0o600)
        with mock.patch.dict(os.environ, {"BYD_APP_KEY_FILE": str(good)}):
            credstore.save(CREDS, "aes")
            os.chmod(good, 0o640)
            self.assertIsNone(credstore.load())
            self.assertIn("chmod 600", credstore.describe()["reason"])

    def test_describe_has_no_secrets_and_no_paths(self):
        credstore.save(CREDS, "aes")
        text = str(credstore.describe())
        for leak in (CREDS["password"], CREDS["username"], self.tmp.name, "credential.key", "xdg"):
            self.assertNotIn(leak, text, leak)
        d = credstore.describe()
        self.assertTrue(d["present"] and d["readable"])
        self.assertIn("outside the data folder", d["key_source"] or "")

    def test_method_name_names_the_key_source(self):
        self.assertIn("AES-256-GCM", credstore.method_name("aes"))
        self.assertIn("config folder", credstore.method_name("aes"))


class Common(Base):
    def test_delete_exists_version(self):
        credstore.save(CREDS)
        self.assertTrue(credstore.exists())
        v = credstore.version()
        self.assertIsNotNone(v)
        self.assertTrue(credstore.delete())
        self.assertFalse(credstore.exists())
        self.assertIsNone(credstore.load())
        self.assertFalse(credstore.delete())
        self.assertNotEqual(credstore.version(), v)

    def test_version_changes_on_every_save(self):
        credstore.save(CREDS)
        os.utime(credstore.path(), (1, 1))
        old = credstore.version()
        credstore.save(dict(CREDS, username="another@example.com"))
        self.assertNotEqual(credstore.version(), old)
        self.assertEqual(credstore.load()["username"], "another@example.com")

    def test_no_temp_file_is_left_behind(self):
        credstore.save(CREDS)
        self.assertEqual(sorted(p.name for p in credstore.path().parent.iterdir()), ["credentials.bin"])

    def test_incomplete_or_oversized_credentials_are_refused(self):
        for bad in ({}, {k: v for k, v in CREDS.items() if k != "password"}, dict(CREDS, password=""), dict(CREDS, username=5),
                    dict(CREDS, password="x" * 600), None, "creds"):
            with self.assertRaises(ValueError):
                credstore.save(bad)
        self.assertFalse(credstore.exists())

    def test_extra_keys_are_not_stored(self):
        credstore.save(dict(CREDS, evil="payload"))
        self.assertNotIn("evil", credstore.load())

    def test_unknown_scheme_is_refused(self):
        with self.assertRaises(ValueError):
            credstore.save(CREDS, "rot13")

    def test_there_is_no_plain_text_store(self):
        credstore.save(CREDS)
        self.assertNotIn(CREDS["password"].encode(), credstore.path().read_bytes())
        self.assertNotIn(b"F", [credstore.path().read_bytes()[len(credstore.MAGIC): len(credstore.MAGIC) + 1]])

    def test_default_scheme_follows_the_platform(self):
        self.assertEqual(credstore.default_scheme(), "dpapi" if sys.platform == "win32" else "aes")


@unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows-only")
class Dpapi(Base):
    def test_round_trip_and_nothing_readable(self):
        credstore.save(CREDS, "dpapi")
        self.assertEqual(credstore.load(), CREDS)
        raw = credstore.path().read_bytes()
        for secret in (CREDS["password"], CREDS["username"], "Sup3r"):
            self.assertNotIn(secret.encode(), raw)
        self.assertEqual(raw[len(credstore.MAGIC): len(credstore.MAGIC) + 1], credstore.KIND_DPAPI)

    def test_a_blob_made_for_another_purpose_is_not_accepted(self):
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
        data = b'{"username": "a", "password": "b"}'
        buf = ctypes.create_string_buffer(data, len(data))
        inb = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out = Blob()
        ctypes.windll.crypt32.CryptProtectData.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                                           ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        self.assertTrue(ctypes.windll.crypt32.CryptProtectData(ctypes.byref(inb), "x", None, None, None, 1, ctypes.byref(out)))
        foreign = ctypes.string_at(out.pbData, out.cbData)
        p = credstore.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(credstore.MAGIC + credstore.KIND_DPAPI + foreign)
        self.assertIsNone(credstore.load())


@unittest.skipUnless(POSIX, "a Windows-made blob cannot be opened on POSIX")
class ForeignBlob(Base):
    def test_a_windows_blob_on_linux_is_not_readable_and_says_so(self):
        p = credstore.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(credstore.MAGIC + credstore.KIND_DPAPI + b"\x01" * 60)
        self.assertIsNone(credstore.load())
        self.assertIn("Windows", credstore.describe()["reason"])


class DataDir(unittest.TestCase):
    def test_env_relocates_the_data_directory(self):
        with mock.patch.dict(os.environ, {"BYD_DATA_DIR": "/var/lib/byd-range"}):
            self.assertEqual(db.data_dir(), Path("/var/lib/byd-range"))

    def test_default_is_next_to_the_code(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BYD_DATA_DIR", None)
            self.assertEqual(db.data_dir().name, "data")


if __name__ == "__main__":
    unittest.main()
