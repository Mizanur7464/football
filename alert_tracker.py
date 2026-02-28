"""
Prevent duplicate alerts per match/rule and log all triggered alerts.
"""

import logging
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)


class AlertTracker:
    def __init__(self, log_dir: str = "logs"):
        self._sent: Set[tuple[int, str]] = set()  # (fixture_id, rule_key)
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = self._log_dir / "alerts.log"

    def already_sent(self, fixture_id: int, rule_key: str) -> bool:
        return (fixture_id, rule_key) in self._sent

    def mark_sent(self, fixture_id: int, rule_key: str) -> None:
        self._sent.add((fixture_id, rule_key))

    def log_alert(
        self,
        fixture_id: int,
        rule_key: str,
        market_name: str,
        home_team: str,
        away_team: str,
        minute: int,
        score: str,
    ) -> None:
        line = (
            f"fixture_id={fixture_id} rule={rule_key} market={market_name} "
            f"match={home_team} vs {away_team} minute={minute} score={score}\n"
        )
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            logger.warning("Could not write alert log: %s", e)
        logger.info("Alert triggered: %s %s %s vs %s", rule_key, market_name, home_team, away_team)
