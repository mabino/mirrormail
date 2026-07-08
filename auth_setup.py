#!/usr/bin/env python3
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error

CLIENT_ID_M365 = "9a5bf30c-26d2-43fb-ab89-40c2136d88b4"
SCOPES_M365 = ["https://outlook.office.com/IMAP.AccessAsUser.All", "offline_access"]

def load_config(config_path):
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load existing config: {e}")
    return {}

def save_config(config, config_path):
    try:
        db_dir = os.path.dirname(os.path.abspath(config_path))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
            
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=4)
        print(f"\nConfiguration successfully saved to {config_path}")
    except Exception as e:
        print(f"Error saving configuration: {e}")

def run_m365_device_flow(client_id):
    print("\n--- Initiating Microsoft 365 Authentication ---")
    device_code_url = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    payload = {
        "client_id": client_id,
        "scope": " ".join(SCOPES_M365)
    }
    
    data = urllib.parse.urlencode(payload).encode('utf-8')
    req = urllib.request.Request(device_code_url, data=data, headers=headers, method='POST')
    
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"Error requesting device code from Microsoft: {e}")
        return None
        
    user_code = res_data.get("user_code")
    device_code = res_data.get("device_code")
    verification_uri = res_data.get("verification_uri")
    interval = res_data.get("interval", 5)
    expires_in = res_data.get("expires_in", 900)
    
    print("\n----------------------------------------------------")
    print(res_data.get("message", f"To sign in, open {verification_uri} and enter the code: {user_code}"))
    print("----------------------------------------------------\n")
    print("Waiting for Microsoft authentication (polling endpoints)...")

    token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    token_payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": client_id,
        "device_code": device_code
    }
    token_data = urllib.parse.urlencode(token_payload).encode('utf-8')
    
    start_time = time.time()
    while True:
        if time.time() - start_time > expires_in:
            print("Microsoft authentication request expired.")
            return None
            
        token_req = urllib.request.Request(token_url, data=token_data, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(token_req) as response:
                token_res = json.loads(response.read().decode('utf-8'))
            refresh_token = token_res.get("refresh_token")
            if refresh_token:
                print("Microsoft authentication successful!")
                return refresh_token
            else:
                print("Error: Refresh token not found in Microsoft response.")
                return None
                
        except urllib.error.HTTPError as e:
            try:
                err_res = json.loads(e.read().decode('utf-8'))
                error = err_res.get("error")
                if error == "authorization_pending":
                    time.sleep(interval)
                    continue
                else:
                    print(f"\nMicrosoft Authentication error: {error} - {err_res.get('error_description')}")
                    return None
            except Exception:
                print(f"\nHTTP Error {e.code}: {e.reason}")
                return None
        except Exception as e:
            print(f"\nAn error occurred while polling: {e}")
            return None

def run_google_device_flow(client_id, client_secret):
    print("\n--- Initiating Google OAuth2 Authentication ---")
    # Google Device Code URL
    device_code_url = "https://oauth2.googleapis.com/device/code"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    payload = {
        "client_id": client_id,
        "scope": "https://mail.google.com/"
    }
    
    data = urllib.parse.urlencode(payload).encode('utf-8')
    req = urllib.request.Request(device_code_url, data=data, headers=headers, method='POST')
    
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"Error requesting device code from Google: {e}")
        return None
        
    user_code = res_data.get("user_code")
    device_code = res_data.get("device_code")
    verification_url = res_data.get("verification_url", "https://google.com/device")
    interval = res_data.get("interval", 5)
    expires_in = res_data.get("expires_in", 1800)
    
    print("\n----------------------------------------------------")
    print(f"To sign in, open Google device login:\n  {verification_url}\n\nAnd enter the code:\n  {user_code}")
    print("----------------------------------------------------\n")
    print("Waiting for Google authentication (polling endpoints)...")

    token_url = "https://oauth2.googleapis.com/token"
    token_payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
    }
    token_data = urllib.parse.urlencode(token_payload).encode('utf-8')
    
    start_time = time.time()
    while True:
        if time.time() - start_time > expires_in:
            print("Google authentication request expired.")
            return None
            
        token_req = urllib.request.Request(token_url, data=token_data, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(token_req) as response:
                token_res = json.loads(response.read().decode('utf-8'))
            refresh_token = token_res.get("refresh_token")
            if refresh_token:
                print("Google authentication successful!")
                return refresh_token
            else:
                print("Warning: Google login succeeded, but no refresh token was returned.")
                print("If this occurs repeatedly, reset your app's permissions in your Google Account.")
                return token_res.get("access_token")
        except urllib.error.HTTPError as e:
            try:
                err_res = json.loads(e.read().decode('utf-8'))
                error = err_res.get("error")
                if error == "authorization_pending":
                    time.sleep(interval)
                    continue
                else:
                    print(f"\nGoogle Authentication error: {error} - {err_res.get('error_description')}")
                    return None
            except Exception:
                print(f"\nHTTP Error {e.code}: {e.reason}")
                return None
        except Exception as e:
            print(f"\nAn error occurred while polling Google: {e}")
            return None

def main():
    config_path = "config.json"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    config = load_config(config_path)

    print("Microsoft 365 & Gmail Bridge Setup Utility")
    print("==========================================")
    
    current_m365 = config.get("m365_email", "")
    if not current_m365 or "YOUR_M365_EMAIL" in current_m365:
        m365_email = input("Enter Microsoft 365 email: ").strip()
    else:
        m365_email = input(f"Enter Microsoft 365 email [{current_m365}]: ").strip() or current_m365

    current_gmail = config.get("gmail_email", "")
    if not current_gmail or "YOUR_GMAIL_EMAIL" in current_gmail:
        gmail_email = input("Enter Gmail email: ").strip()
    else:
        gmail_email = input(f"Enter Gmail email [{current_gmail}]: ").strip() or current_gmail

    if not m365_email or not gmail_email:
        print("Error: Microsoft 365 and Gmail emails are required.")
        sys.exit(1)

    config["m365_client_id"] = config.get("m365_client_id") or CLIENT_ID_M365
    if config["m365_client_id"] == "YOUR_CLIENT_ID" or not config["m365_client_id"]:
        config["m365_client_id"] = CLIENT_ID_M365
    config["m365_email"] = m365_email
    config["gmail_email"] = gmail_email

    current_method = config.get("gmail_auth_method", "app_password")
    print("\nGmail Authentication Methods:")
    print(" [1] App Password (Default, simple, recommended for personal accounts)")
    print(" [2] Google OAuth2 REST API (Requires GCP project, client ID/secret)")
    print(" [3] Google OAuth2 IMAP XOAUTH2 (Requires GCP project, client ID/secret)")
    
    choice = input(f"Choose authentication method [default: {current_method}]: ").strip()
    
    auth_method = current_method
    if choice == "1":
        auth_method = "app_password"
    elif choice == "2":
        auth_method = "oauth2_api"
    elif choice == "3":
        auth_method = "oauth2_imap"

    config["gmail_auth_method"] = auth_method

    if auth_method == "app_password":
        current_pwd = config.get("gmail_password", "")
        if not current_pwd or "YOUR_GMAIL_APP_PASSWORD" in current_pwd:
            gmail_pwd = input("Enter Gmail App Password: ").strip()
        else:
            gmail_pwd = input("Enter Gmail App Password [Press Enter to keep existing]: ").strip() or current_pwd
            
        if not gmail_pwd:
            print("Error: Gmail App Password is required for this method.")
            sys.exit(1)
        config["gmail_password"] = gmail_pwd
        config.pop("gmail_client_id", None)
        config.pop("gmail_client_secret", None)
        config.pop("gmail_refresh_token", None)
    else:
        current_g_id = config.get("gmail_client_id", "")
        current_g_secret = config.get("gmail_client_secret", "")
        
        print("\nTo set up Google OAuth2, you need a Google Cloud Project with a 'Desktop App' OAuth client ID.")
        gmail_client_id = input(f"Enter Google Client ID [{current_g_id}]: ").strip() or current_g_id
        gmail_client_secret = input(f"Enter Google Client Secret [{current_g_secret}]: ").strip() or current_g_secret
        
        if not gmail_client_id or not gmail_client_secret:
            print("Error: Google OAuth Client ID and Client Secret are required.")
            sys.exit(1)
            
        config["gmail_client_id"] = gmail_client_id
        config["gmail_client_secret"] = gmail_client_secret
        
        gmail_refresh = run_google_device_flow(gmail_client_id, gmail_client_secret)
        if not gmail_refresh:
            print("Error: Failed to obtain Google OAuth2 refresh token.")
            sys.exit(1)
        config["gmail_refresh_token"] = gmail_refresh
        config.pop("gmail_password", None)

    m365_refresh = run_m365_device_flow(config["m365_client_id"])
    if not m365_refresh:
        print("Error: Failed to obtain Microsoft 365 refresh token.")
        sys.exit(1)
    config["m365_refresh_token"] = m365_refresh

    save_config(config, config_path)
    print("\nInitialization Complete! You are now ready to run bridge_daemon.py.")

if __name__ == "__main__":
    main()
