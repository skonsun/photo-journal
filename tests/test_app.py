"""HTTP integration tests; all photos and credentials are disposable fixtures."""
import http.cookiejar
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
TEST_PASSWORD = "unit-test-admin-secret-123456"


class AppTests(unittest.TestCase):
    @classmethod
    def start_app(cls, data, password=TEST_PASSWORD, originals=True):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {
            **os.environ,
            "PHOTO_ROOT": str(cls.photos),
            "DATA_ROOT": str(data),
            "PORT": str(port),
            "ADMIN_PASSWORD": password,
            "SCAN_INTERVAL": "5",
            "ALLOW_ORIGINAL": str(originals).lower(),
        }
        process = subprocess.Popen(
            [sys.executable, str(PROJECT / "server.py")],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        base = f"http://127.0.0.1:{port}"
        for _ in range(150):
            if process.poll() is not None:
                raise AssertionError(process.stderr.read())
            try:
                with urllib.request.urlopen(base + "/health", timeout=0.3) as response:
                    if response.status == 200:
                        return process, base
            except (OSError, urllib.error.URLError):
                time.sleep(0.05)
        process.terminate()
        process.wait(timeout=10)
        raise AssertionError("App did not become ready")

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.photos = cls.root / "photos"
        cls.photos.mkdir()
        for number in range(1, 62):
            exif = Image.Exif()
            exif[36867] = (datetime(2020, 1, 1) + timedelta(days=number)).strftime(
                "%Y:%m:%d %H:%M:%S"
            )
            Image.new("RGB", (160, 100), (number * 3 % 255, 90, 120)).save(
                cls.photos / f"{62-number:03}.jpg", exif=exif
            )
        cls.outside = cls.root / "outside.jpg"
        Image.new("RGB", (30, 30), "red").save(cls.outside)
        try:
            (cls.photos / "outside-link.jpg").symlink_to(cls.outside)
        except OSError:
            pass
        cls.process, cls.base = cls.start_app(cls.root / "data")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)
        cls.process.stderr.close()
        cls.temp.cleanup()

    def client(self, base=None):
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )
        opener.base = base or self.base
        return opener

    def request(self, path, body=None, client=None, method=None, origin=None):
        client = client or self.client()
        headers = {}
        if body is not None:
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if origin:
            headers["Origin"] = origin
        return client.open(
            urllib.request.Request(client.base + path, body, headers, method=method),
            timeout=5,
        )

    def json(self, path, body=None, client=None):
        with self.request(path, body, client) as response:
            return json.load(response)

    def login(self, base=None):
        client = self.client(base)
        self.json("/api/admin/login", {"password": TEST_PASSWORD}, client)
        return client

    def gallery(self, base=None):
        client = self.login(base)
        result = self.json(
            "/api/admin/save",
            {"title": "Fixture collection", "folder": "", "sort": "taken", "cover": ""},
            client,
        )
        gid = result["id"]
        for _ in range(200):
            info = self.json("/api/gallery/" + gid, client=client)
            if not info["indexing"]:
                return client, gid, info
            time.sleep(0.05)
        self.fail("Gallery indexing did not complete")

    def test_health_and_head(self):
        self.assertTrue(self.json("/health")["ok"])
        with self.request("/health", method="HEAD") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")

    def test_management_requires_authentication(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.json("/api/admin/state")
        self.assertEqual(error.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.json("/api/admin/login", {"password": "incorrect-password"})
        self.assertEqual(error.exception.code, 401)
        self.assertIn("galleries", self.json("/api/admin/state", client=self.login()))

    def test_exif_order_and_pagination(self):
        client, gid, first = self.gallery()
        second = self.json(f"/api/gallery/{gid}?offset=24", client=client)
        third = self.json(f"/api/gallery/{gid}?offset=48", client=client)
        self.assertEqual(first["count"], 61)
        self.assertEqual([len(x["photos"]) for x in (first, second, third)], [24, 24, 13])
        self.assertEqual(first["photos"][0]["name"], "061.jpg")
        self.assertEqual(first["photos"][0]["dateSource"], "exif")
        combined = first["photos"] + second["photos"] + third["photos"]
        self.assertEqual(len({x["id"] for x in combined}), 61)
        self.assertFalse(third["hasMore"])
        self.assertNotIn("path", combined[0])
        self.assertNotIn("folder", first)

    def test_thumbnail_cache_and_original_identity(self):
        client, gid, info = self.gallery()
        photo = info["photos"][0]
        with self.request(f"/image/{gid}/{photo['id']}?v={photo['v']}") as response:
            self.assertIn("immutable", response.headers["Cache-Control"])
            preview = response.read()
        self.assertIsNone(Image.open(io.BytesIO(preview)).getexif().get(36867))
        path = f"/original/{gid}/{photo['id']}?v={photo['v']}"
        with self.request(path) as response:
            self.assertEqual(response.read(), (self.photos / photo["name"]).read_bytes())
        with self.request(path, method="HEAD") as response:
            self.assertEqual(int(response.headers["Content-Length"]), (self.photos / photo["name"]).stat().st_size)

    def test_folder_boundary_and_cross_origin_post(self):
        client = self.login()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.json("/api/admin/folders?path=..", client=client)
        self.assertEqual(error.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "/api/admin/save",
                {"title": "Invalid", "folder": ""},
                client,
                origin="https://untrusted.invalid",
            )
        self.assertEqual(error.exception.code, 403)

    def test_cancel_share_revokes_original(self):
        client, gid, info = self.gallery()
        photo = info["photos"][0]
        self.json("/api/admin/delete", {"id": gid}, client)
        for path in (f"/api/gallery/{gid}", f"/original/{gid}/{photo['id']}"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(path)
            self.assertEqual(error.exception.code, 404)

    def test_add_and_delete_updates_version(self):
        client, gid, info = self.gallery()
        fixture = self.photos / "new-fixture.jpg"
        try:
            Image.new("RGB", (60, 60), "green").save(fixture)
            self.json("/api/admin/rescan", {"id": gid}, client)
            updated = self.json(f"/api/gallery/{gid}?versionOnly=1", client=client)
            self.assertEqual(updated["count"], 62)
            self.assertNotEqual(updated["version"], info["version"])
        finally:
            fixture.unlink(missing_ok=True)
        self.json("/api/admin/rescan", {"id": gid}, client)
        self.assertEqual(self.json(f"/api/gallery/{gid}?versionOnly=1")["count"], 61)

    def test_original_access_can_be_disabled(self):
        process, base = self.start_app(self.root / "no-originals", originals=False)
        try:
            client, gid, info = self.gallery(base)
            self.assertFalse(info["allowOriginal"])
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(f"/original/{gid}/{info['photos'][0]['id']}", client=client)
            self.assertEqual(error.exception.code, 403)
        finally:
            process.terminate()
            process.wait(timeout=10)
            process.stderr.close()

    def test_placeholder_password_is_rejected(self):
        env = {
            **os.environ,
            "PHOTO_ROOT": str(self.photos),
            "DATA_ROOT": str(self.root / "weak-password"),
            "ADMIN_PASSWORD": "CHANGE_ME_example_only",
        }
        result = subprocess.run(
            [sys.executable, str(PROJECT / "server.py")],
            env=env, capture_output=True, text=True, timeout=5,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ADMIN_PASSWORD", result.stderr)


if __name__ == "__main__":
    unittest.main()
