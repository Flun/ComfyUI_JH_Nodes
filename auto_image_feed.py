import base64
import datetime
import functools
import hashlib
import ipaddress
import io
import json
import os
import random
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from contextlib import contextmanager

import numpy as np
import requests
import torch
from PIL import Image, ImageOps

import comfy.model_management
import folder_paths
from comfy_execution.progress import get_progress_state
from server import PromptServer

try:
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    webdriver = None
    TimeoutException = Exception
    WebDriverException = Exception
    WebDriverWait = None


FEED_DIR = os.path.join(folder_paths.get_user_directory(), "jh_auto_image_feed")
PROFILE_DIR = os.path.join(FEED_DIR, "chrome_profile")
DATABASE_PATH = os.path.join(FEED_DIR, "seen.sqlite3")
PRESETS_PATH = os.path.join(FEED_DIR, "successful_searches.json")
DRIVER_PID_PATH = os.path.join(FEED_DIR, "chromedriver.pid")
DATABASE_LOCK = threading.Lock()
PRESETS_LOCK = threading.Lock()
SESSION_LOCK = threading.Lock()
BROWSER_LOCK = threading.Lock()
ACTIVE_DRIVER_PIDS = set()
SESSION_SEEN_KEYS = set()
SESSION_SEEN_HASHES = set()
SESSION_OUTPUT_COUNT = 0
PENDING_HISTORY = {}
PENDING_HISTORY_LOCK = threading.Lock()
ACTIVE_DC_POSTS = {}
LAST_DC_OUTPUT_POSTS = {}
DC_PAGE_CACHE = {}
DC_SEARCH_PAGE_URLS = {}
SOURCE_SCROLL_DEPTH = {}
ARCA_PAGE_CURSORS = {}
MAX_IMAGE_BYTES = 50 * 1024 * 1024
DC_PAGE_CACHE_TTL = 300
DHASH_MAX_DISTANCE = 4
MAX_SOURCE_SEARCH_PASSES = 3
UNLIMITED_RETRY_DELAY = 5.0
DEFAULT_SEARCH_TIMEOUT_MINUTES = 10
MAX_COLLECTION_ATTEMPTS = 6
MAX_ARCA_PAGE_SCANS = 25
MAX_SUCCESSFUL_SEARCHES_PER_SOURCE = 30
LOCAL_DIRECTORY_SOURCE = "Local / NAS Directory"
SOURCE_TYPES = ("Google Images", LOCAL_DIRECTORY_SOURCE, "Instagram User", "Instagram Hashtag", "Reddit Subreddit", "DCInside Gallery", "Arca.live Channel", "Website URL", "X Search", "Mixed Sources")
LOCAL_IMAGE_EXTENSIONS = {
    ".apng", ".avif", ".bmp", ".gif", ".heic", ".heif", ".jfif", ".jpeg", ".jpg",
    ".png", ".tif", ".tiff", ".webp",
}
DC_GALLERIES = (
    "실시간 베스트 갤러리 (dcbest)",
    "여자 갤러리 (duwk)",
    "연예인 갤러리 (enterpic)",
    "실시간 지구촌 갤러리 (singlebungle1472)",
    "직접 입력 (ID/URL)",
)
DC_GALLERY_URLS = {
    DC_GALLERIES[0]: "https://gall.dcinside.com/board/lists/?id=dcbest",
    DC_GALLERIES[1]: "https://gall.dcinside.com/mgallery/board/lists/?id=duwk",
    DC_GALLERIES[2]: "https://gall.dcinside.com/mgallery/board/lists/?id=enterpic",
    DC_GALLERIES[3]: "https://gall.dcinside.com/mgallery/board/lists/?id=singlebungle1472",
}
ULTRALYTICS_DIR = os.path.join(folder_paths.models_dir, "ultralytics")
folder_paths.add_model_folder_path("ultralytics", ULTRALYTICS_DIR, is_default=True)


def _load_auto_feed_presets():
    try:
        with open(PRESETS_PATH, "r", encoding="utf-8") as file:
            presets = json.load(file)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as error:
        print(f"[JH Auto Image Feed] Could not read successful searches: {error}")
        return []
    return presets if isinstance(presets, list) else []


def get_auto_feed_presets():
    with PRESETS_LOCK:
        return _load_auto_feed_presets()


def delete_auto_feed_preset(preset_id):
    with PRESETS_LOCK:
        presets = _load_auto_feed_presets()
        filtered = [preset for preset in presets if preset.get("id") != preset_id]
        if len(filtered) == len(presets):
            return False
        os.makedirs(FEED_DIR, exist_ok=True)
        with open(PRESETS_PATH, "w", encoding="utf-8") as file:
            json.dump(filtered, file, ensure_ascii=False)
        return True


def _successful_search_preset(source, query, title_filter, search_mode, dc_gallery, dc_gallery_custom,
                              arca_channel, arca_mode, reddit_mode, reddit_subreddit, reddit_keyword):
    values = {}
    if source == LOCAL_DIRECTORY_SOURCE:
        values = {"directory_path": query}
        label = query
    elif source == "DCInside Gallery":
        values = {"dc_gallery": dc_gallery, "dc_gallery_custom": dc_gallery_custom, "title_filter": title_filter}
        gallery_label = dc_gallery_custom if dc_gallery == DC_GALLERIES[-1] and dc_gallery_custom else dc_gallery
        label = " / ".join(value for value in (gallery_label, title_filter) if value)
    elif source == "Arca.live Channel":
        values = {"arca_channel": arca_channel, "arca_mode": arca_mode, "title_filter": title_filter}
        label = " / ".join(value for value in (arca_channel, title_filter) if value)
    elif source == "Reddit Subreddit":
        values = {"reddit_mode": reddit_mode, "reddit_subreddit": reddit_subreddit, "reddit_keyword": reddit_keyword}
        label = f"{reddit_mode}: {reddit_keyword if reddit_mode == 'Keyword Search' else reddit_subreddit}"
    else:
        values = {"query": query}
        if source == "X Search":
            values["search_mode"] = search_mode
        label = f"{search_mode}: {query}" if source == "X Search" else query
    values = {key: str(value or "").strip() for key, value in values.items()}
    canonical = json.dumps({"source": source, "values": values}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"id": hashlib.sha256(canonical.encode("utf-8")).hexdigest(), "source": source, "label": label.strip(), "values": values}


def _record_successful_search(*args):
    preset = _successful_search_preset(*args)
    if not preset["label"]:
        return
    with PRESETS_LOCK:
        presets = [item for item in _load_auto_feed_presets() if item.get("id") != preset["id"]]
        presets.insert(0, preset)
        counts = {}
        limited = []
        for item in presets:
            source = item.get("source")
            counts[source] = counts.get(source, 0) + 1
            if counts[source] <= MAX_SUCCESSFUL_SEARCHES_PER_SOURCE:
                limited.append(item)
        os.makedirs(FEED_DIR, exist_ok=True)
        temporary_path = PRESETS_PATH + ".tmp"
        with open(temporary_path, "w", encoding="utf-8") as file:
            json.dump(limited, file, ensure_ascii=False)
        os.replace(temporary_path, PRESETS_PATH)
    PromptServer.instance.send_sync("jh-auto-feed-presets-updated", {}, PromptServer.instance.client_id)


def _find_chrome():
    candidates = [
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    if os.name != "nt":
        candidates += [
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/google-chrome",
            "/snap/bin/chromium",
            "/opt/google/chrome/chrome",
            "/opt/chromium/chromium",
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
        ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    if os.name != "nt":
        for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
            resolved = shutil.which(name)
            if resolved:
                return resolved
    raise RuntimeError("Google Chrome was not found")


def _stored_driver_pid():
    try:
        with open(DRIVER_PID_PATH, "r", encoding="ascii") as file:
            return int(file.read().strip())
    except (OSError, ValueError):
        return None


def _remove_driver_pid(pid=None):
    if pid is not None and _stored_driver_pid() != pid:
        return
    try:
        os.remove(DRIVER_PID_PATH)
    except FileNotFoundError:
        pass


def _linux_crawler_pids():
    """Return PIDs of processes whose command line references the JH profile dir."""
    try:
        output = subprocess.run(
            ["pgrep", "-f", PROFILE_DIR],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(pid) for pid in output.split() if pid.strip().isdigit()]


def _linux_pid_is_crawler(pid):
    """True if the PID's command line references the JH profile dir or chromedriver."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as file:
            cmdline = file.read().decode(errors="replace").replace("\0", " ")
    except OSError:
        return False
    return PROFILE_DIR in cmdline or "chromedriver" in cmdline


def _linux_kill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _cleanup_stale_driver():
    with BROWSER_LOCK:
        pid = _stored_driver_pid()
        if pid is None:
            return
        if pid in ACTIVE_DRIVER_PIDS:
            raise RuntimeError("The JH crawler is already using the browser profile.")
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            if result.stdout.lstrip().lower().startswith('"chromedriver.exe"'):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                )
        elif _linux_pid_is_crawler(pid):
            _linux_kill(pid)
        _remove_driver_pid(pid)


def stop_active_crawlers():
    with BROWSER_LOCK:
        pids = list(ACTIVE_DRIVER_PIDS)
    if os.name == "nt":
        stopped = 0
        for pid in pids:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            if not result.stdout.lstrip().lower().startswith('"chromedriver.exe"'):
                continue
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
            stopped += 1
        return stopped
    profile_pids = [pid for pid in _linux_crawler_pids() if _linux_pid_is_crawler(pid)]
    target_pids = list(dict.fromkeys(pids + profile_pids))
    stopped = 0
    for pid in target_pids:
        if _linux_kill(pid):
            stopped += 1
    return stopped


def _remember_driver(driver):
    pid = driver.service.process.pid
    with BROWSER_LOCK:
        os.makedirs(FEED_DIR, exist_ok=True)
        with open(DRIVER_PID_PATH, "w", encoding="ascii") as file:
            file.write(str(pid))
        ACTIVE_DRIVER_PIDS.add(pid)


def _close_driver(driver):
    pid = driver.service.process.pid
    try:
        try:
            driver.quit()
        except WebDriverException:
            pass
    finally:
        if pid is not None:
            with BROWSER_LOCK:
                ACTIVE_DRIVER_PIDS.discard(pid)
                _remove_driver_pid(pid)


def _check_interrupted():
    comfy.model_management.throw_exception_if_processing_interrupted()


def _interruptible_sleep(seconds):
    deadline = time.monotonic() + seconds
    while True:
        _check_interrupted()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.25, remaining))


def _interruptible_wait(driver, timeout, condition):
    def check(current):
        _check_interrupted()
        return condition(current)

    return WebDriverWait(driver, timeout).until(check)


def _source_url(source, query, period, safe_search, search_mode="Top", reddit_mode="Subreddit", ranking="Source Order", arca_mode="Best"):
    query = query.strip()
    if not query:
        raise ValueError("directory path is empty" if source == LOCAL_DIRECTORY_SOURCE else "query is empty")
    if source == LOCAL_DIRECTORY_SOURCE:
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(query.strip('"'))))
        if not os.path.isdir(path):
            raise ValueError(f"Local / NAS directory does not exist or is not accessible: {path}")
        return path
    if source == "Google Images":
        params = {"tbm": "isch", "q": query, "safe": "active" if safe_search == "On" else "off"}
        if ranking == "Newest First":
            google_period = {"Hour": "h", "Day": "d", "Week": "w", "Month": "m", "Year": "y", "All": None}[period]
            params["tbs"] = f"qdr:{google_period},sbd:1" if google_period else "sbd:1"
        return "https://www.google.com/search?" + urllib.parse.urlencode(params)
    if source == "Instagram User":
        username = query.lstrip("@").strip("/")
        if not re.fullmatch(r"[A-Za-z0-9._]+", username):
            raise ValueError("Instagram username is invalid")
        return f"https://www.instagram.com/{username}/"
    if source == "Instagram Hashtag":
        tag = query.lstrip("#").strip("/")
        if not re.fullmatch(r"[^\s/#]+", tag):
            raise ValueError("Instagram hashtag is invalid")
        return f"https://www.instagram.com/explore/tags/{urllib.parse.quote(tag)}/"
    if source == "DCInside Gallery":
        if query.startswith(("http://", "https://")):
            parsed = urllib.parse.urlparse(query)
            if parsed.scheme != "https" or parsed.hostname != "gall.dcinside.com":
                raise ValueError("DCInside URL must use https://gall.dcinside.com")
            return query
        if query.startswith("minor:"):
            return "https://gall.dcinside.com/mgallery/board/lists/?id=" + urllib.parse.quote(query[6:])
        if query.startswith("mini:"):
            return "https://gall.dcinside.com/mini/board/lists/?id=" + urllib.parse.quote(query[5:])
        if not re.fullmatch(r"[A-Za-z0-9_]+", query):
            raise ValueError("DCInside gallery ID is invalid")
        return "https://gall.dcinside.com/board/lists/?id=" + query
    if source == "Arca.live Channel":
        channel = query.removeprefix("https://arca.live/b/").strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", channel):
            raise ValueError("Arca.live channel name is invalid")
        suffix = "?mode=best" if arca_mode == "Best" else ""
        return f"https://arca.live/b/{channel}{suffix}"
    if source == "Website URL":
        if not _is_public_url(query):
            raise ValueError("Website URL must be a public http(s) address")
        return query
    if source == "X Search":
        params = {"q": query, "src": "typed_query", "f": "live" if search_mode == "Latest" or ranking == "Newest First" else "top"}
        return "https://x.com/search?" + urllib.parse.urlencode(params)
    reddit_period = {"Hour": "hour", "Day": "day", "Week": "week", "Month": "month", "Year": "year", "All": "all"}[period]
    if reddit_mode == "Keyword Search":
        params = {"q": query, "sort": "new" if ranking == "Newest First" else "top", "t": reddit_period, "type": "link"}
        return "https://www.reddit.com/search/?" + urllib.parse.urlencode(params)
    subreddit = query.removeprefix("r/").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_]+", subreddit):
        raise ValueError("subreddit is invalid")
    if ranking == "Newest First":
        return f"https://www.reddit.com/r/{subreddit}/new/"
    return f"https://www.reddit.com/r/{subreddit}/top/?t={reddit_period}"


def _post_timestamp(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    for date_format in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y.%m.%d", "%y/%m/%d", "%B %d, %Y", "%b %d, %Y", "%H:%M"):
        try:
            parsed = datetime.datetime.strptime(value, date_format)
        except ValueError:
            continue
        if date_format == "%H:%M":
            now = datetime.datetime.now()
            parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
        return parsed.timestamp()
    return None


def _filter_recent_candidates(candidates, period):
    period_seconds = {"Hour": 3600, "Day": 86400, "Week": 7 * 86400, "Month": 31 * 86400, "Year": 366 * 86400}
    cutoff = time.time() - period_seconds[period]
    dated = [(candidate, _post_timestamp(candidate.get("post_date"))) for candidate in candidates]
    dated = [(candidate, timestamp) for candidate, timestamp in dated if timestamp is not None]
    if not dated:
        return candidates
    return [candidate for candidate, timestamp in dated if timestamp >= cutoff]


def _filter_popular_candidates(source, candidates, min_popularity, min_comments, min_views):
    if source in ("Reddit Subreddit", "X Search"):
        return [
            candidate for candidate in candidates
            if candidate.get("rank_score", 0) >= min_popularity and candidate.get("comment_count", 0) >= min_comments
        ]
    if source.startswith("Instagram"):
        return [
            candidate for candidate in candidates
            if candidate.get("like_count", 0) >= min_popularity and candidate.get("comment_count", 0) >= min_comments
        ]
    if source == "DCInside Gallery":
        return [
            candidate for candidate in candidates
            if candidate.get("rank_score", 0) >= min_popularity
            and candidate.get("comment_count", 0) >= min_comments
            and candidate.get("view_count", 0) >= min_views
        ]
    if source == "Arca.live Channel":
        return [
            candidate for candidate in candidates
            if candidate.get("rank_score", 0) >= min_popularity and candidate.get("comment_count", 0) >= min_comments
        ]
    return candidates


def _dc_gallery_query(gallery, custom, fallback):
    if gallery in DC_GALLERY_URLS:
        return DC_GALLERY_URLS[gallery]
    return custom.strip() or fallback


@contextmanager
def _open_database():
    os.makedirs(FEED_DIR, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS seen (
                source TEXT NOT NULL,
                item_key TEXT NOT NULL,
                url TEXT NOT NULL,
                content_hash TEXT,
                status TEXT NOT NULL,
                score REAL,
                metadata TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source, item_key)
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS seen_content_hash ON seen(content_hash)")
        yield connection
        connection.commit()
    finally:
        connection.close()


def _known_keys(source, keys):
    if not keys:
        return set()
    with DATABASE_LOCK, _open_database() as connection:
        known = set()
        for offset in range(0, len(keys), 500):
            chunk = keys[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"SELECT item_key FROM seen WHERE source = ? AND status = 'accepted' AND item_key IN ({placeholders})",
                (source, *chunk),
            )
            known.update(row[0] for row in rows)
        return known


def _known_hash(content_hash):
    with DATABASE_LOCK, _open_database() as connection:
        hashes = connection.execute("SELECT DISTINCT content_hash FROM seen WHERE status = 'accepted' AND content_hash IS NOT NULL")
        return any(_hash_distance(content_hash, row[0]) <= DHASH_MAX_DISTANCE for row in hashes)


def _session_known_keys(source, keys):
    with SESSION_LOCK:
        known = {key for key in keys if (source, key) in SESSION_SEEN_KEYS}
    with PENDING_HISTORY_LOCK:
        for records in PENDING_HISTORY.values():
            pending_keys = {
                record["candidate"]["key"] for record in records
                if record["source"] == source
            }
            known.update(key for key in keys if key in pending_keys)
    return known


def _session_known_hash(content_hash):
    with SESSION_LOCK:
        known_hashes = tuple(SESSION_SEEN_HASHES)
    with PENDING_HISTORY_LOCK:
        known_hashes += tuple(
            record["content_hash"]
            for records in PENDING_HISTORY.values()
            for record in records
            if record.get("content_hash")
        )
    return any(_hash_distance(content_hash, known_hash) <= DHASH_MAX_DISTANCE for known_hash in known_hashes)


def _hash_distance(first, second):
    try:
        return (int(first, 16) ^ int(second, 16)).bit_count()
    except (TypeError, ValueError):
        return 65


def _record_session(source, item_key, content_hash=None, output=False):
    global SESSION_OUTPUT_COUNT
    with SESSION_LOCK:
        SESSION_SEEN_KEYS.add((source, item_key))
        if content_hash:
            SESSION_SEEN_HASHES.add(content_hash)
        if output:
            SESSION_OUTPUT_COUNT += 1


def _session_output_count():
    with SESSION_LOCK:
        return SESSION_OUTPUT_COUNT


def _record_candidate(source, candidate, status, content_hash=None, score=None):
    stored_candidate = {
        key: value for key, value in candidate.items() if key not in ("frame_bytes", "_analysis", "_content_hash")
    }
    metadata = json.dumps(stored_candidate, ensure_ascii=False, separators=(",", ":"))
    with DATABASE_LOCK, _open_database() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO seen(source, item_key, url, content_hash, status, score, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source, candidate["key"], candidate["url"], content_hash, status, score, metadata),
        )


def _stage_candidate_until_success(source, candidate, content_hash, score, session_keys=()):
    prompt_id = get_progress_state().prompt_id
    if not prompt_id:
        print("[JH Auto Image Feed] Prompt ID unavailable; recording accepted history immediately.")
        _record_session(source, candidate["key"], content_hash, output=True)
        for item_key in session_keys:
            _record_session(source, item_key)
        _record_candidate(source, candidate, "accepted", content_hash, score)
        return
    record = {
        "source": source,
        "candidate": dict(candidate),
        "content_hash": content_hash,
        "score": score,
        "session_keys": tuple(session_keys),
    }
    with PENDING_HISTORY_LOCK:
        records = PENDING_HISTORY.setdefault(prompt_id, [])
        records[:] = [
            current for current in records
            if (current["source"], current["candidate"]["key"]) != (source, candidate["key"])
        ]
        records.append(record)


def _finish_pending_history(prompt_id, succeeded):
    if not prompt_id:
        return
    with PENDING_HISTORY_LOCK:
        records = PENDING_HISTORY.pop(prompt_id, [])
    if not succeeded:
        return
    for record in records:
        source = record["source"]
        candidate = record["candidate"]
        content_hash = record["content_hash"]
        _record_candidate(source, candidate, "accepted", content_hash, record["score"])
        _record_session(source, candidate["key"], content_hash, output=True)
        for item_key in record["session_keys"]:
            _record_session(source, item_key)


def _install_history_completion_hook():
    server = getattr(PromptServer, "instance", None)
    if server is None:
        return False
    current = server.send_sync
    if getattr(current, "_jh_auto_feed_history_hook", False):
        return True

    def send_sync_with_history(event, data, sid=None):
        if event in ("execution_success", "execution_error", "execution_interrupted"):
            try:
                _finish_pending_history(data.get("prompt_id"), event == "execution_success")
            except Exception as error:
                print(f"[JH Auto Image Feed] Could not finalize deferred history: {error}")
        return current(event, data, sid)

    send_sync_with_history._jh_auto_feed_history_hook = True
    server.send_sync = send_sync_with_history
    return True


def _claim_media(source, candidate, enforce_history, persist_history=False):
    if not enforce_history:
        return True
    media_key = "media:" + candidate["media_id"]
    if media_key in _session_known_keys(source, [media_key]):
        return False
    with DATABASE_LOCK, _open_database() as connection:
        already_captured = connection.execute(
            """
            SELECT 1 FROM seen
            WHERE source = ? AND (
                (item_key = ? AND status = 'media_captured') OR (
                    metadata LIKE ? AND (
                        metadata LIKE '%\"media_type\":\"video_frame\"%' OR
                        metadata LIKE '%\"media_type\":\"animated_image%'
                    )
                )
            ) LIMIT 1
            """,
            (source, media_key, f'%"page_url":"{candidate["page_url"]}"%'),
        ).fetchone() is not None
    if already_captured:
        _record_session(source, media_key)
        return False
    media_record = {
        "key": media_key,
        "url": candidate["url"],
        "page_url": candidate["page_url"],
        "media_id": candidate["media_id"],
        "media_type": candidate["media_type"],
        "media_extension": candidate.get("media_extension", ""),
    }
    if persist_history:
        _record_session(source, media_key)
        _record_candidate(source, media_record, "media_captured")
    return True


def _accepted_count():
    with DATABASE_LOCK, _open_database() as connection:
        return connection.execute("SELECT COUNT(*) FROM seen WHERE status = 'accepted'").fetchone()[0]


def _mixed_sources(value):
    aliases = {
        "local": LOCAL_DIRECTORY_SOURCE,
        "directory": LOCAL_DIRECTORY_SOURCE,
        "nas": LOCAL_DIRECTORY_SOURCE,
        "google": "Google Images",
        "instagram_user": "Instagram User",
        "instagram_tag": "Instagram Hashtag",
        "reddit": "Reddit Subreddit",
        "dc": "DCInside Gallery",
        "arca": "Arca.live Channel",
        "url": "Website URL",
        "x": "X Search",
    }
    specs = []
    for line_number, line in enumerate(value.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 2:
            raise ValueError(f"Mixed Sources line {line_number} must use source | query")
        source = aliases.get(parts[0].lower(), parts[0])
        if source not in SOURCE_TYPES or source == "Mixed Sources":
            raise ValueError(f"Mixed Sources line {line_number} has an unknown source: {parts[0]}")
        title_filter = parts[2] if source == "DCInside Gallery" and len(parts) > 2 else ""
        search_mode = parts[2] if source == "X Search" and len(parts) > 2 else "Top"
        if search_mode not in ("Top", "Latest"):
            raise ValueError(f"Mixed Sources line {line_number} X mode must be Top or Latest")
        specs.append((source, parts[1], title_filter, search_mode))
    if not specs:
        raise ValueError("Mixed Sources has no source lines")
    return specs


def _is_public_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def _download_image_bytes(url, page_url):
    if not _is_public_url(url):
        raise RuntimeError("candidate URL does not resolve to a public address")
    headers = {
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": page_url,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138 Safari/537.36",
    }
    with requests.get(url, headers=headers, stream=True, timeout=(10, 30)) as response:
        response.raise_for_status()
        if not _is_public_url(response.url):
            raise RuntimeError("candidate redirected to a non-public address")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if not content_type.startswith("image/") and content_type != "application/octet-stream":
            raise RuntimeError(f"candidate returned {content_type or 'unknown content'}")
        chunks = []
        size = 0
        for chunk in response.iter_content(64 * 1024):
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise RuntimeError("candidate image exceeds 50 MB")
            chunks.append(chunk)
    return b"".join(chunks)


def _stable_media_key(page_url, media_url):
    page = urllib.parse.urlparse(page_url)
    media = urllib.parse.urlparse(media_url)
    identity = urllib.parse.urlunparse((page.scheme.lower(), page.netloc.lower(), page.path, "", "", ""))
    identity += "\n" + urllib.parse.urlunparse((media.scheme.lower(), media.netloc.lower(), media.path, "", "", ""))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _image_frames(data, scan_fps=2.0, max_seconds=30):
    try:
        source = Image.open(io.BytesIO(data))
    except OSError as error:
        raise RuntimeError(f"candidate is not a valid image: {error}") from error
    frame_count = getattr(source, "n_frames", 1)
    if frame_count <= 1:
        yield ImageOps.exif_transpose(source).convert("RGB"), 0, 0.0
        return
    elapsed_ms = 0
    next_sample_ms = 0.0
    sample_interval_ms = 1000.0 / max(scan_fps, 0.01)
    for frame_index in range(frame_count):
        source.seek(frame_index)
        duration_ms = max(int(source.info.get("duration", 100)), 1)
        if elapsed_ms >= next_sample_ms:
            yield ImageOps.exif_transpose(source.copy()).convert("RGB"), frame_index, elapsed_ms / 1000.0
            next_sample_ms += sample_interval_ms
        elapsed_ms += duration_ms
        if elapsed_ms > max_seconds * 1000:
            break
def _download_image(url, page_url):
    return next(_image_frames(_download_image_bytes(url, page_url), 1, 1))[0]


def _read_local_image_bytes(path):
    size = os.path.getsize(path)
    if size > MAX_IMAGE_BYTES:
        raise RuntimeError("candidate image exceeds 50 MB")
    with open(path, "rb") as file:
        return file.read()


def _candidate_frames(source_name, candidate, media_mode, scan_fps, max_seconds, enforce_history, persist_history=False):
    if candidate.get("frame_bytes") is not None:
        with Image.open(io.BytesIO(candidate["frame_bytes"])) as frame:
            yield frame.convert("RGB"), candidate.get("frame_index", 0), candidate.get("frame_time", 0.0)
        return
    local_path = candidate.get("local_path")
    data = _read_local_image_bytes(local_path) if local_path else _download_image_bytes(candidate["url"], candidate["page_url"])
    with Image.open(io.BytesIO(data)) as source:
        image_format = (source.format or "image").lower()
        candidate["media_format"] = image_format
        candidate["media_extension"] = ".jpg" if image_format == "jpeg" else "." + image_format
        if getattr(source, "n_frames", 1) > 1:
            candidate["media_type"] = "animated_image"
            candidate["media_id"] = hashlib.sha256(
                (candidate["page_url"] + "\n" + candidate["url"]).encode("utf-8")
            ).hexdigest()
            if not _claim_media(source_name, candidate, enforce_history, persist_history):
                return
    if media_mode == "Images + Video/GIF":
        yield from _image_frames(data, scan_fps, max_seconds)
    else:
        yield next(_image_frames(data, 1, 1))


def _local_directory_candidates(directory, recursive, read_dimensions=True):
    candidates = []
    walk = os.walk(directory)
    for current_directory, child_directories, filenames in walk:
        child_directories.sort(key=str.casefold)
        filenames.sort(key=str.casefold)
        for filename in filenames:
            _check_interrupted()
            extension = os.path.splitext(filename)[1].lower()
            if extension not in LOCAL_IMAGE_EXTENSIONS:
                continue
            path = os.path.abspath(os.path.join(current_directory, filename))
            try:
                stat = os.stat(path)
                width = height = 0
                frame_count = 1
                if read_dimensions:
                    with Image.open(path) as image:
                        width, height = image.size
                        frame_count = getattr(image, "n_frames", 1)
            except (OSError, ValueError):
                continue
            identity = f"{os.path.normcase(path)}\n{stat.st_size}\n{stat.st_mtime_ns}"
            candidates.append({
                "key": "local:" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                "url": path,
                "page_url": current_directory,
                "local_path": path,
                "filename": filename,
                "relative_path": os.path.relpath(path, directory),
                "width": width,
                "height": height,
                "post_date": datetime.datetime.fromtimestamp(stat.st_mtime, datetime.timezone.utc).isoformat(),
                "media_type": "animated_image" if frame_count > 1 else "image",
                "file_size": stat.st_size,
            })
        if not recursive:
            break
    candidates.sort(key=lambda candidate: candidate["relative_path"].casefold())
    return candidates


def _dhash(image):
    pixels = np.asarray(image.convert("L").resize((9, 8), Image.Resampling.LANCZOS))
    bits = pixels[:, 1:] > pixels[:, :-1]
    value = sum(int(bit) << index for index, bit in enumerate(bits.flat))
    return f"{value:016x}"


def _image_history_hash(source, image):
    if source != LOCAL_DIRECTORY_SOURCE:
        return _dhash(image)
    rgb = image.convert("RGB")
    digest = hashlib.sha256()
    digest.update(f"{rgb.width}x{rgb.height}\0".encode("ascii"))
    digest.update(rgb.tobytes())
    return digest.hexdigest()


def _pil_to_tensor(image):
    array = np.asarray(image, dtype=np.float32) / 255.0
    return torch.from_numpy(array)[None,]


def _preview_data_url(image):
    preview = image.copy()
    preview.thumbnail((640, 640), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    preview.save(buffer, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _send_auto_feed_preview(input_image, output_image, candidate, unique_id):
    if unique_id is None:
        return
    metadata = {
        key: value for key, value in candidate.items()
        if key not in ("frame_bytes", "_analysis", "_content_hash")
    }
    PromptServer.instance.send_sync(
        "jh-auto-feed-preview",
        {
            "node_id": str(unique_id),
            "input_image": _preview_data_url(input_image),
            "output_image": _preview_data_url(output_image),
            "metadata": metadata,
        },
        PromptServer.instance.client_id,
    )


def _send_auto_feed_status(unique_id, status):
    if unique_id is None:
        return
    PromptServer.instance.send_sync(
        "jh-auto-feed-status",
        {"node_id": str(unique_id), "status": status},
        PromptServer.instance.client_id,
    )


def _model_choices():
    models = [name for name in folder_paths.get_filename_list("ultralytics") if name.lower().endswith(".pt")]
    return sorted(models) or ["No Ultralytics models found"]


def _preferred_model(models, text, fallback):
    for model in models:
        if text.lower() in model.lower():
            return model
    return fallback


class WomanSubjectDetector:
    def __init__(self, woman_model, person_model, face_model=None):
        self.woman_model = self._load(woman_model)
        self.person_model = self._load(person_model)
        self.face_model = self._load(face_model) if face_model else None

    @staticmethod
    def _load(name):
        path = folder_paths.get_full_path("ultralytics", name)
        if not path:
            raise RuntimeError(f"Ultralytics model not found: {name}")
        from ultralytics import YOLO
        return YOLO(path)

    @staticmethod
    def _boxes(result, image_area):
        boxes = []
        if result.boxes is None:
            return boxes
        for xyxy, confidence in zip(result.boxes.xyxy.cpu().tolist(), result.boxes.conf.cpu().tolist()):
            x1, y1, x2, y2 = xyxy
            area = max(0.0, x2 - x1) * max(0.0, y2 - y1) / image_area
            boxes.append((x1, y1, x2, y2, float(confidence), area))
        return boxes

    def analyze(self, image):
        preview = image.copy()
        preview.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        device = 0 if torch.cuda.is_available() else "cpu"
        woman_result = self.woman_model.predict(preview, imgsz=1024, conf=0.2, device=device, verbose=False)[0]
        width, height = preview.size
        image_area = width * height
        woman_boxes = self._boxes(woman_result, image_area)
        if not woman_boxes:
            return {"score": 0.0, "face_confidence": 0.0, "crop_box": None, "woman_count": 0, "person_count": 0}
        face = max(woman_boxes, key=lambda box: box[4] * (1.0 + min(box[5] / 0.04, 1.0)))

        if self.face_model is not None:
            face_result = self.face_model.predict(preview, imgsz=640, conf=0.2, device=device, verbose=False)[0]
            face_boxes = self._boxes(face_result, image_area)
            woman_center = ((face[0] + face[2]) / 2, (face[1] + face[3]) / 2)
            matching_faces = [
                box for box in face_boxes
                if (face[0] <= (box[0] + box[2]) / 2 <= face[2] and face[1] <= (box[1] + box[3]) / 2 <= face[3])
                or (box[0] <= woman_center[0] <= box[2] and box[1] <= woman_center[1] <= box[3])
            ]
            face_confidence = max((box[4] for box in matching_faces), default=0.0)
        else:
            face_confidence = 0.0

        person_result = self.person_model.predict(preview, imgsz=640, conf=0.2, device=device, verbose=False)[0]
        person_boxes = self._boxes(person_result, image_area)

        face_x = (face[0] + face[2]) / (2 * width)
        face_y = (face[1] + face[3]) / (2 * height)
        center = max(0.0, 1.0 - (((face_x - 0.5) ** 2 + (face_y - 0.45) ** 2) ** 0.5) / 0.72)
        face_presence = min(face[5] / 0.025, 1.0)

        if person_boxes:
            largest_person = max(box[5] for box in person_boxes)
            total_person = sum(box[5] for box in person_boxes)
            subject_area = min(largest_person / 0.35, 1.0)
            dominance = min(largest_person / max(total_person, 1e-6), 1.0)
        else:
            subject_area = face_presence * 0.6
            dominance = 0.7
        score = min(1.0, 0.45 * face[4] + 0.2 * center + 0.15 * face_presence + 0.1 * subject_area + 0.1 * dominance)

        face_center = ((face[0] + face[2]) / 2, (face[1] + face[3]) / 2)
        matched_people = [
            person for person in person_boxes
            if person[0] <= face_center[0] <= person[2] and person[1] <= face_center[1] <= person[3]
        ]
        if matched_people:
            crop_source = max(matched_people, key=lambda box: box[5])
        else:
            face_width = face[2] - face[0]
            face_height = face[3] - face[1]
            crop_source = (
                face_center[0] - face_width * 1.4,
                face_center[1] - face_height * 0.8,
                face_center[0] + face_width * 1.4,
                face_center[1] + face_height * 3.2,
                face[4],
                face[5],
            )
        scale_x = image.width / width
        scale_y = image.height / height
        crop_box = (
            crop_source[0] * scale_x,
            crop_source[1] * scale_y,
            crop_source[2] * scale_x,
            crop_source[3] * scale_y,
        )
        subject_aspect = max(0.0, crop_box[3] - crop_box[1]) / max(1.0, crop_box[2] - crop_box[0])
        return {
            "score": score,
            "face_confidence": face_confidence,
            "crop_box": crop_box,
            "woman_count": len(woman_boxes),
            "person_count": len(person_boxes),
            "subject_aspect": subject_aspect,
        }

    def auto_orient(self, image, threshold):
        analysis = self.analyze(image)
        transform = "EXIF"
        if image.width > image.height * 1.2:
            best_value = analysis["score"] + 0.18 * min(analysis.get("subject_aspect", 0.0), 2.0)
            minimum_gain = 0.06 if analysis["score"] < max(threshold, 0.65) else 0.09
            for name, transpose in (("Rotate 90", Image.Transpose.ROTATE_90), ("Rotate 270", Image.Transpose.ROTATE_270)):
                rotated = image.transpose(transpose)
                rotated_analysis = self.analyze(rotated)
                rotated_value = rotated_analysis["score"] + 0.18 * min(rotated_analysis.get("subject_aspect", 0.0), 2.0)
                if rotated_value > best_value + minimum_gain:
                    image = rotated
                    analysis = rotated_analysis
                    transform = name
                    best_value = rotated_value
        return image, analysis, transform


class _ContinueSourceSearch(Exception):
    pass


class _RetrySourceLater(_ContinueSourceSearch):
    pass


class _SimpleImagePassThrough:
    @staticmethod
    def analyze(image):
        return {
            "score": 0.0,
            "face_confidence": 0.0,
            "crop_box": None,
            "woman_count": 0,
            "person_count": 0,
        }


def _crop_primary_woman(image, analysis, crop_mode, margin):
    crop_box = analysis["crop_box"]
    if not crop_box or crop_mode == "None":
        return image, None
    is_composite = analysis["woman_count"] > 1 or analysis["person_count"] > 1
    if crop_mode == "Auto Composite" and (not is_composite or image.width < image.height * 1.25):
        return image, None

    x1, y1, x2, y2 = crop_box
    width = x2 - x1
    height = y2 - y1
    x1 = max(0, int(x1 - width * margin))
    y1 = max(0, int(y1 - height * margin))
    x2 = min(image.width, int(x2 + width * margin))
    y2 = min(image.height, int(y2 + height * margin))
    if x2 - x1 < 128 or y2 - y1 < 128 or (x2 - x1) * (y2 - y1) > image.width * image.height * 0.9:
        return image, None
    return image.crop((x1, y1, x2, y2)), (x1, y1, x2, y2)


def _make_driver(headless):
    if webdriver is None:
        raise RuntimeError("Selenium is not installed. Run pip install -r requirements.txt with ComfyUI's Python.")
    _cleanup_stale_driver()
    os.makedirs(PROFILE_DIR, exist_ok=True)
    options = webdriver.ChromeOptions()
    options.binary_location = _find_chrome()
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-notifications")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--no-first-run")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")
    options.add_argument("--window-size=1440,1200")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    if headless:
        options.add_argument("--headless=new")
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        options.add_argument("--no-sandbox")
    try:
        driver = webdriver.Chrome(options=options)
    except WebDriverException as error:
        raise RuntimeError("Chrome could not start. Run JH Browser Session Setup again if the login profile is open.") from error
    _remember_driver(driver)
    try:
        driver.set_page_load_timeout(45)
        driver.set_script_timeout(20)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })
    except WebDriverException:
        _close_driver(driver)
        raise
    return driver


def _browser_translation_text(driver, provider):
    if provider == "Google":
        return driver.execute_script("""
            return Array.from(document.querySelectorAll('c-wiz[role="region"] span.ryNqvb'))
                .map(element => element.innerText.trim())
                .filter(Boolean)
                .join(' ');
        """)
    return driver.execute_script("""
        const element = document.querySelector('div[class*="target-area"] div[role="textbox"]');
        return element ? element.innerText.trim() : '';
    """)


@functools.lru_cache(maxsize=128)
def _translate_text(provider, text, target_language):
    text = str(text or "").strip()
    if not text or provider == "Off":
        return ""
    limits = {"Google": 5000, "Papago": 3000}
    if provider not in limits:
        raise ValueError(f"Unknown translation provider: {provider}")
    original_length = len(text)
    limit = limits[provider]
    truncated = original_length > limit
    if truncated:
        text = text[:limit].rstrip()
        print(f"[JH Translation] {provider} input truncated from {original_length} to {len(text)} characters.")

    if provider == "Google":
        url = "https://translate.google.com/?" + urllib.parse.urlencode({
            "sl": "auto", "tl": target_language, "text": text, "op": "translate",
        })
    else:
        url = "https://papago.naver.com/?" + urllib.parse.urlencode({
            "sk": "auto", "tk": target_language, "st": text,
        })

    driver = _make_driver(True)
    try:
        driver.get(url)
        translated = _interruptible_wait(driver, 30, lambda active_driver: _browser_translation_text(active_driver, provider))
        if truncated:
            return f"[번역 잘림: {provider} 제한으로 원문 {original_length:,}자 중 앞 {len(text):,}자만 번역됨]\n{translated}"
        return translated
    except TimeoutException as error:
        raise RuntimeError(f"{provider} translation result did not appear within 30 seconds") from error
    except WebDriverException as error:
        raise RuntimeError(f"{provider} browser translation failed: {error.msg}") from error
    finally:
        _close_driver(driver)


def translate_to_korean(provider, text):
    return _translate_text(provider, text, "ko")


def translate_to_english(provider, text):
    return _translate_text(provider, text, "en")


def _decode_js_url(value):
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")


def _google_candidates(driver, search_url):
    candidates = []
    pattern = re.compile(r'\["(https?[^"\\]*(?:\\.[^"\\]*)*)",(\d+),(\d+)\]')
    for match in pattern.finditer(driver.page_source):
        url = _decode_js_url(match.group(1))
        width, height = int(match.group(2)), int(match.group(3))
        if min(width, height) < 256 or "gstatic.com/images/branding" in url:
            continue
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        candidates.append({"key": key, "url": url, "page_url": search_url, "width": width, "height": height})
    if candidates:
        candidates.sort(key=lambda item: "gstatic.com" in urllib.parse.urlparse(item["url"]).hostname)
        return candidates
    return _dom_image_candidates(driver, search_url, "")


def _dom_image_candidates(driver, source_url, container_selector):
    script = """
        const root = arguments[0] ? document.querySelector(arguments[0]) : document;
        if (!root) return [];
        return Array.from(root.querySelectorAll('img')).map(img => {
            const post = img.closest('shreddit-post');
            const article = img.closest('article');
            const anchor = img.closest('a');
            const postAnchor = img.closest('a[href*="/p/"], a[href*="/reel/"]');
            const container = post || article;
            const altDate = (img.alt || '').match(/\bon ([A-Z][a-z]+ \d{1,2}, \d{4})(?:[,.]|$)/)?.[1] || '';
            const src = img.dataset.src || img.dataset.original || img.dataset.lazySrc || img.currentSrc || img.src || '';
            return {
                url: src ? new URL(src, location.href).href : '',
                page_url: post?.permalink ? new URL(post.permalink, location.origin).href : (postAnchor?.href || anchor?.href || location.href),
                width: img.naturalWidth || Number(img.getAttribute('width')) || 0,
                height: img.naturalHeight || Number(img.getAttribute('height')) || 0,
                title: post?.getAttribute('post-title') || img.alt || '',
                rank_score: Number(post?.getAttribute('score') || 0),
                comment_count: Number(post?.getAttribute('comment-count') || 0),
                post_date: container?.querySelector('time')?.getAttribute('datetime') || post?.getAttribute('created-timestamp') || altDate,
                is_pinned: Boolean(postAnchor?.querySelector('svg[aria-label="Pinned post"]'))
            };
        });
    """
    items = driver.execute_script(script, container_selector)
    candidates = []
    for item in items:
        url = item.get("url", "")
        width = item.get("width", 0)
        height = item.get("height", 0)
        if not url.startswith(("http://", "https://")) or (width and height and min(width, height) < 180):
            continue
        item["page_url"] = item.get("page_url") or source_url
        item["key"] = hashlib.sha256((item["page_url"] + "\n" + url).encode("utf-8")).hexdigest()
        candidates.append(item)
    return candidates


def _website_container(source_url):
    parsed = urllib.parse.urlparse(source_url)
    if parsed.hostname in ("arca.live", "www.arca.live") and parsed.path.startswith("/b/"):
        return ".article-body, .article-content, .fr-view, article"
    if parsed.hostname in ("m.dcinside.com", "gall.dcinside.com") and "/board/" in parsed.path:
        return ".write_div"
    return ""


def _website_candidate_key(source_url, media_url):
    parsed = urllib.parse.urlparse(media_url)
    if parsed.hostname in ("ac.arca.live", "da.arca.live"):
        media_url = urllib.parse.urlunparse(parsed._replace(query="", fragment=""))
    return hashlib.sha256((source_url + "\n" + media_url).encode("utf-8")).hexdigest()


def _instagram_engagement(driver, candidates):
    pages = {}
    for candidate in candidates:
        parsed = urllib.parse.urlparse(candidate.get("page_url", ""))
        if parsed.hostname not in ("www.instagram.com", "instagram.com") or not parsed.path.startswith(("/p/", "/reel/")):
            continue
        pages.setdefault(candidate["page_url"], []).append(candidate)
    if not pages:
        return candidates

    original_handle = driver.current_window_handle
    opened_tab = False
    try:
        driver.switch_to.new_window("tab")
        opened_tab = True
        for page_url, page_candidates in pages.items():
            _check_interrupted()
            try:
                driver.get(page_url)
                _interruptible_wait(driver, 15, lambda current: current.execute_script("return document.readyState") == "complete")
                engagement = driver.execute_script(r"""
                    const totals = {like_count: null, comment_count: null};
                    const numberValue = value => {
                        if (typeof value === 'number') return value;
                        const text = String(value || '').replaceAll(',', '').trim();
                        const match = text.match(/([0-9]+(?:\.[0-9]+)?)\s*([KMB]|만|천)?/i);
                        if (!match) return null;
                        const scale = {k: 1e3, m: 1e6, b: 1e9, '만': 1e4, '천': 1e3}[String(match[2] || '').toLowerCase()] || 1;
                        return Math.round(Number(match[1]) * scale);
                    };
                    const visit = value => {
                        if (Array.isArray(value)) {
                            value.forEach(visit);
                            return;
                        }
                        if (!value || typeof value !== 'object') return;
                        const type = String(value.interactionType?.['@type'] || value.interactionType || value['@type'] || '');
                        const count = numberValue(value.userInteractionCount);
                        if (count !== null && type.includes('LikeAction')) totals.like_count = count;
                        if (count !== null && type.includes('CommentAction')) totals.comment_count = count;
                        const commentCount = numberValue(value.commentCount);
                        if (commentCount !== null) totals.comment_count = commentCount;
                        Object.values(value).forEach(visit);
                    };
                    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
                        try { visit(JSON.parse(script.textContent)); } catch {}
                    }
                    const description = document.querySelector('meta[property="og:description"]')?.content || '';
                    const likes = description.match(/([0-9.,]+\s*(?:[KMB]|만|천)?)\s*(?:likes?|개의?\s*좋아요)/i) || description.match(/좋아요\s*([0-9.,]+\s*(?:만|천)?)/);
                    const comments = description.match(/([0-9.,]+\s*(?:[KMB]|만|천)?)\s*(?:comments?|개의?\s*댓글)/i) || description.match(/댓글\s*([0-9.,]+\s*(?:만|천)?)/);
                    if (totals.like_count === null && likes) totals.like_count = numberValue(likes[1]);
                    if (totals.comment_count === null && comments) totals.comment_count = numberValue(comments[1]);
                    return {like_count: totals.like_count || 0, comment_count: totals.comment_count || 0};
                """)
            except WebDriverException:
                continue
            for candidate in page_candidates:
                candidate.update(engagement)
    except WebDriverException:
        return candidates
    finally:
        if opened_tab:
            driver.close()
        driver.switch_to.window(original_handle)
    return candidates


def _title_matches(title, title_filter):
    if not title_filter:
        return True
    if title_filter.startswith("re:"):
        try:
            return re.search(title_filter[3:], title, re.IGNORECASE) is not None
        except re.error as error:
            raise ValueError(f"Invalid title filter regex: {error}") from error
    return title_filter.casefold() in title.casefold()


def _dcinside_list_url(source_url, title_filter, page=1):
    parsed = urllib.parse.urlparse(source_url)
    query = urllib.parse.parse_qs(parsed.query)
    if page > 1 and (not title_filter or title_filter.startswith("re:")):
        query["page"] = [str(page)]
    else:
        query.pop("page", None)
    if not title_filter or title_filter.startswith("re:"):
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))
    if query.get("id") == ["dcbest"]:
        query.setdefault("_dcbest", ["9"])
    query["s_type"] = ["search_subject_memo"]
    query["s_keyword"] = [title_filter]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))


def _arca_list_url(source_url, page):
    parsed = urllib.parse.urlparse(source_url)
    query = urllib.parse.parse_qs(parsed.query)
    query["p"] = [str(max(1, page))]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))


def _dc_post_key(page_url):
    query = urllib.parse.parse_qs(urllib.parse.urlparse(page_url).query)
    gallery_id = query.get("id", [""])[0]
    post_no = query.get("no", [""])[0]
    return "dcpost:" + hashlib.sha256(f"{gallery_id}\n{post_no}".encode("utf-8")).hexdigest()


def _known_dc_post_keys():
    with DATABASE_LOCK, _open_database() as connection:
        rows = connection.execute(
            "SELECT metadata FROM seen WHERE source = 'DCInside Gallery' AND status = 'accepted'"
        )
        known = set()
        for row in rows:
            try:
                page_url = json.loads(row[0] or "{}").get("page_url", "")
            except json.JSONDecodeError:
                continue
            if page_url:
                known.add(_dc_post_key(page_url))
        return known


def _dcinside_candidates(driver, source_url, title_filter, post_limit, media_mode="Images + Video/GIF",
                         video_scan_fps=2.0, video_max_seconds=30, max_candidates=20, enforce_history=True,
                         detector=None, woman_threshold=0.55, face_check=False, face_confidence=0.4):
    posts = driver.execute_script("""
        const rows = document.querySelectorAll('tr.ub-content[data-no]');
        return Array.from(rows).map(row => {
            const anchor = row.querySelector('.gall_tit a[href*="/view/"]');
            const pageUrl = anchor ? new URL(anchor.getAttribute('href'), location.origin).href : '';
            return {
                no: row.dataset.no || (pageUrl ? new URL(pageUrl).searchParams.get('no') : '') || '',
                title: row.querySelector('.gall_tit')?.innerText?.trim() || '',
                page_url: pageUrl,
                rank_score: Number(row.querySelector('.gall_recommend')?.innerText || 0),
                view_count: Number(row.querySelector('.gall_count')?.innerText || 0),
                comment_count: Number((row.querySelector('.reply_num')?.innerText || '').replace(/[^0-9]/g, '') || 0),
                post_date: row.querySelector('.gall_date')?.getAttribute('title') || row.querySelector('.gall_date')?.innerText?.trim() || ''
            };
        }).filter(post => post.page_url);
    """)
    posts = [post for post in posts if _title_matches(post["title"], title_filter)][:post_limit]
    candidates = []
    for post in posts:
        _check_interrupted()
        driver.get(post["page_url"])
        _interruptible_wait(driver, 15, lambda current: current.execute_script("return document.readyState") == "complete")
        try:
            _interruptible_wait(driver, 3, lambda current: current.find_elements("css selector", ".writing_view_box img"))
        except WebDriverException:
            pass
        images = driver.execute_script("""
            return Array.from(document.querySelectorAll('.writing_view_box img')).map(img => ({
                url: img.dataset.original || img.getAttribute('data-src') || img.currentSrc || img.src || '',
                width: img.naturalWidth || 0,
                height: img.naturalHeight || 0
            }));
        """)
        for image_index, image in enumerate(images):
            if not image["url"].startswith(("http://", "https://")) or min(image["width"], image["height"]) < 180:
                continue
            post_key = _dc_post_key(post["page_url"])
            candidates.append({
                "key": f"{post_key}:image:{image_index}",
                "post_key": post_key,
                "url": image["url"],
                "page_url": post["page_url"],
                "width": image["width"],
                "height": image["height"],
                "title": post["title"],
                "rank_score": post["rank_score"],
                "view_count": post["view_count"],
                "comment_count": post["comment_count"],
                "post_date": post["post_date"],
            })
        if media_mode == "Images + Video/GIF":
            video_candidates = _video_frame_candidates(
                driver, "DCInside Gallery", source_url, video_scan_fps, video_max_seconds, max_candidates,
                enforce_history, detector, woman_threshold, face_check, face_confidence,
            )
            for candidate in video_candidates:
                candidate["title"] = post["title"]
                candidate["rank_score"] = post["rank_score"]
                candidate["view_count"] = post["view_count"]
                candidate["comment_count"] = post["comment_count"]
                candidate["post_date"] = post["post_date"]
                candidate["post_key"] = _dc_post_key(post["page_url"])
            candidates.extend(video_candidates)
            if any(candidate.get("_analysis", {}).get("score", 0.0) >= woman_threshold for candidate in video_candidates):
                return candidates
    return candidates


def _arca_candidates(driver, source_url, title_filter, post_limit, min_popularity=0, min_comments=0, browser_state=None):
    posts = driver.execute_script("""
        return Array.from(document.querySelectorAll('a.vrow.column:not(.notice)')).map(row => ({
            title: row.querySelector('.title')?.innerText?.trim() || '',
            page_url: row.href || '',
            rank_score: Number((row.querySelector('.col-rate')?.innerText || '0').replaceAll(',', '')) || 0,
            comment_count: Number((row.querySelector('.comment-count')?.innerText || '0').replace(/[^0-9]/g, '')) || 0,
            post_date: row.querySelector('time')?.getAttribute('datetime') || row.querySelector('time')?.innerText?.trim() || ''
        })).filter(post => post.page_url);
    """)
    if browser_state is not None:
        browser_state["arca_post_count"] = len(posts)
        browser_state["arca_post_signature"] = hashlib.sha256("\n".join(post["page_url"] for post in posts).encode("utf-8")).hexdigest()
    posts = [
        post for post in posts
        if _title_matches(post["title"], title_filter)
        and post["rank_score"] >= min_popularity
        and post["comment_count"] >= min_comments
    ]
    matching_posts = len(posts)
    if post_limit > 0:
        posts = posts[:post_limit]
    if browser_state is not None:
        browser_state["arca_matching_posts"] = matching_posts
        browser_state["arca_inspected_posts"] = len(posts)
    candidates = []
    consecutive_failures = 0
    for post in posts:
        _check_interrupted()
        result = driver.execute_async_script("""
            const url = arguments[0];
            const done = arguments[arguments.length - 1];
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 15000);
            fetch(url, {credentials: 'include', signal: controller.signal, cache: 'no-store'})
                .then(async response => ({status: response.status, html: await response.text()}))
                .then(({status, html}) => {
                    clearTimeout(timer);
                    const documentCopy = new DOMParser().parseFromString(html, 'text/html');
                    const title = documentCopy.title || '';
                    if (title.includes('Just a moment')) {
                        done({verification: true, status, images: []});
                        return;
                    }
                    const root = documentCopy.querySelector('.article-body .article-content, .article-content.fr-view, .article-body .fr-view');
                    if (!root) {
                        done({error: `HTTP ${status}: article content was not found`, images: []});
                        return;
                    }
                    const images = Array.from(root.querySelectorAll('img')).map(img => {
                        const src = img.dataset.src || img.dataset.original || img.getAttribute('src') || '';
                        return {
                            url: src ? new URL(src, url).href : '',
                            width: Number(img.getAttribute('width')) || 0,
                            height: Number(img.getAttribute('height')) || 0
                        };
                    });
                    done({status, images});
                })
                .catch(error => {
                    clearTimeout(timer);
                    done({error: error?.name === 'AbortError' ? 'request timed out after 15 seconds' : String(error), images: []});
                });
        """, post["page_url"])
        if result.get("verification"):
            raise RuntimeError("Arca.live needs browser verification. Run JH Browser Session Setup with Arca.live, finish verification, and close that Chrome window.")
        if result.get("error"):
            consecutive_failures += 1
            if consecutive_failures >= 3:
                raise RuntimeError(f"Arca.live stopped after 3 consecutive post-load failures. Last failure: {result['error']}")
            continue
        consecutive_failures = 0
        images = result.get("images", [])
        for image in images:
            if not image["url"].startswith(("http://", "https://")):
                continue
            candidates.append({
                "key": _stable_media_key(post["page_url"], image["url"]),
                "url": image["url"],
                "page_url": post["page_url"],
                "width": image["width"],
                "height": image["height"],
                "title": post["title"],
                "rank_score": post["rank_score"],
                "comment_count": post["comment_count"],
                "post_date": post["post_date"],
            })
    return candidates


def _randomize_dc_candidates(candidates, seed, source_url, title_filter, mode):
    generator = random.Random(seed)
    groups = {}
    for candidate in candidates:
        groups.setdefault(candidate["page_url"], []).append(candidate)
    state_key = (source_url, title_filter)
    if mode == "Random Across Posts":
        post_groups = list(groups.items())
        generator.shuffle(post_groups)
        for _, group in post_groups:
            generator.shuffle(group)
        with SESSION_LOCK:
            last_post = LAST_DC_OUTPUT_POSTS.get(state_key)
        if len(post_groups) > 1 and post_groups[0][0] == last_post:
            post_groups.append(post_groups.pop(0))
        ordered = []
        while post_groups:
            remaining = []
            for page_url, group in post_groups:
                ordered.append(group.pop())
                if group:
                    remaining.append((page_url, group))
            generator.shuffle(remaining)
            post_groups = remaining
        return ordered

    with SESSION_LOCK:
        page_url = ACTIVE_DC_POSTS.get(state_key)
        if page_url not in groups:
            page_url = generator.choice(list(groups)) if groups else None
            if page_url:
                ACTIVE_DC_POSTS[state_key] = page_url
            else:
                ACTIVE_DC_POSTS.pop(state_key, None)
    selected = groups.get(page_url, [])
    generator.shuffle(selected)
    return selected


def _x_candidates(driver, source_url):
    items = driver.execute_script("""
        return Array.from(document.querySelectorAll('article')).flatMap(article => {
            const status = article.querySelector('a[href*="/status/"]');
            const pageUrl = status ? new URL(status.getAttribute('href'), location.origin).href : '';
            const title = article.querySelector('[data-testid="tweetText"]')?.innerText || '';
            const likeLabel = article.querySelector('[data-testid="like"]')?.getAttribute('aria-label') || '';
            const replyLabel = article.querySelector('[data-testid="reply"]')?.getAttribute('aria-label') || '';
            const score = Number((likeLabel.match(/[0-9,]+/)?.[0] || '0').replaceAll(',', ''));
            const comments = Number((replyLabel.match(/[0-9,]+/)?.[0] || '0').replaceAll(',', ''));
            const postDate = article.querySelector('time')?.getAttribute('datetime') || '';
            return Array.from(article.querySelectorAll('[data-testid="tweetPhoto"] img')).map(img => ({
                url: img.currentSrc || img.src || '',
                page_url: pageUrl,
                width: img.naturalWidth || 0,
                height: img.naturalHeight || 0,
                title: title,
                rank_score: score,
                comment_count: comments,
                post_date: postDate
            }));
        });
    """)
    candidates = []
    for item in items:
        if not item["url"].startswith("https://pbs.twimg.com/media/"):
            continue
        parsed = urllib.parse.urlparse(item["url"])
        params = urllib.parse.parse_qs(parsed.query)
        params["name"] = ["orig"]
        item["url"] = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(params, doseq=True)))
        item["page_url"] = item.get("page_url") or source_url
        item["key"] = hashlib.sha256((item["page_url"] + "\n" + item["url"]).encode("utf-8")).hexdigest()
        candidates.append(item)
    return candidates


def _reddit_candidates(driver, source_url):
    items = driver.execute_script("""
        const containers = Array.from(document.querySelectorAll('shreddit-post'));
        for (const link of document.querySelectorAll('a[href*="/comments/"]')) {
            let container = link;
            while (container && container !== document.body) {
                const imageCount = container.querySelectorAll('img').length;
                const postLinkCount = container.querySelectorAll('a[href*="/comments/"]').length;
                if (imageCount > 0 && postLinkCount > 0 && postLinkCount <= 4) break;
                container = container.parentElement;
            }
            if (container && container !== document.body) containers.push(container);
        }
        const seen = new Set();
        return containers.flatMap(container => {
            if (seen.has(container)) return [];
            seen.add(container);
            const permalink = container.getAttribute?.('permalink') || container.querySelector('a[href*="/comments/"]')?.href || '';
            const pageUrl = permalink ? new URL(permalink, location.origin).href : '';
            const postDate = container.querySelector('time')?.getAttribute('datetime') || container.getAttribute?.('created-timestamp') || '';
            return Array.from(container.querySelectorAll('img')).map(img => ({
                url: img.currentSrc || img.src || '',
                page_url: pageUrl,
                width: img.naturalWidth || 0,
                height: img.naturalHeight || 0,
                title: container.getAttribute?.('post-title') || container.querySelector('h2')?.innerText || '',
                rank_score: Number(container.getAttribute?.('score') || 0),
                comment_count: Number(container.getAttribute?.('comment-count') || 0),
                post_date: postDate
            }));
        });
    """)
    candidates = []
    for item in items:
        parsed = urllib.parse.urlparse(item.get("url", ""))
        if parsed.hostname not in {"i.redd.it", "preview.redd.it", "external-preview.redd.it"}:
            continue
        width = item.get("width", 0)
        height = item.get("height", 0)
        if not item.get("page_url") or (width and height and min(width, height) < 180):
            continue
        item["key"] = hashlib.sha256((item["page_url"] + "\n" + item["url"]).encode("utf-8")).hexdigest()
        candidates.append(item)
    return candidates


def _reddit_redgifs_posts(driver):
    return driver.execute_script("""
        return Array.from(document.querySelectorAll('shreddit-post')).flatMap(post => {
            const contentUrl = post.getAttribute('content-href') || '';
            let parsed;
            try {
                parsed = new URL(contentUrl);
            } catch {
                return [];
            }
            if (parsed.protocol !== 'https:' || !['redgifs.com', 'www.redgifs.com'].includes(parsed.hostname) || !parsed.pathname.startsWith('/watch/')) return [];
            const permalink = post.getAttribute('permalink') || '';
            if (!permalink) return [];
            return [{
                content_url: parsed.href,
                page_url: new URL(permalink, location.origin).href,
                title: post.getAttribute('post-title') || '',
                rank_score: Number(post.getAttribute('score') || 0),
                comment_count: Number(post.getAttribute('comment-count') || 0),
                post_date: post.getAttribute('created-timestamp') || ''
            }];
        });
    """)


def _reddit_redgifs_candidates(driver, posts, scan_fps, max_seconds, max_candidates, enforce_history,
                                detector=None, woman_threshold=0.55, face_check=False, face_confidence=0.4):
    if not posts:
        return []
    original_handle = driver.current_window_handle
    opened_tab = False
    candidates = []
    try:
        driver.switch_to.new_window("tab")
        opened_tab = True
        for post in posts:
            _check_interrupted()
            if len(candidates) >= max_candidates:
                break
            try:
                driver.get(post["content_url"])
                _interruptible_wait(driver, 15, lambda current: current.find_elements("css selector", "video"))
                frames = _video_frame_candidates(
                    driver, "Reddit Subreddit", post["content_url"], scan_fps, max_seconds,
                    max_candidates - len(candidates), enforce_history, detector, woman_threshold,
                    face_check, face_confidence,
                )
            except WebDriverException:
                continue
            for candidate in frames:
                candidate.update({
                    "url": post["content_url"],
                    "page_url": post["page_url"],
                    "title": post["title"],
                    "rank_score": post["rank_score"],
                    "comment_count": post["comment_count"],
                    "post_date": post["post_date"],
                })
            candidates.extend(frames)
            accepted = any(
                candidate.get("_analysis", {}).get("score", 0) >= woman_threshold
                and (not face_check or candidate["_analysis"]["face_confidence"] >= face_confidence)
                and (not enforce_history or not _session_known_hash(candidate["_content_hash"]))
                and (not enforce_history or not _known_hash(candidate["_content_hash"]))
                for candidate in frames if candidate.get("_analysis") is not None
            )
            if accepted:
                break
    except WebDriverException:
        return candidates
    finally:
        if opened_tab:
            driver.close()
        driver.switch_to.window(original_handle)
    return candidates


def _video_frame_candidates(driver, source, source_url, scan_fps, max_seconds, max_candidates, enforce_history,
                            detector=None, woman_threshold=0.55, face_check=False, face_confidence=0.4):
    videos = [(video, None) for video in driver.find_elements("css selector", "video")]
    if source == "Reddit Subreddit":
        for player in driver.find_elements("css selector", "shreddit-player"):
            try:
                videos.extend((video, player) for video in player.shadow_root.find_elements("css selector", "video"))
            except WebDriverException:
                continue
    candidates = []
    seen_media_ids = set()
    for video_index, (video, host) in enumerate(videos):
        _check_interrupted()
        try:
            info = driver.execute_script("""
                const video = arguments[0];
                const host = arguments[1];
                const article = video.closest('article, shreddit-post') || host?.closest('shreddit-post') || host;
                let pageUrl = '';
                if (article) {
                    pageUrl = article.getAttribute?.('permalink') ||
                        article.querySelector('a[href*="/status/"], a[href*="/comments/"], a[href*="/reel/"], a[href*="/p/"]')?.href || '';
                }
                if (!pageUrl) pageUrl = location.href;
                const likeLabel = article?.querySelector('[data-testid="like"]')?.getAttribute('aria-label') || '';
                const replyLabel = article?.querySelector('[data-testid="reply"]')?.getAttribute('aria-label') || '';
                return {
                    duration: Number.isFinite(video.duration) ? video.duration : 0,
                    page_url: new URL(pageUrl, location.origin).href,
                    title: article?.getAttribute?.('post-title') || article?.innerText?.slice(0, 500) || '',
                    rank_score: Number(article?.getAttribute?.('score') || (likeLabel.match(/[0-9,]+/)?.[0] || '0').replaceAll(',', '')),
                    comment_count: Number(article?.getAttribute?.('comment-count') || (replyLabel.match(/[0-9,]+/)?.[0] || '0').replaceAll(',', '')),
                    post_date: article?.querySelector('time')?.getAttribute('datetime') || article?.getAttribute?.('created-timestamp') || '',
                    width: video.videoWidth || video.clientWidth || 0,
                    height: video.videoHeight || video.clientHeight || 0,
                    current_src: video.currentSrc || video.src || ''
                };
            """, video, host)
            driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", host or video)
            ready_info = driver.execute_async_script("""
                const video = arguments[0], done = arguments[arguments.length - 1];
                let finished = false;
                const finish = () => {
                    if (finished) return;
                    finished = true;
                    clearInterval(poll);
                    clearTimeout(timeout);
                    done({
                        ready: video.readyState >= 2 && video.videoWidth > 0 && video.videoHeight > 0,
                        duration: Number.isFinite(video.duration) ? video.duration : 0,
                        width: video.videoWidth || video.clientWidth || 0,
                        height: video.videoHeight || video.clientHeight || 0,
                        current_src: video.currentSrc || video.src || ''
                    });
                };
                const check = () => {
                    if (video.readyState >= 2 && video.videoWidth > 0 && video.videoHeight > 0) finish();
                };
                const poll = setInterval(check, 100);
                const timeout = setTimeout(finish, 5000);
                video.muted = true;
                video.playsInline = true;
                const playing = video.play();
                if (playing) playing.catch(() => {});
                check();
            """, video)
            if not ready_info.get("ready"):
                continue
            info.update(ready_info)
            duration = min(float(info.get("duration") or max_seconds), float(max_seconds))
            frame_total = max(1, int(duration * scan_fps) + 1)
            media_id = hashlib.sha256(info["page_url"].encode("utf-8")).hexdigest()
            if media_id in seen_media_ids:
                continue
            seen_media_ids.add(media_id)
            extension = os.path.splitext(urllib.parse.urlparse(info.get("current_src", "")).path)[1].lower()
            if extension not in (".mp4", ".webm", ".mov", ".m4v", ".gif"):
                extension = ".video"
            media_record = {
                "url": info.get("current_src") or f"{info['page_url']}#video-{video_index}",
                "page_url": info["page_url"],
                "media_id": media_id,
                "media_type": "video",
                "media_extension": extension,
                "rank_score": info.get("rank_score", 0),
                "comment_count": info.get("comment_count", 0),
                "post_date": info.get("post_date", ""),
            }
            if not _claim_media(source, media_record, enforce_history):
                continue
            frame_keys = [
                hashlib.sha256((media_id + f"\n{frame_index}\n{frame_index / scan_fps:.3f}").encode("utf-8")).hexdigest()
                for frame_index in range(frame_total)
            ]
            known_keys = set()
            if enforce_history:
                known_keys.update(_session_known_keys(source, frame_keys))
                known_keys.update(_known_keys(source, frame_keys))
            media_candidates = []
            accepted_frame = False
            for frame_index in range(frame_total):
                _check_interrupted()
                if len(candidates) + len(media_candidates) >= max_candidates:
                    break
                frame_time = frame_index / scan_fps
                key = frame_keys[frame_index]
                if key in known_keys:
                    continue
                seek_result = driver.execute_async_script("""
                    const video = arguments[0], target = arguments[1], done = arguments[arguments.length - 1];
                    let finished = false;
                    let timeout;
                    const finish = () => {
                        if (finished) return;
                        finished = true;
                        clearTimeout(timeout);
                        video.removeEventListener('seeked', finish);
                        done({ready: video.readyState >= 2, current_time: video.currentTime});
                    };
                    video.muted = true;
                    video.pause();
                    video.addEventListener('seeked', finish, {once: true});
                    timeout = setTimeout(finish, 2500);
                    video.currentTime = Math.min(target, Math.max(0, (Number.isFinite(video.duration) ? video.duration : target) - 0.001));
                    if (Math.abs(video.currentTime - target) < 0.05 && video.readyState >= 2) finish();
                """, video, frame_time)
                if not seek_result.get("ready"):
                    continue
                if urllib.parse.urlparse(driver.current_url).hostname in ("redgifs.com", "www.redgifs.com"):
                    data_url = driver.execute_script("""
                        const video = arguments[0];
                        const scale = Math.min(1, 1024 / Math.max(video.videoWidth, video.videoHeight));
                        const canvas = document.createElement('canvas');
                        canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
                        canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
                        canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
                        return canvas.toDataURL('image/png');
                    """, video)
                    png = base64.b64decode(data_url.split(",", 1)[1])
                else:
                    png = video.screenshot_as_png
                with Image.open(io.BytesIO(png)) as frame:
                    width, height = frame.size
                if min(width, height) < 180:
                    continue
                candidate = {
                    "key": key,
                    "url": media_record["url"],
                    "page_url": info["page_url"],
                    "width": width,
                    "height": height,
                    "title": info.get("title", ""),
                    "rank_score": info.get("rank_score", 0),
                    "comment_count": info.get("comment_count", 0),
                    "post_date": info.get("post_date", ""),
                    "media_type": "video_frame",
                    "media_id": media_id,
                    "media_extension": extension,
                    "frame_index": frame_index,
                    "frame_time": frame_time,
                    "frame_bytes": png,
                }
                media_candidates.append(candidate)
                if detector is not None:
                    with Image.open(io.BytesIO(png)) as frame:
                        frame = frame.convert("RGB")
                        candidate["_analysis"] = detector.analyze(frame)
                        candidate["_content_hash"] = _dhash(frame)
                    duplicate = enforce_history and _session_known_hash(candidate["_content_hash"])
                    if enforce_history and not duplicate:
                        duplicate = _known_hash(candidate["_content_hash"])
                    face_ok = not face_check or candidate["_analysis"]["face_confidence"] >= face_confidence
                    if candidate["_analysis"]["score"] >= woman_threshold and face_ok and not duplicate:
                        accepted_frame = True
                        break
            if not media_candidates:
                continue
            candidates.extend(media_candidates)
            if accepted_frame or len(candidates) >= max_candidates:
                return candidates
        except (OSError, WebDriverException, ValueError):
            continue
    return candidates


def _collect_candidates(source, source_url, scroll_rounds, headless, title_filter="", max_candidates=20, dc_page=1,
                        media_mode="Images + Video/GIF", video_scan_fps=2.0, video_max_seconds=30, enforce_history=True,
                        detector=None, woman_threshold=0.55, face_check=False, face_confidence=0.4,
                        driver=None, browser_state=None, arca_min_popularity=0, arca_min_comments=0):
    dc_search_key = (source_url, title_filter)
    if source == "DCInside Gallery" and title_filter and not title_filter.startswith("re:") and dc_page > 1:
        with SESSION_LOCK:
            browse_url = DC_SEARCH_PAGE_URLS.get(dc_search_key, {}).get(dc_page)
        if not browse_url:
            return []
    elif source == "Arca.live Channel":
        browse_url = _arca_list_url(source_url, dc_page)
    else:
        browse_url = _dcinside_list_url(source_url, title_filter, dc_page) if source == "DCInside Gallery" else source_url
    cache_key = (browse_url, dc_page)
    if source == "DCInside Gallery":
        with SESSION_LOCK:
            cached = DC_PAGE_CACHE.get(cache_key)
            if cached and time.monotonic() - cached[0] < DC_PAGE_CACHE_TTL:
                return [dict(candidate) for candidate in cached[1]]
    owns_driver = driver is None
    if owns_driver:
        driver = _make_driver(headless)
    browser_state = browser_state if browser_state is not None else {}
    try:
        page_reused = source not in ("DCInside Gallery", "Arca.live Channel") and browser_state.get("browse_url") == browse_url
        if not page_reused:
            _check_interrupted()
            driver.get(browse_url)
            _interruptible_wait(driver, 20, lambda current: current.execute_script("return document.readyState") == "complete")
            if source == "DCInside Gallery" and len(driver.page_source) < 500:
                raise RuntimeError("DCInside returned an empty page. Requests are temporarily blocked; stop the feed and try again later.")
            if source == "Website URL" and not _is_public_url(driver.current_url):
                raise RuntimeError("Website redirected to a non-public address")
            selectors = {
                "Google Images": "img",
                "Instagram User": "main img, main video",
                "Instagram Hashtag": "main img, main video",
                "Reddit Subreddit": "shreddit-post, a[href*='/comments/']",
                "DCInside Gallery": "tr.ub-content[data-no]",
                "Arca.live Channel": "a.vrow.column",
                "Website URL": "img",
                "X Search": "article",
            }
            selector = selectors.get(source)
            if selector:
                try:
                    _interruptible_wait(driver, 5, lambda current: current.find_elements("css selector", selector))
                except WebDriverException:
                    pass
            browser_state["browse_url"] = browse_url
            browser_state["scroll_depth"] = 0
        if source == "DCInside Gallery" and title_filter and not title_filter.startswith("re:"):
            next_url = driver.execute_script(
                "return document.querySelector('.bottom_paging_wrap.re a.search_next')?.href || ''"
            )
            if next_url:
                parsed_next = urllib.parse.urlparse(next_url)
                next_query = urllib.parse.parse_qs(parsed_next.query)
                next_query["s_keyword"] = [title_filter]
                next_url = urllib.parse.urlunparse(parsed_next._replace(query=urllib.parse.urlencode(next_query, doseq=True)))
                with SESSION_LOCK:
                    DC_SEARCH_PAGE_URLS.setdefault(dc_search_key, {})[dc_page + 1] = next_url
        if "google.com/sorry/" in driver.current_url:
            raise RuntimeError("Google blocked headless browsing. Run JH Browser Session Setup, complete the check, then use headless=false.")
        if source == "X Search" and ("/login" in driver.current_url or "/onboarding/" in driver.current_url):
            raise RuntimeError("X requires login. Run JH Browser Session Setup with X + Instagram, finish login, and close that Chrome window.")
        if source.startswith("Instagram") and "/accounts/login" in driver.current_url:
            raise RuntimeError("Instagram requires login. Run JH Browser Session Setup with X + Instagram, finish login, and close that Chrome window.")
        if source == "DCInside Gallery":
            candidates = _dcinside_candidates(
                driver, source_url, title_filter, max(5, min(max_candidates, 30)), media_mode,
                video_scan_fps, video_max_seconds, max_candidates, enforce_history, detector,
                woman_threshold, face_check, face_confidence,
            )
        elif source == "Arca.live Channel":
            candidates = _arca_candidates(
                driver, source_url, title_filter, max_candidates,
                arca_min_popularity, arca_min_comments, browser_state,
            )
        else:
            previous_depth = browser_state.get("scroll_depth", 0)
            additional_rounds = max(0, scroll_rounds - previous_depth)
            for _ in range(additional_rounds):
                _check_interrupted()
                previous_state = driver.execute_script(
                    "return [document.body.scrollHeight, document.querySelectorAll('img, video, article, shreddit-post').length]"
                )
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
                try:
                    _interruptible_wait(driver, 1.5, lambda current: current.execute_script(
                        "const before=arguments[0]; return document.body.scrollHeight!==before[0] || "
                        "document.querySelectorAll('img, video, article, shreddit-post').length!==before[1]",
                        previous_state,
                    ))
                except WebDriverException:
                    pass
            browser_state["scroll_depth"] = max(previous_depth, scroll_rounds)
        if source == "Google Images":
            candidates = _google_candidates(driver, source_url)
        elif source.startswith("Instagram"):
            candidates = _dom_image_candidates(driver, source_url, "main")
        elif source == "Reddit Subreddit":
            candidates = _reddit_candidates(driver, source_url)
            redgifs_posts = _reddit_redgifs_posts(driver) if media_mode == "Images + Video/GIF" else []
        elif source == "X Search":
            candidates = _x_candidates(driver, source_url)
        elif source == "Website URL":
            candidates = _dom_image_candidates(driver, source_url, _website_container(source_url))
            for candidate in candidates:
                candidate["page_url"] = source_url
                candidate["key"] = _website_candidate_key(source_url, candidate["url"])
        if media_mode == "Images + Video/GIF" and source in ("Instagram User", "Instagram Hashtag", "Reddit Subreddit", "X Search"):
            video_candidates = _video_frame_candidates(
                driver, source, source_url, video_scan_fps, video_max_seconds, max_candidates, enforce_history,
                detector, woman_threshold, face_check, face_confidence
            )
            if source == "Reddit Subreddit":
                video_candidates.extend(_reddit_redgifs_candidates(
                    driver, redgifs_posts, video_scan_fps, video_max_seconds, max_candidates,
                    enforce_history, detector, woman_threshold, face_check, face_confidence,
                ))
            candidates = video_candidates + candidates
    finally:
        if owns_driver:
            _close_driver(driver)
    unique = {}
    for candidate in candidates:
        unique.setdefault(candidate["key"], candidate)
    candidates = list(unique.values())
    if source == "DCInside Gallery":
        with SESSION_LOCK:
            DC_PAGE_CACHE[cache_key] = (time.monotonic(), [dict(candidate) for candidate in candidates])
    return candidates


def _unseen_candidates(source, candidates, enforce_history):
    if enforce_history:
        session_known = _session_known_keys(source, [candidate["key"] for candidate in candidates])
        candidates = [candidate for candidate in candidates if candidate["key"] not in session_known]
        known = _known_keys(source, [candidate["key"] for candidate in candidates])
        candidates = [candidate for candidate in candidates if candidate["key"] not in known]
    return candidates


def _shuffle_media_candidates(candidates, seed):
    groups = {}
    for candidate in candidates:
        groups.setdefault(candidate.get("media_id", candidate["key"]), []).append(candidate)
    ordered_groups = list(groups.values())
    random.Random(seed).shuffle(ordered_groups)
    candidates = []
    for group in ordered_groups:
        group.sort(key=lambda candidate: candidate.get("frame_index", 0))
        candidates.extend(group)
    return candidates


class JHBrowserSessionSetup:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"site": (["Instagram", "Reddit", "Google", "Arca.live", "X", "X + Instagram"],)}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("profile_directory",)
    FUNCTION = "open_browser"
    OUTPUT_NODE = True
    CATEGORY = "JH/Image"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def open_browser(self, site):
        os.makedirs(PROFILE_DIR, exist_ok=True)
        urls = {
            "Instagram": ["https://www.instagram.com/accounts/login/"],
            "Reddit": ["https://www.reddit.com/"],
            "Google": ["https://www.google.com/"],
            "Arca.live": ["https://arca.live/b/aireal"],
            "X": ["https://x.com/login"],
            "X + Instagram": ["https://x.com/login", "https://www.instagram.com/accounts/login/"],
        }
        subprocess.Popen([
            _find_chrome(), f"--user-data-dir={PROFILE_DIR}", "--profile-directory=Default", "--no-first-run", *urls[site]
        ])
        return (PROFILE_DIR,)


class JHAutoImageFeed:
    @classmethod
    def INPUT_TYPES(cls):
        models = _model_choices()
        return {
            "required": {
                "source": (SOURCE_TYPES,),
                "query": ("STRING", {"default": "portrait photography woman", "multiline": True}),
                "ranking": (["Source Order", "Random", "Largest First", "Newest First"],),
                "period": (["Hour", "Day", "Week", "Month", "Year", "All"], {"default": "Week"}),
                "safe_search": (["Off", "On"],),
                "scroll_rounds": ("INT", {"default": 3, "min": 0, "max": 20}),
                "max_candidates": ("INT", {"default": 0, "min": 0, "max": 200}),
                "woman_threshold": ("FLOAT", {"default": 0.55, "min": 0.0, "max": 1.0, "step": 0.01}),
                "woman_model": (models, {"default": _preferred_model(models, "WomanFace", models[0])}),
                "person_model": (models, {"default": _preferred_model(models, "person_yolov8n-seg", models[0])}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                "headless": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                "title_filter": ("STRING", {"default": ""}),
                "search_mode": (["Top", "Latest"],),
                "history_mode": (["Normal", "Test (No Write)", "Allow Duplicates"],),
                "history_commit": (["On Image Load", "On Workflow Success"],),
                "orientation_mode": (["EXIF", "EXIF + Auto Rotate"],),
                "crop_mode": (["None", "Auto Composite", "Primary Woman"],),
                "crop_margin": ("FLOAT", {"default": 0.08, "min": 0.0, "max": 0.5, "step": 0.01}),
                "dc_gallery": (DC_GALLERIES, {"default": DC_GALLERIES[0]}),
                "dc_gallery_custom": ("STRING", {"default": ""}),
                "dc_random_mode": (["Random Across Posts", "Exhaust One Post First"],),
                "arca_channel": ("STRING", {"default": "aireal"}),
                "reddit_mode": (["Subreddit", "Keyword Search"],),
                "reddit_subreddit": ("STRING", {"default": "portraits"}),
                "reddit_keyword": ("STRING", {"default": "portrait photography woman"}),
                "media_mode": (["Images + Video/GIF", "Images Only"],),
                "video_scan_fps": ("FLOAT", {"default": 2.0, "min": 0.25, "max": 10.0, "step": 0.25}),
                "video_max_seconds": ("INT", {"default": 30, "min": 1, "max": 300}),
                "quality_filter": ("BOOLEAN", {"default": False}),
                "min_popularity": ("INT", {"default": 10, "min": 0, "max": 1000000}),
                "min_comments": ("INT", {"default": 0, "min": 0, "max": 1000000}),
                "min_views": ("INT", {"default": 0, "min": 0, "max": 100000000}),
                "min_megapixels": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 100.0, "step": 0.1}),
                "face_check": ("BOOLEAN", {"default": False}),
                "face_confidence": ("FLOAT", {"default": 0.4, "min": 0.2, "max": 1.0, "step": 0.01}),
                "face_model": (models, {"default": _preferred_model(models, "face_yolov8n.pt", models[0])}),
                "arca_mode": (["Best", "All"],),
                "url_single_character_sheet": ("BOOLEAN", {"default": True, "label_on": "One", "label_off": "Every image"}),
                "directory_path": ("STRING", {"default": ""}),
                "directory_recursive": ("BOOLEAN", {"default": True}),
                "processing_mode": (["Advanced", "Simple"],),
                "search_timeout_minutes": ("INT", {"default": DEFAULT_SEARCH_TIMEOUT_MINUTES, "min": 1, "max": 1440}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING", "STRING", "FLOAT", "IMAGE")
    RETURN_NAMES = ("image", "source_url", "page_url", "metadata_json", "woman_subject_score", "original_image")
    OUTPUT_IS_LIST = (True, True, True, True, True, True)
    FUNCTION = "next_image"
    CATEGORY = "JH/Image"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def next_image(self, source, query, ranking, period, safe_search, scroll_rounds, max_candidates,
                   woman_threshold, woman_model, person_model, seed, headless, title_filter="", search_mode="Top",
                   history_mode="Normal", history_commit="On Image Load", orientation_mode="EXIF", crop_mode="None", crop_margin=0.08,
                   dc_gallery="직접 입력 (ID/URL)", dc_gallery_custom="", dc_random_mode="Random Across Posts",
                   arca_channel="aireal",
                   reddit_mode="Subreddit", reddit_subreddit="", reddit_keyword="",
                   media_mode="Images + Video/GIF", video_scan_fps=2.0, video_max_seconds=30, unique_id=None,
                   quality_filter=False, min_popularity=10, min_comments=0, min_views=0,
                   min_megapixels=0.0, face_check=False, face_confidence=0.4, face_model=None, arca_mode="Best",
                   url_single_character_sheet=True, directory_path="", directory_recursive=True,
                   processing_mode="Advanced", search_timeout_minutes=DEFAULT_SEARCH_TIMEOUT_MINUTES):
        _install_history_completion_hook()
        if history_mode == "On Workflow Success":
            history_mode = "Normal"
            history_commit = "On Workflow Success"
        simple_mode = processing_mode == "Simple"
        if simple_mode:
            detector = _SimpleImagePassThrough()
            woman_threshold = 0.0
            orientation_mode = "EXIF"
            crop_mode = "None"
            min_megapixels = 0.0
            face_check = False
            quality_filter = False
        else:
            detector = WomanSubjectDetector(woman_model, person_model, face_model if face_check else None)
        if source == LOCAL_DIRECTORY_SOURCE:
            query = directory_path.strip() or query
        preset_args = (source, query, title_filter, search_mode, dc_gallery, dc_gallery_custom, arca_channel, arca_mode, reddit_mode, reddit_subreddit, reddit_keyword)
        if source != "Mixed Sources":
            if source == "DCInside Gallery":
                query = _dc_gallery_query(dc_gallery, dc_gallery_custom, query)
            elif source == "Arca.live Channel":
                query = arca_channel.strip() or "aireal"
            elif source == "Reddit Subreddit":
                query = (reddit_keyword if reddit_mode == "Keyword Search" else reddit_subreddit).strip() or query
            result = self._next_from_source(source, query, ranking, period, safe_search, scroll_rounds, max_candidates,
                                            woman_threshold, seed, headless, title_filter, search_mode, history_mode,
                                            history_commit,
                                            orientation_mode, crop_mode, crop_margin, detector, dc_random_mode, reddit_mode,
                                            media_mode, video_scan_fps, video_max_seconds, quality_filter, min_popularity,
                                            min_comments, min_views, min_megapixels, face_check, face_confidence, unique_id, arca_mode,
                                            directory_recursive, processing_mode, search_timeout_minutes)
            if source == "Website URL" and url_single_character_sheet:
                best_index = max(range(len(result[0])), key=lambda index: (
                    result[4][index], result[0][index].shape[1] * result[0][index].shape[2]
                ))
                for index in range(len(result[0])):
                    is_reference = index == best_index
                    result[0][index]._jh_character_sheet_reference = is_reference
                    result[5][index]._jh_character_sheet_reference = is_reference
            _record_successful_search(*preset_args)
            return result

        specs = _mixed_sources(query)
        mixed_pass = 0
        mixed_deadline = time.monotonic() + max(1, search_timeout_minutes) * 60
        while max_candidates == 0 or mixed_pass == 0:
            if max_candidates == 0 and time.monotonic() >= mixed_deadline:
                raise RuntimeError(f"Mixed Sources search timed out after {search_timeout_minutes} minutes without a usable image.")
            random.Random(seed + _accepted_count() + _session_output_count() + mixed_pass).shuffle(specs)
            errors = []
            for index, (mixed_source, mixed_query, mixed_title_filter, mixed_search_mode) in enumerate(specs):
                try:
                    result = self._next_from_source(
                        mixed_source, mixed_query, ranking, period, safe_search, scroll_rounds, 200 if max_candidates == 0 else max_candidates,
                        woman_threshold, seed + mixed_pass * len(specs) + index, headless, mixed_title_filter, mixed_search_mode, history_mode,
                        history_commit,
                        orientation_mode, crop_mode, crop_margin, detector, dc_random_mode, "Subreddit",
                        media_mode, video_scan_fps, video_max_seconds,
                        quality_filter, min_popularity, min_comments, min_views,
                        min_megapixels, face_check, face_confidence, unique_id, arca_mode, directory_recursive,
                        processing_mode, search_timeout_minutes,
                    )
                    _record_successful_search(*preset_args)
                    return result
                except (RuntimeError, ValueError) as error:
                    errors.append(f"{mixed_source}: {error}")
            if max_candidates != 0:
                raise RuntimeError("Mixed Sources found no usable image. " + " | ".join(errors))
            mixed_pass += 1
            _send_auto_feed_status(unique_id, "Mixed Sources found no usable image. Retrying all sources...")
            _interruptible_sleep(UNLIMITED_RETRY_DELAY)

    def _next_from_source(self, source, query, ranking, period, safe_search, scroll_rounds, max_candidates,
                          woman_threshold, seed, headless, title_filter, search_mode, history_mode,
                          history_commit,
                          orientation_mode, crop_mode, crop_margin, detector, dc_random_mode, reddit_mode="Subreddit",
                          media_mode="Images + Video/GIF", video_scan_fps=2.0, video_max_seconds=30,
                          quality_filter=False, min_popularity=10, min_comments=0, min_views=0,
                          min_megapixels=0.0, face_check=False, face_confidence=0.4, unique_id=None, arca_mode="Best",
                          directory_recursive=True, processing_mode="Advanced",
                          search_timeout_minutes=DEFAULT_SEARCH_TIMEOUT_MINUTES):
        unlimited_search = max_candidates == 0 and source != LOCAL_DIRECTORY_SOURCE
        if source == LOCAL_DIRECTORY_SOURCE and max_candidates == 0:
            max_candidates = 2 ** 31 - 1
        elif unlimited_search:
            max_candidates = 200
        search_deadline = time.monotonic() + max(1, search_timeout_minutes) * 60
        search_pass = 0
        last_failure = ""
        driver = None
        browser_state = {}
        _send_auto_feed_status(unique_id, f"Starting {source} crawler...")
        try:
            if source not in ("DCInside Gallery", LOCAL_DIRECTORY_SOURCE):
                driver = _make_driver(headless)
            while unlimited_search or search_pass < MAX_SOURCE_SEARCH_PASSES:
                _check_interrupted()
                if unlimited_search and time.monotonic() >= search_deadline:
                    raise RuntimeError(
                        f"{source} search timed out after {search_timeout_minutes} minutes without a usable image."
                    )
                batch_total = "unlimited" if unlimited_search else str(MAX_SOURCE_SEARCH_PASSES)
                _send_auto_feed_status(unique_id, f"Loading candidate batch {search_pass + 1}/{batch_total}...")
                try:
                    return self._next_from_source_once(
                        source, query, ranking, period, safe_search, scroll_rounds, max_candidates,
                        woman_threshold, seed + search_pass, headless, title_filter, search_mode, history_mode,
                        history_commit,
                        orientation_mode, crop_mode, crop_margin, detector, dc_random_mode, reddit_mode,
                        media_mode, video_scan_fps, video_max_seconds, quality_filter, min_popularity,
                        min_comments, min_views, min_megapixels, face_check, face_confidence, unique_id, driver, browser_state, arca_mode,
                        directory_recursive, processing_mode, unlimited_search,
                    )
                except _ContinueSourceSearch as error:
                    last_failure = str(error)
                    search_pass += 1
                    if isinstance(error, _RetrySourceLater):
                        if time.monotonic() + UNLIMITED_RETRY_DELAY >= search_deadline:
                            raise RuntimeError(
                                f"{source} search timed out after {search_timeout_minutes} minutes without a usable image."
                            )
                        _send_auto_feed_status(unique_id, f"No usable image is currently available. Retrying in {UNLIMITED_RETRY_DELAY:g} seconds...")
                        _interruptible_sleep(UNLIMITED_RETRY_DELAY)
            error = RuntimeError(
                f"{source} found no usable image after {MAX_SOURCE_SEARCH_PASSES} candidate batches. "
                f"Last batch: {last_failure or 'no candidates were available'}."
            )
            _send_auto_feed_status(unique_id, f"Stopped: {error}")
            raise error
        except (RuntimeError, ValueError) as error:
            _send_auto_feed_status(unique_id, f"Failed: {error}")
            raise
        finally:
            if driver is not None:
                _close_driver(driver)

    def _next_from_source_once(self, source, query, ranking, period, safe_search, scroll_rounds, max_candidates,
                               woman_threshold, seed, headless, title_filter, search_mode, history_mode,
                               history_commit,
                               orientation_mode, crop_mode, crop_margin, detector, dc_random_mode, reddit_mode,
                               media_mode, video_scan_fps, video_max_seconds, quality_filter, min_popularity,
                               min_comments, min_views, min_megapixels, face_check, face_confidence, unique_id, driver, browser_state, arca_mode,
                               directory_recursive=True, processing_mode="Advanced", unlimited_search=False):
        source_url = _source_url(source, query, period, safe_search, search_mode, reddit_mode, ranking, arca_mode)
        enforce_history = history_mode != "Allow Duplicates"
        write_history = history_mode == "Normal"
        record_history = write_history and history_commit == "On Image Load"
        defer_history = write_history and history_commit == "On Workflow Success"
        collect_all = source == "Website URL"
        accepted_outputs = ([], [], [], [], [], [])
        accepted_records = []
        candidates = []
        last_scan_status = ""
        more_candidates_available = False
        current_arca_page = None
        arca_cursor_key = None
        if source == LOCAL_DIRECTORY_SOURCE:
            read_local_dimensions = processing_mode != "Simple" or ranking == "Largest First"
            collected_candidates = _local_directory_candidates(source_url, directory_recursive, read_local_dimensions)
            unseen_candidates = _unseen_candidates(source, collected_candidates, enforce_history)
            more_candidates_available = len(unseen_candidates) > max_candidates
            candidates = unseen_candidates
            last_scan_status = (
                f"directory scan found {len(collected_candidates)} images, "
                f"history/duplicate skipped {len(collected_candidates) - len(unseen_candidates)}, "
                f"ready to inspect {min(len(candidates), max_candidates)}"
            )
            _send_auto_feed_status(unique_id, last_scan_status.capitalize() + ".")
        elif source == "DCInside Gallery":
            avoid_previous_post = ranking == "Random" and dc_random_mode == "Random Across Posts"
            seen_post_keys = set()
            if avoid_previous_post and enforce_history:
                with SESSION_LOCK:
                    seen_post_keys.update(key for known_source, key in SESSION_SEEN_KEYS if known_source == source and key.startswith("dcpost:"))
                seen_post_keys.update(_known_dc_post_keys())
            with SESSION_LOCK:
                previous_post = LAST_DC_OUTPUT_POSTS.get((source_url, title_filter)) if avoid_previous_post else None
            previous_post_fallback = []
            dc_page = 1
            while True:
                _check_interrupted()
                _send_auto_feed_status(unique_id, f"Scanning DC search page {dc_page}...")
                collected_candidates = _collect_candidates(
                    source, source_url, scroll_rounds, headless, title_filter, max_candidates, dc_page,
                    media_mode, video_scan_fps, video_max_seconds, enforce_history, detector, woman_threshold,
                    face_check, face_confidence, driver, browser_state
                )
                if not collected_candidates:
                    break
                page_candidates = _filter_popular_candidates(source, collected_candidates, min_popularity, min_comments, min_views) if quality_filter else collected_candidates
                if not page_candidates:
                    dc_page += 1
                    continue
                if seen_post_keys:
                    page_candidates = [candidate for candidate in page_candidates if candidate["post_key"] not in seen_post_keys]
                if not page_candidates:
                    dc_page += 1
                    continue
                unseen = _unseen_candidates(source, page_candidates, enforce_history)[:max_candidates]
                if previous_post:
                    previous_post_fallback.extend(candidate for candidate in unseen if candidate["page_url"] == previous_post)
                    candidates.extend(candidate for candidate in unseen if candidate["page_url"] != previous_post)
                    if len(candidates) >= max_candidates:
                        break
                elif unseen:
                    candidates = unseen
                    break
                dc_page += 1
            if not candidates and previous_post_fallback:
                candidates = previous_post_fallback
        elif source == "Arca.live Channel":
            arca_min_popularity = min_popularity if quality_filter else 0
            arca_min_comments = min_comments if quality_filter else 0
            arca_cursor_key = (source_url, title_filter, arca_min_popularity, arca_min_comments)
            with SESSION_LOCK:
                resume_page = max(2, ARCA_PAGE_CURSORS.get(arca_cursor_key, 2))
            arca_page = 1
            page_scans = 0
            previous_page_signature = None
            while page_scans < MAX_ARCA_PAGE_SCANS:
                _check_interrupted()
                page_scans += 1
                browser_state.pop("arca_post_count", None)
                browser_state.pop("arca_matching_posts", None)
                browser_state.pop("arca_inspected_posts", None)
                browser_state.pop("arca_post_signature", None)
                collected_candidates = _collect_candidates(
                    source, source_url, scroll_rounds, headless, title_filter, max_candidates, arca_page,
                    media_mode, video_scan_fps, video_max_seconds, enforce_history, detector, woman_threshold,
                    face_check, face_confidence, driver, browser_state, arca_min_popularity, arca_min_comments,
                )
                post_count = browser_state.get("arca_post_count", 0)
                matching_posts = browser_state.get("arca_matching_posts", 0)
                inspected_posts = browser_state.get("arca_inspected_posts", 0)
                page_signature = browser_state.get("arca_post_signature")
                if arca_page > 1 and page_signature and page_signature == previous_page_signature:
                    last_scan_status = f"Arca pagination stopped at page {arca_page}: the site returned the previous page again"
                    post_count = 0
                    break
                previous_page_signature = page_signature
                page_candidates = _filter_popular_candidates(source, collected_candidates, min_popularity, min_comments, min_views) if quality_filter else collected_candidates
                unseen_candidates = _unseen_candidates(source, page_candidates, enforce_history)
                candidates = unseen_candidates
                last_scan_status = (
                    f"Arca page {arca_page}: posts {post_count}, matching posts {matching_posts}, inspected posts {inspected_posts}, "
                    f"images {len(collected_candidates)}, history/duplicate skipped {len(page_candidates) - len(unseen_candidates)}, "
                    f"ready to inspect {len(candidates)}"
                )
                _send_auto_feed_status(unique_id, last_scan_status + ".")
                if candidates:
                    current_arca_page = arca_page
                    if arca_page > 1:
                        with SESSION_LOCK:
                            ARCA_PAGE_CURSORS[arca_cursor_key] = arca_page
                    break
                if post_count == 0:
                    break
                if arca_page == 1:
                    arca_page = resume_page
                else:
                    arca_page += 1
                    with SESSION_LOCK:
                        ARCA_PAGE_CURSORS[arca_cursor_key] = arca_page
            if page_scans >= MAX_ARCA_PAGE_SCANS and not candidates and post_count > 0:
                raise _ContinueSourceSearch(last_scan_status)
        else:
            depth_key = (source, source_url)
            with SESSION_LOCK:
                scroll_depth = max(scroll_rounds, SOURCE_SCROLL_DEPTH.get(depth_key, 0))
            scroll_step = max(1, scroll_rounds)
            previous_keys = None
            stable_depths = 0
            retried_page = False
            collection_attempts = 0
            while collection_attempts < MAX_COLLECTION_ATTEMPTS:
                _check_interrupted()
                collection_attempts += 1
                collected_candidates = _collect_candidates(
                    source, source_url, scroll_depth, headless, title_filter, max_candidates, 1,
                    media_mode, video_scan_fps, video_max_seconds, enforce_history, detector, woman_threshold,
                    face_check, face_confidence, driver, browser_state
                )
                if quality_filter and source.startswith("Instagram") and (min_popularity > 0 or min_comments > 0):
                    collected_candidates = _instagram_engagement(driver, collected_candidates)
                page_candidates = _filter_popular_candidates(source, collected_candidates, min_popularity, min_comments, min_views) if quality_filter else collected_candidates
                unseen_candidates = _unseen_candidates(source, page_candidates, enforce_history)
                more_candidates_available = not collect_all and len(unseen_candidates) > max_candidates
                candidates = unseen_candidates if collect_all else unseen_candidates[:max_candidates]
                quality_skipped = len(collected_candidates) - len(page_candidates)
                history_skipped = len(page_candidates) - len(unseen_candidates)
                last_scan_status = (
                    f"scan {collection_attempts}/{MAX_COLLECTION_ATTEMPTS}: found {len(collected_candidates)}, "
                    f"quality filtered {quality_skipped}, history/duplicate skipped {history_skipped}, "
                    f"ready to inspect {len(candidates)}"
                )
                _send_auto_feed_status(unique_id, last_scan_status.capitalize() + ".")
                with SESSION_LOCK:
                    SOURCE_SCROLL_DEPTH[depth_key] = scroll_depth
                if candidates:
                    break
                current_keys = {candidate["key"] for candidate in collected_candidates}
                stable_depths = stable_depths + 1 if current_keys == previous_keys else 0
                if stable_depths >= 2:
                    if retried_page:
                        break
                    browser_state.clear()
                    previous_keys = None
                    stable_depths = 0
                    retried_page = True
                    continue
                previous_keys = current_keys
                scroll_depth += scroll_step
        if ranking == "Random":
            if source == "DCInside Gallery":
                candidates = _randomize_dc_candidates(candidates, seed, source_url, title_filter, dc_random_mode)
            else:
                candidates = _shuffle_media_candidates(candidates, seed)
        elif ranking == "Largest First":
            candidates.sort(key=lambda item: item.get("width", 0) * item.get("height", 0), reverse=True)
        elif ranking == "Newest First":
            if source not in (LOCAL_DIRECTORY_SOURCE, "Google Images", "DCInside Gallery", "Arca.live Channel") and period != "All":
                candidates = _filter_recent_candidates(candidates, period)
            candidates.sort(key=lambda item: (
                not item.get("is_pinned", False),
                _post_timestamp(item.get("post_date")) or float("-inf"),
            ), reverse=True)
        elif source == "Reddit Subreddit":
            candidates.sort(key=lambda item: item.get("rank_score", 0), reverse=True)
        if source == LOCAL_DIRECTORY_SOURCE:
            candidates = candidates[:max_candidates]

        rejected = {"small": 0, "session_duplicate": 0, "history_duplicate": 0, "woman": 0, "face": 0, "error": 0}
        for candidate_index, candidate in enumerate(candidates, 1):
            candidate_accepted = False
            _check_interrupted()
            action = "Loading" if processing_mode == "Simple" else "Inspecting"
            _send_auto_feed_status(unique_id, f"{action} candidate {candidate_index}/{len(candidates)}...")
            try:
                frames = _candidate_frames(
                    source, candidate, media_mode, video_scan_fps, video_max_seconds, enforce_history, record_history
                )
                last_hash = None
                last_score = None
                for image, frame_index, frame_time in frames:
                    _check_interrupted()
                    frame_key = candidate["key"] if candidate.get("media_type") == "video_frame" else f"{candidate['key']}:{frame_index}"
                    megapixels = image.width * image.height / 1_000_000
                    if megapixels < min_megapixels:
                        rejected["small"] += 1
                        _send_auto_feed_status(unique_id, f"Rejected {candidate_index}/{len(candidates)}: {megapixels:.2f} MP is below {min_megapixels:.2f} MP.")
                        last_hash = candidate.get("_content_hash") or _image_history_hash(source, image)
                        _record_session(source, frame_key)
                        continue
                    analysis = candidate.get("_analysis")
                    orientation_transform = "EXIF"
                    if orientation_mode == "EXIF + Auto Rotate":
                        image, analysis, orientation_transform = detector.auto_orient(image, woman_threshold)
                    content_hash = candidate.get("_content_hash") if orientation_transform == "EXIF" else None
                    content_hash = content_hash or _image_history_hash(source, image)
                    last_hash = content_hash
                    if enforce_history and _session_known_hash(content_hash):
                        rejected["session_duplicate"] += 1
                        _send_auto_feed_status(unique_id, f"Rejected {candidate_index}/{len(candidates)}: duplicate image from this session.")
                        _record_session(source, frame_key)
                        continue
                    if enforce_history and _known_hash(content_hash):
                        rejected["history_duplicate"] += 1
                        _send_auto_feed_status(unique_id, f"Rejected {candidate_index}/{len(candidates)}: image already exists in feed history.")
                        _record_session(source, frame_key)
                        continue
                    if analysis is None:
                        analysis = detector.analyze(image)
                    score = analysis["score"]
                    last_score = score
                    if score < woman_threshold:
                        rejected["woman"] += 1
                        _send_auto_feed_status(unique_id, f"Rejected {candidate_index}/{len(candidates)}: woman score {score:.3f} is below {woman_threshold:.3f}.")
                        _record_session(source, frame_key)
                        continue
                    if face_check and analysis["face_confidence"] < face_confidence:
                        rejected["face"] += 1
                        _send_auto_feed_status(unique_id, f"Rejected {candidate_index}/{len(candidates)}: face score {analysis['face_confidence']:.3f} is below {face_confidence:.3f}.")
                        _record_session(source, frame_key)
                        continue
                    original_size = image.size
                    input_preview = image.copy()
                    image, crop_box = _crop_primary_woman(image, analysis, crop_mode, crop_margin)
                    candidate["source"] = source
                    candidate["query"] = query
                    candidate["woman_subject_score"] = score
                    if face_check:
                        candidate["face_confidence"] = analysis["face_confidence"]
                    candidate["orientation_transform"] = orientation_transform
                    candidate["original_size"] = original_size
                    candidate["megapixels"] = megapixels
                    candidate["output_size"] = image.size
                    candidate["crop_box"] = crop_box
                    candidate["history_mode"] = history_mode
                    candidate["history_commit"] = history_commit
                    candidate["processing_mode"] = processing_mode
                    candidate["frame_index"] = frame_index
                    candidate["frame_time"] = frame_time
                    if candidate.get("media_type") != "video_frame" and frame_index:
                        candidate["media_type"] = "animated_image_frame"
                    if collect_all:
                        accepted_records.append((dict(candidate), content_hash, score))
                    else:
                        dc_post_keys = ()
                        if source == "DCInside Gallery" and dc_random_mode == "Random Across Posts":
                            dc_post_keys = (candidate["post_key"],)
                        if defer_history:
                            _stage_candidate_until_success(source, candidate, content_hash, score, dc_post_keys)
                        else:
                            _record_session(source, candidate["key"], content_hash, output=True)
                            for item_key in dc_post_keys:
                                _record_session(source, item_key)
                            if dc_post_keys:
                                with SESSION_LOCK:
                                    LAST_DC_OUTPUT_POSTS[(source_url, title_filter)] = candidate["page_url"]
                            if record_history:
                                _record_candidate(source, candidate, "accepted", content_hash, score)
                    output_candidate = {
                        key: value for key, value in candidate.items()
                        if key not in ("frame_bytes", "_analysis", "_content_hash")
                    }
                    metadata = json.dumps(output_candidate, ensure_ascii=False, separators=(",", ":"))
                    _send_auto_feed_preview(input_preview, image, output_candidate, unique_id)
                    output = (_pil_to_tensor(image), candidate["url"], candidate["page_url"], metadata, score, _pil_to_tensor(input_preview))
                    if not collect_all:
                        return tuple([value] for value in output)
                    for values, value in zip(accepted_outputs, output):
                        values.append(value)
                    candidate_accepted = True
                    _send_auto_feed_status(unique_id, f"Accepted {len(accepted_outputs[0])}/{len(candidates)} images from the URL...")
            except (OSError, requests.RequestException, RuntimeError) as error:
                rejected["error"] += 1
                _send_auto_feed_status(unique_id, f"Candidate {candidate_index}/{len(candidates)} failed: {error}")
                if not candidate_accepted:
                    candidate["error"] = str(error)
                    _record_session(source, candidate["key"])
                    if record_history:
                        _record_candidate(source, candidate, "download_failed")
                continue
            if not candidate_accepted:
                _record_session(source, candidate["key"])
                if record_history:
                    _record_candidate(source, candidate, "rejected", last_hash, last_score)
        if accepted_outputs[0]:
            for candidate, content_hash, score in accepted_records:
                if defer_history:
                    _stage_candidate_until_success(source, candidate, content_hash, score)
                else:
                    _record_session(source, candidate["key"], content_hash, output=True)
                    if record_history:
                        _record_candidate(source, candidate, "accepted", content_hash, score)
            _send_auto_feed_status(unique_id, f"Accepted {len(accepted_outputs[0])} images from the URL.")
            return accepted_outputs
        if not candidates:
            if source == "Website URL":
                raise RuntimeError(f"Website URL was exhausted. Last {last_scan_status or 'scan found no media'}.")
            if unlimited_search:
                raise _RetrySourceLater(last_scan_status or "no unseen candidates were available")
            if source == "DCInside Gallery":
                history_hint = " Previously accepted images are excluded; use history_mode=Allow Duplicates to reuse them." if enforce_history else ""
                raise RuntimeError("DCInside search was exhausted: no unused image passed the title, size, and detector filters." + history_hint)
            if source == "Arca.live Channel":
                raise RuntimeError(f"Arca.live pages were exhausted or no unseen posts matched the filters. Last {last_scan_status or 'page scan found no posts'}.")
            raise RuntimeError(f"The source was exhausted: no more unseen candidates could be loaded. Last {last_scan_status or 'scan found no media'}.")
        if source not in (LOCAL_DIRECTORY_SOURCE, "DCInside Gallery", "Arca.live Channel"):
            with SESSION_LOCK:
                SOURCE_SCROLL_DEPTH[depth_key] = scroll_depth + scroll_step
        summary = ", ".join(f"{name.replace('_', ' ')} {count}" for name, count in rejected.items() if count)
        if source == "Website URL":
            error = RuntimeError(f"Website URL was exhausted. Rejections: {summary or 'no usable images'}.")
            _send_auto_feed_status(unique_id, f"Failed: {error}")
            raise error
        _send_auto_feed_status(unique_id, f"Batch exhausted ({summary or 'no usable frames'}). Loading another batch...")
        raise _ContinueSourceSearch(summary or "no usable frames")


_install_history_completion_hook()
