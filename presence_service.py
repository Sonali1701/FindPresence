"""Background poller. Only runs alerts within the IST monitoring window."""
import logging
import os
import time
from datetime import datetime, time as dtime, timezone, timedelta

from graph_client import GraphClient, ACTIVE_STATES
import db

log = logging.getLogger("presence")

# Asia/Kolkata is UTC+5:30 with no DST — hard-code to avoid tzdata dependency.
IST_OFFSET = timedelta(hours=5, minutes=30)


def now_ist():
    return datetime.now(timezone.utc) + IST_OFFSET


def _parse_hhmm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


def in_window(cfg, now=None):
    now = now or now_ist()
    start = _parse_hhmm(cfg.get("window_start_ist", "18:30"))
    end = _parse_hhmm(cfg.get("window_end_ist", "03:30"))
    t = now.time()
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end


def load_ignore_file(path):
    if not path or not os.path.exists(path):
        return set()
    out = set()
    with open(path) as f:
        for line in f:
            line = line.strip().lower()
            if line and not line.startswith("#"):
                out.add(line)
    return out


def user_email(u):
    return (u.get("mail") or u.get("userPrincipalName") or "").lower()


def pick_users(client, cfg):
    users = client.list_users()
    if cfg.get("monitor_scope") == "list":
        wanted = {e.lower() for e in cfg.get("monitor_users", [])}
        users = [u for u in users if user_email(u) in wanted]
    return users


def build_alert_html(name, email, last_active_iso, current_state, minutes):
    last_active = "never observed active in this session" if not last_active_iso \
        else last_active_iso
    return (
        f"<p><b>{name}</b> ({email}) has been inactive on Teams "
        f"for <b>{minutes} minutes</b>.</p>"
        f"<ul>"
        f"<li>Current presence: <b>{current_state}</b></li>"
        f"<li>Last seen active: {last_active}</li>"
        f"<li>Detected at: {now_ist().strftime('%Y-%m-%d %H:%M IST')}</li>"
        f"</ul>"
        f"<p><i>FindPresence monitor</i></p>"
    )


def run_loop(cfg, stop_event):
    client = GraphClient(cfg["tenant_id"], cfg["client_id"], cfg["client_secret"])
    conn = db.connect(cfg["db_path"])
    db.init_db(conn)

    threshold = cfg.get("inactive_threshold_minutes", 10) * 60
    interval = cfg.get("poll_interval_seconds", 60)

    log.info("Poller configured: threshold=%ds, interval=%ds, window=%s-%s IST",
             threshold, interval, cfg.get("window_start_ist", "18:30"), cfg.get("window_end_ist", "03:30"))

    poll_count = 0
    while not stop_event.is_set():
        try:
            poll_count += 1
            log.info("Poll iteration #%d at %s IST", poll_count, now_ist().strftime("%H:%M:%S"))
            poll_once(client, conn, cfg, threshold)
        except Exception as e:
            log.exception("poll loop error: %s", e)
        stop_event.wait(interval)


def poll_once(client, conn, cfg, threshold):
    armed = in_window(cfg)
    
    if not armed:
        log.info("Outside monitoring window - skipping poll (current: %s IST, window: %s-%s)",
                 now_ist().strftime("%H:%M"), cfg.get("window_start_ist", "18:30"), cfg.get("window_end_ist", "03:30"))
        return

    log.info("Starting poll - inside monitoring window")
    
    try:
        users = pick_users(client, cfg)
        log.info("Fetched %d users from Microsoft Graph", len(users))
    except Exception as e:
        log.error("Failed to fetch users from Microsoft Graph: %s", e)
        return
    
    file_ignore = load_ignore_file(cfg.get("ignore_file"))

    # Refresh user rows, apply ignore.txt
    for u in users:
        email = user_email(u)
        db.upsert_user(conn, u["id"], email, u.get("displayName", ""))
        if email in file_ignore:
            db.set_ignored(conn, u["id"], True)

    # Pull current ignore flags + ids actually being monitored
    rows = {r["id"]: r for r in db.all_users(conn)}
    active_ids = [u["id"] for u in users
                  if not rows.get(u["id"]) or not rows[u["id"]]["ignored"]]

    log.info("Active users to poll: %d (ignored: %d)", len(active_ids), len(users) - len(active_ids))

    if not active_ids:
        db.log_poll(conn, armed, 0, note="no users to poll")
        log.info("No active users to poll - skipping")
        return

    try:
        presences = client.get_presences(active_ids)
        log.info("Fetched presence for %d users", len(presences))
    except Exception as e:
        log.error("Failed to fetch presence data: %s", e)
        return
    
    now = time.time()

    for uid in active_ids:
        user_row = rows.get(uid)
        if not user_row:
            continue
        email = user_row["email"]
        name = user_row["display_name"]
        pres = presences.get(uid, {})
        availability = pres.get("availability", "Unknown")

        if availability in ACTIVE_STATES:
            # User is active — close any open inactivity event.
            if user_row["in_inactive_streak"]:
                db.end_inactivity(conn, uid, now)
            db.set_user_state(
                conn, uid,
                current_state=availability,
                current_state_since=now,
                last_active_ts=now,
                in_inactive_streak=0,
                streak_started_ts=None,
                streak_alerted=0,
            )
        else:
            # User is inactive.
            if not user_row["in_inactive_streak"]:
                # First inactive tick — start a streak (and an event row).
                db.start_inactivity(conn, uid, email, name, availability, now)
                db.set_user_state(
                    conn, uid,
                    current_state=availability,
                    current_state_since=now,
                    in_inactive_streak=1,
                    streak_started_ts=now,
                    streak_alerted=0,
                )
                streak_started = now
                already_alerted = False
            else:
                db.set_user_state(conn, uid, current_state=availability)
                streak_started = user_row["streak_started_ts"] or now
                already_alerted = bool(user_row["streak_alerted"])

            inactive_for = now - streak_started
            if armed and not already_alerted and inactive_for >= threshold:
                minutes = int(inactive_for // 60)
                last_active_iso = None
                if user_row["last_active_ts"]:
                    last_active_iso = datetime.fromtimestamp(
                        user_row["last_active_ts"], tz=timezone.utc
                    ).isoformat()
                try:
                    client.send_mail(
                        sender=cfg["notify_from"],
                        to=cfg["notify_to"],
                        subject=f"[Teams] {name} inactive {minutes}m",
                        body_html=build_alert_html(
                            name, email, last_active_iso, availability, minutes),
                    )
                    db.mark_alerted(conn, uid, now)
                    db.set_user_state(conn, uid, streak_alerted=1)
                    log.info("ALERT %s (%s) inactive %dm", name, email, minutes)
                except Exception as e:
                    log.error("send_mail failed for %s: %s", email, e)

    db.log_poll(conn, armed, len(active_ids))
