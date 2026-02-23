import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
import app.main as main_module
from app.models import User


class AuthCookieProxyTests(unittest.TestCase):
    def setUp(self):
        self._orig_app_env = main_module.APP_ENV
        self._orig_cookie_secure_raw = main_module.COOKIE_SECURE_RAW
        self._orig_cookie_secure_explicit = main_module.COOKIE_SECURE_EXPLICIT
        self._orig_cookie_secure = main_module.COOKIE_SECURE

        main_module.APP_ENV = "production"
        main_module.COOKIE_SECURE_RAW = None
        main_module.COOKIE_SECURE_EXPLICIT = False
        main_module.COOKIE_SECURE = True

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_auth_proxy.db"
        self.engine = create_engine(
            f"sqlite:///{self.db_path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self._orig_session_local = main_module.SessionLocal
        main_module.SessionLocal = self.SessionLocal

        Base.metadata.create_all(bind=self.engine)

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db

        with self.SessionLocal() as db:
            db.add(
                User(
                    email="proxy@example.com",
                    password_hash=main_module._hash_password("password123"),
                    is_active=True,
                )
            )
            db.commit()

    def tearDown(self):
        main_module.APP_ENV = self._orig_app_env
        main_module.COOKIE_SECURE_RAW = self._orig_cookie_secure_raw
        main_module.COOKIE_SECURE_EXPLICIT = self._orig_cookie_secure_explicit
        main_module.COOKIE_SECURE = self._orig_cookie_secure
        main_module.SessionLocal = self._orig_session_local
        app.dependency_overrides.clear()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_login_cookie_without_tls_is_not_secure_by_default(self):
        client = TestClient(app, base_url="http://testserver")

        response = client.post(
            "/auth/login",
            data={"email": "proxy@example.com", "password": "password123"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        set_cookie = response.headers.get("set-cookie", "")
        self.assertIn("session_token=", set_cookie)
        self.assertNotIn("; Secure", set_cookie)

    def test_login_cookie_with_forwarded_https_is_secure(self):
        client = TestClient(app, base_url="http://testserver")

        response = client.post(
            "/auth/login",
            data={"email": "proxy@example.com", "password": "password123"},
            headers={"x-forwarded-proto": "https"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        set_cookie = response.headers.get("set-cookie", "")
        self.assertIn("session_token=", set_cookie)
        self.assertIn("; Secure", set_cookie)


if __name__ == "__main__":
    unittest.main()
