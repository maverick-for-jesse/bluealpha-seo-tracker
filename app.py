import os
import io
import csv
import json
import re
import logging
from datetime import datetime, timedelta, date
from functools import wraps
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
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
    tags = db.Column(db.String(500), nullable=True)  # comma-separated: "belts,tactical"
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


class Competitor(db.Model):
    __tablename__ = "competitors"
    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(500), nullable=False, unique=True)
    nickname = db.Column(db.String(200), nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    rankings = db.relationship("CompetitorRanking", backref="competitor_ref", lazy="dynamic",
                               cascade="all, delete-orphan")


class CompetitorRanking(db.Model):
    __tablename__ = "competitor_rankings"
    id = db.Column(db.Integer, primary_key=True)
    keyword_id = db.Column(db.Integer, db.ForeignKey("keywords.id", ondelete="CASCADE"), nullable=False)
    competitor_id = db.Column(db.Integer, db.ForeignKey("competitors.id", ondelete="CASCADE"), nullable=False)
    checked_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    position = db.Column(db.Integer, nullable=True)
    url = db.Column(db.String(2000), nullable=True)
    keyword_ref = db.relationship("Keyword", backref=db.backref("competitor_rankings", lazy="dynamic",
                                  cascade="all, delete-orphan"))


class KeywordQuestion(db.Model):
    __tablename__ = "keyword_questions"
    id = db.Column(db.Integer, primary_key=True)
    keyword_id = db.Column(db.Integer, db.ForeignKey("keywords.id"), nullable=False)
    question = db.Column(db.String(1000), nullable=False)
    snippet = db.Column(db.Text, nullable=True)   # Google's answer snippet
    source_url = db.Column(db.String(2000), nullable=True)
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    keyword_ref = db.relationship("Keyword", backref=db.backref("questions", lazy="dynamic",
                                  cascade="all, delete-orphan"))


class ContentIdea(db.Model):
    __tablename__ = "content_ideas"
    id = db.Column(db.Integer, primary_key=True)
    keyword_id = db.Column(db.Integer, db.ForeignKey("keywords.id"), nullable=False)
    title = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(50), default="idea", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    keyword_ref = db.relationship("Keyword", backref=db.backref("content_ideas", lazy="dynamic"))


class SearchHistory(db.Model):
    __tablename__ = "search_history"
    id = db.Column(db.Integer, primary_key=True)
    search_query = db.Column("query", db.String(500), nullable=False)
    location_code = db.Column(db.String(10), default="2840")
    searched_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class SeoChecklist(db.Model):
    __tablename__ = "seo_checklist"
    id = db.Column(db.Integer, primary_key=True)
    keyword_id = db.Column(db.Integer, db.ForeignKey("keywords.id"), nullable=False)
    item_key = db.Column(db.String(100), nullable=False)   # e.g. "title_tag"
    completed = db.Column(db.Boolean, default=False, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default="manual", nullable=False)  # pass/fail/warn/manual
    detail = db.Column(db.String(500), nullable=True)  # e.g. "Title is 45 chars"
    keyword_ref = db.relationship("Keyword", backref=db.backref("checklist_items", lazy="dynamic",
                                  cascade="all, delete-orphan"))


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


def check_rank_for_domain(keyword_text: str, domain: str):
    """Call SerpAPI and return (position, url) for a given domain, or (None, None)."""
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
        if domain in link:
            return i, link

    return None, None


def fetch_people_also_ask(keyword_text: str):
    """
    Fetch People Also Ask questions from SerpAPI for a keyword.
    Returns list of dicts: {question, snippet, source_url}
    """
    api_key = get_serpapi_key()
    if not api_key:
        return []
    params = {
        "engine": "google",
        "q": keyword_text,
        "api_key": api_key,
        "gl": "us",
        "hl": "en",
        "num": 10,
    }
    response = requests.get("https://serpapi.com/search", params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    questions = []
    for item in data.get("related_questions", []):
        questions.append({
            "question": item.get("question", ""),
            "snippet": item.get("snippet", ""),
            "source_url": item.get("link", ""),
        })
    return questions


def store_questions(keyword_id: int, questions: list):
    """Store PAA questions for a keyword, replacing any previous set."""
    KeywordQuestion.query.filter_by(keyword_id=keyword_id).delete()
    for q in questions:
        kq = KeywordQuestion(
            keyword_id=keyword_id,
            question=q["question"],
            snippet=q.get("snippet"),
            source_url=q.get("source_url"),
        )
        db.session.add(kq)
    db.session.commit()


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
    competitors = Competitor.query.filter_by(active=True).all()
    results = []
    alert_lines = []

    for kw in keywords:
        try:
            # Get previous position before storing new one
            prev_ranking = kw.latest_ranking()
            prev_pos = prev_ranking.position if prev_ranking else None

            position, url = check_rank_for_keyword(kw.keyword)
            store_ranking(kw.id, position, url)
            results.append({"keyword": kw.keyword, "position": position, "url": url})
            logger.info(f"Checked '{kw.keyword}': position={position}")

            # --- Ranking Alerts ---
            kw_name = kw.keyword
            if prev_pos is not None and position is not None:
                drop = position - prev_pos
                if drop >= 5:
                    alert_lines.append(f"📉 '{kw_name}' dropped {drop} spots: #{prev_pos} → #{position}")
            if position == 1:
                alert_lines.append(f"🏆 '{kw_name}' hit #1!")
            elif position is not None and position <= 10 and (prev_pos is None or prev_pos > 10):
                alert_lines.append(f"🎉 '{kw_name}' entered Top 10: now #{position}")
            elif (prev_pos is not None and prev_pos <= 10) and (position is None or position > 10):
                pos_str = f"#{position}" if position else "unranked"
                alert_lines.append(f"⚠️ '{kw_name}' left Top 10: now {pos_str}")

        except Exception as e:
            logger.error(f"Error checking '{kw.keyword}': {e}")
            results.append({"keyword": kw.keyword, "error": str(e)})

        # --- Competitor Rankings ---
        for comp in competitors:
            try:
                comp_position, comp_url = check_rank_for_domain(kw.keyword, comp.domain)
                cr = CompetitorRanking(
                    keyword_id=kw.id,
                    competitor_id=comp.id,
                    checked_at=datetime.utcnow(),
                    position=comp_position,
                    url=comp_url,
                )
                db.session.add(cr)
                db.session.commit()
            except Exception as e:
                logger.error(f"Error checking competitor '{comp.domain}' for '{kw.keyword}': {e}")

    # Send WhatsApp alerts if any
    if alert_lines:
        try:
            body = "🚨 SEO Alert — Blue Alpha\n\n" + "\n".join(alert_lines)
            send_whatsapp_message(body)
        except Exception as e:
            logger.error(f"Alert WhatsApp send failed: {e}")

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
    tag_filter = request.args.get("tag", "").strip().lower()
    keywords = Keyword.query.filter_by(active=True).all()

    # Collect all unique tags
    all_tags = set()
    for kw in keywords:
        if kw.tags:
            for t in kw.tags.split(","):
                t = t.strip()
                if t:
                    all_tags.add(t)
    all_tags = sorted(all_tags)

    rows = []
    for kw in keywords:
        # Tag filter
        kw_tags = [t.strip() for t in kw.tags.split(",")] if kw.tags else []
        if tag_filter and tag_filter not in kw_tags:
            continue

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
            "tags": kw_tags,
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
    return render_template("dashboard.html", rows=rows, sort=sort, all_keywords=all_keywords,
                           all_tags=all_tags, tag_filter=tag_filter)


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
            # Fetch search volume from DataForSEO
            try:
                vol_data = dataforseo_keyword_data([kw_text])
                d = vol_data.get(kw_text.lower(), {})
                vol = d.get("search_volume")
                comp = d.get("competition")
                cpc = d.get("cpc")
                if vol is not None:
                    kw.monthly_volume = vol
                    db.session.commit()
                if result:
                    result["monthly_volume"] = vol
                    result["competition"] = comp
                    result["cpc"] = cpc
                    result["difficulty"] = estimate_seo_difficulty(vol, comp, cpc)
                    result["traffic_estimates"] = {
                        pos: estimate_traffic(pos, vol)
                        for pos in [1, 3, 5, 10]
                    } if vol else None
            except Exception as e:
                logger.warning(f"Volume fetch failed for {kw_text}: {e}")
    return render_template("add_keyword.html", result=result)


@app.route("/keywords/bulk-add", methods=["GET", "POST"])
@login_required
def bulk_add_keywords():
    results = []
    if request.method == "POST":
        raw = request.form.get("keywords", "").strip()
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines:
            flash("Please enter at least one keyword.", "warning")
            return render_template("bulk_add_keywords.html", results=None)

        added = []
        skipped = []
        for kw_text in lines:
            if Keyword.query.filter(func.lower(Keyword.keyword) == kw_text.lower()).first():
                skipped.append(kw_text)
                continue
            kw = Keyword(keyword=kw_text)
            db.session.add(kw)
            added.append(kw_text)
        db.session.commit()

        # Fetch volumes for all new keywords in one DataForSEO call
        if added:
            try:
                vol_data = dataforseo_keyword_data(added)
                for kw_text in added:
                    d = vol_data.get(kw_text.lower(), {})
                    vol = d.get("search_volume")
                    comp = d.get("competition")
                    cpc_val = d.get("cpc")
                    kw_obj = Keyword.query.filter(func.lower(Keyword.keyword) == kw_text.lower()).first()
                    if kw_obj and vol is not None:
                        kw_obj.monthly_volume = vol
                    results.append({
                        "keyword": kw_text,
                        "monthly_volume": vol,
                        "difficulty": estimate_seo_difficulty(vol, comp, cpc_val),
                        "traffic_1": estimate_traffic(1, vol) if vol else None,
                        "traffic_5": estimate_traffic(5, vol) if vol else None,
                    })
                db.session.commit()
            except Exception as e:
                logger.warning(f"Bulk volume fetch failed: {e}")
                for kw_text in added:
                    results.append({"keyword": kw_text, "monthly_volume": None,
                                    "difficulty": None, "traffic_1": None, "traffic_5": None})

        # Now check ranks for all added keywords
        for r in results:
            try:
                position, url = check_rank_for_keyword(r["keyword"])
                kw_obj = Keyword.query.filter(func.lower(Keyword.keyword) == r["keyword"].lower()).first()
                if kw_obj:
                    store_ranking(kw_obj.id, position, url)
                r["position"] = position
            except Exception as e:
                r["position"] = None
                r["rank_error"] = str(e)

        if skipped:
            flash(f'{len(skipped)} keyword(s) already existed and were skipped.', "info")
        flash(f'✅ {len(added)} keyword(s) added and ranked.', "success")

    return render_template("bulk_add_keywords.html", results=results)


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

    stored_questions = kw.questions.order_by(KeywordQuestion.fetched_at.desc()).all()
    checklist_items = get_or_create_checklist(keyword_id)
    checklist_map = {item.item_key: item for item in checklist_items}
    completed_count = sum(1 for i in checklist_items if i.status == "pass")
    auto_detectable = {"title_tag","title_length","meta_desc","url_slug",
                       "h1_keyword","first_100_words","image_alt","internal_links"}

    # Traffic estimates
    latest = kw.latest_ranking()
    current_pos = latest.position if latest else None
    vol = kw.monthly_volume
    current_traffic = estimate_traffic(current_pos, vol)

    traffic_scenarios = []
    if vol:
        for pos, label in [(1,"#1"), (3,"#3"), (5,"#5"), (10,"#10")]:
            t = estimate_traffic(pos, vol)
            traffic_scenarios.append({
                "pos": pos, "label": label,
                "traffic": t,
                "is_current": current_pos == pos,
                "better_than_current": current_pos is None or pos < current_pos,
            })

    # Historical traffic trend (last 30 rankings)
    traffic_history = []
    for r in reversed(rankings[:30]):
        traffic_history.append({
            "date": r.checked_at.strftime("%b %d"),
            "position": r.position,
            "traffic": estimate_traffic(r.position, vol) if vol else None,
        })

    return render_template(
        "keyword_history.html",
        kw=kw,
        rankings=rankings,
        chart_labels=json.dumps(chart_labels),
        chart_data=json.dumps([p if p != "null" else None for p in chart_data]),
        questions=stored_questions,
        checklist_items=SEO_CHECKLIST_ITEMS,
        checklist_map=checklist_map,
        completed_count=completed_count,
        total_items=len(SEO_CHECKLIST_ITEMS),
        auto_detectable=auto_detectable,
        current_traffic=current_traffic,
        current_pos=current_pos,
        monthly_volume=vol,
        traffic_scenarios=traffic_scenarios,
        traffic_history=traffic_history,
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
# Routes — Keyword Tags
# ---------------------------------------------------------------------------
@app.route("/keywords/<int:keyword_id>/tags", methods=["POST"])
@login_required
def update_keyword_tags(keyword_id):
    kw = Keyword.query.get_or_404(keyword_id)
    raw = request.form.get("tags", "").strip()
    # Normalize: strip spaces around commas, lowercase
    tags_list = [t.strip().lower() for t in raw.split(",") if t.strip()]
    kw.tags = ",".join(tags_list) if tags_list else None
    db.session.commit()
    flash(f'Tags updated for "{kw.keyword}".', "success")
    return redirect(url_for("keyword_history", keyword_id=keyword_id))


# ---------------------------------------------------------------------------
# Routes — Competitors
# ---------------------------------------------------------------------------
@app.route("/competitors", methods=["GET", "POST"])
@login_required
def competitors():
    if request.method == "POST":
        domain = request.form.get("domain", "").strip().lower()
        nickname = request.form.get("nickname", "").strip()
        if not domain or not nickname:
            flash("Both domain and nickname are required.", "warning")
        elif Competitor.query.filter_by(domain=domain).first():
            flash(f'Competitor "{domain}" already exists.', "warning")
        else:
            comp = Competitor(domain=domain, nickname=nickname)
            db.session.add(comp)
            db.session.commit()
            flash(f'Competitor "{nickname}" ({domain}) added.', "success")
        return redirect(url_for("competitors"))

    all_competitors = Competitor.query.order_by(Competitor.added_at.desc()).all()
    keywords = Keyword.query.filter_by(active=True).order_by(Keyword.keyword).all()

    # Build table: for each keyword, get latest our rank + latest competitor ranks
    rows = []
    for kw in keywords:
        latest_ours = kw.latest_ranking()
        our_pos = latest_ours.position if latest_ours else None
        comp_data = {}
        for comp in all_competitors:
            latest_cr = (CompetitorRanking.query
                         .filter_by(keyword_id=kw.id, competitor_id=comp.id)
                         .order_by(CompetitorRanking.checked_at.desc())
                         .first())
            comp_data[comp.id] = latest_cr.position if latest_cr else None
        rows.append({
            "keyword_id": kw.id,
            "keyword": kw.keyword,
            "our_pos": our_pos,
            "comp_positions": comp_data,
        })

    return render_template("competitors.html",
                           competitors=all_competitors,
                           rows=rows)


@app.route("/competitors/<int:competitor_id>/delete", methods=["POST"])
@login_required
def delete_competitor(competitor_id):
    comp = Competitor.query.get_or_404(competitor_id)
    name = comp.nickname
    db.session.delete(comp)
    db.session.commit()
    flash(f'Competitor "{name}" deleted.', "success")
    return redirect(url_for("competitors"))


# ---------------------------------------------------------------------------
# Routes — Cannibalization Detector
# ---------------------------------------------------------------------------
@app.route("/cannibalization")
@login_required
def cannibalization():
    keywords = Keyword.query.filter_by(active=True).all()

    all_rows = []
    url_groups = {}  # url -> list of {keyword, position}

    for kw in keywords:
        latest = kw.latest_ranking()
        pos = latest.position if latest else None
        url = latest.url if latest else None
        all_rows.append({
            "keyword": kw.keyword,
            "keyword_id": kw.id,
            "position": pos,
            "url": url,
        })
        if url:
            if url not in url_groups:
                url_groups[url] = []
            url_groups[url].append({"keyword": kw.keyword, "keyword_id": kw.id, "position": pos})

    # Find cannibalization conflicts: same URL, 2+ keywords
    conflicts = {url: kws for url, kws in url_groups.items() if len(kws) >= 2}

    return render_template("cannibalization.html",
                           conflicts=conflicts,
                           all_rows=all_rows)


# ---------------------------------------------------------------------------
# Content Ideas — People Also Ask
# ---------------------------------------------------------------------------

@app.route("/keywords/<int:keyword_id>/checklist-refresh", methods=["POST"])
@login_required
def checklist_refresh(keyword_id):
    """Run the page grader on the keyword's ranking URL and auto-check items."""
    kw = Keyword.query.get_or_404(keyword_id)
    latest = kw.latest_ranking()

    if not latest or not latest.url:
        flash("⚠️ No ranking URL found yet — check rankings first.", "warning")
        return redirect(url_for("keyword_history", keyword_id=keyword_id))

    try:
        analysis = analyze_page_seo(latest.url, kw.keyword)
    except Exception as e:
        flash(f"Failed to analyze page: {e}", "danger")
        return redirect(url_for("keyword_history", keyword_id=keyword_id))

    checks_by_name = {c["name"]: c for c in analysis["checks"]}
    kw_lower = kw.keyword.lower()

    # Ensure all checklist items exist
    get_or_create_checklist(keyword_id)

    def set_item(item_key, status, detail=""):
        item = SeoChecklist.query.filter_by(keyword_id=keyword_id, item_key=item_key).first()
        if item:
            item.status = status
            item.completed = (status == "pass")
            item.completed_at = datetime.utcnow() if status == "pass" else None
            item.detail = detail

    def grader_status(check_name):
        c = checks_by_name.get(check_name, {})
        return c.get("status", "fail"), c.get("message", "")

    # Title tag contains keyword
    s, msg = grader_status("Keyword in Title")
    set_item("title_tag", s, msg)

    # Title length
    s, msg = grader_status("Title Tag Length")
    set_item("title_length", s, msg)

    # Meta description written & includes keyword
    s1, msg1 = grader_status("Meta Description")
    s2, msg2 = grader_status("Keyword in Meta Description")
    if s1 == "fail":
        set_item("meta_desc", "fail", msg1)
    elif s2 == "fail":
        set_item("meta_desc", "warn", "Meta description exists but doesn't include the keyword.")
    else:
        set_item("meta_desc", "pass", msg1)

    # URL slug
    url_lower = latest.url.lower()
    kw_slug = kw_lower.replace(" ", "-")
    if kw_slug in url_lower or kw_lower.replace(" ", "") in url_lower:
        set_item("url_slug", "pass", f"Keyword found in URL: {latest.url}")
    else:
        set_item("url_slug", "fail", f"Keyword not found in URL: {latest.url}")

    # H1
    s, msg = grader_status("Keyword in H1")
    set_item("h1_keyword", s, msg)

    # First 100 words (use density as proxy)
    s, msg = grader_status("Keyword Density")
    if s == "fail":
        set_item("first_100_words", "warn", "Keyword density is very low — may not appear early in content.")
    else:
        set_item("first_100_words", "pass", msg)

    # Image alt text
    s, msg = grader_status("Image Alt Text")
    set_item("image_alt", s, msg)

    # Internal links
    s, msg = grader_status("Internal Links")
    set_item("internal_links", s, msg)

    db.session.commit()

    passed = sum(1 for k in ["title_tag","title_length","meta_desc","url_slug",
                              "h1_keyword","first_100_words","image_alt","internal_links"]
                 if SeoChecklist.query.filter_by(keyword_id=keyword_id, item_key=k, status="pass").first())

    flash(f"✅ Refreshed from page grader — {passed}/8 auto-checks passed. "
          f"Overall page score: {analysis['score']}/100.", "success")
    return redirect(url_for("keyword_history", keyword_id=keyword_id))


@app.route("/keywords/<int:keyword_id>/checklist-toggle", methods=["POST"])
@login_required
def checklist_toggle(keyword_id):
    item_key = request.form.get("item_key")
    if not item_key:
        return jsonify({"error": "missing item_key"}), 400
    item = SeoChecklist.query.filter_by(keyword_id=keyword_id, item_key=item_key).first()
    if not item:
        return jsonify({"error": "not found"}), 404
    item.completed = not item.completed
    item.completed_at = datetime.utcnow() if item.completed else None
    db.session.commit()
    total = SeoChecklist.query.filter_by(keyword_id=keyword_id).count()
    done = SeoChecklist.query.filter_by(keyword_id=keyword_id, completed=True).count()
    return jsonify({"completed": item.completed, "done": done, "total": total})


@app.route("/keywords/<int:keyword_id>/fetch-questions", methods=["POST"])
@login_required
def fetch_keyword_questions(keyword_id):
    kw = Keyword.query.get_or_404(keyword_id)
    try:
        questions = fetch_people_also_ask(kw.keyword)
        store_questions(keyword_id, questions)
        flash(f"✅ Found {len(questions)} questions for \"{kw.keyword}\".", "success")
    except Exception as e:
        flash(f"Failed to fetch questions: {e}", "danger")
    return redirect(url_for("keyword_history", keyword_id=keyword_id))


# ---------------------------------------------------------------------------
# DataForSEO — Keyword Research
# ---------------------------------------------------------------------------

SEO_CHECKLIST_ITEMS = [
    {"key": "title_tag",        "label": "Title tag includes the keyword",             "priority": "high"},
    {"key": "title_length",     "label": "Title tag is 50–60 characters",              "priority": "high"},
    {"key": "meta_desc",        "label": "Meta description written & includes keyword","priority": "high"},
    {"key": "url_slug",         "label": "URL slug contains the keyword (e.g. /battle-belt)", "priority": "high"},
    {"key": "h1_keyword",       "label": "H1 heading includes the keyword",            "priority": "high"},
    {"key": "first_100_words",  "label": "Keyword appears in first 100 words of page", "priority": "high"},
    {"key": "image_alt",        "label": "Product images have keyword-rich alt text",  "priority": "medium"},
    {"key": "internal_links",   "label": "Other pages link to this product page",      "priority": "medium"},
    {"key": "blog_post",        "label": "Supporting blog post written & links here",  "priority": "medium"},
    {"key": "reviews",          "label": "Product has customer reviews (adds content)","priority": "medium"},
    {"key": "page_speed",       "label": "Page loads in under 3 seconds on mobile",   "priority": "medium"},
    {"key": "schema_markup",    "label": "Product schema markup added (price, availability)", "priority": "low"},
    {"key": "backlinks",        "label": "At least one external site links to this page", "priority": "low"},
    {"key": "paa_answered",     "label": "People Also Ask questions answered on page or blog", "priority": "low"},
]


def get_or_create_checklist(keyword_id):
    """Ensure all checklist items exist for a keyword, return them."""
    existing = {item.item_key: item for item in
                SeoChecklist.query.filter_by(keyword_id=keyword_id).all()}
    created = False
    for item in SEO_CHECKLIST_ITEMS:
        if item["key"] not in existing:
            new_item = SeoChecklist(keyword_id=keyword_id, item_key=item["key"], status="manual")
            db.session.add(new_item)
            created = True
    if created:
        db.session.commit()
    return SeoChecklist.query.filter_by(keyword_id=keyword_id).all()


def save_search_history(query, location_code="2840"):
    """Save a keyword research search, keeping only the last 10 unique queries."""
    # Remove duplicate if already in history
    existing = SearchHistory.query.filter(
        func.lower(SearchHistory.search_query) == query.lower()
    ).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
    # Add new entry
    entry = SearchHistory(search_query=query, location_code=str(location_code))
    db.session.add(entry)
    db.session.commit()
    # Keep only 10 most recent
    all_entries = SearchHistory.query.order_by(SearchHistory.searched_at.desc()).all()
    if len(all_entries) > 10:
        for old in all_entries[10:]:
            db.session.delete(old)
        db.session.commit()


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


def estimate_seo_difficulty(search_volume, competition, cpc):
    """
    Estimate SEO difficulty (0-100) from available signals.
    Uses search volume, ad competition, and CPC as proxies.
    High volume + high ad competition + high CPC = hard to rank.
    """
    if search_volume is None and competition is None and cpc is None:
        return None

    score = 0

    # Search volume component (0-40 pts): more searches = more competition
    vol = search_volume or 0
    if vol >= 100000:
        score += 40
    elif vol >= 50000:
        score += 35
    elif vol >= 10000:
        score += 28
    elif vol >= 5000:
        score += 22
    elif vol >= 1000:
        score += 15
    elif vol >= 500:
        score += 10
    else:
        score += 5

    # Ad competition component (0-35 pts): high paid competition = hard organic too
    comp = float(competition) if competition is not None else 0.5
    score += round(comp * 35)

    # CPC component (0-25 pts): high CPC = high commercial value = more competitors
    cpc_val = float(cpc) if cpc is not None else 0
    if cpc_val >= 5:
        score += 25
    elif cpc_val >= 3:
        score += 20
    elif cpc_val >= 2:
        score += 15
    elif cpc_val >= 1:
        score += 10
    elif cpc_val >= 0.5:
        score += 5
    else:
        score += 2

    return min(score, 100)


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
    questions = []
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

            # Get volume data for all
            volume_data = dataforseo_keyword_data(all_keywords, location_code=loc)

            # Get currently tracked keywords for badge
            tracked_set = {kw.keyword.lower() for kw in Keyword.query.all()}

            results = []
            for kw_text in all_keywords:
                d = volume_data.get(kw_text.lower(), {})
                comp = d.get("competition")
                cpc = d.get("cpc")
                vol = d.get("search_volume")
                results.append({
                    "keyword": d.get("keyword", kw_text),
                    "search_volume": vol,
                    "competition": comp,
                    "cpc": cpc,
                    "trend": d.get("trend", []),
                    "difficulty": estimate_seo_difficulty(vol, comp, cpc),
                    "already_tracked": kw_text.lower() in tracked_set,
                })

            # Sort: seed first, then by volume desc
            results.sort(key=lambda r: (
                r["keyword"].lower() != query.lower(),
                -(r["search_volume"] or 0)
            ))

            # Suggestions = related keywords not in main results (chips)
            suggestions = [r["keyword"] for r in results[1:15]]

            # People Also Ask questions for seed keyword
            try:
                questions = fetch_people_also_ask(query)
            except Exception as e:
                logger.warning(f"PAA fetch failed: {e}")
                questions = []

            # Save to search history
            try:
                save_search_history(query, location_code)
            except Exception as e:
                logger.warning(f"History save failed: {e}")

        except Exception as e:
            logger.error(f"DataForSEO error: {e}")
            error = f"Keyword research failed: {e}"

    recent_searches = SearchHistory.query.order_by(SearchHistory.searched_at.desc()).limit(10).all()

    return render_template(
        "keyword_research.html",
        results=results,
        query=query,
        suggestions=suggestions,
        questions=questions,
        error=error,
        location_code=location_code,
        recent_searches=recent_searches,
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
# SEO Grader helpers
# ---------------------------------------------------------------------------

def analyze_page_seo(url: str, keyword: str = ""):
    """Fetch a URL and analyze its on-page SEO. Returns a dict of checks and score."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BlueAlphaSEOBot/1.0)"}
    resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    kw_lower = keyword.strip().lower()

    # --- Title ---
    title_tag = soup.find("title")
    title_text = title_tag.get_text(strip=True) if title_tag else ""
    title_len = len(title_text)

    # --- Meta description ---
    meta_desc = ""
    meta_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if meta_tag:
        meta_desc = meta_tag.get("content", "")
    meta_len = len(meta_desc)

    # --- H1 ---
    h1_tags = soup.find_all("h1")
    h1_count = len(h1_tags)
    h1_text = " ".join(h.get_text(strip=True) for h in h1_tags)

    # --- H2s ---
    h2_count = len(soup.find_all("h2"))

    # --- Images without alt ---
    images = soup.find_all("img")
    imgs_no_alt = sum(1 for img in images if not img.get("alt", "").strip())

    # --- Body word count ---
    body = soup.find("body")
    body_text = body.get_text(separator=" ", strip=True) if body else ""
    words = re.findall(r"\b\w+\b", body_text)
    word_count = len(words)

    # --- Keyword density ---
    kw_density = 0.0
    if kw_lower and word_count > 0:
        kw_words = kw_lower.split()
        text_lower = body_text.lower()
        if len(kw_words) == 1:
            kw_occurrences = len(re.findall(r"\b" + re.escape(kw_lower) + r"\b", text_lower))
        else:
            kw_occurrences = text_lower.count(kw_lower)
        kw_density = round((kw_occurrences / word_count) * 100, 2)

    # --- Internal links ---
    parsed = urlparse(url)
    base_domain = parsed.netloc
    links = soup.find_all("a", href=True)
    internal_links = sum(
        1 for a in links
        if a["href"].startswith("/") or base_domain in a["href"]
    )

    # --- URL structure ---
    has_query = bool(parsed.query)
    url_path = parsed.path
    url_clean = not has_query and len(url_path) < 80

    # --- Build checks ---
    checks = []
    total_points = 0
    max_points = 0

    def add_check(name, status, points, max_pts, message, recommendation=""):
        nonlocal total_points, max_points
        total_points += points
        max_points += max_pts
        checks.append({
            "name": name,
            "status": status,  # pass/warn/fail
            "points": points,
            "max": max_pts,
            "message": message,
            "recommendation": recommendation,
        })

    # Title checks
    if not title_text:
        add_check("Title Tag", "fail", 0, 15, "No title tag found.",
                  "Add a <title> tag with 50-60 characters including your target keyword.")
    elif 50 <= title_len <= 60:
        add_check("Title Tag Length", "pass", 10, 10, f"Title is {title_len} chars (ideal 50-60).")
    elif title_len < 30:
        add_check("Title Tag Length", "fail", 3, 10, f"Title is only {title_len} chars (too short).",
                  "Expand your title to 50-60 characters.")
    elif title_len > 70:
        add_check("Title Tag Length", "warn", 6, 10, f"Title is {title_len} chars (may be truncated in SERPs).",
                  "Shorten title to under 60 characters to avoid truncation.")
    else:
        add_check("Title Tag Length", "warn", 7, 10, f"Title is {title_len} chars (slightly outside ideal 50-60).",
                  "Aim for 50-60 characters.")

    if title_text:
        if kw_lower and kw_lower in title_text.lower():
            add_check("Keyword in Title", "pass", 10, 10, f'Keyword "{keyword}" found in title.')
        elif kw_lower:
            add_check("Keyword in Title", "fail", 0, 10, f'Keyword "{keyword}" not found in title.',
                      "Include your target keyword near the beginning of the title tag.")
        else:
            add_check("Keyword in Title", "pass", 10, 10, "No keyword to check.", "")

    # Meta description checks
    if not meta_desc:
        add_check("Meta Description", "fail", 0, 15, "No meta description found.",
                  "Add a meta description of 150-160 characters including your target keyword.")
    elif 150 <= meta_len <= 160:
        add_check("Meta Description Length", "pass", 10, 10, f"Meta description is {meta_len} chars (ideal).")
    elif meta_len < 100:
        add_check("Meta Description Length", "warn", 5, 10, f"Meta description is only {meta_len} chars (too short).",
                  "Expand to 150-160 characters for better CTR.")
    elif meta_len > 170:
        add_check("Meta Description Length", "warn", 6, 10, f"Meta description is {meta_len} chars (may be truncated).",
                  "Shorten to under 160 characters.")
    else:
        add_check("Meta Description Length", "warn", 7, 10, f"Meta description is {meta_len} chars (close to ideal 150-160).")

    if meta_desc:
        if kw_lower and kw_lower in meta_desc.lower():
            add_check("Keyword in Meta Description", "pass", 5, 5, f'Keyword "{keyword}" found in meta description.')
        elif kw_lower:
            add_check("Keyword in Meta Description", "warn", 2, 5, f'Keyword "{keyword}" not in meta description.',
                      "Include your target keyword naturally in the meta description.")
        else:
            add_check("Keyword in Meta Description", "pass", 5, 5, "No keyword to check.")

    # H1 checks
    if h1_count == 0:
        add_check("H1 Tag", "fail", 0, 15, "No H1 tag found.",
                  "Add exactly one H1 tag containing your target keyword.")
    elif h1_count == 1:
        if kw_lower and kw_lower in h1_text.lower():
            add_check("H1 Tag", "pass", 15, 15, f"One H1 found containing the keyword.")
        elif kw_lower:
            add_check("H1 Tag", "warn", 10, 15, f"One H1 found but keyword \"{keyword}\" not in H1.",
                      "Include your target keyword in the H1 tag.")
        else:
            add_check("H1 Tag", "pass", 15, 15, "One H1 tag found.")
    else:
        add_check("H1 Tag", "warn", 8, 15, f"{h1_count} H1 tags found (should be exactly 1).",
                  "Use only one H1 per page.")

    # H2 check
    if h2_count >= 2:
        add_check("H2 Tags", "pass", 5, 5, f"{h2_count} H2 tags found — good content structure.")
    elif h2_count == 1:
        add_check("H2 Tags", "warn", 3, 5, "Only 1 H2 tag — consider adding more subheadings.",
                  "Add H2 subheadings to improve content structure and scannability.")
    else:
        add_check("H2 Tags", "fail", 0, 5, "No H2 tags found.",
                  "Add H2 subheadings to organize your content.")

    # Images alt text
    if images:
        if imgs_no_alt == 0:
            add_check("Image Alt Text", "pass", 5, 5, f"All {len(images)} images have alt text.")
        else:
            add_check("Image Alt Text", "warn", max(0, 5 - imgs_no_alt), 5,
                      f"{imgs_no_alt} of {len(images)} images missing alt text.",
                      "Add descriptive alt text to all images for accessibility and SEO.")
    else:
        add_check("Image Alt Text", "pass", 5, 5, "No images found on page.")

    # Word count
    if word_count >= 1000:
        add_check("Word Count", "pass", 10, 10, f"{word_count:,} words — great content depth.")
    elif word_count >= 500:
        add_check("Word Count", "warn", 6, 10, f"{word_count:,} words — decent but could be deeper.",
                  "Aim for 1,000+ words for competitive topics.")
    else:
        add_check("Word Count", "fail", 2, 10, f"Only {word_count:,} words — thin content.",
                  "Expand to at least 500-1,000 words with useful, relevant information.")

    # Keyword density
    if kw_lower:
        if 1.0 <= kw_density <= 2.5:
            add_check("Keyword Density", "pass", 5, 5, f"Keyword density is {kw_density}% (ideal 1-2.5%).")
        elif kw_density < 0.5:
            add_check("Keyword Density", "warn", 2, 5, f"Keyword density is {kw_density}% (too low).",
                      "Mention your keyword more naturally throughout the content.")
        elif kw_density > 3.5:
            add_check("Keyword Density", "fail", 0, 5, f"Keyword density is {kw_density}% (keyword stuffing risk).",
                      "Reduce keyword repetition — aim for 1-2.5%.")
        else:
            add_check("Keyword Density", "warn", 3, 5, f"Keyword density is {kw_density}% (slightly outside ideal).")
    else:
        add_check("Keyword Density", "pass", 5, 5, "No keyword specified.")

    # Internal links
    if internal_links >= 3:
        add_check("Internal Links", "pass", 5, 5, f"{internal_links} internal links — good internal linking.")
    elif internal_links >= 1:
        add_check("Internal Links", "warn", 3, 5, f"Only {internal_links} internal link(s).",
                  "Add more internal links to help users and Google discover other pages.")
    else:
        add_check("Internal Links", "fail", 0, 5, "No internal links found.",
                  "Add internal links to related pages on your site.")

    # URL structure
    if url_clean:
        add_check("URL Structure", "pass", 5, 5, "URL is clean and concise.")
    elif has_query:
        add_check("URL Structure", "warn", 2, 5, "URL contains query parameters.",
                  "Use clean, descriptive URLs without query strings when possible.")
    else:
        add_check("URL Structure", "warn", 3, 5, "URL path is long (80+ chars).",
                  "Keep URLs short and descriptive.")

    # Compute final score
    score = round((total_points / max_points) * 100) if max_points > 0 else 0

    return {
        "url": url,
        "keyword": keyword,
        "title": title_text,
        "title_len": title_len,
        "meta_desc": meta_desc,
        "meta_len": meta_len,
        "h1_count": h1_count,
        "h2_count": h2_count,
        "word_count": word_count,
        "kw_density": kw_density,
        "internal_links": internal_links,
        "imgs_no_alt": imgs_no_alt,
        "img_total": len(images),
        "checks": checks,
        "score": score,
    }


# ---------------------------------------------------------------------------
# Routes — SEO Grader
# ---------------------------------------------------------------------------

@app.route("/seo-grader", methods=["GET", "POST"])
@login_required
def seo_grader():
    analysis = None
    error = None
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        keyword = request.form.get("keyword", "").strip()
        if not url:
            flash("Please enter a URL.", "warning")
        else:
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            try:
                analysis = analyze_page_seo(url, keyword)
            except Exception as e:
                error = str(e)
                flash(f"Failed to analyze page: {e}", "danger")
    return render_template("seo_grader.html", analysis=analysis, error=error)


# ---------------------------------------------------------------------------
# Routes — Content Calendar
# ---------------------------------------------------------------------------

@app.route("/content-calendar")
@login_required
def content_calendar():
    # Fetch all keywords that have PAA questions
    keywords_with_questions = (
        db.session.query(Keyword)
        .join(KeywordQuestion, KeywordQuestion.keyword_id == Keyword.id)
        .filter(Keyword.active == True)
        .distinct()
        .all()
    )

    cards = []
    for kw in keywords_with_questions:
        questions_list = kw.questions.order_by(KeywordQuestion.fetched_at.asc()).all()
        if not questions_list:
            continue

        # Top question = first/most compelling as post title
        top_question = questions_list[0].question

        # Check if ContentIdea already exists for this keyword
        idea = ContentIdea.query.filter_by(keyword_id=kw.id).first()
        if not idea:
            idea = ContentIdea(
                keyword_id=kw.id,
                title=top_question,
                status="idea",
            )
            db.session.add(idea)
            db.session.flush()

        # Estimate traffic at #1
        traffic_potential = None
        if kw.monthly_volume:
            traffic_potential = round(kw.monthly_volume * 0.314)

        cards.append({
            "idea": idea,
            "keyword": kw,
            "questions": questions_list,
            "traffic_potential": traffic_potential,
        })

    db.session.commit()
    return render_template("content_calendar.html", cards=cards)


@app.route("/content-calendar/update-status", methods=["POST"])
@login_required
def content_calendar_update_status():
    data = request.get_json(force=True)
    idea_id = data.get("idea_id")
    new_status = data.get("status", "idea")
    notes = data.get("notes")

    idea = ContentIdea.query.get(idea_id)
    if not idea:
        return jsonify({"error": "Not found"}), 404

    valid_statuses = ["idea", "in_progress", "published"]
    if new_status not in valid_statuses:
        return jsonify({"error": "Invalid status"}), 400

    idea.status = new_status
    if notes is not None:
        idea.notes = notes
    db.session.commit()

    return jsonify({"ok": True, "status": idea.status, "idea_id": idea.id})


# ---------------------------------------------------------------------------
# Routes — SERP Preview
# ---------------------------------------------------------------------------

def fetch_page_meta(url: str):
    """Fetch title and meta description from a URL."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BlueAlphaSEOBot/1.0)"}
    resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    meta_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    description = meta_tag.get("content", "") if meta_tag else ""

    return title, description


def format_breadcrumb_url(url: str) -> str:
    """Format URL in Google's breadcrumb style."""
    parsed = urlparse(url)
    parts = [parsed.netloc]
    path_parts = [p for p in parsed.path.split("/") if p]
    parts.extend(path_parts)
    return " › ".join(parts)


@app.route("/serp-preview", methods=["GET", "POST"])
@login_required
def serp_preview():
    fetched_title = ""
    fetched_desc = ""
    fetched_url = ""
    error = None
    tracked_urls = []

    # Collect tracked ranking URLs for the dropdown
    recent_rankings = (
        db.session.query(Ranking)
        .filter(Ranking.url.isnot(None))
        .order_by(Ranking.checked_at.desc())
        .limit(50)
        .all()
    )
    seen_urls = set()
    for r in recent_rankings:
        if r.url and r.url not in seen_urls:
            seen_urls.add(r.url)
            tracked_urls.append({"url": r.url, "keyword": r.keyword_ref.keyword if r.keyword_ref else ""})

    if request.method == "POST":
        fetched_url = request.form.get("url", "").strip()
        if not fetched_url:
            flash("Please enter a URL.", "warning")
        else:
            if not fetched_url.startswith(("http://", "https://")):
                fetched_url = "https://" + fetched_url
            try:
                fetched_title, fetched_desc = fetch_page_meta(fetched_url)
            except Exception as e:
                error = str(e)
                flash(f"Failed to fetch page: {e}", "danger")

    return render_template(
        "serp_preview.html",
        fetched_title=fetched_title,
        fetched_desc=fetched_desc,
        fetched_url=fetched_url,
        breadcrumb_url=format_breadcrumb_url(fetched_url) if fetched_url else "",
        error=error,
        tracked_urls=tracked_urls,
    )


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
