#!/usr/bin/env python3
"""
Telegram Bot v2: Anschreiben-Generator + Tägliche Jobsuche
- Täglich 10 passende Stellen von LinkedIn/Indeed suchen
- Anschreiben per Knopfdruck erstellen
"""

import os
import io
import json
import logging
import asyncio
import hashlib
import re
from datetime import datetime, time
from pathlib import Path

import httpx
import anthropic
from jobspy import scrape_jobs
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Konfiguration ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
DATA_DIR       = Path("user_data")
DATA_DIR.mkdir(exist_ok=True)

# Uhrzeit des täglichen Job-Digests (UTC → z.B. 06:00 UTC = 08:00 CEST)
DAILY_HOUR   = int(os.environ.get("DAILY_HOUR", "6"))
DAILY_MINUTE = int(os.environ.get("DAILY_MINUTE", "0"))

# ConversationHandler-States
(
    SETUP_CHOICE, SETUP_CV, SETUP_MUSTER,
    JOB_TITLE, JOB_LOCATION, JOB_KEYWORDS, JOB_REMOTE, JOB_TIME,
) = range(8)

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ── Datenpersistenz ───────────────────────────────────────────────────────────

def load_user_data(user_id: int) -> dict:
    path = DATA_DIR / f"{user_id}_data.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

def save_user_data(user_id: int, data: dict):
    path = DATA_DIR / f"{user_id}_data.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def load_all_users() -> list[int]:
    """Gibt alle User-IDs zurück, die einen Job-Alert aktiviert haben."""
    users = []
    for f in DATA_DIR.glob("*_data.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            uid = int(f.stem.split("_")[0])
            if d.get("job_prefs") and d.get("job_alert_active"):
                users.append(uid)
        except Exception:
            pass
    return users

def job_id(job: dict) -> str:
    """Stabile ID aus Jobtitel + Firma + URL."""
    raw = (job.get("title", "") + job.get("company", "") + job.get("job_url", "")).encode()
    return hashlib.md5(raw).hexdigest()[:12]

def save_job_cache(user_id: int, jobs: list[dict]):
    """Speichert die letzten gesendeten Jobs für Callback-Lookup."""
    path = DATA_DIR / f"{user_id}_jobs.json"
    cache = {job_id(j): j for j in jobs}
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

def load_job_from_cache(user_id: int, jid: str) -> dict | None:
    path = DATA_DIR / f"{user_id}_jobs.json"
    if not path.exists():
        return None
    cache = json.loads(path.read_text(encoding="utf-8"))
    return cache.get(jid)

# ── Jobsuche ──────────────────────────────────────────────────────────────────

def search_jobs_sync(prefs: dict) -> list[dict]:
    """
    Sucht Jobs via JobSpy (LinkedIn + Indeed).
    Läuft synchron – wird per run_in_executor aufgerufen.
    """
    search_term = prefs.get("title", "Software Engineer")
    location    = prefs.get("location", "Deutschland")
    keywords    = prefs.get("keywords", "")
    is_remote   = prefs.get("remote", False)

    # Vollständiger Suchbegriff
    full_query = f"{search_term} {keywords}".strip()

    try:
        df = scrape_jobs(
            site_name=["linkedin", "indeed"],
            search_term=full_query,
            location=location,
            results_wanted=20,          # mehr holen, dann filtern
            hours_old=48,               # max. 2 Tage alt
            country_indeed="Germany",
            linkedin_fetch_description=True,
            is_remote=is_remote,
            verbose=0,
        )
    except Exception as e:
        logger.error(f"JobSpy Fehler: {e}")
        return []

    if df is None or df.empty:
        return []

    jobs = []
    for _, row in df.iterrows():
        jobs.append({
            "title":       str(row.get("title", "")).strip(),
            "company":     str(row.get("company", "")).strip(),
            "location":    str(row.get("location", "")).strip(),
            "job_url":     str(row.get("job_url", "")).strip(),
            "description": str(row.get("description", ""))[:4000].strip(),
            "date_posted": str(row.get("date_posted", "")).strip(),
            "site":        str(row.get("site", "")).strip(),
        })
        if len(jobs) >= 10:
            break

    return jobs

async def search_jobs(prefs: dict) -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, search_jobs_sync, prefs)

# ── KI-Funktionen ─────────────────────────────────────────────────────────────

def generate_anschreiben(lebenslauf: str, muster: str, stellenbeschreibung: str) -> str:
    prompt = f"""Du bist ein professioneller Karriereberater.
Erstelle auf Basis der folgenden Informationen ein überzeugendes, individuelles Anschreiben auf Deutsch.

## Lebenslauf:
{lebenslauf}

## Muster-Anschreiben (Stil & Ton übernehmen):
{muster}

## Stellenausschreibung:
{stellenbeschreibung}

## Regeln:
- Passe das Anschreiben exakt an die Anforderungen der Stelle an.
- Übernimm denselben Stil wie im Muster-Anschreiben.
- Hebe die relevantesten Qualifikationen hervor.
- Max. eine DIN-A4-Seite (ca. 300–400 Wörter).
- Struktur: Ort/Datum, Betreff, Anrede, 3–4 Absätze, Grußformel.
- Nur das fertige Anschreiben ausgeben, ohne Kommentare.
"""
    msg = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text

# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

async def fetch_url_text(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        text = re.sub(r"<[^>]+>", " ", r.text)
        return re.sub(r"\s+", " ", text).strip()[:12000]

async def download_telegram_file(file_id: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    tg_file = await context.bot.get_file(file_id)
    async with httpx.AsyncClient() as client:
        r = await client.get(tg_file.file_path)
        content = r.content
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(content))
            return "\n".join(p.extract_text() or "" for p in reader.pages)
        except Exception:
            return content.decode("latin-1", errors="replace")

def site_emoji(site: str) -> str:
    return {"linkedin": "💼", "indeed": "🔍", "glassdoor": "🏢"}.get(site.lower(), "📌")

# ── Job-Nachrichten senden ────────────────────────────────────────────────────

async def send_jobs_to_user(user_id: int, jobs: list[dict], bot):
    if not jobs:
        await bot.send_message(
            chat_id=user_id,
            text="😕 Heute keine passenden Stellen gefunden. Morgen wieder!",
        )
        return

    save_job_cache(user_id, jobs)

    header = (
        f"🌅 *Dein tägliches Job-Update* ({datetime.now().strftime('%d.%m.%Y')})\n"
        f"Ich habe *{len(jobs)} passende Stellen* für dich gefunden:\n"
        + "─" * 30
    )
    await bot.send_message(chat_id=user_id, text=header, parse_mode="Markdown")

    for i, job in enumerate(jobs, 1):
        jid  = job_id(job)
        icon = site_emoji(job.get("site", ""))
        text = (
            f"{icon} *{i}. {job['title']}*\n"
            f"🏢 {job['company']}\n"
            f"📍 {job['location']}\n"
            f"📅 {job.get('date_posted', 'k.A.')}\n"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✍️ Anschreiben erstellen", callback_data=f"anschreiben:{user_id}:{jid}"),
            InlineKeyboardButton("🔗 Zur Stelle", url=job["job_url"]),
        ]])
        await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        await asyncio.sleep(0.3)   # Flood-Schutz

# ── Täglicher Job-Scheduler ───────────────────────────────────────────────────

async def daily_job_search(context: ContextTypes.DEFAULT_TYPE):
    """Wird täglich um DAILY_HOUR:DAILY_MINUTE UTC ausgeführt."""
    logger.info("Tägliche Jobsuche startet...")
    user_ids = load_all_users()
    for uid in user_ids:
        try:
            data  = load_user_data(uid)
            prefs = data.get("job_prefs", {})
            await context.bot.send_message(
                chat_id=uid,
                text="🔍 Suche gerade nach passenden Stellen für dich...",
            )
            jobs = await search_jobs(prefs)
            await send_jobs_to_user(uid, jobs, context.bot)
        except Exception as e:
            logger.error(f"Fehler bei User {uid}: {e}")

# ── Command-Handler ───────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Willkommen beim Bewerbungs-Bot v2!*\n\n"
        "📋 *Befehle:*\n"
        "• /setup – Lebenslauf & Muster-Anschreiben hinterlegen\n"
        "• /jobsetup – Job-Suchpräferenzen festlegen\n"
        "• /suchenow – Sofort nach Jobs suchen\n"
        "• /alert – Täglichen Job-Alert ein-/ausschalten\n"
        "• /status – Übersicht über deine Einstellungen\n"
        "• /help – Hilfe\n\n"
        "💡 Oder schick direkt einen *Link* zu einer Stellenausschreibung!",
        parse_mode="Markdown",
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Anleitung:*\n\n"
        "1️⃣ /setup → Lebenslauf & Muster-Anschreiben hochladen\n"
        "2️⃣ /jobsetup → Berufsfeld, Ort & Stichwörter festlegen\n"
        "3️⃣ /alert → Täglichen Digest aktivieren\n"
        "4️⃣ Jeden Morgen bekommst du 10 Jobs – klick auf *Anschreiben erstellen*!\n\n"
        "📌 Oder schick jederzeit einen Stellenlink für ein sofortiges Anschreiben.",
        parse_mode="Markdown",
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    data = load_user_data(uid)
    prefs = data.get("job_prefs", {})

    cv_ok     = "✅" if data.get("lebenslauf") else "❌"
    muster_ok = "✅" if data.get("muster")     else "❌"
    alert_ok  = "✅ aktiv" if data.get("job_alert_active") else "❌ inaktiv"
    prefs_ok  = f"✅ {prefs.get('title','?')} in {prefs.get('location','?')}" if prefs else "❌ nicht gesetzt"

    await update.message.reply_text(
        f"📊 *Dein Status:*\n\n"
        f"{cv_ok} Lebenslauf\n"
        f"{muster_ok} Muster-Anschreiben\n"
        f"🔍 Job-Präferenzen: {prefs_ok}\n"
        f"🔔 Täglicher Alert: {alert_ok}\n\n"
        f"⏰ Alert-Uhrzeit: täglich {DAILY_HOUR:02d}:{DAILY_MINUTE:02d} UTC",
        parse_mode="Markdown",
    )

async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    data = load_user_data(uid)

    if not data.get("job_prefs"):
        await update.message.reply_text("❌ Bitte zuerst /jobsetup ausführen!")
        return

    current = data.get("job_alert_active", False)
    data["job_alert_active"] = not current
    save_user_data(uid, data)

    if not current:
        await update.message.reply_text(
            f"🔔 Täglicher Job-Alert *aktiviert*!\n"
            f"Du bekommst jeden Tag um {DAILY_HOUR:02d}:{DAILY_MINUTE:02d} UTC deine Job-Vorschläge.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("🔕 Täglicher Job-Alert *deaktiviert*.", parse_mode="Markdown")

async def cmd_suchenow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    data = load_user_data(uid)

    if not data.get("job_prefs"):
        await update.message.reply_text("❌ Bitte zuerst /jobsetup ausführen!")
        return

    msg = await update.message.reply_text("🔍 Suche nach passenden Stellen... (kann 30–60 Sek. dauern)")
    prefs = data["job_prefs"]
    jobs  = await search_jobs(prefs)
    await msg.delete()
    await send_jobs_to_user(uid, jobs, context.bot)

# ── Job-Setup ConversationHandler ─────────────────────────────────────────────

async def cmd_jobsetup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 *Job-Präferenzen einrichten*\n\n"
        "Wie lautet deine *gewünschte Berufsbezeichnung*?\n"
        "_(z.B. „Software Engineer", „Marketing Manager", „Data Analyst")_",
        parse_mode="Markdown",
    )
    return JOB_TITLE

async def jobsetup_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["job_title"] = update.message.text.strip()
    await update.message.reply_text(
        "📍 In welcher *Stadt / Region* suchst du?\n"
        "_(z.B. „Berlin", „München", „Remote", „Deutschland")_",
        parse_mode="Markdown",
    )
    return JOB_LOCATION

async def jobsetup_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["job_location"] = update.message.text.strip()
    await update.message.reply_text(
        "🏷️ Welche *weiteren Stichwörter* soll die Suche berücksichtigen?\n"
        "_(z.B. „Python React", „agil Scrum", „Teilzeit" – oder `skip` für keine)_",
        parse_mode="Markdown",
    )
    return JOB_KEYWORDS

async def jobsetup_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    context.user_data["job_keywords"] = "" if raw.lower() == "skip" else raw
    await update.message.reply_text(
        "🏠 Nur *Remote-Stellen* anzeigen?\n"
        "Antworte mit `ja` oder `nein`.",
        parse_mode="Markdown",
    )
    return JOB_REMOTE

async def jobsetup_remote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["job_remote"] = update.message.text.strip().lower() in ("ja", "yes", "j", "y")
    await update.message.reply_text(
        "⏰ Um wie viel Uhr (UTC) soll der tägliche Job-Alert kommen?\n"
        f"_(Standard: {DAILY_HOUR:02d}:{DAILY_MINUTE:02d} UTC – einfach `ok` eingeben)_\n"
        "Oder eigene Uhrzeit: z.B. `07:30`",
        parse_mode="Markdown",
    )
    return JOB_TIME

async def jobsetup_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    hour, minute = DAILY_HOUR, DAILY_MINUTE

    if raw != "ok":
        try:
            parts  = raw.split(":")
            hour   = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except Exception:
            await update.message.reply_text("Ungültiges Format. Nutze `HH:MM` oder `ok`.", parse_mode="Markdown")
            return JOB_TIME

    uid  = update.effective_user.id
    data = load_user_data(uid)
    data["job_prefs"] = {
        "title":    context.user_data["job_title"],
        "location": context.user_data["job_location"],
        "keywords": context.user_data.get("job_keywords", ""),
        "remote":   context.user_data.get("job_remote", False),
        "hour":     hour,
        "minute":   minute,
    }
    save_user_data(uid, data)

    await update.message.reply_text(
        f"✅ *Job-Präferenzen gespeichert!*\n\n"
        f"🔎 Suche: `{data['job_prefs']['title']}` in `{data['job_prefs']['location']}`\n"
        f"🏷️ Keywords: `{data['job_prefs']['keywords'] or '–'}`\n"
        f"🏠 Remote: {'Ja' if data['job_prefs']['remote'] else 'Nein'}\n"
        f"⏰ Alert: täglich {hour:02d}:{minute:02d} UTC\n\n"
        f"Nutze /alert um den täglichen Alert zu aktivieren.\n"
        f"Oder /suchenow für eine sofortige Suche!",
        parse_mode="Markdown",
    )
    return ConversationHandler.END

async def jobsetup_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Job-Setup abgebrochen.")
    return ConversationHandler.END

# ── Dokument-Setup ConversationHandler ───────────────────────────────────────

async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚙️ *Dokumente hinterlegen*\n\n"
        "• `1` – Lebenslauf\n"
        "• `2` – Muster-Anschreiben\n"
        "• `3` – Beides",
        parse_mode="Markdown",
    )
    return SETUP_CHOICE

async def setup_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    context.user_data["setup_choice"] = choice
    if choice in ("1", "3"):
        await update.message.reply_text("📄 Lebenslauf als *.txt* oder *.pdf* schicken.", parse_mode="Markdown")
        return SETUP_CV
    elif choice == "2":
        await update.message.reply_text("📝 Muster-Anschreiben als *.txt* oder *.pdf* schicken.", parse_mode="Markdown")
        return SETUP_MUSTER
    else:
        await update.message.reply_text("Bitte 1, 2 oder 3 eingeben.")
        return SETUP_CHOICE

async def setup_cv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        await update.message.reply_text("Bitte eine Datei schicken.")
        return SETUP_CV
    await update.message.reply_text("⏳ Verarbeite...")
    text = await download_telegram_file(update.message.document.file_id, context)
    data = load_user_data(update.effective_user.id)
    data["lebenslauf"] = text
    save_user_data(update.effective_user.id, data)
    if context.user_data.get("setup_choice") == "3":
        await update.message.reply_text("✅ Lebenslauf gespeichert!\n\nJetzt das *Muster-Anschreiben* schicken.", parse_mode="Markdown")
        return SETUP_MUSTER
    await update.message.reply_text("✅ Lebenslauf gespeichert!")
    return ConversationHandler.END

async def setup_muster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        await update.message.reply_text("Bitte eine Datei schicken.")
        return SETUP_MUSTER
    await update.message.reply_text("⏳ Verarbeite...")
    text = await download_telegram_file(update.message.document.file_id, context)
    data = load_user_data(update.effective_user.id)
    data["muster"] = text
    save_user_data(update.effective_user.id, data)
    await update.message.reply_text("✅ Muster-Anschreiben gespeichert! Nutze /suchenow für eine sofortige Jobsuche.")
    return ConversationHandler.END

async def setup_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Abgebrochen.")
    return ConversationHandler.END

# ── Callback: Anschreiben per Button ─────────────────────────────────────────

async def callback_anschreiben(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts   = query.data.split(":")   # "anschreiben:{user_id}:{jid}"
    user_id = int(parts[1])
    jid     = parts[2]

    data = load_user_data(user_id)
    if not data.get("lebenslauf") or not data.get("muster"):
        await query.message.reply_text("❌ Bitte zuerst /setup ausführen (Lebenslauf + Muster-Anschreiben).")
        return

    job = load_job_from_cache(user_id, jid)
    if not job:
        await query.message.reply_text("❌ Job nicht mehr im Cache. Bitte /suchenow ausführen.")
        return

    msg = await query.message.reply_text(
        f"✍️ Erstelle Anschreiben für *{job['title']}* bei *{job['company']}*...",
        parse_mode="Markdown",
    )

    stellentext = (
        f"Stelle: {job['title']}\n"
        f"Unternehmen: {job['company']}\n"
        f"Ort: {job['location']}\n\n"
        f"Beschreibung:\n{job['description']}"
    )

    try:
        loop        = asyncio.get_event_loop()
        anschreiben = await loop.run_in_executor(
            None,
            generate_anschreiben,
            data["lebenslauf"],
            data["muster"],
            stellentext,
        )
    except Exception as e:
        await msg.edit_text(f"❌ Fehler: {e}")
        return

    await msg.delete()

    header = f"📄 *Anschreiben – {job['title']} @ {job['company']}*\n" + "─" * 30 + "\n\n"
    full   = header + anschreiben

    if len(full) <= 4096:
        await query.message.reply_text(full, parse_mode="Markdown")
    else:
        await query.message.reply_document(
            document=io.BytesIO(anschreiben.encode("utf-8")),
            filename=f"Anschreiben_{job['company'].replace(' ','_')}.txt",
            caption=f"📄 Dein Anschreiben für {job['title']} @ {job['company']}",
        )

# ── URL-Handler (direkter Stellenlink) ───────────────────────────────────────

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url     = update.message.text.strip()
    user_id = update.effective_user.id
    data    = load_user_data(user_id)

    if not data.get("lebenslauf") or not data.get("muster"):
        await update.message.reply_text("❌ Bitte zuerst /setup ausführen.")
        return

    msg = await update.message.reply_text("🔍 Lade Stellenausschreibung...")
    try:
        stellentext = await fetch_url_text(url)
    except Exception as e:
        await msg.edit_text(f"❌ URL konnte nicht geladen werden: {e}")
        return

    await msg.edit_text("✍️ Generiere Anschreiben... (20–30 Sek.)")
    try:
        loop        = asyncio.get_event_loop()
        anschreiben = await loop.run_in_executor(
            None, generate_anschreiben,
            data["lebenslauf"], data["muster"], stellentext,
        )
    except Exception as e:
        await msg.edit_text(f"❌ Fehler bei der Generierung: {e}")
        return

    await msg.delete()
    header = "📄 *Dein Anschreiben:*\n" + "─" * 30 + "\n\n"
    full   = header + anschreiben

    if len(full) <= 4096:
        await update.message.reply_text(full, parse_mode="Markdown")
    else:
        await update.message.reply_document(
            document=io.BytesIO(anschreiben.encode("utf-8")),
            filename="Anschreiben.txt",
            caption="📄 Dein Anschreiben (als Datei)",
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Sende einen *Link* zur Stellenausschreibung oder nutze /suchenow für automatische Jobsuche.",
        parse_mode="Markdown",
    )

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Dokument-Setup
    doc_setup = ConversationHandler(
        entry_points=[CommandHandler("setup", cmd_setup)],
        states={
            SETUP_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_choice)],
            SETUP_CV:     [MessageHandler(filters.Document.ALL, setup_cv)],
            SETUP_MUSTER: [MessageHandler(filters.Document.ALL, setup_muster)],
        },
        fallbacks=[CommandHandler("abbrechen", setup_cancel)],
    )

    # Job-Präferenzen-Setup
    job_setup = ConversationHandler(
        entry_points=[CommandHandler("jobsetup", cmd_jobsetup)],
        states={
            JOB_TITLE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, jobsetup_title)],
            JOB_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, jobsetup_location)],
            JOB_KEYWORDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, jobsetup_keywords)],
            JOB_REMOTE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, jobsetup_remote)],
            JOB_TIME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, jobsetup_time)],
        },
        fallbacks=[CommandHandler("abbrechen", jobsetup_cancel)],
    )

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("alert",    cmd_alert))
    app.add_handler(CommandHandler("suchenow", cmd_suchenow))
    app.add_handler(doc_setup)
    app.add_handler(job_setup)
    app.add_handler(CallbackQueryHandler(callback_anschreiben, pattern=r"^anschreiben:"))
    app.add_handler(MessageHandler(filters.Entity("url"), handle_url))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Täglicher Job-Alert (alle aktiven User)
    app.job_queue.run_daily(
        daily_job_search,
        time=time(hour=DAILY_HOUR, minute=DAILY_MINUTE),
        name="daily_jobs",
    )

    logger.info(f"Bot startet – täglicher Alert um {DAILY_HOUR:02d}:{DAILY_MINUTE:02d} UTC")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
