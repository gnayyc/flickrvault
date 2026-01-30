#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "flickr-api>=0.8.0",
# ]
# ///
"""
flickrvault v2.0
- Full metadata backup (EXIF, tags, geo, albums, people, stats)
- Incremental sync via lastupdate timestamp
- SQLite index for fast 50K+ photo lookup
- Visual browse (HTML gallery / web server)
"""

import argparse
import json
import os
import re
import sys
import time
import webbrowser
import tempfile
import http.server
import socketserver
import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import random

import flickr_api
from flickr_api.api import flickr


# ============== RETRY HELPER ==============

# Global rate limit state (shared across threads)
_rate_limit_lock = threading.Lock()
_rate_limit_until = 0  # Unix timestamp when rate limit expires
_rate_limit_consecutive = 0  # Track consecutive rate limits for exponential backoff
_request_delay = 0.2   # Dynamic delay - starts fast, adjusts based on rate limits
_request_delay_min = 0.1  # Minimum delay (10 req/sec theoretical max)
_request_delay_max = 2.0  # Maximum delay when hitting rate limits
_success_count = 0  # Track consecutive successes for speed-up


def check_rate_limit():
    """Check if we're currently rate limited, wait if needed."""
    global _rate_limit_until
    with _rate_limit_lock:
        now = time.time()
        if _rate_limit_until > now:
            wait_time = _rate_limit_until - now
            mins, secs = divmod(int(wait_time), 60)
            if mins > 0:
                print(f"\n⏳ Rate limited, waiting {mins}m {secs}s...", flush=True)
            else:
                print(f"\n⏳ Rate limited, waiting {secs}s...", flush=True)
            time.sleep(wait_time)


def set_rate_limit(base_seconds=120):
    """Set rate limit pause and slow down request rate."""
    global _rate_limit_until, _rate_limit_consecutive, _request_delay, _success_count
    with _rate_limit_lock:
        _rate_limit_consecutive += 1
        _success_count = 0  # Reset success counter
        # Slow down: double the delay (up to max)
        old_delay = _request_delay
        _request_delay = min(_request_delay * 2, _request_delay_max)
        # Exponential backoff for pause: 120s, 240s, 480s, 600s (max 10 min)
        wait_seconds = min(base_seconds * (2 ** (_rate_limit_consecutive - 1)), 600)
        new_until = time.time() + wait_seconds
        # Only extend if this is longer than current wait
        if new_until > _rate_limit_until:
            _rate_limit_until = new_until
            mins, secs = divmod(int(wait_seconds), 60)
            delay_info = f" (delay: {old_delay:.1f}s → {_request_delay:.1f}s)"
            if mins > 0:
                print(f"\n⚠️  Rate limit #{_rate_limit_consecutive}! Pausing {mins}m {secs}s{delay_info}", flush=True)
            else:
                print(f"\n⚠️  Rate limit #{_rate_limit_consecutive}! Pausing {secs}s{delay_info}", flush=True)


def reset_rate_limit_backoff():
    """Reset rate limit counter and potentially speed up after consecutive successes."""
    global _rate_limit_consecutive, _request_delay, _success_count
    with _rate_limit_lock:
        _rate_limit_consecutive = 0
        _success_count += 1
        # Speed up after 100 consecutive successes (reduce delay by 10%)
        if _success_count >= 100 and _request_delay > _request_delay_min:
            old_delay = _request_delay
            _request_delay = max(_request_delay * 0.9, _request_delay_min)
            _success_count = 0  # Reset counter
            if os.environ.get('FLICKR_DEBUG'):
                print(f"\n✨ 100 successes! Speed up: {old_delay:.2f}s → {_request_delay:.2f}s", flush=True)


def is_rate_limit_error(error):
    """Check if error is a 429 rate limit."""
    error_str = str(error)
    return '429' in error_str or 'Too Many Requests' in error_str


def retry_on_error(func, max_retries=3, base_delay=2, exceptions=(Exception,)):
    """Retry a function with exponential backoff.

    Args:
        func: Callable to execute
        max_retries: Maximum number of retry attempts
        base_delay: Base delay in seconds (doubles each retry)
        exceptions: Tuple of exceptions to catch and retry

    Returns:
        Result of func() if successful

    Raises:
        Last exception if all retries failed
    """
    last_error = None
    rate_limit_retries = 0
    max_rate_limit_retries = 10  # More retries since we use exponential backoff

    for attempt in range(max_retries + 1 + max_rate_limit_retries):
        try:
            # Check if we're rate limited before making request
            check_rate_limit()
            # Random delay between requests to avoid rate limiting (jitter helps)
            jitter = random.uniform(0.5, 1.5)  # 50% to 150% of base delay
            time.sleep(_request_delay * jitter)
            result = func()
            # Success - reset consecutive rate limit counter
            reset_rate_limit_backoff()
            return result
        except exceptions as e:
            last_error = e
            # Debug: show what error triggered rate limit detection
            if os.environ.get('FLICKR_DEBUG'):
                print(f"[DEBUG] Exception: {type(e).__name__}: {e}")
            # Check for rate limit error
            if is_rate_limit_error(e):
                rate_limit_retries += 1
                if rate_limit_retries <= max_rate_limit_retries:
                    set_rate_limit()  # Auto-calculate wait with exponential backoff
                    check_rate_limit()  # Actually wait
                    continue  # Retry after rate limit wait
                else:
                    raise last_error  # Too many rate limits
            elif attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                error_type = type(e).__name__
                log_debug(f"    ⚠️  {error_type}, retrying in {delay}s... ({attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                raise last_error

# ============== CONFIG ==============

def get_config_dir():
    """Get config directory from env var or default."""
    env_path = os.environ.get('FLICKRVAULT_CONFIG')
    if env_path:
        return Path(env_path)
    return Path.home() / '.config' / 'flickrvault'

CONFIG_DIR = get_config_dir()
CONFIG_FILE = CONFIG_DIR / 'config.json'
TOKEN_FILE = CONFIG_DIR / 'token'


def set_config_dir(path: Path):
    """Set config directory (called from CLI)."""
    global CONFIG_DIR, CONFIG_FILE, TOKEN_FILE
    CONFIG_DIR = Path(path)
    CONFIG_FILE = CONFIG_DIR / 'config.json'
    TOKEN_FILE = CONFIG_DIR / 'token'

EXTRAS_LIST = [
    'date_taken', 'date_upload', 'last_update',
    'geo', 'tags', 'machine_tags', 'views', 'media',
    'original_format', 'description', 'license',
    'url_sq', 'url_q', 'url_t', 'url_s', 'url_m', 'url_z', 'url_l', 'url_o',
    'path_alias', 'owner_name'
]
EXTRAS = ','.join(EXTRAS_LIST)

# ============== OUTPUT / LOGGING ==============

class OutputLevel:
    QUIET = 0
    NORMAL = 1
    VERBOSE = 2

OUTPUT_LEVEL = OutputLevel.NORMAL
PROGRESS_MODE = False


def set_output_level(level: int, progress: bool = False):
    """Set global output level."""
    global OUTPUT_LEVEL, PROGRESS_MODE
    OUTPUT_LEVEL = level
    PROGRESS_MODE = progress


def log_info(msg: str):
    """Print info level message."""
    if OUTPUT_LEVEL >= OutputLevel.NORMAL and not PROGRESS_MODE:
        print(msg)


def log_debug(msg: str):
    """Print debug/verbose level message."""
    if OUTPUT_LEVEL >= OutputLevel.VERBOSE and not PROGRESS_MODE:
        print(msg)


def log_error(msg: str):
    """Print error message (always shown)."""
    print(msg, file=sys.stderr)


class ProgressBar:
    """Simple progress bar for terminal with speed and ETA."""

    def __init__(self, total: int, desc: str = '', width: int = 30):
        self.total = total
        self.current = 0
        self.desc = desc
        self.width = width
        self.status = ''
        self._last_line_len = 0
        self._start_time = time.time()
        self._last_update_time = self._start_time
        self._last_count = 0
        self._bytes_total = 0  # Track total bytes downloaded

    def add_bytes(self, bytes_count: int):
        """Add downloaded bytes for speed calculation."""
        self._bytes_total += bytes_count

    def update(self, n: int = 1, status: str = None):
        """Update progress by n steps."""
        self.current = min(self.current + n, self.total)
        if status:
            self.status = status
        self._render()

    def set(self, current: int, status: str = None):
        """Set progress to specific value."""
        self.current = min(current, self.total)
        if status:
            self.status = status
        self._render()

    def _format_time(self, seconds: float) -> str:
        """Format seconds as HH:MM:SS or MM:SS."""
        if seconds < 0 or seconds > 86400 * 7:  # Cap at 7 days
            return "--:--"
        hours, remainder = divmod(int(seconds), 3600)
        minutes, secs = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    def _format_bytes(self, bytes_count: int) -> str:
        """Format bytes as human readable."""
        if bytes_count < 1024:
            return f"{bytes_count}B"
        elif bytes_count < 1024 * 1024:
            return f"{bytes_count / 1024:.1f}KB"
        elif bytes_count < 1024 * 1024 * 1024:
            return f"{bytes_count / 1024 / 1024:.1f}MB"
        else:
            return f"{bytes_count / 1024 / 1024 / 1024:.2f}GB"

    def _render(self):
        """Render progress bar."""
        if not PROGRESS_MODE:
            return

        now = time.time()
        elapsed = now - self._start_time
        pct = self.current / self.total if self.total > 0 else 0
        filled = int(self.width * pct)
        bar = '█' * filled + '░' * (self.width - filled)

        # Calculate speed (items per minute)
        speed_str = ""
        eta_str = ""
        net_speed_str = ""
        if elapsed > 2 and self.current > 0:
            speed = self.current / elapsed * 60  # per minute
            speed_str = f"{speed:.1f}/min"

            # ETA
            remaining = self.total - self.current
            if speed > 0:
                eta_seconds = remaining / (speed / 60)
                eta_str = f"ETA {self._format_time(eta_seconds)}"

            # Network speed (MB/s)
            if self._bytes_total > 0:
                net_speed = self._bytes_total / elapsed  # bytes per second
                if net_speed >= 1024 * 1024:
                    net_speed_str = f"{net_speed / 1024 / 1024:.1f}MB/s"
                else:
                    net_speed_str = f"{net_speed / 1024:.0f}KB/s"

        # Truncate status if too long
        max_status_len = 20
        status = self.status[:max_status_len] if len(self.status) > max_status_len else self.status

        # Build line: Phase 3: [████░░░░] 100/1000 | 5.2/min | 2.3MB/s | ETA 12:34 | status
        parts = [f"\r{self.desc}: [{bar}] {self.current}/{self.total}"]
        if speed_str:
            parts.append(speed_str)
        if net_speed_str:
            parts.append(net_speed_str)
        if eta_str:
            parts.append(eta_str)
        if status:
            parts.append(status)
        line = " | ".join(parts)

        # Clear previous line if it was longer
        if len(line) < self._last_line_len:
            line += ' ' * (self._last_line_len - len(line))
        self._last_line_len = len(line)

        print(line, end='', flush=True)

    def finish(self, msg: str = None):
        """Finish progress bar."""
        if PROGRESS_MODE:
            elapsed = time.time() - self._start_time
            elapsed_str = self._format_time(elapsed)
            if msg:
                self.status = f"{msg} ({elapsed_str})"
            else:
                self.status = f"Done ({elapsed_str})"
            self._render()
            print()  # New line


class StatusLine:
    """Fixed status line at bottom of terminal."""

    def __init__(self):
        self.phase = ''
        self.task = ''
        self.progress = ''
        self._last_len = 0

    def update(self, phase: str = None, task: str = None, progress: str = None):
        """Update status line."""
        if phase is not None:
            self.phase = phase
        if task is not None:
            self.task = task
        if progress is not None:
            self.progress = progress
        self._render()

    def _render(self):
        """Render status line."""
        if not PROGRESS_MODE:
            return

        line = f"\r[{self.phase}] {self.task} {self.progress}"

        if len(line) < self._last_len:
            line += ' ' * (self._last_len - len(line))
        self._last_len = len(line)

        print(line, end='', flush=True)

    def clear(self):
        """Clear status line."""
        if PROGRESS_MODE:
            print('\r' + ' ' * self._last_len + '\r', end='', flush=True)


# Global status line
STATUS = StatusLine()


# ============== HELPERS ==============

class RateLimitError(Exception):
    """Raised when Flickr API returns 429 Too Many Requests."""
    pass


def flickr_call(method, retries=3, **kwargs):
    """Call Flickr API and return parsed JSON with retry."""
    kwargs['format'] = 'json'
    kwargs['nojsoncallback'] = 1

    def _call():
        result = method(**kwargs)
        if isinstance(result, bytes):
            text = result.decode('utf-8')
            # Debug: show first request response
            if os.environ.get('FLICKR_DEBUG'):
                print(f"[DEBUG] Response type: bytes, length: {len(text)}")
                print(f"[DEBUG] Response preview: {text[:300]}")
            # Check for empty response
            if not text:
                raise Exception("Empty response from Flickr API")
            # Check for HTML error page (not JSON)
            if text.startswith('<!DOCTYPE') or text.startswith('<html'):
                # Only treat as rate limit if it explicitly says 429 in HTML context
                if 'HTTP 429' in text or '>429<' in text or 'Too Many Requests' in text:
                    raise RateLimitError("429 Too Many Requests (HTML response)")
                preview = text[:300].replace('\n', ' ')
                raise Exception(f"HTML error response (check API key/auth): {preview}")
            # Try to parse JSON first
            try:
                data = json.loads(text)
                # Check for API error response
                if data.get('stat') == 'fail':
                    code = data.get('code', 0)
                    msg = data.get('message', 'Unknown error')
                    if code == 429 or 'Too Many' in str(msg):
                        raise RateLimitError(f"429 Too Many Requests: {msg}")
                    raise Exception(f"Flickr API error {code}: {msg}")
                return data
            except json.JSONDecodeError:
                # Non-JSON, non-HTML response - might be rate limit page
                if 'Too Many Requests' in text:
                    raise RateLimitError("429 Too Many Requests")
                raise Exception(f"Invalid response: {text[:200]}")
        return result

    return retry_on_error(_call, max_retries=retries)


# ============== AUTH ==============

def load_config(interactive: bool = False):
    """Load API key and secret from config file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    config = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            config = json.load(f)
    
    # Check if API credentials are missing
    if not config.get('api_key') or not config.get('api_secret'):
        if not interactive:
            print(f"❌ API credentials not configured.")
            print(f"   Run: flickr auth")
            sys.exit(1)
        
        # Interactive setup
        print("="*50)
        print("🔐 Flickr API Setup")
        print("="*50)
        print("\nYou need a Flickr API key to use this tool.")
        print("Let me open the Flickr App registration page...\n")
        
        api_url = "https://www.flickr.com/services/apps/create/apply/"
        print(f"📎 {api_url}")
        webbrowser.open(api_url)
        
        print("\n" + "-"*50)
        print("Fill out the form on Flickr:")
        print("  • App name: anything (e.g., 'My Backup Tool')")
        print("  • App description: personal backup")
        print("  • Select 'Non-commercial'")
        print("-"*50)
        
        print("\nAfter submitting, you'll see your Key and Secret.")
        api_key = input("\n🔑 Paste your API Key: ").strip()
        api_secret = input("🔒 Paste your API Secret: ").strip()
        
        if not api_key or not api_secret:
            print("❌ API key and secret are required.")
            sys.exit(1)
        
        config['api_key'] = api_key
        config['api_secret'] = api_secret
        
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        
        print(f"\n✓ Saved to {CONFIG_FILE}")
    
    return config


def authenticate(interactive: bool = False):
    """Authenticate with Flickr and cache the token."""
    config = load_config(interactive=interactive)
    flickr_api.set_keys(api_key=config['api_key'], api_secret=config['api_secret'])
    
    if TOKEN_FILE.exists():
        # Verify token is still valid
        try:
            flickr_api.set_auth_handler(str(TOKEN_FILE))
            flickr_api.test.login()  # Test if token works
        except:
            print("⚠️  Saved token expired, re-authenticating...")
            TOKEN_FILE.unlink()
            authenticate(interactive=True)
            return
    else:
        if not interactive:
            print("❌ Not authenticated. Run: flickr auth")
            sys.exit(1)
        
        print("\n" + "="*50)
        print("🔗 OAuth Authorization")
        print("="*50)
        
        auth = flickr_api.auth.AuthHandler()
        url = auth.get_authorization_url('delete')  # 'delete' includes read/write
        
        print("\nOpening Flickr authorization page...")
        print(f"📎 {url}")
        webbrowser.open(url)
        
        print("\n" + "-"*50)
        print("Click 'OK, I'LL AUTHORIZE IT' on Flickr,")
        print("then copy the 9-digit verification code.")
        print("-"*50)
        
        verifier = input("\n🔢 Paste verification code: ").strip()
        
        if not verifier:
            print("❌ Verification code required.")
            sys.exit(1)
        
        auth.set_verifier(verifier)
        auth.save(str(TOKEN_FILE))
        flickr_api.set_auth_handler(auth)
        
        print("\n✅ Authentication successful!")


def get_user():
    """Get authenticated user."""
    return flickr_api.test.login()


# ============== DATABASE ==============

def init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize SQLite database."""
    # check_same_thread=False allows cross-thread access (we use db_lock for safety)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS photos (
            id TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            taken_date TEXT,
            upload_date TEXT,
            last_update TEXT,
            license INTEGER,
            views INTEGER DEFAULT 0,
            latitude REAL,
            longitude REAL,
            original_format TEXT,
            media TEXT,
            local_path TEXT,
            meta_path TEXT,
            downloaded_at TEXT,
            file_hash TEXT
        );
        
        CREATE TABLE IF NOT EXISTS albums (
            id TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            photo_count INTEGER,
            video_count INTEGER,
            primary_photo_id TEXT,
            date_create TEXT,
            date_update TEXT
        );
        
        CREATE TABLE IF NOT EXISTS album_photos (
            album_id TEXT,
            photo_id TEXT,
            position INTEGER,
            PRIMARY KEY (album_id, photo_id)
        );
        
        CREATE TABLE IF NOT EXISTS tags (
            photo_id TEXT,
            tag_id TEXT,
            tag_raw TEXT,
            tag_clean TEXT,
            author TEXT,
            PRIMARY KEY (photo_id, tag_id)
        );
        
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sync_type TEXT,
            started_at TEXT,
            completed_at TEXT,
            photos_scanned INTEGER,
            photos_downloaded INTEGER,
            photos_updated INTEGER,
            errors INTEGER
        );
        
        CREATE INDEX IF NOT EXISTS idx_photos_taken ON photos(taken_date);
        CREATE INDEX IF NOT EXISTS idx_photos_update ON photos(last_update);
        CREATE INDEX IF NOT EXISTS idx_album_photos_photo ON album_photos(photo_id);
    ''')
    
    conn.commit()
    return conn


# ============== SYNC ==============

def fetch_recently_updated_photos(min_date: int, progress_callback=None, limit: int = None) -> list:
    """Fetch photos updated since min_date (Unix timestamp).

    Uses flickr.photos.recentlyUpdated API for efficient incremental sync.
    """
    all_photos = []
    page = 1
    per_page = 500 if not limit else min(500, limit)
    total_pages = None

    while True:
        if progress_callback:
            progress_callback(f"Fetching updates page {page}{'/' + str(total_pages) if total_pages else ''}...")
        elif PROGRESS_MODE:
            STATUS.update(phase='[1/4] API', task='Fetching recently updated...',
                         progress=f"page {page}{'/' + str(total_pages) if total_pages else ''}")

        result = flickr_call(
            flickr.photos.recentlyUpdated,
            min_date=min_date,
            extras=EXTRAS,
            per_page=per_page,
            page=page
        )

        photos = result['photos']['photo']
        total_pages = int(result['photos']['pages'])
        total = int(result['photos']['total'])

        if not photos:
            break

        all_photos.extend(photos)

        if progress_callback:
            progress_callback(f"   Found {len(all_photos)}/{total} updated photos")

        # Check limit
        if limit and len(all_photos) >= limit:
            all_photos = all_photos[:limit]
            break

        if page >= total_pages:
            break
        page += 1

    return all_photos


def get_last_sync_timestamp(conn) -> int:
    """Get the most recent lastupdate timestamp from cached photos.

    Returns Unix timestamp or 0 if no cached data.
    """
    cursor = conn.cursor()
    row = cursor.execute('SELECT MAX(last_update) FROM photos WHERE last_update IS NOT NULL').fetchone()
    if row and row[0]:
        try:
            return int(row[0])
        except (ValueError, TypeError):
            pass
    return 0


def get_cached_photo_count(conn) -> int:
    """Get number of photos in cache."""
    cursor = conn.cursor()
    row = cursor.execute('SELECT COUNT(*) FROM photos').fetchone()
    return row[0] if row else 0


def get_flickr_photo_count(user_nsid: str) -> int:
    """Get total photo count from Flickr (single API call)."""
    result = flickr_call(
        flickr.people.getPhotos,
        user_id=user_nsid,
        per_page=1,  # Only need count, not actual photos
        page=1
    )
    return int(result['photos']['total'])


def fetch_all_photos_metadata(user_nsid: str, progress_callback=None, limit: int = None,
                               order: str = 'newest', from_date: str = None,
                               to_date: str = None) -> list:
    """Fetch basic metadata for all photos using batch API.

    Args:
        order: 'newest' (default) or 'oldest'
        from_date: Start date (YYYY-MM-DD or YYYY)
        to_date: End date (YYYY-MM-DD or YYYY)
    """
    all_photos = []
    page = 1
    per_page = 500 if not limit else min(500, limit)
    total_pages = None

    # Build API parameters
    use_search = from_date or to_date  # Use search API for date filtering

    # Sort order: date-posted-desc (newest) or date-posted-asc (oldest)
    # For search: date-taken-asc or date-taken-desc
    sort_order = 'date-taken-asc' if order == 'oldest' else 'date-taken-desc'

    # Parse date range
    min_date = None
    max_date = None
    if from_date:
        min_date = f"{from_date}-01-01" if len(from_date) == 4 else from_date
    if to_date:
        max_date = f"{to_date}-12-31" if len(to_date) == 4 else to_date

    while True:
        if progress_callback:
            progress_callback(f"Fetching page {page}{'/' + str(total_pages) if total_pages else ''}...")
        elif PROGRESS_MODE:
            STATUS.update(phase='[1/4] API', task='Fetching all photos...',
                         progress=f"page {page}{'/' + str(total_pages) if total_pages else ''}")

        if use_search:
            # Use search API for date filtering
            params = {
                'user_id': user_nsid,
                'extras': EXTRAS,
                'per_page': per_page,
                'page': page,
                'sort': sort_order,
            }
            if min_date:
                params['min_taken_date'] = min_date
            if max_date:
                params['max_taken_date'] = max_date

            result = flickr_call(flickr.photos.search, **params)
        else:
            # Use getPhotos for simple listing (no date filter)
            result = flickr_call(
                flickr.people.getPhotos,
                user_id=user_nsid,
                extras=EXTRAS,
                per_page=per_page,
                page=page
            )

        photos = result['photos']['photo']
        total_pages = int(result['photos']['pages'])
        total = int(result['photos']['total'])

        if not photos:
            break

        # Reverse if using getPhotos (which doesn't support sort) and want oldest first
        if not use_search and order == 'oldest':
            # For oldest-first without date filter, we need to start from the last page
            # This is handled by reversing the final list
            pass

        all_photos.extend(photos)

        if progress_callback:
            date_info = ""
            if from_date or to_date:
                date_info = f" [{from_date or '...'} ~ {to_date or '...'}]"
            if limit:
                progress_callback(f"  → {len(all_photos)}/{limit} photos (of {total} total){date_info}")
            else:
                progress_callback(f"  → {len(all_photos)}/{total} photos{date_info}")

        # Stop if we have enough photos
        if limit and len(all_photos) >= limit:
            all_photos = all_photos[:limit]
            break

        if page >= total_pages:
            break
        page += 1

    # For oldest-first without date filter, reverse the list
    if not use_search and order == 'oldest':
        all_photos = all_photos[::-1]
        if limit:
            all_photos = all_photos[:limit]

    return all_photos


def fetch_all_albums(user, progress_callback=None) -> list:
    """Fetch all albums with metadata."""
    photosets = list(retry_on_error(lambda: user.getPhotosets()))
    total = len(photosets)
    albums = []
    for i, ps in enumerate(photosets, 1):
        if progress_callback:
            progress_callback(f"  [{i}/{total}] Fetching album info...")
        elif PROGRESS_MODE:
            STATUS.update(phase='[4/4] Albums', task='Fetching album metadata...', progress=f"{i}/{total}")
        try:
            info = retry_on_error(lambda ps=ps: ps.getInfo())
            albums.append({
                'id': ps.id,
                'title': info.get('title', ''),
                'description': info.get('description', ''),
                'photo_count': int(info.get('photos', 0)),
                'video_count': int(info.get('videos', 0)),
                'primary_photo_id': info.get('primary', ''),
                'date_create': info.get('date_create', ''),
                'date_update': info.get('date_update', ''),
            })
        except Exception as e:
            log_error(f"  ⚠️  Failed to fetch album {ps.id}: {e}")
    return albums


def fetch_album_photos(album_id: str, progress_callback=None) -> list:
    """Fetch photo IDs in an album with retry."""
    ps = flickr_api.Photoset(id=album_id)
    photo_ids = []
    photos = retry_on_error(lambda: list(ps.getPhotos()))
    for i, photo in enumerate(photos):
        photo_ids.append({'photo_id': photo.id, 'position': i})
        if progress_callback and (i + 1) % 50 == 0:
            progress_callback(f"     {i + 1} photos...")
    return photo_ids


def fetch_photo_details(photo_id: str, verbose=False) -> dict:
    """Fetch detailed info for a single photo (getInfo + getExif)."""
    details = {'id': photo_id}

    try:
        if verbose:
            print(f" info", end='', flush=True)
        photo = flickr_api.Photo(id=photo_id)
        info = retry_on_error(lambda: photo.getInfo())
        
        # Basic info
        details['title'] = info.get('title', '')
        details['description'] = info.get('description', '')
        details['license'] = info.get('license', 0)
        details['views'] = int(info.get('views', 0))
        details['media'] = info.get('media', 'photo')
        details['original_format'] = info.get('originalformat', 'jpg')
        details['rotation'] = info.get('rotation', 0)
        
        # Dates
        dates = info.get('dates', {})
        details['taken'] = dates.get('taken', '')
        details['uploaded'] = dates.get('posted', '')
        details['updated'] = dates.get('lastupdate', '')
        
        # Owner
        owner = info.get('owner', {})
        details['owner'] = {
            'nsid': owner.get('nsid', ''),
            'username': owner.get('username', ''),
            'realname': owner.get('realname', ''),
        }
        
        # Visibility
        vis = info.get('visibility', {})
        details['visibility'] = {
            'is_public': bool(int(vis.get('ispublic', 0))),
            'is_friend': bool(int(vis.get('isfriend', 0))),
            'is_family': bool(int(vis.get('isfamily', 0))),
        }
        
        # Tags
        details['tags'] = []
        for tag in info.get('tags', {}).get('tag', []):
            details['tags'].append({
                'id': tag.get('id', ''),
                'raw': tag.get('raw', ''),
                'clean': tag.get('_content', ''),
                'author': tag.get('author', ''),
            })
        
        # URLs
        details['urls'] = {
            'photopage': info.get('urls', {}).get('url', [{}])[0].get('_content', ''),
        }
        
        # Notes
        details['notes'] = []
        for note in info.get('notes', {}).get('note', []):
            details['notes'].append({
                'id': note.get('id', ''),
                'author': note.get('author', ''),
                'text': note.get('_content', ''),
                'x': note.get('x'), 'y': note.get('y'),
                'w': note.get('w'), 'h': note.get('h'),
            })
        
    except Exception as e:
        details['_error_info'] = str(e)

    # EXIF
    try:
        if verbose:
            print(f" exif", end='', flush=True)
        photo = flickr_api.Photo(id=photo_id)
        exif_data = retry_on_error(lambda: photo.getExif())
        exif = {}
        
        exif_map = {
            'Make': 'make', 'Model': 'model', 'Lens': 'lens',
            'Exposure': 'exposure', 'Aperture': 'aperture',
            'ISO Speed': 'iso', 'Focal Length': 'focal_length',
            'Flash': 'flash', 'White Balance': 'white_balance',
            'Exposure Program': 'exposure_program',
        }
        
        for item in exif_data:
            label = item.get('label', '')
            if label in exif_map:
                exif[exif_map[label]] = item.get('clean', item.get('raw', ''))
        
        details['exif'] = exif
    except Exception as e:
        details['exif'] = {}
        details['_error_exif'] = str(e)
    
    # Location
    try:
        if verbose:
            print(f" geo", end='', flush=True)
        photo = flickr_api.Photo(id=photo_id)
        loc = retry_on_error(lambda: photo.getLocation())
        details['geo'] = {
            'latitude': float(loc.get('latitude', 0)),
            'longitude': float(loc.get('longitude', 0)),
            'accuracy': int(loc.get('accuracy', 0)),
            'locality': loc.get('locality', {}).get('_content', ''),
            'county': loc.get('county', {}).get('_content', ''),
            'region': loc.get('region', {}).get('_content', ''),
            'country': loc.get('country', {}).get('_content', ''),
        }
    except:
        details['geo'] = None
    
    # Contexts (albums)
    try:
        if verbose:
            print(f" ctx", end='', flush=True)
        photo = flickr_api.Photo(id=photo_id)
        contexts = flickr_call(flickr.photos.getAllContexts, photo_id=photo_id)
        details['albums'] = []
        for ps in contexts.get('set', []):
            details['albums'].append({
                'id': ps.get('id', ''),
                'title': ps.get('title', ''),
            })
    except:
        details['albums'] = []
    
    # Favorites count
    try:
        faves = flickr_call(flickr.photos.getFavorites, photo_id=photo_id, per_page=1)
        details['faves'] = int(faves.get('photo', {}).get('total', 0))
    except:
        details['faves'] = 0
    
    # Comments count
    try:
        comments = flickr_call(flickr.photos.comments.getList, photo_id=photo_id)
        details['comments_count'] = len(comments.get('comments', {}).get('comment', []))
    except:
        details['comments_count'] = 0
    
    # People tagged
    try:
        people = flickr_call(flickr.photos.people.getList, photo_id=photo_id)
        details['people'] = []
        for person in people.get('people', {}).get('person', []):
            details['people'].append({
                'nsid': person.get('nsid', ''),
                'username': person.get('username', ''),
            })
    except:
        details['people'] = []
    
    return details


def download_photo_file(photo_id: str, output_path: Path, size='Original', verbose=False) -> tuple:
    """Download original photo file with retry.

    Returns:
        (success: bool, bytes_downloaded: int)
    """
    try:
        if verbose:
            print(f"    Getting sizes...", end='', flush=True)
        photo = flickr_api.Photo(id=photo_id)
        sizes = retry_on_error(lambda: photo.getSizes())

        # Try to get original, fall back to largest
        url = None
        selected_size = None
        for s in ['Original', 'Large 2048', 'Large 1600', 'Large', 'Medium 800']:
            if s in sizes:
                url = sizes[s]['source']
                selected_size = s
                break

        if not url and sizes:
            url = list(sizes.values())[-1]['source']
            selected_size = list(sizes.keys())[-1]

        if url:
            if verbose:
                print(f" downloading ({selected_size})...", end='', flush=True)
            retry_on_error(lambda: photo.save(str(output_path), size_label=size))
            # Get file size after download
            file_size = output_path.stat().st_size if output_path.exists() else 0
            return True, file_size
        return False, 0
    except Exception as e:
        print(f"    ✗ Download error: {e}")
        return False, 0


def get_photo_year_month(photo_data: dict) -> tuple:
    """Extract year and month from photo data."""
    taken = photo_data.get('datetaken', '') or photo_data.get('taken', '')
    if taken:
        try:
            dt = datetime.strptime(taken[:10], '%Y-%m-%d')
            return dt.year, dt.month
        except:
            pass
    
    # Fallback to upload date (unix timestamp)
    uploaded = photo_data.get('dateupload', '') or photo_data.get('uploaded', '')
    if uploaded:
        try:
            dt = datetime.fromtimestamp(int(uploaded))
            return dt.year, dt.month
        except:
            pass
    
    return datetime.now().year, datetime.now().month


def sync_backup(output_dir: Path, full: bool = False, download_photos: bool = True,
                max_workers: int = 1, limit: int = None, skip_albums: bool = False,
                album_limit: int = None, order: str = 'newest',
                from_date: str = None, to_date: str = None,
                request_delay: float = 0, wait_minutes: int = 0):
    """Main sync function."""
    global _request_delay
    # If delay specified, use fixed delay; otherwise use dynamic (default 0.2s)
    if request_delay > 0:
        _request_delay = request_delay

    # Wait before starting if requested (for rate limit cooldown)
    if wait_minutes > 0:
        print(f"⏳ Waiting {wait_minutes} minutes before starting (rate limit cooldown)...")
        for remaining in range(wait_minutes * 60, 0, -30):
            mins, secs = divmod(remaining, 60)
            print(f"   {mins}:{secs:02d} remaining...", flush=True)
            time.sleep(min(30, remaining))
        print("   Starting now!")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Init DB
    db_path = output_dir / 'backup.db'
    conn = init_db(db_path)
    
    user = get_user()
    user_nsid = user.id
    username = user.username

    log_info(f"👤 User: {username} ({user_nsid})")
    
    # Save account info
    account_info = {
        'nsid': user_nsid,
        'username': username,
        'backup_started': datetime.now().isoformat(),
        'backup_version': '2.0',
    }
    (output_dir / 'account.json').write_text(json.dumps(account_info, indent=2))
    
    stats = {'scanned': 0, 'downloaded': 0, 'updated': 0, 'skipped': 0, 'errors': 0}
    cursor = conn.cursor()

    # Check for incremental sync
    cached_count = get_cached_photo_count(conn)
    last_sync_ts = get_last_sync_timestamp(conn)

    # Auto-detect incomplete sync by comparing local count vs Flickr total
    flickr_total = 0
    auto_full = False
    if not full and cached_count > 0 and last_sync_ts > 0 and not from_date and not to_date:
        log_info("🔍 Checking Flickr photo count...")
        flickr_total = get_flickr_photo_count(user_nsid)
        if cached_count < flickr_total:
            auto_full = True
            log_info(f"   ⚠️  Incomplete sync detected: {cached_count} local < {flickr_total} on Flickr")
            log_info(f"   → Switching to full mode to catch missing photos")

    use_incremental = not full and not auto_full and cached_count > 0 and last_sync_ts > 0 and not from_date and not to_date

    # Start sync log (after auto-detection)
    sync_started = datetime.now().isoformat()
    sync_type = 'incremental' if use_incremental else ('auto-full' if auto_full else 'full')
    cursor.execute('''
        INSERT INTO sync_log (sync_type, started_at, photos_scanned, photos_downloaded, photos_updated, errors)
        VALUES (?, ?, 0, 0, 0, 0)
    ''', (sync_type, sync_started))
    sync_id = cursor.lastrowid
    conn.commit()

    # Print sync mode banner
    if PROGRESS_MODE:
        if use_incremental:
            last_sync_time = datetime.fromtimestamp(last_sync_ts).strftime('%Y-%m-%d %H:%M')
            print(f"🔄 Incremental sync (cache: {cached_count} photos, last: {last_sync_time})")
        elif auto_full:
            print(f"📥 Full sync (auto: {cached_count}/{flickr_total} photos cached)")
        else:
            mode = "full" if full else "initial"
            print(f"📥 Full sync ({mode})")
        print()

    # Phase 1: Fetch photos metadata
    if use_incremental:
        # Incremental: only fetch recently updated photos
        last_sync_time = datetime.fromtimestamp(last_sync_ts).strftime('%Y-%m-%d %H:%M:%S')
        log_info(f"\n📋 Phase 1: Fetching updates since {last_sync_time}...")
        log_info(f"   (Cached: {cached_count} photos, use --full to re-fetch all)")
        if PROGRESS_MODE:
            STATUS.update(phase='[1/4] API', task='Fetching recently updated photos...', progress='')
        all_photos = fetch_recently_updated_photos(
            min_date=last_sync_ts,
            progress_callback=log_info if not PROGRESS_MODE else None,
            limit=limit
        )
        log_info(f"   Found: {len(all_photos)} updated photos")
    else:
        # Full fetch
        order_desc = "oldest first" if order == 'oldest' else "newest first"
        date_range = ""
        if from_date or to_date:
            date_range = f" [{from_date or '...'} ~ {to_date or '...'}]"
        log_info(f"\n📋 Phase 1: Fetching photo list ({order_desc}){date_range}...")
        if PROGRESS_MODE:
            STATUS.update(phase='[1/4] API', task='Fetching all photos from Flickr...', progress='')
        all_photos = fetch_all_photos_metadata(
            user_nsid,
            progress_callback=log_info if not PROGRESS_MODE else None,
            limit=limit,
            order=order,
            from_date=from_date,
            to_date=to_date
        )
        log_info(f"   Total: {len(all_photos)} photos")

    # Phase 2: Determine what needs update
    log_info("\n🔍 Phase 2: Checking for updates...")
    if PROGRESS_MODE:
        STATUS.update(phase='[2/4] Compare', task='Comparing with local cache...', progress='')
    photos_to_update = []
    total_photos = len(all_photos)

    for i, p in enumerate(all_photos):
        if PROGRESS_MODE and (i % 100 == 0 or i == total_photos - 1):
            STATUS.update(phase='[2/4] Compare', task='Comparing with local cache...',
                         progress=f"{i + 1}/{total_photos}")
        photo_id = p['id']
        flickr_updated = p.get('lastupdate', '0')
        
        # Check DB
        row = cursor.execute('SELECT last_update, local_path FROM photos WHERE id = ?', (photo_id,)).fetchone()
        
        if row is None:
            # New photo
            photos_to_update.append((p, 'new'))
        elif full:
            # Full sync - update all
            photos_to_update.append((p, 'full'))
        elif str(flickr_updated) > str(row['last_update'] or '0'):
            # Updated on Flickr
            photos_to_update.append((p, 'updated'))
        elif download_photos and row['local_path'] and not Path(row['local_path']).exists():
            # File missing locally
            photos_to_update.append((p, 'missing'))
        else:
            stats['skipped'] += 1
    
    log_info(f"   New: {len([x for x in photos_to_update if x[1] == 'new'])}")
    log_info(f"   Updated: {len([x for x in photos_to_update if x[1] == 'updated'])}")
    log_info(f"   Missing: {len([x for x in photos_to_update if x[1] == 'missing'])}")
    log_info(f"   Skipped: {stats['skipped']}")

    # Phase 3: Fetch details & download
    interrupted = False
    if photos_to_update:
        worker_count = max_workers if download_photos else min(max_workers, 4)
        log_info(f"\n📥 Phase 3: Downloading {len(photos_to_update)} photos ({worker_count} workers)...")

        photos_dir = output_dir / 'photos'
        photos_dir.mkdir(exist_ok=True)

        # Progress bar for Phase 3
        if PROGRESS_MODE:
            desc = '[3/4] Download' if download_photos else '[3/4] Metadata'
            progress = ProgressBar(len(photos_to_update), desc=desc)

        # Thread-safe counters and lock
        stats_lock = threading.Lock()
        db_lock = threading.Lock()
        completed_count = [0]  # Use list for mutable in closure

        def process_photo(task):
            """Worker function to process a single photo."""
            p, reason = task
            photo_id = p['id']
            result = {'downloaded': 0, 'updated': 0, 'errors': 0, 'bytes': 0}

            try:
                # Determine path
                year, month = get_photo_year_month(p)
                month_dir = photos_dir / str(year) / f"{year}-{month:02d}"
                month_dir.mkdir(parents=True, exist_ok=True)

                fmt = p.get('originalformat', 'jpg') or 'jpg'
                photo_path = month_dir / f"{photo_id}.{fmt}"
                meta_path = month_dir / f"{photo_id}.json"

                # Fetch detailed metadata
                details = fetch_photo_details(photo_id, verbose=False)

                # Add sizes from batch data
                details['sizes'] = {}
                for size_key in ['url_sq', 'url_q', 'url_t', 'url_s', 'url_m', 'url_z', 'url_l', 'url_o']:
                    if p.get(size_key):
                        details['sizes'][size_key] = p[size_key]

                # Save metadata JSON
                details['_backup'] = {
                    'downloaded_at': datetime.now().isoformat(),
                    'file_path': str(photo_path) if download_photos else None,
                }
                meta_path.write_text(json.dumps(details, indent=2, ensure_ascii=False), encoding='utf-8')

                # Download photo file
                if download_photos:
                    success, file_bytes = download_photo_file(photo_id, photo_path, verbose=False)
                    if success:
                        result['downloaded'] = 1
                        result['bytes'] = file_bytes
                    else:
                        result['errors'] = 1
                else:
                    result['updated'] = 1

                # Update DB (thread-safe)
                with db_lock:
                    cursor.execute('''
                        INSERT OR REPLACE INTO photos
                        (id, title, description, taken_date, upload_date, last_update,
                         license, views, latitude, longitude, original_format, media,
                         local_path, meta_path, downloaded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        photo_id,
                        details.get('title', ''),
                        details.get('description', ''),
                        details.get('taken', p.get('datetaken', '')),
                        details.get('uploaded', p.get('dateupload', '')),
                        p.get('lastupdate', ''),
                        details.get('license', 0),
                        details.get('views', 0),
                        details.get('geo', {}).get('latitude') if details.get('geo') else None,
                        details.get('geo', {}).get('longitude') if details.get('geo') else None,
                        fmt,
                        details.get('media', 'photo'),
                        str(photo_path) if download_photos else None,
                        str(meta_path),
                        datetime.now().isoformat(),
                    ))

                    # Save tags
                    for tag in details.get('tags', []):
                        cursor.execute('''
                            INSERT OR REPLACE INTO tags (photo_id, tag_id, tag_raw, tag_clean, author)
                            VALUES (?, ?, ?, ?, ?)
                        ''', (photo_id, tag.get('id', ''), tag.get('raw', ''), tag.get('clean', ''), tag.get('author', '')))

                return photo_id, reason, result, None

            except Exception as e:
                result['errors'] = 1
                return photo_id, reason, result, str(e)

        try:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {executor.submit(process_photo, task): task for task in photos_to_update}

                for future in as_completed(futures):
                    photo_id, reason, result, error = future.result()

                    with stats_lock:
                        stats['scanned'] += 1
                        stats['downloaded'] += result['downloaded']
                        stats['updated'] += result['updated']
                        stats['errors'] += result['errors']
                        completed_count[0] += 1

                        if PROGRESS_MODE:
                            progress.add_bytes(result['bytes'])
                            status = f"{photo_id} ({reason})"
                            if error:
                                status = f"Error: {error[:20]}"
                            progress.set(completed_count[0], status=status)
                        else:
                            if error:
                                print(f"[{completed_count[0]}/{len(photos_to_update)}] {photo_id} ✗ {error}")
                            else:
                                print(f"[{completed_count[0]}/{len(photos_to_update)}] {photo_id} ✓")

                        # Commit every 10 photos
                        if completed_count[0] % 10 == 0:
                            conn.commit()

                conn.commit()  # Final commit

        except KeyboardInterrupt:
            interrupted = True
            conn.commit()  # Save progress
            print()
            print(f"\n⚠️  Interrupted! Progress saved.")
            print(f"   Downloaded: {stats['downloaded']}, Errors: {stats['errors']}")
            print(f"   Remaining: ~{len(photos_to_update) - completed_count[0]} photos")
            print(f"   Run 'sync' again to continue.")

        if PROGRESS_MODE and not interrupted:
            total_dl = progress._format_bytes(progress._bytes_total) if progress._bytes_total > 0 else ""
            dl_info = f", {total_dl}" if total_dl else ""
            progress.finish(msg=f"Done: {stats['downloaded']} downloaded{dl_info}, {stats['errors']} errors")

    # Phase 4: Sync albums
    if interrupted:
        log_info("\n📁 Phase 4: Skipped (interrupted)")
        albums = []
    elif skip_albums:
        log_info("\n📁 Phase 4: Skipped (--skip-albums)")
        albums = []
    else:
        log_info("\n📁 Phase 4: Syncing albums...")
        if PROGRESS_MODE:
            STATUS.update(phase='[4/4] Albums', task='Fetching album list from Flickr...', progress='')
        try:
            albums = fetch_all_albums(user, progress_callback=log_info if not PROGRESS_MODE else None)
        except KeyboardInterrupt:
            interrupted = True
            albums = []
            print(f"\n⚠️  Interrupted during album fetch!")
        log_info(f"   Found {len(albums)} albums")
        if album_limit:
            albums = albums[:album_limit]
            log_info(f"   Limited to: {album_limit} albums")

    albums_dir = output_dir / 'albums'
    albums_dir.mkdir(exist_ok=True)

    albums_index = []

    # Progress bar for Phase 4
    if PROGRESS_MODE and albums:
        album_progress = ProgressBar(len(albums), desc='[4/4] Albums')

    try:
        for i, album in enumerate(albums, 1):
            if PROGRESS_MODE:
                album_progress.set(i - 1, status=album['title'][:25])
            else:
                print(f"   [{i}/{len(albums)}] {album['title']} ({album['photo_count']} photos)...", end='', flush=True)

            # Save to DB
            cursor.execute('''
                INSERT OR REPLACE INTO albums
                (id, title, description, photo_count, video_count, primary_photo_id, date_create, date_update)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                album['id'], album['title'], album['description'],
                album['photo_count'], album['video_count'], album['primary_photo_id'],
                album['date_create'], album['date_update']
            ))

            # Fetch album photos
            try:
                album_photos = fetch_album_photos(album['id'])
                for ap in album_photos:
                    cursor.execute('''
                        INSERT OR REPLACE INTO album_photos (album_id, photo_id, position)
                        VALUES (?, ?, ?)
                    ''', (album['id'], ap['photo_id'], ap['position']))

                # Save album JSON
                album_data = {**album, 'photos': [ap['photo_id'] for ap in album_photos]}
                album_file = albums_dir / f"{album['id']}.json"
                album_file.write_text(json.dumps(album_data, indent=2, ensure_ascii=False), encoding='utf-8')

                albums_index.append({
                    'id': album['id'],
                    'title': album['title'],
                    'photo_count': album['photo_count'],
                })
                if not PROGRESS_MODE:
                    print(f" ✓")
            except Exception as e:
                if not PROGRESS_MODE:
                    print(f" ✗ {e}")

            conn.commit()  # Commit after each album

    except KeyboardInterrupt:
        interrupted = True
        conn.commit()
        print()
        print(f"\n⚠️  Interrupted! Album progress saved.")
        print(f"   Synced: {len(albums_index)}/{len(albums)} albums")

    if PROGRESS_MODE and albums and not interrupted:
        album_progress.finish(msg="Done")

    # Save albums index
    (albums_dir / '_index.json').write_text(json.dumps({'albums': albums_index}, indent=2))
    conn.commit()

    # Update sync log
    cursor.execute('''
        UPDATE sync_log SET
            completed_at = ?,
            photos_scanned = ?,
            photos_downloaded = ?,
            photos_updated = ?,
            errors = ?
        WHERE id = ?
    ''', (datetime.now().isoformat(), stats['scanned'], stats['downloaded'], stats['updated'], stats['errors'], sync_id))
    conn.commit()
    conn.close()

    # Summary (always show)
    if PROGRESS_MODE:
        STATUS.clear()
    print(f"\n{'='*50}")
    if interrupted:
        print(f"⚠️  Backup interrupted (progress saved)")
    else:
        print(f"✅ Backup complete!")
    print(f"   📷 Photos: {stats['downloaded']} downloaded, {stats['updated']} updated, {stats['skipped']} skipped")
    print(f"   📁 Albums: {len(albums_index)}")
    print(f"   ❌ Errors: {stats['errors']}")
    print(f"   📂 Location: {output_dir}")
    if interrupted:
        print(f"\n💡 Run 'sync' again to continue from where you left off.")


# ============== LIST ==============

def list_albums():
    """List all albums for authenticated user."""
    user = get_user()
    print(f"User: {user.username}\n")
    
    photosets = list(user.getPhotosets())
    print(f"{'ID':<20} {'Photos':>6}  Title")
    print("-" * 60)
    
    for ps in photosets:
        info = ps.getInfo()
        print(f"{ps.id:<20} {info.get('photos', '?'):>6}  {info.get('title', 'Untitled')}")
    
    print(f"\nTotal: {len(photosets)} albums")


def show_stats(output_dir: Path):
    """Show backup statistics."""
    db_path = output_dir / 'backup.db'
    if not db_path.exists():
        print(f"No backup found at {output_dir}")
        return
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Photo stats
    total = cursor.execute('SELECT COUNT(*) FROM photos').fetchone()[0]
    with_geo = cursor.execute('SELECT COUNT(*) FROM photos WHERE latitude IS NOT NULL').fetchone()[0]
    downloaded = cursor.execute('SELECT COUNT(*) FROM photos WHERE local_path IS NOT NULL').fetchone()[0]
    
    # Album stats
    albums = cursor.execute('SELECT COUNT(*) FROM albums').fetchone()[0]
    
    # Tag stats
    tags = cursor.execute('SELECT COUNT(DISTINCT tag_clean) FROM tags').fetchone()[0]
    
    # Last sync
    last_sync = cursor.execute('SELECT * FROM sync_log ORDER BY id DESC LIMIT 1').fetchone()
    
    print(f"📊 Backup Statistics")
    print(f"{'='*40}")
    print(f"📷 Photos: {total}")
    print(f"   ├─ Downloaded: {downloaded}")
    print(f"   ├─ With geo: {with_geo}")
    print(f"📁 Albums: {albums}")
    print(f"🏷️  Unique tags: {tags}")
    
    if last_sync:
        print(f"\n🕐 Last sync: {last_sync[3] or last_sync[2]}")
        print(f"   Type: {last_sync[1]}")
        print(f"   Downloaded: {last_sync[5]}, Errors: {last_sync[7]}")
    
    conn.close()


# ============== BROWSE (HTML Gallery) ==============

def get_photo_thumb_url(photo, size='q'):
    """Get thumbnail URL for a photo."""
    try:
        sizes = photo.getSizes()
        size_map = {'s': 'Square', 'q': 'Large Square', 't': 'Thumbnail', 
                    'm': 'Small', 'n': 'Small 320', 'z': 'Medium 640'}
        label = size_map.get(size, 'Large Square')
        if label in sizes:
            return sizes[label]['source']
        for s in ['Large Square', 'Square', 'Thumbnail', 'Small']:
            if s in sizes:
                return sizes[s]['source']
        return list(sizes.values())[0]['source'] if sizes else None
    except:
        return None


def browse_by_date(limit=500, year=None, month=None):
    """Generate HTML gallery organized by date."""
    user = get_user()
    user_nsid = user.id
    print(f"Fetching photos for {user.username}...")
    
    photos_data = []
    page = 1
    per_page = 100
    
    while len(photos_data) < limit:
        print(f"  Page {page}...", end='\r')
        
        if year and month:
            min_date = f"{year}-{month:02d}-01"
            max_date = f"{year}-{month:02d}-31"
            results = flickr_call(
                flickr.photos.search,
                user_id=user_nsid,
                min_taken_date=min_date,
                max_taken_date=max_date,
                extras=EXTRAS,
                per_page=per_page,
                page=page
            )
        else:
            results = flickr_call(
                flickr.people.getPhotos,
                user_id=user_nsid,
                extras=EXTRAS,
                per_page=per_page,
                page=page
            )
        
        photos = results['photos']['photo']
        if not photos:
            break
        
        for p in photos:
            thumb_url = p.get('url_q') or p.get('url_sq') or p.get('url_m')
            if thumb_url:
                photos_data.append({
                    'id': p['id'],
                    'title': p.get('title', ''),
                    'taken': p.get('datetaken', ''),
                    'uploaded': p.get('dateupload', ''),
                    'thumb_url': thumb_url,
                    'large_url': p.get('url_l') or p.get('url_z') or thumb_url,
                    'page_url': f"https://www.flickr.com/photos/{user_nsid}/{p['id']}"
                })
        
        page += 1
        if page > int(results['photos']['pages']):
            break
    
    print(f"\nFetched {len(photos_data)} photos")
    
    # Generate HTML
    html = generate_browse_html(photos_data, f"{user.username}'s Photos", group_by='date')
    
    output_file = Path(tempfile.gettempdir()) / 'flickr_browse_date.html'
    output_file.write_text(html, encoding='utf-8')
    print(f"Gallery: {output_file}")
    webbrowser.open(f'file://{output_file}')


def browse_by_album():
    """Generate HTML gallery organized by album."""
    user = get_user()
    user_nsid = user.id
    print(f"Fetching albums for {user.username}...")
    
    photosets = list(user.getPhotosets())
    print(f"Found {len(photosets)} albums")
    
    albums_data = {}
    
    for i, ps in enumerate(photosets):
        info = ps.getInfo()
        album_name = info.get('title', 'Untitled')
        print(f"  [{i+1}/{len(photosets)}] {album_name}...", end='\r')
        
        try:
            photos = list(ps.getPhotos())[:100]
            album_photos = []
            
            for photo in photos:
                thumb_url = get_photo_thumb_url(photo, 'q')
                if thumb_url:
                    album_photos.append({
                        'id': photo.id,
                        'title': getattr(photo, 'title', ''),
                        'thumb_url': thumb_url,
                        'page_url': f"https://www.flickr.com/photos/{user_nsid}/{photo.id}"
                    })
            
            if album_photos:
                albums_data[album_name] = album_photos
                
        except Exception as e:
            print(f"\n  Error: {album_name}: {e}")
    
    total = sum(len(p) for p in albums_data.values())
    print(f"\nFetched {total} photos from {len(albums_data)} albums")
    
    html = generate_browse_html(albums_data, f"{user.username}'s Albums", group_by='album')
    
    output_file = Path(tempfile.gettempdir()) / 'flickr_browse_album.html'
    output_file.write_text(html, encoding='utf-8')
    print(f"Gallery: {output_file}")
    webbrowser.open(f'file://{output_file}')


def generate_browse_html(data, title, group_by=None):
    """Generate HTML gallery."""
    html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title}</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, sans-serif; background: #1a1a1a; color: #fff; padding: 20px; }}
        h1 {{ margin-bottom: 20px; font-weight: 300; }}
        h2 {{ margin: 30px 0 15px; font-weight: 400; color: #aaa; font-size: 1.2em; }}
        .gallery {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; }}
        .photo {{ aspect-ratio: 1; overflow: hidden; border-radius: 4px; transition: transform 0.2s; }}
        .photo:hover {{ transform: scale(1.05); z-index: 10; }}
        .photo img {{ width: 100%; height: 100%; object-fit: cover; cursor: pointer; }}
        .photo a {{ display: block; width: 100%; height: 100%; }}
    </style>
</head>
<body>
    <h1>📷 {title}</h1>
'''
    
    if group_by == 'date':
        by_date = defaultdict(list)
        for p in data:
            date_str = (p.get('taken', '') or p.get('uploaded', ''))[:10] or 'Unknown'
            by_date[date_str].append(p)
        
        for date in sorted(by_date.keys(), reverse=True):
            html += f'<h2>{date} ({len(by_date[date])})</h2>\n<div class="gallery">\n'
            for p in by_date[date]:
                html += f'<div class="photo"><a href="{p["page_url"]}" target="_blank"><img src="{p["thumb_url"]}" loading="lazy"></a></div>\n'
            html += '</div>\n'
    
    elif group_by == 'album':
        for album_name, photos in data.items():
            html += f'<h2>{album_name} ({len(photos)})</h2>\n<div class="gallery">\n'
            for p in photos:
                html += f'<div class="photo"><a href="{p["page_url"]}" target="_blank"><img src="{p["thumb_url"]}" loading="lazy"></a></div>\n'
            html += '</div>\n'
    
    html += '</body></html>'
    return html


# ============== SERVE ==============

def serve_gallery(port=8080):
    """Start local web server for browsing."""
    user = get_user()
    user_nsid = user.id
    username = user.username
    
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        
        def do_GET(self):
            if self.path == '/' or self.path.startswith('/index'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                html = f'''<!DOCTYPE html>
<html><head><title>{username} - Flickr</title>
<style>
body {{ font-family: -apple-system, sans-serif; background: #111; color: #fff; }}
.header {{ position: fixed; top: 0; left: 0; right: 0; background: #222; padding: 15px 20px; z-index: 100; display: flex; gap: 20px; align-items: center; }}
.tab {{ padding: 8px 16px; background: #333; border: none; color: #fff; cursor: pointer; border-radius: 4px; }}
.tab.active {{ background: #0066cc; }}
.content {{ padding: 80px 20px 20px; }}
.gallery {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 6px; }}
.photo {{ aspect-ratio: 1; overflow: hidden; border-radius: 3px; background: #333; }}
.photo img {{ width: 100%; height: 100%; object-fit: cover; }}
.section {{ margin-bottom: 30px; }}
.section-title {{ font-size: 1.1em; color: #888; margin-bottom: 10px; }}
</style></head><body>
<div class="header"><h1 style="font-size:1.3em;font-weight:400">📷 {username}</h1>
<button class="tab active" onclick="load('date')">By Date</button>
<button class="tab" onclick="load('album')">By Album</button></div>
<div class="content" id="content"><p>Loading...</p></div>
<script>
async function load(mode) {{
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    event.target.classList.add('active');
    const r = await fetch('/api/'+mode);
    const d = await r.json();
    let h = '';
    if (mode === 'date') {{
        const byDate = {{}};
        d.photos.forEach(p => {{
            const dt = (p.taken||'').slice(0,10)||'Unknown';
            if(!byDate[dt]) byDate[dt]=[];
            byDate[dt].push(p);
        }});
        Object.keys(byDate).sort().reverse().forEach(dt => {{
            h += '<div class="section"><div class="section-title">'+dt+' ('+byDate[dt].length+')</div><div class="gallery">';
            byDate[dt].forEach(p => h += '<div class="photo"><a href="'+p.url+'" target="_blank"><img src="'+p.thumb+'" loading="lazy"></a></div>');
            h += '</div></div>';
        }});
    }} else {{
        d.albums.forEach(a => {{
            h += '<div class="section"><div class="section-title">'+a.title+' ('+a.photos.length+')</div><div class="gallery">';
            a.photos.forEach(p => h += '<div class="photo"><a href="'+p.url+'" target="_blank"><img src="'+p.thumb+'" loading="lazy"></a></div>');
            h += '</div></div>';
        }});
    }}
    document.getElementById('content').innerHTML = h;
}}
load('date');
</script></body></html>'''
                self.wfile.write(html.encode())
            
            elif self.path == '/api/date':
                photos = []
                for page in range(1, 6):
                    r = flickr_call(flickr.people.getPhotos, user_id=user_nsid, extras=EXTRAS, per_page=100, page=page)
                    for p in r['photos']['photo']:
                        photos.append({'id': p['id'], 'taken': p.get('datetaken',''),
                            'thumb': p.get('url_q') or p.get('url_sq',''),
                            'url': f"https://flickr.com/photos/{user_nsid}/{p['id']}"})
                    if page >= int(r['photos']['pages']): break
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'photos': photos}).encode())
            
            elif self.path == '/api/album':
                albums = []
                for ps in list(user.getPhotosets())[:20]:
                    info = ps.getInfo()
                    photos = []
                    for photo in list(ps.getPhotos())[:50]:
                        thumb = get_photo_thumb_url(photo, 'q')
                        if thumb: photos.append({'id': photo.id, 'thumb': thumb, 
                            'url': f"https://flickr.com/photos/{user_nsid}/{photo.id}"})
                    albums.append({'id': ps.id, 'title': info.get('title',''), 'photos': photos})
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'albums': albums}).encode())
            
            else:
                self.send_error(404)
    
    with socketserver.TCPServer(("", port), Handler) as httpd:
        url = f"http://localhost:{port}"
        print(f"🌐 Server: {url}")
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


# ============== DOWNLOAD ALBUM ==============

def extract_album_id(album_input: str, lookup_by_name: bool = True) -> str:
    """Extract album ID from URL, name, or return as-is.

    Supports:
    - Album ID (numeric string)
    - Flickr URL
    - Album name (if lookup_by_name=True)
    """
    # Check if it's a URL
    if 'flickr.com' in album_input:
        parts = album_input.rstrip('/').split('/')
        for i, part in enumerate(parts):
            if part == 'albums' and i + 1 < len(parts):
                return parts[i + 1]
        for part in reversed(parts):
            if part.isdigit():
                return part

    # Check if it's already an ID (all digits)
    if album_input.isdigit():
        return album_input

    # Try to look up by name
    if lookup_by_name:
        try:
            user = get_user()
            for ps in user.getPhotosets():
                info = ps.getInfo()
                if info.get('title', '') == album_input:
                    print(f"📁 Found album: {album_input} → {ps.id}")
                    return ps.id
            print(f"⚠️  Album not found by name: {album_input}")
        except Exception as e:
            print(f"⚠️  Error looking up album: {e}")

    return album_input


def download_album(album_id: str, output_dir: Path, limit: int = None, 
                   size: str = 'original', meta_only: bool = False):
    """Download photos from a specific album."""
    album_id = extract_album_id(album_id)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"📁 Fetching album {album_id}...")
    
    try:
        photoset = flickr_api.Photoset(id=album_id)
        info = photoset.getInfo()
        title = info.get('title', album_id)
        total = int(info.get('photos', 0)) + int(info.get('videos', 0))
        owner = info.get('owner', {})
        owner_nsid = owner.get('nsid', '') if isinstance(owner, dict) else str(owner)
        
        print(f"   Title: {title}")
        print(f"   Photos: {total}")
    except Exception as e:
        print(f"❌ Error fetching album: {e}")
        return
    
    safe_title = "".join(c for c in title if c.isalnum() or c in ' -_').strip()[:80]
    album_dir = output_dir / f"{safe_title}_{album_id}"
    album_dir.mkdir(parents=True, exist_ok=True)
    
    (album_dir / '_album.json').write_text(json.dumps({
        'id': album_id, 'title': title, 'photo_count': total,
        'downloaded_at': datetime.now().isoformat()
    }, indent=2, ensure_ascii=False))
    
    photos = list(photoset.getPhotos())
    if limit:
        photos = photos[:limit]
    
    print(f"\n📥 Downloading {len(photos)} photos...")
    downloaded = errors = 0
    
    for i, photo in enumerate(photos, 1):
        photo_id = photo.id
        print(f"[{i}/{len(photos)}] {photo_id}", end='')
        
        try:
            pinfo = photo.getInfo()
            fmt = pinfo.get('originalformat', 'jpg') or 'jpg'
            photo_file = album_dir / f"{photo_id}.{fmt}"
            meta_file = album_dir / f"{photo_id}.json"
            
            if photo_file.exists() and meta_file.exists():
                print(f" (skip)")
                continue
            
            details = {
                'id': photo_id,
                'title': pinfo.get('title', ''),
                'taken': pinfo.get('dates', {}).get('taken', ''),
            }
            try:
                details['tags'] = [t.text for t in photo.getTags()]
            except:
                details['tags'] = []
            
            meta_file.write_text(json.dumps(details, indent=2, ensure_ascii=False))
            
            if not meta_only:
                sizes = photo.getSizes()
                size_prefs = SIZE_MAP.get(size, SIZE_MAP['original'])
                for pref in size_prefs:
                    if pref in sizes:
                        photo.save(str(photo_file), size_label=pref)
                        break
                downloaded += 1
                print(f" ✓")
            else:
                print(f" ✓ (meta)")
                downloaded += 1
                
        except Exception as e:
            print(f" ✗ {e}")
            errors += 1
    
    print(f"\n✅ Done: {downloaded} downloaded, {errors} errors")
    print(f"📂 {album_dir}")


# ============== UPLOAD ==============

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.tiff', '.tif', '.bmp', '.webp'}
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.m4v', '.3gp'}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS


def upload_photos(path: Path, album_name: str = None, tags: str = '', 
                  privacy: str = 'private', title: str = None, description: str = None,
                  limit: int = None, dry_run: bool = False):
    """Upload photos to Flickr."""
    path = Path(path)
    
    if path.is_file():
        files = [path] if path.suffix.lower() in MEDIA_EXTS else []
    else:
        files = sorted([f for f in path.rglob('*') if f.is_file() and f.suffix.lower() in MEDIA_EXTS])
        # Use folder name as album if not specified
        if not album_name:
            album_name = path.name
    
    if not files:
        print(f"❌ No media files found")
        return
    
    if limit:
        files = files[:limit]
    
    print(f"📤 Uploading {len(files)} files")
    if album_name:
        print(f"   Album: {album_name}")
    print(f"   Privacy: {privacy}")
    
    if dry_run:
        for f in files:
            print(f"  [dry] {f.name}")
        return
    
    is_public = 1 if privacy == 'public' else 0
    is_friend = 1 if privacy == 'friends' else 0
    is_family = 1 if privacy == 'family' else 0
    
    uploaded_ids = []
    
    for i, f in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {f.name}", end='')
        try:
            photo = flickr_api.upload(
                photo_file=str(f),
                title=title or f.stem,
                description=description or '',
                tags=tags or '',
                is_public=is_public,
                is_friend=is_friend,
                is_family=is_family,
            )
            uploaded_ids.append(photo.id)
            print(f" ✓ {photo.id}")
        except Exception as e:
            print(f" ✗ {e}")
    
    if album_name and uploaded_ids:
        print(f"\n📁 Album: {album_name}")
        try:
            user = get_user()
            existing = None
            for ps in user.getPhotosets():
                if ps.getInfo().get('title') == album_name:
                    existing = ps
                    break
            
            if existing:
                for pid in uploaded_ids:
                    try:
                        existing.addPhoto(photo=flickr_api.Photo(id=pid))
                    except:
                        pass
                print(f"   Added to existing")
            else:
                album = flickr_api.Photoset.create(title=album_name, 
                    primary_photo=flickr_api.Photo(id=uploaded_ids[0]))
                for pid in uploaded_ids[1:]:
                    try:
                        album.addPhoto(photo=flickr_api.Photo(id=pid))
                    except:
                        pass
                print(f"   Created: {album.id}")
        except Exception as e:
            print(f"   ✗ {e}")
    
    print(f"\n✅ Uploaded: {len(uploaded_ids)}")


# ============== TAG ==============

def manage_tags(photo_id: str, action: str, tags: list):
    """Manage tags on a photo."""
    photo = flickr_api.Photo(id=photo_id)
    
    if action == 'list':
        try:
            current = photo.getTags()
            print(f"🏷️  Tags for {photo_id}:")
            for t in current:
                print(f"   • {t.text}")
        except Exception as e:
            print(f"❌ {e}")
    
    elif action == 'add':
        for tag in tags:
            try:
                photo.addTags(tags=tag)
                print(f"✓ Added: {tag}")
            except Exception as e:
                print(f"✗ {tag}: {e}")
    
    elif action == 'remove':
        try:
            current = {t.text.lower(): t for t in photo.getTags()}
            for tag in tags:
                if tag.lower() in current:
                    current[tag.lower()].remove()
                    print(f"✓ Removed: {tag}")
                else:
                    print(f"  Not found: {tag}")
        except Exception as e:
            print(f"❌ {e}")


# ============== DELETE ==============

def delete_photos(photo_ids: list, confirm: bool = True):
    """Delete photos from Flickr."""
    if not photo_ids:
        print("❌ No photo IDs specified")
        return
    
    print(f"🗑️  Deleting {len(photo_ids)} photo(s)...")
    
    if confirm:
        resp = input(f"Are you sure? (yes/no): ").strip().lower()
        if resp != 'yes':
            print("Cancelled.")
            return
    
    deleted = errors = 0
    for pid in photo_ids:
        try:
            photo = flickr_api.Photo(id=pid)
            photo.delete()
            print(f"  ✓ {pid}")
            deleted += 1
        except Exception as e:
            print(f"  ✗ {pid}: {e}")
            errors += 1
    
    print(f"\n✅ Deleted: {deleted}, Errors: {errors}")


def delete_album(album_id: str, keep_photos: bool = True, confirm: bool = True):
    """Delete an album from Flickr."""
    album_id = extract_album_id(album_id)
    
    try:
        photoset = flickr_api.Photoset(id=album_id)
        info = photoset.getInfo()
        title = info.get('title', album_id)
        count = info.get('photos', 0)
        
        print(f"🗑️  Album: {title}")
        print(f"   Photos: {count}")
        print(f"   Keep photos: {'Yes' if keep_photos else 'No (will delete!)'}")
        
        if confirm:
            resp = input("Delete this album? (yes/no): ").strip().lower()
            if resp != 'yes':
                print("Cancelled.")
                return
        
        if not keep_photos:
            print("   Deleting photos first...")
            for photo in photoset.getPhotos():
                try:
                    photo.delete()
                    print(f"     ✓ {photo.id}")
                except:
                    pass
        
        photoset.delete()
        print(f"✅ Album deleted")

    except Exception as e:
        print(f"❌ Error: {e}")


def delete_albums_batch(pattern: str = None, empty: bool = False,
                        max_photos: int = None, confirm: bool = True):
    """Delete multiple albums matching criteria."""
    user = get_user()
    photosets = list(user.getPhotosets())

    regex = re.compile(pattern) if pattern else None

    # Find matching albums
    to_delete = []
    print(f"Scanning {len(photosets)} albums...")
    for ps in photosets:
        info = ps.getInfo()
        title = info.get('title', '')
        count = int(info.get('photos', 0))

        if regex and not regex.search(title):
            continue
        if empty and count > 0:
            continue
        if max_photos is not None and count > max_photos:
            continue

        to_delete.append({
            'id': ps.id,
            'title': title,
            'photo_count': count,
        })

    if not to_delete:
        print("No albums matched the criteria.")
        return

    # Show what will be deleted
    print(f"\n🗑️  Albums to delete ({len(to_delete)}):")
    print(f"{'ID':<20} {'#':>5}  Title")
    print("-" * 60)
    for album in to_delete:
        print(f"{album['id']:<20} {album['photo_count']:>5}  {album['title']}")

    print(f"\n⚠️  This will delete {len(to_delete)} albums (photos will be kept)")

    if confirm:
        resp = input("Type 'yes' to confirm: ").strip().lower()
        if resp != 'yes':
            print("Cancelled.")
            return

    # Delete albums
    deleted = errors = 0
    for album in to_delete:
        try:
            ps = flickr_api.Photoset(id=album['id'])
            ps.delete()
            print(f"  ✓ {album['title']}")
            deleted += 1
        except Exception as e:
            print(f"  ✗ {album['title']}: {e}")
            errors += 1

    print(f"\n✅ Deleted: {deleted}, Errors: {errors}")


def merge_albums(target_id: str, source_ids: list = None, pattern: str = None,
                 delete_source: bool = False, confirm: bool = True,
                 create_target: bool = False):
    """Merge albums into one target album."""
    target_ps = None
    target_name = target_id  # Save original name for creating

    # First, find source albums (we need them to potentially create target)
    sources = []
    if source_ids:
        for sid in source_ids:
            sid = extract_album_id(sid)
            try:
                ps = flickr_api.Photoset(id=sid)
                info = ps.getInfo()
                sources.append({
                    'id': sid,
                    'title': info.get('title', ''),
                    'photo_count': int(info.get('photos', 0)),
                    'photoset': ps,
                })
            except:
                print(f"  ⚠️  Album not found: {sid}")

    if pattern:
        user = get_user()
        regex = re.compile(pattern)
        print(f"Scanning albums for pattern: {pattern}")
        for ps in user.getPhotosets():
            info = ps.getInfo()
            title = info.get('title', '')
            if regex.search(title):
                sources.append({
                    'id': ps.id,
                    'title': title,
                    'photo_count': int(info.get('photos', 0)),
                    'photoset': ps,
                })

    if not sources:
        print("No source albums found.")
        return

    # Try to find or create target album
    target_id_resolved = extract_album_id(target_id, lookup_by_name=True)

    try:
        target_ps = flickr_api.Photoset(id=target_id_resolved)
        target_info = target_ps.getInfo()
        print(f"📁 Target album: {target_info.get('title', target_id_resolved)}")
        # Remove target from sources if it was included
        sources = [s for s in sources if s['id'] != target_id_resolved]
    except Exception as e:
        if create_target:
            # Create new album using first photo from first source
            print(f"📁 Creating new album: {target_name}")
            first_photo = None
            for source in sources:
                try:
                    photos = list(source['photoset'].getPhotos())
                    if photos:
                        first_photo = photos[0]
                        break
                except:
                    continue

            if not first_photo:
                print("❌ Cannot create album: no photos found in source albums")
                return

            try:
                target_ps = flickr_api.Photoset.create(
                    title=target_name,
                    primary_photo=first_photo
                )
                print(f"   ✓ Created album: {target_ps.id}")
            except Exception as ce:
                print(f"❌ Failed to create album: {ce}")
                return
        else:
            print(f"❌ Target album not found: {target_id}")
            print(f"   Use --create to create a new album with this name")
            return

    if not sources:
        print("No source albums found.")
        return

    # Show merge plan
    total_photos = sum(s['photo_count'] for s in sources)
    print(f"\n📥 Source albums ({len(sources)}, {total_photos} photos):")
    for s in sources:
        print(f"   {s['id']}  {s['photo_count']:>5}  {s['title']}")

    if delete_source:
        print(f"\n⚠️  Source albums will be deleted after merge")

    if confirm:
        resp = input("\nType 'yes' to merge: ").strip().lower()
        if resp != 'yes':
            print("Cancelled.")
            return

    # Merge photos
    merged = errors = 0
    for source in sources:
        print(f"\n  Merging: {source['title']}...")
        try:
            for photo in source['photoset'].getPhotos():
                try:
                    target_ps.addPhoto(photo=photo)
                    merged += 1
                except:
                    pass  # Photo might already be in target
            print(f"    ✓ {source['photo_count']} photos")

            if delete_source:
                source['photoset'].delete()
                print(f"    🗑️  Album deleted")
        except Exception as e:
            print(f"    ✗ Error: {e}")
            errors += 1

    print(f"\n✅ Merged {merged} photos from {len(sources)} albums")
    if errors:
        print(f"   Errors: {errors}")


# ============== REPLACE ==============

def replace_photo(photo_id: str, file_path: Path):
    """Replace a photo file (keeps ID, comments, faves)."""
    file_path = Path(file_path)
    if not file_path.exists():
        print(f"❌ File not found: {file_path}")
        return
    
    print(f"🔄 Replacing photo {photo_id} with {file_path.name}...")
    
    try:
        photo = flickr_api.Photo(id=photo_id)
        old_info = photo.getInfo()
        print(f"   Old title: {old_info.get('title', '')}")
        
        # Flickr API: flickr.replace
        result = flickr_api.replace(
            photo_file=str(file_path),
            photo_id=photo_id
        )
        
        print(f"✅ Replaced successfully")
        print(f"   Photo ID: {photo_id} (unchanged)")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        print("   Note: Replace may fail if you don't own the photo")


# ============== RENAME ==============

def rename_photo(photo_id: str, new_title: str = None, new_description: str = None):
    """Rename a photo's title and/or description."""
    try:
        photo = flickr_api.Photo(id=photo_id)
        info = photo.getInfo()
        
        print(f"📝 Photo {photo_id}")
        print(f"   Current title: {info.get('title', '')}")
        
        kwargs = {}
        if new_title is not None:
            kwargs['title'] = new_title
        if new_description is not None:
            kwargs['description'] = new_description
        
        if kwargs:
            photo.setMeta(**kwargs)
            print(f"✅ Updated")
            if new_title:
                print(f"   New title: {new_title}")
            if new_description:
                print(f"   New description: {new_description}")
        else:
            print("   Nothing to change")
            
    except Exception as e:
        print(f"❌ Error: {e}")


def rename_album(album_id: str, new_title: str = None, new_description: str = None):
    """Rename an album's title and/or description."""
    album_id = extract_album_id(album_id)
    
    try:
        photoset = flickr_api.Photoset(id=album_id)
        info = photoset.getInfo()
        
        print(f"📝 Album {album_id}")
        print(f"   Current title: {info.get('title', '')}")
        
        title = new_title if new_title else info.get('title', '')
        desc = new_description if new_description is not None else info.get('description', '')
        
        photoset.editMeta(title=title, description=desc)
        print(f"✅ Updated")
        if new_title:
            print(f"   New title: {new_title}")
            
    except Exception as e:
        print(f"❌ Error: {e}")


# ============== PRIVACY ==============

def set_privacy(photo_ids: list, privacy: str):
    """Set privacy for multiple photos."""
    if not photo_ids:
        print("❌ No photo IDs specified")
        return
    
    privacy_map = {
        'public': {'is_public': 1, 'is_friend': 0, 'is_family': 0},
        'private': {'is_public': 0, 'is_friend': 0, 'is_family': 0},
        'friends': {'is_public': 0, 'is_friend': 1, 'is_family': 0},
        'family': {'is_public': 0, 'is_friend': 0, 'is_family': 1},
        'friends_family': {'is_public': 0, 'is_friend': 1, 'is_family': 1},
    }
    
    if privacy not in privacy_map:
        print(f"❌ Invalid privacy: {privacy}")
        print(f"   Options: {', '.join(privacy_map.keys())}")
        return
    
    settings = privacy_map[privacy]
    print(f"🔒 Setting {len(photo_ids)} photo(s) to '{privacy}'...")
    
    updated = errors = 0
    for pid in photo_ids:
        try:
            photo = flickr_api.Photo(id=pid)
            photo.setPerms(**settings)
            print(f"  ✓ {pid}")
            updated += 1
        except Exception as e:
            print(f"  ✗ {pid}: {e}")
            errors += 1
    
    print(f"\n✅ Updated: {updated}, Errors: {errors}")


# ============== MOVE / COPY ==============

def move_photo(photo_id: str, to_album_id: str, from_album_id: str = None):
    """Move photo to another album."""
    to_album_id = extract_album_id(to_album_id)
    
    try:
        photo = flickr_api.Photo(id=photo_id)
        to_album = flickr_api.Photoset(id=to_album_id)
        
        # Add to new album
        to_album.addPhoto(photo=photo)
        print(f"✓ Added to album {to_album_id}")
        
        # Remove from old album if specified
        if from_album_id:
            from_album_id = extract_album_id(from_album_id)
            from_album = flickr_api.Photoset(id=from_album_id)
            from_album.removePhoto(photo=photo)
            print(f"✓ Removed from album {from_album_id}")
        
        print(f"✅ Moved photo {photo_id}")
        
    except Exception as e:
        print(f"❌ Error: {e}")


def copy_photo(photo_id: str, to_album_id: str):
    """Copy photo to another album (add without removing from original)."""
    to_album_id = extract_album_id(to_album_id)
    
    try:
        photo = flickr_api.Photo(id=photo_id)
        to_album = flickr_api.Photoset(id=to_album_id)
        to_album.addPhoto(photo=photo)
        print(f"✅ Copied photo {photo_id} to album {to_album_id}")
        
    except Exception as e:
        print(f"❌ Error: {e}")


# ============== ALBUM MANAGEMENT ==============

def create_album(title: str, description: str = '', primary_photo_id: str = None):
    """Create a new album."""
    if not primary_photo_id:
        print("❌ Need a primary photo ID to create album")
        print("   Upload a photo first, then create album with that photo")
        return
    
    try:
        primary = flickr_api.Photo(id=primary_photo_id)
        album = flickr_api.Photoset.create(title=title, description=description, primary_photo=primary)
        print(f"✅ Created album: {title}")
        print(f"   ID: {album.id}")
        
    except Exception as e:
        print(f"❌ Error: {e}")


def reorder_album(album_id: str, order_by: str = 'date'):
    """Reorder photos in an album."""
    album_id = extract_album_id(album_id)
    
    try:
        photoset = flickr_api.Photoset(id=album_id)
        photos = list(photoset.getPhotos())
        
        print(f"📋 Reordering album {album_id} by {order_by}...")
        print(f"   Photos: {len(photos)}")
        
        # Get photo info for sorting
        photo_data = []
        for p in photos:
            try:
                info = p.getInfo()
                photo_data.append({
                    'id': p.id,
                    'title': info.get('title', ''),
                    'taken': info.get('dates', {}).get('taken', ''),
                })
            except:
                photo_data.append({'id': p.id, 'title': '', 'taken': ''})
        
        # Sort
        if order_by == 'date':
            photo_data.sort(key=lambda x: x['taken'])
        elif order_by == 'date_desc':
            photo_data.sort(key=lambda x: x['taken'], reverse=True)
        elif order_by == 'title':
            photo_data.sort(key=lambda x: x['title'].lower())
        
        # Apply order
        photo_ids = ','.join([p['id'] for p in photo_data])
        flickr_call(flickr.photosets.reorderPhotos, photoset_id=album_id, photo_ids=photo_ids)
        
        print(f"✅ Reordered")
        
    except Exception as e:
        print(f"❌ Error: {e}")


# ============== INFO ==============

def show_photo_info(photo_id: str):
    """Show detailed info for a photo."""
    try:
        photo = flickr_api.Photo(id=photo_id)
        info = photo.getInfo()
        
        print(f"📷 Photo {photo_id}")
        print(f"{'='*50}")
        print(f"Title: {info.get('title', '')}")
        print(f"Description: {info.get('description', '')[:100]}")
        
        dates = info.get('dates', {})
        print(f"\n📅 Dates")
        print(f"   Taken: {dates.get('taken', 'N/A')}")
        print(f"   Uploaded: {dates.get('posted', 'N/A')}")
        
        vis = info.get('visibility', {})
        privacy = 'Public' if vis.get('ispublic') else 'Private'
        if vis.get('isfriend'): privacy += '+Friends'
        if vis.get('isfamily'): privacy += '+Family'
        print(f"\n🔒 Privacy: {privacy}")
        
        print(f"👁️  Views: {info.get('views', 0)}")
        
        # Tags
        try:
            tags = photo.getTags()
            if tags:
                print(f"\n🏷️  Tags: {', '.join([t.text for t in tags])}")
        except:
            pass
        
        # Albums
        try:
            contexts = flickr_call(flickr.photos.getAllContexts, photo_id=photo_id)
            albums = contexts.get('set', [])
            if albums:
                print(f"\n📁 Albums:")
                for a in albums:
                    print(f"   • {a.get('title', '')} ({a.get('id', '')})")
        except:
            pass
        
        # EXIF
        try:
            exif = photo.getExif()
            if exif:
                print(f"\n📸 EXIF:")
                for item in exif[:10]:
                    print(f"   {item.get('label', '')}: {item.get('clean', item.get('raw', ''))}")
        except:
            pass
        
        # URL
        owner = info.get('owner', {})
        nsid = owner.get('nsid', '') if isinstance(owner, dict) else str(owner)
        print(f"\n🔗 URL: https://www.flickr.com/photos/{nsid}/{photo_id}")
        
    except Exception as e:
        print(f"❌ Error: {e}")


# ============== FAVORITES ==============

def list_faves(limit: int = 50):
    """List your favorite photos."""
    try:
        user = get_user()
        result = flickr_call(flickr.favorites.getList, user_id=user.id, per_page=limit, extras=EXTRAS)
        photos = result.get('photos', {}).get('photo', [])
        
        print(f"❤️  Your favorites ({len(photos)}):")
        print(f"{'ID':<15} {'Date':<12} Title")
        print("-" * 50)
        
        for p in photos:
            print(f"{p['id']:<15} {p.get('datetaken', '')[:10]:<12} {p.get('title', '')[:30]}")
            
    except Exception as e:
        print(f"❌ Error: {e}")


def add_fave(photo_id: str):
    """Add photo to favorites."""
    try:
        flickr_call(flickr.favorites.add, photo_id=photo_id)
        print(f"❤️  Added {photo_id} to favorites")
    except Exception as e:
        print(f"❌ Error: {e}")


def remove_fave(photo_id: str):
    """Remove photo from favorites."""
    try:
        flickr_call(flickr.favorites.remove, photo_id=photo_id)
        print(f"💔 Removed {photo_id} from favorites")
    except Exception as e:
        print(f"❌ Error: {e}")


# ============== COMMENTS ==============

def list_comments(photo_id: str):
    """List comments on a photo."""
    try:
        result = flickr_call(flickr.photos.comments.getList, photo_id=photo_id)
        comments = result.get('comments', {}).get('comment', [])
        
        print(f"💬 Comments on {photo_id}:")
        
        if not comments:
            print("   (no comments)")
            return
        
        for c in comments:
            author = c.get('authorname', 'Unknown')
            date = c.get('datecreate', '')
            text = c.get('_content', '')[:100]
            print(f"\n   [{c.get('id', '')}] {author} ({date})")
            print(f"   {text}")
            
    except Exception as e:
        print(f"❌ Error: {e}")


def add_comment(photo_id: str, text: str):
    """Add a comment to a photo."""
    try:
        flickr.photos.comments.addComment(photo_id=photo_id, comment_text=text)
        print(f"💬 Comment added to {photo_id}")
    except Exception as e:
        print(f"❌ Error: {e}")


def delete_comment(comment_id: str):
    """Delete a comment."""
    try:
        flickr.photos.comments.deleteComment(comment_id=comment_id)
        print(f"🗑️  Comment {comment_id} deleted")
    except Exception as e:
        print(f"❌ Error: {e}")


# ============== DOWNLOAD BY PHOTO ==============

def download_albums_by_photo(photo_id: str, output_dir: Path, limit: int = None,
                              size: str = 'original', meta_only: bool = False):
    """Download all albums containing a specific photo."""
    print(f"🔍 Finding albums for photo {photo_id}...")

    try:
        contexts = flickr_call(flickr.photos.getAllContexts, photo_id=photo_id)
        albums = contexts.get('set', [])
        
        if not albums:
            print("   No albums found for this photo")
            return
        
        print(f"   Found {len(albums)} albums:")
        for a in albums:
            print(f"   • {a.get('title', 'Untitled')} ({a.get('id')})")
        
        print()
        for i, album in enumerate(albums, 1):
            print(f"\n[Album {i}/{len(albums)}] {album.get('title', 'Untitled')}")
            download_album(
                album_id=album.get('id'),
                output_dir=output_dir,
                limit=limit,
                size=size,
                meta_only=meta_only
            )
        
        print(f"\n{'='*40}")
        print(f"✅ Downloaded {len(albums)} albums")
        
    except Exception as e:
        print(f"❌ Error: {e}")


# ============== SEARCH ==============

def search_photos(user: str = None, tags: str = None, text: str = None, 
                  date: str = None, limit: int = 100, download: bool = False,
                  output_dir: Path = None):
    """Search photos."""
    params = {'extras': EXTRAS, 'per_page': min(limit, 500)}
    
    if user:
        if '@' in user or user.isdigit():
            params['user_id'] = user
        else:
            try:
                found = flickr_call(flickr.people.findByUsername, username=user)
                params['user_id'] = found['user']['nsid']
            except:
                print(f"❌ User not found: {user}")
                return
    else:
        params['user_id'] = get_user().id
    
    if tags:
        params['tags'] = tags
        params['tag_mode'] = 'all'
    if text:
        params['text'] = text
    if date:
        if len(date) == 7:
            params['min_taken_date'] = f"{date}-01"
            params['max_taken_date'] = f"{date}-31"
        else:
            params['min_taken_date'] = date
            params['max_taken_date'] = date
    
    print(f"🔍 Searching...")
    
    try:
        results = flickr_call(flickr.photos.search, **params)
        photos = results['photos']['photo']
        total = int(results['photos']['total'])
        
        print(f"   Found: {total}")
        
        for p in photos[:limit]:
            taken = p.get('datetaken', '')[:10]
            print(f"  {p['id']}  {taken}  {p.get('title', '')[:40]}")
        
        if download and output_dir:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            print(f"\n📥 Downloading...")
            for p in photos[:limit]:
                try:
                    photo = flickr_api.Photo(id=p['id'])
                    photo.save(str(output_dir / f"{p['id']}.jpg"))
                    print(f"  ✓ {p['id']}")
                except:
                    print(f"  ✗ {p['id']}")
            print(f"📂 {output_dir}")
            
    except Exception as e:
        print(f"❌ {e}")


# ============== LIST (extended) ==============

def list_albums_for_user(username: str = None, pattern: str = None,
                         empty: bool = False, max_photos: int = None, min_photos: int = None):
    """List albums for a user with optional filters."""
    if username:
        try:
            found = flickr_call(flickr.people.findByUsername, username=username)
            user_nsid = found['user']['nsid']
            print(f"👤 {username} ({user_nsid})")
            person = flickr_api.Person(id=user_nsid)
            photosets = list(person.getPhotosets())
        except:
            print(f"❌ User not found: {username}")
            return []
    else:
        user = get_user()
        print(f"👤 {user.username}")
        photosets = list(user.getPhotosets())

    # Compile regex pattern if provided
    regex = re.compile(pattern) if pattern else None

    # Filter and collect album info
    filtered = []
    print(f"\nScanning {len(photosets)} albums...")
    for i, ps in enumerate(photosets):
        info = ps.getInfo()
        title = info.get('title', '')
        count = int(info.get('photos', 0))

        # Apply filters
        if regex and not regex.search(title):
            continue
        if empty and count > 0:
            continue
        if max_photos is not None and count > max_photos:
            continue
        if min_photos is not None and count < min_photos:
            continue

        filtered.append({
            'id': ps.id,
            'title': title,
            'photo_count': count,
        })

    # Display results
    print(f"\n{'ID':<20} {'#':>5}  Title")
    print("-" * 60)
    for album in filtered:
        print(f"{album['id']:<20} {album['photo_count']:>5}  {album['title']}")

    print(f"\nMatched: {len(filtered)} / {len(photosets)} albums")
    return filtered


# ============== CLI ==============

SIZE_MAP = {
    'original': ['Original', 'Large 2048', 'Large 1600', 'Large'],
    'large': ['Large 2048', 'Large 1600', 'Large', 'Medium 800'],
    'medium': ['Medium 800', 'Medium 640', 'Medium'],
    'small': ['Small 320', 'Small', 'Thumbnail'],
    'thumb': ['Large Square', 'Square'],
}


def main():
    parser = argparse.ArgumentParser(description='flickrvault v2.0')
    parser.add_argument('-c', '--config', type=Path, metavar='DIR',
                        help='Config directory (default: ~/.config/flickrvault or $FLICKRVAULT_CONFIG)')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='Quiet mode (errors only)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Verbose mode (debug output)')
    parser.add_argument('--progress', action='store_true',
                        help='Show progress bar instead of line-by-line output')
    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # Auth
    subparsers.add_parser('auth', help='Authenticate with Flickr')
    
    # Sync (main backup command)
    sync_parser = subparsers.add_parser('sync', help='Sync/backup your photos from Flickr')
    sync_parser.add_argument('-o', '--output', type=Path, default=Path('./flickr_backup'),
                             help='Output directory (default: ./flickr_backup)')
    sync_parser.add_argument('--full', action='store_true', help='Full sync (re-download all metadata)')
    sync_parser.add_argument('--meta-only', action='store_true', help='Only sync metadata, skip photo files')
    sync_parser.add_argument('-n', '--limit', type=int, help='Limit number of photos (for testing)')
    sync_parser.add_argument('--skip-albums', action='store_true', help='Skip album sync (Phase 4)')
    sync_parser.add_argument('--album-limit', type=int, help='Limit number of albums to sync')
    sync_parser.add_argument('--order', choices=['newest', 'oldest'], default='newest',
                             help='Photo order: newest first (default) or oldest first')
    sync_parser.add_argument('--from-date', help='Start date (YYYY-MM-DD or YYYY)')
    sync_parser.add_argument('--to-date', help='End date (YYYY-MM-DD or YYYY)')
    sync_parser.add_argument('-w', '--workers', type=int, default=4,
                             help='Number of parallel download workers (default: 4)')
    sync_parser.add_argument('--delay', type=float, default=0,
                             help='Initial delay between API requests (default: dynamic 0.2s, auto-adjusts)')
    sync_parser.add_argument('--wait', type=int, default=0,
                             help='Wait N minutes before starting (for rate limit cooldown)')
    # Output control (also available as global options before command)
    sync_parser.add_argument('-q', '--quiet', action='store_true', help='Quiet mode (errors only)')
    sync_parser.add_argument('-v', '--verbose', action='store_true', help='Verbose mode')
    sync_parser.add_argument('--progress', action='store_true', help='Show progress bar')

    # Download (specific album or by photo)
    dl_parser = subparsers.add_parser('download', help='Download album (by ID/URL or by photo)')
    dl_parser.add_argument('album', nargs='?', help='Album URL or ID')
    dl_parser.add_argument('--photo', '-p', help='Download all albums containing this photo ID')
    dl_parser.add_argument('-o', '--output', type=Path, default=Path('./flickr_download'),
                           help='Output directory')
    dl_parser.add_argument('-n', '--limit', type=int, help='Limit number of photos per album')
    dl_parser.add_argument('--size', choices=['original', 'large', 'medium', 'small', 'thumb'],
                           default='original', help='Download size (default: original)')
    dl_parser.add_argument('--meta-only', action='store_true', help='Only download metadata')
    
    # Upload
    up_parser = subparsers.add_parser('upload', help='Upload photos to Flickr')
    up_parser.add_argument('path', type=Path, help='File or folder to upload')
    up_parser.add_argument('-a', '--album', help='Album name (create if not exists)')
    up_parser.add_argument('-t', '--tags', help='Tags (comma-separated)')
    up_parser.add_argument('--privacy', choices=['public', 'private', 'friends', 'family'],
                           default='private', help='Privacy (default: private)')
    up_parser.add_argument('--title', help='Photo title (for single file)')
    up_parser.add_argument('--description', help='Photo description')
    up_parser.add_argument('-n', '--limit', type=int, help='Limit number of uploads')
    up_parser.add_argument('--dry-run', action='store_true', help='Show what would be uploaded')
    
    # Tag management
    tag_parser = subparsers.add_parser('tag', help='Manage photo tags')
    tag_parser.add_argument('photo_id', help='Photo ID')
    tag_parser.add_argument('action', choices=['list', 'add', 'remove'], help='Action')
    tag_parser.add_argument('tags', nargs='*', help='Tags to add/remove')
    
    # Search
    search_parser = subparsers.add_parser('search', help='Search photos')
    search_parser.add_argument('-u', '--user', help='Username or NSID (default: you)')
    search_parser.add_argument('-t', '--tags', help='Tags (comma-separated)')
    search_parser.add_argument('--text', help='Search text')
    search_parser.add_argument('--date', help='Date (YYYY-MM or YYYY-MM-DD)')
    search_parser.add_argument('-n', '--limit', type=int, default=100, help='Limit results')
    search_parser.add_argument('--download', action='store_true', help='Download results')
    search_parser.add_argument('-o', '--output', type=Path, default=Path('./flickr_search'),
                               help='Output directory for downloads')
    
    # Stats
    stats_parser = subparsers.add_parser('stats', help='Show backup statistics')
    stats_parser.add_argument('-o', '--output', type=Path, default=Path('./flickr_backup'),
                              help='Backup directory')
    
    # List
    list_parser = subparsers.add_parser('list', help='List albums')
    list_parser.add_argument('-u', '--user', help='Username or NSID (default: you)')
    list_parser.add_argument('--pattern', '-p', help='Filter albums by regex pattern')
    list_parser.add_argument('--empty', action='store_true', help='List only empty albums')
    list_parser.add_argument('--max-photos', type=int, help='List albums with at most N photos')
    list_parser.add_argument('--min-photos', type=int, help='List albums with at least N photos')
    
    # Browse
    browse_parser = subparsers.add_parser('browse', help='Visual browse in browser')
    browse_parser.add_argument('--by', choices=['date', 'album'], default='date')
    browse_parser.add_argument('-n', '--limit', type=int, default=500)
    browse_parser.add_argument('--year', type=int)
    browse_parser.add_argument('--month', type=int)
    
    # Serve
    serve_parser = subparsers.add_parser('serve', help='Start local web server')
    serve_parser.add_argument('-p', '--port', type=int, default=8080)
    
    # Delete
    del_parser = subparsers.add_parser('delete', help='Delete photos or albums')
    del_parser.add_argument('type', choices=['photo', 'album'], help='What to delete')
    del_parser.add_argument('ids', nargs='+', help='Photo/Album IDs to delete')
    del_parser.add_argument('--keep-photos', action='store_true', help='Keep photos when deleting album')
    del_parser.add_argument('-y', '--yes', action='store_true', help='Skip confirmation')
    
    # Replace
    replace_parser = subparsers.add_parser('replace', help='Replace photo file (keeps ID/comments/faves)')
    replace_parser.add_argument('photo_id', help='Photo ID to replace')
    replace_parser.add_argument('file', type=Path, help='New file')
    
    # Rename
    rename_parser = subparsers.add_parser('rename', help='Rename photo or album')
    rename_parser.add_argument('type', choices=['photo', 'album'], help='What to rename')
    rename_parser.add_argument('id', help='Photo/Album ID')
    rename_parser.add_argument('title', help='New title')
    rename_parser.add_argument('--description', '-d', help='New description')
    
    # Privacy
    priv_parser = subparsers.add_parser('privacy', help='Set photo privacy')
    priv_parser.add_argument('photo_ids', nargs='+', help='Photo IDs')
    priv_parser.add_argument('--set', required=True, 
                             choices=['public', 'private', 'friends', 'family', 'friends_family'],
                             help='Privacy setting')
    
    # Move
    move_parser = subparsers.add_parser('move', help='Move photo to album')
    move_parser.add_argument('photo_id', help='Photo ID')
    move_parser.add_argument('--to', required=True, help='Destination album ID')
    move_parser.add_argument('--from', dest='from_album', help='Source album ID (optional)')
    
    # Copy
    copy_parser = subparsers.add_parser('copy', help='Copy photo to album')
    copy_parser.add_argument('photo_id', help='Photo ID')
    copy_parser.add_argument('--to', required=True, help='Destination album ID')
    
    # Album management
    album_parser = subparsers.add_parser('album', help='Album management')
    album_sub = album_parser.add_subparsers(dest='album_action')
    
    album_create = album_sub.add_parser('create', help='Create album')
    album_create.add_argument('title', help='Album title')
    album_create.add_argument('--description', '-d', default='', help='Description')
    album_create.add_argument('--photo', '-p', required=True, help='Primary photo ID')
    
    album_delete = album_sub.add_parser('delete', help='Delete album(s)')
    album_delete.add_argument('album_id', nargs='?', help='Album ID (or use --pattern)')
    album_delete.add_argument('--pattern', '-p', help='Delete albums matching regex pattern')
    album_delete.add_argument('--empty', action='store_true', help='Delete empty albums')
    album_delete.add_argument('--max-photos', type=int, help='Delete albums with at most N photos')
    album_delete.add_argument('-y', '--yes', action='store_true', help='Skip confirmation')

    album_reorder = album_sub.add_parser('reorder', help='Reorder photos in album')
    album_reorder.add_argument('album_id', help='Album ID')
    album_reorder.add_argument('--by', choices=['date', 'date_desc', 'title'], default='date')

    album_merge = album_sub.add_parser('merge', help='Merge albums into one')
    album_merge.add_argument('--into', required=True, help='Target album ID or name')
    album_merge.add_argument('--create', action='store_true', help='Create target album if not exists (uses --into as name)')
    album_merge.add_argument('--from', dest='from_albums', nargs='+', help='Source album IDs or names')
    album_merge.add_argument('--pattern', '-p', help='Merge albums matching regex pattern')
    album_merge.add_argument('--delete-source', action='store_true', help='Delete source albums after merge')
    album_merge.add_argument('-y', '--yes', action='store_true', help='Skip confirmation')
    
    # Info
    info_parser = subparsers.add_parser('info', help='Show photo info')
    info_parser.add_argument('photo_id', help='Photo ID')
    
    # Fave
    fave_parser = subparsers.add_parser('fave', help='Manage favorites')
    fave_parser.add_argument('action', choices=['list', 'add', 'remove'], help='Action')
    fave_parser.add_argument('photo_id', nargs='?', help='Photo ID (for add/remove)')
    fave_parser.add_argument('-n', '--limit', type=int, default=50, help='Limit for list')
    
    # Comment
    comment_parser = subparsers.add_parser('comment', help='Manage comments')
    comment_parser.add_argument('photo_id', help='Photo ID')
    comment_parser.add_argument('action', choices=['list', 'add', 'delete'], help='Action')
    comment_parser.add_argument('text', nargs='?', help='Comment text (for add) or comment ID (for delete)')
    
    args = parser.parse_args()

    # Set config directory if specified
    if args.config:
        set_config_dir(args.config)

    # Set output level (check both global and subcommand options)
    quiet = getattr(args, 'quiet', False)
    verbose = getattr(args, 'verbose', False)
    progress = getattr(args, 'progress', False)

    if quiet:
        set_output_level(OutputLevel.QUIET, progress=False)
    elif verbose:
        set_output_level(OutputLevel.VERBOSE, progress=False)
    elif progress:
        set_output_level(OutputLevel.NORMAL, progress=True)
    else:
        set_output_level(OutputLevel.NORMAL, progress=False)

    if not args.command:
        parser.print_help()
        return

    # Auth command is interactive, others require existing auth
    if args.command == 'auth':
        authenticate(interactive=True)
        print("\n" + "="*50)
        user = get_user()
        print(f"✅ Logged in as: {user.username}")
        print(f"📁 Config: {CONFIG_FILE}")
        print(f"🔑 Token: {TOKEN_FILE}")
        print("="*50)
        return  # auth command done
    else:
        authenticate(interactive=False)
    
    if args.command == 'sync':
        sync_backup(
            output_dir=args.output,
            full=args.full,
            download_photos=not args.meta_only,
            max_workers=args.workers,
            limit=args.limit,
            skip_albums=args.skip_albums,
            album_limit=args.album_limit,
            order=args.order,
            from_date=args.from_date,
            to_date=args.to_date,
            request_delay=args.delay,
            wait_minutes=args.wait
        )
    
    elif args.command == 'download':
        if args.photo:
            download_albums_by_photo(
                photo_id=args.photo,
                output_dir=args.output,
                limit=args.limit,
                size=args.size,
                meta_only=args.meta_only
            )
        elif args.album:
            download_album(
                album_id=args.album,
                output_dir=args.output,
                limit=args.limit,
                size=args.size,
                meta_only=args.meta_only
            )
        else:
            print("❌ Specify album ID/URL or --photo <photo_id>")
    
    elif args.command == 'upload':
        upload_photos(
            path=args.path,
            album_name=args.album,
            tags=args.tags or '',
            privacy=args.privacy,
            title=args.title,
            description=args.description,
            limit=args.limit,
            dry_run=args.dry_run
        )
    
    elif args.command == 'tag':
        manage_tags(args.photo_id, args.action, args.tags)
    
    elif args.command == 'search':
        search_photos(
            user=args.user,
            tags=args.tags,
            text=args.text,
            date=args.date,
            limit=args.limit,
            download=args.download,
            output_dir=args.output
        )
    
    elif args.command == 'stats':
        show_stats(args.output)
    
    elif args.command == 'list':
        list_albums_for_user(
            username=getattr(args, 'user', None),
            pattern=getattr(args, 'pattern', None),
            empty=getattr(args, 'empty', False),
            max_photos=getattr(args, 'max_photos', None),
            min_photos=getattr(args, 'min_photos', None)
        )
    
    elif args.command == 'browse':
        if args.by == 'album':
            browse_by_album()
        else:
            browse_by_date(limit=args.limit, year=args.year, month=args.month)
    
    elif args.command == 'serve':
        serve_gallery(port=args.port)
    
    elif args.command == 'delete':
        if args.type == 'photo':
            delete_photos(args.ids, confirm=not args.yes)
        else:
            for aid in args.ids:
                delete_album(aid, keep_photos=args.keep_photos, confirm=not args.yes)
    
    elif args.command == 'replace':
        replace_photo(args.photo_id, args.file)
    
    elif args.command == 'rename':
        if args.type == 'photo':
            rename_photo(args.id, new_title=args.title, new_description=args.description)
        else:
            rename_album(args.id, new_title=args.title, new_description=args.description)
    
    elif args.command == 'privacy':
        set_privacy(args.photo_ids, args.set)
    
    elif args.command == 'move':
        move_photo(args.photo_id, args.to, args.from_album)
    
    elif args.command == 'copy':
        copy_photo(args.photo_id, args.to)
    
    elif args.command == 'album':
        if args.album_action == 'create':
            create_album(args.title, args.description, args.photo)
        elif args.album_action == 'delete':
            # Check if using pattern/batch mode or single album
            if args.pattern or args.empty or args.max_photos:
                delete_albums_batch(
                    pattern=args.pattern,
                    empty=args.empty,
                    max_photos=args.max_photos,
                    confirm=not args.yes
                )
            elif args.album_id:
                delete_album(args.album_id, confirm=not args.yes)
            else:
                print("Usage: flickr album delete <album_id> or --pattern <regex>")
        elif args.album_action == 'reorder':
            reorder_album(args.album_id, order_by=args.by)
        elif args.album_action == 'merge':
            merge_albums(
                target_id=args.into,
                source_ids=args.from_albums,
                pattern=args.pattern,
                delete_source=args.delete_source,
                confirm=not args.yes,
                create_target=args.create
            )
        else:
            print("Usage: flickr album {create|delete|reorder|merge}")
    
    elif args.command == 'info':
        show_photo_info(args.photo_id)
    
    elif args.command == 'fave':
        if args.action == 'list':
            list_faves(args.limit)
        elif args.action == 'add':
            if args.photo_id:
                add_fave(args.photo_id)
            else:
                print("❌ Photo ID required")
        elif args.action == 'remove':
            if args.photo_id:
                remove_fave(args.photo_id)
            else:
                print("❌ Photo ID required")
    
    elif args.command == 'comment':
        if args.action == 'list':
            list_comments(args.photo_id)
        elif args.action == 'add':
            if args.text:
                add_comment(args.photo_id, args.text)
            else:
                print("❌ Comment text required")
        elif args.action == 'delete':
            if args.text:
                delete_comment(args.text)  # text is actually comment_id here
            else:
                print("❌ Comment ID required")


if __name__ == '__main__':
    main()
