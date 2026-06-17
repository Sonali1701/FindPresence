"""Flask dashboard + background presence poller."""
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   session, jsonify, abort)

import db
from config_loader import load_config
from presence_service import (run_loop, in_window, now_est, EST_OFFSET,
                             load_employees_config, user_should_show_available,
                             send_daily_report)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")

cfg = load_config()
app = Flask(__name__)
app.secret_key = cfg.get("dashboard_password", "change-me") + "-session"

_stop = threading.Event()
_poller_thread = None


def start_poller():
    global _poller_thread
    if _poller_thread and _poller_thread.is_alive():
        return
    _poller_thread = threading.Thread(
        target=run_loop, args=(cfg, _stop), daemon=True, name="poller")
    _poller_thread.start()
    log.info("poller started")


def require_login(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == cfg.get("dashboard_password"):
            session["auth"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        error = "Wrong password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _fmt_est(ts):
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + EST_OFFSET
    return dt.strftime("%Y-%m-%d %H:%M:%S EST")


def _fmt_dur(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


app.jinja_env.filters["est"] = _fmt_est
app.jinja_env.filters["dur"] = _fmt_dur


@app.route("/")
@require_login
def dashboard():
    conn = db.connect(cfg["db_path"])
    db.init_db(conn)
    emp_config = load_employees_config()

    now = time.time()
    days = int(request.args.get("days", 1))
    summary_start = now - days * 24 * 3600
    week_start = now - 7 * 24 * 3600

    summary = db.user_summary(conn, summary_start)
    events = db.recent_events(conn, summary_start, limit=50)

    # Adjust presence display based on display windows (show "Available" during pre-work gaps)
    def adjust_display_state(user_row, emp_data):
        """If user is in display window but not monitoring window, show as Available."""
        if not emp_data:
            return user_row
        if user_should_show_available(emp_data):
            # User is in display window but not monitoring window - show as "Available"
            user_copy = dict(user_row)
            user_copy["current_state"] = "Available"
            user_copy["_display_override"] = True
            return user_copy
        return dict(user_row)

    # Apply display window logic to summary
    summary_with_display = []
    for u in summary:
        u_dict = dict(u) if not isinstance(u, dict) else u
        email = u_dict.get("email", "").lower()
        emp_data = emp_config.get(email)
        adjusted = adjust_display_state(u_dict, emp_data)
        summary_with_display.append(adjusted)
    summary = summary_with_display
    last_poll, last_ok, last_err = db.poll_health(conn)

    total_alerts = sum(r["alert_count"] for r in summary)
    total_inactive = sum(r["total_inactive_seconds"] for r in summary)
    monitored = sum(1 for r in summary if not r["ignored"])
    users_with_data = sum(1 for r in summary if r["current_state"])

    # Currently inactive users (real-time)
    currently_inactive = db.currently_inactive_users(conn)
    currently_inactive_count = len(currently_inactive)

    # Repeat offenders — users who triggered 10+ min threshold most times
    offenders = db.repeat_offenders(conn, summary_start)

    # Frequency chart (bar) — times each user hit 10+ min threshold
    freq_rows = db.inactivity_frequency_chart(conn, summary_start)
    freq_labels = [r["display_name"] or r["email"] or "?" for r in freq_rows]
    freq_values = [r["times_alerted"] for r in freq_rows]

    # Weekly inactive-minutes chart (existing)
    week_summary = db.user_summary(conn, week_start)
    top = [r for r in week_summary if (r["total_inactive_seconds"] or 0) > 0][:10]
    chart_labels = [r["display_name"] or r["email"] or "?" for r in top]
    chart_values = [int((r["total_inactive_seconds"] or 0) / 60) for r in top]

    # Users who had at least one 10+ min alert in the period — for prominent display
    alerted_users = [r for r in summary if (r["alert_count"] or 0) > 0 and not r["ignored"]]

    # Polling status for the banner.
    status = "ok"
    status_msg = "Polls are running."
    if not last_poll:
        status = "wait"
        status_msg = "Waiting for the first poll to complete…"
    elif not last_ok or (last_err and last_err["ts"] > (last_ok["ts"] if last_ok else 0)):
        status = "error"
        status_msg = (last_err["error_text"] if last_err else "Recent poll failed.")
    elif (time.time() - last_poll["ts"]) > 180:
        status = "stale"
        status_msg = f"No poll in {int((time.time() - last_poll['ts']) // 60)} min."

    return render_template(
        "dashboard.html",
        cfg=cfg,
        emp_config=emp_config,
        armed=in_window(cfg),
        now_est_str=now_est().strftime("%Y-%m-%d %H:%M:%S EST"),
        window=f"{cfg['window_start_ist']} – {cfg['window_end_ist']} IST",
        threshold_min=cfg["inactive_threshold_minutes"],
        users=summary,
        alerted_users=alerted_users,
        events=events,
        latest_poll=last_poll,
        last_ok=last_ok,
        last_err=last_err,
        status=status,
        status_msg=status_msg,
        total_alerts=total_alerts,
        total_inactive=total_inactive,
        monitored=monitored,
        users_with_data=users_with_data,
        currently_inactive=currently_inactive,
        currently_inactive_count=currently_inactive_count,
        offenders=offenders,
        freq_labels=freq_labels,
        freq_values=freq_values,
        chart_labels=chart_labels,
        chart_values=chart_values,
        notify_to=cfg["notify_to"],
        days=days,
    )


@app.route("/users/<uid>/ignore", methods=["POST"])
@require_login
def toggle_ignore(uid):
    conn = db.connect(cfg["db_path"])
    row = conn.execute("SELECT ignored FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        abort(404)
    db.set_ignored(conn, uid, not row["ignored"])
    return redirect(url_for("dashboard"))


@app.route("/users/<uid>")
@require_login
def user_detail(uid):
    conn = db.connect(cfg["db_path"])
    db.init_db(conn)
    
    days = int(request.args.get("days", 30))
    detail = db.user_detail(conn, uid, since_ts=time.time() - days * 24 * 3600)
    daily_breakdown = db.user_daily_breakdown(conn, uid, days=days)
    trend_data = db.user_trend_data(conn, uid, days=days)
    
    if not detail:
        abort(404)
    
    user = detail["user"]
    events = detail["events"]
    
    total_inactive = sum(e["duration_seconds"] or 0 for e in events)
    total_alerts = sum(e["alerted"] or 0 for e in events)
    avg_inactive = total_inactive / len(events) if events else 0
    
    return render_template(
        "user_detail.html",
        cfg=cfg,
        user=user,
        events=events,
        daily_breakdown=daily_breakdown,
        trend_data=trend_data,
        days=days,
        total_inactive=total_inactive,
        total_alerts=total_alerts,
        avg_inactive=avg_inactive,
        now_est_str=now_est().strftime("%Y-%m-%d %H:%M:%S EST"),
    )


@app.route("/healthz")
def healthz():
    return jsonify(ok=True, armed=in_window(cfg),
                   now_est=now_est().isoformat())


@app.route("/send-daily-report")
@require_login
def trigger_daily_report():
    """Manually trigger daily report for yesterday."""
    from presence_service import GraphClient

    conn = db.connect(cfg["db_path"])
    db.init_db(conn)
    emp_config = load_employees_config()

    # Get yesterday's date range (midnight to midnight EST)
    now = now_est()
    yesterday = now - timedelta(days=1)
    day_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    day_start_ts = day_start.timestamp()
    day_end_ts = day_end.timestamp()

    try:
        client = GraphClient(cfg["tenant_id"], cfg["client_id"], cfg["client_secret"])
        send_daily_report(client, conn, cfg, day_start_ts, day_end_ts, emp_config)
        return jsonify(ok=True, message=f"Daily report sent for {day_start.strftime('%Y-%m-%d')}")
    except Exception as e:
        log.error("Failed to send daily report: %s", e)
        return jsonify(ok=False, error=str(e)), 500


# Render's gunicorn imports `app`, so kick off the poller on import.
start_poller()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
