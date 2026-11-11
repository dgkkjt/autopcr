import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from quart_auth import current_user

from autopcr.http_server import httpserver


class OneTimeLoginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        cache = self.directory.name
        for name, value in (
            ('CACHE_DIR', cache),
            ('CACHE_HTTP_DIR', cache),
            ('ONE_TIME_TOKENS_FILE', str(Path(cache) / 'one_time_tokens.json')),
        ):
            patcher = patch.object(httpserver, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = httpserver.HttpServer()
        self.app = self.server.quart
        self.app.config['TESTING'] = True
        self.app.register_blueprint(self.server.app)

        @self.app.route('/test-current-user')
        async def authenticated_user():
            return current_user.auth_id or '', 200

        self.client = self.app.test_client()
        self.token = 'test-one-time-token'
        self.url = '/daily/api/one_time_login'
        self.seed_token(int(time.time()) + 120)

    def seed_token(self, expires_at):
        httpserver._save_one_time_tokens({
            self.token: {'qid': '12345', 'expires_at': expires_at, 'used': False},
        })

    async def test_previews_do_not_consume_link_and_post_logs_in_once(self):
        for method in ('HEAD', 'GET', 'GET'):
            response = await self.client.open(
                self.url, method=method, query_string={'token': self.token},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertIn(self.token, httpserver._load_one_time_tokens())
        body = await response.get_data(as_text=True)
        self.assertIn('确认登录', body)
        self.assertIn('method="post"', body)
        self.assertIn('action="/daily/api/one_time_login"', body)
        response = await self.client.get('/test-current-user')
        self.assertEqual(await response.get_data(as_text=True), '')

        response = await self.client.post(self.url, form={'token': self.token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/daily/account')
        self.assertNotIn(self.token, httpserver._load_one_time_tokens())
        response = await self.client.get('/test-current-user')
        self.assertEqual(await response.get_data(as_text=True), '12345')

        response = await self.client.post(self.url, form={'token': self.token})
        self.assertEqual(response.status_code, 400)
        response = await self.client.get(self.url, query_string={'token': self.token})
        self.assertEqual(response.status_code, 400)

    async def test_token_expiring_between_confirmation_and_post_is_rejected(self):
        response = await self.client.get(self.url, query_string={'token': self.token})
        self.assertEqual(response.status_code, 200)
        with patch.object(httpserver.time, 'time', return_value=time.time() + 121):
            response = await self.client.post(self.url, form={'token': self.token})
        self.assertEqual(response.status_code, 400)
        response = await self.client.get('/test-current-user')
        self.assertEqual(await response.get_data(as_text=True), '')

    async def test_missing_or_unknown_token_is_rejected(self):
        for token in ('', 'unknown-token'):
            response = await self.client.get(self.url, query_string={'token': token})
            self.assertEqual(response.status_code, 400)
            response = await self.client.post(self.url, form={'token': token})
            self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
