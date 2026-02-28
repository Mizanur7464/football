"""
Trigger rules: check fixture + odds against config rules.
"""

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _fixture_minute(fixture: dict) -> int:
    """Elapsed minute from fixture status."""
    status = (fixture.get("fixture") or {}).get("status") or {}
    return int(status.get("elapsed") or 0)


def _fixture_score(fixture: dict) -> tuple[int, int]:
    """(home_goals, away_goals)."""
    g = (fixture.get("goals") or {})
    return (int(g.get("home") or 0), int(g.get("away") or 0))


def _find_odds_value(
    odds_response: list[dict],
    market_pattern: str,
    bookmaker_name: Optional[str] = None,
) -> Optional[float]:
    """
    Find latest odds value for a market name matching market_pattern.
    If bookmaker_name is set, only use odds from that bookmaker.
    odds_response: list of {league, fixture, bookmakers: [{name, bets: [{name, values: [{odd or value}]}]}]}
    """
    for fixture_odds in odds_response:
        for book in (fixture_odds.get("bookmakers") or []):
            book_name = (book.get("name") or "").strip()
            if bookmaker_name and book_name and book_name.lower() != bookmaker_name.lower():
                continue
            for bet in (book.get("bets") or []):
                name = (bet.get("name") or "").strip()
                if not name:
                    continue
                if not re.search(market_pattern, name, re.I):
                    continue
                values = bet.get("values") or []
                for v in values:
                    try:
                        raw = v.get("odd") or v.get("value")
                        if raw is None:
                            continue
                        return float(raw)
                    except (TypeError, ValueError):
                        pass
    return None


def _odds_for_over_05_first_half(
    odds_response: list[dict],
    bookmaker_name: Optional[str],
) -> Optional[float]:
    patterns = [
        r"over\s*0\.5\s*.*(?:1st\s*half|first\s*half|1st\s*h)",
        r"goals.*1st\s*half.*over\s*0\.5",
    ]
    for p in patterns:
        v = _find_odds_value(odds_response, p, bookmaker_name)
        if v is not None:
            return v
    return _find_odds_value(odds_response, r"over\s*0\.5", bookmaker_name)


def _odds_for_btts_yes(
    odds_response: list[dict],
    bookmaker_name: Optional[str],
) -> Optional[float]:
    return _find_odds_value(
        odds_response,
        r"both\s*teams\s*to\s*score|btts.*yes|yes.*btts",
        bookmaker_name,
    ) or _find_odds_value(odds_response, r"btts", bookmaker_name)


def _odds_for_over_05_full(
    odds_response: list[dict],
    bookmaker_name: Optional[str],
) -> Optional[float]:
    # Prefer full match / 90 min
    v = _find_odds_value(odds_response, r"over\s*0\.5.*(?:full|90|match)", bookmaker_name)
    if v is not None:
        return v
    return _find_odds_value(odds_response, r"over\s*0\.5", bookmaker_name)


def check_over_05_first_half(
    fixture: dict,
    odds_response: list[dict],
    rule: dict,
    bookmaker_name: Optional[str],
) -> bool:
    """Over 0.5 First Half: odds >= min_odds, minute < 45, score 0-0."""
    if not rule.get("enabled", True):
        return False
    minute = _fixture_minute(fixture)
    if minute >= (rule.get("max_minute") or 45):
        return False
    effective_bookmaker = rule.get("bookmaker") or bookmaker_name
    home, away = _fixture_score(fixture)
    if rule.get("require_score_0_0", True) and (home != 0 or away != 0):
        return False
    odds = _odds_for_over_05_first_half(odds_response, effective_bookmaker)
    if odds is None:
        return False
    return odds >= (rule.get("min_odds") or 1.50)


def check_btts_yes(
    fixture: dict,
    odds_response: list[dict],
    rule: dict,
    bookmaker_name: Optional[str],
) -> bool:
    """BTTS Yes: odds >= min_odds, 1'-90', market alive (at least one team 0)."""
    if not rule.get("enabled", True):
        return False
    effective_bookmaker = rule.get("bookmaker") or bookmaker_name
    minute = _fixture_minute(fixture)
    if minute < (rule.get("min_minute") or 1) or minute > (rule.get("max_minute") or 90):
        return False
    home, away = _fixture_score(fixture)
    if rule.get("require_market_alive", True):
        if home > 0 and away > 0:
            return False  # both scored = market dead
    odds = _odds_for_btts_yes(odds_response, effective_bookmaker)
    if odds is None:
        return False
    return odds >= (rule.get("min_odds") or 2.00)


def check_over_05_full_at_60(
    fixture: dict,
    odds_response: list[dict],
    rule: dict,
    bookmaker_name: Optional[str],
) -> bool:
    """Over 0.5 Full at 60': minute >= 60, score 0-0, odds >= min_odds."""
    if not rule.get("enabled", True):
        return False
    effective_bookmaker = rule.get("bookmaker") or bookmaker_name
    minute = _fixture_minute(fixture)
    if minute < (rule.get("from_minute") or 60):
        return False
    home, away = _fixture_score(fixture)
    if rule.get("require_score_0_0", True) and (home != 0 or away != 0):
        return False
    odds = _odds_for_over_05_full(odds_response, effective_bookmaker)
    if odds is None:
        return False
    return odds >= (rule.get("min_odds") or 1.80)


RULE_CHECKERS = {
    "over_05_first_half": (check_over_05_first_half, "Over 0.5 goals – 1st half"),
    "btts_yes": (check_btts_yes, "Both Teams To Score – Yes"),
    "over_05_full_at_60": (check_over_05_full_at_60, "Over 0.5 goals – full match"),
}


def run_rules(fixture: dict, odds_response: list[dict], rules_config: dict) -> list[tuple[str, str]]:
    """
    Run all configured rules. Returns list of (rule_key, market_display_name) that triggered.
    """
    triggered = []
    bookmaker_name = (rules_config or {}).get("bookmaker")
    for rule_key, (check_fn, market_name) in RULE_CHECKERS.items():
        rule = (rules_config or {}).get(rule_key) or {}
        if not rule.get("enabled", True):
            continue
        try:
            if check_fn(fixture, odds_response, rule, bookmaker_name):
                triggered.append((rule_key, rule.get("market_name") or market_name))
        except Exception as e:
            logger.warning("Rule %s error: %s", rule_key, e)
    return triggered
