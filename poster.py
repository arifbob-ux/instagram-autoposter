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
        self.schedule_mode = env("SCHEDULE_MODE", "spread").lower()   # spread | batch
        self.batch_times = [t.strip() for t in env("BATCH_TIMES", "09:00,18:00").split(",")]
        self.batch_window_min = int(env("BATCH_WINDOW_MINUTES", "120"))
        self.batch_gap = int(env("BATCH_GAP_SECONDS", "10"))
        self.require_caption = env("REQUIRE_CAPTION", "false").lower() == "true"
        self.cover_url = env("COVER_URL")  # public JPEG used as the cover of every Reel
        self.delete_after_post =env("DELETE_AFTER_POST", "false").lower() == "true"


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


def ready_queue(cfg, drive):
    """Queue of (video, caption_file) pairs. Pairing is by identical file name
    (abc.mp4 <-> abc.txt). With REQUIRE_CAPTION, clips without a caption are held back."""
    queue = drive.queue(cfg.queue)
    names = {os.path.splitext(f["name"])[0] for f in drive.list_files(cfg.queue)
             if f["name"].lower().endswith(".txt")}
    videos = {os.path.splitext(v["name"])[0] for v, _ in queue}
    for orphan in sorted(names - videos):
        log(f"warning: caption '{orphan}.txt' has no matching video - check the file names")
    if cfg.require_caption:
        for v, c in queue:
            if not c:
                log(f"HOLD: {v['name']} has no matching .txt caption - not posted")
        queue = [(v, c) for v, c in queue if c]
    return queue


def post_one(cfg, ig, drive, video, cap_file):
    """Publishes one clip with its own caption. Returns the Reel link, or None on failure."""
    caption = drive.read_text(cap_file["id"]) if cap_file else cfg.default_caption
    log(f"posting {video['name']}  |  caption from {cap_file['name'] if cap_file else 'DEFAULT'}: "
        f"{caption[:70]!r}")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, video["name"])
        drive.download(video["id"], path)
        try:
            media_id = ig.post_reel(path, caption, cfg.share_to_feed, log=log, cover_url=cfg.cover_url)
        except IGError as e:
            log(f"FAILED: {e}")
            if cfg.failed:
                drive.move(video["id"], cfg.queue, cfg.failed)
                if cap_file:
                    drive.move(cap_file["id"], cfg.queue, cfg.failed)
                log("moved clip to failed folder so the queue keeps moving")
            return None

    try:
        link = ig.permalink(media_id)
    except IGError:
        link = f"media {media_id}"
    log(f"published {video['name']} -> {link}")
    for f in [video] + ([cap_file] if cap_file else []):
        if cfg.delete_after_post:
            try:
                drive.trash(f["id"])
                continue
            except Exception as e:
                log(f"could not delete {f['name']} ({e}); moving to posted folder instead")
        drive.move(f["id"], cfg.queue, cfg.posted)
    return link


def run_batch(cfg, ig, drive, count, label, dry_run=False):
    queue = ready_queue(cfg, drive)
    take = queue[:count]
    log(f"{label}: {len(take)} of {len(queue)} ready clips")
    if dry_run:
        for v, c in take:
            log(f"[dry-run] {v['name']}  <->  {c['name'] if c else 'DEFAULT caption'}")
        return 0
    results, failed = [], 0
    for i, (v, c) in enumerate(take, 1):
        used, quota = ig.publishing_usage()
        if used >= quota:
            log(f"Instagram API limit reached ({used}/{quota}) - stopping, the rest stay queued")
            break
        log(f"--- {label} {i}/{len(take)} ---")
        link = post_one(cfg, ig, drive, v, c)
        if link:
            results.append((v["name"], link))
        else:
            failed += 1
        if i < len(take):
            time.sleep(cfg.batch_gap)
    log("=" * 60)
    log(f"{label} DONE: {len(results)} posted, {failed} failed. Links for submission:")
    for name, link in results:
        print(f"  {link}   ({name})", flush=True)
    return 1 if failed else 0


def batch_due(cfg, ig, now=None):
    """Returns (label, count_fraction_divisor) if a batch should run now, else (None, reason)."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(cfg.tz)
    slots = []
    for i, t in enumerate(cfg.batch_times):
        h, m = map(int, t.split(":"))
        slots.append((i, local.replace(hour=h, minute=m, second=0, microsecond=0)))
    current = [(i, s) for i, s in slots if s <= local]
    if not current:
        return None, f"before today's first batch ({cfg.batch_times[0]} {cfg.tz.key})"
    i, start = current[-1]
    if (local - start).total_seconds() > cfg.batch_window_min * 60:
        nxt = next((t for j, t in enumerate(cfg.batch_times) if j > i), cfg.batch_times[0] + " tomorrow")
        return None, f"batch {cfg.batch_times[i]} window closed; next batch {nxt}"
    if any(t >= start for t in ig.recent_post_times()):
        return None, f"batch {cfg.batch_times[i]} already posted today"
    remaining_batches = len(cfg.batch_times) - i
    return (f"batch {cfg.batch_times[i]}", remaining_batches), "due"


def tick(cfg, ig, drive, dry_run=False):
    if cfg.schedule_mode == "batch":
        due, reason = batch_due(cfg, ig)
        if not due:
            log(f"skip: {reason}")
            return 0
        label, remaining_batches = due
        n = len(ready_queue(cfg, drive))
        count = -(-n // remaining_batches)  # this batch's share, rounded up
        if n == 0:
            log(f"{label}: queue is empty - add clips!")
            return 0
        return run_batch(cfg, ig, drive, count, label, dry_run)

    should, reason = decide(cfg, ig)
    log(("POST: " if should else "skip: ") + reason)
    if not should:
        return 0
    queue = ready_queue(cfg, drive)
    if not queue:
        log("queue folder is empty - add more clips!")
        return 0
    if dry_run:
        log(f"[dry-run] next: {queue[0][0]['name']}")
        return 0
    return 0 if post_one(cfg, ig, drive, *queue[0]) else 1


def clean_posted(cfg, drive):
    files = drive.list_files(cfg.posted)
    for f in files:
        drive.trash(f["id"])
        log(f"deleted {f['name']} from posted folder")
    log(f"{len(files)} file(s) deleted")


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
    ap.add_argument("--clean-posted", action="store_true", help="delete everything in the posted folder")
    ap.add_argument("--post-all", action="store_true", help="post every ready clip in the queue now")
    ap.add_argument("--pairs", action="store_true", help="list clip/caption pairs, post nothing")
    a = ap.parse_args()

    if a.clean_posted:
        cfg = Config(need_instagram=False)
        return clean_posted(cfg, Drive(cfg.sa_json))

    if a.check_drive:
        cfg = Config(need_instagram=False)
        drive = Drive(cfg.sa_json)
        log(f"Drive service account: {drive.email}")
        for label, fid in (("queue", cfg.queue), ("posted", cfg.posted), ("failed", cfg.failed)):
            if fid:
                log(f"Drive {label} folder OK: '{drive.folder_name(fid)}'")
        q = drive.queue(cfg.queue)
        log(f"{len(q)} clips in queue, {sum(1 for _, c in q if c)} with caption files")
        posted = drive.svc.files().list(
            q=f"'{cfg.posted}' in parents and trashed = false",
            fields="files(name,createdTime,owners(me))", supportsAllDrives=True,
            includeItemsFromAllDrives=True).execute().get("files", [])
        log(f"{len(posted)} file(s) in posted folder")
        for f in posted:
            owner = "robot" if any(o.get("me") for o in f.get("owners", [])) else "you"
            log(f"  - {f['name']} (owner: {owner}, created {f['createdTime']})")
        return 0

    cfg = Config()
    ig = Instagram(cfg.ig_token, cfg.ig_user_id, cfg.ig_host, cfg.ig_version)
    drive = Drive(cfg.sa_json)

    if a.check:
        return check(cfg, ig, drive)
    if a.pairs:
        return run_batch(cfg, ig, drive, 10**6, "pair check", dry_run=True)
    if a.post_all:
        return run_batch(cfg, ig, drive, 10**6, "post-all", a.dry_run)
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
