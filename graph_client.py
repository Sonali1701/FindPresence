"""Thin wrapper around Microsoft Graph for the calls we need."""
import time
import requests
import msal

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = ["https://graph.microsoft.com/.default"]

ACTIVE_STATES = {"Available", "Busy", "DoNotDisturb",
                 "InACall", "InAMeeting", "Presenting"}


class GraphClient:
    def __init__(self, tenant_id, client_id, client_secret):
        self._app = msal.ConfidentialClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=client_secret,
        )
        self._token = None
        self._token_expires = 0

    def _headers(self):
        if not self._token or time.time() > self._token_expires - 60:
            result = self._app.acquire_token_for_client(scopes=SCOPE)
            if "access_token" not in result:
                raise RuntimeError(f"Auth failed: {result}")
            self._token = result["access_token"]
            self._token_expires = time.time() + result.get("expires_in", 3600)
        return {"Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json"}

    def list_users(self):
        """Return all enabled member users with id + mail/userPrincipalName."""
        users = []
        url = (f"{GRAPH}/users?$select=id,displayName,mail,userPrincipalName,"
               "accountEnabled,userType&$top=999")
        while url:
            r = requests.get(url, headers=self._headers(), timeout=30)
            r.raise_for_status()
            data = r.json()
            for u in data.get("value", []):
                if not u.get("accountEnabled", True):
                    continue
                if u.get("userType") == "Guest":
                    continue
                users.append(u)
            url = data.get("@odata.nextLink")
        return users

    def get_presences(self, user_ids):
        """Batch-fetch presence for up to 650 user ids."""
        out = {}
        for i in range(0, len(user_ids), 650):
            chunk = user_ids[i:i + 650]
            r = requests.post(
                f"{GRAPH}/communications/getPresencesByUserId",
                headers=self._headers(),
                json={"ids": chunk},
                timeout=30,
            )
            r.raise_for_status()
            for p in r.json().get("value", []):
                out[p["id"]] = p
        return out

    def send_mail(self, sender, to, subject, body_html):
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": False,
        }
        r = requests.post(
            f"{GRAPH}/users/{sender}/sendMail",
            headers=self._headers(),
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
