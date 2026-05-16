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
from presence_service import run_loop, in_window, now_ist, IST_OFFSET

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


def _fmt_ist(ts):
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + IST_OFFSET
    return dt.strftime("%Y-%m-%d %H:%M:%S IST")


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


app.jinja_env.filters["ist"] = _fmt_ist
app.jinja_env.filters["dur"] = _fmt_dur


@app.route("/")
@require_login
def dashboard():
    conn = db.connect(cfg["db_path"])
    db.init_db(conn)

    now = time.time()
    days = int(request.args.get("days", 1))
    summary_start = now - days * 24 * 3600
    week_start = now - 7 * 24 * 3600

    summary = db.user_summary(conn, summary_start)
    events = db.recent_events(conn, summary_start, limit=100)
    poll = db.latest_poll(conn)

    total_alerts = sum(r["alert_count"] for r in summary)
    total_inactive = sum(r["total_inactive_seconds"] for r in summary)
    monitored = sum(1 for r in summary if not r["ignored"])

    week_summary = db.user_summary(conn, week_start)
    chart_labels = [r["display_name"] or r["email"] or "?" for r in week_summary[:10]]
    chart_values = [int((r["total_inactive_seconds"] or 0) / 60) for r in week_summary[:10]]

    return render_template(
        "dashboard.html",
        cfg=cfg,
        armed=in_window(cfg),
        now_ist_str=now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
        window=f"{cfg['window_start_ist']} – {cfg['window_end_ist']} IST",
        threshold_min=cfg["inactive_threshold_minutes"],
        users=summary,
        events=events,
        latest_poll=poll,
        total_alerts=total_alerts,
        total_inactive=total_inactive,
        monitored=monitored,
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
        now_ist_str=now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
    )


@app.route("/healthz")
def healthz():
    return jsonify(ok=True, armed=in_window(cfg),
                   now_ist=now_ist().isoformat())


# Render's gunicorn imports `app`, so kick off the poller on import.
start_poller()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
