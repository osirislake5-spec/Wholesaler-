# Deploying the daily pull

This has to run on a box you control (VPS, home server, always-on
Replit) — this repo's own remote sessions are ephemeral and get reclaimed,
so nothing scheduled here persists.

## One-time setup on the VPS

```bash
sudo mkdir -p /opt/wholesaler
sudo chown "$USER" /opt/wholesaler
git clone <this-repo-url> /opt/wholesaler
cd /opt/wholesaler

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium   # system libs Chromium needs, Debian/Ubuntu

cp .env.example .env
$EDITOR .env                       # set ANTHROPIC_API_KEY (and SCRAPER_PROXY_URL if you have one)
chmod +x deploy/run.sh
```

Test it manually first: `deploy/run.sh` then check `logs/<today>.log` and
the `survivors_*.csv` / `killed_*.csv` files it writes into the repo root.

## Schedule it — systemd (preferred, most VPS distros)

```bash
sudo cp deploy/satx-pull.service deploy/satx-pull.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now satx-pull.timer
systemctl list-timers satx-pull.timer      # confirm next run time
sudo systemctl start satx-pull.service     # optional: fire it once right now
journalctl -u satx-pull.service -f         # tail logs
```

If you cloned somewhere other than `/opt/wholesaler`, edit
`WorkingDirectory=` and `ExecStart=` in `satx-pull.service` to match.

## Schedule it — cron (no systemd)

```bash
crontab deploy/crontab.txt   # edit the REPO_DIR= path in that file first if not /opt/wholesaler
```

## Getting notified without checking the box yourself

Cheapest option: add a couple lines to the end of `main()` in
`satx_daily_pull.py` to email/text yourself the survivors count (e.g. via
a transactional email API or a Slack webhook) so a day with real deals
doesn't sit unread in a CSV. Say the word and this gets wired in.
