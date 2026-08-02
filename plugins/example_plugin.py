"""
4ST Prime Core — Example Plugin Template
=========================================
Copy this file, rename it (e.g. plugins/my_feature.py), and implement your
commands inside setup(). The bot loads all plugins automatically on start.

IMPORTANT: Remove the line in plugins/__init__.py that skips example_plugin.py
if you actually want this template to run. Otherwise it's always skipped.
"""

from telethon import events


PLUGIN_NAME    = "Example Plugin"
PLUGIN_VERSION = "1.0.0"
PLUGIN_CMDS    = [".hello", ".greet"]


def setup(client, cfg=None, bot_logger=None, **kwargs):
    """
    Called once at startup. Register your Telethon event handlers here.

    Args:
        client      — the Telethon TelegramClient (userbot)
        cfg         — the live config dict (read/write, auto-synced to GitHub)
        bot_logger  — bot_logger(tag, message) logging function
        **kwargs    — future-proof: ignore unknown args
    """
    log = bot_logger or (lambda tag, msg: print(f"[{tag}] {msg}"))
    log("PLUGIN", f"Loading {PLUGIN_NAME} v{PLUGIN_VERSION}")

    # ─────────────────────────────────────────────────────────────────
    # Example command: .hello
    # Replies with a greeting and deletes the trigger message.
    # ─────────────────────────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.hello$"))
    async def cmd_hello(event):
        await event.delete()
        await event.respond("<blockquote>👋 <b>Hello from Example Plugin!</b></blockquote>",
                            parse_mode="html")

    # ─────────────────────────────────────────────────────────────────
    # Example command: .greet <name>
    # Replies with a personalised greeting.
    # ─────────────────────────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.greet\s+(.+)$"))
    async def cmd_greet(event):
        name = event.pattern_match.group(1).strip()
        await event.delete()
        await event.respond(
            f"<blockquote>🌟 <b>Hello, {name}!</b> — sent by 4ST Prime Core.</blockquote>",
            parse_mode="html",
        )

    log("PLUGIN", f"{PLUGIN_NAME} loaded — commands: {', '.join(PLUGIN_CMDS)}")
