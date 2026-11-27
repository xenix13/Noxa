"""Language of the server messages (errors, scheduled tasks, update…).

The text in the code is in French and serves as the key, as in the interface (static/js/i18n.js): tr("Introuvable")
returns the translation in the language of the current request. Composed messages ("Dossier introuvable
depuis le serveur : /mnt/x") are recognised through patterns where {} stands for the variable part.

Language of a request: X-Lang header (sent by the web interface, in the language it shows), otherwise the
account's language, otherwise the server's default language (Settings → Users). It is kept for the
request's thread; background jobs use the default language.
"""
import re
import threading

from .lang_en import EN

LANGUAGES = {"fr": "Français", "en": "English"}
DEFAULT = "en"  # widest audience; a server keeps the language chosen at setup (default_language)
DICTS = {"en": EN}

_local = threading.local()
_server_default = {"lang": DEFAULT}


def valid(lang):
    return lang if lang in LANGUAGES else None


def default_language(db):
    return valid(db.setting("default_language")) or DEFAULT


def configured_language(db):
    """Language an administrator chose for the server, or None (then the interface follows the browser)."""
    return valid(db.setting("default_language"))


def set_server_default(lang):
    """Language of the background jobs (kept here so as not to read the database outside a request)."""
    _server_default["lang"] = valid(lang) or DEFAULT


def set_lang(lang):
    _local.lang = valid(lang)


def current():
    return getattr(_local, "lang", None) or _server_default["lang"]


def request_language(req, db):
    """Language of a request: interface header, account, server default language."""
    return (valid((req.headers.get("X-Lang") or "").strip()) or (req.user and valid(req.user.get("language")))
            or default_language(db))


_patterns = {}  # language -> [(expression, translation)] for the keys holding {}


def _templates(lang):
    if lang not in _patterns:
        out = []
        for k, v in DICTS[lang].items():
            if "{}" in k:
                rx = "^" + re.escape(k).replace(r"\{\}", "(.+?)") + "$"
                out.append((re.compile(rx, re.S), v))
        out.sort(key=lambda p: -len(p[0].pattern))  # the most precise pattern first
        _patterns[lang] = out
    return _patterns[lang]


def tr(text, lang=None):
    """Translation of text (as is when there is none, or in French)."""
    lang = lang or current()
    d = DICTS.get(lang)
    if not d or not isinstance(text, str) or not text:
        return text
    hit = d.get(text)
    if hit is not None:
        return hit
    for rx, value in _templates(lang):
        m = rx.match(text)
        if m:
            # the variable parts can themselves be known messages ("… : délai dépassé")
            return value.format(*(d.get(g, g) for g in m.groups()))
    return text
