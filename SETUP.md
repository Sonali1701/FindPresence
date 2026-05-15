# FindPresence

Monitors Microsoft Teams presence for everyone in your tenant. Sends an
email to `ricky@radixsol.com` when anyone is inactive for more than
10 minutes **between 6:30 PM and 3:30 AM IST**. Includes a web dashboard
with per-user analytics.

## Cost

**Nothing extra on Microsoft 365.** Microsoft Graph API, Azure AD app
registration, and the `Presence.Read.All` + `Mail.Send` permissions
are included with any paid M365 plan (Business Basic and up). No Teams
Premium, no Power Automate, no Azure subscription needed.

**Render hosting:**
- Free tier (web service, 512MB, spins down after 15 min idle) — fine if
  you add a free UptimeRobot ping every 5 min to keep it warm during the
  monitoring window.
- Or $7/mo Starter for always-on.

GitHub alone can't host this — Pages is static-only and Actions cron is
poor for stateful pollers. Push the repo to GitHub for source control,
then deploy from there to Render.

## How "inactive" is decided

Active states (the clock resets): `Available`, `Busy`, `DoNotDisturb`,
`InACall`, `InAMeeting`, `Presenting`.

Everything else (Away, BeRightBack, Offline, OutOfOffice) accumulates
inactivity. Teams flips users to `Away` automatically after ~5 min idle,
so a 10-min threshold means "idle ~10 min or longer." One alert per
inactivity streak — when they come back active and idle again, that's
a new streak and a new alert.

## One-time Azure setup

1. <https://entra.microsoft.com> → **App registrations → New registration**.
   Name: `FindPresence`. Single tenant. No redirect URI.
2. Copy **Application (client) ID** and **Directory (tenant) ID**.
3. **Certificates & secrets → New client secret**. Copy the secret *value*.
4. **API permissions → Add → Microsoft Graph → Application permissions**:
   - `Presence.Read.All`
   - `Mail.Send`
   Click **Grant admin consent**.
5. Pick a mailbox to send alerts from (e.g. `alerts@yourdomain.onmicrosoft.com`).
   It needs an Exchange Online license. App-only `Mail.Send` lets the app
   send as that mailbox without its password.

## Run locally

```bash
cp config.example.json config.json
# fill in tenant_id, client_id, client_secret, notify_from, dashboard_password
pip install -r requirements.txt
python app.py
```

Open <http://localhost:5000>, sign in with `dashboard_password`.

## Deploy on Render

1. Push this folder to a GitHub repo.
2. In Render: **New → Blueprint → connect repo**. Render reads
   [render.yaml](render.yaml) and provisions one web service + a 1 GB disk
   so the SQLite DB survives redeploys.
3. After creation, set these secrets in the service's **Environment** tab:
   - `TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET`
   - `NOTIFY_FROM` (the mailbox alerts come from)
   - `DASHBOARD_PASSWORD`
   - `NOTIFY_TO` is already set to `ricky@radixsol.com` in render.yaml.
4. Deploy. Dashboard lives at `https://<your-service>.onrender.com`.

### Keeping the free tier alive

Free Render web services sleep after 15 min idle. Two options:

- **UptimeRobot (free):** add an HTTP monitor for
  `https://<your-service>.onrender.com/healthz` at 5-min interval. Restrict
  the schedule to ~18:00–04:00 IST so polls only happen when needed.
- **Pay $7/mo:** switch the service to Starter plan — always on.

## Ignoring users

Two ways, either works:

- **Dashboard:** click "Ignore" next to any user. Toggle off the same way.
- **ignore.txt:** add emails (one per line). Read every poll, so changes
  apply within ~1 minute. Removing an email from the file does NOT un-ignore;
  use the dashboard for that.

## Files

- `app.py` — Flask app + background poller (one process)
- `presence_service.py` — poll loop, IST window logic, alert sending
- `graph_client.py` — Microsoft Graph wrapper (auth, presence batch, sendMail)
- `db.py` — SQLite schema + queries
- `config_loader.py` — config.json + env-var overrides
- `templates/` — dashboard + login pages
- `render.yaml`, `Procfile` — deploy config
