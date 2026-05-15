"""Load config from config.json, with environment variables overriding."""
import json
import os


ENV_MAP = {
    "tenant_id": "TENANT_ID",
    "client_id": "CLIENT_ID",
    "client_secret": "CLIENT_SECRET",
    "notify_to": "NOTIFY_TO",
    "notify_from": "NOTIFY_FROM",
    "dashboard_password": "DASHBOARD_PASSWORD",
    "db_path": "DB_PATH",
}

DEFAULTS = {
    "inactive_threshold_minutes": 10,
    "poll_interval_seconds": 60,
    "window_start_ist": "18:30",
    "window_end_ist": "03:30",
    "monitor_scope": "all",
    "monitor_users": [],
    "db_path": "findpresence.db",
    "ignore_file": "ignore.txt",
    "notify_to": "ricky@radixsol.com",
    "dashboard_password": "change-me",
}


def load_config(path="config.json"):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    for key, env in ENV_MAP.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg
