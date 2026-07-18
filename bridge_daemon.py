#!/usr/bin/env python3
import argparse
import base64
import email
import imaplib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.parse
import urllib.error

def init_db(db_path):
    """
    Initializes the SQLite database schema for tracking processed emails.
    """
    db_dir = os.path.dirname(os.path.abspath(db_path))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
        
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS processed_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                m365_email TEXT NOT NULL,
                uid_validity INTEGER NOT NULL,
                uid INTEGER NOT NULL,
                message_id TEXT,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(m365_email, uid_validity, uid)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_uid ON processed_emails(m365_email, uid_validity, uid)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_msg_id ON processed_emails(message_id)')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sync_state (
                m365_email TEXT PRIMARY KEY,
                uid_validity INTEGER NOT NULL,
                last_processed_uid INTEGER NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
    finally:
        conn.close()

def get_sync_state(db_path, m365_email):
    """
    Retrieves the sync state (uid_validity, last_processed_uid) for a given email address.
    Returns (uid_validity, last_processed_uid) or (None, None).
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT uid_validity, last_processed_uid FROM sync_state WHERE m365_email = ?
        ''', (m365_email,))
        row = cursor.fetchone()
        if row:
            return row[0], row[1]
    finally:
        conn.close()
    return None, None

def update_sync_state(db_path, m365_email, uid_validity, last_processed_uid):
    """
    Updates or inserts the sync state for a given email address.
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO sync_state (m365_email, uid_validity, last_processed_uid, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ''', (m365_email, uid_validity, last_processed_uid))
        conn.commit()
    finally:
        conn.close()

def ping_healthcheck(url, status="success", message=None):
    """
    Sends a ping to healthchecks.io.
    status can be 'success', 'start', or 'fail'.
    """
    if not url:
        return
    
    ping_url = url
    if status == "start":
        ping_url = f"{url.rstrip('/')}/start"
    elif status == "fail":
        ping_url = f"{url.rstrip('/')}/fail"
        
    try:
        headers = {}
        data = None
        if message:
            data = message.encode('utf-8')
            headers["Content-Type"] = "text/plain"
        
        req = urllib.request.Request(ping_url, data=data, headers=headers, method='POST' if data else 'GET')
        with urllib.request.urlopen(req, timeout=5) as response:
            response.read()
    except Exception as e:
        print(f"Warning: Failed to send healthcheck ping ({status}): {e}")

def is_email_processed(db_path, m365_email, uid_validity, uid, message_id=None):
    """
    Checks if a message has already been processed using either its UID or Message-ID.
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        
        # Check by UID
        cursor.execute('''
            SELECT 1 FROM processed_emails 
            WHERE m365_email = ? AND uid_validity = ? AND uid = ?
        ''', (m365_email, uid_validity, uid))
        if cursor.fetchone():
            return True
            
        # Check by Message-ID if available
        if message_id:
            cursor.execute('''
                SELECT 1 FROM processed_emails 
                WHERE message_id = ?
            ''', (message_id,))
            if cursor.fetchone():
                # Store this UID mapping to speed up future checks
                cursor.execute('''
                    INSERT OR IGNORE INTO processed_emails (m365_email, uid_validity, uid, message_id)
                    VALUES (?, ?, ?, ?)
                ''', (m365_email, uid_validity, uid, message_id))
                conn.commit()
                return True
    finally:
        conn.close()
    return False

def mark_email_processed(db_path, m365_email, uid_validity, uid, message_id=None):
    """
    Marks an email as processed in the database.
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO processed_emails (m365_email, uid_validity, uid, message_id)
            VALUES (?, ?, ?, ?)
        ''', (m365_email, uid_validity, uid, message_id))
        conn.commit()
    finally:
        conn.close()

def refresh_m365_token(config, config_path):
    """
    Refreshes the Microsoft 365 OAuth2 access token.
    Updates config.json if a new refresh token is returned.
    """
    tenant = config.get("m365_tenant", "organizations")
    token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    payload = {
        "grant_type": "refresh_token",
        "client_id": config["m365_client_id"],
        "refresh_token": config["m365_refresh_token"],
        "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
    }
    
    data = urllib.parse.urlencode(payload).encode('utf-8')
    req = urllib.request.Request(token_url, data=data, headers=headers, method='POST')
    
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
    except Exception as e:
        raise Exception(f"Failed to refresh M365 token: {e}")
        
    access_token = res_data.get("access_token")
    if not access_token:
        raise Exception("Access token not found in Microsoft refresh response.")
        
    new_refresh_token = res_data.get("refresh_token")
    if new_refresh_token and new_refresh_token != config["m365_refresh_token"]:
        config["m365_refresh_token"] = new_refresh_token
        try:
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=4)
            print("Successfully saved refreshed Microsoft 365 token to config.")
        except Exception as e:
            print(f"Warning: Failed to save updated Microsoft refresh token: {e}")
            
    return access_token

def refresh_google_token(config):
    """
    Refreshes the Google OAuth2 access token.
    """
    token_url = "https://oauth2.googleapis.com/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    payload = {
        "grant_type": "refresh_token",
        "client_id": config["gmail_client_id"],
        "client_secret": config["gmail_client_secret"],
        "refresh_token": config["gmail_refresh_token"]
    }
    
    data = urllib.parse.urlencode(payload).encode('utf-8')
    req = urllib.request.Request(token_url, data=data, headers=headers, method='POST')
    
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
    except Exception as e:
        raise Exception(f"Failed to refresh Google token: {e}")
        
    access_token = res_data.get("access_token")
    if not access_token:
        raise Exception("Access token not found in Google refresh response.")
    return access_token

def generate_xoauth2_string(email_addr, access_token):
    """
    Generates the SASL XOAUTH2 authentication string.
    """
    auth_string = f"user={email_addr}\x01auth=Bearer {access_token}\x01\x01"
    return auth_string

def connect_m365_imap(email_addr, access_token):
    """
    Establishes IMAP connection to Microsoft 365 and authenticates using SASL XOAUTH2.
    """
    host = "outlook.office365.com"
    port = 993
    try:
        imap = imaplib.IMAP4_SSL(host, port)
        auth_string = generate_xoauth2_string(email_addr, access_token)
        imap.authenticate('XOAUTH2', lambda x: auth_string)
        return imap
    except Exception as e:
        raise Exception(f"Microsoft 365 IMAP authentication failed: {e}")

def connect_m365_password_imap(email_addr, password, host, port, use_ssl=False):
    """
    Establishes IMAP connection to Microsoft 365 (via DavMail or local gateway) using standard password login.
    """
    try:
        if use_ssl:
            imap = imaplib.IMAP4_SSL(host, port)
        else:
            imap = imaplib.IMAP4(host, port)
        imap.login(email_addr, password)
        return imap
    except Exception as e:
        raise Exception(f"Microsoft 365 IMAP login failed on {host}:{port}: {e}")

def connect_gmail_imap(email_addr, password, host="imap.gmail.com", port=993):
    """
    Establishes IMAP connection to Gmail and logs in.
    """
    try:
        imap = imaplib.IMAP4_SSL(host, port)
        imap.login(email_addr, password)
        return imap
    except Exception as e:
        raise Exception(f"Gmail IMAP login failed for {email_addr}: {e}")

def connect_gmail_oauth_imap(email_addr, access_token, host="imap.gmail.com", port=993):
    """
    Establishes IMAP connection to Gmail and authenticates using SASL XOAUTH2.
    """
    try:
        imap = imaplib.IMAP4_SSL(host, port)
        auth_string = generate_xoauth2_string(email_addr, access_token)
        imap.authenticate('XOAUTH2', lambda x: auth_string)
        return imap
    except Exception as e:
        raise Exception(f"Gmail IMAP XOAUTH2 authentication failed: {e}")

def insert_gmail_message_api(access_token, raw_bytes, flags=None):
    """
    Inserts a message into the Gmail mailbox using Google's REST API.
    """
    url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    
    # Map IMAP flags to Gmail labelIds
    label_ids = ["INBOX"]
    if flags:
        flags_lower = [f.lower() for f in flags]
        if "\\seen" not in flags_lower:
            label_ids.append("UNREAD")
        if "\\flagged" in flags_lower:
            label_ids.append("STARRED")
    else:
        label_ids.append("UNREAD")
        
    # Base64url encode the raw RFC822 bytes
    encoded_raw = base64.urlsafe_b64encode(raw_bytes).decode('utf-8').rstrip('=')
    
    payload = {
        "raw": encoded_raw,
        "labelIds": label_ids
    }
    
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    req_data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=req_data, headers=headers, method='POST')
    
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            return res_data.get("id")
    except urllib.error.HTTPError as e:
        try:
            err_content = e.read().decode('utf-8')
            raise Exception(f"Gmail API HTTP {e.code}: {err_content}")
        except Exception:
            raise Exception(f"Gmail API HTTP {e.code}: {e.reason}")
    except Exception as e:
        raise Exception(f"Gmail API call failed: {e}")

def sync_emails(config, config_path):
    """
    Runs one cycle of email synchronization.
    """
    db_path = config.get("database_path", "email_bridge.db")
    init_db(db_path)
    
    hc_url = config.get("healthcheck_url")
    if hc_url:
        ping_healthcheck(hc_url, "start")
        
    try:
        m365_auth_method = config.get("m365_auth_method", "oauth2")
        gmail_method = config.get("gmail_auth_method", "oauth2_api")
        
        m365_access_token = None
        if m365_auth_method == "oauth2":
            print("Refreshing Microsoft 365 token...")
            m365_access_token = refresh_m365_token(config, config_path)
        
        google_access_token = None
        if gmail_method in ("oauth2_imap", "oauth2_api"):
            print("Refreshing Google OAuth2 token...")
            google_access_token = refresh_google_token(config)
        
        print(f"Connecting to Microsoft 365 IMAP for {config['m365_email']}...")
        m365_login_user = config.get("m365_upn", config["m365_email"])
        if m365_auth_method == "password":
            m365_imap = connect_m365_password_imap(
                m365_login_user,
                config["m365_password"],
                config.get("m365_imap_server", "localhost"),
                config.get("m365_imap_port", 1143),
                config.get("m365_imap_use_ssl", False)
            )
        else:
            m365_imap = connect_m365_imap(m365_login_user, m365_access_token)
        
        gmail_imap = None
        try:
            if gmail_method != "oauth2_api":
                print(f"Connecting to Gmail IMAP ({gmail_method}) for {config['gmail_email']}...")
                if gmail_method == "oauth2_imap":
                    gmail_imap = connect_gmail_oauth_imap(
                        config["gmail_email"],
                        google_access_token,
                        config.get("gmail_imap_server", "imap.gmail.com"),
                        config.get("gmail_imap_port", 993)
                    )
                else:
                    gmail_imap = connect_gmail_imap(
                        config["gmail_email"],
                        config["gmail_password"],
                        config.get("gmail_imap_server", "imap.gmail.com"),
                        config.get("gmail_imap_port", 993)
                    )
            
            try:
                # Retrieve UIDVALIDITY
                res, status_data = m365_imap.status("INBOX", "(UIDVALIDITY)")
                if res != 'OK':
                    raise Exception(f"Failed to query INBOX status: {status_data}")
                    
                response = status_data[0].decode('utf-8', errors='ignore')
                match = re.search(r'UIDVALIDITY\s+(\d+)', response)
                if not match:
                    raise Exception(f"Could not parse UIDVALIDITY from: {response}")
                uid_validity = int(match.group(1))
                
                # Select INBOX
                m365_imap.select("INBOX")
                
                # Load stored sync state
                stored_validity, last_processed_uid = get_sync_state(db_path, config["m365_email"])
                
                # Retrieve new UIDs based on state
                if stored_validity == uid_validity and last_processed_uid is not None:
                    # Normal incremental sync
                    print(f"Incremental sync: checking for new messages with UID > {last_processed_uid}...")
                    res, search_data = m365_imap.uid('search', None, f'UID {last_processed_uid + 1}:*')
                    if res != 'OK':
                        raise Exception(f"Failed to search INBOX: {search_data}")
                    
                    m365_uids = []
                    if search_data and search_data[0]:
                        m365_uids = [int(uid) for uid in search_data[0].split()]
                    
                    # Filter out UIDs <= last_processed_uid in case the range search included the boundary
                    m365_uids = [uid for uid in m365_uids if uid > last_processed_uid]
                else:
                    # First run or UIDVALIDITY mismatch -> Perform Bootstrap
                    print("First run or UIDVALIDITY mismatch. Performing initial bootstrap...")
                    res, search_data = m365_imap.uid('search', None, 'ALL')
                    if res != 'OK':
                        raise Exception(f"Failed to search INBOX: {search_data}")
                    
                    m365_uids = []
                    if search_data and search_data[0]:
                        m365_uids = [int(uid) for uid in search_data[0].split()]
                    
                    max_uid = max(m365_uids) if m365_uids else 0
                    print(f"Bootstrapping: marking all {len(m365_uids)} existing emails (up to UID {max_uid}) as processed/ignored.")
                    update_sync_state(db_path, config["m365_email"], uid_validity, max_uid)
                    print("Initial bootstrap complete. Bridge will mirror future incoming emails.")
                    if hc_url:
                        ping_healthcheck(hc_url, "success")
                    return
                    
                print(f"Found {len(m365_uids)} new messages in Microsoft 365 INBOX.")
                
                copied_count = 0
                skipped_count = 0
                
                for uid in m365_uids:
                    # 1. Quick local DB check by UID
                    if is_email_processed(db_path, config["m365_email"], uid_validity, uid, None):
                        skipped_count += 1
                        continue
                        
                    # 2. Fetch headers to get Message-ID, FLAGS, INTERNALDATE
                    print(f"Fetching metadata for UID {uid}...")
                    res, fetch_data = m365_imap.uid('fetch', str(uid), '(INTERNALDATE FLAGS BODY[HEADER])')
                    if res != 'OK':
                        print(f"Error fetching metadata for UID {uid}: {fetch_data}")
                        continue
                    
                    internal_date = None
                    flags = []
                    message_id = None
                    header_text = b""
                    
                    for part in fetch_data:
                        if isinstance(part, tuple):
                            envelope = part[0].decode('utf-8', errors='ignore')
                            
                            # Extract flags
                            flags_match = re.search(r'FLAGS\s+\(([^)]*)\)', envelope)
                            if flags_match:
                                flags = flags_match.group(1).split()
                                
                            # Extract internaldate
                            date_match = re.search(r'INTERNALDATE\s+"([^"]+)"', envelope)
                            if date_match:
                                internal_date = date_match.group(1)
                                
                            header_text = part[1]
                            
                    # Parse message headers to find Message-ID
                    if header_text:
                        try:
                            msg = email.message_from_bytes(header_text)
                            message_id = msg.get("Message-ID")
                            if message_id:
                                message_id = message_id.strip()
                        except Exception as e:
                            print(f"Failed to parse headers for UID {uid}: {e}")
                            
                    # 3. Check by Message-ID
                    if message_id and is_email_processed(db_path, config["m365_email"], uid_validity, uid, message_id):
                        skipped_count += 1
                        continue
                        
                    # 4. Fetch full message body
                    print(f"Fetching full message body for UID {uid}...")
                    res, full_data = m365_imap.uid('fetch', str(uid), '(RFC822)')
                    if res != 'OK':
                        print(f"Error fetching full content for UID {uid}: {full_data}")
                        continue
                        
                    raw_bytes = None
                    for part in full_data:
                        if isinstance(part, tuple):
                            raw_bytes = part[1]
                            break
                            
                    if not raw_bytes:
                        print(f"Empty content returned for UID {uid}")
                        continue
                    
                    # Copy message depending on configuration
                    if gmail_method == "oauth2_api":
                        # REST API Insertion
                        print(f"Copying UID {uid} (Message-ID: {message_id}) via Gmail REST API...")
                        try:
                            msg_api_id = insert_gmail_message_api(google_access_token, raw_bytes, flags)
                            if msg_api_id:
                                print(f"Successfully copied UID {uid} (Gmail API ID: {msg_api_id})")
                                mark_email_processed(db_path, config["m365_email"], uid_validity, uid, message_id)
                                copied_count += 1
                            else:
                                print(f"Failed to copy UID {uid} via Gmail API (no ID returned)")
                        except Exception as e:
                            print(f"Failed to insert message via Gmail API: {e}")
                    else:
                        # IMAP Append (App Password or OAuth2 IMAP)
                        flags_str = '(' + ' '.join(flags) + ')' if flags else None
                        print(f"Copying UID {uid} (Message-ID: {message_id}) to Gmail INBOX...")
                        append_res, append_data = gmail_imap.append('INBOX', flags_str, internal_date, raw_bytes)
                        
                        if append_res == 'OK':
                            print(f"Successfully copied UID {uid} to Gmail IMAP")
                            mark_email_processed(db_path, config["m365_email"], uid_validity, uid, message_id)
                            copied_count += 1
                        else:
                            print(f"Failed to append UID {uid} to Gmail: {append_data}")
                        
                print(f"Sync complete. Copied: {copied_count}, Skipped: {skipped_count}")
                if m365_uids:
                    max_processed_uid = max(m365_uids)
                    update_sync_state(db_path, config["m365_email"], uid_validity, max_processed_uid)
                
            finally:
                if gmail_imap:
                    try:
                        gmail_imap.logout()
                    except Exception:
                        pass
        finally:
            try:
                m365_imap.logout()
            except Exception:
                pass
                
        if hc_url:
            ping_healthcheck(hc_url, "success")
            
    except Exception as e:
        if hc_url:
            import traceback
            err_msg = f"Sync failed: {e}\n\n{traceback.format_exc()}"
            ping_healthcheck(hc_url, "fail", err_msg)
        raise e

def main():
    parser = argparse.ArgumentParser(description="Microsoft 365 to Gmail Email Bridge Daemon")
    parser.add_argument("--config", default="config.json", help="Path to config.json file")
    parser.add_argument("--one-shot", action="store_true", help="Run once and exit immediately")
    parser.add_argument("--interval", type=int, help="Override sync interval in seconds")
    args = parser.parse_args()
    
    config_path = args.config
    if not os.path.exists(config_path):
        print(f"Error: Config file not found at '{config_path}'")
        print("Please run auth_setup.py first or create the config file.")
        sys.exit(1)
        
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
    except Exception as e:
        print(f"Error reading config file: {e}")
        sys.exit(1)
        
    m365_auth_method = config.get("m365_auth_method", "oauth2")
    gmail_method = config.get("gmail_auth_method", "oauth2_api")
    
    # Verify required keys dynamically based on auth method
    required_keys = ["m365_email", "gmail_email"]
    if m365_auth_method == "oauth2":
        required_keys.extend(["m365_client_id", "m365_refresh_token"])
    else:
        required_keys.append("m365_password")
        
    if gmail_method == "app_password":
        required_keys.append("gmail_password")
    else:
        required_keys.extend(["gmail_client_id", "gmail_client_secret", "gmail_refresh_token"])
        
    missing = [k for k in required_keys if not config.get(k)]
    if missing:
        print(f"Error: Missing required configuration keys: {', '.join(missing)}")
        print("Please run auth_setup.py first to configure.")
        sys.exit(1)
        
    sync_interval = args.interval or config.get("sync_interval_seconds", 300)
    
    if args.one-shot:
        print("Starting single-pass synchronization...")
        try:
            sync_emails(config, config_path)
            print("Single-pass synchronization completed successfully.")
        except Exception as e:
            print(f"Synchronization failed: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Starting background synchronization daemon (interval: {sync_interval} seconds)...")
        while True:
            try:
                current_time = time.strftime('%Y-%m-%d %H:%M:%S')
                print(f"\n--- Sync cycle started at {current_time} ---")
                sync_emails(config, config_path)
            except KeyboardInterrupt:
                print("\nDaemon stopped by user.")
                break
            except Exception as e:
                print(f"Error during sync cycle: {e}", file=sys.stderr)
                
            print(f"Sleeping for {sync_interval} seconds...")
            try:
                time.sleep(sync_interval)
            except KeyboardInterrupt:
                print("\nDaemon stopped by user.")
                break

if __name__ == "__main__":
    main()
