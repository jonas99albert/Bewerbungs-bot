# 📄 Anschreiben-Bot v2 – Mit täglicher Jobsuche

Telegram-Bot der täglich 10 passende Stellen von LinkedIn & Indeed sucht und
auf Knopfdruck ein individuelles Anschreiben per Claude AI erstellt.

---

## 🚀 Setup (Raspberry Pi)

```bash
# Dateien auf den Pi kopieren
scp -r telegram_anschreiben_bot_v2/ pi@raspberrypi.local:~/

# SSH auf den Pi
ssh pi@raspberrypi.local

cd ~/telegram_anschreiben_bot_v2
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## ⚙️ Als Systemdienst einrichten

```bash
# Tokens in Service-Datei eintragen
nano anschreiben-bot.service

# Service installieren
sudo cp anschreiben-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable anschreiben-bot
sudo systemctl start anschreiben-bot

# Logs beobachten
journalctl -u anschreiben-bot -f
```

---

## 💬 Bot-Befehle

| Befehl | Beschreibung |
|--------|-------------|
| `/setup` | Lebenslauf & Muster-Anschreiben hochladen |
| `/jobsetup` | Berufsfeld, Ort, Keywords, Uhrzeit festlegen |
| `/alert` | Täglichen Job-Alert ein-/ausschalten |
| `/suchenow` | Sofort nach Jobs suchen |
| `/status` | Aktuelle Einstellungen anzeigen |

## 🔄 Workflow

1. `/setup` → Lebenslauf + Muster-Anschreiben hochladen (.txt oder .pdf)
2. `/jobsetup` → Suchpräferenzen festlegen (Titel, Ort, Keywords, Uhrzeit)
3. `/alert` → Täglichen Digest aktivieren
4. Jeden Morgen: 10 Jobs mit **[✍️ Anschreiben erstellen]** Button
5. Knopf drücken → passendes Anschreiben erscheint sofort im Chat

---

## ⚠️ LinkedIn-Hinweis

JobSpy scraped LinkedIn inoffiziell. LinkedIn blockt gelegentlich Anfragen,
besonders bei häufiger Nutzung. Falls LinkedIn nicht funktioniert:
- Indeed liefert trotzdem Ergebnisse
- VPN auf dem Pi kann helfen
- Alternativ: Suchintervall auf alle 2 Tage reduzieren

---

## 🕐 Zeitzone

Der Alert läuft in UTC. Für Deutschland (CET/CEST) gilt:
- Winter: UTC+1 → `DAILY_HOUR=7` für 08:00 Uhr
- Sommer: UTC+2 → `DAILY_HOUR=6` für 08:00 Uhr
