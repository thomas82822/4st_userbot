"""
4ST Prime Core — Plugin Loader
==============================
Auto-loads every *.py file from this directory (except __init__.py and
files starting with _) as a plugin.

Each plugin file can optionally define:

    def setup(client, cfg, bot_logger, **kwargs):
        '''Called once at startup. Register handlers here.'''
        pass

Usage:
    from plugins import load_all_plugins
    load_all_plugins(client, cfg=cfg, bot_logger=bot_logger)

Adding a new plugin:
    1. Create a new file: plugins/my_feature.py
    2. Define a setup(client, cfg, bot_logger, **kwargs) function
    3. Register your Telethon event handlers inside setup()
    4. Restart the bot — plugin loads automatically

See plugins/example_plugin.py for a ready-to-use template.
"""

import os
import importlib
import importlib.util
import sys


def load_all_plugins(client, **kwargs):
    """
    Discover and load every plugin in this directory.
    Returns list of (filename, success, error) tuples.
    """
    plugin_dir = os.path.dirname(__file__)
    results = []

    for fname in sorted(os.listdir(plugin_dir)):
        if not fname.endswith(".py"):
            continue
        if fname.startswith("_") or fname == "__init__.py":
            continue
        if fname == "example_plugin.py":
            continue  # skip template — remove this line to enable it

        module_name = f"plugins.{fname[:-3]}"
        try:
            spec = importlib.util.spec_from_file_location(
                module_name, os.path.join(plugin_dir, fname)
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)

            if hasattr(mod, "setup"):
                mod.setup(client, **kwargs)

            results.append((fname, True, None))
        except Exception as e:
            results.append((fname, False, str(e)))

    return results
