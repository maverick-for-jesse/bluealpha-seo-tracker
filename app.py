import os
import io
import csv
import json
import logging
from datetime import datetime, timedelta, date
from functools import wraps
from zoneinfo import ZoneInfo

import requests
from flask import (
    Flask, render_template, redirect, url_for, request,
    flash, jsonify, abort, Response
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///seo_tracker.db"
).replace("postgres://", "postgresql://")  # Railway compat
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access the SEO tracker."

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "serpapi.json")

def get_serpapi_key():
    key = os.environ.get("SERPAPI_KEY")
    if key:
        return key
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f).get("api_key", "")
    except FileNotFoundError:
        return ""

def get_cron_secret():
    return os.environ.get("CRON_SECRET", "cron-secret-change-me")

def get_twilio_config():
    return {
        "account_sid": os.environ.get("TWILIO_ACCOUNT_SID", ""),
        "auth_token": os.environ.get("TWILIO_AUTH_TOKEN", ""),
        "from_number": os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886"),
        "to_number": "whatsapp:+16789822442",
    }

TARGET_DOMAIN = "bluealphabelts.com"
ADMIN_EMAIL = "jesse@bluealpha.us"
ADMIN_PASSWORD_HASH = generate_password_hash("BlueAlphaSEO2026!")

DATAFORSEO_LOGIN = os.environ.get("DATAFORSEO_LOGIN", "")
DATAFORSEO_PASSWORD = os.environ.get("DATAFORSEO_PASSWORD", "")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
CTR_BY_POSITION = {
    1: 0.314, 2: 0.175, 3: 0.114, 4: 0.079, 5: 0.061,
    6: 0.046, 7: 0.037, 8: 0.030, 9: 0.024, 10: 0.022,
}

def estimate_traffic(position, monthly_volume):
    """Estimate monthly organic clicks based on rank position and search volume."""
    if position is None or monthly_volume is None:
        return None
    ctr = CTR_BY_POSITION.get(position, 0.01 if position <= 20 else 0.005)
    return round(monthly_volume * ctr)


class Keyword(db.Model):
    __tablename__ = "keywords"
    id = db.Column(db.Integer, primary_key=True)
    keyword = db.Column(db.String(500), nullable=False, unique=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    monthly_volume = db.Column(db.Integer, nullable=True)  # stored from DataForSEO, no daily calls
    rankings = db.relationship("Ranking", backref="keyword_ref", lazy="dynamic",
                               cascade="all, delete-orphan")

    def latest_ranking(self):
        return self.rankings.order_by(Ranking.checked_at.desc()).first()

    def ranking_n_days_ago(self, days=7):
        cutoff = datetime.utcnow() - timedelta(days=days)
        return (self.rankings
                .filter(Ranking.checked_at <= datetime.utcnow() - timedelta(days=days - 1))
                .filter(Ranking.checked_at >= cutoff - timedelta(days=2))
                .order_by(Ranking.checked_at.desc())
                .first())


class Ranking(db.Model):
    __tablename__ = "rankings"
    id = db.Column(db.Integer, primary_key=True)
    keyword_id = db.Column(db.Integer, db.ForeignKey("keywords.id"), nullable=False)
    checked_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    position = db.Column(db.Integer, nullable=True)  # null = not in top 100
    url = db.Column(db.String(2000), nullable=True)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class AdminUser(UserMixin):
    id = 1
    email = ADMIN_EMAIL

    def get_id(self):
        return str(self.id)


@login_manager.user_loader
def load_user(user_id):
    if str(user_id) == "1":
        return AdminUser()
    return None


# ---------------------------------------------------------------------------
# SerpAPI helper
# ---------------------------------------------------------------------------
def check_rank_for_keyword(keyword_text: str):
    """Call SerpAPI and return (position, url) or (None, None)."""
    api_key = get_serpapi_key()
    if not api_key:
        raise ValueError("SERPAPI_KEY not configured")

    params = {
        "engine": "google",
        "q": keyword_text,
        "api_key": api_key,
        "gl": "us",
        "hl": "en",
        "num": 100,
    }
    response = requests.get("https://serpapi.com/search", params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    organic = data.get("organic_results", [])
    for i, result in enumerate(organic[:100], start=1):
        link = result.get("link", "")
        if TARGET_DOMAIN in link:
            return i, link

    return None, None


def store_ranking(keyword_id: int, position, url):
    ranking = Ranking(
        keyword_id=keyword_id,
        checked_at=datetime.utcnow(),
        position=position,
        url=url,
    )
    db.session.add(ranking)
    db.session.commit()
    return ranking


def check_all_active_keywords():
    keywords = Keyword.query.filter_by(active=True).all()
    results = []
    for kw in keywords:
        try:
            position, url = check_rank_for_keyword(kw.keyword)
            store_ranking(kw.id, position, url)
            results.append({"keyword": kw.keyword, "position": position, "url": url})
            logger.info(f"Checked '{kw.keyword}': position={position}")
        except Exception as e:
            logger.error(f"Error checking '{kw.keyword}': {e}")
            results.append({"keyword": kw.keyword, "error": str(e)})
    return results


# ---------------------------------------------------------------------------
# WhatsApp (Twilio)
# ---------------------------------------------------------------------------
def send_whatsapp_message(body: str):
    cfg = get_twilio_config()
    if not cfg["account_sid"] or not cfg["auth_token"]:
        logger.warning("Twilio not configured — skipping WhatsApp message")
        return False
    try:
        from twilio.rest import Client
        client = Client(cfg["account_sid"], cfg["auth_token"])
        client.messages.create(
            body=body,
            from_=cfg["from_number"],
            to=cfg["to_number"],
        )
        logger.info("WhatsApp message sent")
        return True
    except Exception as e:
        logger.error(f"WhatsApp send failed: {e}")
        return False


def build_weekly_summary():
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    keywords = Keyword.query.filter_by(active=True).all()

    movers = []
    for kw in keywords:
        current = kw.latest_ranking()
        old = kw.ranking_n_days_ago(7)
        cur_pos = current.position if current else None
        old_pos = old.position if old else None

        change = None
        if cur_pos and old_pos:
            change = old_pos - cur_pos  # positive = improved

        movers.append({
            "keyword": kw.keyword,
            "current": cur_pos,
            "old": old_pos,
            "change": change,
        })

    movers.sort(key=lambda x: (x["change"] is None, -(x["change"] or 0)))

    top_gainers = [m for m in movers if m["change"] and m["change"] > 0][:5]
    top_drops = [m for m in movers if m["change"] and m["change"] < 0][:5]
    entered_top10 = [m for m in movers if m["current"] and m["current"] <= 10 and (not m["old"] or m["old"] > 10)]
    left_top10 = [m for m in movers if m["old"] and m["old"] <= 10 and (not m["current"] or m["current"] > 10)]

    lines = ["📊 *Blue Alpha SEO Weekly Summary*", f"Week ending {date.today().strftime('%B %d, %Y')}", ""]

    if top_gainers:
        lines.append("📈 *Biggest Gains:*")
        for m in top_gainers:
            lines.append(f"  • {m['keyword']}: #{m['old']} → #{m['current']} (+{m['change']})")
        lines.append("")

    if top_drops:
        lines.append("📉 *Biggest Drops:*")
        for m in top_drops:
            lines.append(f"  • {m['keyword']}: #{m['old']} → #{m['current']} ({m['change']})")
        lines.append("")

    if entered_top10:
        lines.append("🎉 *Entered Top 10:*")
        for m in entered_top10:
            lines.append(f"  • {m['keyword']} (now #{m['current']})")
        lines.append("")

    if left_top10:
        lines.append("⚠️ *Left Top 10:*")
        for m in left_top10:
            lines.append(f"  • {m['keyword']} (now #{m['current']})")
        lines.append("")

    if not top_gainers and not top_drops:
        lines.append("Rankings are stable this week. 👍")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Routes — Auth
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if email == ADMIN_EMAIL.lower() and check_password_hash(ADMIN_PASSWORD_HASH, password):
            login_user(AdminUser(), remember=True)
            next_page = request.args.get("next")
            return redirect(next_page or url_for("dashboard"))
        flash("Invalid email or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You've been logged out.", "info")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def dashboard():
    sort = request.args.get("sort", "alphabetical")
    keywords = Keyword.query.filter_by(active=True).all()

    rows = []
    for kw in keywords:
        current = kw.latest_ranking()
        old = kw.ranking_n_days_ago(7)
        cur_pos = current.position if current else None
        old_pos = old.position if old else None
        cur_url = current.url if current else None

        change = None
        if cur_pos is not None and old_pos is not None:
            change = old_pos - cur_pos  # positive = improved (moved up)
        elif cur_pos is not None and old_pos is None:
            change = None  # new entry

        rows.append({
            "id": kw.id,
            "keyword": kw.keyword,
            "current_pos": cur_pos,
            "old_pos": old_pos,
            "change": change,
            "url": cur_url,
            "checked_at": current.checked_at if current else None,
            "monthly_volume": kw.monthly_volume,
            "estimated_traffic": estimate_traffic(cur_pos, kw.monthly_volume),
        })

    if sort == "best_rank":
        rows.sort(key=lambda r: (r["current_pos"] is None, r["current_pos"] or 999))
    elif sort == "biggest_movers":
        rows.sort(key=lambda r: (r["change"] is None, -(abs(r["change"]) if r["change"] else 0)))
    else:  # alphabetical
        rows.sort(key=lambda r: r["keyword"].lower())

    all_keywords = Keyword.query.order_by(Keyword.keyword).all()
    return render_template("dashboard.html", rows=rows, sort=sort, all_keywords=all_keywords)


# ---------------------------------------------------------------------------
# Routes — Keywords management
# ---------------------------------------------------------------------------
@app.route("/keywords/add", methods=["GET", "POST"])
@login_required
def add_keyword():
    result = None
    if request.method == "POST":
        kw_text = request.form.get("keyword", "").strip()
        if not kw_text:
            flash("Please enter a keyword.", "warning")
        elif Keyword.query.filter(func.lower(Keyword.keyword) == kw_text.lower()).first():
            flash(f'Keyword "{kw_text}" already exists.', "warning")
        else:
            kw = Keyword(keyword=kw_text)
            db.session.add(kw)
            db.session.commit()
            # Check rank immediately
            try:
                position, url = check_rank_for_keyword(kw_text)
                store_ranking(kw.id, position, url)
                result = {"keyword": kw_text, "position": position, "url": url}
            except Exception as e:
                flash(f"Keyword added, but rank check failed: {e}", "warning")
                result = {"keyword": kw_text, "error": str(e)}
    return render_template("add_keyword.html", result=result)


@app.route("/keywords/<int:keyword_id>/toggle", methods=["POST"])
@login_required
def toggle_keyword(keyword_id):
    kw = Keyword.query.get_or_404(keyword_id)
    kw.active = not kw.active
    db.session.commit()
    status = "activated" if kw.active else "deactivated"
    flash(f'Keyword "{kw.keyword}" {status}.', "success")
    return redirect(url_for("dashboard"))


@app.route("/keywords/<int:keyword_id>/delete", methods=["POST"])
@login_required
def delete_keyword(keyword_id):
    kw = Keyword.query.get_or_404(keyword_id)
    name = kw.keyword
    db.session.delete(kw)
    db.session.commit()
    flash(f'Keyword "{name}" deleted.', "success")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Routes — History
# ---------------------------------------------------------------------------
@app.route("/keywords/<int:keyword_id>/history")
@login_required
def keyword_history(keyword_id):
    kw = Keyword.query.get_or_404(keyword_id)
    rankings = (kw.rankings
                .order_by(Ranking.checked_at.desc())
                .all())

    # Chart data — chronological
    chart_labels = []
    chart_data = []
    for r in reversed(rankings):
        chart_labels.append(r.checked_at.strftime("%b %d"))
        chart_data.append(r.position if r.position else "null")

    return render_template(
        "keyword_history.html",
        kw=kw,
        rankings=rankings,
        chart_labels=json.dumps(chart_labels),
        chart_data=json.dumps([p if p != "null" else None for p in chart_data]),
    )


# ---------------------------------------------------------------------------
# Routes — Cron / API
# ---------------------------------------------------------------------------
@app.route("/cron/check-rankings")
def cron_check_rankings():
    secret = request.args.get("secret", "")
    if secret != get_cron_secret():
        abort(403)
    results = check_all_active_keywords()
    # Check if it's Monday for weekly summary (UTC ~14:00 = 9-10 AM ET)
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() == 0:  # Monday
        try:
            summary = build_weekly_summary()
            send_whatsapp_message(summary)
        except Exception as e:
            logger.error(f"Weekly summary failed: {e}")
    return jsonify({"status": "ok", "checked": len(results), "results": results})


@app.route("/cron/weekly-summary")
def cron_weekly_summary():
    secret = request.args.get("secret", "")
    if secret != get_cron_secret():
        abort(403)
    summary = build_weekly_summary()
    sent = send_whatsapp_message(summary)
    return jsonify({"status": "ok", "sent": sent, "summary": summary})


@app.route("/api/check-now", methods=["POST"])
@login_required
def api_check_now():
    results = check_all_active_keywords()
    return jsonify({"status": "ok", "results": results})


# ---------------------------------------------------------------------------
# DataForSEO — Keyword Research
# ---------------------------------------------------------------------------

def dataforseo_keyword_data(keywords: list, location_code: int = 2840, language_code: str = "en"):
    """
    Fetch search volume + competition + CPC for a list of keywords via DataForSEO.
    Returns list of dicts keyed by keyword.
    """
    url = "https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live"
    payload = [{"keywords": keywords, "location_code": location_code, "language_code": language_code}]
    resp = requests.post(
        url,
        auth=(DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    results = {}
    tasks = data.get("tasks", [])
    for task in tasks:
        if task.get("status_code") != 20000:
            raise ValueError(f"DataForSEO error: {task.get('status_message', 'Unknown error')}")
        for item in (task.get("result") or []):
            kw = item.get("keyword", "")
            comp = item.get("competition")
            # DataForSEO returns competition as float (0-1), string float, or label ('LOW','MEDIUM','HIGH')
            comp_map = {"LOW": 0.15, "MEDIUM": 0.5, "HIGH": 0.85}
            if comp is None:
                comp_float = None
            elif isinstance(comp, str):
                comp_float = comp_map.get(comp.upper(), None) if comp.upper() in comp_map else (float(comp) if comp else None)
            else:
                comp_float = float(comp)
            results[kw.lower()] = {
                "keyword": kw,
                "search_volume": item.get("search_volume"),
                "competition": comp_float,
                "cpc": float(item["cpc"]) if item.get("cpc") is not None else None,
                "trend": [m.get("search_volume") for m in (item.get("monthly_searches") or [])[-3:]],
            }
    return results


def dataforseo_keyword_difficulty(keywords: list, location_code: int = 2840, language_code: str = "en"):
    """
    Fetch SEO keyword difficulty (0-100) via DataForSEO Labs.
    Higher = harder to rank organically.
    """
    url = "https://api.dataforseo.com/v3/dataforseo_labs/google/keyword_difficulty/live"
    payload = [{"keywords": keywords, "location_code": location_code, "language_code": language_code}]
    resp = requests.post(
        url,
        auth=(DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    results = {}
    for task in data.get("tasks", []):
        for item in (task.get("result") or []):
            kw = item.get("keyword", "")
            results[kw.lower()] = item.get("keyword_difficulty")
    return results


def dataforseo_related_keywords(seed: str, location_code: int = 2840, language_code: str = "en"):
    """
    Fetch keyword suggestions/ideas for a seed via DataForSEO keyword_ideas endpoint.
    Returns list of keyword strings (top 20 by volume).
    """
    url = "https://api.dataforseo.com/v3/keywords_data/google_ads/keywords_for_keywords/live"
    payload = [{"keywords": [seed], "location_code": location_code, "language_code": language_code}]
    resp = requests.post(
        url,
        auth=(DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    keywords = []
    for task in data.get("tasks", []):
        for item in (task.get("result") or []):
            kw = item.get("keyword", "")
            vol = item.get("search_volume") or 0
            if kw and kw.lower() != seed.lower():
                keywords.append((kw, vol))

    keywords.sort(key=lambda x: x[1], reverse=True)
    return [k for k, _ in keywords[:30]]


@app.route("/keyword-research", methods=["GET", "POST"])
@login_required
def keyword_research():
    results = None
    suggestions = []
    query = None
    error = None
    location_code = request.form.get("location_code", "2840")

    if request.method == "POST":
        query = request.form.get("keyword", "").strip()
        if not query:
            flash("Please enter a keyword.", "warning")
            return render_template("keyword_research.html", results=None, query=None,
                                   location_code=location_code)
        try:
            loc = int(location_code)
            # Build a list: seed + related suggestions
            related = dataforseo_related_keywords(query, location_code=loc)
            all_keywords = list({query} | set(related[:29]))  # max 30 total

            # Get volume data and SEO difficulty for all
            volume_data = dataforseo_keyword_data(all_keywords, location_code=loc)
            try:
                difficulty_data = dataforseo_keyword_difficulty(all_keywords, location_code=loc)
            except Exception as e:
                logger.warning(f"Difficulty fetch failed: {e}")
                difficulty_data = {}

            # Get currently tracked keywords for badge
            tracked_set = {kw.keyword.lower() for kw in Keyword.query.all()}

            results = []
            for kw_text in all_keywords:
                d = volume_data.get(kw_text.lower(), {})
                results.append({
                    "keyword": d.get("keyword", kw_text),
                    "search_volume": d.get("search_volume"),
                    "competition": d.get("competition"),
                    "cpc": d.get("cpc"),
                    "trend": d.get("trend", []),
                    "difficulty": difficulty_data.get(kw_text.lower()),
                    "already_tracked": kw_text.lower() in tracked_set,
                })

            # Sort: seed first, then by volume desc
            results.sort(key=lambda r: (
                r["keyword"].lower() != query.lower(),
                -(r["search_volume"] or 0)
            ))

            # Suggestions = related keywords not in main results (chips)
            suggestions = [r["keyword"] for r in results[1:15]]

        except Exception as e:
            logger.error(f"DataForSEO error: {e}")
            error = f"Keyword research failed: {e}"

    return render_template(
        "keyword_research.html",
        results=results,
        query=query,
        suggestions=suggestions,
        error=error,
        location_code=location_code,
    )


@app.route("/keyword-research/add-tracked", methods=["POST"])
@login_required
def keyword_research_add_tracked():
    kw_text = request.form.get("keyword", "").strip()
    return_query = request.form.get("return_query", "")
    return_location = request.form.get("return_location", "2840")
    stored_volume = request.form.get("volume")

    if kw_text:
        existing = Keyword.query.filter(func.lower(Keyword.keyword) == kw_text.lower()).first()
        if existing:
            flash(f'"{kw_text}" is already being tracked.', "info")
        else:
            volume = int(stored_volume) if stored_volume and stored_volume.isdigit() else None
            kw = Keyword(keyword=kw_text, monthly_volume=volume)
            db.session.add(kw)
            db.session.commit()
            # Immediately check rank
            try:
                position, url = check_rank_for_keyword(kw_text)
                store_ranking(kw.id, position, url)
                pos_str = f"#{position}" if position else "not in top 100"
                flash(f'✅ "{kw_text}" added to rank tracker — currently {pos_str}.', "success")
            except Exception as e:
                flash(f'"{kw_text}" added to tracker (rank check failed: {e}).', "warning")

    # Redirect back to results
    if return_query:
        return redirect(url_for("keyword_research") + f"?_restore=1")
    return redirect(url_for("keyword_research"))


@app.route("/keyword-research/export")
@login_required
def keyword_research_export():
    query = request.args.get("query", "").strip()
    location_code = int(request.args.get("location_code", "2840"))
    if not query:
        return redirect(url_for("keyword_research"))

    try:
        related = dataforseo_related_keywords(query, location_code=location_code)
        all_keywords = list({query} | set(related[:29]))
        volume_data = dataforseo_keyword_data(all_keywords, location_code=location_code)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Keyword", "Monthly Search Volume", "Competition (0-1)", "CPC ($)"])
        for kw_text in all_keywords:
            d = volume_data.get(kw_text.lower(), {})
            writer.writerow([
                d.get("keyword", kw_text),
                d.get("search_volume", ""),
                d.get("competition", ""),
                d.get("cpc", ""),
            ])

        output.seek(0)
        filename = f"keyword-research-{query.replace(' ', '-')}.csv"
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        flash(f"Export failed: {e}", "danger")
        return redirect(url_for("keyword_research"))


# ---------------------------------------------------------------------------
# Init DB + run
# ---------------------------------------------------------------------------
@app.context_processor
def inject_now():
    return {"now": datetime.utcnow()}


with app.app_context():
    db.create_all()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
