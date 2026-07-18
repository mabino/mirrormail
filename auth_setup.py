#!/usr/bin/env python3
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import re

# Using Microsoft Office Client ID as default since it is a first-party app and bypasses Conditional Access app restrictions
CLIENT_ID_M365 = "d3590ed6-52b3-4102-aeff-aad2292ab01c"
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

def get_m365_tenant(email_addr):
    """
    Extracts a tenant domain from the M365 email address.
    Defaults to 'organizations' for custom domains, and handles personal Microsoft accounts.
    """
    if not email_addr or "@" not in email_addr:
        return "organizations"
    domain = email_addr.split("@")[-1].lower()
    if domain in ("outlook.com", "hotmail.com", "live.com", "msn.com"):
        return "consumers"
    return domain

def run_m365_auth_code_flow(client_id, tenant):
    print(f"\n--- Initiating Microsoft 365 Authentication (Authorization Code Flow) ---")
    redirect_uri = "https://login.microsoftonline.com/common/oauth2/nativeclient"
    auth_params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(SCOPES_M365),
        "state": "12345"
    }
    
    auth_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?" + urllib.parse.urlencode(auth_params)
    
    print("\n==========================================================================")
    print("INSTRUCTIONS:")
    print("1. Open the following URL in your web browser:")
    print(f"\n{auth_url}")
    print("\n2. Log in with your Microsoft 365 account. Because this login occurs in your")
    print("   native browser, any device compliance and MFA checks will succeed.")
    print("3. After a successful login, the browser will redirect to a blank page starting with:")
    print(f"   {redirect_uri}?code=...")
    print("4. Copy the entire URL from the address bar and paste it below.")
    print("==========================================================================\n")
    
    redirected_url = input("Paste the redirected URL here: ").strip()
    if not redirected_url:
        print("Error: No input received.")
        return None
        
    code = None
    match = re.search(r"[?&]code=([^&]+)", redirected_url)
    if match:
        code = match.group(1)
    else:
        code = redirected_url
        
    # Exchange code for tokens
    token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES_M365)
    }
    data = urllib.parse.urlencode(payload).encode('utf-8')
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(token_url, data=data, headers=headers, method='POST')
    
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
        refresh_token = res_data.get("refresh_token")
        if refresh_token:
            print("Microsoft authentication successful!")
            return refresh_token
        else:
            print("Error: Refresh token not found in response.")
            return None
    except urllib.error.HTTPError as e:
        print(f"Error exchanging authorization code: HTTP Error {e.code}: {e.reason}")
        try:
            print("Details:", e.read().decode('utf-8'))
        except Exception:
            pass
        return None
    except Exception as e:
        print(f"Error exchanging authorization code: {e}")
        return None

def run_m365_device_flow(client_id, tenant):
    print(f"\n--- Initiating Microsoft 365 Authentication (Device Code Flow) ---")
    device_code_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode"
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
    except urllib.error.HTTPError as e:
        print(f"Error requesting device code from Microsoft: HTTP Error {e.code}: {e.reason}")
        try:
            body = e.read().decode('utf-8')
            err_res = json.loads(body)
            error_code = err_res.get("error")
            error_desc = err_res.get("error_description", "")
            print(f"Details: {error_desc}")
            
            # Check for AADSTS700016 (app not found in directory / needs consent)
            if error_code == "unauthorized_client" or "AADSTS700016" in error_desc:
                consent_url = (
                    f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
                    f"?client_id={client_id}"
                    f"&response_type=code"
                    f"&redirect_uri=https://login.microsoftonline.com/common/oauth2/nativeclient"
                    f"&response_mode=query"
                    f"&scope=https://outlook.office.com/IMAP.AccessAsUser.All%20offline_access"
                    f"&state=12345"
                )
                print("\n==========================================================================")
                print("ACTION REQUIRED: FIRST-TIME TENANT CONSENT")
                print("==========================================================================")
                print("The application is not yet registered or consented to in your")
                print(f"Microsoft 365 directory (Tenant: '{tenant}').")
                print("\nTo fix this, copy and paste the following URL into your web browser,")
                print("sign in with your M365 account, and grant the requested permissions:")
                print(f"\n{consent_url}")
                print("\nNote: Depending on your organization's security policy, you may need an")
                print("administrator to approve this application consent request.")
                print("==========================================================================\n")
        except Exception:
            pass
        return None
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

    token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
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

def configure_bridge(config_path):
    config = load_config(config_path)
    
    print("\n--- Configure / Update Email Bridge ---")
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

    # Choose Microsoft 365 Connection / Authentication Method
    current_m365_method = config.get("m365_auth_method", "oauth2")
    print("\nMicrosoft 365 Connection & Authentication Methods:")
    print(" [1] Direct OAuth2 (Recommended, direct connection using modern auth)")
    print(" [2] DavMail Sidecar Proxy (Connect via local DavMail IMAP gateway)")
    m365_choice = input(f"Choose connection method [default: {'1' if current_m365_method == 'oauth2' else '2'}]: ").strip()
    
    m365_auth_method = current_m365_method
    if m365_choice == "1":
        m365_auth_method = "oauth2"
    elif m365_choice == "2":
        m365_auth_method = "password"

    config["m365_auth_method"] = m365_auth_method

    if m365_auth_method == "oauth2":
        config["m365_client_id"] = config.get("m365_client_id") or CLIENT_ID_M365
        if config["m365_client_id"] in ("YOUR_CLIENT_ID", "9a5bf30c-26d2-43fb-ab89-40c2136d88b4", "9e5f94bc-e8a4-4e73-b8be-63364c29d753", ""):
            config["m365_client_id"] = CLIENT_ID_M365

        default_tenant = get_m365_tenant(m365_email)
        current_tenant = config.get("m365_tenant", default_tenant)
        m365_tenant = input(f"Enter Microsoft 365 Tenant ID or domain [{current_tenant}]: ").strip() or current_tenant
        config["m365_tenant"] = m365_tenant

        print("\nMicrosoft 365 OAuth2 Flow Options:")
        print(" [1] Authorization Code Flow (Recommended - works with device compliance policies)")
        print(" [2] Device Code Flow (Alternative - may be blocked by some organizations)")
        flow_choice = input("Choose M365 authentication flow [default: 1]: ").strip()
        
        if flow_choice == "2":
            m365_refresh = run_m365_device_flow(config["m365_client_id"], config["m365_tenant"])
        else:
            m365_refresh = run_m365_auth_code_flow(config["m365_client_id"], config["m365_tenant"])
            
        if not m365_refresh:
            print("Error: Failed to obtain Microsoft 365 refresh token.")
            sys.exit(1)
        config["m365_refresh_token"] = m365_refresh
        config.pop("m365_password", None)
        config.pop("m365_imap_server", None)
        config.pop("m365_imap_port", None)
        config.pop("m365_imap_use_ssl", None)
    else:
        # DavMail Proxy Connection Settings
        current_server = config.get("m365_imap_server", "localhost")
        m365_imap_server = input(f"Enter M365/DavMail IMAP server [{current_server}]: ").strip() or current_server
        
        current_port = str(config.get("m365_imap_port", 1143))
        m365_imap_port_input = input(f"Enter M365/DavMail IMAP port [{current_port}]: ").strip()
        m365_imap_port = int(m365_imap_port_input) if m365_imap_port_input else int(current_port)
        
        current_ssl = config.get("m365_imap_use_ssl", False)
        current_ssl_str = "y" if current_ssl else "n"
        m365_imap_use_ssl_input = input(f"Use SSL/TLS for M365/DavMail connection? (y/n) [{current_ssl_str}]: ").strip().lower()
        if m365_imap_use_ssl_input:
            m365_imap_use_ssl = m365_imap_use_ssl_input == "y"
        else:
            m365_imap_use_ssl = current_ssl

        current_pwd = config.get("m365_password", "")
        if not current_pwd:
            m365_password = input("Enter M365/DavMail Password: ").strip()
        else:
            m365_password = input("Enter M365/DavMail Password [Press Enter to keep existing]: ").strip() or current_pwd
            
        if not m365_password:
            print("Error: Password is required for this connection method.")
            sys.exit(1)
            
        config["m365_imap_server"] = m365_imap_server
        config["m365_imap_port"] = m365_imap_port
        config["m365_imap_use_ssl"] = m365_imap_use_ssl
        config["m365_password"] = m365_password
        
        config.pop("m365_client_id", None)
        config.pop("m365_tenant", None)
        config.pop("m365_refresh_token", None)

    config["gmail_imap_server"] = config.get("gmail_imap_server") or "imap.gmail.com"
    config["gmail_imap_port"] = config.get("gmail_imap_port") or 993
    config["sync_interval_seconds"] = config.get("sync_interval_seconds") or 300
    config["database_path"] = config.get("database_path") or "email_bridge.db"

    save_config(config, config_path)
    print("\nInitialization/Update Complete! You are now ready to run bridge_daemon.py.")

def teardown_bridge(config_path):
    print("\n--- Tear Down / Reset Email Bridge ---")
    config = load_config(config_path)
    
    confirm = input("Are you sure you want to delete all local configurations and tracking databases? [y/N]: ").strip().lower()
    if confirm != 'y':
        print("Teardown cancelled.")
        return

    db_path = config.get("database_path", "email_bridge.db")

    deleted_files = []
    for ext in ["", "-journal", "-wal", "-shm"]:
        target = f"{db_path}{ext}" if ext else db_path
        if os.path.exists(target):
            try:
                os.remove(target)
                deleted_files.append(target)
            except Exception as e:
                print(f"Warning: Failed to delete {target}: {e}")

    if os.path.exists(config_path):
        try:
            os.remove(config_path)
            deleted_files.append(config_path)
        except Exception as e:
            print(f"Warning: Failed to delete config file {config_path}: {e}")

    if deleted_files:
        print("\nSuccessfully removed:")
        for f in deleted_files:
            print(f" - {f}")
    else:
        print("\nNo configuration or database files were found to delete.")
        
    print("\nTeardown complete. Local environment has been reset.")

def main():
    config_path = "config.json"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    print("==========================================")
    print("Microsoft 365 & Gmail Bridge Admin Utility")
    print("==========================================")
    print("Choose an action:")
    print(" [1] Setup / Reconfigure Email Bridge (Update configuration and renew tokens)")
    print(" [2] Tear Down / Reset (Delete local config and SQLite tracking database)")
    print(" [3] Exit")
    
    choice = input("Enter choice [1-3]: ").strip()
    if choice == "1":
        configure_bridge(config_path)
    elif choice == "2":
        teardown_bridge(config_path)
    elif choice == "3" or not choice:
        print("Exiting.")
        sys.exit(0)
    else:
        print("Invalid option. Exiting.")
        sys.exit(1)

if __name__ == "__main__":
    main()
