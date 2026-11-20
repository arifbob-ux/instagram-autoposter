# Instagram Auto-Poster: Setup

**How it works:** you drop clips into a Google Drive folder from your PC or phone. GitHub's servers wake up every 10 minutes, and when a time slot is due, they take the next clip, publish it as a Reel through Instagram's **official API**, and move it to a "Posted" folder. Your PC can be off the whole time.

```
Google Drive "Queue" ──► GitHub Actions (every 10 min, free) ──► Instagram Reels API
        ▲                          │
   you add clips                   └──► moves clip to "Posted" (or "Failed")
```

- **Posts per day:** 1 to 100. Instagram allows at most **100 API posts per 24 hours** per account, and the script checks that limit itself.
- **Spacing:** posts are spread evenly across your active hours. For example, 50 a day between 08:00 and 24:00 means one every ~19 minutes.
- **Order:** clips go out in file-name order, so name them `001_...mp4`, `002_...mp4`, and so on.
- **Captions:** put a `.txt` file with the same name next to a clip (`001_kill.mp4` + `001_kill.txt`) and it becomes that clip's caption and hashtags. Clips without one use `DEFAULT_CAPTION`.

Setup takes about 30 to 45 minutes, once.

---

## Step 1: Instagram account
Your account must be **Professional** (Business or Creator). In the Instagram app go to Settings → Account type and tools → Switch to professional account. This is free.

## Step 2: Meta developer app and access token
1. Go to https://developers.facebook.com → **My Apps → Create App** → choose **Other** → type **Business**.
2. In the app dashboard, add the **Instagram** product → **API setup with Instagram login**.
3. Under **Generate access tokens**, click **Add account**, log in to your Instagram account and allow everything. Make sure these permissions are included:
   `instagram_business_basic`, `instagram_business_content_publish`.
4. Click **Generate token** and copy it. It's a long-lived token that lasts 60 days, and the weekly workflow renews it automatically.

You don't need App Review because you are only posting to your own account. The app can stay in Development mode.

## Step 3: Google Drive folders and service account
1. In Google Drive, create 3 folders: **IG Queue**, **IG Posted** and **IG Failed**.
   Open each one and copy the ID from the URL: `drive.google.com/drive/folders/`**`THIS_PART`**
2. Go to https://console.cloud.google.com → create a project → **APIs & Services → Library** → enable **Google Drive API**.
3. Go to **IAM & Admin → Service Accounts → Create service account** (any name) → open it → **Keys → Add key → JSON**. A `.json` file downloads.
4. Open the JSON file and copy the `client_email` value (it looks like `xxx@xxx.iam.gserviceaccount.com`).
   **Share all 3 Drive folders** with that email as an **Editor**.

## Step 4: GitHub repository
1. Create a free account at https://github.com, then create a **new repository** and upload everything in this folder, including the `.github` folder.
   Make the repo **Public**. Public repos get unlimited free Actions minutes, while private ones only get 2,000 minutes a month, which this would exceed. Your passwords stay hidden in Secrets and your videos stay private in Drive. Only this code is public.
2. In the repo, go to **Settings → Secrets and variables → Actions**.

   **Secrets** tab, click **New repository secret** for each:
   | Name | Value |
   |---|---|
   | `IG_ACCESS_TOKEN` | the token from Step 2 |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | the **whole contents** of the .json file from Step 3 |
   | `GH_PAT` | a GitHub token for auto-renewing the IG token (see below) |

   **Variables** tab:
   | Name | Example | Meaning |
   |---|---|---|
   | `DRIVE_QUEUE_FOLDER_ID` | `1AbC...` | the Queue folder ID |
   | `DRIVE_POSTED_FOLDER_ID` | `1XyZ...` | the Posted folder ID |
   | `DRIVE_FAILED_FOLDER_ID` | `1Qrs...` | the Failed folder ID (optional) |
   | `POSTS_PER_DAY` | `50` | 1 to 100 |
   | `TIMEZONE` | `America/New_York` | posting time zone (US Eastern; or `America/Chicago`, `America/Denver`, `America/Los_Angeles`) |
   | `ACTIVE_START_HOUR` | `8` | first post hour (0 to 23) |
   | `ACTIVE_END_HOUR` | `24` | stop hour (use the same value as start to post all 24 hours) |
   | `DEFAULT_CAPTION` | `#gta6 #gaming` | used when a clip has no .txt file |

   **GH_PAT:** go to GitHub → your avatar → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate. Select only this repository, and under Repository permissions set **Secrets: Read and write**. Copy the token into the `GH_PAT` secret.

## Step 5: Test
1. Put 2 or 3 clips into **IG Queue** (vertical 9:16 MP4, 3 seconds to 15 minutes, under 300 MB).
2. In the repo, open **Actions → Post to Instagram → Run workflow** and leave the mode as `check`.
   The log should show your @username, the Drive folder names, the clip count and the posting plan.
3. Run it again with mode `tick` to post one clip now if a slot is due.

That's it. It runs on its own from now on. Keep the Queue folder topped up: at 100 a day you need 100 clips a day in there.

---

## Day-to-day
- **Change posts per day:** edit the `POSTS_PER_DAY` variable. It takes effect on the next run.
- **Pause:** go to Actions → Post to Instagram → ⋯ → **Disable workflow**.
- **See what happened:** open the Actions tab. Each run logs `skip: on schedule..., next slot 14:20` or `published 001_x.mp4`.
- **A clip failed:** it is moved to **IG Failed** so the queue keeps going, and the run log shows Instagram's reason.

## Good to know
- GitHub's 10-minute schedule can run a few minutes late when GitHub is busy. The script catches up on missed slots, so you still reach your daily number.
- Posts you make by hand count toward the daily target.
- Even with the official API, 100 Reels a day from one account is very aggressive. Instagram's algorithm usually spreads reach thinner, and reposting other people's footage can get copyright strikes. Starting around 25 to 40 a day and watching your insights is safer.

## Alternative: run on your own always-on server (VPS)
If you'd rather not use GitHub, any $4–6/month Linux VPS works:
```bash
pip install -r requirements.txt
export IG_ACCESS_TOKEN=... GOOGLE_SERVICE_ACCOUNT_JSON="$(cat sa.json)" DRIVE_QUEUE_FOLDER_ID=... DRIVE_POSTED_FOLDER_ID=... POSTS_PER_DAY=50
python poster.py --loop
```
(Run it under `systemd` or `tmux` so it keeps going, and refresh the token every ~50 days with `python refresh_token.py`.)
