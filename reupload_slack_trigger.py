"""
Seekho Re-Upload Slack Trigger
================================
Runs every 10 minutes on Railway.
1. Polls Redash query 148404 for re_uploaded videos
   — Seekho Health (hi-health) and Seekho Junior (hi-jr)
2. For each new re-upload not yet notified:
   - Posts a Slack message to #re-uploads (C0CD0QA74G5)
   - Highlights if publish date is TODAY or TOMORROW before 12PM
   - Marks as notified in PostgreSQL

Environment variables required:
  REDASH_API_KEY_REUPLOAD  = APAaTgHPjRChOfR644ULmAKIu3cTwgWnuinqyd3H
  REDASH_BASE_URL          = https://analytics.seekho.in
  SLACK_BOT_TOKEN          = your_slack_bot_token
  SLACK_REUPLOAD_CHANNEL   = C0CD0QA74G5
  DATABASE_URL             = postgresql://... (Railway PostgreSQL)
"""

import os
import sys
import requests
import psycopg2
import logging
import re
from datetime import datetime, timedelta, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────
REDASH_API_KEY  = os.environ["REDASH_API_KEY_REUPLOAD"]
REDASH_QUERY_ID = "148404"
REDASH_BASE_URL = os.environ.get("REDASH_BASE_URL", "https://analytics.seekho.in")
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL   = os.environ.get("SLACK_REUPLOAD_CHANNEL", "C0CD0QA74G5")
DATABASE_URL    = os.environ["DATABASE_URL"]
CMS_BASE_URL    = "https://cms.seekho.in/#/videos"

IST = timezone(timedelta(hours=5, minutes=30))

# ── App name mapping ─────────────────────────────────────────
APP_NAMES = {
    "hi-health": "SEEKHO HEALTH",
    "hi-jr":     "SEEKHO JUNIOR",
}

# ── Database ─────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DATABASE_URL)


def setup_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reupload_notifications (
            series_id       INTEGER PRIMARY KEY,
            series_title    TEXT,
            language        TEXT,
            notified_at     TIMESTAMP DEFAULT NOW()
        );
    """)
    conn.commit()
    cur.close()
    conn.close()
    log.info("DB ready.")


def is_notified(series_id: int) -> bool:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM reupload_notifications WHERE series_id = %s",
        (series_id,)
    )
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result is not None


def mark_notified(series_id: int, series_title: str, language: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO reupload_notifications (series_id, series_title, language)
        VALUES (%s, %s, %s)
        ON CONFLICT (series_id) DO UPDATE SET notified_at = NOW();
    """, (series_id, series_title, language))
    conn.commit()
    cur.close()
    conn.close()


# ── Redash ───────────────────────────────────────────────────

def fetch_reuploads() -> list:
    import time
    base = f"{REDASH_BASE_URL}/api"
    headers = {"Authorization": f"Key {REDASH_API_KEY}"}
    params = {"api_key": REDASH_API_KEY}

    # Step 1 (best effort): ask Redash to re-run the query so the data is fresh.
    # If anything goes wrong here we still read the latest saved result below.
    try:
        r = requests.post(f"{base}/queries/{REDASH_QUERY_ID}/results",
                          headers=headers, json={"max_age": 0}, timeout=60)
        job = r.json().get("job") if r.ok else None
        if job:
            for _ in range(30):
                time.sleep(2)
                jr = requests.get(f"{base}/jobs/{job['id']}",
                                  headers=headers, params=params, timeout=30)
                j = jr.json().get("job", {})
                if j.get("status") in (3, 4, 5):
                    log.info(f"Redash refresh finished (job status {j.get('status')}).")
                    break
            else:
                log.warning("Redash refresh still running after 60s; using latest saved result.")
    except Exception as e:
        log.warning(f"Redash refresh step skipped: {e}")

    # Step 2: read the latest result. The '.json' at the end is required.
    resp = requests.get(f"{base}/queries/{REDASH_QUERY_ID}/results.json",
                        params=params, timeout=60)
    resp.raise_for_status()
    body = resp.json()
    if "query_result" not in body:
        raise RuntimeError(f"Unexpected Redash response: {str(body)[:300]}")
    rows = body["query_result"]["data"]["rows"]
    log.info(f"Found {len(rows)} re-uploaded videos.")
    new = [r for r in rows if not is_notified(r["series_id"])]
    log.info(f"{len(new)} new re-uploads to notify.")
    return new


# ── CMS link builder ─────────────────────────────────────────

def build_cms_link(series_id: int, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"{CMS_BASE_URL}/{series_id}/{slug}"


# ── Publish date formatter with highlight ────────────────────

def format_publish_date(publish_dt_str) -> str:
    if not publish_dt_str:
        return "Not scheduled"

    try:
        publish_dt = datetime.fromisoformat(str(publish_dt_str))
        # Make IST-aware if not already
        if publish_dt.tzinfo is None:
            publish_dt = publish_dt.replace(tzinfo=IST)
    except Exception:
        return "Not scheduled"

    now_ist      = datetime.now(IST)
    today_ist    = now_ist.date()
    tomorrow_ist = today_ist + timedelta(days=1)
    publish_date = publish_dt.date()
    publish_hour = publish_dt.hour

    formatted = publish_dt.strftime("%d %b %Y, %I:%M %p IST").upper()

    if publish_date == today_ist:
        return f"🔴 {formatted} *(TODAY)*"
    elif publish_date == tomorrow_ist and publish_hour < 12:
        return f"🟡 {formatted} *(Tomorrow first half)*"
    else:
        return formatted


# ── Slack ────────────────────────────────────────────────────

def send_slack_notification(row: dict):
    app_name      = APP_NAMES.get(row["language"], row["language"].upper())
    series_id     = row["series_id"]
    title         = row["series_title"]
    reupload_time = row.get("reupload_time_ist", "N/A")
    cms_link      = build_cms_link(series_id, title)
    publish_str   = format_publish_date(row.get("proposed_publish_date_ist"))

    message = f""":arrows_counterclockwise: *Re-upload has been submitted*

*App:* {app_name}
*Series ID:* `#{series_id}`
*Title:* {title}
*Proposed Publish Date:* {publish_str}
*Re-uploaded At:* {reupload_time}
*Review:* <{cms_link}|Open in CMS>"""

    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "channel": SLACK_CHANNEL,
        "text": message,
        "mrkdwn": True
    }
    try:
        resp = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=headers,
            json=payload,
            timeout=30
        )
        data = resp.json()
    except Exception as e:
        log.warning(f"Slack request failed for [{series_id}] {title}: {e} - will retry next run")
        return False
    if data.get("ok"):
        log.info(f"Slack notified: [{series_id}] {title}")
        return True
    log.warning(f"Slack failed for [{series_id}] {title}: {data.get('error')} - will retry next run")
    return False


# ── Main ─────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Seekho Re-Upload Slack Trigger — Starting")
    log.info(f"Run time: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
    log.info("=" * 60)

    setup_db()
    rows = fetch_reuploads()

    if not rows:
        log.info("No new re-uploads. Exiting.")
        return

    sent = 0
    for row in rows:
        if send_slack_notification(row):
            mark_notified(row["series_id"], row["series_title"], row["language"])
            sent += 1

    log.info(f"Done. Notified {sent} of {len(rows)} re-upload(s).")


if __name__ == "__main__":
    main()
