"""Background poller. Only runs alerts within the EST monitoring window."""
import logging
import os
import json
import time
from datetime import datetime, time as dtime, timezone, timedelta

from graph_client import GraphClient, ACTIVE_STATES
import db

log = logging.getLogger("presence")

# Eastern Daylight Time (EDT: UTC-4, used May - October)
EST_OFFSET = timedelta(hours=-4)


def now_est():
    return datetime.now(timezone.utc) + EST_OFFSET


def _parse_hhmm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


def in_window(cfg, now=None):
    """Check global monitoring window (for backwards compatibility)."""
    now = now or now_est()
    start = _parse_hhmm(cfg.get("window_start_ist", "18:30"))
    end = _parse_hhmm(cfg.get("window_end_ist", "03:30"))
    t = now.time()
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end


def user_in_window(emp_data, now=None):
    """Check if a user is currently in their monitoring (alert) window."""
    if not emp_data:
        return False
    now = now or now_est()
    try:
        start = _parse_hhmm(emp_data.get("window_start", "18:30"))
        end = _parse_hhmm(emp_data.get("window_end", "03:30"))
    except (ValueError, KeyError):
        return False
    t = now.time()
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end


def user_in_display_window(emp_data, now=None):
    """Check if a user is in their display (working hours) window for dashboard."""
    if not emp_data:
        return False
    now = now or now_est()
    try:
        start = _parse_hhmm(emp_data.get("display_window_start", emp_data.get("window_start", "18:30")))
        end = _parse_hhmm(emp_data.get("display_window_end", emp_data.get("window_end", "03:30")))
    except (ValueError, KeyError):
        return False
    t = now.time()
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end


def user_should_show_available(emp_data, now=None):
    """Check if user should be shown as 'Available' (in display window but not monitoring window)."""
    if not emp_data:
        return False
    # If no separate display window, use monitoring window
    if "display_window_start" not in emp_data:
        return False
    # Show as Available if in display window but NOT in monitoring window
    in_display = user_in_display_window(emp_data, now)
    in_monitoring = user_in_window(emp_data, now)
    return in_display and not in_monitoring


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


def load_employees_config(path=None):
    """Load employees.json with department, location, and per-user time windows."""
    if not path:
        path = os.path.join(os.path.dirname(__file__), "employees.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        # Map email -> {department, location, window_start, window_end}
        return {emp["email"].lower(): emp for emp in data.get("employees", [])}
    except Exception as e:
        log.warning("Failed to load employees.json: %s", e)
        return {}


def user_email(u):
    return (u.get("mail") or u.get("userPrincipalName") or "").lower()


def pick_users(client, cfg, emp_config=None):
    """List all users, then filter to only those in employees.json or config monitor_users."""
    users = client.list_users()

    # If employees.json is loaded, use it as the source of truth
    if emp_config:
        wanted = set(emp_config.keys())
        users = [u for u in users if user_email(u) in wanted]
        return users

    # Fallback to config-based list if employees.json not available
    if cfg.get("monitor_scope") == "list":
        wanted = {e.lower() for e in cfg.get("monitor_users", [])}
        users = [u for u in users if user_email(u) in wanted]

    return users


def _fmt_est_human(ts):
    if not ts:
        return "Never seen active"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + EST_OFFSET
    return dt.strftime("%d %b %Y, %H:%M EST")


def _fmt_est_clock(ts):
    """Format a unix timestamp as EST HH:MM (for login/logout), or — if missing."""
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + EST_OFFSET
    return dt.strftime("%H:%M")


def _window_seconds(emp_data):
    """Length of an employee's full working day (DISPLAY window) in seconds.

    Used as the presence-% denominator. Idle is only ever tracked during the
    narrower monitoring window, so any 'grace' period before monitoring starts
    (e.g. Sonali's 3-7PM IST) automatically counts as available time.
    """
    if not emp_data:
        return 0
    try:
        start_str = emp_data.get("display_window_start") or emp_data["window_start"]
        end_str = emp_data.get("display_window_end") or emp_data["window_end"]
        sh, sm = start_str.split(":")
        eh, em = end_str.split(":")
        start = int(sh) * 3600 + int(sm) * 60
        end = int(eh) * 3600 + int(em) * 60
    except (KeyError, ValueError, AttributeError):
        return 0
    if end <= start:
        end += 24 * 3600
    return end - start


def est_day_bounds(days_ago=1):
    """Return (start_ts_utc, end_ts_utc, 'YYYY-MM-DD') for an EST calendar day N days ago."""
    target = now_est() - timedelta(days=days_ago)
    day_str = target.strftime("%Y-%m-%d")
    # EST midnight wall-clock -> true UTC unix timestamp.
    naive_est_midnight = target.replace(hour=0, minute=0, second=0,
                                        microsecond=0, tzinfo=None)
    start_dt_utc = (naive_est_midnight - EST_OFFSET).replace(tzinfo=timezone.utc)
    start_ts = start_dt_utc.timestamp()
    return start_ts, start_ts + 24 * 3600, day_str


def build_daily_report_html(employees, report_date):
    """Build comprehensive daily report: login/logout, presence %, longest break, total idle."""
    date_str = datetime.fromtimestamp(report_date, tz=timezone.utc).strftime("%d %B %Y")

    rows_html = ""
    total_monitored = 0
    total_idle_all = 0
    concern_count = 0

    for i, emp in enumerate(employees, 1):
        total_monitored += 1
        idle_seconds = emp.get("total_seconds", 0)
        total_idle_all += idle_seconds

        pct = emp.get("presence_pct")           # int or None
        longest_secs = emp.get("longest_break_seconds", 0)

        # "Concern" = low presence (<70%) or a single break over 30 min
        concern = (pct is not None and pct < 70) or longest_secs > 1800
        if concern:
            concern_count += 1
        row_bg = "background:#fff5f5;" if concern else ""

        # Presence % cell color
        if pct is None:
            pct_html = '<span style="color:#9ca3af">—</span>'
        elif pct < 70:
            pct_html = f'<span style="color:#ef4444;font-weight:700">{pct}%</span>'
        elif pct < 85:
            pct_html = f'<span style="color:#f59e0b;font-weight:700">{pct}%</span>'
        else:
            pct_html = f'<span style="color:#16a34a;font-weight:700">{pct}%</span>'

        longest_color = "#ef4444" if longest_secs > 1800 else "#6b7280"

        rows_html += (
            f'<tr style="border-bottom:1px solid #e5e7eb;{row_bg}">'
            f'<td style="padding:10px 12px;text-align:center;font-weight:600">{i}</td>'
            f'<td style="padding:10px 12px;font-weight:600">{emp["name"]}'
            f'<div style="font-size:11px;color:#9ca3af;font-weight:400">{emp["email"]}</div></td>'
            f'<td style="padding:10px 12px;color:#6b7280;font-size:12px">{emp.get("department", "—")}</td>'
            f'<td style="padding:10px 12px;text-align:center;color:#374151;font-size:12px">{emp.get("login", "—")}</td>'
            f'<td style="padding:10px 12px;text-align:center;color:#374151;font-size:12px">{emp.get("logout", "—")}</td>'
            f'<td style="padding:10px 12px;text-align:center">{pct_html}</td>'
            f'<td style="padding:10px 12px;text-align:center;color:{longest_color};font-weight:600">{emp.get("longest_break", "—")}</td>'
            f'<td style="padding:10px 12px;text-align:center;color:#ef4444;font-weight:700">{emp["total_duration"]}</td>'
            f'</tr>'
        )

    def fmt_dur(seconds):
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m"
        return f"{s}s"

    total_idle_str = fmt_dur(total_idle_all)

    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:system-ui,-apple-system,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;padding:32px 0">
  <tr><td align="center">
    <table width="900" cellpadding="0" cellspacing="0"
           style="background:#ffffff;border-radius:10px;overflow:hidden;
                  border:1px solid #e5e7eb;box-shadow:0 1px 4px rgba(0,0,0,.07)">

      <!-- Header -->
      <tr>
        <td style="background:#111827;padding:24px 28px">
          <span style="color:#38bdf8;font-size:14px;font-weight:700;letter-spacing:.06em;
                       text-transform:uppercase">FindPresence</span>
          <span style="color:#ef4444;font-size:14px;font-weight:700;margin-left:12px">
            &bull; Daily Employee Activity Report
          </span>
        </td>
      </tr>

      <!-- Summary Stats -->
      <tr>
        <td style="padding:24px 28px;background:#f9fafb;border-bottom:1px solid #e5e7eb">
          <p style="margin:0 0 12px;font-size:18px;font-weight:700;color:#111827">
            {date_str}
          </p>
          <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
            <tr>
              <td style="padding:8px 0;border-right:1px solid #e5e7eb;padding-right:20px;margin-right:20px">
                <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.06em;font-weight:600">Total Monitored</div>
                <div style="font-size:24px;font-weight:700;color:#111827;margin-top:4px">{total_monitored}</div>
              </td>
              <td style="padding:8px 0;border-right:1px solid #e5e7eb;padding-right:20px;margin-right:20px">
                <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.06em;font-weight:600">Total Idle Time</div>
                <div style="font-size:24px;font-weight:700;color:#ef4444;margin-top:4px">{total_idle_str}</div>
              </td>
              <td style="padding:8px 0">
                <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.06em;font-weight:600">Needs Attention</div>
                <div style="font-size:24px;font-weight:700;color:#ef4444;margin-top:4px">{concern_count}</div>
              </td>
            </tr>
          </table>
        </td>
      </tr>

      <!-- Main Table -->
      <tr>
        <td style="padding:24px 28px">
          <p style="margin:0 0 12px;font-size:13px;font-weight:600;color:#111827">Employee Inactivity Summary</p>
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden">
            <thead>
              <tr style="background:#f3f4f6">
                <th style="padding:12px 12px;text-align:center;font-size:11px;
                           text-transform:uppercase;letter-spacing:.06em;
                           color:#6b7280;font-weight:600;width:36px">#</th>
                <th style="padding:12px 12px;text-align:left;font-size:11px;
                           text-transform:uppercase;letter-spacing:.06em;
                           color:#6b7280;font-weight:600">Employee</th>
                <th style="padding:12px 12px;text-align:left;font-size:11px;
                           text-transform:uppercase;letter-spacing:.06em;
                           color:#6b7280;font-weight:600">Dept</th>
                <th style="padding:12px 12px;text-align:center;font-size:11px;
                           text-transform:uppercase;letter-spacing:.06em;
                           color:#6b7280;font-weight:600">Login</th>
                <th style="padding:12px 12px;text-align:center;font-size:11px;
                           text-transform:uppercase;letter-spacing:.06em;
                           color:#6b7280;font-weight:600">Logout</th>
                <th style="padding:12px 12px;text-align:center;font-size:11px;
                           text-transform:uppercase;letter-spacing:.06em;
                           color:#6b7280;font-weight:600">Presence %</th>
                <th style="padding:12px 12px;text-align:center;font-size:11px;
                           text-transform:uppercase;letter-spacing:.06em;
                           color:#6b7280;font-weight:600">Longest Break</th>
                <th style="padding:12px 12px;text-align:center;font-size:11px;
                           text-transform:uppercase;letter-spacing:.06em;
                           color:#6b7280;font-weight:600">Total Idle</th>
              </tr>
            </thead>
            <tbody>{rows_html}</tbody>
          </table>
        </td>
      </tr>

      <!-- Important Notes -->
      <tr>
        <td style="padding:20px 28px;background:#fffbeb;border-top:1px solid #fcd34d">
          <p style="margin:0 0 10px;font-size:12px;font-weight:700;color:#92400e;text-transform:uppercase;letter-spacing:.05em">
            ⚠️ Important Notes
          </p>
          <ul style="margin:0;padding:0 0 0 20px;color:#92400e;font-size:12px;line-height:1.6">
            <li><b>Login / Logout</b> = first and last time the employee was active during their shift (EDT)</li>
            <li><b>Presence %</b> = share of the shift they were active &mdash; <span style="color:#16a34a;font-weight:700">85%+ good</span>, <span style="color:#f59e0b;font-weight:700">70-84% ok</span>, <span style="color:#ef4444;font-weight:700">below 70% low</span></li>
            <li><b>Longest Break</b> = their single longest continuous idle stretch (a 2h gap matters more than many short ones)</li>
            <li><b>Total Idle</b> = all inactive time added up during the shift</li>
            <li>Red highlighted rows = needs attention (presence below 70% or a break over 30 min)</li>
            <li>Sorted by total idle time (highest first). Times in EDT. Login/Logout shows "—" if the employee was never seen active that day.</li>
          </ul>
        </td>
      </tr>

      <!-- Footer -->
      <tr>
        <td style="background:#f3f4f6;border-top:1px solid #e5e7eb;padding:14px 28px">
          <p style="margin:0;font-size:11px;color:#9ca3af">
            Generated by FindPresence • {datetime.fromtimestamp(report_date, tz=timezone.utc).strftime("%d %b %Y at %H:%M UTC")}
          </p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>
"""


def build_batch_alert_html(to_alert):
    """Build one HTML email summarising all employees who crossed the threshold."""
    n = len(to_alert)
    now_str = now_est().strftime("%d %b %Y, %H:%M EST")
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
    emp_config = load_employees_config()

    threshold = cfg.get("inactive_threshold_minutes", 10) * 60
    interval = cfg.get("poll_interval_seconds", 60)

    log.info("Poller configured: threshold=%ds, interval=%ds, employees=%d",
             threshold, interval, len(emp_config))

    poll_count = 0
    while not stop_event.is_set():
        try:
            poll_count += 1
            log.info("Poll iteration #%d at %s EST", poll_count, now_est().strftime("%H:%M:%S"))
            poll_once(client, conn, cfg, threshold, emp_config)
        except Exception as e:
            log.exception("poll loop error: %s", e)
        stop_event.wait(interval)


def poll_once(client, conn, cfg, threshold, emp_config=None):
    if emp_config is None:
        emp_config = {}
    # Global armed state is not used when we have per-user windows
    # Check per-user armed state in the loop below

    # Filter to only employees in employees.json, then fetch presence for them
    try:
        users = pick_users(client, cfg, emp_config)
    except Exception as e:
        err = f"list_users failed: {e}"
        log.error(err)
        db.log_poll(conn, False, 0, note="list_users error",
                    success=False, error_text=err)
        return

    file_ignore = load_ignore_file(cfg.get("ignore_file"))
    for u in users:
        email = user_email(u)
        emp_data = emp_config.get(email.lower())
        department = emp_data.get("department") if emp_data else None
        location = emp_data.get("location") if emp_data else None
        db.upsert_user(conn, u["id"], email, u.get("displayName", ""), department, location)
        if email in file_ignore:
            db.set_ignored(conn, u["id"], True)

    rows = {r["id"]: r for r in db.all_users(conn)}
    active_ids = [u["id"] for u in users
                  if not rows.get(u["id"]) or not rows[u["id"]]["ignored"]]

    if not active_ids:
        db.log_poll(conn, False, 0, note="no users to poll")
        return

    try:
        presences = client.get_presences(active_ids)
    except Exception as e:
        err = f"get_presences failed: {e}"
        log.error(err)
        db.log_poll(conn, False, len(active_ids), note="presence fetch error",
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

        # Check per-user armed state (in their monitoring window)
        emp_data = emp_config.get(email.lower())
        user_armed = user_in_window(emp_data) if emp_data else in_window(cfg)

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
            # Track login/logout across the full working day (display window),
            # so a grace period before monitoring (e.g. Sonali's 3-7PM) still
            # counts toward her login time.
            in_working_day = user_in_display_window(emp_data) if emp_data else user_armed
            if in_working_day:
                db.record_active(conn, uid, now_est().strftime("%Y-%m-%d"), now)
        elif user_armed:
            # User is INACTIVE and WITHIN their monitoring window — track inactivity
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

            if not already_alerted and inactive_for >= threshold:
                minutes = int(inactive_for // 60)
                to_alert.append({
                    "uid": uid,
                    "name": name or email or uid,
                    "email": email or "—",
                    "state": availability,
                    "minutes": minutes,
                    "last_active": _fmt_est_human(user_row["last_active_ts"]),
                })
        else:
            # User is INACTIVE but OUTSIDE their monitoring window — close streak, don't alert
            if user_row["in_inactive_streak"]:
                db.end_inactivity(conn, uid, now)
                db.set_user_state(
                    conn, uid,
                    current_state=availability,
                    in_inactive_streak=0,
                    streak_started_ts=None,
                    streak_alerted=0,
                )

    # Mark employees as alerted for daily report (do NOT send immediate emails)
    # Emails are sent once daily via send_daily_report() endpoint
    if to_alert:
        for item in to_alert:
            db.mark_alerted(conn, item["uid"], now)
            db.set_user_state(conn, item["uid"], streak_alerted=1)
        log.info("Marked %d employees as alerted (daily report will be sent at end of day)", len(to_alert))

    db.log_poll(conn, True, len(active_ids))  # Always successful poll if we get here


def send_daily_report(client, conn, cfg, date_start_ts, date_end_ts, day_str, emp_config=None):
    """Send daily report: each employee's login/logout, presence %, longest break, total idle."""
    if emp_config is None:
        emp_config = load_employees_config()

    employees_data = db.daily_report_all(conn, date_start_ts, date_end_ts, day_str)

    if not employees_data:
        log.info("Daily report: no data for %s", day_str)
        return

    def fmt_dur(seconds):
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m"
        return f"{s}s"

    employees_list = []
    for row in employees_data:
        emp_data = emp_config.get((row["email"] or "").lower())
        idle = row["total_seconds"] or 0
        shift = _window_seconds(emp_data)
        first_ts = row["first_active_ts"]
        longest = row["longest_break"] or 0

        # Only compute presence % when we actually have signal for the day.
        has_data = (first_ts is not None) or (row["event_count"] or 0) > 0
        if shift > 0 and has_data:
            active = max(0, shift - idle)
            pct = max(0, min(100, int(round(active / shift * 100))))
        else:
            pct = None

        employees_list.append({
            "name": row["display_name"] or row["email"],
            "email": row["email"],
            "department": row["department"] or "—",
            "total_seconds": idle,
            "total_duration": fmt_dur(idle) if idle else "—",
            "login": _fmt_est_clock(first_ts),
            "logout": _fmt_est_clock(row["last_active_ts"]),
            "presence_pct": pct,
            "longest_break": fmt_dur(longest) if longest else "—",
            "longest_break_seconds": longest,
        })

    html = build_daily_report_html(employees_list, date_start_ts)
    if not html:
        return

    total_employees = len(employees_list)
    attention = sum(1 for e in employees_list
                    if (e["presence_pct"] is not None and e["presence_pct"] < 70)
                    or e["longest_break_seconds"] > 1800)
    subject = f"[FindPresence] Daily Report ({day_str}) — {total_employees} employees, {attention} need attention"

    try:
        client.send_mail(
            sender=cfg["notify_from"],
            to=cfg["notify_to"],
            subject=subject,
            body_html=html,
        )
        log.info("Daily report sent — %d employees (%d need attention), %s",
                 total_employees, attention, day_str)
    except Exception as e:
        log.error("send daily report failed: %s", e)
