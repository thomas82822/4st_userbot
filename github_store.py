"""
GitHub-backed durable persistence for data/config.json.

Heroku dynos have an EPHEMERAL filesystem — every file written to disk
during a dyno's life (BOT_USERS, SAVED_STRINGS, warnings, per-account
PYRO_SESSIONS, custom commands, ...) disappears the moment the dyno
restarts, redeploys, or gets rescheduled onto a new box. This module gives
the bot a tiny, dependency-light way to survive that: it mirrors
data/config.json to a GitHub repo via the plain Contents API, and pulls the
last-synced copy back down on a fresh boot when no local copy exists yet
(see load_config() in main.py).

Enable it with two env vars (exposed in app.json's Heroku config form):
  GITHUB_TOKEN  - a fine-grained Personal Access Token scoped to
                  Contents: Read & Write on the target repo only.
                  NEVER hardcode this — it is read from the environment.
  GITHUB_REPO   - "owner/repo" the token can write to.

Optional:
  GITHUB_BRANCH       - branch to read/write (default: "main")
  GITHUB_CONFIG_PATH  - path inside the repo for the synced file
                        (default: "data/config.json")

If GITHUB_TOKEN/GITHUB_REPO are not set, every public function here is a
safe, silent no-op — the bot behaves exactly as it did before this module
existed; it just won't survive a full dyno filesystem wipe.
"""

import base64
import json
import os
import threading
import time

try:
    import requests
except ImportError:
    requests = None

_GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
_GITHUB_REPO   = os.environ.get("GITHUB_REPO", "")
_GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
_GITHUB_PATH   = os.environ.get("GITHUB_CONFIG_PATH", "data/config.json")

_API_BASE = "https://api.github.com"

# Cached blob SHA of the last known remote copy — lets push_config_async
# skip a redundant GET before every PUT once it knows the current sha.
# Cleared on any failure so the next push re-fetches the real value.
_sha_cache = None


def is_enabled() -> bool:
    """True only if the requests library is available AND both required
    env vars are set. Every other function checks this first and no-ops
    if it's False, so callers never need to branch on it themselves."""
    return bool(requests and _GITHUB_TOKEN and _GITHUB_REPO)


def _headers():
    return {
        "Authorization": f"Bearer {_GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _contents_url() -> str:
    return f"{_API_BASE}/repos/{_GITHUB_REPO}/contents/{_GITHUB_PATH}"


def fetch_remote_config(logger=None):
    """Fetch config.json from the GitHub repo.

    Returns a dict on success, or None if disabled/not-yet-created/on any
    error — main.py's caller already treats None as "nothing to restore".
    Also seeds _sha_cache so the very first push_config_async() call after
    boot doesn't need an extra GET just to learn the current blob sha.
    """
    global _sha_cache
    logger = logger or (lambda tag, msg: None)
    if not is_enabled():
        return None
    try:
        resp = requests.get(
            _contents_url(), headers=_headers(),
            params={"ref": _GITHUB_BRANCH}, timeout=15,
        )
        if resp.status_code == 404:
            logger("GITHUB_STORE", "No remote config found yet (first boot).")
            return None
        resp.raise_for_status()
        body = resp.json()
        _sha_cache = body.get("sha")
        content = base64.b64decode(body["content"]).decode("utf-8")
        return json.loads(content)
    except Exception as e:
        logger("GITHUB_STORE_ERR", f"fetch_remote_config failed: {e}")
        return None


def _push_config_sync(data: dict, logger):
    """Runs on a background thread — see push_config_async(). Uses the
    GitHub Contents API's create-or-update-file endpoint, which requires
    the current blob sha whenever the file already exists (otherwise
    GitHub rejects it as a conflicting overwrite)."""
    global _sha_cache
    try:
        payload_str  = json.dumps(data, indent=4)
        b64_content  = base64.b64encode(payload_str.encode("utf-8")).decode("ascii")
        body = {
            "message": f"Sync config.json ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}) [skip ci]",
            "content": b64_content,
            "branch":  _GITHUB_BRANCH,
        }
        sha = _sha_cache
        if sha is None:
            get_resp = requests.get(
                _contents_url(), headers=_headers(),
                params={"ref": _GITHUB_BRANCH}, timeout=15,
            )
            if get_resp.status_code == 200:
                sha = get_resp.json().get("sha")
        if sha:
            body["sha"] = sha

        resp = requests.put(_contents_url(), headers=_headers(), json=body, timeout=15)
        if resp.status_code == 409:
            # Another push landed in between and our cached sha is stale.
            # Clear it so the *next* save_config() call re-fetches the
            # real sha instead of retrying with a value we know is wrong.
            _sha_cache = None
            logger("GITHUB_STORE_ERR", "push_config conflict (409) — will retry on next save.")
            return
        resp.raise_for_status()
        _sha_cache = (resp.json().get("content") or {}).get("sha")
        logger("GITHUB_STORE", "config.json synced to GitHub.")
    except Exception as e:
        _sha_cache = None
        logger("GITHUB_STORE_ERR", f"push_config failed: {e}")


def push_config_async(data: dict, logger=None):
    """Fire-and-forget background sync of `data` to the GitHub repo.

    save_config() in main.py is called from hot paths all over the bot
    (every new user, every saved session, every warning count change), so
    this must never block the caller or raise back into it. A plain
    background thread (not asyncio) is used deliberately — save_config()
    itself is a synchronous function called from both async and sync
    contexts, so it can't reliably schedule an asyncio task.
    """
    logger = logger or (lambda tag, msg: None)
    if not is_enabled():
        return
    try:
        # Snapshot via json round-trip on the calling thread so later
        # mutations to the live `cfg` dict can't race with json.dumps()
        # running concurrently on the background thread.
        snapshot = json.loads(json.dumps(data))
    except Exception as e:
        logger("GITHUB_STORE_ERR", f"push_config_async snapshot failed: {e}")
        return
    threading.Thread(
        target=_push_config_sync, args=(snapshot, logger), daemon=True
    ).start()
