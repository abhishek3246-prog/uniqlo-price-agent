#!/usr/bin/env python3
"""Uniqlo price-watch agent.

Each run:
  1. Pulls the full Uniqlo catalog from the store's public product API.
  2. Compares it with the snapshot saved on the previous run.
  3. Finds new products, price drops, price increases (and optionally removals).
  4. Asks Claude to write a short, readable alert (falls back to a plain list).
  5. Sends the alert to every notification channel you configured.
  6. Saves the new snapshot for the next run.

Configuration is all through environment variables - see README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

def _load_dotenv(path: Path = Path(__file__).parent / ".env") -> None:
    """For local runs: read KEY=value lines from .env (gitignored). Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

REGION = os.getenv("UNIQLO_REGION", "us")
LANG = os.getenv("UNIQLO_LANG", "en")
# Top-level category ids for the US store: Women, Men, Kids, Baby.
CATEGORY_PATHS = [p.strip() for p in os.getenv("UNIQLO_PATHS", "22210,22211,22212,22213").split(",") if p.strip()]
# Optional: only alert on products whose name contains one of these words (comma separated).
KEYWORDS = [k.strip().lower() for k in os.getenv("WATCH_KEYWORDS", "").split(",") if k.strip()]
# Ignore tiny price moves below this many currency units.
MIN_CHANGE = float(os.getenv("MIN_PRICE_CHANGE", "0.01"))
NOTIFY_REMOVED = os.getenv("NOTIFY_REMOVED", "false").lower() == "true"
STATE_FILE = Path(os.getenv("STATE_FILE", Path(__file__).parent / "state" / "snapshot.json"))

API = f"https://www.uniqlo.com/{REGION}/api/commerce/v5/{LANG}/products"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
PAGE = 100


# ---------------------------------------------------------------- fetch

def _get_json(url: str, retries: int = 4) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** (attempt + 1)
            print(f"  fetch failed ({e}); retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _price(prices: dict, key: str) -> float | None:
    p = (prices or {}).get(key) or {}
    return p.get("value")


def fetch_catalog() -> dict[str, dict]:
    products: dict[str, dict] = {}
    for path in CATEGORY_PATHS:
        offset = 0
        while True:
            q = urllib.parse.urlencode({"path": path, "offset": offset, "limit": PAGE, "httpFailure": "true"})
            data = _get_json(f"{API}?{q}")
            if data.get("status") != "ok":
                raise RuntimeError(f"Uniqlo API error for path {path}: {str(data)[:300]}")
            result = data["result"]
            for it in result["items"]:
                pid = it["productId"]
                base, promo = _price(it.get("prices"), "base"), _price(it.get("prices"), "promo")
                currency = (((it.get("prices") or {}).get("base") or {}).get("currency") or {}).get("symbol", "$")
                products[pid] = {
                    "id": pid,
                    "name": it.get("name", ""),
                    "gender": it.get("genderName", ""),
                    "base": base,
                    "price": promo if promo is not None else base,  # what a shopper pays now
                    "currency": currency,
                    "url": f"https://www.uniqlo.com/{REGION}/{LANG}/products/{pid}/{it.get('priceGroup', '00')}",
                }
            total = result["pagination"]["total"]
            offset += len(result["items"])
            if not result["items"] or offset >= total:
                break
            time.sleep(0.5)  # be polite
        print(f"  category {path}: done ({len(products)} unique products so far)")
    return products


# ---------------------------------------------------------------- diff

def _matches(p: dict) -> bool:
    return not KEYWORDS or any(k in p["name"].lower() for k in KEYWORDS)


def diff(old: dict[str, dict], new: dict[str, dict]) -> dict[str, list]:
    added = [p for pid, p in new.items() if pid not in old and _matches(p)]
    removed = [p for pid, p in old.items() if pid not in new and _matches(p)] if NOTIFY_REMOVED else []
    drops, rises = [], []
    for pid, p in new.items():
        o = old.get(pid)
        if not o or not _matches(p) or p["price"] is None or o["price"] is None:
            continue
        delta = p["price"] - o["price"]
        if abs(delta) < MIN_CHANGE:
            continue
        entry = {**p, "old_price": o["price"], "delta": round(delta, 2)}
        (drops if delta < 0 else rises).append(entry)
    drops.sort(key=lambda e: e["delta"])
    rises.sort(key=lambda e: -e["delta"])
    return {"new": added, "price_drop": drops, "price_increase": rises, "removed": removed}


# ---------------------------------------------------------------- message

def _fmt(v: float | None, cur: str) -> str:
    return "n/a" if v is None else f"{cur}{v:,.2f}"


def plain_report(changes: dict[str, list], limit: int = 25) -> str:
    out = []
    sections = [
        ("price_drop", "Price drops", lambda e: f"{e['name']}: {_fmt(e['old_price'], e['currency'])} -> {_fmt(e['price'], e['currency'])}"),
        ("price_increase", "Price increases", lambda e: f"{e['name']}: {_fmt(e['old_price'], e['currency'])} -> {_fmt(e['price'], e['currency'])}"),
        ("new", "New products", lambda e: f"{e['name']} ({e['gender']}) - {_fmt(e['price'], e['currency'])}"),
        ("removed", "No longer listed", lambda e: e["name"]),
    ]
    for key, title, line in sections:
        items = changes[key]
        if not items:
            continue
        out.append(f"{title} ({len(items)}):")
        for e in items[:limit]:
            out.append(f"  - {line(e)}\n    {e['url']}")
        if len(items) > limit:
            out.append(f"  ...and {len(items) - limit} more")
        out.append("")
    return "\n".join(out).strip()


def ai_report(changes: dict[str, list]) -> str | None:
    """Ask Claude for a short shopper-friendly digest. Returns None if not configured or it fails."""
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        return None
    try:
        import anthropic
    except ImportError:
        print("  anthropic package not installed; using plain report", file=sys.stderr)
        return None

    # Keep the payload bounded: the biggest moves matter most.
    trimmed = {k: v[:60] for k, v in changes.items()}
    prompt = (
        "You write push-notification digests for a shopper who tracks the Uniqlo online store.\n"
        "Below is JSON describing what changed since the last check: new products, price drops, "
        "price increases and (maybe) removed products. Prices are what a shopper pays now; "
        "'old_price' is the previous price and 'delta' the change.\n\n"
        "Write a plain-text digest (no markdown tables, no headings with #) of at most ~1200 characters:\n"
        "- First line: a one-sentence summary with counts.\n"
        "- Then the most noteworthy items: the biggest price drops (with % off), notable new arrivals, "
        "and any price increases. Include each item's URL on its own line under it.\n"
        "- If many items changed, group or summarise the rest instead of listing them all.\n"
        "Do not invent products or prices that are not in the data.\n\n"
        f"Counts: { {k: len(v) for k, v in changes.items()} }\n"
        f"Data:\n{json.dumps(trimmed, ensure_ascii=False)}"
    )
    try:
        client = anthropic.Anthropic()
        resp = client.beta.messages.create(
            model=os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
            max_tokens=16000,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            return None
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return text or None
    except anthropic.APIError as e:
        print(f"  Claude summary failed ({e}); using plain report", file=sys.stderr)
        return None


# ---------------------------------------------------------------- notify

def _post(url: str, body: bytes, headers: dict) -> None:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def sms_text(title: str, changes: dict[str, list], limit: int = 1500) -> str:
    """Compact version for SMS: title plus the top few items, kept under one long text."""
    lines = [title]
    for e in changes["price_drop"][:4]:
        off = f" ({-e['delta'] / e['old_price']:.0%} off)" if e["old_price"] else ""
        lines.append(f"↓ {e['name']} {_fmt(e['old_price'], e['currency'])}→{_fmt(e['price'], e['currency'])}{off} {e['url']}")
    for e in changes["new"][:3]:
        lines.append(f"NEW {e['name']} {_fmt(e['price'], e['currency'])} {e['url']}")
    for e in changes["price_increase"][:2]:
        lines.append(f"↑ {e['name']} {_fmt(e['old_price'], e['currency'])}→{_fmt(e['price'], e['currency'])}")
    out = ""
    for line in lines:
        if len(out) + len(line) + 1 > limit:
            break
        out += line + "\n"
    return out.strip()


def send_sms(text: str) -> bool:
    """Text message through Twilio. Needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, SMS_TO."""
    sid, token = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")
    from_, to = os.getenv("TWILIO_FROM"), os.getenv("SMS_TO")
    if not (sid and token and from_ and to):
        return False
    import base64
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    for number in [n.strip() for n in to.split(",") if n.strip()]:
        if not number.startswith("+"):
            number = "+1" + "".join(c for c in number if c.isdigit())  # assume US number
        body = urllib.parse.urlencode({"From": from_, "To": number, "Body": text}).encode()
        _post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", body,
              {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})
    return True


def send_email(to: str, subject: str, body: str) -> bool:
    host = os.getenv("SMTP_HOST")
    if not (host and to):
        return False
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, to
    msg["From"] = os.getenv("EMAIL_FROM", os.getenv("SMTP_USER", "uniqlo-agent@localhost"))
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=30) as s:
        s.starttls()
        if user := os.getenv("SMTP_USER"):
            s.login(user, os.getenv("SMTP_PASSWORD", ""))
        s.send_message(msg)
    return True


def send_whatsapp(text: str) -> bool:
    """Free WhatsApp message to your own phone via CallMeBot (personal use)."""
    phone, key = os.getenv("WHATSAPP_PHONE"), os.getenv("CALLMEBOT_APIKEY")
    if not (phone and key):
        return False
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        digits = "1" + digits  # assume US number
    q = urllib.parse.urlencode({"phone": "+" + digits, "text": text, "apikey": key})
    req = urllib.request.Request(f"https://api.callmebot.com/whatsapp.php?{q}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        reply = r.read().decode(errors="replace")
    if "error" in reply.lower() and "queued" not in reply.lower():
        raise RuntimeError(f"CallMeBot: {reply[:200]}")
    return True


def notify(title: str, text: str, sms: str | None = None) -> list[str]:
    sent = []
    short = sms or f"{title}\n{text}"[:1500]
    if send_whatsapp(short):
        sent.append("whatsapp")
    # Carrier email-to-text address, e.g. 5551234567@vtext.com (Verizon). Short text only.
    if send_email(os.getenv("SMS_EMAIL_TO", ""), "", short[:600]):
        sent.append("text-via-email")
    if send_sms(short):
        sent.append("sms")
    if topic := os.getenv("NTFY_TOPIC"):  # free phone push via the ntfy app
        server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        _post(f"{server}/{topic}", text[:4000].encode(), {"Title": title, "Tags": "shopping"})
        sent.append("ntfy")
    if url := os.getenv("SLACK_WEBHOOK_URL"):
        _post(url, json.dumps({"text": f"*{title}*\n{text}"[:39000]}).encode(), {"Content-Type": "application/json"})
        sent.append("slack")
    if url := os.getenv("DISCORD_WEBHOOK_URL"):
        _post(url, json.dumps({"content": f"**{title}**\n{text}"[:1990]}).encode(), {"Content-Type": "application/json"})
        sent.append("discord")
    if (tok := os.getenv("TELEGRAM_BOT_TOKEN")) and (chat := os.getenv("TELEGRAM_CHAT_ID")):
        body = json.dumps({"chat_id": chat, "text": f"{title}\n\n{text}"[:4000], "disable_web_page_preview": True})
        _post(f"https://api.telegram.org/bot{tok}/sendMessage", body.encode(), {"Content-Type": "application/json"})
        sent.append("telegram")
    if send_email(os.getenv("EMAIL_TO", ""), title, text):
        sent.append("email")
    return sent


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the alert instead of sending it")
    ap.add_argument("--no-save", action="store_true", help="don't update the snapshot")
    args = ap.parse_args()

    print(f"Fetching Uniqlo {REGION.upper()} catalog...")
    current = fetch_catalog()
    if len(current) < 50:  # protects the snapshot from a broken/blocked fetch
        print(f"Only {len(current)} products returned - looks like a bad fetch; aborting.", file=sys.stderr)
        return 1

    previous = json.loads(STATE_FILE.read_text())["products"] if STATE_FILE.exists() else None
    if previous is None:
        print(f"First run: saved a baseline of {len(current)} products. Alerts start from the next run.")
    else:
        changes = diff(previous, current)
        counts = {k: len(v) for k, v in changes.items()}
        print(f"Changes: {counts}")
        if any(counts.values()):
            parts = [f"{counts['price_drop']} price drops" if counts["price_drop"] else "",
                     f"{counts['new']} new" if counts["new"] else "",
                     f"{counts['price_increase']} price increases" if counts["price_increase"] else "",
                     f"{counts['removed']} removed" if counts["removed"] else ""]
            title = "Uniqlo: " + ", ".join(p for p in parts if p)
            text = ai_report(changes) or plain_report(changes)
            sms = sms_text(title, changes)
            if args.dry_run:
                print(f"\n=== {title} ===\n{text}\n\n=== SMS ===\n{sms}\n")
            else:
                sent = notify(title, text, sms)
                print(f"Notified: {', '.join(sent) if sent else 'NO CHANNEL CONFIGURED'}")
                if not sent:
                    print(f"\n=== {title} ===\n{text}\n")
        else:
            print("No changes.")

    if not args.no_save:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"saved_at": int(time.time()), "products": current}, ensure_ascii=False, sort_keys=True, indent=0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
