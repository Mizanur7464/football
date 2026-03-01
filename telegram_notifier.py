"""
Send formatted alerts to Telegram.
"""

import logging
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)


def format_alert(
    home_team: str,
    away_team: str,
    market_name: str,
    trigger_desc: str,
    score_home: int,
    score_away: int,
    minute: int,
) -> str:
    """Plain text alert as requested for MVP."""
    return (
        f"Match Alert: {home_team} vs {away_team}\n"
        f"Market: {market_name}\n"
        f"Trigger: {trigger_desc}\n"
        f"Current Score: {score_home}-{score_away}\n"
        f"Time: {minute}'"
    )


def send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    """Send message to Telegram chat. Returns True on success."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    chat_id = str(chat_id).strip()
    try:
        r = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code != 200:
            logger.warning("Telegram send failed: %s %s", r.status_code, r.text)
            # Retry without parse_mode in case of format issue
            r = requests.post(
                url,
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
        ok = r.status_code == 200
        if not ok:
            logger.warning("Telegram send failed: %s", r.text)
        return ok
    except Exception as e:
        logger.warning("Telegram send error: %s", e)
        return False


def send_alert(
    bot_token: str,
    chat_id: str,
    fixture: dict,
    rule_key: str,
    market_name: str,
    trigger_desc: str,
) -> bool:
    """Build alert text from fixture and send to Telegram."""
    fixture_obj = fixture.get("fixture") or {}
    teams = fixture.get("teams") or {}
    goals = fixture.get("goals") or {}
    status = fixture_obj.get("status") or {}
    home_team = (teams.get("home") or {}).get("name") or "Home"
    away_team = (teams.get("away") or {}).get("name") or "Away"
    score_home = int(goals.get("home") or 0)
    score_away = int(goals.get("away") or 0)
    minute = int(status.get("elapsed") or 0)
    text = format_alert(
        home_team, away_team, market_name, trigger_desc,
        score_home, score_away, minute,
    )
    return send_telegram(bot_token, chat_id, text)


def handle_telegram_commands(
    bot_token: str,
    last_update_id: Optional[int],
    alerts_enabled: bool,
    in_schedule_window: Optional[bool] = None,
) -> Tuple[Optional[int], bool]:
    """
    Poll Telegram getUpdates and:
    - Reply with a welcome message to any chat that sends /start
    - Toggle alerts on/off with /alerts_on and /alerts_off
    - /status: reply with current alerts and schedule state
    Returns (latest_update_id, alerts_enabled_flag).
    """
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params = {
        "timeout": 0,
    }
    if last_update_id is not None:
        params["offset"] = last_update_id + 1

    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("Telegram getUpdates error: %s", e)
        return last_update_id, alerts_enabled

    results = data.get("result") or []
    if not results:
        return last_update_id, alerts_enabled

    new_last_id = last_update_id
    current_enabled = alerts_enabled

    for update in results:
        upd_id = update.get("update_id")
        if isinstance(upd_id, int):
            if new_last_id is None or upd_id > new_last_id:
                new_last_id = upd_id

        message = update.get("message") or update.get("channel_post") or {}
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = chat.get("id")

        if not chat_id or not text:
            continue

        if text == "/start":
            welcome = (
                "Welcome! This is the football alerts bot.\n"
                "You will receive match alerts here when the configured rules are triggered."
            )
            send_telegram(bot_token, str(chat_id), welcome)
        elif text in ("/alerts_on", "/start_alerts"):
            if current_enabled:
                send_telegram(
                    bot_token,
                    str(chat_id),
                    "Alerts are already ON. Live matches are being monitored.",
                )
            else:
                current_enabled = True
                send_telegram(
                    bot_token,
                    str(chat_id),
                    "Alerts have been ENABLED. Live matches will be monitored (including outside the schedule window).",
                )
        elif text in ("/alerts_off", "/stop_alerts"):
            if not current_enabled:
                send_telegram(
                    bot_token,
                    str(chat_id),
                    "Alerts are already OFF. Send /alerts_on to enable.",
                )
            else:
                current_enabled = False
                send_telegram(
                    bot_token,
                    str(chat_id),
                    "Alerts have been PAUSED. The bot will not poll matches until you send /alerts_on.",
                )
        elif text == "/status":
            alert_state = "ON – you will receive match alerts when rules trigger." if current_enabled else "OFF – send /alerts_on to enable."
            lines = [
                "Bot status",
                "──────────",
                f"Alerts: {alert_state}",
            ]
            if in_schedule_window is not None:
                lines.append("Schedule window: " + ("active" if in_schedule_window else "inactive"))
            send_telegram(bot_token, str(chat_id), "\n".join(lines))

    return new_last_id, current_enabled
