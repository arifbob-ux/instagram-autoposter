"""Instagram auto-poster.

Each run ("tick") decides whether a post is due, and if so takes the next clip
from the Google Drive queue folder, publishes it as a Reel, and moves it to the
posted folder. Posts are spread evenly over your active hours.

    python poster.py            one tick (what GitHub Actions runs)
    python poster.py --check    verify Instagram + Drive access, show the plan
    python poster.py --dry-run  show what would happen, post nothing
    python poster.py --loop     run forever (for a VPS / always-on server)
"""
import argparse
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from drive import Drive
from instagram import IGError, Instagram


def env(name, default=None, required=False):
    v = os.environ.get(name, "").strip()
    if not v:
        if required:
            sys.exit(f"Missing required setting: {name}")
        return default
    return v


class Config:
    def __init__(self, need_instagram=True):
        self.ig_token = env("IG_ACCESS_TOKEN", required=need_instagram)
        self.ig_user_id = env("IG_USER_ID")
        self.ig_host = env("IG_GRAPH_HOST", "graph.instagram.com")
        self.ig_version = env("IG_API_VERSION", "v23.0")
        self.sa_json = env("GOOGLE_SERVICE_ACCOUNT_JSON", required=True)
        self.queue = env("DRIVE_QUEUE_FOLDER_ID", required=True)
        self.posted = env("DRIVE_POSTED_FOLDER_ID", required=True)
        self.failed = env("DRIVE_FAILED_FOLDER_ID")
        self.posts_per_day = max(1, min(100, int(env("POSTS_PER_DAY", "25"))))
        self.tz = ZoneInfo(env("TIMEZONE", "America/New_York"))
        self.start_hour = int(env("ACTIVE_START_HOUR", "8"))
        self.end_hour = int(env("ACTIVE_END_HOUR", "24")) % 24
        self.default_caption = env("DEFAULT_CAPTION", "")
        self.share_to_feed = env("SHARE_TO_FEED", "true").lower() == "true"
        self.min_gap_min = float(env("MIN_GAP_MINUTES", "5"))
        self.loop_seconds = int(env("LOOP_SECONDS", "300"))


def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def active_window(cfg, now_local):
    """Return (window_start, window_end) containing now, or None if outside active hours."""
    s, e = cfg.start_hour, cfg.end_hour
    day = now_local.replace(minute=0, second=0, microsecond=0)
    start_today = day.replace(hour=s)
    if s == e:  # 24h
        start = start_today if now_local >= start_today else start_today - timedelta(days=1)
        return start, start + timedelta(days=1)
    length = timedelta(hours=(e - s) % 24)
    for start in (start_today, start_today - timedelta(days=1)):
        if start <= now_local < start + length:
            return start, start + length
    return None


def decide(cfg, ig, now=None):
    """Returns (should_post: bool, reason: str)."""
    now = now or datetime.now(timezone.utc)
    now_local = now.astimezone(cfg.tz)
    win = active_window(cfg, now_local)
    if not win:
        return False, f"outside active hours ({cfg.start_hour}:00-{cfg.end_hour}:00 {cfg.tz.key})"
    start, end = win

    used, quota = ig.publishing_usage()
    if used >= quota:
        return False, f"Instagram API limit reached ({used}/{quota} in last 24h)"

    times = ig.recent_post_times()
    posted_in_window = sum(1 for t in times if t >= start)
    if posted_in_window >= cfg.posts_per_day:
        return False, f"daily target done ({posted_in_window}/{cfg.posts_per_day})"

    gap = (end - start) / cfg.posts_per_day
    slots_due = min(cfg.posts_per_day, int((now_local - start) / gap) + 1)
    if posted_in_window >= slots_due:
        nxt = start + gap * slots_due
        return False, f"on schedule ({posted_in_window}/{cfg.posts_per_day} today), next slot {nxt:%H:%M}"

    if times and (now - max(times)).total_seconds() < cfg.min_gap_min * 60:
        return False, f"last post was under {cfg.min_gap_min:g} min ago"

    return True, f"slot due ({posted_in_window}/{slots_due} due so far, target {cfg.posts_per_day})"


def tick(cfg, ig, drive, dry_run=False):
    should, reason = decide(cfg, ig)
    log(("POST: " if should else "skip: ") + reason)
    if not should:
        return 0

    queue = drive.queue(cfg.queue)
    if not queue:
        log("queue folder is empty - add more clips!")
        return 0
    video, cap_file = queue[0]
    log(f"next clip: {video['name']}  ({len(queue)} in queue)")
    if len(queue) <= cfg.posts_per_day:
        log(f"warning: only {len(queue)} clips left (~{len(queue)/cfg.posts_per_day:.1f} days)")

    caption = drive.read_text(cap_file["id"]) if cap_file else cfg.default_caption
    if dry_run:
        log(f"[dry-run] would post with caption: {caption[:120]!r}")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, video["name"])
        drive.download(video["id"], path)
        try:
            media_id = ig.post_reel(path, caption, cfg.share_to_feed, log=log)
        except IGError as e:
            log(f"FAILED: {e}")
            if cfg.failed:
                drive.move(video["id"], cfg.queue, cfg.failed)
                if cap_file:
                    drive.move(cap_file["id"], cfg.queue, cfg.failed)
                log("moved clip to failed folder so the queue keeps moving")
            return 1

    drive.move(video["id"], cfg.queue, cfg.posted)
    if cap_file:
        drive.move(cap_file["id"], cfg.queue, cfg.posted)
    log(f"published {video['name']} -> media {media_id}")
    return 0


def check(cfg, ig, drive):
    me = ig.whoami()
    log(f"Instagram OK: @{me.get('username')} (id {ig.user_id})")
    used, quota = ig.publishing_usage()
    log(f"API publishing quota: {used}/{quota} used in last 24h")
    log(f"Drive service account: {drive.email}")
    for label, fid in (("queue", cfg.queue), ("posted", cfg.posted), ("failed", cfg.failed)):
        if fid:
            log(f"Drive {label} folder OK: '{drive.folder_name(fid)}'")
    q = drive.queue(cfg.queue)
    log(f"{len(q)} clips in queue, {sum(1 for _, c in q if c)} with caption files")
    hours = (cfg.end_hour - cfg.start_hour) % 24 or 24
    log(f"plan: {cfg.posts_per_day}/day between {cfg.start_hour}:00-{cfg.end_hour}:00 "
        f"{cfg.tz.key} = one every {hours*60/cfg.posts_per_day:.0f} min")
    log("decision now: " + " / ".join(map(str, decide(cfg, ig))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--check-drive", action="store_true", help="test Drive access only")
    a = ap.parse_args()

    if a.check_drive:
        cfg = Config(need_instagram=False)
        drive = Drive(cfg.sa_json)
        log(f"Drive service account: {drive.email}")
        for label, fid in (("queue", cfg.queue), ("posted", cfg.posted), ("failed", cfg.failed)):
            if fid:
                log(f"Drive {label} folder OK: '{drive.folder_name(fid)}'")
        q = drive.queue(cfg.queue)
        log(f"{len(q)} clips in queue, {sum(1 for _, c in q if c)} with caption files")
        return 0

    cfg = Config()
    ig = Instagram(cfg.ig_token, cfg.ig_user_id, cfg.ig_host, cfg.ig_version)
    drive = Drive(cfg.sa_json)

    if a.check:
        return check(cfg, ig, drive)
    if not a.loop:
        return tick(cfg, ig, drive, a.dry_run)
    while True:
        try:
            tick(cfg, ig, drive, a.dry_run)
        except Exception as e:  # keep the server running through transient errors
            log(f"error: {e}")
        time.sleep(cfg.loop_seconds)


if __name__ == "__main__":
    sys.exit(main() or 0)
