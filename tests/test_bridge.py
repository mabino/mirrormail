import os
import sys
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock, call

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import bridge_daemon
import auth_setup

class TestBridgeDaemon(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for config and db files
        self.test_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.test_dir.name, "test_email_bridge.db")
        self.config_path = os.path.join(self.test_dir.name, "test_config.json")
        self.config = {
            "m365_client_id": "test_client_id",
            "m365_email": "m365@example.com",
            "m365_refresh_token": "original_refresh_token",
            "gmail_email": "gmail@example.com",
            "gmail_password": "gmail_password",
            "gmail_auth_method": "app_password",
            "gmail_imap_server": "imap.gmail.com",
            "gmail_imap_port": 993,
            "database_path": self.db_path
        }
        with open(self.config_path, 'w') as f:
            json.dump(self.config, f)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_init_db(self):
        bridge_daemon.init_db(self.db_path)
        self.assertTrue(os.path.exists(self.db_path))
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='processed_emails';")
        self.assertIsNotNone(cursor.fetchone())
        conn.close()

    def test_is_and_mark_email_processed(self):
        bridge_daemon.init_db(self.db_path)
        
        self.assertFalse(bridge_daemon.is_email_processed(self.db_path, "m365@example.com", 12345, 1, "msg-123"))
        
        bridge_daemon.mark_email_processed(self.db_path, "m365@example.com", 12345, 1, "msg-123")
        
        self.assertTrue(bridge_daemon.is_email_processed(self.db_path, "m365@example.com", 12345, 1, None))
        self.assertTrue(bridge_daemon.is_email_processed(self.db_path, "m365@example.com", 99999, 9, "msg-123"))
        self.assertTrue(bridge_daemon.is_email_processed(self.db_path, "m365@example.com", 99999, 9, None))

    def test_generate_xoauth2_string(self):
        auth_str = bridge_daemon.generate_xoauth2_string("test@example.com", "token123")
        expected = "user=test@example.com\x01auth=Bearer token123\x01\x01"
        self.assertEqual(auth_str, expected)

    @patch('urllib.request.urlopen')
    def test_refresh_m365_token(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token"
        }).encode('utf-8')
        mock_urlopen.return_value.__enter__.return_value = mock_response

        access_token = bridge_daemon.refresh_m365_token(self.config, self.config_path)
        
        self.assertEqual(access_token, "new_access_token")
        self.assertEqual(self.config["m365_refresh_token"], "new_refresh_token")

    @patch('urllib.request.urlopen')
    def test_refresh_google_token(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "google_access_token_123"
        }).encode('utf-8')
        mock_urlopen.return_value.__enter__.return_value = mock_response

        config = {
            "gmail_client_id": "google_client_id",
            "gmail_client_secret": "google_client_secret",
            "gmail_refresh_token": "google_refresh_token"
        }
        access_token = bridge_daemon.refresh_google_token(config)
        self.assertEqual(access_token, "google_access_token_123")

    @patch('imaplib.IMAP4_SSL')
    def test_connect_m365_imap(self, mock_imap_ssl):
        mock_instance = MagicMock()
        mock_imap_ssl.return_value = mock_instance
        
        imap = bridge_daemon.connect_m365_imap("m365@example.com", "token123")
        
        self.assertEqual(imap, mock_instance)
        mock_instance.authenticate.assert_called_once()

    @patch('imaplib.IMAP4_SSL')
    def test_connect_gmail_imap(self, mock_imap_ssl):
        mock_instance = MagicMock()
        mock_imap_ssl.return_value = mock_instance
        
        imap = bridge_daemon.connect_gmail_imap("gmail@example.com", "password123")
        
        self.assertEqual(imap, mock_instance)
        mock_instance.login.assert_called_once_with("gmail@example.com", "password123")

    @patch('imaplib.IMAP4_SSL')
    def test_connect_gmail_oauth_imap(self, mock_imap_ssl):
        mock_instance = MagicMock()
        mock_imap_ssl.return_value = mock_instance
        
        imap = bridge_daemon.connect_gmail_oauth_imap("gmail@example.com", "google_token_123")
        
        self.assertEqual(imap, mock_instance)
        mock_instance.authenticate.assert_called_once()

    @patch('urllib.request.urlopen')
    def test_insert_gmail_message_api(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "id": "gmail_msg_api_id_100"
        }).encode('utf-8')
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Test insert message with flags
        msg_id = bridge_daemon.insert_gmail_message_api(
            "google_token_123",
            b"From: test@example.com\r\n\r\nHello",
            ["\\Seen", "\\Flagged"]
        )
        self.assertEqual(msg_id, "gmail_msg_api_id_100")
        
        # Verify post payload
        called_args = mock_urlopen.call_args[0][0]
        self.assertEqual(called_args.method, "POST")
        self.assertEqual(called_args.full_url, "https://gmail.googleapis.com/gmail/v1/users/me/messages")
        self.assertEqual(called_args.headers["Authorization"], "Bearer google_token_123")
        
        # Test request body parsing
        body = json.loads(called_args.data.decode('utf-8'))
        self.assertIn("raw", body)
        self.assertIn("labelIds", body)
        # "\Seen" means read (not UNREAD), "\Flagged" means STARRED
        self.assertEqual(sorted(body["labelIds"]), sorted(["INBOX", "STARRED"]))

    @patch('bridge_daemon.connect_gmail_imap')
    @patch('bridge_daemon.connect_m365_imap')
    @patch('bridge_daemon.refresh_m365_token')
    def test_sync_emails_app_password_copies_new_message(self, mock_refresh, mock_connect_m365, mock_connect_gmail):
        mock_refresh.return_value = "access_token"
        
        # Pre-populate sync state to trigger incremental sync instead of bootstrap
        bridge_daemon.init_db(self.db_path)
        bridge_daemon.update_sync_state(self.db_path, "m365@example.com", 100, 41)
        
        mock_m365 = MagicMock()
        mock_connect_m365.return_value = mock_m365
        mock_m365.status.return_value = ('OK', [b'"INBOX" (UIDVALIDITY 100)'])
        mock_m365.uid.side_effect = [
            ('OK', [b'42']),
            ('OK', [(b'42 (INTERNALDATE "08-Jul-2026 12:00:00 +0000" FLAGS (\\Seen) BODY[HEADER] {50}', b'Message-ID: <test-id-123>\r\n\r\n')]),
            ('OK', [(b'42 (RFC822 {100}', b'From: sender@example.com\r\n\r\nHello World')])
        ]
        
        mock_gmail = MagicMock()
        mock_connect_gmail.return_value = mock_gmail
        mock_gmail.append.return_value = ('OK', [b'Append UID 1'])
        
        bridge_daemon.sync_emails(self.config, self.config_path)
        
        mock_gmail.append.assert_called_once_with(
            'INBOX',
            '(\\Seen)',
            '08-Jul-2026 12:00:00 +0000',
            b'From: sender@example.com\r\n\r\nHello World'
        )
        self.assertTrue(bridge_daemon.is_email_processed(self.db_path, "m365@example.com", 100, 42, "<test-id-123>"))

    @patch('bridge_daemon.insert_gmail_message_api')
    @patch('bridge_daemon.connect_m365_imap')
    @patch('bridge_daemon.refresh_google_token')
    @patch('bridge_daemon.refresh_m365_token')
    def test_sync_emails_oauth2_api_copies_new_message(self, mock_refresh_m365, mock_refresh_google, mock_connect_m365, mock_insert_api):
        mock_refresh_m365.return_value = "m365_token"
        mock_refresh_google.return_value = "google_token"
        
        # Pre-populate sync state to trigger incremental sync instead of bootstrap
        bridge_daemon.init_db(self.db_path)
        bridge_daemon.update_sync_state(self.db_path, "m365@example.com", 200, 98)
        
        mock_m365 = MagicMock()
        mock_connect_m365.return_value = mock_m365
        mock_m365.status.return_value = ('OK', [b'"INBOX" (UIDVALIDITY 200)'])
        mock_m365.uid.side_effect = [
            ('OK', [b'99']),
            ('OK', [(b'99 (INTERNALDATE "08-Jul-2026 12:00:00 +0000" FLAGS () BODY[HEADER] {50}', b'Message-ID: <oauth-id-456>\r\n\r\n')]),
            ('OK', [(b'99 (RFC822 {100}', b'From: sender@example.com\r\n\r\nHello Google API')])
        ]
        
        mock_insert_api.return_value = "gmail_msg_api_id_200"
        
        # Configure for REST API OAuth
        self.config["gmail_auth_method"] = "oauth2_api"
        self.config["gmail_client_id"] = "test_g_id"
        self.config["gmail_client_secret"] = "test_g_secret"
        self.config["gmail_refresh_token"] = "test_g_refresh"
        
        bridge_daemon.sync_emails(self.config, self.config_path)
        
        mock_insert_api.assert_called_once_with(
            "google_token",
            b'From: sender@example.com\r\n\r\nHello Google API',
            []
        )
        self.assertTrue(bridge_daemon.is_email_processed(self.db_path, "m365@example.com", 200, 99, "<oauth-id-456>"))

    @patch('bridge_daemon.connect_gmail_imap')
    @patch('bridge_daemon.connect_m365_imap')
    @patch('bridge_daemon.refresh_m365_token')
    def test_sync_emails_performs_bootstrap_on_first_run(self, mock_refresh, mock_connect_m365, mock_connect_gmail):
        mock_refresh.return_value = "access_token"
        
        mock_m365 = MagicMock()
        mock_connect_m365.return_value = mock_m365
        mock_m365.status.return_value = ('OK', [b'"INBOX" (UIDVALIDITY 100)'])
        mock_m365.uid.return_value = ('OK', [b'40 41 42'])
        
        # Run sync_emails on uninitialized DB
        bridge_daemon.sync_emails(self.config, self.config_path)
        
        # Check that the max UID (42) and UIDVALIDITY (100) were saved to sync_state
        validity, last_uid = bridge_daemon.get_sync_state(self.db_path, "m365@example.com")
        self.assertEqual(validity, 100)
        self.assertEqual(last_uid, 42)
        
        # No emails should have been copied (Gmail IMAP client append was not called)
        mock_connect_gmail.return_value.append.assert_not_called()


class TestM365AuthCodeFlow(unittest.TestCase):
    """Tests for the M365 Authorization Code Flow (the only supported M365 auth method)."""

    @patch('urllib.request.urlopen')
    @patch('builtins.input', return_value='https://login.microsoftonline.com/common/oauth2/nativeclient?code=AUTH_CODE_XYZ&state=12345')
    def test_auth_code_flow_success(self, mock_input, mock_urlopen):
        """Test successful authorization code exchange returns refresh token."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "at_123",
            "refresh_token": "rt_456"
        }).encode('utf-8')
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = auth_setup.run_m365_auth_code_flow("test_client_id", "example.edu")

        self.assertEqual(result, "rt_456")
        # Verify the token exchange request was made
        called_req = mock_urlopen.call_args[0][0]
        self.assertIn("example.edu", called_req.full_url)
        self.assertEqual(called_req.method, "POST")
        body = called_req.data.decode('utf-8')
        self.assertIn("grant_type=authorization_code", body)
        self.assertIn("code=AUTH_CODE_XYZ", body)

    @patch('urllib.request.urlopen')
    @patch('builtins.input', return_value='https://login.microsoftonline.com/common/oauth2/nativeclient?code=CODE_ABC&state=12345')
    def test_auth_code_flow_no_refresh_token(self, mock_input, mock_urlopen):
        """Test that None is returned when response lacks a refresh token."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "at_only"
        }).encode('utf-8')
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = auth_setup.run_m365_auth_code_flow("test_client_id", "example.edu")

        self.assertIsNone(result)

    @patch('builtins.input', return_value='')
    def test_auth_code_flow_empty_input(self, mock_input):
        """Test that empty user input returns None."""
        result = auth_setup.run_m365_auth_code_flow("test_client_id", "example.edu")
        self.assertIsNone(result)

    @patch('urllib.request.urlopen')
    @patch('builtins.input', return_value='https://login.microsoftonline.com/common/oauth2/nativeclient?code=CODE_ERR&state=12345')
    def test_auth_code_flow_http_error(self, mock_input, mock_urlopen):
        """Test that HTTP errors during token exchange return None."""
        mock_error = MagicMock()
        mock_error.code = 400
        mock_error.reason = "Bad Request"
        mock_error.read.return_value = b'{"error":"invalid_grant"}'
        mock_urlopen.side_effect = __import__('urllib.error', fromlist=['HTTPError']).HTTPError(
            url="https://login.microsoftonline.com/example.edu/oauth2/v2.0/token",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=lambda: b'{"error":"invalid_grant"}')
        )

        result = auth_setup.run_m365_auth_code_flow("test_client_id", "example.edu")
        self.assertIsNone(result)

    @patch('urllib.request.urlopen')
    @patch('builtins.input', return_value='BARE_CODE_ONLY')
    def test_auth_code_flow_bare_code_input(self, mock_input, mock_urlopen):
        """Test that a bare authorization code (not a full URL) is handled."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "at_bare",
            "refresh_token": "rt_bare"
        }).encode('utf-8')
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = auth_setup.run_m365_auth_code_flow("test_client_id", "example.edu")

        self.assertEqual(result, "rt_bare")
        # Verify the bare code was sent as-is
        called_req = mock_urlopen.call_args[0][0]
        body = called_req.data.decode('utf-8')
        self.assertIn("code=BARE_CODE_ONLY", body)

    @patch('urllib.request.urlopen')
    @patch('builtins.input', return_value='https://login.microsoftonline.com/common/oauth2/nativeclient?code=CODE_NET&state=12345')
    def test_auth_code_flow_network_error(self, mock_input, mock_urlopen):
        """Test that generic exceptions during token exchange return None."""
        mock_urlopen.side_effect = Exception("Network timeout")

        result = auth_setup.run_m365_auth_code_flow("test_client_id", "example.edu")
        self.assertIsNone(result)


class TestGoogleAuthCodeFlow(unittest.TestCase):
    """Tests for the Google OAuth2 Authorization Code Flow."""

    @patch('urllib.request.urlopen')
    @patch('builtins.input', return_value='http://localhost/?code=G_CODE_123&state=abc')
    def test_google_auth_code_flow_success(self, mock_input, mock_urlopen):
        """Test successful Google token exchange returns refresh token."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "g_at_123",
            "refresh_token": "g_rt_456"
        }).encode('utf-8')
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = auth_setup.run_google_auth_code_flow("google_id", "google_secret")

        self.assertEqual(result, "g_rt_456")
        called_req = mock_urlopen.call_args[0][0]
        self.assertEqual(called_req.full_url, "https://oauth2.googleapis.com/token")
        self.assertEqual(called_req.method, "POST")
        body = called_req.data.decode('utf-8')
        self.assertIn("grant_type=authorization_code", body)
        self.assertIn("code=G_CODE_123", body)
        self.assertIn("client_id=google_id", body)
        self.assertIn("client_secret=google_secret", body)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost", body)

    @patch('urllib.request.urlopen')
    @patch('builtins.input', return_value='http://localhost/?code=G_CODE_456')
    def test_google_auth_code_flow_no_refresh_token(self, mock_input, mock_urlopen):
        """Test that access token is returned as fallback if refresh token is missing."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "g_at_only"
        }).encode('utf-8')
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = auth_setup.run_google_auth_code_flow("google_id", "google_secret")

        self.assertEqual(result, "g_at_only")

    @patch('builtins.input', return_value='')
    def test_google_auth_code_flow_empty_input(self, mock_input):
        """Test that empty input returns None."""
        result = auth_setup.run_google_auth_code_flow("google_id", "google_secret")
        self.assertIsNone(result)

    @patch('urllib.request.urlopen')
    @patch('builtins.input', return_value='http://localhost/?code=G_CODE_ERR')
    def test_google_auth_code_flow_http_error(self, mock_input, mock_urlopen):
        """Test that HTTP errors return None."""
        mock_urlopen.side_effect = __import__('urllib.error', fromlist=['HTTPError']).HTTPError(
            url="https://oauth2.googleapis.com/token",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=lambda: b'{"error":"invalid_grant"}')
        )

        result = auth_setup.run_google_auth_code_flow("google_id", "google_secret")
        self.assertIsNone(result)

    @patch('urllib.request.urlopen')
    @patch('builtins.input', return_value='http://localhost/?code=G_CODE_NET')
    def test_google_auth_code_flow_network_error(self, mock_input, mock_urlopen):
        """Test that general exceptions return None."""
        mock_urlopen.side_effect = Exception("Connection closed")

        result = auth_setup.run_google_auth_code_flow("google_id", "google_secret")
        self.assertIsNone(result)


class TestAuthSetupHelpers(unittest.TestCase):
    """Tests for auth_setup helper functions."""

    def test_get_m365_tenant_custom_domain(self):
        """Custom org domains should return the domain itself."""
        self.assertEqual(auth_setup.get_m365_tenant("user@princeton.edu"), "princeton.edu")
        self.assertEqual(auth_setup.get_m365_tenant("admin@contoso.com"), "contoso.com")

    def test_get_m365_tenant_consumer_domains(self):
        """Personal Microsoft accounts should return 'consumers'."""
        for domain in ("outlook.com", "hotmail.com", "live.com", "msn.com"):
            self.assertEqual(auth_setup.get_m365_tenant(f"user@{domain}"), "consumers")

    def test_get_m365_tenant_missing_email(self):
        """Empty or invalid emails should fall back to 'organizations'."""
        self.assertEqual(auth_setup.get_m365_tenant(""), "organizations")
        self.assertEqual(auth_setup.get_m365_tenant(None), "organizations")
        self.assertEqual(auth_setup.get_m365_tenant("no-at-sign"), "organizations")

    def test_load_config_missing_file(self):
        """Loading a nonexistent config should return an empty dict."""
        result = auth_setup.load_config("/nonexistent/path/config.json")
        self.assertEqual(result, {})

    def test_load_and_save_config(self):
        """Config round-trip through save and load."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test_config.json")
            data = {"m365_email": "test@example.com", "sync_interval_seconds": 60}
            auth_setup.save_config(data, path)
            loaded = auth_setup.load_config(path)
            self.assertEqual(loaded["m365_email"], "test@example.com")
            self.assertEqual(loaded["sync_interval_seconds"], 60)

    def test_device_code_flow_still_available(self):
        """Verify that run_m365_device_flow is still available as a secondary option."""
        self.assertTrue(hasattr(auth_setup, 'run_m365_device_flow'))


class TestM365PasswordConnection(unittest.TestCase):
    """Tests for M365 password-based connections (such as DavMail sidecar proxy)."""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.test_dir.name, "test_password_bridge.db")

    def tearDown(self):
        self.test_dir.cleanup()

    @patch('imaplib.IMAP4')
    def test_connect_m365_password_imap_no_ssl(self, mock_imap):
        mock_instance = MagicMock()
        mock_imap.return_value = mock_instance
        
        imap = bridge_daemon.connect_m365_password_imap(
            "m365@example.com", "secret123", "localhost", 1143, use_ssl=False
        )
        
        self.assertEqual(imap, mock_instance)
        mock_imap.assert_called_once_with("localhost", 1143)
        mock_instance.login.assert_called_once_with("m365@example.com", "secret123")

    @patch('imaplib.IMAP4_SSL')
    def test_connect_m365_password_imap_with_ssl(self, mock_imap_ssl):
        mock_instance = MagicMock()
        mock_imap_ssl.return_value = mock_instance
        
        imap = bridge_daemon.connect_m365_password_imap(
            "m365@example.com", "secret123", "localhost", 993, use_ssl=True
        )
        
        self.assertEqual(imap, mock_instance)
        mock_imap_ssl.assert_called_once_with("localhost", 993)
        mock_instance.login.assert_called_once_with("m365@example.com", "secret123")

    @patch('bridge_daemon.connect_gmail_imap')
    @patch('bridge_daemon.connect_m365_password_imap')
    @patch('bridge_daemon.refresh_m365_token')
    def test_sync_emails_password_auth_does_not_refresh_token(self, mock_refresh, mock_connect_m365, mock_connect_gmail):
        # Setup config for password-based M365 auth
        config = {
            "m365_auth_method": "password",
            "m365_email": "m365@example.com",
            "m365_password": "m365_password_123",
            "m365_imap_server": "davmail-host",
            "m365_imap_port": 1143,
            "m365_imap_use_ssl": False,
            "gmail_email": "gmail@example.com",
            "gmail_password": "gmail_password_123",
            "gmail_auth_method": "app_password",
            "database_path": self.db_path
        }
        
        # Pre-populate sync state to trigger incremental sync instead of bootstrap
        bridge_daemon.init_db(self.db_path)
        bridge_daemon.update_sync_state(self.db_path, "m365@example.com", 100, 41)
        
        mock_m365 = MagicMock()
        mock_connect_m365.return_value = mock_m365
        mock_m365.status.return_value = ('OK', [b'"INBOX" (UIDVALIDITY 100)'])
        mock_m365.uid.side_effect = [
            ('OK', [b'42']),
            ('OK', [(b'42 (INTERNALDATE "08-Jul-2026 12:00:00 +0000" FLAGS (\\Seen) BODY[HEADER] {50}', b'Message-ID: <test-id-123>\r\n\r\n')]),
            ('OK', [(b'42 (RFC822 {100}', b'From: sender@example.com\r\n\r\nHello World')])
        ]
        
        mock_gmail = MagicMock()
        mock_connect_gmail.return_value = mock_gmail
        mock_gmail.append.return_value = ('OK', [b'Append UID 1'])
        
        bridge_daemon.sync_emails(config, "fake_config_path")
        
        # Verify refresh_m365_token was NEVER called
        mock_refresh.assert_not_called()
        
        # Verify connect_m365_password_imap was called with correct args
        mock_connect_m365.assert_called_once_with(
            "m365@example.com",
            "m365_password_123",
            "davmail-host",
            1143,
            False
        )


class TestHealthcheckMonitoring(unittest.TestCase):
    """Tests for healthchecks.io ping reporting functionality."""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.test_dir.name, "test_hc_bridge.db")
        self.config_path = os.path.join(self.test_dir.name, "config.json")
        self.config = {
            "m365_email": "m365@example.com",
            "gmail_email": "gmail@example.com",
            "gmail_auth_method": "app_password",
            "gmail_password": "pass",
            "database_path": self.db_path,
            "healthcheck_url": "https://hc-ping.com/fake-uuid"
        }
        bridge_daemon.init_db(self.db_path)
        bridge_daemon.update_sync_state(self.db_path, "m365@example.com", 100, 41)

    def tearDown(self):
        self.test_dir.cleanup()

    @patch('urllib.request.urlopen')
    def test_ping_healthcheck_success_and_start(self, mock_urlopen):
        """Verify get/start/success endpoint triggers get pings."""
        bridge_daemon.ping_healthcheck("https://hc-ping.com/123", "start")
        called_req = mock_urlopen.call_args[0][0]
        self.assertEqual(called_req.full_url, "https://hc-ping.com/123/start")
        self.assertEqual(called_req.method, "GET")

        bridge_daemon.ping_healthcheck("https://hc-ping.com/123", "success")
        called_req2 = mock_urlopen.call_args[0][0]
        self.assertEqual(called_req2.full_url, "https://hc-ping.com/123")
        self.assertEqual(called_req2.method, "GET")

    @patch('urllib.request.urlopen')
    def test_ping_healthcheck_fail_sends_post_payload(self, mock_urlopen):
        """Verify fail endpoint sends POST request with error payload."""
        bridge_daemon.ping_healthcheck("https://hc-ping.com/123", "fail", "Error traceback detail")
        called_req = mock_urlopen.call_args[0][0]
        self.assertEqual(called_req.full_url, "https://hc-ping.com/123/fail")
        self.assertEqual(called_req.method, "POST")
        self.assertEqual(called_req.data, b"Error traceback detail")
        self.assertEqual(called_req.headers["Content-type"], "text/plain")

    @patch('bridge_daemon.ping_healthcheck')
    @patch('bridge_daemon.connect_gmail_imap')
    @patch('bridge_daemon.connect_m365_imap')
    @patch('bridge_daemon.refresh_m365_token')
    def test_sync_emails_sends_start_and_success_pings(self, mock_refresh, mock_connect_m365, mock_connect_gmail, mock_ping):
        mock_refresh.return_value = "at_123"
        
        mock_m365 = MagicMock()
        mock_connect_m365.return_value = mock_m365
        mock_m365.status.return_value = ('OK', [b'"INBOX" (UIDVALIDITY 100)'])
        mock_m365.uid.return_value = ('OK', [b''])
        
        bridge_daemon.sync_emails(self.config, self.config_path)
        
        mock_ping.assert_any_call("https://hc-ping.com/fake-uuid", "start")
        mock_ping.assert_any_call("https://hc-ping.com/fake-uuid", "success")

    @patch('bridge_daemon.ping_healthcheck')
    @patch('bridge_daemon.connect_gmail_imap')
    @patch('bridge_daemon.connect_m365_imap')
    @patch('bridge_daemon.refresh_m365_token')
    def test_sync_emails_sends_fail_ping_on_exception(self, mock_refresh, mock_connect_m365, mock_connect_gmail, mock_ping):
        mock_refresh.return_value = "at_123"
        
        mock_m365 = MagicMock()
        mock_connect_m365.return_value = mock_m365
        mock_m365.status.side_effect = Exception("IMAP connection broken")
        
        with self.assertRaises(Exception):
            bridge_daemon.sync_emails(self.config, self.config_path)
            
        mock_ping.assert_any_call("https://hc-ping.com/fake-uuid", "start")
        mock_ping.assert_any_call(
            "https://hc-ping.com/fake-uuid", 
            "fail", 
            unittest.mock.ANY
        )


if __name__ == '__main__':
    unittest.main()
