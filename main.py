"""
Telegram relay (Telethon user session): source bot/chat -> optional transform ->
route by Bet Type (or other rules) -> exactly one target group per message.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

import yaml
from dotenv import load_dotenv
from telethon import events
from telethon.errors import RPCError
from telethon.sync import TelegramClient
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import ChatInviteAlready

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _parse_chat_ids(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def _apply_transform(text: str, transform_cfg: dict) -> str:
    out = text or ""
    for rule in (transform_cfg.get("replace") or []):
        frm = str((rule or {}).get("from") or "")
        to = str((rule or {}).get("to") or "")
        if frm:
            out = out.replace(frm, to)
    prepend_text = str(transform_cfg.get("prepend_text") or "").strip()
    append_text = str(transform_cfg.get("append_text") or "").strip()
    if prepend_text:
        out = f"{prepend_text}\n{out}"
    if append_text:
        out = f"{out}\n{append_text}"
    return out.strip()


def _template_render(template: str, data: dict[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        key = (m.group(1) or "").strip()
        return str(data.get(key, ""))

    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", repl, template)


def _parse_opportunity_message(text: str) -> dict[str, str]:
    """
    Parse the common 'Opportunity' message format into fields.
    Missing fields are returned as empty strings.
    """
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    # keep only non-empty for some lookups, but preserve original for stats scanning
    non_empty = [ln.strip() for ln in lines if ln.strip()]

    data: dict[str, str] = {
        "bet_type": "",
        "league": "",
        "team_line": "",
        "mins_played": "",
        "pre_match_probability": "",
        "live_odds": "",
        "goals": "",
        "attacks": "",
        "dang_att": "",
        "shots": "",
        "sot": "",
        "corners": "",
        "possession": "",
        "player_name": "",
        "num_fouls": "",
        "foul_times": "",
        "referee": "",
    }

    # Header like: "📈 1H Goal (Yes) Opportunity"
    if non_empty:
        data["bet_type"] = re.sub(r"^\s*[^\w]*\s*", "", non_empty[0]).strip()

    # Team line: starts with 🆚
    for ln in non_empty:
        if "🆚" in ln:
            data["team_line"] = ln.replace("🆚", "").strip()
            break

    # League line: starts with 🏆
    for ln in non_empty:
        if "🏆" in ln:
            data["league"] = ln.replace("🏆", "").strip()
            break

    # Minutes played: starts with ⏲️
    for ln in non_empty:
        if "Mins Played" in ln or "⏲️" in ln:
            # examples: "⏲️ 19 Mins Played"
            m = re.search(r"(\d+)\s*Mins\s*Played", ln, flags=re.I)
            if m:
                data["mins_played"] = m.group(1).strip()
            else:
                data["mins_played"] = ln.replace("⏲️", "").strip()
            break
        # examples: "51' Played"
        m2 = re.search(r"(\d+)\s*'?\s*Played\b", ln, flags=re.I)
        if m2:
            data["mins_played"] = m2.group(1).strip()
            break

    # Live odds: starts with 🔥 or "LIVE Odds:"
    for ln in non_empty:
        if "Live Odds" in ln or "LIVE Odds" in ln or "🔥" in ln:
            m = re.search(r"Live\s*Odds:\s*([0-9]+(?:\.[0-9]+)?)", ln, flags=re.I)
            if m:
                data["live_odds"] = m.group(1).strip()
            else:
                data["live_odds"] = ln.replace("🔥", "").strip().replace("Live Odds:", "").strip()
            break

    # Pre-match probability: starts with 🎯
    for ln in non_empty:
        if "Pre-Match Probability" in ln or "🎯" in ln:
            m = re.search(r"Pre-?Match\s*Probability:\s*([0-9]+(?:\.[0-9]+)?%?)", ln, flags=re.I)
            if m:
                data["pre_match_probability"] = m.group(1).strip()
            else:
                data["pre_match_probability"] = (
                    ln.replace("🎯", "")
                    .strip()
                    .replace("Pre-Match Probability:", "")
                    .strip()
                )
            break

    # Stats lines: look for "Attacks:", "Dang. Attacks:", etc.
    def pick(label: str, key: str, aliases: list[str]) -> None:
        for ln in non_empty:
            for a in aliases:
                if ln.lower().startswith(a.lower()):
                    val = ln.split(":", 1)[1].strip() if ":" in ln else ln.strip()
                    data[key] = val
                    return

    pick("Goals", "goals", ["Goals:", "▪️ Goals:", "■ Goals:"])
    pick("Attacks", "attacks", ["Attacks:", "▪️ Attacks:", "■ Attacks:"])
    pick("Dang. Att", "dang_att", ["Dang. Attacks:", "▪️ Dang. Attacks:", "■ Dang. Attacks:"])
    pick("Shots", "shots", ["Shots:", "▪️ Shots:", "■ Shots:"])
    pick("SoT", "sot", ["SoT:", "▪️ SoT:", "■ SoT:"])
    pick("Corners", "corners", ["Corners:", "▪️ Corners:", "■ Corners:"])
    pick("Possession", "possession", ["Possession:", "▪️ Possession:", "■ Possession:"])

    # Yellow card / foul alert extras
    for ln in non_empty:
        if "👕" in ln:
            data["player_name"] = ln.replace("👕", "").strip()
            break

    for ln in non_empty:
        # examples: "❌ 3 Fouls, No Card"
        m = re.search(r"(\d+)\s*Fouls?\b", ln, flags=re.I)
        if m:
            data["num_fouls"] = m.group(1).strip()
            break

    for ln in non_empty:
        if "Foul Times" in ln or "▶️" in ln:
            # examples: "▶️ Foul Times: 35, 46, 51"
            val = ln.replace("▶️", "").strip()
            val = val.split(":", 1)[1].strip() if ":" in val else val
            data["foul_times"] = val
            break

    for ln in non_empty:
        if "Ref:" in ln or "Referee" in ln or "🙋" in ln:
            # examples: "Ref: Edina Alves Batista"
            val = re.sub(r"^.*Ref(?:eree)?:", "", ln, flags=re.I).strip()
            data["referee"] = val if val else ln.replace("🙋🏾‍♂️", "").strip()
            break

    return data


def _apply_route_format(text: str, route: dict | None) -> str:
    if not route or not isinstance(route, dict):
        return text
    fmt = route.get("format") or {}
    if not isinstance(fmt, dict) or not fmt:
        return text

    template = str(fmt.get("template") or "").strip()
    if not template:
        return text

    data = _parse_opportunity_message(text)
    override_bet_type = str(fmt.get("bet_type") or "").strip()
    if override_bet_type:
        data["bet_type"] = override_bet_type

    out = _template_render(template, data).strip()
    return out if out else text


def _invite_hash_from_target(raw: str) -> str | None:
    s = raw.strip()
    m = re.search(r"(?:https?://)?t\.me/\+([A-Za-z0-9_-]+)", s, flags=re.I)
    if m:
        return m.group(1)
    m2 = re.fullmatch(r"\+([A-Za-z0-9_-]+)", s)
    if m2:
        return m2.group(1)
    return None


def _resolve_target_peer(client: TelegramClient, chosen: str) -> int | str:
    """Numeric id, @username, or t.me/+invite -> peer id for send_message."""
    s = chosen.strip()
    h = _invite_hash_from_target(s)
    if h:
        res = client(CheckChatInviteRequest(h))
        if isinstance(res, ChatInviteAlready):
            return res.chat.id
        upd = client(ImportChatInviteRequest(h))
        return upd.chats[0].id
    if s.startswith("@"):
        return s[1:]
    try:
        return int(s)
    except ValueError:
        return s.lstrip("@")


def _make_peer_resolver(client: TelegramClient) -> Callable[[str], int | str]:
    cache: dict[str, int | str] = {}

    def resolve(chosen: str) -> int | str:
        key = chosen.strip()
        if key not in cache:
            cache[key] = _resolve_target_peer(client, key)
        return cache[key]

    return resolve


def _warmup_targets(client: TelegramClient, relay_cfg: dict, extra_ids: list[str]) -> Callable[[str], int | str]:
    resolve = _make_peer_resolver(client)
    seen: set[str] = set()

    def touch(raw: Any) -> None:
        for part in _parse_chat_ids(raw):
            if not part or part in seen:
                continue
            seen.add(part)
            resolve(part)

    for route in relay_cfg.get("routes") or []:
        if isinstance(route, dict):
            touch(route.get("target_chat_id") or route.get("target"))
    touch(relay_cfg.get("fallback_target_chat_id"))
    for x in extra_ids:
        touch(x)
    return resolve


def _peer_from_chosen(chosen: str, resolve_peer: Callable[[str], int | str] | None) -> int | str:
    if resolve_peer:
        return resolve_peer(chosen)
    try:
        return int(chosen)
    except ValueError:
        return str(chosen).lstrip("@")


def _route_allows_source(route: dict, source_chat_id: int) -> bool:
    """If route has source_chat_ids, only those chats may use this route; else any allowed global source."""
    raw = route.get("source_chat_ids")
    if raw is None or raw == "":
        return True
    allowed = _parse_chat_ids(raw)
    if not allowed:
        return True
    for s in allowed:
        try:
            if int(s.strip()) == source_chat_id:
                return True
        except ValueError:
            continue
    return False


def _to_chats_filter(raw_ids: list[str]) -> list[int | str]:
    out: list[int | str] = []
    for s in raw_ids:
        if s.startswith("@"):
            out.append(s[1:])
            continue
        try:
            out.append(int(s))
        except ValueError:
            out.append(s)
    return out


def _route_target_peers(
    text: str,
    relay_cfg: dict,
    default_target_peers: list[int | str],
    default_target_ids: list[str],
    resolve_peer: Callable[[str], int | str] | None = None,
    source_chat_id: int | None = None,
) -> tuple[list[int | str], list[str], dict | None]:
    routes = relay_cfg.get("routes") or []
    if not routes:
        return default_target_peers, default_target_ids, None

    for route in routes:
        if not isinstance(route, dict):
            continue
        if source_chat_id is not None and not _route_allows_source(route, source_chat_id):
            continue
        match = route.get("match") or {}
        contains = match.get("contains") or []
        regex = str(match.get("regex") or "").strip()

        ok = False
        if contains:
            ok = any(str(s).lower() in text.lower() for s in contains if str(s))
        if not ok and regex:
            try:
                ok = re.search(regex, text, flags=re.I | re.DOTALL) is not None
            except re.error as e:
                logger.warning("Invalid route regex (%s): %s", route.get("name"), e)
                ok = False

        if not ok:
            continue

        raw_target = route.get("target_chat_id") or route.get("target") or ""
        ids = _parse_chat_ids(raw_target)
        if not ids:
            continue

        chosen = ids[0]
        peer = _peer_from_chosen(chosen, resolve_peer)
        return [peer], [str(chosen)], route

    fallback = relay_cfg.get("fallback_target_chat_id")
    if fallback:
        ids = _parse_chat_ids(fallback)
        if ids:
            chosen = ids[0]
            peer = _peer_from_chosen(chosen, resolve_peer)
            return [peer], [str(chosen)], None

    return [], [], None


def _relay_text_passes_filter(text: str, relay_cfg: dict) -> bool:
    """Optional gates on raw incoming text (same chat: keep only what you want)."""
    flt = relay_cfg.get("filter")
    if not isinstance(flt, dict) or not flt:
        return True

    require_contains = flt.get("require_any_contains") or []
    if require_contains:
        if not any(str(s).lower() in text.lower() for s in require_contains if str(s)):
            return False

    require_regex = str(flt.get("require_regex") or "").strip()
    if require_regex:
        try:
            if re.search(require_regex, text, flags=re.I | re.DOTALL) is None:
                return False
        except re.error as e:
            logger.warning("Invalid filter.require_regex: %s", e)
            return False

    for s in flt.get("skip_if_any_contains") or []:
        if str(s) and str(s).lower() in text.lower():
            return False

    skip_regex = str(flt.get("skip_if_regex") or "").strip()
    if skip_regex:
        try:
            if re.search(skip_regex, text, flags=re.I | re.DOTALL) is not None:
                return False
        except re.error as e:
            logger.warning("Invalid filter.skip_if_regex: %s", e)
            return False

    return True


def main() -> None:
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        logger.error("config.yaml not found. Copy config.example.yaml and fill settings.")
        sys.exit(1)

    config = load_config(str(config_path))
    th_cfg = config.get("telethon") or {}
    relay_cfg = config.get("relay") or {}
    transform_cfg = config.get("transform") or {}

    api_id = int(os.getenv("TELEGRAM_API_ID") or th_cfg.get("api_id") or 0)
    api_hash = str(os.getenv("TELEGRAM_API_HASH") or th_cfg.get("api_hash") or "").strip()
    session_name = str(th_cfg.get("session_name") or os.getenv("TELEGRAM_SESSION_NAME") or "relay_session").strip()
    session_path = Path(__file__).parent / session_name

    if not api_id or not api_hash:
        logger.error("Set TELEGRAM_API_ID and TELEGRAM_API_HASH (or telethon.api_id/api_hash).")
        sys.exit(1)

    target_raw = (
        os.getenv("TARGET_CHAT_ID")
        or os.getenv("TELEGRAM_CHAT_ID")
        or relay_cfg.get("target_chat_id")
        or ""
    )
    target_chat_ids = _parse_chat_ids(target_raw)
    env_sources = str(os.getenv("SOURCE_CHAT_IDS") or "").strip()
    if env_sources:
        source_chat_ids = _parse_chat_ids(env_sources)
        logger.info(
            "Using SOURCE_CHAT_IDS from .env (overrides config.yaml): %s",
            source_chat_ids,
        )
    else:
        source_chat_ids = _parse_chat_ids(relay_cfg.get("source_chat_ids"))
        logger.info("Using relay.source_chat_ids from config.yaml: %s", source_chat_ids)
    control_raw = str(os.getenv("CONTROL_CHAT_ID") or relay_cfg.get("control_chat_id") or "").strip()

    routes = relay_cfg.get("routes") or []
    if not routes and not target_chat_ids:
        logger.error("Set relay.routes (Bet Type routing) or relay.target_chat_id / TARGET_CHAT_ID.")
        sys.exit(1)

    placeholder_markers = {"YOUR_SOURCE_CHAT_ID", "your_source_chat_id"}
    discover_mode = (not source_chat_ids) or any(x.strip() in placeholder_markers for x in source_chat_ids)
    if discover_mode:
        logger.warning(
            "DISCOVER mode: incoming messages will log chat_id. Set SOURCE_CHAT_IDS when done."
        )

    target_peers: list[int | str] = []
    for raw in target_chat_ids:
        try:
            target_peers.append(int(raw))
        except ValueError:
            target_peers.append(raw.lstrip("@"))

    control_peer: int | None = None
    if control_raw:
        try:
            control_peer = int(control_raw)
        except ValueError:
            control_peer = None

    phone = os.getenv("TELEGRAM_PHONE", "").strip() or None
    password = os.getenv("TELEGRAM_2FA", "").strip() or None

    session_file = session_path.with_suffix(".session")
    if not session_file.exists() and not phone:
        logger.error(
            "No session file (%s). Set TELEGRAM_PHONE for first login or copy .session here.",
            session_file.name,
        )
        sys.exit(1)

    chats = _to_chats_filter(source_chat_ids) if not discover_mode else []
    relay_enabled = True

    client = TelegramClient(str(session_path), api_id, api_hash)
    client.start(phone=phone, password=password)
    me = client.get_me()
    logger.info("Logged in as user id=%s @%s", me.id, me.username or "—")

    resolve_peer: Callable[[str], int | str] | None = None
    if not discover_mode:
        try:
            resolve_peer = _warmup_targets(client, relay_cfg, target_chat_ids)
        except Exception as e:
            logger.error("Could not resolve a target (invite link or id): %s", e)
            sys.exit(1)

    target_peers_resolved: list[int | str] = []
    if resolve_peer:
        for raw in target_chat_ids:
            target_peers_resolved.append(resolve_peer(raw))
    else:
        target_peers_resolved = target_peers

    def command_allowed(event: events.NewMessage.Event, my_id: int) -> bool:
        if control_peer is not None:
            return int(event.chat_id) == control_peer
        return event.is_private and int(event.chat_id) == my_id

    @client.on(events.NewMessage(func=lambda e: e.is_private))
    async def private_chat_id_cmd(event: events.NewMessage.Event) -> None:
        """প্রাইভেট চ্যাটে আপনি /chatid পাঠালে (outgoing) বা বট রিপ্লাই করলে — সেই চ্যাটের id."""
        if not event.is_private:
            return
        raw = (event.message.message or event.message.text or "").strip()
        if raw.lower() not in ("/chatid", "/sourceid"):
            return
        if int(event.chat_id) == me.id:
            return
        ent = await event.get_chat()
        uname = getattr(ent, "username", None) or "—"
        label = getattr(ent, "first_name", None) or getattr(ent, "title", None) or "—"
        await event.reply(
            "config.yaml → relay.source_chat_ids এ বসান (ইংরেজি সংখ্যা):\n\n"
            f'    - "{event.chat_id}"\n\n'
            f"username: @{uname}\n"
            f"name: {label}"
        )
        logger.info("Sent /chatid reply for chat_id=%s", event.chat_id)

    @client.on(events.NewMessage(chats=chats) if chats else events.NewMessage(incoming=True))
    async def relay_handler(event: events.NewMessage.Event) -> None:
        nonlocal relay_enabled
        text = (event.message.message or event.message.text or "").strip()
        if not text:
            return

        if text.lower() in ("/chatid", "/sourceid"):
            return

        if discover_mode:
            title = getattr(event.chat, "title", None) or getattr(event.chat, "username", None) or "—"
            logger.info("DISCOVER: chat_id=%s title=%s text=%s", event.chat_id, title, text[:120])

        if text.startswith("/"):
            if not command_allowed(event, me.id):
                return
            cmd = text.split()[0].lower()
            if cmd == "/start":
                await client.send_message(
                    event.chat_id,
                    "Relay running.\n/status\n/relay_on\n/relay_off\n\n"
                    "অন্য বটের chat id: সেই বটের চ্যাটে /chatid পাঠান।",
                )
            elif cmd == "/status":
                await client.send_message(
                    event.chat_id,
                    f"Relay: {'ON' if relay_enabled else 'OFF'}\n"
                    f"Sources: {source_chat_ids}\n"
                    f"Routes: {len(routes)} rule(s)\n"
                    f"Default targets: {target_chat_ids}",
                )
            elif cmd == "/relay_on":
                relay_enabled = True
                await client.send_message(event.chat_id, "Relay ON.")
            elif cmd == "/relay_off":
                relay_enabled = False
                await client.send_message(event.chat_id, "Relay OFF.")
            return

        if not relay_enabled:
            return
        if discover_mode:
            return

        if not _relay_text_passes_filter(text, relay_cfg):
            logger.info("Filtered out (relay.filter) chat_id=%s", event.chat_id)
            return

        pre_text = _apply_transform(text, transform_cfg)
        peers, ids, matched_route = _route_target_peers(
            pre_text,
            relay_cfg,
            target_peers_resolved,
            target_chat_ids,
            resolve_peer,
            source_chat_id=int(event.chat_id),
        )
        if not peers:
            logger.info(
                "No route matched; skipped chat_id=%s msg_id=%s preview=%r",
                event.chat_id,
                event.message.id,
                (pre_text[:200] + "…") if len(pre_text) > 200 else pre_text,
            )
            return

        out_text = _apply_route_format(pre_text, matched_route)

        for peer in peers:
            try:
                await client.send_message(peer, out_text)
            except RPCError as e:
                logger.warning("send_message failed (target=%s): %s", peer, e)
        logger.info("Relayed from chat_id=%s to targets=%s", event.chat_id, ids)

    if discover_mode:
        logger.info("DISCOVER mode (no relay). Default targets=%s", target_chat_ids)
    else:
        logger.info("Listening sources=%s", chats)

    try:
        client.run_until_disconnected()
    except KeyboardInterrupt:
        logger.info("Stopped.")


if __name__ == "__main__":
    main()
