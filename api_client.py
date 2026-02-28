"""
API-Football client for live fixtures and odds.
Docs: https://www.api-football.com/documentation-v3
"""

import logging
import time
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


class APIFootballError(Exception):
    """API request or response error."""
    pass


class APIFootballClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": "v3.football.api-sports.io",
            "x-apisports-key": api_key,
        })

    def _request(self, endpoint: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        params = params or {}
        try:
            r = self.session.get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            err = data.get("errors")
            if err and isinstance(err, dict):
                raise APIFootballError(str(err))
            return data
        except requests.RequestException as e:
            logger.warning("API request failed: %s", e)
            raise APIFootballError(str(e)) from e

    def get_live_fixtures(self, league_ids: list[int]) -> list[dict]:
        """Fetch live fixtures for given leagues (one request per league)."""
        all_fixtures = []
        for league_id in league_ids:
            try:
                data = self._request(
                    "fixtures",
                    {"league": league_id, "season": self._current_season()}
                )
                response = data.get("response") or []
                for f in response:
                    status = (f.get("fixture") or {}).get("status") or {}
                    short = (status.get("short") or "").upper()
                    if short in ("1H", "2H", "HT", "ET", "P", "LIVE"):
                        all_fixtures.append(f)
                time.sleep(0.2)
            except APIFootballError as e:
                logger.warning("League %s: %s", league_id, e)
        return all_fixtures

    def get_fixture_odds(self, fixture_id: int) -> list[dict]:
        """Get odds for a fixture (all bookmakers/bets)."""
        try:
            data = self._request("odds", {"fixture": fixture_id})
            return data.get("response") or []
        except APIFootballError:
            return []

    def _current_season(self) -> int:
        from datetime import datetime
        now = datetime.utcnow()
        return now.year if now.month >= 7 else now.year - 1
