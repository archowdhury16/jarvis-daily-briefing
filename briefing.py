#!/usr/bin/env python3
"""Jarvis Daily Briefing — generate a self-contained HTML artifact with
audio-reactive 3D orb that reads you a Jarvis-style spoken summary of
weather, news, and your sprint board.

  python briefing.py --tickets tickets.json --open

See README.md for full setup and CLI options.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx


REPO_ROOT = Path(__file__).resolve().parent
TEMPLATE_PATH = REPO_ROOT / "templates" / "briefing.html"


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Config:
    city: str = "Bangalore"
    lat: float = 12.97
    lon: float = 77.59
    timezone: str = "Asia/Kolkata"

    rss_url: str = "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en"
    max_news: int = 5

    voice_id: str = "JBFqnCBsd6RMkjVDRZzb"      # George
    model_id: str = "eleven_turbo_v2_5"
    voice_stability: float = 0.5
    voice_similarity: float = 0.75
    el_key_source: str = "keychain"             # "keychain" | "env"
    el_keychain_service: str = "elevenlabs"
    el_keychain_account: str = "default"

    jira_host: str = ""
    jira_email: str = ""
    jira_jql: str = (
        "assignee = currentUser() AND sprint in openSprints() "
        "AND statusCategory != Done ORDER BY priority DESC, updated DESC"
    )
    jira_max: int = 50
    jira_key_source: str = "keychain"
    jira_keychain_service: str = "atlassian"
    jira_keychain_account: str = "default"

    honorific: str = "sir"

    use_system_trust: bool = False

    @classmethod
    def load(cls, path: Path | None) -> "Config":
        cfg = cls()
        if path and path.exists():
            data = tomllib.loads(path.read_text())
            loc = data.get("location", {})
            cfg.city = loc.get("city", cfg.city)
            cfg.lat = float(loc.get("lat", cfg.lat))
            cfg.lon = float(loc.get("lon", cfg.lon))
            cfg.timezone = loc.get("timezone", cfg.timezone)

            news = data.get("news", {})
            cfg.rss_url = news.get("rss_url", cfg.rss_url)
            cfg.max_news = int(news.get("max_items", cfg.max_news))

            el = data.get("elevenlabs", {})
            cfg.voice_id = el.get("voice_id", cfg.voice_id)
            cfg.model_id = el.get("model_id", cfg.model_id)
            cfg.voice_stability = float(el.get("stability", cfg.voice_stability))
            cfg.voice_similarity = float(el.get("similarity_boost", cfg.voice_similarity))
            cfg.el_key_source = el.get("key_source", cfg.el_key_source)
            cfg.el_keychain_service = el.get("keychain_service", cfg.el_keychain_service)
            cfg.el_keychain_account = el.get("keychain_account", cfg.el_keychain_account)

            jira = data.get("jira", {})
            cfg.jira_host = jira.get("host", cfg.jira_host)
            cfg.jira_email = jira.get("email", cfg.jira_email)
            cfg.jira_jql = jira.get("jql", cfg.jira_jql)
            cfg.jira_max = int(jira.get("max_results", cfg.jira_max))
            cfg.jira_key_source = jira.get("key_source", cfg.jira_key_source)
            cfg.jira_keychain_service = jira.get("keychain_service", cfg.jira_keychain_service)
            cfg.jira_keychain_account = jira.get("keychain_account", cfg.jira_keychain_account)

            cfg.honorific = data.get("honorific", {}).get("title", cfg.honorific)
        return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Network setup — corporate proxy support
# ─────────────────────────────────────────────────────────────────────────────


def setup_tls(use_system_trust: bool) -> Any:
    """Return an httpx `verify` value. If `truststore` is available and the
    user opted in, use the system trust store (handles TLS-inspecting proxies
    like ZScaler that re-sign every cert with a corporate root)."""
    if use_system_trust:
        try:
            import ssl
            import truststore
            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            print("warning: truststore not installed; falling back to default verify", file=sys.stderr)
    bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if bundle and Path(bundle).exists():
        return bundle
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Keychain helper (macOS) — read secrets without putting them in config files
# ─────────────────────────────────────────────────────────────────────────────


def read_keychain(service: str, account: str) -> str:
    if not shutil.which("security"):
        return ""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _git_email() -> str:
    try:
        return subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return ""


def _candidate_accounts(account: str) -> list[str]:
    """Expand the configured `keychain_account` into a list of candidates to try.

    - "auto"      → git email, $USER, "default"
    - "$USER"     → expands to current login username
    - "$EMAIL"    → expands to git config user.email
    - anything else → used as-is (single candidate)

    Lets the same config.toml work across machines / users without edits.
    """
    if not account or account == "auto":
        out = []
        e = _git_email()
        if e:
            out.append(e)
        u = os.environ.get("USER", "")
        if u:
            out.append(u)
        out.append("default")
        return out
    if account == "$USER":
        return [os.environ.get("USER", "default")]
    if account == "$EMAIL":
        return [_git_email() or "default"]
    return [account]


def resolve_secret(source: str, service: str, account: str, env_var: str) -> str:
    """Resolve a secret from env or keychain. The configured *account* may be
    a literal string, a placeholder (`$USER`, `$EMAIL`), or `auto` (try
    several common conventions). Falls back to the env var on any miss."""
    if source == "env":
        return os.environ.get(env_var, "")
    if source == "keychain":
        for candidate in _candidate_accounts(account):
            v = read_keychain(service, candidate)
            if v:
                return v
        return os.environ.get(env_var, "")
    return os.environ.get(env_var, "")


# ─────────────────────────────────────────────────────────────────────────────
# Data sources
# ─────────────────────────────────────────────────────────────────────────────


def fetch_weather(cfg: Config, verify) -> dict:
    r = httpx.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": cfg.lat, "longitude": cfg.lon,
            "current": "temperature_2m,weather_code,relative_humidity_2m,apparent_temperature",
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": cfg.timezone,
        },
        timeout=20, verify=verify,
    )
    r.raise_for_status()
    j = r.json()
    cur, day = j["current"], j["daily"]
    return {
        "temp_c": cur["temperature_2m"],
        "feels_c": cur["apparent_temperature"],
        "humidity": cur["relative_humidity_2m"],
        "code": cur["weather_code"],
        "high_c": day["temperature_2m_max"][0],
        "low_c": day["temperature_2m_min"][0],
    }


def fetch_news(cfg: Config, verify) -> list[dict]:
    r = httpx.get(
        cfg.rss_url, timeout=20, verify=verify,
        headers={"User-Agent": "Mozilla/5.0 (jarvis-daily-briefing)"},
    )
    r.raise_for_status()
    root = ET.fromstring(r.text)
    items = []
    for it in root.findall(".//item")[:cfg.max_news * 2]:
        title = it.findtext("title") or ""
        source = it.findtext("source") or ""
        title = html.unescape(title)
        source = html.unescape(source)
        # Strip trailing " - Source" suffix that Google News appends
        title = re.sub(r"\s*[-–]\s*[^-–]+$", "", title).strip()
        if not title:
            continue
        # Skip exam-result-style noise common in Indian top-stories feed
        low = title.lower()
        if any(s in low for s in ("ssc results", "marks memo", "result link")):
            continue
        items.append({"headline": title, "source": source})
        if len(items) >= cfg.max_news:
            break
    return items


def fetch_jira_via_rest(cfg: Config, verify) -> list[dict]:
    """Atlassian Cloud REST search via Basic Auth.

    Many corporate gateways (Q2 Enterprise, etc.) reject Basic Auth at the
    perimeter — symptom is `WWW-Authenticate: OAuth realm` header on 401.
    In that case, prefer --tickets path/to/tickets.json instead.
    """
    token = resolve_secret(
        cfg.jira_key_source, cfg.jira_keychain_service, cfg.jira_keychain_account,
        "JIRA_API_TOKEN",
    )
    if not (cfg.jira_host and cfg.jira_email and token):
        return []
    auth_b64 = base64.b64encode(f"{cfg.jira_email}:{token}".encode()).decode()
    r = httpx.post(
        f"https://{cfg.jira_host}/rest/api/3/search/jql",
        headers={
            "Authorization": f"Basic {auth_b64}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={
            "jql": cfg.jira_jql,
            "maxResults": cfg.jira_max,
            "fields": ["summary", "status", "priority", "issuetype", "duedate"],
        },
        timeout=30, verify=verify,
    )
    r.raise_for_status()
    out = []
    for issue in r.json().get("issues", []):
        f = issue.get("fields", {}) or {}
        out.append({
            "key": issue.get("key", ""),
            "priority": ((f.get("priority") or {}).get("name") or "None"),
            "status": ((f.get("status") or {}).get("name") or ""),
            "type": ((f.get("issuetype") or {}).get("name") or ""),
            "due": f.get("duedate"),
            "summary": f.get("summary") or "",
            "url": f"https://{cfg.jira_host}/browse/{issue.get('key', '')}",
        })
    return out


def load_tickets_file(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON array of ticket objects")
    required = {"key", "priority", "status", "type", "summary", "url"}
    for i, t in enumerate(data):
        missing = required - set(t)
        if missing:
            raise ValueError(f"{path}: ticket #{i+1} missing keys: {missing}")
        t.setdefault("due", None)
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Prose generation — deterministic Jarvis-style briefing
# ─────────────────────────────────────────────────────────────────────────────


PRIORITY_ORDER = ["Highest", "Critical", "High", "P1", "P2", "Medium", "P3", "Low", "P4", "Trivial", "None"]


def _prio_rank(p: str) -> int:
    try:
        return PRIORITY_ORDER.index(p)
    except ValueError:
        return len(PRIORITY_ORDER)


def _greeting(hour: int) -> str:
    if hour < 12:  return "Good morning"
    if hour < 17:  return "Good afternoon"
    return "Good evening"


def _spell_key(key: str) -> str:
    """Speak 'GD-11065' as 'G D dash one one zero six five' so TTS doesn't
    eat it as one giant word."""
    parts = []
    for ch in key:
        if ch.isalpha():
            parts.append(ch.upper())
        elif ch.isdigit():
            parts.append({"0":"zero","1":"one","2":"two","3":"three","4":"four",
                          "5":"five","6":"six","7":"seven","8":"eight","9":"nine"}[ch])
        elif ch == "-":
            parts.append("dash")
    return " ".join(parts)


def build_prose(cfg: Config, weather: dict, news: list[dict], tickets: list[dict],
                now: datetime) -> str:
    greeting = _greeting(now.hour)
    h = cfg.honorific

    # Time phrase
    time_str = now.strftime("%I:%M %p").lstrip("0")

    # Weather sentence
    wx = (
        f"The time in {cfg.city} is {time_str}, and it's a {round(weather['temp_c'])} "
        f"degrees outside — feeling closer to {round(weather['feels_c'])}, "
        f"with the day expected to climb to {round(weather['high_c'])}."
    )

    # News block
    if news:
        first = news[0]["headline"]
        news_lines = [f"In the headlines, {h}: {first}."]
        for n in news[1:4]:
            news_lines.append(n["headline"].rstrip(".") + ".")
        if len(news) >= 5:
            news_lines.append(f"And closer to home, {news[4]['headline'].lower().rstrip('.')}.")
        news_block = " ".join(news_lines)
    else:
        news_block = ""

    # Tickets
    if not tickets:
        ticket_block = (
            f"Your sprint board is clear today, {h}. A rare and welcome state. "
            f"I'd suggest using the breathing room to clear backlog or take a "
            f"proper break."
        )
    else:
        sorted_tickets = sorted(tickets, key=lambda t: (_prio_rank(t["priority"]),
                                                        0 if t.get("due") else 1))
        n = len(sorted_tickets)
        # Priority shape sentence
        counts: dict[str, int] = {}
        for t in tickets:
            counts[t["priority"]] = counts.get(t["priority"], 0) + 1
        ordered = [(p, counts[p]) for p in PRIORITY_ORDER if counts.get(p)]
        shape = ", ".join(f"{c} {p}" for p, c in ordered) or f"{n} items"

        lead = sorted_tickets[0]
        rest = sorted_tickets[1:]

        lines = [f"On your sprint board, {n} {'item' if n == 1 else 'items'} today, {h}: {shape}."]
        lines.append(
            f"I'd lead with {_spell_key(lead['key'])} — {lead['summary']}. "
            f"It's {lead['priority']} priority, {lead['status'].lower()}"
            + (f", and the due date is {lead['due']}" if lead.get('due') else "")
            + ". I'd open that one first."
        )
        for t in rest[:3]:
            due = f" due {t['due']}" if t.get("due") else ""
            lines.append(
                f"Then {_spell_key(t['key'])}: {t['summary']}, "
                f"{t['priority']} and {t['status'].lower()}{due}."
            )
        if len(rest) > 3:
            lines.append(f"There are {len(rest)-3} more lighter items further down the queue.")
        ticket_block = " ".join(lines)

    closing = f"That's the shape of the day, {h}."

    parts = [f"{greeting}, {h}.", wx]
    if news_block:
        parts.append(news_block)
    parts.append(ticket_block)
    parts.append(closing)
    return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# TTS — ElevenLabs primary, Kokoro fallback
# ─────────────────────────────────────────────────────────────────────────────


def synthesize_elevenlabs(cfg: Config, text: str, verify) -> bytes | None:
    token = resolve_secret(
        cfg.el_key_source, cfg.el_keychain_service, cfg.el_keychain_account,
        "ELEVENLABS_API_KEY",
    )
    if not token:
        print("elevenlabs: no API key (set ELEVENLABS_API_KEY or store in keychain)", file=sys.stderr)
        return None
    try:
        r = httpx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{cfg.voice_id}",
            headers={
                "xi-api-key": token,
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "model_id": cfg.model_id,
                "voice_settings": {
                    "stability": cfg.voice_stability,
                    "similarity_boost": cfg.voice_similarity,
                },
            },
            timeout=180, verify=verify,
        )
    except httpx.HTTPError as exc:
        print(f"elevenlabs: network error: {exc}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"elevenlabs: HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return None
    print(f"✓ ElevenLabs TTS rendered ({len(r.content):,} bytes)", file=sys.stderr)
    return r.content


def synthesize_kokoro(text: str) -> bytes | None:
    try:
        import io
        import numpy as np
        import soundfile as sf
        from kokoro import KPipeline
    except ImportError:
        print("kokoro: not installed (pip install kokoro soundfile)", file=sys.stderr)
        return None
    try:
        pipe = KPipeline(lang_code="a")
        chunks = [a for _, _, a in pipe(text, voice="am_adam", speed=1.0)]
    except Exception as exc:  # noqa: BLE001
        print(f"kokoro: pipeline error: {exc}", file=sys.stderr)
        return None
    if not chunks:
        return None
    combined = np.concatenate(chunks)
    buf = io.BytesIO()
    sf.write(buf, combined, 24000, format="WAV")
    print(f"✓ Kokoro TTS rendered (fallback, {len(buf.getvalue()):,} bytes)", file=sys.stderr)
    return buf.getvalue()


def synthesize_audio(cfg: Config, text: str, verify) -> tuple[bytes, str] | None:
    """Returns (audio_bytes, mime_type) or None if both backends fail."""
    audio = synthesize_elevenlabs(cfg, text, verify)
    if audio:
        return audio, "audio/mpeg"
    audio = synthesize_kokoro(text)
    if audio:
        return audio, "audio/wav"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Artifact builder
# ─────────────────────────────────────────────────────────────────────────────


def build_artifact(cfg: Config, weather: dict, news: list[dict], tickets: list[dict],
                   prose: str, audio: tuple[bytes, str] | None,
                   now: datetime, out_path: Path) -> None:
    audio_b64 = base64.b64encode(audio[0]).decode() if audio else ""
    audio_mime = audio[1] if audio else "audio/mpeg"
    data = {
        "generated_at_iso": now.isoformat(timespec="seconds"),
        "greeting": _greeting(now.hour),
        "time_label": now.strftime("%I:%M %p").lstrip("0"),
        "date_label": now.strftime("%A, %d %B %Y"),
        "city": cfg.city,
        "honorific": cfg.honorific,
        "weather": weather,
        "news": news,
        "tickets": tickets,
        "prose": prose,
        "audio_b64": audio_b64,
        "audio_mime": audio_mime,
    }
    template = TEMPLATE_PATH.read_text()
    # Inject as a JS object literal (template uses `const data = __INJECT_DATA__;`),
    # not as a string passed to JSON.parse — embedding a serialized object literal
    # directly is both correct and faster than parsing a string at load time.
    final = template.replace("__INJECT_DATA__", json.dumps(data))
    out_path.write_text(final)
    print(f"✓ briefing.html written ({out_path.stat().st_size:,} bytes) -> {out_path}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate a self-contained daily briefing artifact (HTML + embedded audio).",
    )
    ap.add_argument("--config", type=Path, default=Path("config.toml"),
                    help="Path to config TOML (default: config.toml)")
    ap.add_argument("--tickets", type=Path,
                    help="Path to a JSON file with ticket data (overrides Jira REST fetch)")
    ap.add_argument("--no-tickets", action="store_true",
                    help="Skip the sprint board section entirely")
    ap.add_argument("--out", type=Path, default=Path("briefing.html"),
                    help="Output HTML path (default: briefing.html)")
    ap.add_argument("--open", action="store_true",
                    help="Open the artifact in the default browser when done")
    ap.add_argument("--no-tts", action="store_true",
                    help="Skip TTS synthesis (silent artifact)")
    ap.add_argument("--use-system-trust", action="store_true",
                    help="Use system trust store via the truststore package "
                         "(needed behind TLS-inspecting corporate proxies)")
    # Per-run overrides for common knobs
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--city", type=str)
    ap.add_argument("--voice-id", type=str)
    ap.add_argument("--jira-host", type=str)
    ap.add_argument("--jira-email", type=str)
    ap.add_argument("--jira-token", type=str, help="Pass token directly (otherwise from keychain/env)")
    ap.add_argument("--jql", type=str)
    args = ap.parse_args()

    cfg = Config.load(args.config if args.config.exists() else None)
    if args.lat is not None: cfg.lat = args.lat
    if args.lon is not None: cfg.lon = args.lon
    if args.city: cfg.city = args.city
    if args.voice_id: cfg.voice_id = args.voice_id
    if args.jira_host: cfg.jira_host = args.jira_host
    if args.jira_email: cfg.jira_email = args.jira_email
    if args.jira_token: os.environ["JIRA_API_TOKEN"] = args.jira_token
    if args.jql: cfg.jira_jql = args.jql

    verify = setup_tls(args.use_system_trust)

    print("Fetching weather…", file=sys.stderr)
    weather = fetch_weather(cfg, verify)
    print(f"✓ weather: {round(weather['temp_c'])}°C, code {weather['code']}", file=sys.stderr)

    print("Fetching news…", file=sys.stderr)
    news = fetch_news(cfg, verify)
    print(f"✓ {len(news)} headlines", file=sys.stderr)

    if args.no_tickets:
        tickets: list[dict] = []
    elif args.tickets:
        tickets = load_tickets_file(args.tickets)
        print(f"✓ {len(tickets)} tickets from {args.tickets}", file=sys.stderr)
    elif cfg.jira_host:
        tickets = fetch_jira_via_rest(cfg, verify)
        print(f"✓ {len(tickets)} tickets from {cfg.jira_host}", file=sys.stderr)
    else:
        tickets = []
        print("(no ticket source configured — sprint board section will be empty)", file=sys.stderr)

    now = datetime.now(ZoneInfo(cfg.timezone))
    prose = build_prose(cfg, weather, news, tickets, now)
    print(f"✓ prose: {len(prose)} chars / ~{len(prose.split())} words", file=sys.stderr)

    audio = None
    if not args.no_tts:
        audio = synthesize_audio(cfg, prose, verify)
        if not audio:
            print("warning: no audio generated — artifact will be silent", file=sys.stderr)

    build_artifact(cfg, weather, news, tickets, prose, audio, now, args.out)

    if args.open:
        if sys.platform == "darwin":
            subprocess.run(["open", str(args.out)])
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", str(args.out)])
        elif sys.platform == "win32":
            os.startfile(str(args.out))  # type: ignore[attr-defined]

    return 0


if __name__ == "__main__":
    sys.exit(main())
