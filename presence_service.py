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


def _fmt_ist_human(ts):
    if not ts:
        return "Never seen active"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + IST_OFFSET
    return dt.strftime("%d %b %Y, %H:%M IST")


def build_batch_alert_html(to_alert):
    """Build one HTML email summarising all employees who crossed the threshold."""
    n = len(to_alert)
    now_str = now_ist().strftime("%d %b %Y, %H:%M IST")
    employee_word = "employees" if n != 1 else "employee"

    rows_html = ""
    for item in to_alert:
        state_color = "#ef4444" if item["state"] in ("Away", "Offline", "BeRightBack",
                                                      "OutOfOffice", "PresenceUnknown") \
                      else "#f59e0b"
        rows_html += (
            f'<tr style="border-bottom:1px solid #e5e7eb">'
            f'<td style="padding:10px 14px;font-weight:600">{item["name"]}</td>'
            f'<td style="padding:10px 14px;color:#6b7280">{item["email"]}</td>'
            f'<td style="padding:10px 14px;color:{state_color};font-weight:500">{item["state"]}</td>'
            f'<td style="padding:10px 14px;font-weight:700;color:#ef4444">{item["minutes"]} min</td>'
            f'<td style="padding:10px 14px;color:#6b7280">{item["last_active"]}</td>'
            f'</tr>'
        )

    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:system-ui,-apple-system,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;padding:32px 0">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0"
           style="background:#ffffff;border-radius:10px;overflow:hidden;
                  border:1px solid #e5e7eb;box-shadow:0 1px 4px rgba(0,0,0,.07)">

      <!-- Header -->
      <tr>
        <td style="background:#111827;padding:20px 28px">
          <span style="color:#38bdf8;font-size:13px;font-weight:700;letter-spacing:.06em;
                       text-transform:uppercase">FindPresence</span>
          <span style="color:#ef4444;font-size:13px;font-weight:700;margin-left:12px">
            &bull; Inactivity Report
          </span>
        </td>
      </tr>

      <!-- Summary -->
      <tr>
        <td style="padding:24px 28px 16px">
          <p style="margin:0 0 6px;font-size:22px;font-weight:700;color:#111827">
            {n} {employee_word} idle for 10+ minutes
          </p>
          <p style="margin:0;font-size:13px;color:#6b7280">
            Detected at <b>{now_str}</b> during the monitoring window.
            The following {employee_word} {'have' if n != 1 else 'has'} been inactive on
            Microsoft Teams beyond the alert threshold.
          </p>
        </td>
      </tr>

      <!-- Table -->
      <tr>
        <td style="padding:0 28px 24px">
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden">
            <thead>
              <tr style="background:#f3f4f6">
                <th style="padding:10px 14px;text-align:left;font-size:11px;
                           text-transform:uppercase;letter-spacing:.06em;
                           color:#6b7280;font-weight:600">Name</th>
                <th style="padding:10px 14px;text-align:left;font-size:11px;
                           text-transform:uppercase;letter-spacing:.06em;
                           color:#6b7280;font-weight:600">Email</th>
                <th style="padding:10px 14px;text-align:left;font-size:11px;
                           text-transform:uppercase;letter-spacing:.06em;
                           color:#6b7280;font-weight:600">State</th>
                <th style="padding:10px 14px;text-align:left;font-size:11px;
                           text-transform:uppercase;letter-spacing:.06em;
                           color:#6b7280;font-weight:600">Idle for</th>
                <th style="padding:10px 14px;text-align:left;font-size:11px;
                           text-transform:uppercase;letter-spacing:.06em;
                           color:#6b7280;font-weight:600">Last active</th>
              </tr>
            </thead>
            <tbody>{rows_html}</tbody>
          </table>
        </td>
      </tr>

      <!-- Footer -->
      <tr>
        <td style="background:#f9fafb;border-top:1px solid #e5e7eb;padding:14px 28px">
          <p style="margin:0;font-size:11px;color:#9ca3af">
            Generated by <b>FindPresence</b> at {now_str}.
            Open the dashboard to see full details and history.
          </p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>
"""


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

    # We still refresh the user list outside the window so the dashboard
    # shows the roster — but we only fetch presence + alert while armed.
    try:
        users = pick_users(client, cfg)
    except Exception as e:
        err = f"list_users failed: {e}"
        log.error(err)
        db.log_poll(conn, armed, 0, note="list_users error",
                    success=False, error_text=err)
        return

    file_ignore = load_ignore_file(cfg.get("ignore_file"))
    for u in users:
        email = user_email(u)
        db.upsert_user(conn, u["id"], email, u.get("displayName", ""))
        if email in file_ignore:
            db.set_ignored(conn, u["id"], True)

    if not armed:
        db.log_poll(conn, armed, 0, note="outside window")
        return

    rows = {r["id"]: r for r in db.all_users(conn)}
    active_ids = [u["id"] for u in users
                  if not rows.get(u["id"]) or not rows[u["id"]]["ignored"]]

    if not active_ids:
        db.log_poll(conn, armed, 0, note="no users to poll")
        return

    try:
        presences = client.get_presences(active_ids)
    except Exception as e:
        err = f"get_presences failed: {e}"
        log.error(err)
        db.log_poll(conn, armed, len(active_ids), note="presence fetch error",
                    success=False, error_text=err)
        return

    now = time.time()
    to_alert = []  # collect employees who crossed threshold this poll

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
                to_alert.append({
                    "uid": uid,
                    "name": name or email or uid,
                    "email": email or "—",
                    "state": availability,
                    "minutes": minutes,
                    "last_active": _fmt_ist_human(user_row["last_active_ts"]),
                })

    # Send ONE batched report email for all employees who crossed threshold this poll.
    if armed and to_alert:
        n = len(to_alert)
        names = ", ".join(item["name"] for item in to_alert[:3])
        if n > 3:
            names += f" +{n - 3} more"
        subject = (
            f"[FindPresence] {n} employee{'s' if n != 1 else ''} idle 10+ min"
            f" — {now_ist().strftime('%d %b, %H:%M IST')}"
        )
        try:
            client.send_mail(
                sender=cfg["notify_from"],
                to=cfg["notify_to"],
                subject=subject,
                body_html=build_batch_alert_html(to_alert),
            )
            for item in to_alert:
                db.mark_alerted(conn, item["uid"], now)
                db.set_user_state(conn, item["uid"], streak_alerted=1)
            log.info("ALERT batch email sent — %d employees: %s", n, names)
        except Exception as e:
            log.error("batch send_mail failed: %s", e)

    db.log_poll(conn, armed, len(active_ids))
