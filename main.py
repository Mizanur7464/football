"""
Football match alert bot: monitor live matches via API-Football, run trigger rules, send Telegram alerts.
Run 24/7 on VPS; config via config.yaml. Secrets can be set in .env (copy from .env.example).
"""

import logging
import os
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from datetime import datetime
from zoneinfo import ZoneInfo

load_dotenv()
  
from api_client import APIFootballClient, APIFootballError
from rules import run_rules
from telegram_notifier import send_alert, send_telegram, handle_telegram_commands
from alert_tracker import AlertTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def in_active_window(schedule_cfg: dict, now_utc: datetime) -> bool:
    """
    Check if we are inside the active schedule window.
    schedule_cfg contains timezone + per-day ranges in HH:MM (in that timezone).
    now_utc must be timezone-aware UTC; if naive, we treat it as UTC.
    """
    if not schedule_cfg:
        return True

    # Ensure we have UTC-aware time so astimezone() converts correctly
    utc = ZoneInfo("UTC")
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=utc)

    tz_name = schedule_cfg.get("timezone", "Europe/London")
    try:
        local = now_utc.astimezone(ZoneInfo(tz_name))
    except Exception:
        # Fallback: use UTC if timezone not available
        local = now_utc.astimezone(utc) if now_utc.tzinfo else now_utc.replace(tzinfo=utc)

    weekday = local.weekday()  # 0=Monday, 6=Sunday
    minutes = local.hour * 60 + local.minute

    if 0 <= weekday <= 4:
        day_cfg = schedule_cfg.get("monday_friday") or {}
    elif weekday == 5:
        day_cfg = schedule_cfg.get("saturday") or {}
    else:
        day_cfg = schedule_cfg.get("sunday") or {}

    start_str = (day_cfg.get("start") or "").strip()
    end_str = (day_cfg.get("end") or "").strip()
    if not start_str or not end_str:
        return False

    try:
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
    except ValueError:
        return False

    start_min = sh * 60 + sm
    end_min = eh * 60 + em

    return start_min <= minutes <= end_min


def main():
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        logger.error("config.yaml not found. Copy config.example.yaml and set API key, Telegram token, chat_id.")
        sys.exit(1)

    config = load_config(str(config_path))
    api_cfg = config.get("api_football") or {}
    tg_cfg = config.get("telegram") or {}
    league_ids = config.get("league_ids") or []
    poll_interval = max(15, config.get("poll_interval_seconds") or 30)
    schedule_cfg = config.get("schedule") or {}
    rules_cfg = config.get("rules") or {}

    api_key = (os.getenv("API_FOOTBALL_KEY") or (api_cfg.get("api_key") or "").strip())
    if not api_key or api_key == "YOUR_API_FOOTBALL_KEY" or api_key == "your_api_football_key_here":
        logger.error("Set API_FOOTBALL_KEY in .env or api_football.api_key in config.yaml")
        sys.exit(1)

    bot_token = (os.getenv("TELEGRAM_BOT_TOKEN") or (tg_cfg.get("bot_token") or "").strip())
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or (tg_cfg.get("chat_id") or "").strip())
    if not bot_token or bot_token in ("YOUR_TELEGRAM_BOT_TOKEN", "your_telegram_bot_token_here"):
        logger.error("Set TELEGRAM_BOT_TOKEN in .env or telegram.bot_token in config.yaml")
        sys.exit(1)
    if not chat_id or chat_id in ("YOUR_CHAT_ID", "your_chat_id_here"):
        logger.error("Set TELEGRAM_CHAT_ID in .env or telegram.chat_id in config.yaml")
        sys.exit(1)

    # Ensure no whitespace; Telegram expects string or int
    chat_id = str(chat_id).strip()

    if not league_ids:
        logger.warning("No league_ids in config; add at least one league.")

    client = APIFootballClient(
        api_cfg.get("base_url", "https://v3.football.api-sports.io"),
        api_key,
    )
    tracker = AlertTracker(log_dir="logs")

    # One-time welcome message when the bot process starts
    welcome_text = (
        "Football alert bot is now running.\n"
        "You will receive alerts here when your configured rules are triggered "
        "on live matches."
    )
    try:
        send_telegram(bot_token, chat_id, welcome_text)
    except Exception:
        logger.warning("Failed to send welcome message to Telegram chat.")

    # Track Telegram updates and allow /start + /alerts_on/off control
    last_update_id: int | None = None
    alerts_enabled: bool = True
    last_poll_ts: float = 0.0  # last time we polled API-Football
    in_window_prev: bool = False  # previous schedule state (for auto-on at next window)

    logger.info("Bot started. Leagues=%s, poll_interval=%ss", len(league_ids), poll_interval)

    # Small base sleep so Telegram commands feel fast (1–2 seconds delay max)
    base_sleep = 2

    while True:
        try:
            now = datetime.now(ZoneInfo("UTC"))

            # Check schedule window (times in config are in schedule.timezone, e.g. Europe/London)
            in_window = in_active_window(schedule_cfg, now)

            # If a new schedule window just started, auto-enable alerts again
            # and notify the main chat that scheduled monitoring has started.
            if in_window and not in_window_prev:
                alerts_enabled = True
                try:
                    # Show current time in schedule timezone so user can verify (e.g. UK)
                    tz_name = schedule_cfg.get("timezone", "Europe/London")
                    try:
                        local_now = now.astimezone(ZoneInfo(tz_name))
                        time_in_tz = local_now.strftime("%H:%M")
                    except Exception:
                        time_in_tz = "—"
                    start_msg = (
                        "Scheduled monitoring window has started.\n"
                        f"Time in schedule timezone ({tz_name}): {time_in_tz}\n"
                        "Alerts are now ACTIVE for live matches within your rules."
                    )
                    send_telegram(bot_token, chat_id, start_msg)
                    logger.info("Schedule window started at %s %s", tz_name, time_in_tz)
                except Exception:
                    logger.warning("Failed to send schedule start message.")
            in_window_prev = in_window

            # Always handle Telegram commands (/start, /alerts_on, /alerts_off)
            last_update_id, alerts_enabled = handle_telegram_commands(
                bot_token,
                last_update_id,
                alerts_enabled,
            )

            # If alerts are paused, just sleep a bit and keep listening for commands
            if not alerts_enabled:
                logger.debug("Alerts are paused via Telegram command; skipping API polling.")
                time.sleep(base_sleep)
                continue

            # When alerts are ON (schedule auto or manual /alerts_on), always poll – schedule does not block
            # Throttle API polling to poll_interval seconds
            now_ts = time.time()
            if now_ts - last_poll_ts < poll_interval:
                time.sleep(base_sleep)
                continue

            fixtures = client.get_live_fixtures(league_ids)
            last_poll_ts = now_ts

            if not fixtures:
                logger.debug("No live fixtures right now.")
                time.sleep(base_sleep)
                continue

            for fixture_data in fixtures:
                fixture_obj = fixture_data.get("fixture") or {}
                fixture_id = fixture_obj.get("id")
                if not fixture_id:
                    continue
                odds_list = client.get_fixture_odds(fixture_id, live=True)
                # API returns list of {league, fixture, bookmakers}; we need bookmakers flattened for odds
                odds_for_rules = []
                for o in odds_list:
                    odds_for_rules.append(o)
                triggered = run_rules(fixture_data, odds_for_rules, rules_cfg)
                for rule_key, market_name in triggered:
                    if tracker.already_sent(fixture_id, rule_key):
                        continue
                    trigger_desc = f"Rule: {rule_key}"
                    sent_ok = send_alert(
                        bot_token,
                        chat_id,
                        fixture_data,
                        rule_key,
                        market_name,
                        trigger_desc,
                    )
                    if sent_ok:
                        tracker.mark_sent(fixture_id, rule_key)
                        teams = fixture_data.get("teams") or {}
                        goals = fixture_data.get("goals") or {}
                        status = fixture_obj.get("status") or {}
                        tracker.log_alert(
                            fixture_id,
                            rule_key,
                            market_name,
                            (teams.get("home") or {}).get("name") or "Home",
                            (teams.get("away") or {}).get("name") or "Away",
                            int(status.get("elapsed") or 0),
                            f"{goals.get('home', 0)}-{goals.get('away', 0)}",
                        )
                    else:
                        logger.warning(
                            "Alert not sent (Telegram failed): fixture_id=%s rule=%s",
                            fixture_id,
                            rule_key,
                        )

        except APIFootballError as e:
            logger.warning("API error: %s", e)
            time.sleep(base_sleep)
        except Exception as e:
            logger.exception("Loop error: %s", e)
            time.sleep(base_sleep)


if __name__ == "__main__":
    main()
