# Telegram Traffic CRM

FastAPI + SQLAlchemy + SQLite mini-CRM for Telegram Mini Apps.

## Features

- Telegram Web App `initData` validation on the server.
- Fallback admin login for opening the CRM in a normal browser.
- Offers: name, GEO, payout and currency.
- Tracking links with unique slugs.
- Click tracking: IP, User-Agent, Referer, UTM parameters and Telegram start parameter.
- Daily click chart.
- Responsive dark UI for phone/Telegram.
- SQLite database created automatically.
- `/health` endpoint for hosting health checks.

## Structure

```text
telegram-traffic-crm/
├── main.py
├── database.py
├── requirements.txt
├── render.yaml
├── .python-version
├── .env.example
├── README.md
└── templates/
    ├── login.html
    └── dashboard.html
```

## Local run

```bash
python -m venv .venv
```

Windows:
```bash
.venv\Scripts\activate
```

macOS/Linux:
```bash
source .venv/bin/activate
```

Install:
```bash
pip install -r requirements.txt
```

Set environment variables:

```text
BOT_TOKEN=your_bot_token
APP_SECRET=some-long-random-secret
ADMIN_USERNAME=admin
ADMIN_PASSWORD=strong-password
COOKIE_SECURE=false
```

Run:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://127.0.0.1:8000`.

## Telegram Mini App setup

1. Create a bot in BotFather.
2. Put its bot token into `BOT_TOKEN`.
3. Deploy this project to a public HTTPS host.
4. Configure the bot's Mini App/Web App URL to the deployed HTTPS URL.
5. Open the app from Telegram.

The dashboard sends `Telegram.WebApp.initData` to `/api/telegram-auth`.
The backend validates the HMAC before trusting the Telegram user.

## Tracking

If a link has slug `binance-ua`, use:

```text
https://YOUR-DOMAIN/t/binance-ua
```

Examples:

```text
https://YOUR-DOMAIN/t/binance-ua?utm_source=telegram&utm_campaign=test
```

The tracker records the click and redirects to the destination URL while preserving query parameters.

## Important SQLite hosting note

SQLite is excellent for a small single-instance CRM. For serious production traffic or multiple instances, move the database to PostgreSQL.

On hosts where the filesystem is ephemeral, the SQLite file can be lost on redeploy/restart. If persistent storage is not guaranteed, use PostgreSQL.

## Security

Change `ADMIN_PASSWORD` and `APP_SECRET`.
Use HTTPS and `COOKIE_SECURE=true` in production.
Do not commit `.env` or secrets to GitHub.
Do not expose the bot token in frontend JavaScript.
