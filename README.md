# Football Match Alert Bot

Python Telegram bot that monitors live football matches via **API-Football**, evaluates custom trigger rules, and sends real-time alerts to your Telegram chat.

## Features

- **Live match monitoring** for multiple leagues (configurable league IDs)
- **Trigger rules** (config-driven):
  - Over 0.5 First Half Goals (odds ≥ 1.50, score 0–0, minute < 45)
  - Both Teams To Score – Yes (odds ≥ 2.00, market alive)
  - Over 0.5 Full-Time at 60' (odds ≥ 1.80, score 0–0, minute ≥ 60)
- **Duplicate prevention**: one alert per match/rule
- **Logging**: all triggered alerts written to `logs/alerts.log`
- **Config file**: API keys, league IDs, rules, poll interval, time windows
- **24/7 ready**: run on a VPS with proper error handling

## Requirements

- Python 3.10+
- API-Football API key ([api-football.com](https://www.api-football.com/))
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Telegram Chat ID (where alerts will be sent)

## Installation

### 1. Clone or copy the project

```bash
cd "d:\Project\football bot"
```

### 2. Create virtual environment (recommended)

```bash
python -m venv venv
venv\Scripts\activate
```

On Linux/Mac:

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure

Copy the example config and edit with your values:

```bash
copy config.example.yaml config.yaml
```

**Secrets (recommended: use .env):**

```bash
copy .env.example .env
```

Edit `.env` and set:

- **API_FOOTBALL_KEY**: Your API-Football key
- **TELEGRAM_BOT_TOKEN**: Your Telegram bot token
- **TELEGRAM_CHAT_ID**: Your Telegram chat ID (user or group)

The bot reads these from `.env` first; if not set, it uses `config.yaml`. So you can keep secrets only in `.env` (which is not committed).

**In config.yaml** set:

- **league_ids**: List of league IDs to monitor (e.g. 39 = Premier League)
- **rules**: Enable/disable or adjust min_odds, minutes, etc.

To get your **Chat ID**: message your bot, then open:

`https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`

Look for `"chat":{"id": ...}` in the response.

### 5. Run the bot

```bash
python main.py
```

For production (VPS), run in background or as a service:

```bash
# Linux example with nohup
nohup python main.py > bot.log 2>&1 &

# Or use systemd / screen / tmux
```

## Configuration

| Key | Description |
|-----|-------------|
| `api_football.base_url` | API base URL (default: v3.football.api-sports.io) |
| `api_football.api_key` | Your API key |
| `telegram.bot_token` | Telegram bot token |
| `telegram.chat_id` | Target chat ID |
| `league_ids` | List of league IDs (15+ supported) |
| `poll_interval_seconds` | Seconds between poll cycles (min 15) |
| `active_hours_utc` | Empty = 24/7; or list of UTC hours to run |
| `rules.*` | Enable/disable and thresholds per rule |

## Alert format (MVP)

Alerts are sent as plain text, for example:

```
Match Alert: Team A vs Team B
Market: Over 0.5 goals – 1st half
Trigger: Rule: over_05_first_half
Current Score: 0-0
Time: 23'
```

## Project structure

```
football bot/
  config.example.yaml   # Template config
  config.yaml           # Your config (do not commit)
  requirements.txt
  main.py               # Entry point, main loop
  api_client.py         # API-Football client
  rules.py              # Trigger rules logic
  telegram_notifier.py  # Format and send Telegram alerts
  alert_tracker.py      # Duplicate prevention + file logging
  logs/                 # alerts.log created at runtime
  README.md
```

## API-Football notes

- **Authentication**: Use your API key in `config.yaml`. The client sends both `x-rapidapi-key` and `x-apisports-key` for compatibility.
- **Rate limits**: Respect your plan's request limits. Increase `poll_interval_seconds` if you hit limits.
- **Live odds**: Some plans have separate live odds endpoints; the bot uses the standard odds endpoint per fixture.

## Adding or changing rules

1. Edit `config.yaml` under `rules` (enable/disable, min_odds, minutes).
2. For new rule types, add the rule config and implement the checker in `rules.py`, then register it in `RULE_CHECKERS`.

## Support

For rule updates or changes, adjust the config file or extend the code as above. Full source is provided for customization.
