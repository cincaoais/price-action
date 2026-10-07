"""Telegram notifications via plain Bot API HTTP (stdlib only).
send() is a silent no-op when bot_token is empty, so replay/dev just works."""
import json
import urllib.parse
import urllib.request


def send(cfg: dict, text: str) -> None:
    tg = (cfg or {}).get("telegram") or {}
    token, chat = tg.get("bot_token", ""), tg.get("chat_id", "")
    if not token or not chat:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=10)
    except Exception as e:          # never let a Telegram outage kill the runner
        print(f"telegram send failed: {e}")
