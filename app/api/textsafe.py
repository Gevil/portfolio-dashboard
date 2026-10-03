"""Sanitising of untrusted third-party text (RSS/GDELT/Finnhub headlines,
filing titles) before it reaches an LLM prompt, a push notification or a log.

Headlines are attacker-influenced input: they can carry control / bidi
characters, long padding, and URLs. ``clean_text`` removes what has no business
in a one-line headline; it does NOT make text trustworthy - prompts must still
frame it as data (see app/playbooks/*.md).
"""
import re
import unicodedata

# C0/C1 controls (keeping \t \n), zero-width + bidi override/isolate marks, BOM.
_CONTROL = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f"
    "\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_WS = re.compile(r"\s+")


def clean_text(value, max_len: int = 300, strip_urls: bool = True) -> str:
    """One-line, control-free, length-capped text. Non-strings -> ''."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = _CONTROL.sub("", text)
    if strip_urls:
        text = _URL.sub("", text)
    text = _WS.sub(" ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "\u2026"
    return text


def clean_headline(value, max_len: int = 200) -> str:
    return clean_text(value, max_len=max_len, strip_urls=True)
