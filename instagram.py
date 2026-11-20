"""Minimal client for the official Instagram Graph API (Reels publishing)."""
import os
import time
from datetime import datetime

import requests


class IGError(Exception):
    pass


class Instagram:
    def __init__(self, token, user_id=None, host="graph.instagram.com", version="v23.0"):
        self.token = token
        self.version = version
        self.base = f"https://{host}/{version}"
        self.user_id = user_id or self._resolve_user_id()

    # ---------- low level ----------
    def _req(self, method, path, params=None, **kw):
        params = dict(params or {})
        params["access_token"] = self.token
        r = requests.request(method, f"{self.base}/{path}", params=params, timeout=60, **kw)
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text}
        if r.status_code >= 400 or (isinstance(data, dict) and "error" in data):
            raise IGError(f"{method} {path} -> HTTP {r.status_code}: {data}")
        return data

    def _resolve_user_id(self):
        me = self._req("GET", "me", params={"fields": "user_id,username"})
        self.username = me.get("username")
        return me.get("user_id") or me["id"]

    # ---------- info ----------
    def whoami(self):
        return self._req("GET", str(self.user_id), params={"fields": "id,username"})

    def publishing_usage(self):
        """Returns (used_in_last_24h, allowed_per_24h)."""
        d = self._req("GET", f"{self.user_id}/content_publishing_limit",
                      params={"fields": "config,quota_usage"})
        row = (d.get("data") or [{}])[0]
        used = int(row.get("quota_usage", 0))
        total = int((row.get("config") or {}).get("quota_total", 100))
        return used, total

    def recent_post_times(self, limit=100):
        d = self._req("GET", f"{self.user_id}/media",
                      params={"fields": "timestamp", "limit": limit})
        out = []
        for m in d.get("data", []):
            out.append(datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z"))
        return out

    # ---------- publishing ----------
    def post_reel(self, video_path, caption="", share_to_feed=True, log=print):
        container = self._req("POST", f"{self.user_id}/media", params={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "share_to_feed": "true" if share_to_feed else "false",
        })
        cid = container["id"]
        uri = container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{self.version}/{cid}"
        log(f"  container {cid} created, uploading {os.path.getsize(video_path)/1e6:.1f} MB")

        size = os.path.getsize(video_path)
        with open(video_path, "rb") as f:
            r = requests.post(uri, data=f, timeout=900, headers={
                "Authorization": f"OAuth {self.token}",
                "offset": "0",
                "file_size": str(size),
            })
        if r.status_code >= 400:
            raise IGError(f"upload failed HTTP {r.status_code}: {r.text}")

        self._wait_ready(cid, log)
        res = self._req("POST", f"{self.user_id}/media_publish", params={"creation_id": cid})
        return res["id"]

    def _wait_ready(self, cid, log, timeout=900):
        start = time.time()
        while time.time() - start < timeout:
            d = self._req("GET", cid, params={"fields": "status_code,status"})
            code = d.get("status_code")
            if code == "FINISHED":
                return
            if code in ("ERROR", "EXPIRED"):
                raise IGError(f"processing failed: {d}")
            log(f"  processing... ({code})")
            time.sleep(10)
        raise IGError("timed out waiting for Instagram to process the video")


def refresh_long_lived_token(token, host="graph.instagram.com"):
    """Extends an Instagram-Login long-lived token by another 60 days."""
    r = requests.get(f"https://{host}/refresh_access_token",
                     params={"grant_type": "ig_refresh_token", "access_token": token}, timeout=60)
    data = r.json()
    if "access_token" not in data:
        raise IGError(f"token refresh failed: {data}")
    return data["access_token"], data.get("expires_in")
