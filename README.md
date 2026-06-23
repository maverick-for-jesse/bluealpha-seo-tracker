# Blue Alpha SEO Tracker

SEO rank tracking dashboard for [bluealphabelts.com](https://bluealphabelts.com).

## Features

- 🎯 Track Google rankings for any keyword
- 📈 Daily automated rank checks via cron
- 📊 Historical charts per keyword (Chart.js)
- 📱 Weekly WhatsApp summary every Monday at 9 AM ET
- 🔐 Single admin login
- 🎨 Navy/white Blue Alpha branding

## Stack

- **Backend:** Flask + SQLAlchemy
- **Database:** PostgreSQL (Railway)
- **Frontend:** Bootstrap 5 + Chart.js
- **Rank Data:** SerpAPI (Google Search)
- **WhatsApp:** Twilio
- **Hosting:** Railway

## Setup

### Local Development

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Create config/serpapi.json with your key
mkdir -p config
echo '{"api_key": "YOUR_KEY"}' > config/serpapi.json

python app.py
```

Visit http://localhost:5000 — login with `jesse@bluealpha.us` / `BlueAlphaSEO2026!`

### Railway Deployment

**Required Environment Variables:**

| Variable | Value |
|----------|-------|
| `DATABASE_URL` | Auto-set by Railway Postgres plugin |
| `SERPAPI_KEY` | `8bdfe6a2b68b926f259bee305d0ba254788a2f4184845a347a6743c1a6078cc4` |
| `CRON_SECRET` | Generate a strong random string |
| `SECRET_KEY` | Generate a strong random string |
| `TWILIO_ACCOUNT_SID` | From Twilio console |
| `TWILIO_AUTH_TOKEN` | From Twilio console |
| `TWILIO_WHATSAPP_FROM` | `whatsapp:+14155238886` (Twilio sandbox) or your number |

### Cron Job (Railway)

Add a second Railway service:
- **Image:** `curlimages/curl`
- **Command:** `curl -s "https://your-app.railway.app/cron/check-rankings?secret=$CRON_SECRET"`
- **Schedule:** `0 14 * * *` (10 AM ET daily)

## Endpoints

| Route | Description |
|-------|-------------|
| `GET /` | Dashboard |
| `GET /login` | Login page |
| `GET /keywords/add` | Add keyword form |
| `GET /keywords/<id>/history` | Keyword history chart |
| `POST /keywords/<id>/toggle` | Toggle active/inactive |
| `POST /keywords/<id>/delete` | Delete keyword |
| `POST /api/check-now` | Manual rank check (requires login) |
| `GET /cron/check-rankings?secret=` | Cron rank check |
| `GET /cron/weekly-summary?secret=` | Send WhatsApp summary |

## WhatsApp Setup (Twilio)

1. Create a Twilio account
2. Set up WhatsApp sandbox or a dedicated number
3. Add `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` to Railway env vars
4. Weekly summary auto-sends every Monday when cron runs (detected server-side)

Or hit `/cron/weekly-summary?secret=YOUR_SECRET` manually anytime.
