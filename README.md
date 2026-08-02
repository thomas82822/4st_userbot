# 🚀 4ST Prime Core — Telegram Music Userbot

A powerful Python-based Telegram Userbot & Bot framework with music streaming, admin tools, and GitHub-backed persistence.

---

## ✨ Features

- ⚡ Fast Performance
- 🤖 Telegram Userbot + Bot (Telethon + Pyrogram)
- 🎵 Voice chat music streaming via PyTgCalls
- 🎶 YouTube audio/video with cookie support (`YTDLP_COOKIES`)
- 🎨 Premium Emoji engine
- 🛠 Modular command system — easy plugin additions
- ☁️ GitHub-backed durable persistence (survives Heroku restarts)
- 🔄 24/7 Hosting on Heroku

---

## 🎵 Music Commands

> **All commands require a dot prefix** — `.play`, `.skip`, `.end` etc.

| Command | Description |
|---|---|
| `.play <song>` | Search & play a song by name |
| `.play` (reply) | Play the replied audio/video message |
| `.vplay <song>` | Search & play video in voice chat |
| `.playforce <song>` | Clear queue and force-play immediately |
| `.skip` | Skip to the next song in queue |
| `.pause` | Pause playback |
| `.resume` | Resume paused playback |
| `.end` | Stop music and leave voice chat |
| `.queue` | Show current queue |
| `.loop` | Toggle loop mode |
| `.mstatus` | Show music engine status |
| `.forall` | Allow everyone in chat to use `.play` |
| `.me` | Restrict `.play` to owner/sudo only |

### 🍪 YouTube Cookies Setup (for better quality & age-restricted videos)

1. Log into YouTube in Chrome or Firefox
2. Install **"Get cookies.txt LOCALLY"** browser extension
3. Go to `youtube.com` → click extension → Export for this domain
4. Copy the entire file content
5. Set it as `YTDLP_COOKIES` in Heroku Config Vars (or `.env`)

---

## 🔌 Plugin System

Add new commands without touching `main.py` — just drop a `.py` file in `plugins/`:

```bash
# 1. Copy the template
cp plugins/example_plugin.py plugins/my_feature.py

# 2. Implement your commands in setup()
# 3. Restart bot — loads automatically
```

See [`plugins/README.md`](plugins/README.md) for full docs and the API reference.

---

## 📂 Repo Structure

```
4st_userbot/
├── main.py                  # Core bot logic, all built-in commands
├── music_sources.py         # YouTube + 12 free music source resolvers
├── github_store.py          # GitHub-backed config persistence
├── plugins/                 # ← Add new features here (auto-loaded)
│   ├── __init__.py          # Plugin loader
│   ├── README.md            # Plugin docs + API reference
│   └── example_plugin.py   # Copy-paste template
├── data/
│   └── config.json          # Runtime config (auto-synced to GitHub)
├── requirements.txt
├── .env.example             # All supported environment variables
├── Aptfile                  # System deps (ffmpeg etc.)
└── Procfile                 # Heroku process definition
```

---

## 🚀 Deploy

### Deploy to Heroku

[![Deploy](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/thomas82822/4st_userbot)

### Environment Variables

Set these as Heroku Config Vars:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_API_ID` | ✅ | From https://my.telegram.org |
| `TELEGRAM_API_HASH` | ✅ | From https://my.telegram.org |
| `TELEGRAM_BOT_TOKEN` | ✅ | From @BotFather |
| `TELEGRAM_PRIMARY_SESSION` | ✅ | Telethon session string for owner account |
| `TELEGRAM_PYRO_SESSION` | ❌ | Pyrogram session string for music engine |
| `TELEGRAM_OWNER_ID` | ✅ | Your numeric Telegram user ID |
| `TELEGRAM_OWNER_USERNAME` | ❌ | Your Telegram @username (without @) |
| `TELEGRAM_LOG_CHANNEL_ID` | ❌ | Numeric ID of log channel/group |
| `TELEGRAM_HELP_LINK` | ❌ | t.me link for Help/Report button |
| `YTDLP_COOKIES` | ⭐ | YouTube cookies (Netscape format) — recommended for best quality |
| `GEMINI_API_KEY` | ❌ | Google Gemini API key for AI features |
| `GITHUB_TOKEN` | ✅ | PAT (repo scope) for data persistence |
| `GITHUB_REPO` | ✅ | `owner/repo` for data sync (e.g. `yourname/bot-data`) |
| `GITHUB_BRANCH` | ❌ | Branch for data sync (default: `main`) |
| `GITHUB_CONFIG_PATH` | ❌ | Path inside repo for config (default: `data/config.json`) |

> **Tip:** See `.env.example` for the complete list with descriptions.

---

## 📦 Stack

- Python 3.11
- Telethon 1.36
- Pyrogram (PyroFork) + PyTgCalls
- yt-dlp for music extraction (40-jugad bypass + cookie support)
- Google Gemini AI
- Heroku (worker dyno)

## 📜 License

MIT License
