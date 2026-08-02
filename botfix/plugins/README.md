# 🔌 4ST Prime Core — Plugin System

Plugins let you add new commands without touching `main.py`.  
Each plugin is a single `.py` file in this folder — the bot loads them all automatically on startup.

---

## ⚡ Add a New Plugin in 3 Steps

**1. Copy the template**
```bash
cp plugins/example_plugin.py plugins/my_feature.py
```

**2. Implement your commands**

```python
# plugins/my_feature.py

from telethon import events

PLUGIN_NAME = "My Feature"
PLUGIN_CMDS = [".mycommand"]

def setup(client, cfg=None, bot_logger=None, **kwargs):
    """Called once at startup. Register handlers here."""

    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.mycommand$"))
    async def cmd_my(event):
        await event.delete()
        await event.respond("<blockquote>✅ My command works!</blockquote>", parse_mode="html")
```

**3. Restart the bot** — plugin loads automatically. Done.

---

## 📂 Files in this Folder

| File | Description |
|---|---|
| `__init__.py` | Plugin auto-loader (do not edit) |
| `example_plugin.py` | Ready-to-use template with comments |
| _(your plugin)_.py | Your new feature |

---

## 🧩 Plugin API

Your `setup()` function receives:

| Argument | Type | Description |
|---|---|---|
| `client` | `TelegramClient` | Telethon userbot client |
| `cfg` | `dict` | Live config (read/write, auto-synced to GitHub) |
| `bot_logger` | `callable` | `bot_logger(tag, msg)` for structured logging |

---

## 📋 Command Naming Rules

- All commands **must start with a dot**: `.mycommand`
- Use **snake_case** for multi-word commands: `.my_command` or `.mycommand`
- Keep command names short and unique
- Document your commands in `PLUGIN_CMDS = [".cmd1", ".cmd2"]`

---

## 💡 Tips

- Read `cfg` for user settings, write back with `cfg["MY_SETTING"] = value` then `save_config(cfg)`
- Use `bot_logger("MY_TAG", "message")` for consistent log formatting
- Use `asyncio.create_task(event.delete())` to delete the trigger message non-blockingly
- Wrap API calls in `try/except` — Telegram errors should never crash the bot
- Test in a private chat or test group before deploying to production

---

## 🗂️ Repo Structure

```
4st_userbot/
├── main.py                  # Core bot logic, startup, reconnect loop
├── music_sources.py         # YouTube + 12 free music source resolvers
├── github_store.py          # GitHub-backed config persistence
├── plugins/                 # ← Add new features here
│   ├── __init__.py          # Plugin auto-loader
│   ├── README.md            # This file
│   └── example_plugin.py   # Copy-paste template
├── data/
│   └── config.json          # Runtime config (auto-synced to GitHub)
├── requirements.txt
└── .env.example             # All supported environment variables
```
