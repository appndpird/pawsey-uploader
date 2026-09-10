"""
Pawsey Data Management App (PDMA)
A cross-platform (Windows / Ubuntu) GUI for managing rclone transfers to
Pawsey Acacia object storage.

(Formerly "Pawsey Uploader". The on-disk config/log folder ~/.pawsey_uploader
is intentionally kept unchanged so existing projects and history carry over.)

Features
--------
- Configure / edit the rclone Pawsey remote (access key, secret, endpoint)
- Browse to a source folder (defaults to the app's own directory)
- Mandatory transfer-notes field that is written to a permanent log file
- Normal Copy with live progress
- Mirror Sync (exact replica - extra confirmation required)
- Resume Last Transfer (re-runs the previous copy; rclone skips done files)
- List, create, and delete buckets (delete needs typed-name confirmation)
- Storage usage visualisation (bar chart, dependency-free Canvas)
- Editable default project / bucket / source folder
- In-app Help tab

Dependencies: Python 3.9+ and rclone on PATH. No third-party Python packages.
Run with:  python pawsey_uploader.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import queue
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import traceback
import getpass
import urllib.parse
import urllib.request
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

try:
    # Embedded brand logos (base64 PNG). Optional: the app still runs if the
    # module is missing (the header just omits the logos).
    import logo_data
except Exception:  # pragma: no cover - logos are cosmetic
    logo_data = None

# ---------------------------------------------------------------------------
# Constants & defaults
# ---------------------------------------------------------------------------

APP_NAME = "Pawsey Data Management App (PDMA)"
APP_VERSION = "2.4"
APP_TAGLINE = "DPIRD · APPN  |  Pawsey Acacia object storage manager"

# ---------------------------------------------------------------------------
# Light "DPIRD teal" theme palette
# ---------------------------------------------------------------------------
THEME = {
    "bg":          "#F4F6F8",   # window / tab background (very light grey)
    "surface":     "#FFFFFF",   # cards, header, entries
    "surface_alt": "#EDF1F4",   # subtle panel / hover
    "border":      "#D5DCE2",   # hairline borders
    "text":        "#1F2A33",   # primary text (dark slate)
    "text_muted":  "#5B6B78",   # secondary text
    "accent":      "#1A6B8A",   # DPIRD teal/blue - primary actions, active tab
    "accent_dark": "#13556E",   # pressed / hover-darker
    "accent_soft": "#E1EEF3",   # tinted selection / accent wash
    "danger":      "#B22222",   # destructive actions
    "danger_dark": "#8E1B1B",
    "success":     "#2E7D32",   # ok / completed
    "warning":     "#B26B00",
}

APP_DIR = Path.home() / ".pawsey_uploader"
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "transfer_log.jsonl"
LAST_TRANSFER_FILE = APP_DIR / "last_transfer.json"   # legacy; migrated
RESUME_FILE = APP_DIR / "resume.json"                  # new: multi-slot resume
HEARTBEAT_FILE = APP_DIR / "heartbeat.json"            # liveness for external tools
TRANSFER_LOG_DIR = APP_DIR / "logs"                    # per-transfer verbose logs
BISYNC_STATE_FILE = APP_DIR / "bisync_pairs.json"      # which pairs are initialised
SEND_JOBS_DIR = APP_DIR / "send_jobs"                  # detached project->project copy jobs
# v2.3.1: diagnostics. The .exe has no console, so until now a failing rclone
# call or a crashed UI callback left no trace anywhere - "it can't upload" was
# impossible to investigate after the fact. Every rclone call that exits
# non-zero, every uncaught exception, and each app start is appended here.
DIAG_LOG = APP_DIR / "app_errors.log"
_DIAG_MAX_BYTES = 5 * 1024 * 1024


def _diag(msg: str) -> None:
    """Append one timestamped line to the diagnostics log. Never raises."""
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if DIAG_LOG.exists() and DIAG_LOG.stat().st_size > _DIAG_MAX_BYTES:
            DIAG_LOG.replace(DIAG_LOG.with_suffix(".log.1"))
        with open(DIAG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass

# Soft-delete recycle bin. When enabled, files that a sync would delete or
# overwrite, and items deleted from the Storage tab, are moved into this
# prefix (timestamped) inside the SAME bucket instead of being purged.
# Because rclone's --backup-dir also catches overwritten files, this prefix
# doubles as version history (previous versions land here, dated).
RECYCLE_PREFIX = "_recycle_bin"

# Prefixes the app creates and manages inside a bucket. They are deliberately
# NOT part of the user's dataset and have no local counterpart, so any
# comparison of the two sides has to ignore them - otherwise the recycle bin
# alone shows up as "thousands of files/folders only on Pawsey" and a perfectly
# healthy pair looks like a mismatch.
APP_MANAGED_PREFIXES = (RECYCLE_PREFIX, "_backups", "_shares")

IS_WINDOWS = platform.system() == "Windows"

# UI buffer limits - bounded so the app doesn't accumulate GBs of strings
# in memory over week-long runs. The full verbose stream still goes to disk.
MAX_OUTPUT_LINES = 10_000
TRIM_OUTPUT_TO_LINES = 8_000

# Auto-restart on rclone crash - keeps unattended transfers alive through
# transient network failures. Aborted on user stop or auth failure.
DEFAULT_AUTO_RESTART = True
MAX_RESTART_ATTEMPTS = 10
RESTART_BACKOFF_SECONDS = 30  # delay between attempts

# Heartbeat interval (ms) - external scripts can poll HEARTBEAT_FILE
HEARTBEAT_INTERVAL_MS = 15_000

# Substrings in rclone output that indicate a non-recoverable auth problem.
# When seen, we stop the transfer and do NOT auto-restart.
AUTH_FAILURE_PATTERNS = (
    "UserSuspended",
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "AccessDenied",
    "Access Denied",
    "403 Forbidden",
    "401 Unauthorized",
)

# Errors where retrying the SAME command is futile - the user must change
# something first (a wrong bucket/path, or a bisync that aborted and now
# needs a --resync). Auto-restart is suppressed when these appear.
FATAL_NONRETRYABLE_PATTERNS = (
    "directory not found",
    "bucket does not exist",
    "NoSuchBucket",
    "Bisync critical error",
    "Bisync aborted",
    "Must run --resync to recover",
    "cannot find prior Path1 or Path2 listings",
    "too many deletes",
    "Safety abort",
)

# Two-way sync refuses to run until rclone has a "baseline" (its record of
# what both sides looked like after the last successful run). These strings
# mean exactly that - the fix is a --resync, not a retry.
BISYNC_NEEDS_RESYNC_PATTERNS = (
    "must run --resync",
    "cannot find prior path1 or path2 listings",
    "prior listing file not found",
    "--resync is required",
)

# Flags that make rclone carry EMPTY folders across, in both directions.
#   --create-empty-src-dirs  replicate (and, for mirror sync, remove) empty
#                            directories at the destination instead of
#                            silently ignoring them.
#   --s3-directory-markers   on object storage a "folder" does not exist as a
#                            real thing; it is implied by the keys under it.
#                            An EMPTY folder therefore has nothing to imply it
#                            and vanishes unless rclone writes a zero-byte
#                            marker object named "<folder>/". Without this
#                            flag --create-empty-src-dirs has no visible
#                            effect against Pawsey.
# Both are needed together; the S3 flag is ignored for local-only paths.
S3_MARKER_FLAG = "--s3-directory-markers"
EMPTY_DIR_FLAGS = ("--create-empty-src-dirs", S3_MARKER_FLAG)

# Once folder markers exist, every operation that DELETES or MOVES a folder
# has to know about them too. Without the flag rclone cannot see a marker,
# so purging a folder whose only content is its own marker fails outright
# ("Object name contains unsupported characters" - it tries to delete "/")
# and the folder stays behind as a ghost. Delete/move commands take the
# marker flag alone; --create-empty-src-dirs is a copy-side flag.
S3_DELETE_FLAGS = (S3_MARKER_FLAG,)

# v2.4: flags for every `rclone lsjson` that feeds the Storage browser.
# On S3 the original modification time and the MIME type are NOT part of a
# bucket listing - they live in per-object metadata, so plain `lsjson` issues
# one HEAD request PER OBJECT to fill them in. A folder of 8,400 images took
# 4.5 minutes to list that way (measured against Pawsey), which blew through
# the browser's 2-minute limit: the folder then showed up EMPTY with a
# "Failed to list … Command timed out" error, i.e. "the files are not there".
# With these two flags the same folder lists in 2 seconds (10 requests).
#   --use-server-modtime  show the object's upload time instead of the file's
#                         original mtime (no HEAD needed; the browser only
#                         displays it, nothing compares it)
#   --no-mimetype         the browser never shows the MIME type anyway
FAST_LIST_FLAGS = ("--no-mimetype", "--use-server-modtime")

# Default app config (merged with on-disk config on load)
DEFAULT_CONFIG = {
    "remote_name": "pawsey",
    "access_key_id": "",
    "secret_access_key": "",
    "endpoint": "https://projects.pawsey.org.au",
    "provider": "Ceph",
    # Multi-project support: each saved project is one rclone S3 remote.
    # projects = { "<remote_name>": {endpoint, access_key_id,
    #              secret_access_key, provider, label} }
    # active_project names the one used as the app-wide default.
    "projects": {},
    "active_project": "",
    "default_project": "",
    "default_bucket": "sample-data",
    "default_source": "",
    "rclone_path": "",
    "s3_chunk_size": "64M",
    "s3_upload_concurrency": 4,
    "transfers": 1,
    "checkers": 16,
    "retries": 5,
    "low_level_retries": 10,
    "auto_restart": DEFAULT_AUTO_RESTART,
    "max_restart_attempts": MAX_RESTART_ATTEMPTS,
    "restart_backoff_seconds": RESTART_BACKOFF_SECONDS,
    "bwlimit": "",
    "autosync_interval_min": 15,
    "use_recycle_bin": True,
    "limit_deletes": True,
    "max_delete_percent": 50,
    # --- v2.1: safety defaults + password protection ---
    # SHA-256 hash of the app password. Empty string => the default
    # password ("appn") is in force until the user changes it.
    "app_password_hash": "",
    # These safe options start ON and can only be turned OFF (or a
    # destructive mode selected) after entering the app password.
    "default_preview_only": True,
    "default_verify_both": True,
}

# Factory-default password. Used until the user sets their own in Settings.
DEFAULT_APP_PASSWORD = "appn"

# Regex to parse rclone --progress lines
PROGRESS_RE = re.compile(
    r"Transferred:\s+([\d.]+\s*\w+)\s*/\s*([\d.]+\s*\w+),\s*(\d+)%"
)

# Log-level prefix of an rclone message line, e.g.
#   2026/07/29 10:22:33 INFO  : sub/a.txt: Copied (new)
#   2026/07/29 10:22:33 NOTICE: sub/a.txt: Skipped copy as --dry-run is set
# Matched with a regex (rather than str.split) so a file whose NAME contains
# "INFO" can't be mistaken for the level marker.
LOG_LEVEL_RE = re.compile(r"\b(?:INFO|NOTICE)\s*:\s*")

# rclone colourises some log lines (e.g. "\x1b[32mBisync successful\x1b[0m")
# and does so even when its output is a pipe rather than a terminal. Left in
# place, a trailing reset code breaks exact matches like line.endswith(
# ": Deleted"), so strip the escape sequences before classifying a line.
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _app_prefix_excludes() -> list[str]:
    """rclone --exclude args that hide the app's own bucket prefixes.
    Both forms are needed: "p/**" drops the contents, "p/" drops the folder."""
    args: list[str] = []
    for p in APP_MANAGED_PREFIXES:
        args += ["--exclude", f"{p}/**", "--exclude", f"{p}/"]
    return args


def _new_change_counts() -> dict:
    """Fresh per-run change tally. 'dirs' counts folder creations/removals,
    which is how an empty-folder-only change becomes visible."""
    return {"new": 0, "updated": 0, "deleted": 0, "renamed": 0, "dirs": 0}


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _subprocess_kwargs() -> dict:
    """Cross-platform kwargs for Popen so we can stop the process cleanly
    and so no extra console window pops up on Windows."""
    kw = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if IS_WINDOWS:
        kw["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        kw["start_new_session"] = True
    return kw


def stop_process(proc: Optional[subprocess.Popen]) -> None:
    """Stop an rclone subprocess cleanly, cross-platform."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _pid_alive(pid: Optional[int]) -> bool:
    """Best-effort check whether a process id is still running."""
    if not pid:
        return False
    try:
        if IS_WINDOWS:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW)
            return str(int(pid)) in (out.stdout or "")
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def run_rclone_capture(args: list[str], timeout: int = 60) -> tuple[int, str]:
    """Run rclone and return (returncode, combined output). Synchronous."""
    try:
        result = subprocess.run(
            [RCLONE_EXE, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=(subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0),
        )
        out = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            _diag(f"rclone rc={result.returncode}: "
                  f"{' '.join(str(a) for a in args[:3])} | "
                  f"{out.strip()[-600:]}")
        return result.returncode, out
    except FileNotFoundError:
        _diag(f"rclone NOT FOUND at {RCLONE_EXE!r} for: "
              f"{' '.join(str(a) for a in args[:3])}")
        return 127, "rclone executable not found"
    except subprocess.TimeoutExpired:
        _diag(f"rclone TIMEOUT after {timeout}s: "
              f"{' '.join(str(a) for a in args[:3])}")
        return 124, "Command timed out"
    except Exception as e:
        _diag(f"rclone call error: {e!r} for: "
              f"{' '.join(str(a) for a in args[:3])}")
        return 1, f"Error: {e}"


# ---------------------------------------------------------------------------
# rclone executable discovery
# ---------------------------------------------------------------------------

# Path (or bare name) used for every rclone invocation. Resolved at startup.
RCLONE_EXE: str = "rclone"


def _app_dir() -> Path:
    """Directory containing the running script or frozen .exe."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_rclone_executable(configured: str = "") -> Optional[str]:
    """
    Locate an rclone executable. Order:
      1. Explicit path saved in app config (if it exists)
      2. `rclone` on PATH
      3. Same folder as this script / the frozen .exe
      4. Common Anaconda/Miniconda env layouts on this OS
    Returns an absolute path, or None.
    """
    import shutil

    # 1. Configured path
    if configured:
        p = Path(configured)
        if p.is_file():
            return str(p)

    # 2. PATH
    found = shutil.which("rclone")
    if found:
        return found

    exe_name = "rclone.exe" if IS_WINDOWS else "rclone"

    # 3. Next to the script / .exe (lets users drop rclone.exe in beside us)
    sibling = _app_dir() / exe_name
    if sibling.is_file():
        return str(sibling)

    # 4. Common conda installation roots
    home = Path.home()
    candidate_roots: list[Path] = []
    if IS_WINDOWS:
        candidate_roots = [
            home / "AppData" / "Local" / "miniconda3",
            home / "AppData" / "Local" / "anaconda3",
            home / "AppData" / "Local" / "miniforge3",
            home / "miniconda3",
            home / "anaconda3",
            Path("C:/ProgramData/miniconda3"),
            Path("C:/ProgramData/Anaconda3"),
        ]
        scripts_subdir = "Scripts"
    else:
        candidate_roots = [
            home / "miniconda3",
            home / "anaconda3",
            home / "miniforge3",
            Path("/opt/miniconda3"),
            Path("/opt/anaconda3"),
        ]
        scripts_subdir = "bin"

    for root in candidate_roots:
        if not root.is_dir():
            continue
        # Base install
        p = root / scripts_subdir / exe_name
        if p.is_file():
            return str(p)
        # envs/*/Scripts (or bin)/rclone
        envs = root / "envs"
        if envs.is_dir():
            try:
                for env in envs.iterdir():
                    p = env / scripts_subdir / exe_name
                    if p.is_file():
                        return str(p)
            except OSError:
                pass

    # 5. Standalone install locations
    if IS_WINDOWS:
        for p in (
            Path("C:/Program Files/rclone/rclone.exe"),
            Path("C:/rclone/rclone.exe"),
        ):
            if p.is_file():
                return str(p)
    else:
        for p in (Path("/usr/local/bin/rclone"), Path("/usr/bin/rclone")):
            if p.is_file():
                return str(p)

    return None


def human_bytes(n: float) -> str:
    """Format a byte count nicely."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} EiB"


# ---------------------------------------------------------------------------
# S3 presigned-URL generation (AWS Signature V4) - standard library only.
#
# Pawsey Acacia is a Ceph S3 backend; rclone's `link` command does NOT support
# expiring public links on S3. The S3-native equivalent is a *presigned URL*:
# a normal HTTPS link that embeds a time-limited signature, after which it
# stops working. We build it here with hmac/hashlib so the app keeps its
# "no third-party packages" promise.
# ---------------------------------------------------------------------------

# Max lifetime SigV4 allows for a presigned URL.
PRESIGN_MAX_SECONDS = 7 * 24 * 3600  # 7 days


def _sigv4_uri_encode(value: str, encode_slash: bool) -> str:
    """AWS-style percent-encoding (unreserved chars stay literal)."""
    safe = "-_.~"
    if not encode_slash:
        safe += "/"
    return urllib.parse.quote(value, safe=safe)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def s3_presign_url(endpoint: str, region: str, access_key: str,
                   secret_key: str, bucket: str, key: str,
                   expires: int, *, now: Optional[datetime] = None,
                   download_name: Optional[str] = None,
                   content_type: Optional[str] = None) -> str:
    """Return a time-limited presigned GET URL for endpoint/bucket/key.

    Uses path-style addressing (endpoint/bucket/key), which is what Ceph/RGW
    and rclone's default Pawsey config use. `expires` is in seconds and is
    clamped to the SigV4 maximum of 7 days.

    If `download_name` is given, a signed `response-content-disposition` query
    param is added so the browser saves the object as an attachment with that
    filename (used for "download" links and folder bulk-downloads). Without it
    the link opens inline in the browser (used for "view" links).
    """
    expires = max(1, min(int(expires), PRESIGN_MAX_SECONDS))
    region = region or "us-east-1"
    service = "s3"
    endpoint = endpoint.rstrip("/")
    parsed = urllib.parse.urlsplit(endpoint)
    host = parsed.netloc
    scheme = parsed.scheme or "https"

    dt = now or datetime.now(timezone.utc)
    amzdate = dt.strftime("%Y%m%dT%H%M%SZ")
    datestamp = dt.strftime("%Y%m%d")

    # Canonical URI: path-style /bucket/key with each key segment encoded.
    key = key.lstrip("/")
    canonical_uri = "/" + _sigv4_uri_encode(bucket, True)
    if key:
        canonical_uri += "/" + _sigv4_uri_encode(key, False)

    credential_scope = f"{datestamp}/{region}/{service}/aws4_request"
    qs = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{credential_scope}",
        "X-Amz-Date": amzdate,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    if download_name:
        # Strip characters that don't belong in a Content-Disposition filename.
        safe_name = download_name.replace('"', "").replace("\\", "_")
        qs["response-content-disposition"] = (
            f'attachment; filename="{safe_name}"')
    if content_type:
        # Force how the browser interprets the object (e.g. render the share
        # page as text/html regardless of what was stored on upload).
        qs["response-content-type"] = content_type
    canonical_qs = "&".join(
        f"{_sigv4_uri_encode(k, True)}={_sigv4_uri_encode(v, True)}"
        for k, v in sorted(qs.items())
    )
    canonical_headers = f"host:{host}\n"
    signed_headers = "host"
    payload_hash = "UNSIGNED-PAYLOAD"
    canonical_request = "\n".join([
        "GET", canonical_uri, canonical_qs,
        canonical_headers, signed_headers, payload_hash,
    ])

    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amzdate, credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    return f"{scheme}://{host}{canonical_uri}?{canonical_qs}&X-Amz-Signature={signature}"


# ---------------------------------------------------------------------------
# Permanent (non-expiring) public links.
#
# A presigned URL (above) always expires — SigV4 caps its lifetime at 7 days,
# so it is perfect for temporary sharing but useless for a *published* dataset
# that must stay reachable forever. The S3-native way to get a permanent link
# is to make the object itself publicly readable (a "public-read" ACL) and then
# hand out the plain, unsigned object URL: https://endpoint/bucket/key . That
# link never expires and needs no signature, because anonymous GET is allowed
# on the object. We set the ACL with a normal SigV4-*authenticated* request
# (Authorization header), built here with the standard library so the app keeps
# its "no third-party packages" promise.
#
# Caveat: the Pawsey project/bucket must permit anonymous public reads. Some
# Acacia buckets have public access disabled at the gateway; in that case the
# ACL call (or the follow-up public read-back check) fails and the app tells
# the user to ask Pawsey support to enable public access for the bucket.
# ---------------------------------------------------------------------------


def s3_public_url(endpoint: str, bucket: str, key: str) -> str:
    """Plain, unsigned, path-style object URL — permanent, no signature.
    Only actually reachable once the object has a public-read ACL."""
    endpoint = endpoint.rstrip("/")
    parsed = urllib.parse.urlsplit(endpoint)
    host = parsed.netloc
    scheme = parsed.scheme or "https"
    key = key.lstrip("/")
    uri = "/" + _sigv4_uri_encode(bucket, True)
    if key:
        uri += "/" + _sigv4_uri_encode(key, False)
    return f"{scheme}://{host}{uri}"


def _sigv4_authorized_request(method: str, endpoint: str, region: str,
                              access_key: str, secret_key: str,
                              bucket: str, key: str, *,
                              query: str = "", body: bytes = b"",
                              extra_headers: Optional[dict] = None,
                              now: Optional[datetime] = None,
                              timeout: int = 60):
    """Issue a SigV4 header-authenticated S3 request (path-style) and return
    (http_status, response_bytes). Raises urllib.error.* on transport failure.

    `query` is the raw canonical query string (already sorted, e.g. "acl=").
    """
    region = region or "us-east-1"
    service = "s3"
    endpoint = endpoint.rstrip("/")
    parsed = urllib.parse.urlsplit(endpoint)
    host = parsed.netloc
    scheme = parsed.scheme or "https"

    dt = now or datetime.now(timezone.utc)
    amzdate = dt.strftime("%Y%m%dT%H%M%SZ")
    datestamp = dt.strftime("%Y%m%d")

    key = key.lstrip("/")
    canonical_uri = "/" + _sigv4_uri_encode(bucket, True)
    if key:
        canonical_uri += "/" + _sigv4_uri_encode(key, False)

    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amzdate,
    }
    for k, v in (extra_headers or {}).items():
        headers[k.lower()] = v

    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(
        f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical_request = "\n".join([
        method, canonical_uri, query,
        canonical_headers, signed_headers, payload_hash,
    ])

    credential_scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amzdate, credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}")

    url = f"{scheme}://{host}{canonical_uri}"
    if query:
        url += "?" + query
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        if k == "host":
            continue  # urllib sets Host itself
        req.add_header(k, v)
    req.add_header("Authorization", authorization)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def s3_set_public_read(endpoint: str, region: str, access_key: str,
                       secret_key: str, bucket: str, key: str) -> tuple:
    """Give a single object a public-read ACL so its plain URL works forever.
    Returns (ok: bool, detail: str)."""
    try:
        status, body = _sigv4_authorized_request(
            "PUT", endpoint, region, access_key, secret_key, bucket, key,
            query="acl=", extra_headers={"x-amz-acl": "public-read"})
    except Exception as e:  # network / TLS / DNS
        return False, f"request failed: {e}"
    if 200 <= status < 300:
        return True, "public-read"
    detail = (body or b"").decode("utf-8", "replace")[:400]
    return False, f"HTTP {status}: {detail}"


def s3_publish_object(endpoint: str, region: str, access_key: str,
                      secret_key: str, bucket: str, key: str,
                      download_name: str, content_type: str = None) -> tuple:
    """Publish an object for permanent PUBLIC DOWNLOAD.

    Does a server-side COPY of the object onto itself with
    `x-amz-metadata-directive: REPLACE`, which lets us set — in a single
    request, without re-uploading the data — a public-read ACL *and* a stored
    `Content-Disposition: attachment` header. The attachment header is what
    makes the plain, unsigned URL DOWNLOAD (instead of the browser navigating
    to / rendering the file), so the share page's "Download folder" / "Download
    everything" buttons work: each link downloads without unloading the page.

    Returns (ok: bool, detail: str)."""
    safe_name = (download_name or key.split("/")[-1]).replace('"', "").replace(
        "\\", "_")
    copy_source = "/" + _sigv4_uri_encode(bucket, True) + "/" + \
        _sigv4_uri_encode(key.lstrip("/"), False)
    headers = {
        "x-amz-copy-source": copy_source,
        "x-amz-metadata-directive": "REPLACE",
        "x-amz-acl": "public-read",
        "content-disposition": f'attachment; filename="{safe_name}"',
        "content-type": content_type or "application/octet-stream",
    }
    try:
        status, body = _sigv4_authorized_request(
            "PUT", endpoint, region, access_key, secret_key, bucket, key,
            extra_headers=headers)
    except Exception as e:
        return False, f"request failed: {e}"
    if 200 <= status < 300:
        return True, "published"
    detail = (body or b"").decode("utf-8", "replace")[:400]
    return False, f"HTTP {status}: {detail}"


def s3_url_is_public(url: str, timeout: int = 20) -> bool:
    """Anonymous GET (range 0-0) to confirm the object is world-readable."""
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Range", "bytes=0-0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Config manager
# ---------------------------------------------------------------------------

class ConfigManager:
    def __init__(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.data = DEFAULT_CONFIG.copy()
        self.load()

    def load(self) -> None:
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    on_disk = json.load(f)
                self.data.update(on_disk)
            except Exception as e:
                print(f"Could not read config: {e}", file=sys.stderr)
        # Default source = directory the app is in, if not set
        if not self.data.get("default_source"):
            self.data["default_source"] = str(Path(__file__).resolve().parent)

    def save(self) -> None:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value

    # ----- password helpers (v2.1) -------------------------------------
    @staticmethod
    def _hash_password(pw: str) -> str:
        return hashlib.sha256(("pdma::" + (pw or "")).encode("utf-8")).hexdigest()

    def check_password(self, pw: str) -> bool:
        """True if `pw` matches the stored password (or the factory default
        'appn' when the user has never set one)."""
        stored = self.get("app_password_hash", "") or ""
        if not stored:
            stored = self._hash_password(DEFAULT_APP_PASSWORD)
        return hmac.compare_digest(stored, self._hash_password(pw))

    def set_password(self, pw: str) -> None:
        self.set("app_password_hash", self._hash_password(pw))
        self.save()

    def is_default_password(self) -> bool:
        """True while the app is still using the factory default password."""
        return not (self.get("app_password_hash", "") or "")


# ---------------------------------------------------------------------------
# rclone remote configuration
# ---------------------------------------------------------------------------

class RcloneRemote:
    """Wrapper around the `rclone config` commands for an S3/Ceph remote."""

    @staticmethod
    def list_remotes() -> list[str]:
        rc, out = run_rclone_capture(["listremotes"])
        if rc != 0:
            return []
        return [line.rstrip(":") for line in out.splitlines() if line.strip()]

    @staticmethod
    def upsert_remote(name: str, endpoint: str, access: str, secret: str,
                      provider: str = "Ceph") -> tuple[bool, str]:
        """Create or update an S3 remote (one Pawsey project) in rclone.conf."""
        name = (name or "").strip()
        if not (access and secret and endpoint and name):
            return False, "Project name, endpoint, access key, and secret are all required."
        if not name.replace("_", "").replace("-", "").isalnum():
            return False, ("Project (remote) name can only contain letters, "
                           "numbers, '-' and '_' (no spaces).")

        existing = RcloneRemote.list_remotes()
        is_update = name in existing

        # rclone CLI grammar differs between create and update:
        #   rclone config create NAME TYPE [key value]+
        #   rclone config update NAME [key value]+         (no TYPE)
        # Passing TYPE to update yields "found key without value".
        args = ["config", "update" if is_update else "create", name]
        if not is_update:
            args.append("s3")                  # backend type, create only
        args.extend([
            "provider",          provider or "Ceph",
            "endpoint",          endpoint,
            "access_key_id",     access,
            "secret_access_key", secret,
            "--non-interactive",               # never block waiting on stdin
        ])

        rc, out = run_rclone_capture(args, timeout=30)
        if rc != 0:
            verb = "update" if is_update else "create"
            return False, f"rclone {verb} failed:\n{out}"
        verb = "updated" if is_update else "created"
        return True, f"Project '{name}' {verb} successfully."

    @staticmethod
    def delete_remote(name: str) -> tuple[bool, str]:
        """Remove a remote from rclone.conf."""
        rc, out = run_rclone_capture(
            ["config", "delete", name], timeout=30)
        if rc != 0:
            return False, out
        return True, f"Removed '{name}'."

    @staticmethod
    def upsert_pawsey_remote(cfg: ConfigManager) -> tuple[bool, str]:
        """Create or update the active Pawsey remote based on app config."""
        return RcloneRemote.upsert_remote(
            cfg.get("remote_name", "pawsey"), cfg.get("endpoint"),
            cfg.get("access_key_id"), cfg.get("secret_access_key"),
            cfg.get("provider", "Ceph"))

    @staticmethod
    def show(name: str) -> str:
        rc, out = run_rclone_capture(["config", "show", name])
        return out

    @staticmethod
    def test_remote(name: str) -> tuple[bool, str]:
        """Quick connectivity test - list buckets."""
        rc, out = run_rclone_capture(["lsd", f"{name}:"], timeout=30)
        return (rc == 0), out


# ---------------------------------------------------------------------------
# Resume store
# ---------------------------------------------------------------------------
#
# Keeps a rolling list of transfers (in-progress, failed, completed) so the
# user can resume any of the recent unfinished ones, not just the last one.
#
# File: ~/.pawsey_uploader/resume.json   ->   {"transfers": [ {...}, ... ]}
#
# Each entry:
#   id              "20260517T191300"      -- timestamp-based unique id
#   started_at      ISO timestamp
#   last_updated    ISO timestamp
#   src             absolute source path
#   dest            "remote:bucket/path"
#   mode            "copy" | "sync"
#   notes           user's data description
#   status          "in_progress" | "completed" | "failed" | "stopped"
#   last_progress   last progress line seen
#   restart_count   total auto-restart attempts so far
# ---------------------------------------------------------------------------

class ResumeStore:
    @staticmethod
    def _load_raw() -> dict:
        if RESUME_FILE.exists():
            try:
                return json.loads(RESUME_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"transfers": []}

    @staticmethod
    def _save_raw(data: dict) -> None:
        try:
            RESUME_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"Resume save failed: {e}", file=sys.stderr)

    @staticmethod
    def list_all() -> list[dict]:
        return ResumeStore._load_raw().get("transfers", [])

    @staticmethod
    def list_incomplete() -> list[dict]:
        # Project->project copies are managed by the Storage "Background
        # copies…" manager, not the Transfer-tab resume picker.
        return [t for t in ResumeStore.list_all()
                if t.get("status") not in ("completed",)
                and t.get("kind") != "project_copy"]

    @staticmethod
    def add(entry: dict) -> str:
        data = ResumeStore._load_raw()
        data.setdefault("transfers", []).insert(0, entry)
        # Trim to the most recent 50 entries to bound file size
        data["transfers"] = data["transfers"][:50]
        ResumeStore._save_raw(data)
        return entry["id"]

    @staticmethod
    def update(entry_id: str, **changes) -> None:
        if not entry_id:
            return
        data = ResumeStore._load_raw()
        for t in data.get("transfers", []):
            if t.get("id") == entry_id:
                t.update(changes)
                t["last_updated"] = datetime.now().isoformat(timespec="seconds")
                break
        ResumeStore._save_raw(data)

    @staticmethod
    def get(entry_id: str) -> Optional[dict]:
        for t in ResumeStore.list_all():
            if t.get("id") == entry_id:
                return t
        return None

    @staticmethod
    def remove(entry_id: str) -> None:
        if not entry_id:
            return
        data = ResumeStore._load_raw()
        data["transfers"] = [t for t in data.get("transfers", [])
                             if t.get("id") != entry_id]
        ResumeStore._save_raw(data)

    @staticmethod
    def migrate_legacy() -> None:
        """One-shot migration from the old single-slot file."""
        if not LAST_TRANSFER_FILE.exists() or RESUME_FILE.exists():
            return
        try:
            old = json.loads(LAST_TRANSFER_FILE.read_text(encoding="utf-8"))
            entry = {
                "id": datetime.now().strftime("%Y%m%dT%H%M%S") + "_legacy",
                "started_at": old.get("started_at",
                                      datetime.now().isoformat(timespec="seconds")),
                "src": old.get("src", ""),
                "dest": old.get("dest", ""),
                "mode": old.get("mode", "copy"),
                "notes": old.get("notes", ""),
                "status": "stopped",
                "restart_count": 0,
            }
            ResumeStore.add(entry)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Sleep prevention
# ---------------------------------------------------------------------------
#
# While a transfer is active, we ask the OS to keep the system awake.
# Windows: SetThreadExecutionState - the standard mechanism used by
# installers, downloaders, media players, etc. Released when the transfer
# ends OR when the app closes.
#
# Linux: no portable equivalent in the standard library. Servers typically
# don't sleep; desktop users can run `systemd-inhibit` or `caffeine` as a
# fallback. We log a hint if we can't prevent sleep.
# ---------------------------------------------------------------------------

_SLEEP_LOCK_ACTIVE = False

def prevent_sleep(enable: bool) -> bool:
    """Toggle OS sleep prevention. Returns True if the call had effect."""
    global _SLEEP_LOCK_ACTIVE
    if IS_WINDOWS:
        try:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_AWAYMODE_REQUIRED = 0x00000040  # works while logged in too
            kernel32 = ctypes.windll.kernel32
            if enable:
                flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
                result = kernel32.SetThreadExecutionState(flags)
                _SLEEP_LOCK_ACTIVE = bool(result)
                return _SLEEP_LOCK_ACTIVE
            else:
                kernel32.SetThreadExecutionState(ES_CONTINUOUS)
                _SLEEP_LOCK_ACTIVE = False
                return True
        except Exception as e:
            print(f"prevent_sleep error: {e}", file=sys.stderr)
            return False
    # Non-Windows: best effort - just track the intent
    _SLEEP_LOCK_ACTIVE = enable
    return False


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class PawseyApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.cfg = ConfigManager()
        self._migrate_projects()
        self.proc: Optional[subprocess.Popen] = None
        self.proc_queue: queue.Queue[str] = queue.Queue()
        self.proc_thread: Optional[threading.Thread] = None
        self.transfer_active = False
        # Count of foreground (in-app) project->project copies in flight
        self._send_fg_active = 0
        # Background-call result queue (UI updates after rclone tasks)
        self._bg_queue: queue.Queue = queue.Queue()

        # Long-running-transfer state
        self.current_resume_id: Optional[str] = None
        self.current_transfer_cmd: Optional[list[str]] = None
        self.current_src: Optional[str] = None
        self.current_dest: Optional[str] = None
        self.current_mode: Optional[str] = None
        self.user_stopped = False           # user clicked Stop
        self.auth_failure_seen = False      # set by output sniffer
        self.fatal_error_seen = False       # non-retryable error seen
        self.delete_cap_seen = False        # bisync hit the >50% delete cap
        self.empty_listing_seen = False     # bisync saw an empty Path2 listing
        self.bisync_needs_resync_seen = False   # bisync baseline missing/stale
        self.restart_count = 0
        self.restart_after_id: Optional[str] = None
        self._heartbeat_after_id: Optional[str] = None
        self._last_progress_text = ""
        self._transfer_log_fh = None        # per-transfer disk log file
        # Change tracking for the current transfer
        self._change_counts = _new_change_counts()
        self._change_details: list[str] = []
        self._rename_sources: set[str] = set()
        self._backup_dir_active = False     # set per run in _launch_rclone
        # Scheduled auto-sync
        self._autosync_after_id: Optional[str] = None
        self._autosync_params: Optional[dict] = None

        # Migrate any legacy last_transfer.json into the new resume store
        ResumeStore.migrate_legacy()
        TRANSFER_LOG_DIR.mkdir(parents=True, exist_ok=True)
        SEND_JOBS_DIR.mkdir(parents=True, exist_ok=True)

        # Resolve rclone executable BEFORE any rclone calls happen.
        # This also writes the discovered path back into the config so
        # the next launch is instant.
        self._resolve_rclone_executable(prompt_if_missing=True)

        # v2.3.1: uncaught exceptions in Tk callbacks and worker threads
        # used to disappear (no console). Log them and tell the user.
        self._tk_error_dialogs = 0
        root.report_callback_exception = self._on_uncaught_exception
        threading.excepthook = lambda a: _diag(
            "thread crash: " + "".join(traceback.format_exception(
                a.exc_type, a.exc_value, a.exc_traceback)))
        try:
            who = getpass.getuser()
        except Exception:
            who = "?"
        ver_rc, ver_out = run_rclone_capture(["version"], timeout=20)
        _diag(f"=== start {APP_NAME} v{APP_VERSION} user={who} "
              f"exe={sys.executable} rclone={RCLONE_EXE!r} "
              f"({(ver_out.splitlines() or ['?'])[0].strip() if ver_rc == 0 else 'version check failed'}) "
              f"config={CONFIG_FILE}")

        root.title(f"{APP_NAME} v{APP_VERSION}")
        root.geometry("1120x800")
        root.minsize(940, 660)
        self._logo_images = {}   # keep PhotoImage refs alive

        self._apply_theme()
        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Periodically drain subprocess output
        root.after(100, self._drain_output)
        # Periodically drain background-task results (Storage tab, etc.)
        root.after(120, self._drain_bg_queue)
        # Reconcile detached project->project copy jobs once the UI is up
        root.after(500, self._reconcile_project_copies)

    def _on_uncaught_exception(self, exc_type, exc_value, tb) -> None:
        """Tk callback crashed. Log the traceback and show it (first few)."""
        text = "".join(traceback.format_exception(exc_type, exc_value, tb))
        _diag("UI callback crash:\n" + text)
        self._tk_error_dialogs += 1
        if self._tk_error_dialogs <= 3:
            try:
                messagebox.showerror(
                    APP_NAME,
                    "An internal error occurred. The operation did not "
                    "complete.\n\n"
                    f"{exc_type.__name__}: {exc_value}\n\n"
                    f"Details were written to:\n{DIAG_LOG}\n"
                    "Please send that file to the app maintainer.")
            except Exception:
                pass

    # ----------------------------------------------------- rclone discovery
    def _resolve_rclone_executable(self, prompt_if_missing: bool = True) -> bool:
        """
        Resolve and store the rclone executable path. Updates the module
        global RCLONE_EXE and the saved config.

        If nothing is found and prompt_if_missing is True, asks the user
        to pick rclone.exe with a file dialog. Returns True if rclone is
        usable after this call, False otherwise.
        """
        global RCLONE_EXE
        configured = self.cfg.get("rclone_path", "") or ""
        found = find_rclone_executable(configured)
        if found:
            RCLONE_EXE = found
            self.cfg.set("rclone_path", found)
            self.cfg.save()
            return True

        if not prompt_if_missing:
            return False

        # Tell the user and offer to locate it manually.
        ok = messagebox.askyesno(
            APP_NAME,
            "rclone could not be found automatically.\n\n"
            "The app needs rclone to talk to Pawsey. You can either:\n"
            "  - Install rclone and add it to PATH, then restart the app, or\n"
            "  - Click 'Yes' now to locate rclone.exe manually.\n\n"
            "Locate rclone.exe now?",
        )
        if not ok:
            return False

        filetypes = [("rclone executable", "rclone.exe rclone"), ("All files", "*.*")]
        path = filedialog.askopenfilename(
            title="Locate rclone executable",
            filetypes=filetypes,
        )
        if not path:
            return False
        RCLONE_EXE = path
        self.cfg.set("rclone_path", path)
        self.cfg.save()
        messagebox.showinfo(APP_NAME, f"Using rclone at:\n{path}")
        return True

    # --------------------------------------------------------------- theming
    def _apply_theme(self) -> None:
        """Apply the light DPIRD-teal theme to the root window and all ttk
        widgets. Built on the 'clam' base theme (the only stock ttk theme that
        honours custom colours consistently across Windows and Linux)."""
        T = THEME
        self.root.configure(bg=T["bg"])
        try:
            self.root.option_add("*Font", ("Segoe UI", 10))
        except Exception:
            pass

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        base_font = ("Segoe UI", 10)
        bold_font = ("Segoe UI", 10, "bold")

        # --- Frames / labels -------------------------------------------------
        style.configure(".", background=T["bg"], foreground=T["text"],
                        fieldbackground=T["surface"], font=base_font,
                        bordercolor=T["border"])
        style.configure("TFrame", background=T["bg"])
        style.configure("Surface.TFrame", background=T["surface"])
        style.configure("Header.TFrame", background=T["surface"])
        style.configure("TLabel", background=T["bg"], foreground=T["text"])
        style.configure("Surface.TLabel", background=T["surface"], foreground=T["text"])
        style.configure("Big.TLabel", font=("Segoe UI", 11, "bold"),
                        background=T["bg"], foreground=T["text"])
        style.configure("Muted.TLabel", foreground=T["text_muted"], background=T["bg"])
        style.configure("HeaderTitle.TLabel", background=T["surface"],
                        foreground=T["accent"], font=("Segoe UI Semibold", 17, "bold"))
        style.configure("HeaderSub.TLabel", background=T["surface"],
                        foreground=T["text_muted"], font=("Segoe UI", 9))

        # --- Notebook (tabs) -------------------------------------------------
        style.configure("TNotebook", background=T["bg"], borderwidth=0,
                        tabmargins=(6, 4, 6, 0))
        style.configure("TNotebook.Tab", background=T["surface_alt"],
                        foreground=T["text_muted"], padding=(18, 8),
                        font=base_font, borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", T["accent"]), ("active", T["accent_soft"])],
                  foreground=[("selected", "#FFFFFF"), ("active", T["accent_dark"])])

        # --- Buttons ---------------------------------------------------------
        style.configure("TButton", background=T["surface"], foreground=T["text"],
                        bordercolor=T["border"], focuscolor=T["accent_soft"],
                        padding=(12, 6), font=base_font, relief="flat")
        style.map("TButton",
                  background=[("active", T["surface_alt"]), ("pressed", T["border"])],
                  bordercolor=[("active", T["accent"])])
        # Primary (accent) button
        style.configure("Accent.TButton", background=T["accent"], foreground="#FFFFFF",
                        bordercolor=T["accent"], padding=(14, 7), font=bold_font,
                        relief="flat")
        style.map("Accent.TButton",
                  background=[("active", T["accent_dark"]), ("pressed", T["accent_dark"]),
                              ("disabled", T["border"])],
                  foreground=[("disabled", T["text_muted"])])
        # Destructive button
        style.configure("Danger.TButton", background=T["surface"],
                        foreground=T["danger"], bordercolor=T["danger"],
                        padding=(12, 6), font=bold_font, relief="flat")
        style.map("Danger.TButton",
                  background=[("active", "#F6E6E6"), ("pressed", "#EFD3D3")],
                  foreground=[("active", T["danger_dark"])])

        # --- Inputs ----------------------------------------------------------
        for cls in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(cls, fieldbackground=T["surface"], foreground=T["text"],
                            bordercolor=T["border"], arrowcolor=T["accent"],
                            insertcolor=T["text"], padding=4)
            style.map(cls, bordercolor=[("focus", T["accent"])],
                      fieldbackground=[("readonly", T["surface_alt"])])

        # --- Checkbuttons / radiobuttons ------------------------------------
        for cls in ("TCheckbutton", "TRadiobutton"):
            style.configure(cls, background=T["bg"], foreground=T["text"],
                            focuscolor=T["bg"])
            style.map(cls, background=[("active", T["bg"])],
                      indicatorcolor=[("selected", T["accent"])])

        # --- LabelFrame ------------------------------------------------------
        style.configure("TLabelframe", background=T["bg"], bordercolor=T["border"],
                        relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=T["bg"],
                        foreground=T["accent"], font=bold_font)

        # --- Treeview --------------------------------------------------------
        style.configure("Treeview", background=T["surface"],
                        fieldbackground=T["surface"], foreground=T["text"],
                        bordercolor=T["border"], rowheight=24, font=base_font)
        style.configure("Treeview.Heading", background=T["surface_alt"],
                        foreground=T["text"], font=bold_font, relief="flat",
                        padding=(6, 4))
        style.map("Treeview.Heading", background=[("active", T["accent_soft"])])
        style.map("Treeview",
                  background=[("selected", T["accent"])],
                  foreground=[("selected", "#FFFFFF")])

        # --- Progress / scrollbar / separator -------------------------------
        style.configure("TProgressbar", background=T["accent"],
                        troughcolor=T["surface_alt"], bordercolor=T["border"])
        style.configure("Horizontal.TProgressbar", background=T["accent"],
                        troughcolor=T["surface_alt"], bordercolor=T["border"])
        style.configure("TScrollbar", background=T["surface_alt"],
                        troughcolor=T["bg"], bordercolor=T["border"],
                        arrowcolor=T["text_muted"])
        style.configure("TSeparator", background=T["border"])
        style.configure("Accent.TSeparator", background=T["accent"])

        # --- Status bar ------------------------------------------------------
        style.configure("Status.TLabel", background=T["surface_alt"],
                        foreground=T["text_muted"], font=("Segoe UI", 9))

    def _make_logo(self, key: str, b64: str, max_h: int = 44):
        """Decode an embedded base64 PNG into a PhotoImage and cache it."""
        try:
            img = tk.PhotoImage(data=base64.b64decode(b64))
            # PhotoImage can only integer-subsample; shrink if taller than max_h
            if img.height() > max_h:
                factor = max(1, round(img.height() / max_h))
                img = img.subsample(factor, factor)
            self._logo_images[key] = img
            return img
        except Exception:
            return None

    def _build_header(self) -> None:
        """White brand header: DPIRD logo (primary, left) + APPN logo (right),
        app title, teal accent rule."""
        T = THEME
        header = tk.Frame(self.root, bg=T["surface"])
        header.pack(fill="x", side="top")

        inner = tk.Frame(header, bg=T["surface"])
        inner.pack(fill="x", padx=16, pady=8)

        # DPIRD logo (left, primary) - this is foremost a DPIRD app
        if logo_data is not None:
            dpird = self._make_logo("dpird", logo_data.DPIRD_LOGO_PNG_B64, max_h=64)
            if dpird is not None:
                tk.Label(inner, image=dpird, bg=T["surface"]).pack(side="left", padx=(0, 16))

        # Title block (centre-left)
        titlebox = tk.Frame(inner, bg=T["surface"])
        titlebox.pack(side="left")
        tk.Label(titlebox, text=APP_NAME, bg=T["surface"], fg=T["accent"],
                 font=("Segoe UI Semibold", 17, "bold")).pack(anchor="w")
        tk.Label(titlebox, text=APP_TAGLINE, bg=T["surface"], fg=T["text_muted"],
                 font=("Segoe UI", 9)).pack(anchor="w")

        # APPN logo (top-right), matched height to DPIRD
        if logo_data is not None:
            appn = self._make_logo("appn", logo_data.APPN_LOGO_PNG_B64, max_h=64)
            if appn is not None:
                tk.Label(inner, image=appn, bg=T["surface"]).pack(side="right", anchor="n")

        # Teal accent rule under the header
        tk.Frame(self.root, bg=T["accent"], height=3).pack(fill="x", side="top")

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self._build_header()

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_transfer = ttk.Frame(nb)
        self.tab_buckets = ttk.Frame(nb)
        self.tab_storage = ttk.Frame(nb)
        self.tab_console = ttk.Frame(nb)
        self.tab_history = ttk.Frame(nb)
        self.tab_settings = ttk.Frame(nb)
        self.tab_help = ttk.Frame(nb)

        nb.add(self.tab_transfer, text="  Transfer  ")
        nb.add(self.tab_buckets, text="  Buckets  ")
        nb.add(self.tab_storage, text="  Storage  ")
        nb.add(self.tab_console, text="  Console  ")
        nb.add(self.tab_history, text="  History  ")
        nb.add(self.tab_settings, text="  Settings  ")
        nb.add(self.tab_help, text="  Help  ")

        # Status bar - must be created BEFORE the tabs because some tab
        # builders (e.g. Transfer -> _refresh_remotes) write to status_var.
        self.status_var = tk.StringVar(value="Ready")
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", side="bottom")
        ttk.Separator(bar, orient="horizontal").pack(fill="x")
        ttk.Label(bar, textvariable=self.status_var, anchor="w",
                  style="Status.TLabel", padding=(10, 5)).pack(fill="x")

        self._build_transfer_tab()
        self._build_buckets_tab()
        self._build_storage_tab()
        self._build_console_tab()
        self._build_history_tab()
        self._build_settings_tab()
        self._build_help_tab()

    # ------------------------------------------------------------ Transfer
    def _build_transfer_tab(self) -> None:
        t = self.tab_transfer
        for i in range(2):
            t.columnconfigure(i, weight=1)
        # Progress/output row (8) gets the vertical stretch; set below.

        # --- Source ---
        ttk.Label(t, text="Source folder", style="Big.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 2))

        src_frame = ttk.Frame(t)
        src_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10)
        src_frame.columnconfigure(0, weight=1)

        self.src_var = tk.StringVar(value=self.cfg.get("default_source", ""))
        ttk.Entry(src_frame, textvariable=self.src_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(src_frame, text="Browse…", command=self._browse_source).grid(
            row=0, column=1, padx=(6, 0))
        ttk.Button(src_frame, text="Use app folder",
                   command=lambda: self.src_var.set(str(Path(__file__).resolve().parent))).grid(
            row=0, column=2, padx=(6, 0))

        # --- Destination ---
        ttk.Label(t, text="Destination (Pawsey)", style="Big.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(14, 2))

        dest_frame = ttk.Frame(t)
        dest_frame.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10)
        for c in (1, 3, 5):
            dest_frame.columnconfigure(c, weight=1)

        ttk.Label(dest_frame, text="Remote:").grid(row=0, column=0, sticky="w")
        self.remote_var = tk.StringVar(value=self.cfg.get("remote_name"))
        self.remote_combo = ttk.Combobox(dest_frame, textvariable=self.remote_var, width=18)
        self.remote_combo.grid(row=0, column=1, sticky="ew", padx=(4, 12))

        ttk.Label(dest_frame, text="Bucket:").grid(row=0, column=2, sticky="w")
        self.bucket_var = tk.StringVar(value=self.cfg.get("default_bucket"))
        ttk.Entry(dest_frame, textvariable=self.bucket_var).grid(
            row=0, column=3, sticky="ew", padx=(4, 12))

        ttk.Label(dest_frame, text="Path (optional):").grid(row=0, column=4, sticky="w")
        self.path_var = tk.StringVar()
        ttk.Entry(dest_frame, textvariable=self.path_var).grid(row=0, column=5, sticky="ew", padx=(4, 0))

        ttk.Button(dest_frame, text="Refresh remotes",
                   command=self._refresh_remotes).grid(row=0, column=6, padx=(8, 0))

        # Whether to place the source folder itself at the destination
        # (create a subfolder named after the source) vs. copying its
        # contents directly into the destination path.
        self.include_source_folder = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            dest_frame,
            text="Put the source folder itself at the destination "
                 "(create a subfolder named after it)",
            variable=self.include_source_folder,
            command=self._update_dest_preview).grid(
            row=1, column=0, columnspan=6, sticky="w", pady=(4, 0))

        # Live preview of where files will actually land
        self.dest_preview = ttk.Label(dest_frame, text="", foreground="#1a5d8a")
        self.dest_preview.grid(row=2, column=0, columnspan=7, sticky="w", pady=(2, 0))
        for var in (self.remote_var, self.bucket_var, self.path_var, self.src_var):
            var.trace_add("write", lambda *a: self._update_dest_preview())

        # --- Notes (mandatory) ---
        ttk.Label(t, text="Data description / notes  (required)", style="Big.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w", padx=10, pady=(14, 2))
        self.notes_text = scrolledtext.ScrolledText(t, height=4, wrap="word")
        self.notes_text.grid(row=5, column=0, columnspan=2, sticky="ew", padx=10)

        # --- Sync options (apply to Two-way sync and to overwrite behaviour) ---
        opts = ttk.LabelFrame(t, text="Sync options")
        opts.grid(row=6, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 0))
        opts.columnconfigure(5, weight=1)

        ttk.Label(opts, text="If the same file differs on both sides:").grid(
            row=0, column=0, sticky="w", padx=(8, 4), pady=6)
        self.conflict_var = tk.StringVar(value="newer")
        self.conflict_combo = ttk.Combobox(
            opts, textvariable=self.conflict_var, width=22, state="readonly",
            values=[
                "Keep newer version",
                "Keep both copies",
                "Local always wins",
                "Pawsey always wins",
            ])
        self.conflict_combo.grid(row=0, column=1, sticky="w", padx=(0, 16), pady=6)
        # map display -> value
        self._conflict_map = {
            "Keep newer version": "newer",
            "Keep both copies": "both",
            "Local always wins": "local",
            "Pawsey always wins": "pawsey",
        }
        self.conflict_combo.set("Keep newer version")

        self.verify_checksums = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Verify contents with checksums (slower, safer)",
                        variable=self.verify_checksums).grid(
            row=0, column=2, sticky="w", padx=8, pady=6)

        self.require_name_match = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Require matching folder names",
                        variable=self.require_name_match).grid(
            row=1, column=2, sticky="w", padx=8, pady=(0, 6))

        self.track_renames = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts,
            text="Detect renamed/moved files (server-side, no re-upload)",
            variable=self.track_renames).grid(
            row=2, column=2, sticky="w", padx=8, pady=(0, 6))

        self.use_recycle_bin = tk.BooleanVar(
            value=bool(self.cfg.get("use_recycle_bin", True)))
        ttk.Checkbutton(
            opts,
            text="Recycle bin: soft-delete & keep replaced versions",
            variable=self.use_recycle_bin,
            command=self._on_recycle_toggle).grid(
            row=3, column=2, sticky="w", padx=8, pady=(0, 6))

        # Preview / dry-run: rclone reports what WOULD change but transfers
        # nothing. Lets users sanity-check a Mirror or two-way sync first.
        # v2.1: ON by default; turning it OFF is password-protected.
        self.dry_run = tk.BooleanVar(
            value=bool(self.cfg.get("default_preview_only", True)))
        ttk.Checkbutton(
            opts,
            text="Preview only (dry-run — show changes, transfer nothing)",
            variable=self.dry_run,
            command=self._on_preview_toggle).grid(
            row=5, column=2, sticky="w", padx=8, pady=(0, 6))

        # v2.1: verify both sides after each transfer (rclone check). ON by
        # default; turning it OFF is password-protected.
        self.verify_both = tk.BooleanVar(
            value=bool(self.cfg.get("default_verify_both", True)))
        ttk.Checkbutton(
            opts,
            text="Verify both sides after each transfer (check)",
            variable=self.verify_both,
            command=self._on_verify_both_toggle).grid(
            row=6, column=2, sticky="w", padx=8, pady=(0, 6))

        # Deletion safety cap (two-way sync). Adjustable, and can be turned
        # off. When on, a sync that would delete more than this percentage of
        # files on either side is aborted before any data is removed.
        caprow = ttk.Frame(opts)
        caprow.grid(row=4, column=2, sticky="w", padx=8, pady=(0, 6))
        self.limit_deletes = tk.BooleanVar(
            value=bool(self.cfg.get("limit_deletes", True)))
        ttk.Checkbutton(
            caprow, text="Abort two-way sync if it would delete more than",
            variable=self.limit_deletes,
            command=self._on_delete_cap_change).pack(side="left")
        self.max_delete_value = tk.StringVar(
            value=str(self.cfg.get("max_delete_percent", 50)))
        ent = ttk.Entry(caprow, textvariable=self.max_delete_value, width=4)
        ent.pack(side="left", padx=4)
        ent.bind("<FocusOut>", lambda e: self._on_delete_cap_change())
        ttk.Label(caprow, text="% of files (uncheck = no limit)").pack(side="left")

        ttk.Button(opts, text="Verify both sides (check)",
                   command=self._verify_check).grid(
            row=1, column=0, sticky="w", padx=8, pady=(0, 6))

        ttk.Button(opts, text="Show last changes",
                   command=self._show_last_changes).grid(
            row=2, column=0, sticky="w", padx=8, pady=(0, 6))

        # Auto-sync (two-way only): re-run on a timer like OneDrive
        autobar = ttk.Frame(opts)
        autobar.grid(row=0, column=3, rowspan=3, sticky="nw", padx=(16, 8), pady=6)
        self.autosync_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(autobar, text="Auto-sync (Two-way) every",
                        variable=self.autosync_enabled,
                        command=self._on_autosync_toggle).pack(side="left")
        self.autosync_interval = tk.StringVar(
            value=str(self.cfg.get("autosync_interval_min", 15)))
        ttk.Entry(autobar, textvariable=self.autosync_interval, width=5).pack(
            side="left", padx=4)
        ttk.Label(autobar, text="min").pack(side="left")

        # --- Mode + buttons ---
        mode_frame = ttk.Frame(t)
        mode_frame.grid(row=7, column=0, columnspan=2, sticky="ew", padx=10, pady=10)

        ttk.Label(mode_frame, text="Mode:", style="Big.TLabel").pack(side="left")
        self.mode_var = tk.StringVar(value="copy")
        # Remember the last confirmed mode so a password-gated mode change
        # (Mirror / Two-way) can be reverted if the password check fails.
        self._prev_mode = "copy"
        ttk.Radiobutton(mode_frame, text="Copy (one-way, additive)",
                        variable=self.mode_var, value="copy",
                        command=self._on_mode_change).pack(side="left", padx=(8, 2))
        ttk.Radiobutton(mode_frame, text="Mirror (one-way, exact)",
                        variable=self.mode_var, value="sync",
                        command=self._on_mode_change).pack(side="left", padx=2)
        ttk.Radiobutton(mode_frame, text="Two-way sync (OneDrive-style)",
                        variable=self.mode_var, value="bisync",
                        command=self._on_mode_change).pack(side="left", padx=2)
        ttk.Radiobutton(mode_frame, text="Resume previous…",
                        variable=self.mode_var, value="resume",
                        command=self._on_mode_change).pack(side="left", padx=2)

        ttk.Button(mode_frame, text="Start transfer", style="Accent.TButton",
                   command=self._start_transfer).pack(side="right", padx=(8, 0))
        self.stop_btn = ttk.Button(mode_frame, text="Stop", command=self._stop_transfer,
                                   state="disabled")
        self.stop_btn.pack(side="right")

        # Mode hint - explains rename behaviour per mode so users don't expect
        # Copy to detect renames (rclone can't track renames in copy).
        self.mode_hint = ttk.Label(t, text="", foreground="#a76b00",
                                   wraplength=980, justify="left")
        self.mode_hint.grid(row=8, column=0, columnspan=2, sticky="w", padx=10)

        # --- Progress + output ---
        prog_frame = ttk.Frame(t)
        prog_frame.grid(row=9, column=0, columnspan=2, sticky="nsew", padx=10, pady=(0, 10))
        prog_frame.columnconfigure(0, weight=1)
        prog_frame.rowconfigure(2, weight=1)
        t.rowconfigure(9, weight=1)

        self.progress = ttk.Progressbar(prog_frame, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")

        self.progress_label = ttk.Label(prog_frame, text="Idle")
        self.progress_label.grid(row=1, column=0, sticky="w", pady=(4, 4))

        # Clear the on-screen log only. The full log on disk is untouched.
        ttk.Button(prog_frame, text="Clear output",
                   command=self._clear_output).grid(
            row=1, column=0, sticky="e", pady=(4, 4))

        self.output_text = scrolledtext.ScrolledText(prog_frame, height=12, wrap="word",
                                                    font=("Consolas" if IS_WINDOWS else "Monospace", 9))
        self.output_text.grid(row=2, column=0, sticky="nsew")
        self.output_text.configure(state="disabled")

        self._refresh_remotes()
        self._update_dest_preview()
        self._on_mode_change()

    def _browse_source(self) -> None:
        d = filedialog.askdirectory(initialdir=self.src_var.get() or str(Path.home()),
                                    title="Choose source folder")
        if d:
            self.src_var.set(d)

    # ----- v2.1 password-gated options ----------------------------------
    def _on_preview_toggle(self) -> None:
        """'Preview only' is a safe default. Turning it OFF needs the password."""
        if not self.dry_run.get():          # user is trying to disable it
            if not self._require_password("turn OFF 'Preview only'"):
                self.dry_run.set(True)      # revert — stays protected

    def _on_verify_both_toggle(self) -> None:
        """'Verify both sides' is a safe default. Turning it OFF needs the password."""
        if not self.verify_both.get():
            if not self._require_password("turn OFF 'Verify both sides'"):
                self.verify_both.set(True)

    def _on_mode_change(self) -> None:
        """Update the mode hint so users know how each mode treats renames.

        Mirror and Two-way sync can delete/overwrite data, so selecting them
        is password-protected (v2.1). If the password check fails we revert
        to the previously selected mode.
        """
        new_mode = self.mode_var.get()
        protected = {"sync": "Mirror", "bisync": "Two-way sync"}
        if new_mode in protected and new_mode != self._prev_mode:
            if not self._require_password(f"select {protected[new_mode]} mode"):
                self.mode_var.set(self._prev_mode)  # revert (no command fired)
                new_mode = self._prev_mode
        self._prev_mode = new_mode
        return self._update_mode_hint()

    def _update_mode_hint(self) -> None:
        """Update the mode hint so users know how each mode treats renames."""
        if not hasattr(self, "mode_hint"):
            return
        mode = self.mode_var.get()
        if mode == "copy":
            self.mode_hint.configure(
                text="Copy never deletes and CANNOT detect renames - a renamed "
                     "file uploads under its new name while the old name stays "
                     "on Pawsey (you get both). To apply a rename without "
                     "re-uploading, use Mirror or Two-way sync with 'Detect "
                     "renamed/moved files', or the Storage tab 'Rename / Move…' "
                     "button. Empty folders ARE copied.")
        elif mode == "sync":
            self.mode_hint.configure(
                text="Mirror makes Pawsey exactly match the source (it deletes "
                     "extras on Pawsey, and creates/removes empty folders to "
                     "match). With 'Detect renamed/moved files' on, renames "
                     "become server-side moves (no re-upload).")
        elif mode == "bisync":
            self.mode_hint.configure(
                text="Two-way sync reconciles both sides, empty folders "
                     "included. Renames propagate as renames when 'Detect "
                     "renamed/moved files' is on.")
        else:
            self.mode_hint.configure(text="")

    def _refresh_remotes(self) -> None:
        remotes = RcloneRemote.list_remotes()
        self.remote_combo["values"] = remotes
        if self.remote_var.get() not in remotes and remotes:
            self.remote_var.set(remotes[0])
        self.status_var.set(f"Remotes: {', '.join(remotes) or 'none configured'}")

    def _effective_dest(self, remote: str, bucket: str, path: str, src: str) -> str:
        """Build the actual destination, honouring the 'include source folder'
        option. With it on, the source folder's own name is appended so the
        folder is recreated at the destination rather than its contents being
        poured into the destination path."""
        dest = f"{remote}:{bucket}"
        path = path.strip().lstrip("/")
        if path:
            dest = f"{dest}/{path}"
        if (hasattr(self, "include_source_folder")
                and self.include_source_folder.get() and src):
            leaf = Path(src).name
            # Don't append if the path already ends with this folder name -
            # otherwise typing '2024/test-syn' AND ticking the box would
            # produce '2024/test-syn/test-syn' (an empty, wrong destination).
            already = path and path.rstrip("/").split("/")[-1].lower() == leaf.lower()
            if leaf and not already:
                dest = f"{dest}/{leaf}"
        return dest

    def _update_dest_preview(self) -> None:
        """Refresh the small 'files will land at …' preview label."""
        if not hasattr(self, "dest_preview"):
            return
        remote = self.remote_var.get().strip()
        bucket = self.bucket_var.get().strip()
        path = self.path_var.get().strip()
        src = self.src_var.get().strip()
        if not (remote and bucket):
            self.dest_preview.configure(text="")
            return
        dest = self._effective_dest(remote, bucket, path, src)
        self.dest_preview.configure(text=f"Files will be placed under:  {dest}/")

    # ------------------------------------------------------ Transfer logic
    def _conflict_choice(self) -> str:
        """Return the internal conflict key: newer | both | local | pawsey."""
        disp = self.conflict_combo.get() if hasattr(self, "conflict_combo") else ""
        return self._conflict_map.get(disp, "newer")

    def _on_recycle_toggle(self) -> None:
        self.cfg.set("use_recycle_bin", bool(self.use_recycle_bin.get()))
        self.cfg.save()

    def _delete_cap_percent(self) -> int:
        """Return the percent to pass to bisync --max-delete. If the cap is
        unchecked, return 100 (effectively no limit)."""
        if not (hasattr(self, "limit_deletes") and self.limit_deletes.get()):
            return 100
        try:
            v = int(float(self.max_delete_value.get().strip()))
        except (TypeError, ValueError):
            v = 50
        return max(1, min(100, v))

    def _on_delete_cap_change(self) -> None:
        """Persist the cap setting when the user edits it."""
        try:
            self.cfg.set("limit_deletes", bool(self.limit_deletes.get()))
            self.cfg.set("max_delete_percent", self._delete_cap_percent())
            self.cfg.save()
        except Exception:
            pass
        if hasattr(self, "status_var"):
            if self.limit_deletes.get():
                self.status_var.set(
                    f"Deletion safety cap: abort if >{self._delete_cap_percent()}%"
                    f" of files would be deleted.")
            else:
                self.status_var.set(
                    "Deletion safety cap is OFF - syncs may delete any number "
                    "of files.")

    def _track_renames_strategy(self, verify: bool) -> str:
        """Pick the rename-matching strategy.

        - With checksum verification ON, we keep MD5 metadata on upload, so
          'hash' works for every file (including large multipart objects)
          and is the most accurate way to confirm two paths hold the same
          content - the safest basis for a rename.
        - With verification OFF, multipart objects have no usable MD5, so we
          fall back to 'modtime' (size + modification time), which works for
          objects this app uploaded because rclone preserves their mtime.
        """
        return "hash" if verify else "modtime"

    @staticmethod
    def _recycle_dir_for(dest: str, ts: Optional[str] = None) -> Optional[str]:
        """Given a destination 'remote:bucket/path', return the recycle-bin
        backup path 'remote:bucket/_recycle_bin/<ts>' (outside the synced
        sub-path so there's no loop). Returns None if it can't be derived."""
        try:
            remote_part, sep, path_part = dest.partition(":")
            if not sep:
                return None
            bucket = path_part.split("/")[0]
            if not bucket:
                return None
            ts = ts or datetime.now().strftime("%Y%m%d_%H%M%S")
            return f"{remote_part}:{bucket}/{RECYCLE_PREFIX}/{ts}"
        except Exception:
            return None

    def _s3_flags(self, verify: bool = False) -> list[str]:
        flags = ["--s3-no-check-bucket"]
        # --s3-disable-checksum speeds up large-file uploads by not
        # pre-computing/storing MD5. But that makes later --checksum
        # verification unreliable, so we keep checksums when verify is on.
        if not verify:
            flags.append("--s3-disable-checksum")
        flags += [
            f"--s3-chunk-size={self.cfg.get('s3_chunk_size', '64M')}",
            f"--s3-upload-concurrency={self.cfg.get('s3_upload_concurrency', 4)}",
            "--multi-thread-streams=0",
            f"--transfers={self.cfg.get('transfers', 1)}",
            f"--checkers={self.cfg.get('checkers', 16)}",
            f"--retries={self.cfg.get('retries', 5)}",
            f"--low-level-retries={self.cfg.get('low_level_retries', 10)}",
        ]
        return flags

    def _build_rclone_cmd(self, mode: str, src: str, dest: str,
                          bisync_resync: bool = False) -> list[str]:
        verify = bool(self.verify_checksums.get()) if hasattr(self, "verify_checksums") else False
        conflict = self._conflict_choice()
        track = bool(self.track_renames.get()) if hasattr(self, "track_renames") else False
        recycle = bool(self.use_recycle_bin.get()) if hasattr(self, "use_recycle_bin") else False
        dry = bool(self.dry_run.get()) if hasattr(self, "dry_run") else False
        bwlimit = str(self.cfg.get("bwlimit", "")).strip()

        if mode == "bisync":
            # Two-way sync. PATH1 = local source, PATH2 = pawsey dest.
            cmd = [RCLONE_EXE, "bisync", src, dest]
            cmd += self._s3_flags(verify)
            # Carry empty folders in BOTH directions (see EMPTY_DIR_FLAGS).
            cmd += list(EMPTY_DIR_FLAGS)
            # Conflict resolution
            if conflict == "both":
                cmd += ["--conflict-resolve", "none"]   # keep both, numbered
            elif conflict == "newer":
                cmd += ["--conflict-resolve", "newer", "--conflict-loser", "delete"]
            elif conflict == "local":
                cmd += ["--conflict-resolve", "path1", "--conflict-loser", "delete"]
            elif conflict == "pawsey":
                cmd += ["--conflict-resolve", "path2", "--conflict-loser", "delete"]
            # Detect renames/moves on BOTH sides: a folder/file renamed on
            # either local or Pawsey is applied to the other as a server-side
            # move instead of delete + re-upload. NOT during a --resync: the
            # baseline uses copy/move internally, where rclone ignores
            # --track-renames and logs a noisy (harmless) error, so we omit it.
            if track and not bisync_resync:
                cmd += ["--track-renames",
                        "--track-renames-strategy",
                        self._track_renames_strategy(verify)]
            # Recycle bin: preserve files deleted/overwritten on the Pawsey
            # side into a dated _recycle_bin prefix instead of removing them.
            if recycle:
                rdir = self._recycle_dir_for(dest)
                if rdir:
                    cmd += ["--backup-dir2", rdir]
            # Robustness flags for long unattended runs
            cmd += ["--resilient", "--recover"]
            # Deletion safety cap (adjustable; can be turned off). bisync's
            # --max-delete is a PERCENT. Unchecked => 100 (effectively no
            # limit, since you can never delete more than 100%).
            cmd += ["--max-delete", str(self._delete_cap_percent())]
            if bisync_resync:
                cmd += ["--resync"]
            if verify:
                # Compare by size + modtime + checksum. Including modtime is
                # essential: with plain --checksum, bisync drops modtime and
                # then silently ignores --conflict-resolve newer (it has no
                # time to compare). --compare keeps both, so checksum
                # verification AND "keep newer" both work.
                cmd += ["--compare", "size,modtime,checksum"]
            if bwlimit:
                cmd += ["--bwlimit", bwlimit]
            if dry:
                cmd += ["--dry-run"]
            cmd += ["--progress", "--stats=5s", "-v"]
            return cmd

        # One-way copy or mirror sync
        verb = "sync" if mode == "sync" else "copy"
        cmd = [RCLONE_EXE, verb, src, dest]
        cmd += self._s3_flags(verify)
        # Carry empty folders across (see EMPTY_DIR_FLAGS). Without these an
        # empty local folder is simply skipped, so the destination ends up
        # with the files but not the (deliberately empty) folder structure.
        cmd += list(EMPTY_DIR_FLAGS)

        # Detect renamed/moved files: rclone matches a renamed/moved file by
        # content and performs a server-side move instead of delete+re-upload.
        # Only meaningful for mirror sync (copy never deletes, so a rename
        # just adds the new path and leaves the old one behind).
        if track and mode == "sync":
            cmd += ["--track-renames",
                    "--track-renames-strategy",
                    self._track_renames_strategy(verify)]

        # Backup-dir: where overwritten/deleted files are moved instead of
        # being removed. Recycle bin (if on) takes precedence and catches
        # BOTH mirror-sync deletions and replaced versions. Otherwise the
        # "keep both copies" conflict option uses a _backups prefix.
        backup_dir = None
        if recycle:
            backup_dir = self._recycle_dir_for(dest)
        elif conflict == "both":
            try:
                remote_part, _, path_part = dest.partition(":")
                bucket = path_part.split("/")[0]
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_dir = f"{remote_part}:{bucket}/_backups/{ts}"
            except Exception:
                backup_dir = None
        if backup_dir:
            cmd += ["--backup-dir", backup_dir]

        if verify:
            cmd += ["--checksum"]
        if bwlimit:
            cmd += ["--bwlimit", bwlimit]
        if dry:
            cmd += ["--dry-run"]
        cmd += ["--progress", "--stats=5s", "-v"]
        return cmd

    # ----- bisync initialisation tracking --------------------------------
    @staticmethod
    def _bisync_key(src: str, dest: str) -> str:
        return f"{src}|||{dest}"

    def _bisync_is_initialised(self, src: str, dest: str) -> bool:
        try:
            data = json.loads(BISYNC_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return False
        return self._bisync_key(src, dest) in data.get("pairs", [])

    def _bisync_mark_initialised(self, src: str, dest: str) -> None:
        try:
            data = json.loads(BISYNC_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {"pairs": []}
        key = self._bisync_key(src, dest)
        if key not in data.setdefault("pairs", []):
            data["pairs"].append(key)
        try:
            BISYNC_STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _bisync_forget_initialised(self, src: str, dest: str) -> None:
        """Drop our 'baseline exists' note for a pair.

        Called when rclone tells us the baseline is gone. Otherwise the app
        keeps believing the pair is initialised and never offers the resync
        the next run needs, so every attempt fails the same way."""
        if not (src and dest):
            return
        try:
            data = json.loads(BISYNC_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        key = self._bisync_key(src, dest)
        pairs = data.get("pairs", [])
        if key in pairs:
            data["pairs"] = [p for p in pairs if p != key]
            try:
                BISYNC_STATE_FILE.write_text(json.dumps(data, indent=2),
                                             encoding="utf-8")
            except Exception:
                pass

    def _rebuild_bisync_baseline(self) -> None:
        """Re-run the pair that just failed with --resync, rebuilding the
        baseline listings so later two-way syncs work normally."""
        src, dest = self.current_src, self.current_dest
        if not (src and dest):
            messagebox.showerror(APP_NAME,
                                 "No two-way sync pair is loaded to rebuild.")
            return
        self._pending_bisync_mark = (src, dest)
        self.user_stopped = False
        self.auth_failure_seen = False
        self.fatal_error_seen = False
        self.delete_cap_seen = False
        self.empty_listing_seen = False
        self.bisync_needs_resync_seen = False
        self.restart_count = 0
        self._change_counts = _new_change_counts()
        self._change_details = []
        self._rename_sources = set()
        self._ensure_remote_path(dest)
        cmd = self._build_rclone_cmd("bisync", src, dest, bisync_resync=True)
        self.current_transfer_cmd = cmd
        if self.current_resume_id:
            ResumeStore.update(self.current_resume_id, status="in_progress")
        try:
            log_path = (TRANSFER_LOG_DIR /
                        f"transfer_{self.current_resume_id or 'resync'}.log")
            self._transfer_log_fh = open(log_path, "a", encoding="utf-8")
        except Exception:
            self._transfer_log_fh = None
        self._append_output(
            f"\n=== Rebuilding two-way baseline (--resync) "
            f"{datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
        self._append_output(f"$ {' '.join(_quote(a) for a in cmd)}\n\n")
        self._launch_rclone(cmd)

    # ----- name-match safety --------------------------------------------
    @staticmethod
    def _basename_of_dest(dest: str) -> str:
        """Last path component of a 'remote:bucket/a/b' destination."""
        after_colon = dest.partition(":")[2]
        return after_colon.rstrip("/").split("/")[-1] if after_colon else ""

    def _names_match(self, src: str, dest: str) -> bool:
        src_name = Path(src).name.strip().lower()
        dest_name = self._basename_of_dest(dest).strip().lower()
        return bool(src_name) and src_name == dest_name

    @staticmethod
    def _ensure_remote_path(dest: str) -> None:
        """Make sure the destination folder exists on Pawsey (v2.3: used by
        every folder copy/move, not just two-way sync).

        rclone's --create-empty-src-dirs replicates the empty folders INSIDE
        a source folder, but never the source folder itself: `rclone copy
        <empty folder> pawsey:bucket/x` reports "There was nothing to
        transfer" and writes no marker, so uploading, pasting, moving or
        syncing a folder that is itself empty left no trace on Pawsey and
        nothing to see on the Storage tab. A move of such a folder was worse:
        rclone moved nothing, the app then cleared the old marker, and the
        folder simply vanished. Creating the destination folder first closes
        that gap for every one of those paths.

        Two-way sync additionally refuses to start unless BOTH sides already
        exist: if the Pawsey prefix has no objects under it yet rclone reports
        'error reading source root directory: directory not found', raises a
        Bisync critical error and aborts.

        One idempotent API call fixes both. It writes only the zero-byte
        folder marker, never any data, and does nothing if the prefix already
        exists.
        """
        if not dest:
            return
        try:
            run_rclone_capture(
                ["mkdir", dest, "--s3-no-check-bucket", "--s3-directory-markers"],
                timeout=120)
        except Exception:
            pass  # bisync will report the real problem if this didn't help

    @staticmethod
    def _purge_empty_prefix(path: str) -> bool:
        """Clear a Pawsey prefix that has no files left under it.

        After a server-side folder MOVE, rclone's --delete-empty-src-dirs
        cannot finish the job on object storage: it logs "Removing directory"
        but a folder marker is an object, not a directory it can rmdir, so the
        old folder lingers as a ghost that looks like the move only half
        happened. `purge` does remove markers, so we finish the tidy-up here.

        Guarded on purpose: the prefix is listed first and nothing is purged
        unless there are genuinely NO files under it. A move that only
        partially succeeded therefore can never turn into a data deletion, and
        anything we cannot verify is left strictly alone.

        Returns True only if a purge actually ran and succeeded.
        """
        if not path:
            return False
        try:
            rc, out = run_rclone_capture(
                ["lsf", "-R", "--files-only", path], timeout=3600)
            if rc != 0:
                return False        # can't verify it's empty -> don't touch it
            if any(ln.strip() for ln in out.splitlines()):
                return False        # still holds files -> never purge
            rc2, _ = run_rclone_capture(
                ["purge", path, "--s3-no-check-bucket", *S3_DELETE_FLAGS],
                timeout=3600)
            return rc2 == 0
        except Exception:
            return False

    def _bucket_exists(self, remote: str, bucket: str):
        """Return (exists, available_buckets).

        exists is True/False if we could check, or None if the listing
        itself failed (network/auth) - in which case the caller should NOT
        block the transfer on a bucket-name basis."""
        rc, out = run_rclone_capture(
            ["lsjson", f"{remote}:", "--dirs-only"], timeout=30)
        if rc != 0:
            return None, []
        try:
            names = [e.get("Name") for e in json.loads(out or "[]") if e.get("Name")]
        except Exception:
            return None, []
        return (bucket in names), names

    # ----- verify both sides (rclone check) ------------------------------
    @staticmethod
    def _list_dir_tree(location: str) -> tuple[bool, set[str]]:
        """Return (ok, set of every folder path under `location`).

        `rclone check` compares FILES only - a folder present on one side and
        absent on the other is completely invisible to it, which is how an
        empty-folder difference could survive a "both sides match" verdict.
        Listing the folders separately is the only way to compare them.
        """
        rc, out = run_rclone_capture(
            ["lsf", "-R", "--dirs-only", S3_MARKER_FLAG, location,
             *_app_prefix_excludes()],
            timeout=86400)
        if rc != 0:
            return False, set()
        # run_rclone_capture merges stderr, so keep only real listing lines
        # (rclone's lsf --dirs-only always emits a trailing slash).
        dirs = {ln.strip().rstrip("/") for ln in out.splitlines()
                if ln.strip().endswith("/")}
        return True, {d for d in dirs if d}

    def _verify_check(self) -> None:
        """Compare the current source and destination and report honestly
        whether they hold the same content - files AND folders."""
        if self.transfer_active:
            messagebox.showinfo(APP_NAME, "Wait for the current transfer to finish first.")
            return
        src = self.src_var.get().strip()
        bucket = self.bucket_var.get().strip()
        path = self.path_var.get().strip().lstrip("/")
        remote = self.remote_var.get().strip()
        if not (src and remote and bucket):
            messagebox.showerror(APP_NAME,
                                 "Set source, remote and bucket before verifying.")
            return
        dest = self._effective_dest(remote, bucket, path, src)
        use_checksum = bool(self.verify_checksums.get())

        self.status_var.set("Verifying both sides…")
        self._append_output(
            f"\n=== Verify (rclone check{' --checksum' if use_checksum else ''}"
            f" + folder comparison) {datetime.now():%H:%M:%S} ==="
            f"\n{src}  <->  {dest}\n\n")

        def work():
            # 1. Files. Deliberately NOT --size-only: two files of the same
            #    length with different content pass a size-only check, so the
            #    old behaviour could report "identical content" for files that
            #    were nothing of the sort. Plain `check` compares hashes
            #    wherever the backend has them and falls back to size for the
            #    rest, and tells us how many it could not hash.
            #    The app's own prefixes (recycle bin, backups, share pages)
            #    live in the same bucket but have no local counterpart, so
            #    they are excluded or every verify would "find differences".
            args = ["check", src, dest, "--one-way=false",
                    *_app_prefix_excludes()]
            if use_checksum:
                args.append("--checksum")
            rc, out = run_rclone_capture(args, timeout=86400)
            # 2. Folders, including empty ones (invisible to `check`).
            ok_src, src_dirs = self._list_dir_tree(src)
            ok_dest, dest_dirs = self._list_dir_tree(dest)
            return {
                "rc": rc, "out": out,
                "dirs_ok": ok_src and ok_dest,
                "missing_on_dest": sorted(src_dirs - dest_dirs),
                "missing_on_src": sorted(dest_dirs - src_dirs),
            }

        def done(result, err):
            if err:
                messagebox.showerror(APP_NAME, f"Verify error:\n{err}")
                return
            rc = result["rc"]
            out = result["out"] or ""
            self._append_output(out + "\n")

            md, ms = result["missing_on_dest"], result["missing_on_src"]
            dirs_ok = result["dirs_ok"]
            if not dirs_ok:
                self._append_output(
                    "[folder comparison] could not list one of the two sides - "
                    "folder differences were NOT checked.\n")
            elif md or ms:
                self._append_output("\n--- Folder differences ---\n")
                for d in md:
                    self._append_output(f"[only on LOCAL]  {d}/\n")
                for d in ms:
                    self._append_output(f"[only on PAWSEY] {d}/\n")
                self._append_output("\n")
            else:
                self._append_output(
                    "[folder comparison] folder trees match "
                    "(including empty folders).\n")

            # How many files rclone could not hash? Worth surfacing: those
            # were compared by size alone, so the pass is weaker for them.
            unhashed = ""
            m = re.search(r"(\d+)\s+hashes could not be checked", out)
            if m and m.group(1) != "0":
                unhashed = (
                    f"\n\nNote: {m.group(1)} file(s) had no checksum on the "
                    f"Pawsey side and were compared by size only. Tick "
                    f"'Verify contents with checksums' in Sync options and "
                    f"re-upload them for a full content check.")

            files_ok = (rc == 0)
            folders_ok = dirs_ok and not md and not ms

            if files_ok and folders_ok:
                self.status_var.set("Verify: both sides match (files + folders).")
                messagebox.showinfo(
                    APP_NAME,
                    "Verification passed.\n\n"
                    "Files match, and both sides have the same folder "
                    "structure including empty folders." + unhashed)
            elif files_ok and not dirs_ok:
                self.status_var.set("Verify: files match; folders unchecked.")
                messagebox.showwarning(
                    APP_NAME,
                    "Files match, but the folder lists could not be read, so "
                    "empty folders were NOT verified.\nSee the output log."
                    + unhashed)
            else:
                bits = []
                if not files_ok:
                    bits.append("file differences")
                if md:
                    bits.append(f"{len(md)} folder(s) missing on Pawsey")
                if ms:
                    bits.append(f"{len(ms)} folder(s) missing locally")
                detail = ", ".join(bits)
                self.status_var.set(f"Verify: differences found ({detail}).")
                messagebox.showwarning(
                    APP_NAME,
                    f"Verification found differences between the two sides:\n\n"
                    f"  {detail}\n\n"
                    f"See the output log for the full list. Run a sync to "
                    f"reconcile." + unhashed)

        self._bg_call(work, done)

    def _start_transfer(self) -> None:
        if self.transfer_active:
            messagebox.showinfo(APP_NAME, "A transfer is already running.")
            return

        mode = self.mode_var.get()

        # ---- Determine what to run ----
        resume_entry: Optional[dict] = None
        bisync_resync = False
        self._pending_bisync_mark = None
        if mode == "resume":
            resume_entry = self._pick_resume_dialog()
            if not resume_entry:
                return  # user cancelled or no entries
            src = resume_entry["src"]
            dest = resume_entry["dest"]
            notes = (resume_entry.get("notes", "") +
                     f"\n[Resumed at {datetime.now().isoformat(timespec='seconds')}]")
            actual_mode = resume_entry.get("mode", "copy")
            resume_id = resume_entry["id"]
            # Mark this entry as in_progress again
            ResumeStore.update(resume_id, status="in_progress")
            # For a resumed two-way sync, the baseline already exists, so
            # never re-run --resync (that would discard remote-only changes).
            if actual_mode == "bisync":
                self._pending_bisync_mark = (src, dest)
                bisync_resync = False
        else:
            src = self.src_var.get().strip()
            bucket = self.bucket_var.get().strip()
            path = self.path_var.get().strip().lstrip("/")
            remote = self.remote_var.get().strip()
            notes = self.notes_text.get("1.0", "end").strip()
            actual_mode = mode

            if not src or not Path(src).exists():
                messagebox.showerror(APP_NAME,
                                     f"Source folder is missing or invalid:\n{src}")
                return
            if not remote:
                messagebox.showerror(APP_NAME, "No remote selected.")
                return
            if not bucket:
                messagebox.showerror(APP_NAME, "Bucket name is required.")
                return
            if not notes:
                messagebox.showerror(APP_NAME,
                                     "Notes are required before every transfer. "
                                     "Please describe what data you are transferring.")
                return

            # ---- Pre-flight: does the destination bucket exist? ----
            # Catches typos (e.g. "dprid-appn" vs "dpird-appn") before we
            # fire off a transfer that would otherwise fail with a cryptic
            # "directory not found" and then auto-restart repeatedly.
            self.status_var.set("Checking destination bucket…")
            self.root.update_idletasks()
            ok_bucket, available = self._bucket_exists(remote, bucket)
            if ok_bucket is False:   # definitively absent (None = couldn't check)
                import difflib
                suggestion = ""
                close = difflib.get_close_matches(bucket, available, n=1, cutoff=0.6)
                if close:
                    suggestion = f"\n\nDid you mean:  {close[0]}  ?"
                listing = ("\n  ".join(available) if available
                           else "(none found on this remote)")
                messagebox.showerror(
                    APP_NAME,
                    f"Bucket '{bucket}' was not found on remote '{remote}'.\n"
                    f"Check the spelling in the Bucket field.{suggestion}\n\n"
                    f"Buckets available on '{remote}':\n  {listing}")
                self.status_var.set("Bucket not found - fix the Bucket field.")
                return

            dest = self._effective_dest(remote, bucket, path, src)

            if actual_mode == "sync":
                if not messagebox.askyesno(
                    "Mirror sync warning",
                    f"Mirror sync will DELETE any files at\n  {dest}\n"
                    f"that are not present in the source folder.\n\n"
                    f"Proceed?",
                    icon="warning",
                ):
                    return

            # ---- Name-match safety (hard block when enabled) ----
            if self.require_name_match.get() and not self._names_match(src, dest):
                src_name = Path(src).name
                dest_name = self._basename_of_dest(dest) or "(bucket root)"
                messagebox.showerror(
                    "Folder names do not match - sync blocked",
                    f"The source and destination folder names are different:\n\n"
                    f"  Source ends in:       {src_name}\n"
                    f"  Destination ends in:  {dest_name}\n\n"
                    f"'Require matching folder names' is ON, so this sync is\n"
                    f"blocked to prevent putting data in the wrong place.\n\n"
                    f"To proceed, either:\n"
                    f"  - Fix the source folder or the destination Path so the\n"
                    f"    last folder name matches (e.g. local …/2024  <->  …/2024), or\n"
                    f"  - Untick 'Require matching folder names' if you really\n"
                    f"    intend to sync differently-named folders.")
                return

            # ---- Two-way sync first-run handling ----
            bisync_resync = False
            if actual_mode == "bisync":
                if not self._bisync_is_initialised(src, dest):
                    choice = messagebox.askyesnocancel(
                        "Initialise two-way sync (baseline)",
                        f"This is the FIRST two-way sync between:\n"
                        f"  {src}\n  {dest}\n\n"
                        f"The first run builds a baseline (--resync), which "
                        f"MERGES the two sides:\n"
                        f"  - Every file on either side is copied to the other, "
                        f"so both sides end up with the UNION of all files.\n"
                        f"  - If a file exists under DIFFERENT names on each side "
                        f"(e.g. you renamed it on one side only), BOTH names are "
                        f"kept on BOTH sides - renames are NOT detected during a "
                        f"baseline.\n"
                        f"  - Where the same file differs, the LOCAL copy wins.\n\n"
                        f"So reconcile any renames/cleanup BEFORE the baseline, or "
                        f"expect to remove duplicate-named files afterwards. After "
                        f"the baseline, later runs detect renames normally.\n\n"
                        f"  Yes  = build the baseline now (needed the first time)\n"
                        f"  No   = run a normal two-way sync (fails if no baseline)\n"
                        f"  Cancel = stop",
                        icon="warning",
                    )
                    if choice is None:
                        return
                    bisync_resync = bool(choice)
                self._pending_bisync_mark = (src, dest)
            else:
                self._pending_bisync_mark = None

            # Register a new resume slot
            resume_id = datetime.now().strftime("%Y%m%dT%H%M%S")
            ResumeStore.add({
                "id": resume_id,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "last_updated": datetime.now().isoformat(timespec="seconds"),
                "src": src,
                "dest": dest,
                "mode": actual_mode,
                "notes": notes,
                "status": "in_progress",
                "restart_count": 0,
                "bisync_resync_done": bisync_resync,
            })

        # ---- Persist the JSONL audit log entry ----
        self._append_log({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "mode": actual_mode,
            "source": src,
            "destination": dest,
            "notes": notes,
            "status": "started",
            "resume_id": resume_id,
        })

        # ---- Reset per-transfer state ----
        self.current_resume_id = resume_id
        self.current_src = src
        self.current_dest = dest
        self.current_mode = actual_mode
        self.user_stopped = False
        self.auth_failure_seen = False
        self.fatal_error_seen = False
        self.delete_cap_seen = False
        self.empty_listing_seen = False
        self.bisync_needs_resync_seen = False
        self.restart_count = 0
        # Reset change tracking for this logical transfer (not on auto-restart)
        self._change_counts = _new_change_counts()
        self._change_details = []
        self._rename_sources = set()
        # Two-way sync will not start against a Pawsey prefix that doesn't
        # exist yet - create it first (folder marker only, no data). Copy and
        # mirror get the same on a real run: rclone never creates the source
        # ROOT at the destination, so a source folder that is itself empty
        # would otherwise leave nothing on Pawsey (see _ensure_remote_path).
        # Skipped for a preview so a dry-run still writes nothing at all.
        dry = bool(self.dry_run.get()) if hasattr(self, "dry_run") else False
        if actual_mode == "bisync" or not dry:
            self._ensure_remote_path(dest)
        cmd = self._build_rclone_cmd(actual_mode, src, dest,
                                     bisync_resync=bisync_resync)
        self.current_transfer_cmd = cmd
        # Remember params for scheduled auto-sync (two-way only)
        self._autosync_params = {
            "src": src, "dest": dest, "mode": actual_mode,
            "resume_id": resume_id,
        }

        # ---- Open a per-transfer disk log (full verbose stream) ----
        try:
            log_path = TRANSFER_LOG_DIR / f"transfer_{resume_id}.log"
            self._transfer_log_fh = open(log_path, "a", encoding="utf-8")
            self._transfer_log_fh.write(
                f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} ===\n"
                f"$ {' '.join(_quote(a) for a in cmd)}\n\n")
            self._transfer_log_fh.flush()
        except Exception:
            self._transfer_log_fh = None

        # ---- Spawn rclone ----
        self._append_output(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
        self._append_output(f"$ {' '.join(_quote(a) for a in cmd)}\n\n")
        self._launch_rclone(cmd)

    def _pick_resume_dialog(self) -> Optional[dict]:
        """Show a dialog letting the user pick which incomplete transfer to
        resume. Returns the selected entry dict, or None if cancelled / empty."""
        incomplete = ResumeStore.list_incomplete()
        if not incomplete:
            messagebox.showinfo(
                APP_NAME,
                "There are no incomplete transfers to resume.\n\n"
                "Start a new transfer in Copy or Mirror sync mode.")
            return None

        win = tk.Toplevel(self.root)
        win.title("Resume previous transfer")
        win.geometry("1000x460")
        win.transient(self.root)
        win.grab_set()
        win.columnconfigure(0, weight=1)
        win.rowconfigure(1, weight=1)

        ttk.Label(
            win,
            text=f"{len(incomplete)} incomplete transfer(s). "
                 f"Select one to resume:",
            style="Big.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))

        cols = ("started", "status", "mode", "src", "dest", "notes", "progress")
        tree = ttk.Treeview(win, columns=cols, show="headings",
                            selectmode="browse", height=14)
        for c, w in zip(cols, [140, 110, 70, 200, 200, 200, 130]):
            tree.heading(c, text=c.title())
            tree.column(c, width=w, anchor="w")
        tree.grid(row=1, column=0, sticky="nsew", padx=10)
        sb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        sb.grid(row=1, column=1, sticky="ns", pady=0)
        tree.configure(yscrollcommand=sb.set)

        for e in incomplete:
            tree.insert("", "end", iid=e["id"], values=(
                e.get("started_at", "")[:16].replace("T", " "),
                e.get("status", ""),
                e.get("mode", ""),
                e.get("src", "")[-60:],
                e.get("dest", ""),
                (e.get("notes", "") or "")[:120].replace("\n", " | "),
                e.get("last_progress", "")[:30],
            ))
        # Pre-select the most recent
        tree.selection_set(incomplete[0]["id"])

        chosen: list[Optional[dict]] = [None]

        def on_ok():
            sel = tree.selection()
            if not sel:
                return
            for ent in incomplete:
                if ent["id"] == sel[0]:
                    chosen[0] = ent
                    break
            win.destroy()

        def on_cancel():
            win.destroy()

        def on_forget():
            sel = tree.selection()
            if not sel:
                return
            id_ = sel[0]
            if not messagebox.askyesno(
                    "Forget transfer",
                    f"Remove this transfer from the resume list?\n\n"
                    f"id: {id_}\n\n"
                    f"This does NOT delete anything on Pawsey - it just hides "
                    f"this entry so you stop seeing it here.",
                    parent=win):
                return
            ResumeStore.update(id_, status="completed")  # hide it
            tree.delete(id_)

        bar = ttk.Frame(win)
        bar.grid(row=2, column=0, sticky="ew", padx=10, pady=10)
        ttk.Button(bar, text="Resume selected", command=on_ok).pack(side="right")
        ttk.Button(bar, text="Cancel", command=on_cancel).pack(side="right", padx=8)
        ttk.Button(bar, text="Forget selected", command=on_forget,
                   style="Danger.TButton").pack(side="left")

        tree.bind("<Double-1>", lambda e: on_ok())
        win.wait_window()
        return chosen[0]

    def _launch_rclone(self, cmd: list[str]) -> None:
        """Internal: actually spawn the rclone subprocess and start the reader."""
        # With a --backup-dir in force (recycle bin, or "keep both copies")
        # rclone implements a DELETE as a move into the backup prefix, so the
        # change sniffer must not read every move as a rename. Recorded here
        # because this is the one funnel every launch path goes through.
        self._backup_dir_active = any(
            str(a).startswith("--backup-dir") for a in cmd)
        try:
            self.proc = subprocess.Popen(cmd, **_subprocess_kwargs())
        except FileNotFoundError:
            messagebox.showerror(APP_NAME, "rclone executable not found on PATH.")
            self._cleanup_transfer_state(failed=True)
            return
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Failed to start rclone:\n{e}")
            self._cleanup_transfer_state(failed=True)
            return

        self.transfer_active = True
        self.stop_btn.configure(state="normal")
        self.status_var.set("Transfer running...")
        self.progress["value"] = 0
        self.progress_label.configure(text="Starting...")

        # Engage OS sleep prevention
        prevent_sleep(True)

        # Begin heartbeat
        self._write_heartbeat()
        self._heartbeat_after_id = self.root.after(
            HEARTBEAT_INTERVAL_MS, self._heartbeat_tick)

        # Reader thread
        self.proc_thread = threading.Thread(
            target=self._reader_thread, args=(self.proc,), daemon=True)
        self.proc_thread.start()

    def _stop_transfer(self) -> None:
        if not self.transfer_active:
            return
        if not messagebox.askyesno(
                APP_NAME,
                "Stop the current transfer?\n"
                "Already-uploaded files stay on Pawsey; partial files\n"
                "will resume from scratch when you Resume.\n\n"
                "Auto-restart will be disabled for this transfer."):
            return
        self.user_stopped = True
        # Cancel any pending restart attempt
        if self.restart_after_id:
            try:
                self.root.after_cancel(self.restart_after_id)
            except Exception:
                pass
            self.restart_after_id = None
        stop_process(self.proc)

    def _reader_thread(self, proc: subprocess.Popen) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self.proc_queue.put(line.rstrip("\n"))
        except Exception as e:
            self.proc_queue.put(f"[reader error] {e}")
        finally:
            proc.wait()
            self.proc_queue.put(f"__DONE__:{proc.returncode}")

    def _drain_output(self) -> None:
        try:
            while True:
                line = self.proc_queue.get_nowait()
                if line.startswith("__DONE__:"):
                    rc = int(line.split(":", 1)[1])
                    self._on_transfer_finished(rc)
                else:
                    self._handle_progress_line(line)
                    self._sniff_auth_failure(line)
                    self._sniff_change_event(line)
                    self._append_output(line + "\n")
        except queue.Empty:
            pass
        self.root.after(120, self._drain_output)

    def _sniff_change_event(self, line: str) -> None:
        """Classify a per-file (or per-folder) operation from rclone's log.

        Two log levels matter:

          INFO    a real operation that happened - 'name: Copied (new)',
                  'name: Deleted', 'name: Renamed from "old"' …
          NOTICE  what a --dry-run WOULD have done - 'name: Skipped copy as
                  --dry-run is set'. A preview run emits ONLY these, so while
                  this parser looked at INFO alone every preview finished with
                  a tally of zero and the app announced "No changes - both
                  sides were already in sync" no matter how much the sync
                  actually had to do. That is the bug this handles.

        The periodic stats blocks ('Deleted:  1 (files)', 'Renamed:  1',
        'Transferred: …') and bisync's planning lines ('- Path1 File is new',
        'Queue copy …', 'N changes:') are skipped - otherwise the same change
        gets counted many times. A rename is logged by rclone as a server-side
        copy + a delete + a 'Renamed from' line; we count it once (as renamed)
        and skip the delete of the rename's source so it isn't double-counted.
        """
        line = ANSI_ESCAPE_RE.sub("", line)
        m = LOG_LEVEL_RE.search(line)
        if not m:
            return  # stats blocks / progress lines carry no level marker
        # Skip bisync planning + summary lines (they duplicate the operations)
        if ("- Path1" in line or "- Path2" in line or "Queue " in line
                or "File is new" in line or "File was deleted" in line
                or "changes:" in line or "Making map" in line
                or "checking for diffs" in line):
            return
        after = line[m.end():].strip()
        low = after.lower()

        def record(kind: str, text: str):
            self._change_counts[kind] += 1
            if len(self._change_details) < 2000:
                tag = {"new": "ADD", "updated": "UPD", "deleted": "DEL",
                       "renamed": "REN", "dirs": "DIR"}[kind]
                self._change_details.append(f"[{tag}] {text}")

        # ---- Preview (--dry-run): "Skipped <action> as --dry-run is set" ----
        # rclone reports the action it declined to perform, so these are the
        # changes a real run would make.
        if "as --dry-run is set" in low:
            # Stamping a directory's modification time is bookkeeping, not a
            # content change - counting it would make every preview look busy.
            if "directory modification time" in low:
                return
            if "skipped update" in low:
                # e.g. 'Skipped update modification time as --dry-run is set'
                record("updated", after)
            elif "skipped copy" in low:
                # A preview cannot tell new from replaced, so both land here.
                record("new", after)
            elif "skipped delete" in low or "skipped purge" in low:
                record("deleted", after)
            elif "skipped move" in low or "skipped rename" in low:
                # A preview's move line carries no destination, so when a
                # backup-dir is in force we cannot tell a rename from the
                # move-instead-of-delete (or the backup of a file about to be
                # overwritten). Count it as a deletion: over-stating the
                # destructive column is the safe direction, and the full text
                # of every event is listed under "Show last changes".
                record("deleted" if getattr(self, "_backup_dir_active", False)
                       else "renamed", after)
            elif "skipped make directory" in low or "skipped mkdir" in low:
                record("dirs", after)
            return

        # Server-side copy is the 'copy' half of a track-renames rename.
        # Record the source name so its later 'Deleted' isn't counted, and
        # don't count the copy itself (the 'Renamed from' line counts it).
        if "copied (server-side copy) to:" in low:
            src = after.split(":", 1)[0].strip()
            if src:
                self._rename_sources.add(src)
            return
        if "renamed from" in low:
            record("renamed", after)
            return
        if "moved to:" in low or "moved (server-side)" in low:
            # Unlike a preview, a real run names the destination - so a move
            # INTO the recycle bin / backup prefix can be identified for what
            # it is: a soft delete, or the backup of a file being overwritten.
            # Neither is a rename.
            tail = low.split("moved", 1)[1]
            if any(p in tail for p in APP_MANAGED_PREFIXES):
                record("deleted", after)
            else:
                record("renamed", after)
            return
        # A genuine deletion - unless it's the source side of a rename/move.
        if low.endswith(": deleted") or low == "deleted" or low.endswith(" deleted"):
            fname = after.rsplit(":", 1)[0].strip() if ":" in after else after
            if fname in self._rename_sources:
                return
            record("deleted", after)
            return
        if "copied (new)" in low:
            record("new", after)
            return
        if ("copied (replaced existing)" in low or "copied (replaced)" in low
                or after.endswith(": Updated") or ": updated" in low):
            record("updated", after)
            return
        # Empty-folder operations (only logged once --create-empty-src-dirs is
        # in play, which it now always is). These are the changes that used to
        # be invisible: a folder added or removed with no files in it.
        # rclone's actual wording for a real run is "<folder>: Making
        # directory" (the preview says "Skipped make directory"); without
        # "making directory" here every real run reported "folders: 0".
        if ("created directory" in low or "made directory" in low
                or "making directory" in low
                or "removing directory" in low or "removed directory" in low
                or "creating directory" in low):
            record("dirs", after)
            return

    def _handle_progress_line(self, line: str) -> None:
        m = PROGRESS_RE.search(line)
        if m:
            done, total, pct = m.group(1), m.group(2), int(m.group(3))
            self.progress["value"] = pct
            text = f"{done} / {total}  ({pct}%)"
            self.progress_label.configure(text=text)
            self._last_progress_text = text

    def _sniff_auth_failure(self, line: str) -> None:
        line = ANSI_ESCAPE_RE.sub("", line)
        if not self.auth_failure_seen:
            for pat in AUTH_FAILURE_PATTERNS:
                if pat in line:
                    self.auth_failure_seen = True
                    break
        if not self.fatal_error_seen:
            low = line.lower()
            for pat in FATAL_NONRETRYABLE_PATTERNS:
                if pat.lower() in low:
                    self.fatal_error_seen = True
                    break
        low = line.lower()
        if "too many deletes" in low or "safety abort" in low:
            self.delete_cap_seen = True
        if "empty current path" in low and "listing" in low:
            self.empty_listing_seen = True
        # Two-way sync cannot run without its baseline listings. This is the
        # single most common reason a bisync refuses to do anything, and the
        # fix is a --resync rather than a retry.
        for pat in BISYNC_NEEDS_RESYNC_PATTERNS:
            if pat in low:
                self.bisync_needs_resync_seen = True
                break

    def _clear_output(self) -> None:
        """Clear the on-screen output only. The per-transfer log file on disk
        keeps the entire history - this just gives a clean screen so the next
        run's output is easy to read."""
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", "end")
        self.output_text.insert(
            "end",
            f"--- screen cleared at {datetime.now():%H:%M:%S}; "
            f"the complete log is still saved on disk in {TRANSFER_LOG_DIR} ---\n")
        self.output_text.configure(state="disabled")
        self.status_var.set("Output cleared (full log kept on disk).")

    def _append_output(self, text: str) -> None:
        # Always persist the full stream to the per-transfer disk log
        if self._transfer_log_fh is not None:
            try:
                self._transfer_log_fh.write(text)
                # Flush periodically would be ideal; rely on OS for now
            except Exception:
                pass

        # On-screen widget is bounded so memory stays in check on long runs
        self.output_text.configure(state="normal")
        self.output_text.insert("end", text)
        end = self.output_text.index("end-1c")
        try:
            line_count = int(end.split(".")[0])
        except Exception:
            line_count = 0
        if line_count > MAX_OUTPUT_LINES:
            cut_at = line_count - TRIM_OUTPUT_TO_LINES
            self.output_text.delete("1.0", f"{cut_at}.0")
            # Stamp a marker so user knows we trimmed
            self.output_text.insert(
                "1.0",
                f"--- [older output trimmed at {datetime.now():%H:%M:%S}; "
                f"full log on disk in {TRANSFER_LOG_DIR}] ---\n")
        self.output_text.see("end")
        self.output_text.configure(state="disabled")

    # --- Heartbeat ------------------------------------------------------
    def _write_heartbeat(self) -> None:
        data = {
            "alive": bool(self.transfer_active),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "resume_id": self.current_resume_id,
            "source": self.current_src,
            "destination": self.current_dest,
            "mode": self.current_mode,
            "last_progress": self._last_progress_text,
            "restart_count": self.restart_count,
            "user_stopped": self.user_stopped,
            "auth_failure_seen": self.auth_failure_seen,
        }
        try:
            HEARTBEAT_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _heartbeat_tick(self) -> None:
        if not self.transfer_active:
            return
        self._write_heartbeat()
        if self.current_resume_id:
            ResumeStore.update(
                self.current_resume_id,
                last_progress=self._last_progress_text,
                status="in_progress",
                restart_count=self.restart_count,
            )
        self._heartbeat_after_id = self.root.after(
            HEARTBEAT_INTERVAL_MS, self._heartbeat_tick)

    # --- Cleanup --------------------------------------------------------
    def _cleanup_transfer_state(self, failed: bool = False) -> None:
        """Tear down per-transfer side effects (sleep lock, heartbeat, log file)."""
        prevent_sleep(False)
        if self._heartbeat_after_id:
            try:
                self.root.after_cancel(self._heartbeat_after_id)
            except Exception:
                pass
            self._heartbeat_after_id = None
        # Write final heartbeat
        self.transfer_active = False
        self._write_heartbeat()
        if self._transfer_log_fh is not None:
            try:
                self._transfer_log_fh.flush()
                self._transfer_log_fh.close()
            except Exception:
                pass
            self._transfer_log_fh = None

    def _on_transfer_finished(self, rc: int) -> None:
        self.transfer_active = False
        self.stop_btn.configure(state="disabled")
        status_text = "completed" if rc == 0 else f"failed (exit {rc})"
        self.progress_label.configure(text=f"Transfer {status_text}.")
        self.status_var.set(f"Transfer {status_text}.")
        self._append_output(f"\n=== Transfer {status_text} ===\n")
        self._append_log({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "status": "finished",
            "exit_code": rc,
            "resume_id": self.current_resume_id,
        })

        # Determine auto-restart eligibility
        auto_restart = bool(self.cfg.get("auto_restart", DEFAULT_AUTO_RESTART))
        max_restarts = int(self.cfg.get("max_restart_attempts", MAX_RESTART_ATTEMPTS))
        backoff = int(self.cfg.get("restart_backoff_seconds", RESTART_BACKOFF_SECONDS))

        if rc == 0:
            # Success: mark resume as completed, refresh UI, release locks
            if self.current_resume_id:
                ResumeStore.update(self.current_resume_id, status="completed")
            # If this was a two-way sync, record the pair as initialised so
            # future runs don't re-run the baseline (--resync).
            pending = getattr(self, "_pending_bisync_mark", None)
            if pending:
                self._bisync_mark_initialised(pending[0], pending[1])
                self._pending_bisync_mark = None
            self._cleanup_transfer_state()

            # Build + record the change summary. A preview reports what WOULD
            # change, so it is worded in the conditional - saying "Added 12"
            # after a dry-run would be a lie.
            was_preview = bool(getattr(self, "dry_run", None)
                               and self.dry_run.get())
            c = self._change_counts
            if was_preview:
                summary = (f"WOULD add {c['new']}, update {c['updated']}, "
                           f"rename/move {c['renamed']}, delete {c['deleted']}, "
                           f"create/remove {c['dirs']} folder(s)")
            else:
                summary = (f"Added {c['new']}, updated {c['updated']}, "
                           f"renamed/moved {c['renamed']}, "
                           f"deleted {c['deleted']}, "
                           f"folders created/removed {c['dirs']}")
            self._append_output(f"\n=== Changes this run: {summary} ===\n")
            self._append_log({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "operation": "change_summary",
                "resume_id": self.current_resume_id,
                "destination": self.current_dest,
                "preview_only": was_preview,
                "added": c["new"], "updated": c["updated"],
                "renamed": c["renamed"], "deleted": c["deleted"],
                "folders": c["dirs"],
            })

            total_changes = sum(c.values())
            self._last_change_summary = summary
            self._last_change_details = list(self._change_details)

            # Schedule next auto-sync if enabled and this was a two-way sync
            scheduled = self._maybe_schedule_autosync()

            # Will the authoritative comparison run straight after this?
            will_verify = bool(getattr(self, "verify_both", None) is not None
                               and self.verify_both.get() and not was_preview)

            if total_changes:
                extra = f"\n\nChanges: {summary}"
                if was_preview:
                    extra += ("\n\nThis was a PREVIEW - nothing was transferred."
                              "\nUntick 'Preview only (dry-run)' in Sync options "
                              "to apply these changes.")
            elif was_preview:
                extra = ("\n\nPreview found nothing to do: rclone would not "
                         "add, update, rename or delete anything, and no "
                         "folders would change.")
            else:
                # Only claim the two sides match if something is going to
                # check. rclone's log tells us what it DID, which is not the
                # same as proof that the two sides now agree.
                extra = "\n\nrclone reported no file or folder operations."
                if will_verify:
                    extra += ("\nVerifying both sides now to confirm they "
                              "really do match…")
                else:
                    extra += ("\nTo confirm the two sides genuinely match, "
                              "click 'Verify both sides (check)'.")
            if scheduled:
                extra += (f"\n\nAuto-sync is ON - next run in "
                          f"{self.cfg.get('autosync_interval_min', 15)} min.")
            messagebox.showinfo(
                APP_NAME,
                ("Preview completed successfully." if was_preview
                 else "Transfer completed successfully.") + extra)
            # Refresh the storage tree if loaded
            if hasattr(self, "storage_tree") and self.storage_tree.get_children():
                self._storage_refresh_root()

            # v2.1: auto-verify both sides after a real (non-preview) transfer.
            if will_verify:
                self._append_output(
                    "\n=== Auto-verify (Verify both sides is ON) ===\n")
                self.root.after(200, self._verify_check)

        elif self.user_stopped:
            if self.current_resume_id:
                ResumeStore.update(self.current_resume_id, status="stopped")
            self._cleanup_transfer_state()
            messagebox.showinfo(APP_NAME,
                                "Transfer stopped. You can resume it from the "
                                "Resume previous transfer dialog later.")

        elif self.auth_failure_seen:
            if self.current_resume_id:
                ResumeStore.update(self.current_resume_id, status="failed_auth")
            self._cleanup_transfer_state()
            messagebox.showerror(
                APP_NAME,
                "Transfer stopped due to an AUTHENTICATION failure.\n\n"
                "Possible causes:\n"
                "  - Pawsey access keys have been suspended or rotated.\n"
                "  - The bucket is owned by a different project.\n\n"
                "Open Settings, update the access key + secret, then\n"
                "click 'Test connection' before resuming.")

        elif self.fatal_error_seen:
            if self.current_resume_id:
                ResumeStore.update(self.current_resume_id, status="failed")
            self._cleanup_transfer_state()
            if self.delete_cap_seen:
                messagebox.showerror(
                    APP_NAME,
                    "Two-way sync stopped by the deletion safety cap.\n\n"
                    "It would delete more than your configured limit "
                    f"({self._delete_cap_percent()}%) of the files on one side. "
                    "This usually means you removed or reorganised a lot of "
                    "files (e.g. the earlier cleanup), and the sync is trying "
                    "to apply that.\n\n"
                    "If those deletions ARE intended, in Sync options either:\n"
                    "  - raise the percentage in 'Abort... if it would delete\n"
                    "    more than N% of files', or\n"
                    "  - untick that box to remove the limit,\n"
                    "then start the sync again.\n\n"
                    "If they are NOT intended, do not change it - check the "
                    "folders first. Auto-restart was skipped so it won't keep "
                    "retrying.")
            elif self.empty_listing_seen:
                messagebox.showerror(
                    APP_NAME,
                    "Two-way sync stopped: the Pawsey side of this pair came "
                    "back EMPTY.\n\n"
                    "Almost always this means the destination path doesn't "
                    "actually contain your data. The most common cause is a "
                    "doubled folder name - e.g. Path '2024/test-syn' WITH 'Put "
                    "the source folder itself at the destination' ticked points "
                    "at '2024/test-syn/test-syn', which is empty.\n\n"
                    "Check the blue 'Files will be placed under:' line on the "
                    "Transfer tab - it must point at where your files really "
                    "are. Fix the Path or the checkbox so it matches, then run "
                    "again (the first run will offer a baseline resync).")
            elif self.bisync_needs_resync_seen:
                # bisync keeps a record of what both sides looked like after
                # the last successful run. Without it, it cannot tell "you
                # deleted this file" apart from "the other side gained it",
                # so it refuses to act rather than guess. Offer the rebuild.
                self._bisync_forget_initialised(self.current_src,
                                                self.current_dest)
                if messagebox.askyesno(
                        APP_NAME,
                        "Two-way sync needs a fresh BASELINE before it can "
                        "run.\n\n"
                        "rclone remembers what both sides looked like at the "
                        "end of the last successful two-way sync. That record "
                        "is missing or unusable - usually because this is the "
                        "first run on this machine, a previous run was "
                        "interrupted, or the rclone cache was cleared. Without "
                        "it rclone cannot tell a deletion apart from a new "
                        "file, so it stops instead of guessing.\n\n"
                        "Rebuild the baseline now?\n\n"
                        "The rebuild MERGES the two sides: every file on "
                        "either side is copied to the other, and where the "
                        "same file exists on both but differs, the LOCAL copy "
                        "wins. Nothing is deleted.\n\n"
                        "  Yes = rebuild the baseline now\n"
                        "  No  = change nothing (you can start the sync again "
                        "later; it will offer the baseline)"):
                    self._rebuild_bisync_baseline()
            else:
                messagebox.showerror(
                    APP_NAME,
                    "Transfer stopped due to an error that retrying won't fix.\n\n"
                    "Most likely causes:\n"
                    "  - The bucket or path is misspelled (check the Bucket and\n"
                    "    Path fields - e.g. 'dpird-appn', not 'dprid-appn').\n"
                    "  - A two-way sync aborted and now needs a fresh baseline.\n\n"
                    "Fix the bucket/path, then start again. Auto-restart was\n"
                    "skipped so it won't keep retrying a doomed command.")

        elif auto_restart and self.restart_count < max_restarts:
            # Schedule a restart with backoff
            self.restart_count += 1
            if self.current_resume_id:
                ResumeStore.update(self.current_resume_id,
                                   status="restarting",
                                   restart_count=self.restart_count)
            msg = (f"rclone exited with code {rc}. "
                   f"Auto-restart {self.restart_count}/{max_restarts} in "
                   f"{backoff}s…")
            self._append_output(f"\n!!! {msg}\n")
            self.status_var.set(msg)
            self.progress_label.configure(text=msg)
            self.restart_after_id = self.root.after(
                backoff * 1000, self._auto_restart_now)
            # Note: we DO NOT cleanup transfer state - we want sleep lock
            # and heartbeat to remain active during the wait so the
            # machine doesn't sleep before we restart.
            self.transfer_active = True   # logically still transferring

        else:
            # Failure without restart (auto_restart off or out of attempts)
            if self.current_resume_id:
                ResumeStore.update(self.current_resume_id, status="failed")
            self._cleanup_transfer_state()
            messagebox.showwarning(
                APP_NAME,
                f"Transfer ended with exit code {rc}.\n"
                f"Restart attempts exhausted ({self.restart_count}/{max_restarts}) "
                f"or auto-restart is disabled.\n\n"
                f"Use 'Resume previous transfer' to try again.")

        self._refresh_history()
        # Clear the notes box once the run is truly finished (not while a
        # restart is pending). The note is already saved to the log/resume
        # store, so the box can start fresh for the next transfer.
        if not self.transfer_active:
            try:
                self.notes_text.delete("1.0", "end")
            except Exception:
                pass

    def _auto_restart_now(self) -> None:
        """Called by root.after when the restart backoff elapses."""
        self.restart_after_id = None
        if self.user_stopped:
            return
        if not self.current_transfer_cmd:
            return
        self._append_output(
            f"\n=== Auto-restart attempt {self.restart_count} "
            f"at {datetime.now():%Y-%m-%d %H:%M:%S} ===\n\n")
        self._launch_rclone(self.current_transfer_cmd)

    # ----- Scheduled auto-sync -------------------------------------------
    def _on_autosync_toggle(self) -> None:
        """Persist the interval and cancel any pending schedule when turned off."""
        try:
            interval = int(self.autosync_interval.get().strip())
            if interval >= 1:
                self.cfg.set("autosync_interval_min", interval)
                self.cfg.save()
        except (TypeError, ValueError):
            pass
        if not self.autosync_enabled.get():
            self._cancel_autosync()
            self.status_var.set("Auto-sync disabled.")
        else:
            self.status_var.set(
                "Auto-sync enabled - it starts after your next successful "
                "Two-way sync.")

    def _maybe_schedule_autosync(self) -> bool:
        """If auto-sync is enabled and the last run was a two-way sync,
        schedule the next run. Returns True if scheduled."""
        if not (hasattr(self, "autosync_enabled") and self.autosync_enabled.get()):
            return False
        params = self._autosync_params
        if not params or params.get("mode") != "bisync":
            return False
        try:
            interval = int(self.cfg.get("autosync_interval_min", 15))
        except (TypeError, ValueError):
            interval = 15
        interval = max(1, interval)
        self._cancel_autosync()
        self._autosync_after_id = self.root.after(
            interval * 60 * 1000, self._run_autosync)
        self.status_var.set(f"Auto-sync scheduled in {interval} min.")
        return True

    def _cancel_autosync(self) -> None:
        if self._autosync_after_id:
            try:
                self.root.after_cancel(self._autosync_after_id)
            except Exception:
                pass
            self._autosync_after_id = None

    def _run_autosync(self) -> None:
        """Fire a scheduled two-way sync using the remembered parameters."""
        self._autosync_after_id = None
        if self.transfer_active:
            # Something else is running; try again shortly
            self._autosync_after_id = self.root.after(60 * 1000, self._run_autosync)
            return
        params = self._autosync_params
        if not params:
            return
        src, dest = params["src"], params["dest"]
        if not Path(src).exists():
            self.status_var.set("Auto-sync skipped: source folder missing.")
            return
        self._append_output(
            f"\n=== Scheduled auto-sync at {datetime.now():%Y-%m-%d %H:%M:%S} ===\n\n")
        # Reset per-run state
        self.current_resume_id = params.get("resume_id")
        self.current_src = src
        self.current_dest = dest
        self.current_mode = "bisync"
        self.user_stopped = False
        self.auth_failure_seen = False
        self.fatal_error_seen = False
        self.delete_cap_seen = False
        self.empty_listing_seen = False
        self.bisync_needs_resync_seen = False
        self.restart_count = 0
        self._change_counts = _new_change_counts()
        self._change_details = []
        self._rename_sources = set()
        self._ensure_remote_path(dest)
        cmd = self._build_rclone_cmd("bisync", src, dest, bisync_resync=False)
        self.current_transfer_cmd = cmd
        try:
            log_path = TRANSFER_LOG_DIR / f"transfer_{self.current_resume_id}.log"
            self._transfer_log_fh = open(log_path, "a", encoding="utf-8")
        except Exception:
            self._transfer_log_fh = None
        self._launch_rclone(cmd)

    def _show_last_changes(self) -> None:
        """Popup listing the detailed change events from the last run."""
        details = getattr(self, "_last_change_details", None)
        summary = getattr(self, "_last_change_summary", "No sync run yet.")
        win = tk.Toplevel(self.root)
        win.title("Changes from last sync")
        win.geometry("820x520")
        ttk.Label(win, text=summary, style="Big.TLabel").pack(
            anchor="w", padx=10, pady=(10, 4))
        txt = scrolledtext.ScrolledText(
            win, wrap="none",
            font=("Consolas" if IS_WINDOWS else "Monospace", 9))
        txt.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        if details:
            txt.insert("1.0", "\n".join(details))
        else:
            txt.insert("1.0", "No file-level changes recorded for the last run.\n"
                              "(Either nothing changed, or the run hasn't finished.)")
        txt.configure(state="disabled")
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 10))

    def _append_log(self, entry: dict) -> None:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            print(f"Log write failed: {e}", file=sys.stderr)

    # ------------------------------------------------------------- Buckets
    def _build_buckets_tab(self) -> None:
        t = self.tab_buckets
        t.columnconfigure(0, weight=1)
        t.rowconfigure(2, weight=1)

        top = ttk.Frame(t)
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Remote:").grid(row=0, column=0, sticky="w")
        self.buckets_remote = tk.StringVar(value=self.cfg.get("remote_name"))
        self.buckets_remote_combo = ttk.Combobox(top, textvariable=self.buckets_remote)
        self.buckets_remote_combo.grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(top, text="Refresh", command=self._refresh_buckets).grid(row=0, column=2)

        actions = ttk.Frame(t)
        actions.grid(row=1, column=0, sticky="ew", padx=10)
        ttk.Button(actions, text="Create bucket…",
                   command=self._create_bucket).pack(side="left")
        ttk.Button(actions, text="Delete bucket…",
                   command=self._delete_bucket, style="Danger.TButton").pack(side="left", padx=8)

        cols = ("Bucket name",)
        self.bucket_tree = ttk.Treeview(t, columns=cols, show="headings", height=18)
        for c in cols:
            self.bucket_tree.heading(c, text=c)
            self.bucket_tree.column(c, anchor="w", width=400)
        self.bucket_tree.grid(row=2, column=0, sticky="nsew", padx=10, pady=10)
        sb = ttk.Scrollbar(t, orient="vertical", command=self.bucket_tree.yview)
        sb.grid(row=2, column=1, sticky="ns", pady=10)
        self.bucket_tree.configure(yscrollcommand=sb.set)

        self._refresh_buckets()

    def _refresh_buckets(self) -> None:
        self.buckets_remote_combo["values"] = RcloneRemote.list_remotes()
        remote = self.buckets_remote.get().strip()
        for item in self.bucket_tree.get_children():
            self.bucket_tree.delete(item)
        if not remote:
            return
        rc, out = run_rclone_capture(["lsjson", f"{remote}:", "--dirs-only"], timeout=60)
        if rc != 0:
            messagebox.showerror(APP_NAME, f"Could not list buckets:\n{out}")
            return
        try:
            data = json.loads(out or "[]")
        except json.JSONDecodeError:
            data = []
        for entry in sorted(data, key=lambda e: e.get("Name", "").lower()):
            name = entry.get("Name") or entry.get("Path")
            self.bucket_tree.insert("", "end", values=(name,))
        self.status_var.set(f"{len(data)} bucket(s) on {remote}")

    def _create_bucket(self) -> None:
        remote = self.buckets_remote.get().strip()
        if not remote:
            messagebox.showerror(APP_NAME, "Pick a remote first.")
            return
        name = simpledialog.askstring(
            "Create bucket",
            "New bucket name\n\n(lowercase letters, digits, hyphens; "
            "3-63 chars; must not start/end with hyphen)",
            parent=self.root,
        )
        if not name:
            return
        name = name.strip()
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{1,61}[a-z0-9])?", name):
            messagebox.showerror(APP_NAME,
                                 "Invalid bucket name. Use 3-63 lowercase letters/digits/hyphens.")
            return
        rc, out = run_rclone_capture(["mkdir", f"{remote}:{name}"], timeout=30)
        if rc == 0:
            messagebox.showinfo(APP_NAME, f"Bucket '{name}' created.")
        else:
            messagebox.showerror(APP_NAME, f"Create failed:\n{out}")
        self._refresh_buckets()

    def _delete_bucket(self) -> None:
        sel = self.bucket_tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select a bucket in the list first.")
            return
        name = self.bucket_tree.item(sel[0])["values"][0]
        remote = self.buckets_remote.get().strip()

        warn = (
            f"⚠  PERMANENT DELETION  ⚠\n\n"
            f"This will delete bucket '{name}' on remote '{remote}'\n"
            f"AND ALL OF ITS CONTENTS.\n\n"
            f"This cannot be undone. There is no recycle bin on Acacia.\n\n"
            f"Continue?"
        )
        if not messagebox.askyesno("Confirm deletion", warn, icon="warning"):
            return

        typed = simpledialog.askstring(
            "Type bucket name to confirm",
            f"To confirm, type the bucket name exactly:\n\n   {name}",
            parent=self.root,
        )
        if typed != name:
            messagebox.showinfo(APP_NAME, "Name did not match. Deletion cancelled.")
            return

        self.status_var.set(f"Deleting bucket '{name}'...")
        rc, out = run_rclone_capture(
            ["purge", f"{remote}:{name}", *S3_DELETE_FLAGS], timeout=600)
        if rc == 0:
            messagebox.showinfo(APP_NAME, f"Bucket '{name}' deleted.")
        else:
            messagebox.showerror(APP_NAME, f"Delete failed:\n{out}")
        self._refresh_buckets()

    # ------------------------------------------------------------- Storage
    #
    # The Storage tab is a lightweight file-manager for the Pawsey remote.
    # It uses a lazy-loaded ttk.Treeview: only the immediate children of an
    # expanded node are fetched, and sizes are computed in background threads
    # so the UI never blocks.
    #
    # iid scheme: each tree row's iid is its remote path WITHOUT the
    # `remote:` prefix.  e.g. iid "sample-data" is a bucket;
    # iid "sample-data/GCPs (1)/file.csv" is a file.  Placeholders use the
    # suffix "::LOAD".
    # -----------------------------------------------------------------

    def _bg_call(self, work, on_done) -> None:
        """Run `work()` in a daemon thread and deliver the result to
        `on_done(result, exc)` on the Tk main thread.

        UI updates must happen on the main thread, so the worker pushes
        the completion onto self._bg_queue and the periodic drain on the
        main thread (see _drain_bg_queue) actually invokes on_done.
        """
        def runner():
            try:
                res = work()
                self._bg_queue.put((on_done, res, None))
            except Exception as e:
                self._bg_queue.put((on_done, None, e))
        threading.Thread(target=runner, daemon=True).start()

    def _drain_bg_queue(self) -> None:
        """Poll the background-result queue on the main thread."""
        try:
            while True:
                cb, result, err = self._bg_queue.get_nowait()
                try:
                    cb(result, err)
                except Exception as e:
                    print(f"Background callback error: {e}", file=sys.stderr)
                    _diag("background callback crash:\n"
                          + traceback.format_exc())
        except queue.Empty:
            pass
        self.root.after(80, self._drain_bg_queue)

    # ----- password gate (v2.1) ------------------------------------------
    def _require_password(self, action: str) -> bool:
        """Prompt for the app password before a protected action.

        Returns True if the correct password was entered, False if the user
        cancelled or got it wrong. `action` is a short human description used
        in the prompt, e.g. "turn off Preview only" or "select Mirror mode".
        """
        hint = ("  (default is 'appn' — change it in Settings)"
                if self.cfg.is_default_password() else "")
        pw = simpledialog.askstring(
            "Password required",
            f"Enter the app password to {action}.{hint}",
            show="•", parent=self.root)
        if pw is None:            # user cancelled
            return False
        if self.cfg.check_password(pw):
            return True
        messagebox.showerror(APP_NAME, "Incorrect password. No change was made.")
        return False

    def _bg_stream(self, args, on_line, on_done, timeout: int = 86400,
                   pre: Optional[list] = None) -> None:
        """Run rclone in a daemon thread, streaming each output line to
        `on_line(line)` on the Tk main thread, then delivering the final
        (returncode, tail_output) to `on_done(result, err)` on the main thread.

        Used by the Storage tab so copy / move / delete show live progress
        instead of appearing to hang until the whole operation finishes.

        `pre` is an optional list of short rclone arg-lists to run (and log)
        first, in the same worker thread - e.g. the `mkdir` that makes sure a
        folder destination exists before a copy/move that would otherwise
        skip an empty folder. Their exit codes do not affect the result.
        """
        def runner():
            tail: list[str] = []
            try:
                for pre_args in (pre or []):
                    try:
                        _rc, pre_out = run_rclone_capture(
                            list(pre_args), timeout=600)
                    except Exception as e:      # never block the main op
                        pre_out = str(e)
                    for pl in pre_out.splitlines():
                        if pl.strip():
                            self._bg_queue.put(
                                (lambda _r, _e, l=pl + "\n": on_line(l),
                                 None, None))
                proc = subprocess.Popen(
                    [RCLONE_EXE, *args], **_subprocess_kwargs())
                assert proc.stdout is not None
                for line in proc.stdout:
                    tail.append(line)
                    if len(tail) > 400:
                        tail.pop(0)
                    self._bg_queue.put(
                        (lambda _r, _e, l=line: on_line(l), None, None))
                proc.wait(timeout=timeout)
                rc = proc.returncode
                self._bg_queue.put((on_done, (rc, "".join(tail)), None))
            except FileNotFoundError:
                self._bg_queue.put((on_done, (127, "rclone not found"), None))
            except Exception as e:
                self._bg_queue.put((on_done, (1, str(e)), e))
        threading.Thread(target=runner, daemon=True).start()

    # ----- Storage tab progress helpers (v2.1) ---------------------------
    def _storage_progress_begin(self, title: str, determinate: bool = True,
                                maximum: int = 100) -> None:
        """Reveal the Storage progress panel and reset it for a new op."""
        if not hasattr(self, "storage_progress"):
            return
        self.storage_progressbar.stop()
        if determinate:
            self.storage_progressbar.configure(mode="determinate",
                                               maximum=maximum, value=0)
        else:
            self.storage_progressbar.configure(mode="indeterminate")
            self.storage_progressbar.start(12)
        self.storage_progress_label.configure(text=title)
        self.storage_op_output.configure(state="normal")
        self.storage_op_output.delete("1.0", "end")
        self.storage_op_output.insert(
            "end", f"=== {title}  ({datetime.now():%H:%M:%S}) ===\n")
        self.storage_op_output.configure(state="disabled")

    def _storage_progress_set(self, value: Optional[float] = None,
                              label: Optional[str] = None) -> None:
        if not hasattr(self, "storage_progressbar"):
            return
        if value is not None:
            self.storage_progressbar.configure(mode="determinate", value=value)
        if label is not None:
            self.storage_progress_label.configure(text=label)

    def _storage_progress_log(self, text: str) -> None:
        if not hasattr(self, "storage_op_output"):
            return
        self.storage_op_output.configure(state="normal")
        self.storage_op_output.insert("end", text if text.endswith("\n")
                                      else text + "\n")
        # Keep the on-screen widget bounded.
        end = self.storage_op_output.index("end-1c")
        try:
            lines = int(end.split(".")[0])
        except Exception:
            lines = 0
        if lines > 500:
            self.storage_op_output.delete("1.0", f"{lines - 400}.0")
        self.storage_op_output.see("end")
        self.storage_op_output.configure(state="disabled")

    def _storage_progress_done(self, label: str) -> None:
        if not hasattr(self, "storage_progressbar"):
            return
        self.storage_progressbar.stop()
        self.storage_progressbar.configure(mode="determinate", maximum=100,
                                           value=100)
        self.storage_progress_label.configure(text=label)
        self._storage_progress_log(f"— {label}")

    def _build_storage_tab(self) -> None:
        t = self.tab_storage
        t.columnconfigure(0, weight=1)
        t.rowconfigure(3, weight=1)

        # ----- Top: remote selector + search + refresh -----
        top = ttk.Frame(t)
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        top.columnconfigure(3, weight=1)

        ttk.Label(top, text="Remote:").grid(row=0, column=0, sticky="w")
        self.storage_remote = tk.StringVar(value=self.cfg.get("remote_name"))
        self.storage_remote_combo = ttk.Combobox(
            top, textvariable=self.storage_remote, width=18, state="readonly")
        self.storage_remote_combo.grid(row=0, column=1, sticky="w", padx=(4, 16))
        self.storage_remote_combo.bind(
            "<<ComboboxSelected>>", lambda e: self._storage_refresh_root())

        ttk.Label(top, text="Search:").grid(row=0, column=2, sticky="w")
        self.storage_search_var = tk.StringVar()
        se = ttk.Entry(top, textvariable=self.storage_search_var)
        se.grid(row=0, column=3, sticky="ew", padx=4)
        se.bind("<Return>", lambda e: self._storage_search())
        ttk.Button(top, text="Search",
                   command=self._storage_search).grid(row=0, column=4, padx=2)
        ttk.Button(top, text="Refresh tree",
                   command=self._storage_refresh_root).grid(row=0, column=5, padx=2)

        # ----- Brief usage hint -----
        ttk.Label(
            t,
            text="Click ▶ to expand a bucket or folder. Select an item (or "
                 "several with Ctrl+click / Shift+click), then use the buttons "
                 "below to copy / cut / paste, share a link, upload, download, "
                 "rename or delete. Notes are required for any change.",
            style="Muted.TLabel", wraplength=1040, justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))

        # ----- Summary line -----
        self.storage_summary = ttk.Label(
            t, text="(pick a remote and click 'Refresh tree')",
            style="Big.TLabel")
        self.storage_summary.grid(row=2, column=0, sticky="w", padx=10)

        # ----- The tree -----
        tree_frame = ttk.Frame(t)
        tree_frame.grid(row=3, column=0, sticky="nsew", padx=10, pady=(4, 6))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        self.storage_tree = ttk.Treeview(
            tree_frame,
            columns=("type", "size", "modified"),
            show="tree headings",
            selectmode="extended",
        )
        self.storage_tree.heading("#0", text="Name", anchor="w")
        self.storage_tree.heading("type", text="Type", anchor="w")
        self.storage_tree.heading("size", text="Size", anchor="e")
        self.storage_tree.heading("modified", text="Modified", anchor="w")
        self.storage_tree.column("#0", minwidth=240, width=480, stretch=True)
        self.storage_tree.column("type", minwidth=60, width=80, stretch=False, anchor="w")
        self.storage_tree.column("size", minwidth=80, width=110, stretch=False, anchor="e")
        self.storage_tree.column("modified", minwidth=120, width=140, stretch=False)
        self.storage_tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",
                            command=self.storage_tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.storage_tree.configure(yscrollcommand=vsb.set)

        self.storage_tree.bind("<<TreeviewOpen>>", self._on_tree_expand)
        self.storage_tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # ----- Selection label + freshness indicator -----
        selrow = ttk.Frame(t)
        selrow.grid(row=4, column=0, sticky="ew", padx=10)
        selrow.columnconfigure(0, weight=1)
        self.storage_selected_label = ttk.Label(
            selrow, text="No selection.", foreground="#444")
        self.storage_selected_label.grid(row=0, column=0, sticky="w")
        self.storage_freshness_label = ttk.Label(
            selrow, text="Tree not loaded yet.", foreground="#888")
        self.storage_freshness_label.grid(row=0, column=1, sticky="e")
        self._storage_loaded_at: Optional[datetime] = None
        self._storage_freshness_after_id: Optional[str] = None

        # ----- Notes + action buttons -----
        bottom = ttk.LabelFrame(
            t, text="Action note  (required for upload / new folder / delete)")
        bottom.grid(row=5, column=0, sticky="ew", padx=10, pady=(6, 10))
        bottom.columnconfigure(0, weight=1)

        self.storage_notes = scrolledtext.ScrolledText(bottom, height=2, wrap="word")
        self.storage_notes.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        # Row 1: organise on Pawsey (clipboard + share + rename)
        btns = ttk.Frame(bottom)
        btns.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 2))
        ttk.Button(btns, text="Copy",
                   command=self._storage_copy).pack(side="left", padx=2)
        ttk.Button(btns, text="Cut",
                   command=self._storage_cut).pack(side="left", padx=2)
        self.storage_paste_btn = ttk.Button(
            btns, text="Paste here", command=self._storage_paste,
            style="Accent.TButton", state="disabled")
        self.storage_paste_btn.pack(side="left", padx=2)
        ttk.Button(btns, text="📤 Send to another project…",
                   command=self._storage_send_to_project).pack(side="left", padx=2)
        ttk.Button(btns, text="Background copies…",
                   command=self._project_copies_manager).pack(side="left", padx=2)
        ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(btns, text="🔗 Share / Publish link…",
                   command=self._storage_generate_link).pack(side="left", padx=2)
        ttk.Button(btns, text="Rename / Move…",
                   command=self._storage_rename_move).pack(side="left", padx=2)
        ttk.Button(btns, text="New folder…",
                   command=self._storage_new_folder).pack(side="left", padx=2)
        # Clipboard status (right-aligned)
        self.storage_clip_label = ttk.Label(
            btns, text="Clipboard: empty", style="Muted.TLabel")
        self.storage_clip_label.pack(side="right", padx=4)

        # Row 2: transfer & remove
        btns2 = ttk.Frame(bottom)
        btns2.grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 8))
        ttk.Button(btns2, text="Upload file…",
                   command=self._storage_upload_file).pack(side="left", padx=2)
        ttk.Button(btns2, text="Upload folder…",
                   command=self._storage_upload_folder).pack(side="left", padx=2)
        ttk.Button(btns2, text="Download…",
                   command=self._storage_download_selected).pack(side="left", padx=12)
        ttk.Button(btns2, text="Delete selected",
                   command=self._storage_delete_selected,
                   style="Danger.TButton").pack(side="left", padx=12)
        ttk.Button(btns2, text="Recycle bin…",
                   command=self._storage_recycle_manager).pack(side="left", padx=2)

        # ----- Operation progress panel (v2.1) -----
        # Shows live progress for copy / cut / paste / delete / rename so the
        # user can see that something is happening (X/N items, percentages,
        # and streamed rclone output) instead of a frozen-looking window.
        prog = ttk.LabelFrame(t, text="Operation progress")
        prog.grid(row=6, column=0, sticky="ew", padx=10, pady=(0, 10))
        prog.columnconfigure(0, weight=1)
        self.storage_progressbar = ttk.Progressbar(
            prog, mode="determinate", maximum=100)
        self.storage_progressbar.grid(row=0, column=0, sticky="ew",
                                      padx=8, pady=(8, 2))
        self.storage_progress_label = ttk.Label(prog, text="Idle")
        self.storage_progress_label.grid(row=1, column=0, sticky="w",
                                        padx=8, pady=(0, 2))
        self.storage_op_output = scrolledtext.ScrolledText(
            prog, height=5, wrap="word",
            font=("Consolas" if IS_WINDOWS else "Monospace", 9))
        self.storage_op_output.grid(row=2, column=0, sticky="ew",
                                   padx=8, pady=(0, 8))
        self.storage_op_output.configure(state="disabled")
        self.storage_progress = True   # marker that the panel exists

        # ----- State -----
        self._tree_loaded: set[str] = set()      # iids whose children are populated
        self._tree_sizes: dict[str, int] = {}    # iid (incl. nested) -> bytes
        # Clipboard for copy/cut/paste on Pawsey: {"op": "copy"|"cut",
        # "remote": str, "items": [(iid, type), ...]}
        self._storage_clipboard: Optional[dict] = None

        # Populate remote dropdown now
        remotes = RcloneRemote.list_remotes()
        if remotes:
            self.storage_remote_combo["values"] = remotes
            if self.storage_remote.get() not in remotes:
                self.storage_remote.set(remotes[0])

    # ----- Tree event handlers -------------------------------------------

    def _on_tree_select(self, _event=None) -> None:
        sel = self.storage_tree.selection()
        if not sel:
            self.storage_selected_label.configure(text="No selection.")
            return
        iid = sel[0]
        type_ = self.storage_tree.set(iid, "type")
        size_ = self.storage_tree.set(iid, "size")
        remote = self.storage_remote.get()
        text = f"Selected: {remote}:{iid}   [{type_}, {size_}]"
        self.storage_selected_label.configure(text=text)

    def _on_tree_expand(self, _event=None) -> None:
        iid = self.storage_tree.focus()
        if not iid or iid in self._tree_loaded:
            return
        kids = self.storage_tree.get_children(iid)
        if len(kids) == 1 and kids[0].endswith("::LOAD"):
            self.storage_tree.delete(kids[0])
            self._load_children(iid)
        else:
            # Already real children; mark as loaded
            self._tree_loaded.add(iid)

    # ----- Loading data --------------------------------------------------

    def _storage_refresh_root(self) -> None:
        remote = self.storage_remote.get().strip()
        if not remote:
            messagebox.showerror(APP_NAME, "Pick a remote first.")
            return
        for ch in self.storage_tree.get_children():
            self.storage_tree.delete(ch)
        self._tree_loaded.clear()
        self._tree_sizes.clear()
        self.storage_summary.configure(text=f"Loading buckets on {remote}…")
        self.status_var.set(f"Listing buckets on {remote}…")

        def work():
            rc, out = run_rclone_capture(
                ["lsjson", f"{remote}:", "--dirs-only"], timeout=60)
            if rc != 0:
                raise RuntimeError(out)
            return json.loads(out or "[]")

        def done(buckets, err):
            if err:
                messagebox.showerror(APP_NAME, f"Could not list buckets:\n{err}")
                self.storage_summary.configure(text="Failed to load.")
                return
            for b in sorted(buckets, key=lambda e: (e.get("Name", "") or "").lower()):
                name = b.get("Name") or b.get("Path")
                if not name:
                    continue
                self.storage_tree.insert(
                    "", "end", iid=name, text=f"📦 {name}",
                    values=("bucket", "…", ""))
                # placeholder so the disclosure arrow appears
                self.storage_tree.insert(
                    name, "end", iid=f"{name}::LOAD",
                    text="(loading…)", values=("", "", ""))
                # background compute total bucket size
                self._compute_size_async(remote, name, name)
            self.storage_summary.configure(
                text=f"{remote}:  {len(buckets)} bucket(s) loaded. Sizes computing…")
            self.status_var.set("Buckets loaded.")
            # Mark freshness
            self._storage_loaded_at = datetime.now()
            self._update_storage_freshness()

        self._bg_call(work, done)

    def _update_storage_freshness(self) -> None:
        """Update the 'Tree last loaded N min ago' label and schedule another."""
        if self._storage_loaded_at is None:
            self.storage_freshness_label.configure(
                text="Tree not loaded yet.", foreground="#888")
        else:
            elapsed = (datetime.now() - self._storage_loaded_at).total_seconds()
            if elapsed < 60:
                txt = f"Tree loaded {int(elapsed)}s ago"
                fg = "#1a7f37"
            elif elapsed < 3600:
                mins = int(elapsed // 60)
                txt = f"Tree loaded {mins} min ago"
                fg = "#1a7f37" if mins < 10 else "#a76b00"
            else:
                hrs = elapsed / 3600
                txt = f"Tree loaded {hrs:.1f} h ago - consider refreshing"
                fg = "#a40000"
            self.storage_freshness_label.configure(text=txt, foreground=fg)
        # Cancel any prior tick, then re-schedule
        if self._storage_freshness_after_id:
            try:
                self.root.after_cancel(self._storage_freshness_after_id)
            except Exception:
                pass
        self._storage_freshness_after_id = self.root.after(
            10_000, self._update_storage_freshness)

    def _load_children(self, parent_iid: str) -> None:
        remote = self.storage_remote.get().strip()
        path = parent_iid
        self.status_var.set(f"Loading {path}…")

        def work():
            # FAST_LIST_FLAGS: without them a big folder needs one HEAD per
            # object and used to time out, leaving the folder looking empty.
            rc, out = run_rclone_capture(
                ["lsjson", f"{remote}:{path}", *FAST_LIST_FLAGS],
                timeout=900)
            if rc != 0:
                raise RuntimeError(out)
            return json.loads(out or "[]")

        def done(items, err):
            if err:
                messagebox.showerror(APP_NAME, f"Failed to list {path}:\n{err}")
                self.status_var.set("Load failed.")
                return
            # Directories first, then alphabetical
            items.sort(key=lambda x: (not x.get("IsDir"),
                                      (x.get("Name", "") or "").lower()))
            for item in items:
                name = item.get("Name", "")
                if not name:
                    continue
                is_dir = bool(item.get("IsDir"))
                child_iid = f"{path}/{name}"
                modtime = (item.get("ModTime", "") or "")[:16].replace("T", " ")
                if is_dir:
                    self.storage_tree.insert(
                        parent_iid, "end", iid=child_iid,
                        text=f"📁 {name}",
                        values=("folder", "…", modtime))
                    self.storage_tree.insert(
                        child_iid, "end", iid=f"{child_iid}::LOAD",
                        text="(loading…)", values=("", "", ""))
                    # Folder size in background
                    self._compute_size_async(remote, child_iid, child_iid)
                else:
                    size = item.get("Size", 0)
                    self.storage_tree.insert(
                        parent_iid, "end", iid=child_iid,
                        text=f"📄 {name}",
                        values=("file",
                                human_bytes(size) if size >= 0 else "?",
                                modtime))
                    if size >= 0:
                        self._tree_sizes[child_iid] = size
            self._tree_loaded.add(parent_iid)
            self.status_var.set(f"Loaded {path}.")

        self._bg_call(work, done)

    def _compute_size_async(self, remote: str, path: str, iid: str) -> None:
        """Run `rclone size` for remote:path in the background and update
        the size column of the row identified by iid."""
        def work():
            rc, out = run_rclone_capture(
                ["size", f"{remote}:{path}", "--json"], timeout=600)
            if rc != 0:
                return None
            try:
                d = json.loads(out)
                return int(d.get("bytes", 0)), int(d.get("count", 0))
            except json.JSONDecodeError:
                return None

        def done(result, err):
            if not self.storage_tree.exists(iid):
                return
            if not result:
                self.storage_tree.set(iid, "size", "?")
                return
            size_bytes, _ = result
            self._tree_sizes[iid] = size_bytes
            self.storage_tree.set(iid, "size", human_bytes(size_bytes))
            self._refresh_storage_summary()

        self._bg_call(work, done)

    def _refresh_storage_summary(self) -> None:
        remote = self.storage_remote.get().strip()
        bucket_iids = [iid for iid in self.storage_tree.get_children("")]
        total = sum(self._tree_sizes.get(b, 0) for b in bucket_iids)
        self.storage_summary.configure(
            text=f"{remote}:  {len(bucket_iids)} bucket(s) · {human_bytes(total)}")

    # ----- Helpers for selection / paths ---------------------------------

    def _storage_selection(self):
        """Return (remote, iid_path, item_type) for the selected row, or None."""
        sel = self.storage_tree.selection()
        if not sel:
            return None
        iid = sel[0]
        if iid.endswith("::LOAD"):
            return None
        type_ = self.storage_tree.set(iid, "type")
        remote = self.storage_remote.get().strip()
        return remote, iid, type_

    def _storage_selected_items(self):
        """Return a list of (remote, iid, type) for every selected row,
        skipping placeholder rows. Empty list if nothing usable selected."""
        remote = self.storage_remote.get().strip()
        items = []
        for iid in self.storage_tree.selection():
            if iid.endswith("::LOAD"):
                continue
            type_ = self.storage_tree.set(iid, "type")
            items.append((remote, iid, type_))
        return items

    def _check_notes(self) -> Optional[str]:
        notes = self.storage_notes.get("1.0", "end").strip()
        if not notes:
            messagebox.showerror(
                APP_NAME,
                "Please write an action note before performing this operation.\n"
                "The note will be saved permanently to the transfer log.")
            return None
        return notes

    def _resolve_upload_destination(self):
        """Return (remote, dest_path) of the folder/bucket to upload INTO."""
        sel = self._storage_selection()
        if not sel:
            messagebox.showerror(APP_NAME,
                                 "Select a bucket or folder to upload into first.")
            return None
        remote, iid, type_ = sel
        if type_ == "file":
            # use the file's parent folder
            parent = "/".join(iid.split("/")[:-1])
            return remote, parent
        return remote, iid

    def _refresh_subtree(self, path: str) -> None:
        """Reload immediate children of `path` in the tree and recompute its size."""
        remote = self.storage_remote.get().strip()
        if not path:
            self._storage_refresh_root()
            return
        if not self.storage_tree.exists(path):
            # The parent isn't currently in the tree; do a top-level refresh
            self._storage_refresh_root()
            return
        for c in self.storage_tree.get_children(path):
            self.storage_tree.delete(c)
        self._tree_loaded.discard(path)
        self.storage_tree.insert(path, "end", iid=f"{path}::LOAD",
                                  text="(loading…)", values=("", "", ""))
        self.storage_tree.item(path, open=True)
        self._load_children(path)
        # Also re-size the affected folder/bucket
        self._compute_size_async(remote, path, path)

    # ----- Operations: upload, new folder, delete, download --------------

    def _storage_upload_file(self) -> None:
        notes = self._check_notes()
        if not notes:
            return
        dest = self._resolve_upload_destination()
        if not dest:
            return
        remote, dest_path = dest
        src = filedialog.askopenfilename(title="Choose file to upload")
        if not src:
            return
        src_name = Path(src).name
        full_dest = f"{remote}:{dest_path}/{src_name}"

        self._append_log({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "operation": "upload_file",
            "source": src,
            "destination": full_dest,
            "notes": notes,
            "status": "started",
        })
        self.status_var.set(f"Uploading {src_name}…")

        def work():
            return run_rclone_capture(
                ["copyto", src, full_dest,
                 "--s3-no-check-bucket", "--s3-disable-checksum"],
                timeout=86400)

        def done(result, err):
            if err:
                messagebox.showerror(APP_NAME, f"Upload error:\n{err}")
                return
            rc, out = result
            if rc == 0:
                self._append_log({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "operation": "upload_file",
                    "destination": full_dest,
                    "status": "completed",
                })
                self.storage_notes.delete("1.0", "end")
                self.status_var.set(f"Uploaded {src_name}.")
                messagebox.showinfo(APP_NAME, f"Uploaded:\n{src_name}")
                self._refresh_subtree(dest_path)
                self._refresh_history()
            else:
                messagebox.showerror(APP_NAME, f"Upload failed:\n{out}")
                self.status_var.set("Upload failed.")

        self._bg_call(work, done)

    def _storage_upload_folder(self) -> None:
        notes = self._check_notes()
        if not notes:
            return
        dest = self._resolve_upload_destination()
        if not dest:
            return
        remote, dest_path = dest
        src = filedialog.askdirectory(title="Choose folder to upload")
        if not src:
            return
        src_name = Path(src).name
        full_dest = f"{remote}:{dest_path}/{src_name}"

        self._append_log({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "operation": "upload_folder",
            "source": src,
            "destination": full_dest,
            "notes": notes,
            "status": "started",
        })
        self.status_var.set(f"Uploading folder {src_name}…")

        def work():
            # The folder itself first: rclone replicates empty folders INSIDE
            # the source but never the source root, so an empty folder would
            # otherwise upload as nothing at all (see _ensure_remote_path).
            self._ensure_remote_path(full_dest)
            return run_rclone_capture(
                ["copy", src, full_dest,
                 "--s3-no-check-bucket", "--s3-disable-checksum",
                 *EMPTY_DIR_FLAGS,
                 "--transfers=4"],
                timeout=86400 * 2)

        def done(result, err):
            if err:
                messagebox.showerror(APP_NAME, f"Upload error:\n{err}")
                return
            rc, out = result
            if rc == 0:
                self._append_log({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "operation": "upload_folder",
                    "destination": full_dest,
                    "status": "completed",
                })
                self.storage_notes.delete("1.0", "end")
                self.status_var.set(f"Uploaded folder {src_name}.")
                messagebox.showinfo(APP_NAME, f"Uploaded folder:\n{src_name}")
                self._refresh_subtree(dest_path)
                self._refresh_history()
            else:
                messagebox.showerror(APP_NAME, f"Upload failed:\n{out}")
                self.status_var.set("Upload failed.")

        self._bg_call(work, done)

    def _storage_new_folder(self) -> None:
        notes = self._check_notes()
        if not notes:
            return
        dest = self._resolve_upload_destination()
        if not dest:
            return
        remote, parent_path = dest

        name = simpledialog.askstring(
            "New folder",
            "Folder name (no slashes):",
            parent=self.root,
        )
        if not name:
            return
        name = name.strip().strip("/")
        if not name or "/" in name or "\\" in name:
            messagebox.showerror(
                APP_NAME, "Folder name must not be empty and must not contain slashes.")
            return

        new_full_path = f"{parent_path}/{name}"

        self._append_log({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "operation": "create_folder",
            "destination": f"{remote}:{new_full_path}",
            "notes": notes,
            "status": "started",
        })

        def work():
            # On S3 there are no real folders. Write the same zero-byte
            # "<folder>/" marker every transfer uses (v2.3; previously a
            # visible `.keep` file), so the new folder shows up as an empty
            # folder and not as a folder holding a stray file.
            return run_rclone_capture(
                ["mkdir", f"{remote}:{new_full_path}",
                 "--s3-no-check-bucket", S3_MARKER_FLAG], timeout=60)

        def done(result, err):
            if err:
                messagebox.showerror(APP_NAME, f"Error:\n{err}")
                return
            rc, out = result
            if rc == 0:
                self._append_log({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "operation": "create_folder",
                    "destination": f"{remote}:{new_full_path}",
                    "status": "completed",
                })
                self.storage_notes.delete("1.0", "end")
                messagebox.showinfo(APP_NAME, f"Folder '{name}' created.")
                self._refresh_subtree(parent_path)
                self._refresh_history()
            else:
                messagebox.showerror(APP_NAME, f"Could not create folder:\n{out}")

        self._bg_call(work, done)

    def _storage_delete_selected(self) -> None:
        notes = self._check_notes()
        if not notes:
            return
        items = self._storage_selected_items()
        if not items:
            messagebox.showerror(APP_NAME, "Select one or more files or folders first.")
            return
        # Buckets can't be deleted here
        if any(t == "bucket" for _, _, t in items):
            messagebox.showinfo(
                APP_NAME,
                "One or more selected items are buckets. Use the Buckets tab "
                "to delete a bucket (it requires typing the name to confirm). "
                "Deselect buckets and try again.")
            return

        soft = bool(self.use_recycle_bin.get())
        n = len(items)
        names = "\n  ".join(iid for _, iid, _ in items[:12])
        if n > 12:
            names += f"\n  … and {n - 12} more"

        if soft:
            warn = (f"Move {n} item(s) to the recycle bin?\n\n  {names}\n\n"
                    f"Each is moved server-side into _recycle_bin/<timestamp>/, "
                    f"recoverable later. Continue?")
            if not messagebox.askyesno("Confirm soft-delete", warn):
                return
        else:
            warn = (f"⚠  PERMANENT DELETION  ⚠\n\nThis will permanently delete "
                    f"{n} item(s):\n\n  {names}\n\nThere is no recycle bin "
                    f"(it's turned off). This cannot be undone.\n\nContinue?")
            if not messagebox.askyesno("Confirm deletion", warn, icon="warning"):
                return

        # Process sequentially in the background so the UI stays responsive
        self._delete_queue = list(items)
        self._delete_notes = notes
        self._delete_done = 0
        self._delete_failed = 0
        self._delete_total = n
        self.status_var.set(f"Deleting {n} item(s)…")
        self._storage_progress_begin(
            f"{'Recycling' if soft else 'Deleting'} {n} item(s)",
            determinate=True, maximum=n)
        self._process_next_delete()

    def _process_next_delete(self) -> None:
        if not self._delete_queue:
            # All done
            msg = (f"Deleted {self._delete_done} item(s)."
                   if not self._delete_failed else
                   f"Deleted {self._delete_done} item(s); "
                   f"{self._delete_failed} failed (see log).")
            self.status_var.set(msg)
            self._storage_progress_done(msg)
            self.storage_notes.delete("1.0", "end")
            messagebox.showinfo(APP_NAME, msg)
            self._refresh_history()
            self._refresh_storage_summary()
            return

        remote, iid, type_ = self._delete_queue.pop(0)
        full = f"{remote}:{iid}"
        bucket = iid.split("/")[0]
        path_in_bucket = iid[len(bucket) + 1:]
        in_recycle = (path_in_bucket.startswith(RECYCLE_PREFIX + "/")
                      or path_in_bucket == RECYCLE_PREFIX)
        soft = bool(self.use_recycle_bin.get()) and not in_recycle
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        trash_dest = f"{remote}:{bucket}/{RECYCLE_PREFIX}/{ts}/{path_in_bucket}"
        op = "soft_delete" if soft else f"delete_{type_}"

        self._append_log({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "operation": op, "target": full,
            "notes": self._delete_notes, "status": "started",
        })

        def work():
            if soft:
                if type_ == "file":
                    return run_rclone_capture(
                        ["moveto", full, trash_dest, "--s3-no-check-bucket"],
                        timeout=86400)
                # Folder markers must move with the folder, and the empty
                # sub-folders inside it have to survive the trip to the
                # recycle bin so a restore brings the structure back intact.
                res = run_rclone_capture(
                    ["move", full, trash_dest, "--s3-no-check-bucket",
                     "--delete-empty-src-dirs", *EMPTY_DIR_FLAGS],
                    timeout=86400)
                # rmdir cannot remove the folder's own marker, so without this
                # a "recycled" folder would still appear (empty) where it was.
                if res[0] == 0:
                    self._purge_empty_prefix(full)
                return res
            if type_ == "file":
                return run_rclone_capture(["deletefile", full], timeout=120)
            return run_rclone_capture(
                ["purge", full, *S3_DELETE_FLAGS], timeout=86400)

        idx = self._delete_done + self._delete_failed + 1
        verb = "Recycling" if soft else "Deleting"
        self._storage_progress_set(
            label=f"{verb} {idx}/{self._delete_total}: {iid}")
        self._storage_progress_log(f"[{idx}/{self._delete_total}] {verb.lower()} {full} …")

        def done(result, err):
            ok = (not err) and result and result[0] == 0
            if ok:
                self._delete_done += 1
                self._storage_progress_log(f"    ✓ done: {iid}")
                self._append_log({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "operation": op, "target": full, "status": "completed",
                })
                if self.storage_tree.exists(iid):
                    self.storage_tree.delete(iid)
                self._tree_sizes.pop(iid, None)
                parent_iid = "/".join(iid.split("/")[:-1])
                if parent_iid and self.storage_tree.exists(parent_iid):
                    self._compute_size_async(remote, parent_iid, parent_iid)
                elif self.storage_tree.exists(bucket):
                    self._compute_size_async(remote, bucket, bucket)
            else:
                self._delete_failed += 1
                out = (result[1] if result else str(err))
                self._storage_progress_log(f"    ✗ FAILED: {iid}: {out.strip()[:200]}")
                self._append_output(f"\n[delete failed] {full}: {out}\n")
            processed = self._delete_done + self._delete_failed
            self._storage_progress_set(value=processed)
            self.status_var.set(
                f"Deleting… {processed}/{self._delete_total}")
            self._process_next_delete()

        self._bg_call(work, done)

    def _storage_recycle_manager(self) -> None:
        """Open a small manager for the recycle bin of the bucket that the
        current selection (or the default bucket) lives in: shows size and
        lets the user empty it. Restore is done by browsing the tree and
        using Rename/Move out of the _recycle_bin prefix."""
        remote = self.storage_remote.get().strip()
        if not remote:
            messagebox.showerror(APP_NAME, "Pick a remote first.")
            return
        # Determine bucket from selection, else from default bucket
        bucket = None
        sel = self.storage_tree.selection()
        if sel and not sel[0].endswith("::LOAD"):
            bucket = sel[0].split("/")[0]
        if not bucket:
            bucket = self.cfg.get("default_bucket", "")
        if not bucket:
            messagebox.showinfo(
                APP_NAME,
                "Select a bucket (or a folder/file inside one) first so I "
                "know which bucket's recycle bin to manage.")
            return

        trash = f"{remote}:{bucket}/{RECYCLE_PREFIX}"

        win = tk.Toplevel(self.root)
        win.title(f"Recycle bin - {bucket}")
        win.geometry("560x240")
        win.transient(self.root)
        ttk.Label(win, text=f"Recycle bin for bucket '{bucket}'",
                  style="Big.TLabel").pack(anchor="w", padx=12, pady=(12, 4))
        info = ttk.Label(win, text="Measuring recycle-bin size…",
                         foreground="#555", wraplength=520, justify="left")
        info.pack(anchor="w", padx=12, pady=4)

        ttk.Label(
            win,
            text="Deleted files and previous versions are kept here, grouped\n"
                 "by date. To RESTORE something, close this, browse into the\n"
                 f"{RECYCLE_PREFIX}/ folder in the tree, and use 'Rename / Move…'\n"
                 "to move it back. To free the space, empty the bin below.",
            foreground="#555", justify="left").pack(anchor="w", padx=12, pady=4)

        btnbar = ttk.Frame(win)
        btnbar.pack(fill="x", side="bottom", padx=12, pady=12)
        empty_btn = ttk.Button(btnbar, text="Empty recycle bin",
                               style="Danger.TButton", state="disabled")
        empty_btn.pack(side="left")
        ttk.Button(btnbar, text="Close", command=win.destroy).pack(side="right")

        state = {"bytes": 0, "count": 0}

        def measure():
            return run_rclone_capture(["size", trash, "--json"], timeout=600)

        def measured(result, err):
            if err or not result:
                info.configure(text="Recycle bin is empty or unreadable.")
                return
            rc, out = result
            if rc != 0:
                # Most likely the prefix doesn't exist yet => empty bin
                info.configure(text="Recycle bin is currently empty.")
                return
            try:
                d = json.loads(out)
                state["bytes"] = int(d.get("bytes", 0))
                state["count"] = int(d.get("count", 0))
            except Exception:
                pass
            if state["count"] == 0:
                info.configure(text="Recycle bin is currently empty.")
            else:
                info.configure(
                    text=f"Holding {state['count']:,} item(s), "
                         f"{human_bytes(state['bytes'])}.")
                empty_btn.configure(state="normal")

        def do_empty():
            if not messagebox.askyesno(
                "Empty recycle bin",
                f"Permanently delete EVERYTHING in the recycle bin of "
                f"bucket '{bucket}'?\n\n"
                f"  {state['count']:,} item(s), {human_bytes(state['bytes'])}\n\n"
                f"This frees the storage and CANNOT be undone.",
                icon="warning", parent=win):
                return
            self._append_log({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "operation": "empty_recycle_bin",
                "target": trash,
                "status": "started",
            })
            self.status_var.set(f"Emptying recycle bin for {bucket}…")
            empty_btn.configure(state="disabled")

            def work():
                return run_rclone_capture(
                    ["purge", trash, *S3_DELETE_FLAGS], timeout=86400)

            def done(result, err):
                if err:
                    messagebox.showerror(APP_NAME, f"Error:\n{err}", parent=win)
                    return
                rc, out = result
                if rc == 0:
                    self._append_log({
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "operation": "empty_recycle_bin",
                        "target": trash,
                        "status": "completed",
                    })
                    info.configure(text="Recycle bin emptied.")
                    self.status_var.set("Recycle bin emptied.")
                    messagebox.showinfo(APP_NAME, "Recycle bin emptied.", parent=win)
                    self._refresh_history()
                    if self.storage_tree.get_children():
                        self._storage_refresh_root()
                else:
                    messagebox.showerror(APP_NAME, f"Empty failed:\n{out}", parent=win)

            self._bg_call(work, done)

        empty_btn.configure(command=do_empty)
        self._bg_call(measure, measured)

    def _known_sync_pairs(self) -> list[tuple[str, str]]:
        """Collect (local_src, pawsey_dest) pairs the app knows about, from
        the bisync pair store, the resume store, and the current Transfer
        tab fields. Used to map a Pawsey path back to a local folder."""
        pairs: list[tuple[str, str]] = []
        # Initialised two-way pairs
        try:
            data = json.loads(BISYNC_STATE_FILE.read_text(encoding="utf-8"))
            for key in data.get("pairs", []):
                if "|||" in key:
                    s, d = key.split("|||", 1)
                    pairs.append((s, d))
        except Exception:
            pass
        # Resume store entries
        try:
            for e in ResumeStore.list_all():
                s, d = e.get("src", ""), e.get("dest", "")
                if s and d:
                    pairs.append((s, d))
        except Exception:
            pass
        # Current Transfer-tab fields
        try:
            s = self.src_var.get().strip()
            remote = self.remote_var.get().strip()
            bucket = self.bucket_var.get().strip()
            path = self.path_var.get().strip().lstrip("/")
            if s and remote and bucket:
                d = f"{remote}:{bucket}" + (f"/{path}" if path else "")
                pairs.append((s, d))
        except Exception:
            pass
        # De-dupe, keep order
        seen = set()
        uniq = []
        for p in pairs:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq

    def _map_pawsey_to_local(self, remote: str, bucket: str,
                             rel_in_bucket: str):
        """Given a Pawsey object path (bucket-relative), find the matching
        local absolute path using known sync pairs. Returns (local_path,
        local_src_root) or (None, None) if no pair covers this path.

        Picks the most specific (longest-prefix) matching pair."""
        target = f"{remote}:{bucket}/{rel_in_bucket}".rstrip("/")
        best = None  # (prefix_len, local_path, src_root)
        for src_root, dest in self._known_sync_pairs():
            d = dest.rstrip("/")
            # dest must be 'remote:bucket' or 'remote:bucket/prefix'
            if not (target == d or target.startswith(d + "/")):
                continue
            remainder = target[len(d):].lstrip("/")  # path under the pair root
            local_path = Path(src_root)
            if remainder:
                local_path = local_path / Path(remainder.replace("/", os.sep))
            score = len(d)
            if best is None or score > best[0]:
                best = (score, str(local_path), src_root)
        if best:
            return best[1], best[2]
        return None, None

    def _storage_rename_move(self) -> None:
        """Rename or move a file/folder, applying the change to BOTH sides.

        On Pawsey the change is a server-side move (no data transfer). If the
        item belongs to a known local<->Pawsey sync pair and the matching
        local folder exists, the LOCAL folder is renamed the same way too, so
        both sides stay identical immediately - your next two-way sync sees no
        changes and there is no re-upload or re-download. The local rename is
        done first; if it fails, Pawsey is left untouched so the two sides
        never silently diverge.
        """
        notes = self._check_notes()
        if not notes:
            return
        sel = self._storage_selection()
        if not sel:
            messagebox.showerror(APP_NAME, "Select a file or folder first.")
            return
        remote, iid, type_ = sel
        if type_ == "bucket":
            messagebox.showinfo(
                APP_NAME,
                "Buckets can't be renamed on object storage. Create a new\n"
                "bucket and move the contents into it instead.")
            return

        bucket = iid.split("/")[0]
        path_in_bucket = iid[len(bucket) + 1:]  # part after 'bucket/'

        new_path_in_bucket = simpledialog.askstring(
            "Rename / Move on Pawsey",
            "New name or path, relative to the bucket.\n\n"
            "Examples:\n"
            "  rename:  2024/oldname        ->  2024/newname\n"
            "  move:    2024/Northam        ->  2025/Northam\n\n"
            f"Bucket: {bucket}\nCurrent path:",
            initialvalue=path_in_bucket,
            parent=self.root,
        )
        if not new_path_in_bucket:
            return
        new_path_in_bucket = new_path_in_bucket.strip().strip("/")
        if not new_path_in_bucket or new_path_in_bucket == path_in_bucket:
            return

        src_full = f"{remote}:{iid}"
        dest_full = f"{remote}:{bucket}/{new_path_in_bucket}"

        # Work out whether a matching LOCAL folder/file exists so we can
        # rename it too, keeping both sides in step without a sync.
        local_old, _root_old = self._map_pawsey_to_local(
            remote, bucket, path_in_bucket)
        local_new, _root_new = self._map_pawsey_to_local(
            remote, bucket, new_path_in_bucket)
        do_local = False
        if local_old and local_new and Path(local_old).exists():
            do_local = True

        confirm_msg = (
            f"Rename / move on Pawsey (server-side, no re-upload):\n\n"
            f"  FROM:  {src_full}\n"
            f"  TO:    {dest_full}\n")
        if do_local:
            confirm_msg += (
                f"\nThe matching LOCAL item will be renamed too, so both "
                f"sides stay identical without a sync:\n"
                f"  FROM:  {local_old}\n"
                f"  TO:    {local_new}\n")
        else:
            confirm_msg += (
                f"\n(No matching local folder found in known sync pairs, so "
                f"only Pawsey will change. If you also keep a local copy, "
                f"rename it yourself to match.)\n")
        confirm_msg += "\nContinue?"

        if not messagebox.askyesno("Confirm rename / move", confirm_msg):
            return

        self._append_log({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "operation": f"rename_move_{type_}",
            "source": src_full,
            "destination": dest_full,
            "local_from": local_old if do_local else "",
            "local_to": local_new if do_local else "",
            "notes": notes,
            "status": "started",
        })
        self.status_var.set(f"Renaming/moving {path_in_bucket} → {new_path_in_bucket}…")
        self._storage_progress_begin(
            f"Rename / move: {path_in_bucket} → {new_path_in_bucket}",
            determinate=False)

        # Rename the local side first (fast, local). If it fails we stop and
        # do NOT touch Pawsey, so the two sides never silently diverge.
        local_done = False
        if do_local:
            import shutil
            self._storage_progress_log(f"Renaming local copy: {local_old} → {local_new}")
            try:
                Path(local_new).parent.mkdir(parents=True, exist_ok=True)
                if Path(local_new).exists():
                    raise FileExistsError(
                        f"Target already exists locally:\n{local_new}")
                shutil.move(local_old, local_new)
                local_done = True
                self._storage_progress_log("    ✓ local copy renamed")
            except Exception as e:
                self._storage_progress_done("Rename aborted (local step failed).")
                messagebox.showerror(
                    APP_NAME,
                    f"Local rename failed - Pawsey was NOT changed so the two "
                    f"sides stay consistent:\n\n{e}")
                self.status_var.set("Rename aborted (local step failed).")
                return

        self._storage_progress_log(
            f"Moving on Pawsey (server-side): {src_full} → {dest_full}")

        # moveto for a single file, move for a directory prefix.
        if type_ == "file":
            args = ["moveto", src_full, dest_full]
        else:
            # Carry the folder markers so empty sub-folders survive the move
            # and the old prefix doesn't leave a ghost folder behind.
            args = ["move", src_full, dest_full, "--delete-empty-src-dirs",
                    *EMPTY_DIR_FLAGS]
        args += ["--s3-no-check-bucket", "--stats=1s", "--stats-one-line", "-v"]
        # An EMPTY folder has nothing for `move` to move: rclone would report
        # "nothing to transfer", the marker clean-up below would then remove
        # the old folder, and the folder would vanish. Create the destination
        # first so the folder survives the move (see _ensure_remote_path).
        pre = ([] if type_ == "file" else
               [["mkdir", dest_full, "--s3-no-check-bucket", S3_MARKER_FLAG]])

        def on_line(line: str):
            line = line.rstrip("\n")
            if line.strip():
                self._storage_progress_log("    " + line.strip())

        def done(result, err):
            if err and not result:
                self._storage_progress_done("Rename/move error.")
                messagebox.showerror(APP_NAME, f"Rename/move error:\n{err}")
                return
            rc, out = result
            if rc == 0:
                # Clear the old prefix's leftover folder marker so the
                # renamed-away folder doesn't linger as an empty ghost.
                if type_ != "file":
                    if self._purge_empty_prefix(src_full):
                        self._storage_progress_log(
                            "    ✓ old folder marker cleared")
                self._append_log({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "operation": f"rename_move_{type_}",
                    "source": src_full,
                    "destination": dest_full,
                    "local_from": local_old if local_done else "",
                    "local_to": local_new if local_done else "",
                    "status": "completed",
                })
                self.storage_notes.delete("1.0", "end")
                self.status_var.set("Rename/move complete on both sides.")
                self._storage_progress_done("Rename/move complete on both sides.")
                msg = (f"Done - renamed/moved on Pawsey with no data transfer:\n\n"
                       f"{path_in_bucket}  →  {new_path_in_bucket}")
                if local_done:
                    msg += (f"\n\nThe local folder was renamed to match, so both "
                            f"sides are identical. Your next two-way sync will "
                            f"see no changes.")
                messagebox.showinfo(APP_NAME, msg)
                self._storage_refresh_root()
                self._refresh_history()
            else:
                # Pawsey failed but we already renamed locally - tell the user
                # how to get back in step.
                extra = ""
                if local_done:
                    extra = (f"\n\nNOTE: the LOCAL folder was already renamed to "
                             f"'{Path(local_new).name}'. To get both sides "
                             f"matching, either retry this rename, or rename the "
                             f"local folder back to '{Path(local_old).name}'.")
                self._storage_progress_done("Rename/move failed on Pawsey.")
                messagebox.showerror(APP_NAME, f"Rename/move failed on Pawsey:\n{out}{extra}")
                self.status_var.set("Rename/move failed on Pawsey.")

        self._bg_stream(args, on_line, done, timeout=86400, pre=pre)

    # ===== Copy / Cut / Paste (Windows-style, server-side on Pawsey) ======

    def _update_clip_ui(self) -> None:
        """Refresh the clipboard status label and Paste button state."""
        clip = self._storage_clipboard
        if not clip or not clip.get("items"):
            self.storage_clip_label.configure(text="Clipboard: empty")
            self.storage_paste_btn.configure(state="disabled")
            return
        verb = "Copy" if clip["op"] == "copy" else "Cut"
        n = len(clip["items"])
        first = clip["items"][0][0].split("/")[-1] or clip["items"][0][0]
        label = first if n == 1 else f"{n} items"
        self.storage_clip_label.configure(
            text=f"Clipboard: {verb} → {label}")
        self.storage_paste_btn.configure(state="normal")

    def _storage_set_clipboard(self, op: str) -> None:
        items = self._storage_selected_items()
        # Buckets can't be copied/moved as objects; block clearly.
        items = [(r, i, t) for (r, i, t) in items if t != "bucket"]
        if not items:
            messagebox.showinfo(
                APP_NAME,
                "Select one or more files/folders to "
                f"{'copy' if op == 'copy' else 'cut'} first.\n\n"
                "(Whole buckets can't be copied/cut — make a new bucket and "
                "paste contents into it instead.)")
            return
        remote = items[0][0]
        self._storage_clipboard = {
            "op": op, "remote": remote,
            "items": [(i, t) for (_r, i, t) in items],
        }
        self._update_clip_ui()
        verb = "Copied" if op == "copy" else "Cut"
        self.status_var.set(
            f"{verb} {len(items)} item(s) to clipboard — select a destination "
            f"folder/bucket and click 'Paste here'.")

    def _storage_copy(self) -> None:
        self._storage_set_clipboard("copy")

    def _storage_cut(self) -> None:
        self._storage_set_clipboard("cut")

    def _storage_paste(self) -> None:
        """Paste clipboard items into the selected folder/bucket. Same-remote
        operations are server-side on Pawsey (no download/re-upload)."""
        clip = self._storage_clipboard
        if not clip or not clip.get("items"):
            messagebox.showinfo(APP_NAME, "Clipboard is empty.")
            return
        notes = self._check_notes()
        if not notes:
            return
        # Destination = selected bucket or folder (file → its parent folder).
        sel = self._storage_selection()
        if not sel:
            messagebox.showerror(
                APP_NAME, "Select the destination bucket or folder to paste into.")
            return
        dest_remote, dest_iid, dest_type = sel
        if dest_type == "file":
            dest_iid = "/".join(dest_iid.split("/")[:-1])
        if not dest_iid:
            messagebox.showerror(APP_NAME, "Pick a bucket or folder to paste into.")
            return

        op = clip["op"]
        src_remote = clip["remote"]
        items = clip["items"]

        # Guard: don't paste a folder into itself or its own subtree.
        for iid, type_ in items:
            if type_ != "file":
                if dest_iid == iid or dest_iid.startswith(iid + "/"):
                    messagebox.showerror(
                        APP_NAME,
                        f"Can't paste '{iid.split('/')[-1]}' into itself or one "
                        f"of its own subfolders.")
                    return
            # No-op move into the same parent
            parent = "/".join(iid.split("/")[:-1])
            if op == "cut" and dest_remote == src_remote and parent == dest_iid:
                messagebox.showinfo(
                    APP_NAME, f"'{iid.split('/')[-1]}' is already in that folder.")
                return

        same_remote = (src_remote == dest_remote)
        verb_word = "Copy" if op == "copy" else "Move"
        kind = "server-side on Pawsey (no data transfer)" if same_remote \
            else "between remotes (data is transferred)"
        names = ", ".join(i.split("/")[-1] for i, _ in items[:5])
        if len(items) > 5:
            names += f", … (+{len(items) - 5} more)"
        if not messagebox.askyesno(
                f"Confirm paste ({verb_word.lower()})",
                f"{verb_word} {len(items)} item(s) {kind}:\n\n"
                f"  {names}\n\n"
                f"  INTO:  {dest_remote}:{dest_iid}/\n\nContinue?"):
            return

        self.status_var.set(f"{verb_word}ing {len(items)} item(s)…")
        self._append_log({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "operation": f"paste_{op}",
            "source": f"{src_remote}: {[i for i, _ in items]}",
            "destination": f"{dest_remote}:{dest_iid}",
            "notes": notes,
            "status": "started",
        })

        # Process items one at a time, streaming rclone progress so the user
        # sees a live percentage + X/N item counter for the whole paste.
        self._paste_state = {
            "queue": list(items), "op": op, "verb": verb_word,
            "src_remote": src_remote, "dest_remote": dest_remote,
            "dest_iid": dest_iid, "items": list(items),
            "total": len(items), "done": 0, "failed": 0,
            "failed_list": [],
        }
        self._storage_progress_begin(
            f"{verb_word} {len(items)} item(s) → {dest_remote}:{dest_iid}/",
            determinate=True, maximum=100)
        self._process_next_paste()

    def _process_next_paste(self) -> None:
        st = self._paste_state
        if not st["queue"]:
            self._finish_paste()
            return

        iid, type_ = st["queue"].pop(0)
        idx = st["done"] + st["failed"] + 1
        total = st["total"]
        base = iid.split("/")[-1]
        src_full = f"{st['src_remote']}:{iid}"
        dest_full = f"{st['dest_remote']}:{st['dest_iid']}/{base}"
        op = st["op"]
        if op == "copy":
            args = ["copyto" if type_ == "file" else "copy", src_full, dest_full]
            if type_ != "file":
                args += list(EMPTY_DIR_FLAGS)   # keep empty sub-folders
        else:  # cut → move
            if type_ == "file":
                args = ["moveto", src_full, dest_full]
            else:
                args = ["move", src_full, dest_full, "--delete-empty-src-dirs",
                        *EMPTY_DIR_FLAGS]
        # --stats-one-line gives a compact "Transferred: x / y, NN%" line we
        # can parse into a percentage for the progress bar.
        args += ["--s3-no-check-bucket", "--stats=1s", "--stats-one-line",
                 "-v"]
        # Folder: create the destination first, otherwise an EMPTY folder is
        # "nothing to transfer" for rclone and is silently dropped (and, for a
        # cut, removed at the source too). See _ensure_remote_path.
        pre = ([] if type_ == "file" else
               [["mkdir", dest_full, "--s3-no-check-bucket", S3_MARKER_FLAG]])

        verb = st["verb"]
        self._storage_progress_set(
            label=f"{verb}ing {idx}/{total}: {base}")
        self._storage_progress_log(f"[{idx}/{total}] {verb.lower()} {iid} …")

        base_frac = (idx - 1) / total * 100.0
        span = 100.0 / total

        def on_line(line: str):
            line = line.rstrip("\n")
            if not line.strip():
                return
            m = PROGRESS_RE.search(line)
            if m:
                try:
                    pct = int(m.group(3))
                    self._storage_progress_set(value=base_frac + span * pct / 100.0)
                except Exception:
                    pass
                self._storage_progress_set(
                    label=f"{verb}ing {idx}/{total}: {base} — {line.strip()}")
            else:
                self._storage_progress_log("    " + line.strip())

        def on_done(result, err):
            rc, out = result if result else (1, str(err))
            if rc == 0:
                # A cut of a FOLDER leaves the source's folder marker behind
                # (rmdir can't remove markers); clear it so the moved-from
                # folder doesn't linger as an empty ghost.
                if op != "copy" and type_ != "file":
                    self._purge_empty_prefix(src_full)
                st["done"] += 1
                self._storage_progress_log(f"    ✓ done: {iid}")
            else:
                st["failed"] += 1
                st["failed_list"].append((iid, out))
                self._storage_progress_log(
                    f"    ✗ FAILED: {iid}: {out.strip()[:200]}")
            self._storage_progress_set(value=(st["done"] + st["failed"]) / total * 100.0)
            self.status_var.set(
                f"{verb}ing… {st['done'] + st['failed']}/{total}")
            self._process_next_paste()

        self._bg_stream(args, on_line, on_done, timeout=86400, pre=pre)

    def _finish_paste(self) -> None:
        st = self._paste_state
        op = st["op"]
        verb_word = st["verb"]
        dest_remote = st["dest_remote"]
        dest_iid = st["dest_iid"]
        items = st["items"]
        failed = st["failed_list"]
        total = st["total"]
        self._append_log({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "operation": f"paste_{op}",
            "destination": f"{dest_remote}:{dest_iid}",
            "status": "completed" if not failed else "partial",
            "failed": len(failed),
        })
        self.storage_notes.delete("1.0", "end")
        # A copy stays on the clipboard (like Windows); a cut is consumed.
        if op == "cut":
            self._storage_clipboard = None
            self._update_clip_ui()
        # Refresh destination and (for cut) the source parents.
        self._refresh_subtree(dest_iid)
        if op == "cut":
            for iid, _t in items:
                parent = "/".join(iid.split("/")[:-1])
                if parent and parent != dest_iid:
                    self._refresh_subtree(parent)
        self._refresh_history()
        if failed:
            self._storage_progress_done(
                f"{total - len(failed)}/{total} {verb_word.lower()}d; "
                f"{len(failed)} failed.")
            msg = "\n".join(f"  • {iid}: {out.strip()[:200]}"
                            for iid, out in failed)
            messagebox.showwarning(
                APP_NAME,
                f"{total - len(failed)} of {total} pasted; "
                f"{len(failed)} failed:\n\n{msg}")
            self.status_var.set(f"Paste finished with {len(failed)} error(s).")
        else:
            self._storage_progress_done(
                f"{verb_word}d {total} item(s) into {dest_iid}.")
            self.status_var.set(
                f"Pasted {total} item(s) into {dest_iid}.")
            messagebox.showinfo(
                APP_NAME,
                f"Done — {verb_word.lower()}d {total} item(s) into:\n"
                f"{dest_remote}:{dest_iid}/")

    # ===== Send selected items to another Pawsey project ==================

    def _storage_send_to_project(self) -> None:
        """Copy the selected buckets/folders/files to a DIFFERENT Pawsey
        project (remote). Copy-only — nothing at the destination is ever
        deleted. Because the two projects use different credentials the data
        streams through this machine (download → re-upload), not server-side,
        so large datasets are best run on a Pawsey/Nimbus VM."""
        items = self._storage_selected_items()
        if not items:
            messagebox.showinfo(
                APP_NAME,
                "Select one or more buckets, folders or files to send to "
                "another project first.")
            return
        src_remote = items[0][0]

        remotes = [r for r in RcloneRemote.list_remotes() if r != src_remote]
        if not remotes:
            messagebox.showinfo(
                APP_NAME,
                "No other Pawsey project is configured to send to.\n\n"
                "Add the recipient's project first (Settings → Pawsey projects, "
                "or the Console), then try again. You'll need access to a bucket "
                "on their project — either their access key + secret, or a bucket "
                "policy that grants your key write access.")
            return

        notes = self._check_notes()
        if not notes:
            return

        T = THEME
        dlg = tk.Toplevel(self.root)
        dlg.title("Send to another project")
        dlg.transient(self.root)
        dlg.configure(bg=T["surface"])
        dlg.resizable(False, False)

        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        names = ", ".join(i.split("/")[-1] for _r, i, _t in items[:5])
        if len(items) > 5:
            names += f", … (+{len(items) - 5} more)"
        ttk.Label(
            frm, text=f"Copy {len(items)} item(s) from project “{src_remote}”:",
            font=("Segoe UI Semibold", 10)).grid(
                row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frm, text=names, style="Muted.TLabel",
                  wraplength=440).grid(row=1, column=0, columnspan=2,
                                       sticky="w", pady=(0, 10))

        ttk.Label(frm, text="Destination project:").grid(
            row=2, column=0, sticky="w", pady=3)
        proj_var = tk.StringVar()
        proj_combo = ttk.Combobox(frm, textvariable=proj_var, values=remotes,
                                  state="readonly", width=32)
        proj_combo.grid(row=2, column=1, sticky="ew", pady=3)

        ttk.Label(frm, text="Destination bucket:").grid(
            row=3, column=0, sticky="w", pady=3)
        bucket_var = tk.StringVar()
        bucket_combo = ttk.Combobox(frm, textvariable=bucket_var, width=32)
        bucket_combo.grid(row=3, column=1, sticky="ew", pady=3)

        ttk.Label(frm, text="Subfolder (optional):").grid(
            row=4, column=0, sticky="w", pady=3)
        subfolder_var = tk.StringVar()
        ttk.Entry(frm, textvariable=subfolder_var, width=34).grid(
            row=4, column=1, sticky="ew", pady=3)

        status_lbl = ttk.Label(frm, text="", style="Muted.TLabel",
                               wraplength=440)
        status_lbl.grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 6))

        warn = ("Copy only — nothing on the destination project is deleted.\n"
                "The data is copied THROUGH this machine (download → upload), "
                "not server-side, because the two projects use different keys. "
                "For large datasets run this on a Pawsey/Nimbus VM rather than "
                "a laptop.")
        ttk.Label(frm, text=warn, foreground="#9A5B00",
                  wraplength=440).grid(row=6, column=0, columnspan=2,
                                       sticky="w", pady=(0, 8))

        bg_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frm, variable=bg_var,
            text="Keep running after I close the app (background job)").grid(
                row=7, column=0, columnspan=2, sticky="w")
        ttk.Label(
            frm, style="Muted.TLabel", wraplength=440,
            text="Background: the copy survives closing the app or it crashing, "
                 "logs to a file, and can be resumed from “Background copies…”. "
                 "It still stops if the computer sleeps or shuts down. Unticked: "
                 "runs in-app with a live result, but stops if you close the "
                 "app.").grid(row=8, column=0, columnspan=2, sticky="w",
                              pady=(0, 10))

        def load_buckets(*_):
            r = proj_var.get().strip()
            if not r:
                return
            bucket_var.set("")
            bucket_combo["values"] = []
            status_lbl.configure(text=f"Listing buckets on “{r}”…")

            def work():
                rc, out = run_rclone_capture(
                    ["lsjson", f"{r}:", "--dirs-only"], timeout=60)
                if rc != 0:
                    raise RuntimeError(out)
                data = json.loads(out or "[]")
                return sorted(
                    [(b.get("Name") or b.get("Path")) for b in data
                     if (b.get("Name") or b.get("Path"))], key=str.lower)

            def done(buckets, err):
                if not dlg.winfo_exists():
                    return
                if err:
                    status_lbl.configure(
                        text=f"Could not list buckets on “{r}”: "
                             f"{str(err)[:90]} — you can still type the name.")
                    return
                bucket_combo["values"] = buckets
                if buckets:
                    status_lbl.configure(
                        text=f"{len(buckets)} bucket(s) on “{r}”. Pick one, or "
                             f"type an existing bucket name.")
                else:
                    status_lbl.configure(
                        text=f"No buckets visible on “{r}” (or no list "
                             f"permission). Type the target bucket name.")

            self._bg_call(work, done)

        proj_var.trace_add("write", load_buckets)

        def do_copy():
            dest_remote = proj_var.get().strip()
            dest_bucket = bucket_var.get().strip().strip("/")
            subfolder = subfolder_var.get().strip().strip("/")
            if not dest_remote:
                messagebox.showerror(APP_NAME, "Pick a destination project.",
                                     parent=dlg)
                return
            if not dest_bucket:
                messagebox.showerror(
                    APP_NAME, "Enter or pick a destination bucket.", parent=dlg)
                return
            dest_root = f"{dest_bucket}/{subfolder}" if subfolder else dest_bucket
            background = bool(bg_var.get())
            mode_line = ("It will keep running even if you close the app."
                         if background else
                         "It runs in-app and stops if you close the app.")
            if not messagebox.askyesno(
                    "Confirm copy to another project",
                    f"Copy {len(items)} item(s) from project “{src_remote}” to:\n\n"
                    f"  {dest_remote}:{dest_root}/\n\n"
                    f"Copy only — nothing on “{dest_remote}” is deleted.\n"
                    f"{mode_line}\n\nContinue?", parent=dlg):
                return
            dlg.destroy()
            pairs = [(i, t) for (_r, i, t) in items]
            self._start_send_job(pairs, src_remote, dest_remote, dest_bucket,
                                 subfolder, notes, background)

        btnrow = ttk.Frame(frm)
        btnrow.grid(row=9, column=0, columnspan=2, sticky="e", pady=(4, 0))
        ttk.Button(btnrow, text="Cancel",
                   command=dlg.destroy).pack(side="right", padx=2)
        ttk.Button(btnrow, text="Copy to project", style="Accent.TButton",
                   command=do_copy).pack(side="right", padx=2)

        proj_combo.set(remotes[0])  # triggers bucket load
        dlg.update_idletasks()
        # Centre over the main window
        x = self.root.winfo_rootx() + (self.root.winfo_width()
                                       - dlg.winfo_width()) // 2
        y = self.root.winfo_rooty() + 80
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        dlg.grab_set()

    def _send_build_commands(self, items, src_remote, dest_remote, dest_root):
        """Return one rclone arg-list per item (copy-only, type-aware)."""
        cmds = []
        for it in items:
            iid, type_ = it[0], it[1]
            base = iid.split("/")[-1]
            src_full = f"{src_remote}:{iid}"
            dest_full = f"{dest_remote}:{dest_root}/{base}"
            if type_ == "file":
                cmds.append(["copyto", src_full, dest_full,
                             "--s3-no-check-bucket"])
            else:  # folder or whole bucket
                cmds.append(["copy", src_full, dest_full,
                             "--create-empty-src-dirs", "--s3-directory-markers",
                             "--s3-no-check-bucket"])
        return cmds

    def _start_send_job(self, pairs, src_remote, dest_remote, dest_bucket,
                        subfolder, notes, background) -> None:
        """Register a resumable project->project copy job, then run it either
        in-app (foreground) or as a detached background process."""
        dest_root = f"{dest_bucket}/{subfolder}" if subfolder else dest_bucket
        jid = "pc_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        ext = "cmd" if IS_WINDOWS else "sh"
        entry = {
            "id": jid,
            "kind": "project_copy",
            "status": "running",
            "src_remote": src_remote,
            "items": [[i, t] for (i, t) in pairs],
            "dest_remote": dest_remote,
            "dest_bucket": dest_bucket,
            "dest_subfolder": subfolder,
            "dest_root": dest_root,
            "notes": notes,
            "background": bool(background),
            "started": datetime.now().isoformat(timespec="seconds"),
            "pid": None,
            "log_file": str(SEND_JOBS_DIR / f"{jid}.log"),
            "status_file": str(SEND_JOBS_DIR / f"{jid}.status"),
            "script_file": str(SEND_JOBS_DIR / f"{jid}.{ext}"),
        }
        ResumeStore.add(entry)
        self._append_log({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "operation": "send_to_project",
            "source": f"{src_remote}: {[i for i, _t in pairs]}",
            "destination": f"{dest_remote}:{dest_root}",
            "notes": notes,
            "mode": "background" if background else "foreground",
            "status": "started",
        })
        if background:
            self._launch_send_detached(entry)
        else:
            self._run_send_foreground(entry)

    def _run_send_foreground(self, entry) -> None:
        """Run the cross-project copy in-app (stops if the app closes; the job
        is still recorded so it can be resumed afterwards)."""
        cmds = self._send_build_commands(
            entry["items"], entry["src_remote"], entry["dest_remote"],
            entry["dest_root"])
        dest_remote, dest_root = entry["dest_remote"], entry["dest_root"]
        self._send_fg_active += 1
        self.status_var.set(
            f"Copying {len(cmds)} item(s) to {dest_remote}:{dest_root}/ …")

        def work():
            results = []
            for it, args in zip(entry["items"], cmds):
                if it[1] != "file":
                    # Empty folders need their root created explicitly
                    # (rclone copies what is inside a folder, never the
                    # folder itself). See _ensure_remote_path.
                    self._ensure_remote_path(args[2])
                rc, out = run_rclone_capture(args, timeout=86400)
                results.append((it[0], rc, out))
            return results

        def done(results, err):
            self._send_fg_active = max(0, self._send_fg_active - 1)
            if err:
                ResumeStore.update(entry["id"], status="failed")
                messagebox.showerror(APP_NAME, f"Copy error:\n{err}")
                self.status_var.set("Copy to project failed.")
                return
            failed = [(iid, out) for (iid, rc, out) in results if rc != 0]
            ResumeStore.update(
                entry["id"], status="completed" if not failed else "failed")
            self._append_log({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "operation": "send_to_project",
                "destination": f"{dest_remote}:{dest_root}",
                "status": "completed" if not failed else "partial",
                "failed": len(failed),
            })
            self.storage_notes.delete("1.0", "end")
            self._refresh_history()
            ok = len(results) - len(failed)
            if failed:
                msg = "\n".join(f"  • {iid}: {out.strip()[:200]}"
                                for iid, out in failed)
                messagebox.showwarning(
                    APP_NAME,
                    f"{ok} of {len(results)} copied to {dest_remote}; "
                    f"{len(failed)} failed:\n\n{msg}\n\n"
                    f"Resume it later from “Background copies…”.")
                self.status_var.set(
                    f"Copy to project finished with {len(failed)} error(s).")
            else:
                self.status_var.set(
                    f"Copied {ok} item(s) to {dest_remote}:{dest_root}/.")
                messagebox.showinfo(
                    APP_NAME,
                    f"Done — copied {ok} item(s) to:\n"
                    f"{dest_remote}:{dest_root}/\n\n"
                    f"Switch the Remote dropdown to “{dest_remote}” and refresh "
                    f"to see them.")

        self._bg_call(work, done)

    # ----- Detached (survives app close) + resume -------------------------

    def _write_send_script(self, entry, cmds) -> str:
        """Write a launcher script that runs the rclone copies sequentially,
        logs to a file, and writes a final status sentinel. Returns its path."""
        log, status = entry["log_file"], entry["status_file"]
        prog = ["--stats", "30s", "--stats-one-line", "-v"]
        path = entry["script_file"]
        if IS_WINDOWS:
            def q(s):
                return '"' + str(s) + '"'
            lines = ["@echo off", 'set "FAILED="',
                     f'> {q(log)} echo === project-copy {entry["id"]} ===']
            for args in cmds:
                if args[0] == "copy":
                    # Folder: make sure it exists even if it is empty
                    # (rclone never creates the source root itself).
                    mk = [RCLONE_EXE, "mkdir", args[2],
                          "--s3-no-check-bucket", S3_MARKER_FLAG]
                    lines.append(" ".join(q(t) for t in mk)
                                 + f" >> {q(log)} 2>&1")
                toks = [RCLONE_EXE] + args + prog
                lines.append(" ".join(q(t) for t in toks) + f" >> {q(log)} 2>&1")
                lines.append('if errorlevel 1 set "FAILED=1"')
            lines.append(f'if defined FAILED (> {q(status)} echo failed) '
                         f'else (> {q(status)} echo completed)')
            text = "\r\n".join(lines) + "\r\n"
        else:
            def q(s):
                return shlex.quote(str(s))
            lines = ["#!/bin/sh", "FAILED=0",
                     f'echo "=== project-copy {entry["id"]} ===" > {q(log)}']
            for args in cmds:
                if args[0] == "copy":
                    mk = [RCLONE_EXE, "mkdir", args[2],
                          "--s3-no-check-bucket", S3_MARKER_FLAG]
                    lines.append(" ".join(q(t) for t in mk)
                                 + f" >> {q(log)} 2>&1 || true")
                toks = [RCLONE_EXE] + args + prog
                lines.append(" ".join(q(t) for t in toks)
                             + f" >> {q(log)} 2>&1 || FAILED=1")
            lines.append(f'if [ "$FAILED" -ne 0 ]; then echo failed > {q(status)}; '
                         f'else echo completed > {q(status)}; fi')
            text = "\n".join(lines) + "\n"
        Path(path).write_text(text, encoding="utf-8")
        if not IS_WINDOWS:
            try:
                os.chmod(path, 0o755)
            except Exception:
                pass
        return path

    def _spawn_detached(self, script_path) -> int:
        """Launch the launcher script so it outlives this app. Returns the
        child process id.

        NOTE: we use CREATE_NO_WINDOW, NOT DETACHED_PROCESS. DETACHED_PROCESS
        breaks the in-script output redirection to the log for the rclone
        grandchild. It isn't needed for survival anyway: Windows does not kill
        child processes when the parent exits (Python never puts them in a
        kill-on-close job), so a plain background child keeps running after the
        app closes."""
        if IS_WINDOWS:
            flags = (subprocess.CREATE_NO_WINDOW
                     | subprocess.CREATE_NEW_PROCESS_GROUP)
            p = subprocess.Popen(
                ["cmd", "/c", script_path],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=flags,
                close_fds=True, cwd=str(APP_DIR))
        else:
            p = subprocess.Popen(
                ["/bin/sh", script_path],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
                close_fds=True, cwd=str(APP_DIR))
        return p.pid

    def _launch_send_detached(self, entry) -> None:
        cmds = self._send_build_commands(
            entry["items"], entry["src_remote"], entry["dest_remote"],
            entry["dest_root"])
        try:
            Path(entry["status_file"]).unlink()
        except Exception:
            pass
        try:
            self._write_send_script(entry, cmds)
            pid = self._spawn_detached(entry["script_file"])
        except Exception as e:
            ResumeStore.update(entry["id"], status="failed")
            messagebox.showerror(
                APP_NAME, f"Could not start background copy:\n{e}")
            self.status_var.set("Background copy failed to start.")
            return
        ResumeStore.update(entry["id"], pid=pid, status="running")
        entry["pid"] = pid
        try:
            self.storage_notes.delete("1.0", "end")
        except Exception:
            pass
        self.status_var.set(
            f"Background copy started → {entry['dest_remote']}:"
            f"{entry['dest_root']}/  (keeps running if you close the app).")
        messagebox.showinfo(
            APP_NAME,
            f"Background copy started.\n\nCopying to:\n  "
            f"{entry['dest_remote']}:{entry['dest_root']}/\n\n"
            f"You can close the app — the copy keeps running. Reopen the app "
            f"and use Storage → “Background copies…” to check status or resume "
            f"it.\n\nLog file:\n{entry['log_file']}")
        self._poll_send_job(entry["id"])

    def _read_send_status(self, entry):
        try:
            txt = Path(entry["status_file"]).read_text(
                encoding="utf-8", errors="ignore").strip().lower()
            if "completed" in txt:
                return "completed"
            if "failed" in txt:
                return "failed"
        except Exception:
            pass
        return None

    def _poll_send_job(self, entry_id) -> None:
        """While the app is open, watch a detached job and update its status
        when it finishes (or detect that it was interrupted)."""
        entry = ResumeStore.get(entry_id)
        if not entry or entry.get("kind") != "project_copy":
            return
        status = self._read_send_status(entry)
        if status in ("completed", "failed"):
            ResumeStore.update(entry_id, status=status)
            self._refresh_history()
            dest = f"{entry['dest_remote']}:{entry['dest_root']}/"
            if status == "completed":
                self.status_var.set(f"Background copy to {dest} completed.")
            else:
                self.status_var.set(
                    f"Background copy to {dest} finished with errors — see log.")
            return
        if entry.get("background") and not _pid_alive(entry.get("pid")):
            ResumeStore.update(entry_id, status="interrupted")
            self.status_var.set(
                f"Background copy to {entry['dest_remote']}: was interrupted — "
                f"resume it from “Background copies…”.")
            return
        self.root.after(5000, lambda: self._poll_send_job(entry_id))

    def _reconcile_project_copies(self) -> None:
        """At startup: finalise finished detached jobs, mark dead ones as
        interrupted, re-attach polling to any still running."""
        still_running = []
        for e in ResumeStore.list_all():
            if e.get("kind") != "project_copy" or e.get("status") != "running":
                continue
            status = self._read_send_status(e)
            if status in ("completed", "failed"):
                ResumeStore.update(e["id"], status=status)
            elif e.get("background") and _pid_alive(e.get("pid")):
                still_running.append(e["id"])
            else:
                ResumeStore.update(e["id"], status="interrupted")
        for jid in still_running:
            self._poll_send_job(jid)
        resumable = [e for e in ResumeStore.list_all()
                     if e.get("kind") == "project_copy"
                     and e.get("status") in ("interrupted", "stopped", "failed")]
        if resumable:
            self.status_var.set(
                f"{len(resumable)} project-copy job(s) can be resumed — "
                f"Storage → “Background copies…”.")

    def _resume_send_job(self, entry) -> None:
        """Resume an interrupted/failed job as a detached background copy.
        rclone copy skips files already at the destination, so this continues
        rather than restarting."""
        entry = dict(entry)
        entry["background"] = True
        ResumeStore.update(
            entry["id"], status="running", background=True,
            resumed_at=datetime.now().isoformat(timespec="seconds"))
        self._launch_send_detached(entry)

    def _project_copies_manager(self) -> None:
        """List project->project copy jobs with Resume / Open log / Remove."""
        T = THEME
        dlg = tk.Toplevel(self.root)
        dlg.title("Background copies — project → project")
        dlg.transient(self.root)
        dlg.configure(bg=T["surface"])
        dlg.geometry("780x400")

        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Project → project copy jobs (copy-only):",
                  font=("Segoe UI Semibold", 10)).pack(anchor="w")

        cols = ("status", "route", "started")
        tree = ttk.Treeview(frm, columns=cols, show="headings", height=11)
        tree.heading("status", text="Status")
        tree.heading("route", text="From  →  To")
        tree.heading("started", text="Started")
        tree.column("status", width=110, anchor="w")
        tree.column("route", width=450, anchor="w")
        tree.column("started", width=160, anchor="w")
        tree.pack(fill="both", expand=True, pady=(6, 6))

        id_by_row = {}

        def refresh():
            tree.delete(*tree.get_children())
            id_by_row.clear()
            for e in ResumeStore.list_all():
                if e.get("kind") != "project_copy":
                    continue
                route = (f"{e.get('src_remote')}  →  "
                         f"{e.get('dest_remote')}:{e.get('dest_root')}/")
                row = tree.insert("", "end",
                                  values=(e.get("status", "?"), route,
                                          e.get("started", "")))
                id_by_row[row] = e["id"]

        def selected_entry():
            sel = tree.selection()
            if not sel:
                return None
            return ResumeStore.get(id_by_row.get(sel[0]))

        def do_resume():
            e = selected_entry()
            if not e:
                messagebox.showinfo(APP_NAME, "Pick a job first.", parent=dlg)
                return
            if e.get("status") == "running" and _pid_alive(e.get("pid")):
                messagebox.showinfo(
                    APP_NAME, "That job is still running.", parent=dlg)
                return
            dlg.destroy()
            self._resume_send_job(e)

        def do_open_log():
            e = selected_entry()
            if not e:
                return
            self._open_path(e.get("log_file", ""))

        def do_remove():
            e = selected_entry()
            if not e:
                return
            if e.get("status") == "running" and _pid_alive(e.get("pid")):
                if not messagebox.askyesno(
                        APP_NAME,
                        "That job looks like it is still running. Remove it from "
                        "the list anyway?\n(The background copy itself is not "
                        "stopped.)", parent=dlg):
                    return
            ResumeStore.remove(e["id"])
            refresh()

        btns = ttk.Frame(frm)
        btns.pack(fill="x")
        ttk.Button(btns, text="Resume", style="Accent.TButton",
                   command=do_resume).pack(side="left", padx=2)
        ttk.Button(btns, text="Open log…",
                   command=do_open_log).pack(side="left", padx=2)
        ttk.Button(btns, text="Remove from list",
                   command=do_remove).pack(side="left", padx=2)
        ttk.Button(btns, text="Refresh",
                   command=refresh).pack(side="left", padx=2)
        ttk.Button(btns, text="Close",
                   command=dlg.destroy).pack(side="right", padx=2)

        refresh()
        dlg.grab_set()

    # ===== Generate shareable (presigned) link ============================

    def _remote_s3_params(self, remote: str) -> Optional[dict]:
        """Pull S3 credentials/endpoint for `remote` from rclone's own config
        (so it works for any S3 remote, not just the saved Pawsey one)."""
        rc, out = run_rclone_capture(["config", "dump"], timeout=30)
        if rc != 0:
            return None
        try:
            data = json.loads(out or "{}")
        except Exception:
            return None
        r = data.get(remote) or {}
        access = r.get("access_key_id") or self.cfg.get("access_key_id", "")
        secret = r.get("secret_access_key") or self.cfg.get("secret_access_key", "")
        endpoint = r.get("endpoint") or self.cfg.get("endpoint", "")
        region = r.get("region") or "us-east-1"
        if not (access and secret and endpoint):
            return None
        return {"access": access, "secret": secret,
                "endpoint": endpoint, "region": region}

    def _ask_link_options(self) -> Optional[dict]:
        """Modal dialog: pick a TEMPORARY (expiring) or PERMANENT (public,
        no-expiry) share link. Returns one of:
            {"mode": "temporary", "secs": <int>}
            {"mode": "permanent"}
        or None if cancelled."""
        T = THEME
        win = tk.Toplevel(self.root)
        win.title("Share link options")
        win.configure(bg=T["bg"])
        win.transient(self.root)
        win.resizable(False, False)
        win.grab_set()
        result = {"value": None}

        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="How should this link work?",
                  style="Big.TLabel").grid(row=0, column=0, columnspan=3,
                                           sticky="w", pady=(0, 12))

        mode = tk.StringVar(value="temporary")

        # --- Temporary option -------------------------------------------
        ttk.Radiobutton(frm, text="Temporary link (expiring)", value="temporary",
                        variable=mode).grid(row=1, column=0, columnspan=3,
                                            sticky="w")
        trow = ttk.Frame(frm)
        trow.grid(row=2, column=0, columnspan=3, sticky="w", padx=(24, 0),
                  pady=(2, 2))
        amount = tk.IntVar(value=7)
        unit = tk.StringVar(value="days")
        sp = ttk.Spinbox(trow, from_=1, to=999, textvariable=amount, width=6)
        sp.pack(side="left")
        cb = ttk.Combobox(trow, textvariable=unit, width=10, state="readonly",
                          values=["minutes", "hours", "days"])
        cb.pack(side="left", padx=(8, 0))
        ttk.Label(frm, text="Stops working after this time. Best for sharing "
                            "with collaborators or reviewers. S3's maximum is "
                            "7 days.",
                  style="Muted.TLabel", wraplength=430, justify="left").grid(
            row=3, column=0, columnspan=3, sticky="w", padx=(24, 0),
            pady=(2, 12))

        # --- Permanent option -------------------------------------------
        ttk.Radiobutton(frm, text="Permanent link (public, never expires)",
                        value="permanent", variable=mode).grid(
            row=4, column=0, columnspan=3, sticky="w")
        ttk.Label(frm, text="Makes the data PUBLIC on the internet with no "
                            "expiry — anyone with the link can read it, forever, "
                            "with no Pawsey account. Use this only for datasets "
                            "you intend to PUBLISH. Requires the bucket to allow "
                            "public access.",
                  style="Muted.TLabel", wraplength=430, justify="left").grid(
            row=5, column=0, columnspan=3, sticky="w", padx=(24, 0),
            pady=(2, 12))

        def ok():
            if mode.get() == "temporary":
                mult = {"minutes": 60, "hours": 3600, "days": 86400}[unit.get()]
                try:
                    secs = int(amount.get()) * mult
                except Exception:
                    secs = 0
                if secs <= 0:
                    messagebox.showerror(APP_NAME, "Enter a positive duration.",
                                         parent=win)
                    return
                if secs > PRESIGN_MAX_SECONDS:
                    messagebox.showinfo(
                        APP_NAME,
                        "S3 presigned links can last at most 7 days; "
                        "capping at 7 days.\n\nFor a link with no expiry, choose "
                        "the Permanent option instead.",
                        parent=win)
                    secs = PRESIGN_MAX_SECONDS
                result["value"] = {"mode": "temporary", "secs": secs}
                win.destroy()
            else:
                # Extra confirmation — this exposes data publicly and is a
                # one-way action until the ACL is manually revoked.
                if not messagebox.askyesno(
                        APP_NAME,
                        "Create a PERMANENT PUBLIC link?\n\n"
                        "• The selected data will be readable by ANYONE on the "
                        "internet, with no expiry and no account.\n"
                        "• Only do this for datasets you intend to publish.\n"
                        "• Never publish data containing personal, sensitive or "
                        "embargoed information.\n\n"
                        "Continue?", icon="warning", parent=win):
                    return
                result["value"] = {"mode": "permanent"}
                win.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=3, sticky="e", pady=(4, 0))
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=4)
        ttk.Button(btns, text="Generate link", style="Accent.TButton",
                   command=ok).pack(side="right", padx=4)
        win.bind("<Escape>", lambda e: win.destroy())
        sp.focus_set()
        self.root.wait_window(win)
        return result["value"]

    def _storage_generate_link(self) -> None:
        """Create a shareable link for the selected file, folder or bucket.

        Temporary mode → S3 presigned URL(s) that expire (max 7 days).
        Permanent mode → sets a public-read ACL and hands back the plain,
        never-expiring object URL — for publishing datasets. A file gives one
        link; a folder or bucket builds a browseable index page (uploaded into
        the bucket) and returns one link to that page."""
        sel = self._storage_selection()
        if not sel:
            messagebox.showerror(
                APP_NAME, "Select a file, folder or bucket to share first.")
            return
        remote, iid, type_ = sel
        params = self._remote_s3_params(remote)
        if not params:
            messagebox.showerror(
                APP_NAME,
                f"Couldn't read S3 credentials for remote '{remote}'.\n\n"
                "Generate links needs the access key, secret and endpoint. "
                "Configure the remote on the Settings tab (or in rclone) first.")
            return

        opts = self._ask_link_options()
        if not opts:
            return
        permanent = opts["mode"] == "permanent"
        expires = None if permanent else opts["secs"]

        bucket = iid.split("/")[0]
        key = iid[len(bucket) + 1:]  # '' for a whole bucket

        if type_ == "file":
            if permanent:
                self._generate_permanent_file_link(remote, bucket, key, iid,
                                                    params)
            else:
                url = s3_presign_url(
                    params["endpoint"], params["region"], params["access"],
                    params["secret"], bucket, key, expires)
                self._show_single_link(url, iid, expires)
                self._append_log({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "operation": "share_link_file",
                    "source": f"{remote}:{iid}",
                    "expires_seconds": expires,
                    "status": "completed",
                })
                self._refresh_history()
        else:
            # The published page always lets recipients download the whole
            # dataset / a folder / individual files (folder structure preserved
            # in Chrome/Edge). For permanent datasets we ALSO offer to build a
            # single ZIP — a one-file download that works in every browser.
            make_zip = False
            if permanent:
                make_zip = messagebox.askyesno(
                    APP_NAME,
                    "Also build a single downloadable ZIP of the whole "
                    "dataset?\n\n"
                    "The published page already lets people download the whole "
                    "dataset, a folder, or individual files (in Chrome/Edge the "
                    "original folder structure is recreated).\n\n"
                    "A ZIP adds a one-file download that works in EVERY browser "
                    "— handy for citing. But:\n"
                    "• PDMA streams every file through this machine to build it "
                    "and uploads it into the bucket (uses extra storage ≈ the "
                    "dataset size).\n"
                    "• Large datasets take a while and need temporary disk "
                    "space.\n"
                    "• The ZIP is a snapshot — re-publish to refresh it.\n\n"
                    "Yes = also build the ZIP.   No = page only (no ZIP).",
                    icon="question")
            self._generate_folder_links(remote, bucket, key, iid, params,
                                        expires, make_zip=make_zip)

    def _build_dataset_zip(self, remote, bucket, prefix, listing, label,
                           params):
        """Stream every listed object into one ZIP (preserving folder
        structure), upload it into the bucket under _shares/, and publish it as
        a permanent public download. Returns a dict on success or ("error",msg).

        Streaming via `rclone cat` keeps only the finished ZIP on disk (no full
        second copy of the tree)."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_key = f"_shares/{label}_{ts}.zip"
        tmp_zip = Path(tempfile.gettempdir()) / f"pdma_zip_{ts}.zip"
        n_total = len(listing)
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        try:
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_STORED,
                                 allowZip64=True) as zf:
                for idx, f in enumerate(listing):
                    rel = f.get("Path", "")
                    if not rel:
                        continue
                    full_key = f"{prefix}/{rel}" if prefix else rel
                    target = f"{remote}:{bucket}/{full_key}"
                    proc = subprocess.Popen(
                        [RCLONE_EXE, "cat", target],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        creationflags=flags)
                    with zf.open(rel, "w") as dst:
                        while True:
                            chunk = proc.stdout.read(1024 * 1024)
                            if not chunk:
                                break
                            dst.write(chunk)
                    proc.stdout.close()
                    if proc.wait() != 0:
                        return ("error",
                                f"Could not read '{rel}' while building the ZIP.")
                    if idx % 5 == 0:
                        self.root.after(0, lambda i=idx: self.status_var.set(
                            f"Building ZIP… {i}/{n_total} files"))
        except Exception as e:
            try:
                tmp_zip.unlink()
            except Exception:
                pass
            return ("error", f"ZIP build failed: {e}")

        zip_size = tmp_zip.stat().st_size
        self.root.after(0, lambda: self.status_var.set(
            f"Uploading ZIP ({human_bytes(zip_size)})…"))
        rc, out = run_rclone_capture(
            ["copyto", str(tmp_zip), f"{remote}:{bucket}/{zip_key}",
             "--s3-no-check-bucket",
             "--header-upload", "Content-Type: application/zip"],
            timeout=86400)
        try:
            tmp_zip.unlink()
        except Exception:
            pass
        if rc != 0:
            return ("error", f"Could not upload the ZIP:\n{out}")
        ok, detail = s3_publish_object(
            params["endpoint"], params["region"], params["access"],
            params["secret"], bucket, zip_key,
            download_name=f"{label}.zip", content_type="application/zip")
        if not ok:
            return ("error",
                    f"ZIP uploaded but could not be made public:\n{detail}")
        return {"url": s3_public_url(params["endpoint"], bucket, zip_key),
                "size": zip_size, "key": zip_key}

    def _generate_permanent_file_link(self, remote, bucket, key, iid, params):
        """Set a public-read ACL on one object and return its permanent URL."""
        self.status_var.set(f"Publishing {iid} as a permanent public link…")

        def work():
            ok, detail = s3_set_public_read(
                params["endpoint"], params["region"], params["access"],
                params["secret"], bucket, key)
            if not ok:
                return ("error", detail)
            url = s3_public_url(params["endpoint"], bucket, key)
            public = s3_url_is_public(url)
            return ("ok", {"url": url, "verified": public})

        def done(result, err):
            if err:
                messagebox.showerror(APP_NAME,
                                     f"Permanent link failed:\n{err}")
                self.status_var.set("Permanent link failed.")
                return
            status, payload = result
            if status == "error":
                messagebox.showerror(
                    APP_NAME,
                    "Could not make this object public:\n\n"
                    f"{payload}\n\n"
                    "This usually means the bucket does not allow public "
                    "access. Ask Pawsey support to enable public read for the "
                    "bucket, or use a temporary link instead.")
                self.status_var.set("Permanent link failed.")
                return
            self._append_log({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "operation": "publish_link_file",
                "source": f"{remote}:{iid}",
                "expires_seconds": None,
                "status": "completed",
            })
            self._refresh_history()
            self.status_var.set("Permanent public link ready.")
            self._show_single_link(payload["url"], iid, None,
                                   verified=payload["verified"])

        self._bg_call(work, done)

    def _show_single_link(self, url: str, iid: str, expires: Optional[int],
                          description: Optional[str] = None,
                          verified: Optional[bool] = None) -> None:
        """Show a share URL with a Copy button. `expires` is seconds for a
        temporary link, or None for a permanent (public) link. `description`
        explains what the link is (file vs browseable folder page)."""
        T = THEME
        permanent = expires is None
        win = tk.Toplevel(self.root)
        win.title("Shareable link")
        win.configure(bg=T["bg"])
        win.transient(self.root)
        win.grab_set()
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)
        heading = ("Permanent public link (never expires)" if permanent
                   else "Shareable link (works anywhere, expiring)")
        ttk.Label(frm, text=heading, style="Big.TLabel").pack(anchor="w")
        if description is None:
            if permanent:
                description = ("Permanent public link to this file. It never "
                               "expires and opens in any browser with no Pawsey "
                               "account — suitable for publishing.")
            else:
                description = (f"Direct link to this file. Send it to anyone — it "
                              f"opens in any browser, no Pawsey account needed, "
                              f"and expires in {self._human_duration(expires)}.")
        if permanent and verified is False:
            description += ("\n\n⚠ The ACL was set, but a public read-back "
                            "check did not succeed yet. Public visibility can "
                            "take a moment, or the bucket may restrict anonymous "
                            "access — test the link in a private browser window.")
        ttk.Label(frm, text=f"{iid}", style="Muted.TLabel").pack(anchor="w", pady=(2, 2))
        ttk.Label(frm, text=description, style="Muted.TLabel",
                  wraplength=620, justify="left").pack(anchor="w", pady=(0, 8))
        txt = scrolledtext.ScrolledText(frm, height=4, width=86, wrap="char")
        txt.insert("1.0", url)
        txt.configure(state="normal")
        txt.pack(fill="both", expand=True)

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            self.status_var.set("Link copied to clipboard.")

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Copy to clipboard", style="Accent.TButton",
                   command=copy).pack(side="left")
        ttk.Button(btns, text="Open in browser",
                   command=lambda: self._open_url(url)).pack(side="left", padx=6)
        ttk.Button(btns, text="Close", command=win.destroy).pack(side="right")
        copy()  # copy immediately for convenience

    def _generate_folder_links(self, remote, bucket, key, iid, params, expires,
                               make_zip=False):
        """Make ONE shareable link for a whole folder/bucket that works from any
        browser, anywhere. We build a self-contained HTML 'file browser' (expand
        folders, view/download any file, download a whole folder at once) and
        UPLOAD it into the bucket under `_shares/`.

        `expires` is seconds for a TEMPORARY link (every file link + the page
        are presigned and expire together) or None for a PERMANENT published
        dataset (each object is given a public-read ACL and the page links use
        plain, never-expiring URLs). `make_zip` (permanent only) also bundles
        the whole dataset into one public ZIP and adds a one-click download."""
        permanent = expires is None
        noun = "published dataset" if permanent else "shareable page"
        self.status_var.set(f"Building {noun} for {iid}…")
        src = f"{remote}:{iid}"
        prefix = key.rstrip("/")

        def work():
            # List files, skipping our own share pages and the recycle bin.
            rc, out = run_rclone_capture(
                ["lsjson", src, "-R", "--files-only", "--no-modtime",
                 "--no-mimetype",
                 "--exclude", "_shares/**",
                 "--exclude", f"{RECYCLE_PREFIX}/**"],
                timeout=900)
            if rc != 0:
                return ("error", out)
            try:
                listing = json.loads(out or "[]")
            except Exception as e:
                return ("error", str(e))
            if not listing:
                return ("empty", None)

            files = []
            acl_failures = 0
            n_total = len(listing)
            for idx, f in enumerate(listing):
                rel = f.get("Path", "")
                if not rel:
                    continue
                full_key = f"{prefix}/{rel}" if prefix else rel
                if permanent:
                    # Publish: make each object public AND give it a stored
                    # Content-Disposition: attachment so its plain URL downloads
                    # (this is what makes "Download folder / everything" work).
                    ok, _ = s3_publish_object(
                        params["endpoint"], params["region"], params["access"],
                        params["secret"], bucket, full_key,
                        download_name=rel.replace("/", "_"),
                        content_type=f.get("MimeType"))
                    if not ok:
                        # Fall back to a plain public-read ACL so at least the
                        # per-file links still work.
                        ok2, _ = s3_set_public_read(
                            params["endpoint"], params["region"],
                            params["access"], params["secret"], bucket,
                            full_key)
                        if not ok2:
                            acl_failures += 1
                    url = s3_public_url(params["endpoint"], bucket, full_key)
                    view = dl = url
                    if idx % 25 == 0:
                        self.root.after(0, lambda i=idx: self.status_var.set(
                            f"Publishing… {i}/{n_total} files"))
                else:
                    # Two presigned URLs per file: inline 'view' + attachment.
                    view = s3_presign_url(
                        params["endpoint"], params["region"], params["access"],
                        params["secret"], bucket, full_key, expires)
                    dl = s3_presign_url(
                        params["endpoint"], params["region"], params["access"],
                        params["secret"], bucket, full_key, expires,
                        download_name=rel.replace("/", "_"))
                files.append({"p": rel, "s": f.get("Size", 0) or 0,
                              "v": view, "d": dl})
            total = sum(f["s"] for f in files)

            # If publishing and EVERY object refused a public ACL, the bucket
            # almost certainly blocks public access — stop with clear guidance.
            if permanent and acl_failures == len(files) and files:
                return ("acl_denied", None)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            label = (prefix or bucket).rstrip("/").split("/")[-1] or "root"

            # Optionally also bundle the whole dataset into one public ZIP.
            zip_info = None
            if permanent and make_zip:
                zres = self._build_dataset_zip(remote, bucket, prefix, listing,
                                               label, params)
                zip_info = zres if isinstance(zres, dict) else {"error": zres[1]}

            # Build the page and upload it INTO the bucket so the link is public.
            html = self._build_links_html(iid, files, expires, total,
                                           permanent=permanent,
                                           zip_info=zip_info)
            page_key = f"_shares/{label}_{ts}.html"
            tmp = Path(tempfile.gettempdir()) / f"pawsey_share_{ts}.html"
            try:
                tmp.write_text(html, encoding="utf-8")
            except Exception as e:
                return ("error", f"Could not write temp page: {e}")
            page_dest = f"{remote}:{bucket}/{page_key}"
            rc2, out2 = run_rclone_capture(
                ["copyto", str(tmp), page_dest, "--s3-no-check-bucket",
                 "--header-upload", "Content-Type: text/html; charset=utf-8"],
                timeout=300)
            try:
                tmp.unlink()
            except Exception:
                pass
            if rc2 != 0:
                return ("error", f"Could not upload the share page:\n{out2}")

            if permanent:
                # Make the index page itself public; its stored Content-Type
                # (text/html, set on upload) makes the plain URL render.
                s3_set_public_read(
                    params["endpoint"], params["region"], params["access"],
                    params["secret"], bucket, page_key)
                page_url = s3_public_url(params["endpoint"], bucket, page_key)
                verified = s3_url_is_public(page_url)
            else:
                # Presign the page itself, forcing it to render as HTML.
                page_url = s3_presign_url(
                    params["endpoint"], params["region"], params["access"],
                    params["secret"], bucket, page_key, expires,
                    content_type="text/html; charset=utf-8")
                verified = None
            return ("ok", {"url": page_url, "n": len(files),
                           "total": total, "page_key": page_key,
                           "acl_failures": acl_failures, "verified": verified,
                           "zip_info": zip_info})

        def done(result, err):
            if err:
                messagebox.showerror(APP_NAME, f"Link generation failed:\n{err}")
                self.status_var.set("Link generation failed.")
                return
            status, payload = result
            if status == "error":
                messagebox.showerror(APP_NAME, f"Could not create link:\n{payload}")
                self.status_var.set("Link generation failed.")
                return
            if status == "acl_denied":
                messagebox.showerror(
                    APP_NAME,
                    "Could not publish: the bucket refused public-read access "
                    "on its objects.\n\n"
                    "Ask Pawsey support to enable public access for this "
                    "bucket, or use a temporary link instead.")
                self.status_var.set("Publish failed (public access denied).")
                return
            if status == "empty":
                messagebox.showinfo(APP_NAME, "No files found under that folder.")
                self.status_var.set("Ready")
                return
            self._append_log({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "operation": "publish_link_folder" if permanent
                             else "share_link_folder",
                "source": src,
                "destination": f"{remote}:{bucket}/{payload['page_key']}",
                "files": payload["n"],
                "expires_seconds": expires,
                "status": "completed",
            })
            self._refresh_history()
            if payload.get("acl_failures"):
                messagebox.showwarning(
                    APP_NAME,
                    f"{payload['acl_failures']} of {payload['n']} file(s) could "
                    "not be made public and may not open from the link. The "
                    "rest were published successfully.")
            zi = payload.get("zip_info")
            if zi and zi.get("error"):
                messagebox.showwarning(
                    APP_NAME,
                    "The dataset was published, but the single-ZIP download "
                    f"could not be built:\n\n{zi['error']}\n\n"
                    "The page (whole-dataset / folder / file downloads) still "
                    "works.")
            if permanent:
                zip_ok = bool(zi and zi.get("url"))
                self.status_var.set(
                    f"Published dataset ready · {payload['n']} file(s)"
                    + (" · ZIP included." if zip_ok else "."))
                desc = (
                    f"Permanent published dataset · {payload['n']} file(s), "
                    f"{human_bytes(payload['total'])}. This one link opens a "
                    f"browseable page in any browser — anyone can download the "
                    f"whole dataset, a folder, or individual files, with no "
                    f"account and no expiry. Ideal for citing or publishing "
                    f"data.")
                if zip_ok:
                    desc += (f" A one-click 'Download entire dataset (ZIP, "
                             f"{human_bytes(zi['size'])})' button is also at "
                             f"the top of the page.")
            else:
                self.status_var.set(
                    f"Shareable link ready for {payload['n']} file(s).")
                desc = (
                    f"Browseable page · {payload['n']} file(s), "
                    f"{human_bytes(payload['total'])}. Send this one link to "
                    f"anyone — it opens in any browser and lets them view or "
                    f"download files, or download whole folders. It works "
                    f"everywhere and expires in {self._human_duration(expires)}.")
            self._show_single_link(payload["url"], iid, expires,
                                   description=desc,
                                   verified=payload.get("verified"))

        self._bg_call(work, done)

    @staticmethod
    def _human_duration(secs: int) -> str:
        if secs % 86400 == 0:
            d = secs // 86400
            return f"{d} day" + ("s" if d != 1 else "")
        if secs % 3600 == 0:
            h = secs // 3600
            return f"{h} hour" + ("s" if h != 1 else "")
        m = max(1, secs // 60)
        return f"{m} minute" + ("s" if m != 1 else "")

    def _build_links_html(self, title_path, files, expires, total,
                          permanent: bool = False, zip_info=None) -> str:
        """Self-contained file-browser page: expandable folder tree, download
        any file, a folder, or the whole dataset. `permanent` switches the
        wording/banner between an expiring share and a published (public,
        no-expiry) dataset.

        Whole-dataset / folder download preserves the original subfolder
        structure on browsers that support the File System Access API
        (Chrome/Edge) — the page fetches each object (same S3 host, so no CORS)
        and writes it into a user-chosen folder, recreating subdirectories.
        Other browsers fall back to saving files individually into Downloads
        (folder path kept in each file's name).

        `zip_info` (permanent only), when it has a 'url', also adds a one-click
        single-ZIP download button that works in every browser."""
        T = THEME
        from html import escape
        generated = datetime.now().strftime("%Y-%m-%d %H:%M")
        # Embed the file list as JSON; neutralise any "</script>" in the data.
        data = json.dumps(files, separators=(",", ":")).replace("<", "\\u003c")
        zip_button = ""
        if zip_info and zip_info.get("url"):
            zip_button = (
                f'<a class="zipbtn" href="{escape(zip_info["url"])}">'
                f'⬇ Download entire dataset — one ZIP '
                f'({human_bytes(zip_info["size"])})</a>')
        struct_note = (
            "ℹ “Download whole dataset” / “Download folder”: in Chrome or Edge "
            "you'll be asked to pick a destination folder once, then the "
            "original subfolders and files are recreated there exactly as on "
            "Pawsey. In other browsers files save individually into your "
            "Downloads folder (the folder path is kept in each file's name); "
            "allow “multiple downloads” if asked.")
        if permanent:
            title_prefix = "Published dataset"
            meta_validity = "permanent public links (no expiry)"
            zip_note = ("ℹ Easiest for everyone: the “Download entire dataset "
                        "— one ZIP” button above gives the whole dataset as a "
                        "single file (unzip to restore the folders).<br>"
                        if zip_button else "")
            note_html = (
                "ℹ These are permanent public links — anyone with this page "
                "can download the whole dataset, a folder, or individual "
                "files, with no expiry and no account. Intended for open "
                "access.<br>" + zip_note + struct_note)
        else:
            title_prefix = "Shared files"
            meta_validity = (f"links valid for {self._human_duration(expires)} "
                             f"from generation")
            note_html = (
                "⚠ These are time-limited links — anyone with this page can "
                "download these files until the links expire. Don't post it "
                "publicly unless that's intended.<br>" + struct_note)
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title_prefix} — {escape(title_path)}</title>
<style>
 :root{{--accent:{T['accent']};--accent-dark:{T['accent_dark']};
   --border:{T['border']};--muted:{T['text_muted']}}}
 *{{box-sizing:border-box}}
 body{{font-family:'Segoe UI',Arial,sans-serif;background:{T['bg']};
   color:{T['text']};margin:0}}
 header{{background:#fff;border-bottom:3px solid var(--accent);padding:16px 28px}}
 h1{{margin:0;font-size:20px;color:var(--accent)}}
 .meta{{color:var(--muted);font-size:13px;margin-top:4px}}
 main{{padding:16px 28px;max-width:1100px}}
 .toolbar{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}}
 input.filter{{flex:1;min-width:180px;padding:8px 10px;border:1px solid var(--border);
   border-radius:6px;font-size:14px}}
 button{{font:inherit;cursor:pointer;border:1px solid var(--accent);background:var(--accent);
   color:#fff;padding:7px 12px;border-radius:6px}}
 button:hover{{background:var(--accent-dark)}}
 button.ghost{{background:#fff;color:var(--accent)}}
 button.ghost:hover{{background:{T['accent_soft']}}}
 .tree{{background:#fff;border:1px solid var(--border);border-radius:8px;
   box-shadow:0 1px 3px rgba(0,0,0,.06);overflow:hidden}}
 details{{border-top:1px solid {T['surface_alt']}}}
 details>summary{{list-style:none;cursor:pointer;padding:8px 12px;display:flex;
   align-items:center;gap:8px;user-select:none}}
 details>summary::-webkit-details-marker{{display:none}}
 summary:hover{{background:{T['surface_alt']}}}
 .caret{{transition:transform .15s;color:var(--muted)}}
 details[open]>summary .caret{{transform:rotate(90deg)}}
 .fname{{flex:1;font-weight:600}}
 .count{{color:var(--muted);font-size:12px;font-weight:400}}
 .children{{padding-left:20px;border-left:2px solid {T['surface_alt']};margin-left:18px}}
 .file{{display:flex;align-items:center;gap:8px;padding:6px 12px;
   border-top:1px solid {T['surface_alt']}}}
 .file .nm{{flex:1}}
 .file a{{color:var(--accent);text-decoration:none}}
 .file a:hover{{text-decoration:underline}}
 .sz{{color:var(--muted);font-size:12px;white-space:nowrap;min-width:80px;text-align:right}}
 .mini{{padding:3px 8px;font-size:12px}}
 .note{{color:{T['warning']};font-size:13px;margin-top:16px;line-height:1.5}}
 .primary{{font-size:15px;padding:10px 16px;font-weight:600}}
 .zipbtn{{display:inline-flex;align-items:center;gap:8px;background:var(--accent);
   color:#fff;text-decoration:none;font-weight:600;font-size:15px;
   padding:11px 18px;border-radius:8px;box-shadow:0 2px 6px rgba(0,0,0,.15)}}
 .zipbtn:hover{{background:var(--accent-dark)}}
 #toast{{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);
   background:{T['text']};color:#fff;padding:10px 18px;border-radius:8px;
   font-size:14px;opacity:0;transition:opacity .2s;pointer-events:none}}
 #toast.show{{opacity:.95}}
</style></head><body>
<header>
  <h1>📦 {title_prefix} — {escape(title_path)}</h1>
  <div class="meta">Generated {generated} · {len(files)} file(s) · {human_bytes(total)}
    · {meta_validity}.</div>
</header>
<main>
  {('<div style="margin:0 0 14px">' + zip_button + '</div>') if zip_button else ''}
  <div class="toolbar">
    <button class="primary" onclick="dl('')">⬇ Download whole dataset ({len(files)} files)</button>
    <input class="filter" placeholder="Filter by name…" oninput="filt(this.value)">
  </div>
  <div id="tree" class="tree"></div>
  <p class="note">{note_html}</p>
</main>
<div id="toast"></div>
<script>
const FILES = {data};
// ---- build nested tree from "a/b/c.txt" paths ----
const root = {{dirs:{{}}, files:[]}};
for (const f of FILES) {{
  const parts = f.p.split('/'); let node = root;
  for (let i=0;i<parts.length-1;i++) {{
    node.dirs[parts[i]] = node.dirs[parts[i]] || {{dirs:{{}}, files:[]}};
    node = node.dirs[parts[i]];
  }}
  node.files.push(f);
}}
function countFiles(node) {{
  let n = node.files.length;
  for (const k in node.dirs) n += countFiles(node.dirs[k]);
  return n;
}}
function sizeOf(node) {{
  let s = node.files.reduce((a,f)=>a+f.s,0);
  for (const k in node.dirs) s += sizeOf(node.dirs[k]);
  return s;
}}
function hb(n) {{
  const u=['B','KB','MB','GB','TB']; let i=0;
  while (n>=1024 && i<u.length-1) {{ n/=1024; i++; }}
  return n.toFixed(i?1:0)+' '+u[i];
}}
function esc(s) {{ const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }}
function renderDir(node, prefix) {{
  let html = '';
  const names = Object.keys(node.dirs).sort((a,b)=>a.localeCompare(b));
  for (const name of names) {{
    const child = node.dirs[name];
    const full = prefix ? prefix+'/'+name : name;
    const n = countFiles(child);
    html += `<details data-path="${{esc(full)}}">`
      + `<summary><span class="caret">▶</span>`
      + `<span class="fname">📁 ${{esc(name)}}</span>`
      + `<span class="count">${{n}} file${{n!=1?'s':''}} · ${{hb(sizeOf(child))}}</span>`
      + `<button class="ghost mini" onclick="event.preventDefault();dl('${{esc(full)}}')">⬇ Download folder</button>`
      + `</summary><div class="children">${{renderDir(child, full)}}</div></details>`;
  }}
  const fs = node.files.slice().sort((a,b)=>a.p.localeCompare(b.p));
  for (const f of fs) {{
    const base = f.p.split('/').pop();
    html += `<div class="file" data-name="${{esc(f.p.toLowerCase())}}">`
      + `<span class="nm">📄 <a href="${{esc(f.v)}}" target="_blank" rel="noopener">${{esc(base)}}</a></span>`
      + `<span class="sz">${{hb(f.s)}}</span>`
      + `<a class="ghost mini" style="text-decoration:none;border:1px solid var(--accent);border-radius:6px" href="${{esc(f.d)}}">⬇</a>`
      + `</div>`;
  }}
  return html;
}}
document.getElementById('tree').innerHTML = renderDir(root, '');
// ---- download (whole dataset / folder / file) ----
// Chrome/Edge (File System Access API + secure context) recreate the folder
// structure into a user-picked directory; other browsers fall back to flat
// per-file downloads.
const FS_OK = ('showDirectoryPicker' in window) && window.isSecureContext;
let toastT;
function toast(msg) {{
  const t=document.getElementById('toast'); t.textContent=msg; t.classList.add('show');
  clearTimeout(toastT); toastT=setTimeout(()=>t.classList.remove('show'), 3000);
}}
function under(prefix) {{
  return prefix==='' ? FILES
    : FILES.filter(f => f.p===prefix || f.p.startsWith(prefix+'/'));
}}
async function dl(prefix) {{
  const sel = under(prefix);
  if (!sel.length) {{ toast('No files here.'); return; }}
  if (FS_OK) return dlStructured(sel);
  return dlFlat(sel);
}}
async function dlStructured(sel) {{
  let dir;
  try {{ dir = await window.showDirectoryPicker({{mode:'readwrite'}}); }}
  catch(e) {{ toast('Cancelled.'); return; }}
  let ok=0, fail=0;
  for (let i=0;i<sel.length;i++) {{
    const f = sel[i];
    const parts = f.p.split('/');
    const name = parts.pop();
    try {{
      let d = dir;
      for (const p of parts) {{ d = await d.getDirectoryHandle(p, {{create:true}}); }}
      const fh = await d.getFileHandle(name, {{create:true}});
      const w = await fh.createWritable();
      const resp = await fetch(f.d);
      if (!resp.ok || !resp.body) throw new Error('HTTP '+resp.status);
      await resp.body.pipeTo(w);
      ok++;
    }} catch(e) {{ fail++; }}
    if (i%2===0 || i===sel.length-1)
      toast('Saving '+(i+1)+' / '+sel.length+'…');
  }}
  toast('Done — '+ok+' file'+(ok!=1?'s':'')+' saved'
        +(fail?(', '+fail+' failed'):'')+' (folders preserved).');
}}
async function dlFlat(sel) {{
  toast('Starting '+sel.length+' download'+(sel.length!=1?'s':'')+'…');
  for (let i=0;i<sel.length;i++) {{
    const a=document.createElement('a'); a.href=sel[i].d; a.style.display='none';
    document.body.appendChild(a); a.click(); a.remove();
    if (i % 5 === 4) toast('Downloading '+(i+1)+' / '+sel.length+'…');
    await new Promise(r=>setTimeout(r, 350));   // stagger so the browser keeps up
  }}
  toast('All '+sel.length+' download'+(sel.length!=1?'s':'')+' triggered.');
}}
// ---- live filter ----
function filt(q) {{
  q=q.trim().toLowerCase();
  document.querySelectorAll('.file').forEach(el=>{{
    el.style.display = !q || el.dataset.name.includes(q) ? '' : 'none';
  }});
  document.querySelectorAll('details').forEach(d=>{{
    const any=[...d.querySelectorAll('.file')].some(f=>f.style.display!=='none');
    d.style.display = !q || any ? '' : 'none';
    if (q && any) d.open = true;
  }});
}}
</script>
</body></html>"""

    def _open_url(self, url: str) -> None:
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Couldn't open browser:\n{e}")

    def _storage_download_selected(self) -> None:
        items = self._storage_selected_items()
        items = [(r, i, t) for (r, i, t) in items if t != "bucket"]
        if not items:
            messagebox.showerror(APP_NAME,
                                 "Select one or more files or folders first "
                                 "(buckets can't be downloaded directly).")
            return

        if len(items) == 1 and items[0][2] == "file":
            # Single file: let the user pick the exact save name
            remote, iid, _ = items[0]
            name = iid.split("/")[-1] or iid
            local = filedialog.asksaveasfilename(initialfile=name,
                                                 title="Save file as")
            if not local:
                return
            base = None
            plan = [(f"{remote}:{iid}", local, "file")]
        else:
            # Multiple items (or a folder): pick one destination directory and
            # download each item into it, keeping its own name.
            base = filedialog.askdirectory(
                title=f"Choose a folder to download {len(items)} item(s) into")
            if not base:
                return
            plan = []
            for remote, iid, type_ in items:
                name = iid.split("/")[-1] or iid
                dest_local = str(Path(base) / name)
                plan.append((f"{remote}:{iid}", dest_local, type_))

        self._dl_queue = plan
        self._dl_done = 0
        self._dl_failed = 0
        self._dl_total = len(plan)
        self.status_var.set(f"Downloading {self._dl_total} item(s)…")
        self._process_next_download()

    def _process_next_download(self) -> None:
        if not self._dl_queue:
            msg = (f"Downloaded {self._dl_done} item(s)."
                   if not self._dl_failed else
                   f"Downloaded {self._dl_done}; {self._dl_failed} failed "
                   f"(see log).")
            self.status_var.set(msg)
            messagebox.showinfo(APP_NAME, msg)
            return
        full, local, type_ = self._dl_queue.pop(0)
        if type_ == "file":
            cmd = ["copyto", full, local, "--s3-disable-checksum"]
        else:
            cmd = ["copy", full, local, "--s3-disable-checksum",
                   *EMPTY_DIR_FLAGS, "--transfers=4"]

        def work():
            if type_ != "file":
                # An empty Pawsey folder downloads as nothing unless the
                # local folder itself is created (rclone only replicates
                # the folders INSIDE the source, never the root).
                try:
                    Path(local).mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass
            return run_rclone_capture(cmd, timeout=86400 * 2)

        def done(result, err):
            ok = (not err) and result and result[0] == 0
            if ok:
                self._dl_done += 1
            else:
                self._dl_failed += 1
                out = (result[1] if result else str(err))
                self._append_output(f"\n[download failed] {full}: {out}\n")
            self.status_var.set(
                f"Downloading… {self._dl_done + self._dl_failed}/{self._dl_total}")
            self._process_next_download()

        self._bg_call(work, done)

    # ----- Search --------------------------------------------------------

    def _storage_search(self) -> None:
        q = self.storage_search_var.get().strip()
        if not q:
            messagebox.showinfo(APP_NAME, "Type a search term first.")
            return
        remote = self.storage_remote.get().strip()
        if not remote:
            messagebox.showerror(APP_NAME, "Pick a remote first.")
            return

        # Scope: the bucket containing the current selection, else all buckets
        scope = ""
        sel = self.storage_tree.selection()
        if sel:
            iid = sel[0]
            scope = iid.split("/")[0]   # bucket name
        target = f"{remote}:{scope}" if scope else f"{remote}:"
        scope_desc = scope if scope else "(all buckets)"

        self.status_var.set(f"Searching '{q}' in {scope_desc}…")

        def work():
            rc, out = run_rclone_capture(
                ["lsjson", target, "--recursive", "--files-only",
                 *FAST_LIST_FLAGS],
                timeout=1800)
            if rc != 0:
                raise RuntimeError(out)
            items = json.loads(out or "[]")
            ql = q.lower()
            matches = [it for it in items
                       if ql in (it.get("Name", "") or "").lower()
                       or ql in (it.get("Path", "") or "").lower()]
            return matches

        def done(matches, err):
            self.status_var.set(
                "Search done." if err is None else "Search failed.")
            if err:
                messagebox.showerror(APP_NAME, f"Search failed:\n{err}")
                return
            self._show_search_results(matches, q, scope_desc, scope)

        self._bg_call(work, done)

    def _show_search_results(self, matches, query, scope_desc, scope_bucket):
        win = tk.Toplevel(self.root)
        win.title(f"Search: '{query}'")
        win.geometry("950x520")

        ttk.Label(
            win,
            text=f"{len(matches)} match(es) for '{query}' in {scope_desc}",
            style="Big.TLabel",
        ).pack(padx=10, pady=(10, 4), anchor="w")

        tree = ttk.Treeview(
            win, columns=("path", "size", "modified"),
            show="headings", height=22, selectmode="browse")
        tree.heading("path", text="Path")
        tree.heading("size", text="Size")
        tree.heading("modified", text="Modified")
        tree.column("path", width=620)
        tree.column("size", width=100, anchor="e")
        tree.column("modified", width=160)
        tree.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        for m in matches:
            path = m.get("Path", "")
            size = m.get("Size", 0)
            mt = (m.get("ModTime", "") or "")[:16].replace("T", " ")
            tree.insert("", "end",
                        values=(path,
                                human_bytes(size) if size >= 0 else "?",
                                mt))

        def reveal():
            sel = tree.selection()
            if not sel:
                return
            path = tree.item(sel[0])["values"][0]
            # Build full iid: prepend bucket if we scoped to one
            full_iid = (f"{scope_bucket}/{path}" if scope_bucket else path)
            self._reveal_in_tree(full_iid)
            win.destroy()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bar, text="Reveal in tree",
                   command=reveal).pack(side="left")
        ttk.Button(bar, text="Close",
                   command=win.destroy).pack(side="right")
        tree.bind("<Double-1>", lambda e: reveal())

    def _reveal_in_tree(self, full_iid: str) -> None:
        """Expand the main tree down to `full_iid` (a path like
        'bucket/sub/file') and select it. Loads lazily as needed."""
        if not full_iid:
            return
        parts = full_iid.split("/")
        # Walk down progressively, loading children as needed
        current = ""
        def step(i):
            nonlocal current
            if i >= len(parts):
                # Final selection
                if self.storage_tree.exists(full_iid):
                    self.storage_tree.see(full_iid)
                    self.storage_tree.selection_set(full_iid)
                    self._on_tree_select()
                return
            current = parts[0] if i == 0 else "/".join(parts[:i + 1])
            if not self.storage_tree.exists(current):
                # Tree not loaded enough; refresh root and abort fancy reveal
                self._storage_refresh_root()
                return
            # Force-load if not yet loaded
            if current not in self._tree_loaded and i < len(parts) - 1:
                kids = self.storage_tree.get_children(current)
                if len(kids) == 1 and kids[0].endswith("::LOAD"):
                    self.storage_tree.delete(kids[0])
                # Inline load (synchronously for reveal correctness)
                remote = self.storage_remote.get().strip()
                rc, out = run_rclone_capture(
                    ["lsjson", f"{remote}:{current}", *FAST_LIST_FLAGS],
                    timeout=900)
                if rc == 0:
                    try:
                        items = json.loads(out or "[]")
                    except json.JSONDecodeError:
                        items = []
                    items.sort(key=lambda x: (not x.get("IsDir"),
                                              (x.get("Name", "") or "").lower()))
                    for item in items:
                        n = item.get("Name", "")
                        if not n:
                            continue
                        child_iid = f"{current}/{n}"
                        if self.storage_tree.exists(child_iid):
                            continue
                        is_dir = bool(item.get("IsDir"))
                        modtime = (item.get("ModTime", "") or "")[:16].replace("T", " ")
                        if is_dir:
                            self.storage_tree.insert(
                                current, "end", iid=child_iid,
                                text=f"📁 {n}", values=("folder", "…", modtime))
                            self.storage_tree.insert(
                                child_iid, "end", iid=f"{child_iid}::LOAD",
                                text="(loading…)", values=("", "", ""))
                        else:
                            size = item.get("Size", 0)
                            self.storage_tree.insert(
                                current, "end", iid=child_iid,
                                text=f"📄 {n}",
                                values=("file",
                                        human_bytes(size) if size >= 0 else "?",
                                        modtime))
                    self._tree_loaded.add(current)
            self.storage_tree.item(current, open=True)
            step(i + 1)
        step(0)


    # ------------------------------------------------------------- Console
    def _build_console_tab(self) -> None:
        """A built-in command console for running rclone (or any) commands and
        seeing their output live, without leaving the app."""
        T = THEME
        t = self.tab_console
        t.columnconfigure(0, weight=1)
        t.rowconfigure(2, weight=1)

        ttk.Label(t, text="Command console", style="Big.TLabel").grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 2))
        ttk.Label(
            t,
            text="Type a command and press Enter (or click Run). With "
                 "'rclone' prefix ticked you can type just the rclone "
                 "arguments, e.g.  lsd pawsey:  or  size pawsey:my-bucket. "
                 "Untick it to run any other command. ↑/↓ recalls history.",
            style="Muted.TLabel", wraplength=1040, justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))

        # Output area
        out_frame = ttk.Frame(t)
        out_frame.grid(row=2, column=0, sticky="nsew", padx=10)
        out_frame.columnconfigure(0, weight=1)
        out_frame.rowconfigure(0, weight=1)
        self.console_output = scrolledtext.ScrolledText(
            out_frame, height=20, wrap="word", state="disabled",
            bg="#10242E", fg="#E6F0F3", insertbackground="#E6F0F3",
            font=("Consolas", 10), relief="flat", borderwidth=0)
        self.console_output.grid(row=0, column=0, sticky="nsew")
        self.console_output.tag_configure("cmd", foreground="#7FD0E8",
                                          font=("Consolas", 10, "bold"))
        self.console_output.tag_configure("err", foreground="#F2A6A6")
        self.console_output.tag_configure("ok", foreground="#A6E3A6")

        # Input row
        inrow = ttk.Frame(t)
        inrow.grid(row=3, column=0, sticky="ew", padx=10, pady=(8, 4))
        inrow.columnconfigure(1, weight=1)

        self.console_prefix_rclone = tk.BooleanVar(value=True)
        ttk.Checkbutton(inrow, text="rclone", variable=self.console_prefix_rclone
                        ).grid(row=0, column=0, padx=(0, 6))
        self.console_entry = ttk.Entry(inrow, font=("Consolas", 10))
        self.console_entry.grid(row=0, column=1, sticky="ew")
        self.console_entry.bind("<Return>", lambda e: self._console_run())
        self.console_entry.bind("<Up>", self._console_history_prev)
        self.console_entry.bind("<Down>", self._console_history_next)
        self.console_run_btn = ttk.Button(inrow, text="Run",
                                           style="Accent.TButton",
                                           command=self._console_run)
        self.console_run_btn.grid(row=0, column=2, padx=(6, 0))
        self.console_stop_btn = ttk.Button(inrow, text="Stop", state="disabled",
                                           command=self._console_stop)
        self.console_stop_btn.grid(row=0, column=3, padx=(6, 0))
        ttk.Button(inrow, text="Clear", command=self._console_clear).grid(
            row=0, column=4, padx=(6, 0))

        # Quick-command shortcuts
        quick = ttk.Frame(t)
        quick.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 10))
        ttk.Label(quick, text="Quick:", style="Muted.TLabel").pack(side="left")
        for label, cmd in (
            ("Version", "version"),
            ("List remotes", "listremotes"),
            ("List buckets", f"lsd {self.cfg.get('remote_name', 'pawsey')}:"),
            ("About selected remote", f"about {self.cfg.get('remote_name', 'pawsey')}:"),
        ):
            ttk.Button(quick, text=label,
                       command=lambda c=cmd: self._console_fill(c)
                       ).pack(side="left", padx=3)

        # Console state
        self.console_proc: Optional[subprocess.Popen] = None
        self.console_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._console_history: list[str] = []
        self._console_hist_idx = 0
        self.root.after(120, self._console_drain)

    def _console_fill(self, cmd: str) -> None:
        self.console_prefix_rclone.set(True)
        self.console_entry.delete(0, "end")
        self.console_entry.insert(0, cmd)
        self.console_entry.focus_set()

    def _console_history_prev(self, _e=None):
        if not self._console_history:
            return "break"
        self._console_hist_idx = max(0, self._console_hist_idx - 1)
        self.console_entry.delete(0, "end")
        self.console_entry.insert(0, self._console_history[self._console_hist_idx])
        return "break"

    def _console_history_next(self, _e=None):
        if not self._console_history:
            return "break"
        self._console_hist_idx = min(len(self._console_history),
                                     self._console_hist_idx + 1)
        self.console_entry.delete(0, "end")
        if self._console_hist_idx < len(self._console_history):
            self.console_entry.insert(0, self._console_history[self._console_hist_idx])
        return "break"

    # Patterns that delete/destroy data - we double-check before running.
    _CONSOLE_DESTRUCTIVE = (
        "delete", "purge", "rmdir", "rmdirs", "deletefile", "cleanup",
        " rm ", " del ", "format", "config delete",
    )

    def _console_append(self, text: str, tag: str = "") -> None:
        self.console_output.configure(state="normal")
        if tag:
            self.console_output.insert("end", text, tag)
        else:
            self.console_output.insert("end", text)
        self.console_output.see("end")
        self.console_output.configure(state="disabled")

    def _console_clear(self) -> None:
        self.console_output.configure(state="normal")
        self.console_output.delete("1.0", "end")
        self.console_output.configure(state="disabled")

    def _console_run(self) -> None:
        if self.console_proc is not None:
            messagebox.showinfo(APP_NAME, "A command is already running. Stop it "
                                          "first or wait for it to finish.")
            return
        raw = self.console_entry.get().strip()
        if not raw:
            return
        use_rclone = bool(self.console_prefix_rclone.get())

        # Build the command line.
        if use_rclone:
            if not RCLONE_EXE:
                messagebox.showerror(APP_NAME, "rclone path is not set. Set it on "
                                               "the Settings tab.")
                return
            cmdline = f'"{RCLONE_EXE}" {raw}'
            display = f"rclone {raw}"
        else:
            cmdline = raw
            display = raw

        # Destructive-command guard.
        low = f" {raw.lower()} "
        if any(p in low for p in self._CONSOLE_DESTRUCTIVE):
            if not messagebox.askyesno(
                    "Confirm potentially destructive command",
                    f"This command can permanently delete or destroy data:\n\n"
                    f"  {display}\n\nRun it anyway?"):
                return

        # History
        self._console_history.append(raw)
        self._console_hist_idx = len(self._console_history)

        self._console_append(f"\n$ {display}\n", "cmd")
        self.console_entry.delete(0, "end")
        self.console_run_btn.configure(state="disabled")
        self.console_stop_btn.configure(state="normal")
        self.status_var.set(f"Console: running '{display}'…")

        # _subprocess_kwargs() already supplies stdout=PIPE, stderr=STDOUT
        # (merged), text + bufsize, and the no-window flags. Add stdin + a
        # forgiving UTF-8 decode so odd bytes never crash the reader.
        kwargs = _subprocess_kwargs()
        kwargs.update(stdin=subprocess.DEVNULL, encoding="utf-8",
                      errors="replace")
        try:
            self.console_proc = subprocess.Popen(cmdline, shell=True, **kwargs)
        except Exception as e:
            self._console_append(f"Failed to start: {e}\n", "err")
            self._console_finish(-1)
            return
        threading.Thread(target=self._console_reader,
                         args=(self.console_proc,), daemon=True).start()

    def _console_reader(self, proc: subprocess.Popen) -> None:
        try:
            for line in iter(proc.stdout.readline, ""):
                self.console_queue.put(("line", line))
            proc.stdout.close()
        except Exception:
            pass
        rc = proc.wait()
        self.console_queue.put(("done", str(rc)))

    def _console_drain(self) -> None:
        try:
            while True:
                kind, payload = self.console_queue.get_nowait()
                if kind == "line":
                    self._console_append(payload)
                elif kind == "done":
                    self._console_finish(int(payload))
        except queue.Empty:
            pass
        self.root.after(120, self._console_drain)

    def _console_finish(self, rc: int) -> None:
        if rc == 0:
            self._console_append("✓ done (exit 0)\n", "ok")
            self.status_var.set("Console: command finished.")
        else:
            self._console_append(f"✗ exit code {rc}\n", "err")
            self.status_var.set(f"Console: command exited with code {rc}.")
        self.console_proc = None
        self.console_run_btn.configure(state="normal")
        self.console_stop_btn.configure(state="disabled")

    def _console_stop(self) -> None:
        if self.console_proc is not None:
            stop_process(self.console_proc)
            self._console_append("\n[stopped by user]\n", "err")

    # ------------------------------------------------------------- History
    def _build_history_tab(self) -> None:
        t = self.tab_history
        t.columnconfigure(0, weight=1)
        t.rowconfigure(1, weight=1)

        top = ttk.Frame(t)
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        ttk.Button(top, text="Refresh", command=self._refresh_history).pack(side="left")
        ttk.Button(top, text="Open log folder",
                   command=lambda: self._open_path(APP_DIR)).pack(side="left", padx=8)

        self.history_text = scrolledtext.ScrolledText(
            t, wrap="word",
            font=("Consolas" if IS_WINDOWS else "Monospace", 9))
        self.history_text.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        self.history_text.configure(state="disabled")

        self._refresh_history()

    def _refresh_history(self) -> None:
        self.history_text.configure(state="normal")
        self.history_text.delete("1.0", "end")
        if not LOG_FILE.exists():
            self.history_text.insert("end", "(no transfers logged yet)\n")
        else:
            try:
                with open(LOG_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                for ln in lines[-500:]:
                    try:
                        e = json.loads(ln)
                    except Exception:
                        continue
                    ts = e.get("timestamp", "?")
                    op = e.get("operation")
                    if "source" in e and "destination" in e and not op:
                        # Transfer-tab style entry
                        self.history_text.insert(
                            "end",
                            f"[{ts}] {e.get('mode', '').upper()} "
                            f"{e.get('source', '')} -> {e.get('destination', '')}\n"
                            f"   Notes: {e.get('notes', '').strip()}\n"
                            f"   Status: {e.get('status', '')}\n\n",
                        )
                    elif op:
                        # Storage-tab operation
                        target = e.get("destination") or e.get("target") or ""
                        src = e.get("source", "")
                        line = f"[{ts}] {op.upper().replace('_', ' ')}"
                        if src:
                            line += f": {src} -> {target}"
                        elif target:
                            line += f": {target}"
                        self.history_text.insert("end", line + "\n")
                        if e.get("notes"):
                            self.history_text.insert(
                                "end", f"   Notes: {e['notes'].strip()}\n")
                        self.history_text.insert(
                            "end", f"   Status: {e.get('status', '')}\n\n")
                    else:
                        self.history_text.insert(
                            "end",
                            f"[{ts}] {e.get('status', '')} (exit {e.get('exit_code', '?')})\n\n",
                        )
            except Exception as e:
                self.history_text.insert("end", f"Log read error: {e}\n")
        self.history_text.configure(state="disabled")

    def _open_path(self, p: Path) -> None:
        try:
            if IS_WINDOWS:
                os.startfile(str(p))  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Could not open folder:\n{e}")

    # ------------------------------------------------------------- Settings
    # ===== Multi-project (multiple Pawsey remotes) =======================

    def _migrate_projects(self) -> None:
        """Ensure the projects dict exists. Seed it from the legacy single-
        remote config the first time so existing users keep their setup."""
        projects = self.cfg.get("projects") or {}
        if not projects:
            name = (self.cfg.get("remote_name") or "").strip()
            access = self.cfg.get("access_key_id") or ""
            secret = self.cfg.get("secret_access_key") or ""
            if name and access and secret:
                projects = {name: {
                    "endpoint": self.cfg.get("endpoint", ""),
                    "access_key_id": access,
                    "secret_access_key": secret,
                    "provider": self.cfg.get("provider", "Ceph"),
                    "label": name,
                }}
                self.cfg.set("projects", projects)
                self.cfg.set("active_project", name)
                self.cfg.save()
        if not self.cfg.get("active_project") and projects:
            # Pick the current remote_name if present, else the first project.
            rn = self.cfg.get("remote_name")
            self.cfg.set("active_project",
                         rn if rn in projects else next(iter(projects)))
            self.cfg.save()

    def _projects(self) -> dict:
        return dict(self.cfg.get("projects") or {})

    def _project_names(self) -> list:
        return sorted(self._projects().keys())

    def _set_active_project(self, name: str, *, announce: bool = True) -> None:
        """Make `name` the app-wide default project and push its creds into the
        top-level config keys the rest of the app reads."""
        projects = self._projects()
        if name not in projects:
            return
        p = projects[name]
        self.cfg.set("active_project", name)
        self.cfg.set("remote_name", name)
        self.cfg.set("endpoint", p.get("endpoint", ""))
        self.cfg.set("access_key_id", p.get("access_key_id", ""))
        self.cfg.set("secret_access_key", p.get("secret_access_key", ""))
        self.cfg.set("provider", p.get("provider", "Ceph"))
        self.cfg.save()
        # Repoint the remote selectors on the other tabs.
        for var_name in ("remote_var", "buckets_remote", "storage_remote"):
            var = getattr(self, var_name, None)
            if var is not None:
                try:
                    var.set(name)
                except Exception:
                    pass
        if hasattr(self, "_refresh_remotes"):
            self._refresh_remotes()
        if hasattr(self, "_refresh_buckets"):
            try:
                self._refresh_buckets()
            except Exception:
                pass
        self.status_var.set(f"Active project: {name}")
        if announce:
            messagebox.showinfo(
                APP_NAME, f"'{name}' is now the active project.\n\n"
                          "The Transfer, Buckets and Storage tabs now default "
                          "to it. You can still pick any project from the "
                          "Remote dropdown on those tabs.")

    def _refresh_projects_ui(self) -> None:
        names = self._project_names()
        active = self.cfg.get("active_project", "")
        values = [f"★ {n}" if n == active else f"   {n}" for n in names]
        self.proj_combo["values"] = values
        # Keep the current selection if possible, else select the active one.
        cur = self._selected_project_name()
        target = cur if cur in names else (active if active in names else
                                           (names[0] if names else ""))
        if target:
            idx = names.index(target)
            self.proj_combo.current(idx)
            self._load_project_into_fields(target)
        else:
            self.proj_combo.set("")
            self._clear_project_fields()
        self.proj_active_label.configure(
            text=f"Active project: {active or '(none)'}")

    def _selected_project_name(self) -> str:
        val = self.proj_combo.get().strip()
        return val.lstrip("★ ").strip()

    def _clear_project_fields(self) -> None:
        for var in (self.s_name, self.s_endpoint, self.s_access, self.s_secret):
            var.set("")
        self.s_provider.set("Ceph")
        if not self.s_endpoint.get():
            self.s_endpoint.set("https://projects.pawsey.org.au")

    def _load_project_into_fields(self, name: str) -> None:
        p = self._projects().get(name, {})
        self.s_name.set(name)
        self.s_endpoint.set(p.get("endpoint", "https://projects.pawsey.org.au"))
        self.s_provider.set(p.get("provider", "Ceph"))
        self.s_access.set(p.get("access_key_id", ""))
        self.s_secret.set(p.get("secret_access_key", ""))

    def _on_project_selected(self, _e=None) -> None:
        name = self._selected_project_name()
        if name:
            self._load_project_into_fields(name)

    def _project_new(self) -> None:
        self.proj_combo.set("")
        self._clear_project_fields()
        self.s_name.set("")
        self.status_var.set("Enter the new project's details, then 'Save project'.")

    def _project_save(self) -> None:
        name = self.s_name.get().strip()
        endpoint = self.s_endpoint.get().strip()
        access = self.s_access.get().strip()
        secret = self.s_secret.get().strip()
        provider = self.s_provider.get().strip() or "Ceph"
        # Write the rclone remote first; if that fails, don't save a half-config.
        ok, msg = RcloneRemote.upsert_remote(name, endpoint, access, secret, provider)
        if not ok:
            messagebox.showerror(APP_NAME, msg)
            return
        projects = self._projects()
        first = not projects
        projects[name] = {
            "endpoint": endpoint, "access_key_id": access,
            "secret_access_key": secret, "provider": provider, "label": name,
        }
        self.cfg.set("projects", projects)
        self.cfg.save()
        # The very first project (or saving the active one) becomes active.
        if first or name == self.cfg.get("active_project"):
            self._set_active_project(name, announce=False)
        self._refresh_projects_ui()
        self._refresh_remotes()
        messagebox.showinfo(APP_NAME, msg)

    def _project_set_active(self) -> None:
        name = self._selected_project_name()
        if not name or name not in self._projects():
            messagebox.showerror(APP_NAME, "Pick a saved project first.")
            return
        self._set_active_project(name)
        self._refresh_projects_ui()

    def _project_test(self) -> None:
        name = self._selected_project_name() or self.s_name.get().strip()
        if not name:
            messagebox.showerror(APP_NAME, "Pick or name a project first.")
            return
        self.status_var.set(f"Testing connection to {name}…")
        ok, out = RcloneRemote.test_remote(name)
        if ok:
            messagebox.showinfo(
                APP_NAME, f"Connection to '{name}' OK.\n\nBuckets:\n{out or '(none)'}")
            self.status_var.set(f"{name}: connection OK.")
        else:
            messagebox.showerror(APP_NAME, f"Connection to '{name}' failed:\n{out}")
            self.status_var.set(f"{name}: connection failed.")

    def _project_remove(self) -> None:
        name = self._selected_project_name()
        projects = self._projects()
        if not name or name not in projects:
            messagebox.showerror(APP_NAME, "Pick a saved project to remove.")
            return
        if not messagebox.askyesno(
                APP_NAME,
                f"Remove project '{name}' from the app and from rclone?\n\n"
                "This only forgets the connection/credentials — it does NOT "
                "delete any data on Pawsey."):
            return
        RcloneRemote.delete_remote(name)
        projects.pop(name, None)
        self.cfg.set("projects", projects)
        # If we removed the active one, fall back to another project.
        if self.cfg.get("active_project") == name:
            if projects:
                self._set_active_project(next(iter(projects)), announce=False)
            else:
                self.cfg.set("active_project", "")
        self.cfg.save()
        self._refresh_projects_ui()
        self._refresh_remotes()
        self.status_var.set(f"Removed project '{name}'.")

    def _build_settings_tab(self) -> None:
        t = self.tab_settings
        for i in range(2):
            t.columnconfigure(i, weight=1)

        # rclone executable section
        re_frame = ttk.LabelFrame(t, text="rclone executable")
        re_frame.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 6))
        re_frame.columnconfigure(1, weight=1)

        self.s_rclone_path = tk.StringVar(value=RCLONE_EXE if RCLONE_EXE != "rclone" else self.cfg.get("rclone_path", ""))
        ttk.Label(re_frame, text="Path to rclone").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(re_frame, textvariable=self.s_rclone_path).grid(
            row=0, column=1, sticky="ew", padx=8, pady=6)
        re_btns = ttk.Frame(re_frame)
        re_btns.grid(row=0, column=2, padx=8, pady=6)
        ttk.Button(re_btns, text="Browse…",
                   command=self._pick_rclone).pack(side="left", padx=2)
        ttk.Button(re_btns, text="Auto-detect",
                   command=self._autodetect_rclone).pack(side="left", padx=2)
        ttk.Button(re_btns, text="Save",
                   command=self._save_rclone_path).pack(side="left", padx=2)
        self.s_rclone_status = ttk.Label(re_frame, text="", foreground="#1a7f37")
        self.s_rclone_status.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 6))
        self._update_rclone_status_label()

        # Pawsey projects section (multi-project)
        rs = ttk.LabelFrame(t, text="Pawsey projects  (each project = one rclone remote)")
        rs.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=6)
        rs.columnconfigure(1, weight=1)

        self.s_name = tk.StringVar(value=self.cfg.get("remote_name"))
        self.s_endpoint = tk.StringVar(value=self.cfg.get("endpoint"))
        self.s_access = tk.StringVar(value=self.cfg.get("access_key_id"))
        self.s_secret = tk.StringVar(value=self.cfg.get("secret_access_key"))
        self.s_provider = tk.StringVar(value=self.cfg.get("provider") or "Ceph")

        # Top row: project picker + active indicator
        picker = ttk.Frame(rs)
        picker.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 2))
        picker.columnconfigure(1, weight=1)
        ttk.Label(picker, text="Saved project:").grid(row=0, column=0, sticky="w")
        self.proj_combo = ttk.Combobox(picker, state="readonly", width=28)
        self.proj_combo.grid(row=0, column=1, sticky="w", padx=(6, 12))
        self.proj_combo.bind("<<ComboboxSelected>>", self._on_project_selected)
        self.proj_active_label = ttk.Label(picker, text="", style="Muted.TLabel")
        self.proj_active_label.grid(row=0, column=2, sticky="e")

        ttk.Separator(rs, orient="horizontal").grid(
            row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=4)

        # Editable fields for the selected/new project
        fields = [
            ("Project name (remote)", self.s_name, False),
            ("Endpoint URL", self.s_endpoint, False),
            ("Provider", self.s_provider, False),
            ("Access key ID", self.s_access, False),
            ("Secret access key", self.s_secret, True),
        ]
        for i, (label, var, secret) in enumerate(fields):
            r = i + 2
            ttk.Label(rs, text=label).grid(row=r, column=0, sticky="w", padx=8, pady=4)
            ttk.Entry(rs, textvariable=var, show="•" if secret else "").grid(
                row=r, column=1, sticky="ew", padx=8, pady=4)

        ttk.Label(rs, text="Tip: to add another project's storage you need its "
                          "access key, secret and bucket — see the Help tab. "
                          "'Set as active' makes a project the default across "
                          "all tabs.",
                  style="Muted.TLabel", wraplength=720, justify="left").grid(
            row=len(fields) + 2, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 4))

        btns = ttk.Frame(rs)
        btns.grid(row=len(fields) + 3, column=0, columnspan=2, sticky="ew",
                  padx=8, pady=8)
        ttk.Button(btns, text="New project",
                   command=self._project_new).pack(side="left", padx=2)
        ttk.Button(btns, text="Save project", style="Accent.TButton",
                   command=self._project_save).pack(side="left", padx=2)
        ttk.Button(btns, text="Set as active",
                   command=self._project_set_active).pack(side="left", padx=2)
        ttk.Button(btns, text="Test connection",
                   command=self._project_test).pack(side="left", padx=2)
        ttk.Button(btns, text="Remove", style="Danger.TButton",
                   command=self._project_remove).pack(side="right", padx=2)

        # Defaults section
        ds = ttk.LabelFrame(t, text="Defaults")
        ds.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))
        ds.columnconfigure(1, weight=1)

        self.s_project = tk.StringVar(value=self.cfg.get("default_project"))
        self.s_bucket = tk.StringVar(value=self.cfg.get("default_bucket"))
        self.s_source = tk.StringVar(value=self.cfg.get("default_source"))

        ttk.Label(ds, text="Default project").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(ds, textvariable=self.s_project).grid(row=0, column=1, sticky="ew", padx=8, pady=4)
        ttk.Label(ds, text="Default bucket").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(ds, textvariable=self.s_bucket).grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        ttk.Label(ds, text="Default source folder").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        srcrow = ttk.Frame(ds)
        srcrow.grid(row=2, column=1, sticky="ew", padx=8, pady=4)
        srcrow.columnconfigure(0, weight=1)
        ttk.Entry(srcrow, textvariable=self.s_source).grid(row=0, column=0, sticky="ew")
        ttk.Button(srcrow, text="Browse…",
                   command=lambda: self._pick_dir(self.s_source)).grid(row=0, column=1, padx=(6, 0))

        # Advanced section
        adv = ttk.LabelFrame(t, text="Advanced rclone options")
        adv.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))
        for i in range(4):
            adv.columnconfigure(i, weight=1)

        self.s_chunk = tk.StringVar(value=str(self.cfg.get("s3_chunk_size")))
        self.s_conc = tk.StringVar(value=str(self.cfg.get("s3_upload_concurrency")))
        self.s_transfers = tk.StringVar(value=str(self.cfg.get("transfers")))
        self.s_checkers = tk.StringVar(value=str(self.cfg.get("checkers")))
        self.s_retries = tk.StringVar(value=str(self.cfg.get("retries")))
        self.s_llr = tk.StringVar(value=str(self.cfg.get("low_level_retries")))
        self.s_bwlimit = tk.StringVar(value=str(self.cfg.get("bwlimit", "")))

        adv_rows = [
            ("S3 chunk size", self.s_chunk),
            ("Upload concurrency", self.s_conc),
            ("Transfers", self.s_transfers),
            ("Checkers", self.s_checkers),
            ("Retries", self.s_retries),
            ("Low-level retries", self.s_llr),
            ("Bandwidth limit (e.g. 10M, off=blank)", self.s_bwlimit),
        ]
        for i, (label, var) in enumerate(adv_rows):
            r, col = divmod(i, 2)
            ttk.Label(adv, text=label).grid(row=r, column=col * 2, sticky="w", padx=8, pady=4)
            ttk.Entry(adv, textvariable=var, width=14).grid(
                row=r, column=col * 2 + 1, sticky="w", padx=8, pady=4)

        # Security / password section (v2.1)
        sec = ttk.LabelFrame(t, text="Security  (password protects destructive "
                                     "options)")
        sec.grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))
        sec.columnconfigure(1, weight=1)
        self.s_password_status = ttk.Label(sec, text="", style="Muted.TLabel",
                                            wraplength=760, justify="left")
        self.s_password_status.grid(row=0, column=0, columnspan=3, sticky="w",
                                    padx=8, pady=(8, 4))
        secbtns = ttk.Frame(sec)
        secbtns.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))
        ttk.Button(secbtns, text="Set / change password…",
                   command=self._change_password).pack(side="left", padx=2)
        ttk.Button(secbtns, text="Reset to default…",
                   command=self._reset_password).pack(side="left", padx=2)
        ttk.Label(
            sec,
            text="The password is required to turn OFF 'Preview only' or "
                 "'Verify both sides', and to select Mirror or Two-way sync "
                 "mode. Default password is 'appn'.",
            style="Muted.TLabel", wraplength=760, justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))
        self._update_password_status()

        ttk.Button(t, text="Save defaults",
                   command=self._save_defaults).grid(row=5, column=1, sticky="e",
                                                    padx=10, pady=(0, 10))

        # Populate the project picker now that all widgets exist.
        self._refresh_projects_ui()

    # -------- password management (Settings tab, v2.1) --------
    def _update_password_status(self) -> None:
        if not hasattr(self, "s_password_status"):
            return
        if self.cfg.is_default_password():
            self.s_password_status.configure(
                text="Password: using the factory default ('appn'). "
                     "Set your own below.")
        else:
            self.s_password_status.configure(
                text="Password: a custom password is set.")

    def _change_password(self) -> None:
        current = simpledialog.askstring(
            "Current password",
            "Enter the CURRENT password"
            + ("  (default is 'appn')" if self.cfg.is_default_password() else "")
            + ":",
            show="•", parent=self.root)
        if current is None:
            return
        if not self.cfg.check_password(current):
            messagebox.showerror(APP_NAME, "Incorrect current password.")
            return
        new1 = simpledialog.askstring(
            "New password", "Enter the NEW password:", show="•",
            parent=self.root)
        if new1 is None:
            return
        new1 = new1.strip()
        if not new1:
            messagebox.showerror(APP_NAME, "Password cannot be empty.")
            return
        new2 = simpledialog.askstring(
            "Confirm password", "Re-enter the NEW password:", show="•",
            parent=self.root)
        if new2 is None:
            return
        if new1 != new2:
            messagebox.showerror(APP_NAME, "The two entries do not match. "
                                           "Password unchanged.")
            return
        self.cfg.set_password(new1)
        self._update_password_status()
        messagebox.showinfo(APP_NAME, "Password changed.")

    def _reset_password(self) -> None:
        current = simpledialog.askstring(
            "Confirm reset",
            "Enter the CURRENT password to reset it back to the default "
            "('appn'):",
            show="•", parent=self.root)
        if current is None:
            return
        if not self.cfg.check_password(current):
            messagebox.showerror(APP_NAME, "Incorrect current password.")
            return
        # Clearing the stored hash reverts to the factory default password.
        self.cfg.set("app_password_hash", "")
        self.cfg.save()
        self._update_password_status()
        messagebox.showinfo(APP_NAME,
                            "Password reset to the default ('appn').")

    # -------- rclone path helpers (used by the Settings tab) --------
    def _update_rclone_status_label(self) -> None:
        if RCLONE_EXE and RCLONE_EXE != "rclone":
            try:
                rc, out = run_rclone_capture(["version"], timeout=10)
                first_line = (out or "").splitlines()[0] if out else ""
                if rc == 0:
                    self.s_rclone_status.configure(
                        text=f"OK: {first_line}", foreground="#1a7f37")
                    return
            except Exception:
                pass
        self.s_rclone_status.configure(
            text="rclone not configured - the app will fail until this is set.",
            foreground="#a40000")

    def _pick_rclone(self) -> None:
        path = filedialog.askopenfilename(
            title="Locate rclone executable",
            filetypes=[("rclone executable", "rclone.exe rclone"),
                       ("All files", "*.*")],
            initialdir=str(Path(self.s_rclone_path.get()).parent)
            if self.s_rclone_path.get() else str(Path.home()),
        )
        if path:
            self.s_rclone_path.set(path)

    def _autodetect_rclone(self) -> None:
        found = find_rclone_executable("")  # ignore current config
        if found:
            self.s_rclone_path.set(found)
            messagebox.showinfo(APP_NAME, f"Found rclone at:\n{found}")
        else:
            messagebox.showwarning(
                APP_NAME,
                "Could not auto-detect rclone.\n"
                "Use 'Browse…' to point at rclone.exe manually.")

    def _save_rclone_path(self) -> None:
        global RCLONE_EXE
        path = self.s_rclone_path.get().strip()
        if path and not Path(path).is_file():
            messagebox.showerror(APP_NAME, f"Not a valid file:\n{path}")
            return
        self.cfg.set("rclone_path", path)
        self.cfg.save()
        RCLONE_EXE = path if path else "rclone"
        self._update_rclone_status_label()
        # Push fresh remote list to the Transfer tab
        if hasattr(self, "_refresh_remotes"):
            self._refresh_remotes()
        messagebox.showinfo(APP_NAME, "rclone path saved.")

    def _pick_dir(self, var: tk.StringVar) -> None:
        d = filedialog.askdirectory(initialdir=var.get() or str(Path.home()))
        if d:
            var.set(d)

    def _save_defaults(self) -> None:
        self.cfg.set("default_project", self.s_project.get().strip())
        self.cfg.set("default_bucket", self.s_bucket.get().strip())
        self.cfg.set("default_source", self.s_source.get().strip())
        # Bandwidth limit is a free-form rclone value (e.g. "10M", "" = off)
        self.cfg.set("bwlimit", self.s_bwlimit.get().strip())

        for key, var in [
            ("s3_chunk_size", self.s_chunk),
            ("s3_upload_concurrency", self.s_conc),
            ("transfers", self.s_transfers),
            ("checkers", self.s_checkers),
            ("retries", self.s_retries),
            ("low_level_retries", self.s_llr),
        ]:
            val = var.get().strip()
            if key == "s3_chunk_size":
                self.cfg.set(key, val)
            else:
                try:
                    self.cfg.set(key, int(val))
                except ValueError:
                    pass
        self.cfg.save()

        # Push current defaults into the Transfer tab
        self.src_var.set(self.cfg.get("default_source"))
        self.bucket_var.set(self.cfg.get("default_bucket"))
        self.remote_var.set(self.cfg.get("remote_name"))
        messagebox.showinfo(APP_NAME, "Defaults saved.")

    # --------------------------------------------------------------- Help
    def _build_help_tab(self) -> None:
        t = self.tab_help
        t.columnconfigure(0, weight=1)
        t.rowconfigure(0, weight=1)

        txt = scrolledtext.ScrolledText(t, wrap="word", padx=12, pady=12)
        txt.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        LOG_DIR = TRANSFER_LOG_DIR
        help_text = f"""\
{APP_NAME} v{APP_VERSION}
============================================================

What this app does
------------------
A friendly wrapper around `rclone` for transferring data to Pawsey Acacia
object storage. It manages your remote credentials, runs copy/sync jobs
with progress, lets you create or delete buckets safely, and visualises
how much space you are using.

Getting started
---------------
1. Open the SETTINGS tab.
   - Enter your Pawsey access key ID and secret access key.
   - Endpoint defaults to https://projects.pawsey.org.au (Acacia).
   - Click 'Save & apply to rclone' - this writes the remote into
     ~/.config/rclone/rclone.conf (Linux/Mac) or %APPDATA%\\rclone\\
     rclone.conf (Windows) via the official `rclone config` command.
   - Click 'Test connection' to confirm.

2. Open the TRANSFER tab.
   - Pick a source folder (defaults to this app's directory).
   - Confirm the destination remote, bucket, and optional sub-path.
   - IMPORTANT - how the source folder maps to the destination:
       By default the tool copies the CONTENTS of the source folder
       into the destination path (this is rclone's behaviour). So
       copying  E:\test-syn  to bucket  dpird-appn  path  2024  puts the
       files at  dpird-appn/2024/...  (NOT dpird-appn/2024/test-syn/...).
       If you want the source FOLDER ITSELF recreated at the destination,
       tick "Put the source folder itself at the destination" - then the
       files land at  dpird-appn/2024/test-syn/...  instead. The small
       blue "Files will be placed under: …" line always shows exactly
       where files will go, so check it before starting.
   - WRITE A NOTE about the data - this is mandatory and goes into
     the transfer log file.
   - Choose a mode:
       * Copy (one-way, additive) - uploads new/changed files; never
         deletes anything on Pawsey. Safe default for archiving.
       * Mirror (one-way, exact)  - makes Pawsey an exact replica of the
         source; DELETES anything on Pawsey not in the source.
       * Two-way sync (OneDrive-style) - keeps local and Pawsey identical
         in BOTH directions. A change on either side (add, edit, delete)
         is propagated to the other on the next run. This is real
         bidirectional sync via `rclone bisync`.
       * Resume previous transfer - pick any incomplete past transfer.
   - Click 'Start transfer'.
   - PREVIEW FIRST (optional): tick "Preview only (dry-run)" in Sync
     options to have rclone report exactly what it WOULD copy, change or
     delete without transferring or removing anything. Great for checking
     a Mirror or two-way sync before you commit. (With the box unticked,
     every transfer is a real live run - the app never silently previews.)
     A preview reports its findings in the conditional - "WOULD add 12,
     update 3, …" - so you can always tell a preview from a real run.

EMPTY FOLDERS
-------------
  Every mode (Copy, Mirror, Two-way sync) carries EMPTY folders across, in
  both directions, and the Storage tab's upload/download/copy/paste actions
  do too. If your dataset has placeholder folders with nothing in them yet,
  they will exist on Pawsey after the transfer.

  Why this needs special handling: Pawsey's Acacia is object storage, where
  a "folder" is not a real object - it is merely implied by the keys of the
  files inside it. An empty folder has no files to imply it, so it simply
  has nowhere to exist. The app therefore runs rclone with
  --create-empty-src-dirs --s3-directory-markers, which writes a zero-byte
  marker object named "<folder>/" to hold the folder open. rclone (and this
  app's Storage browser) shows those markers as ordinary folders, not as
  stray files.

  If you already uploaded a dataset before this behaviour was in place, the
  empty folders are missing on Pawsey. Just run the same transfer again -
  Copy will add the missing folders without re-uploading any files.

Multiple Pawsey projects (Settings tab)
---------------------------------------
  Each Pawsey project is a separate set of Acacia keys, managed here as a
  separate "project" (rclone remote). Most of the time you work in ONE active
  project, but you can save several and switch between them.
    - NEW PROJECT: click 'New project', fill in a project name (letters,
      numbers, - and _; no spaces), the endpoint
      (https://projects.pawsey.org.au), provider (Ceph), and the access
      key + secret, then 'Save project'.
    - SET AS ACTIVE: pick a saved project and click 'Set as active' (the
      active one is marked with a ★). All tabs then default to it; you can
      still pick any project from the Remote dropdown on each tab.
    - TEST / REMOVE: 'Test connection' lists its buckets; 'Remove' forgets
      the project and its keys (it never deletes data on Pawsey).

  COPYING BETWEEN PROJECTS: once two projects are saved, copy across them on
  the Storage tab — Copy/Cut in one project, switch the Remote dropdown to the
  other, then Paste — or use the Console: `copy projectA:bucket projectB:bucket`.
  If the two projects use different keys the data streams through your machine
  (not server-side), so run big migrations on a Pawsey/Nimbus VM.

  ACCESS TO SOMEONE ELSE'S PROJECT: you need an access key + secret that has
  permission on their bucket(s). Best options: ask their project owner to add
  you to the project (you make your own keys), or to grant your existing key
  read access to specific buckets via a bucket policy (this also enables fast
  server-side copies). Sharing raw keys works but is least secure.

New in v2.1
-----------
  SAFER DEFAULTS: "Preview only (dry-run)" and "Verify both sides after each
  transfer (check)" are now switched ON by default. A dry-run first shows you
  exactly what a sync would change before anything is transferred, and the
  auto-verify confirms both sides match after a real transfer.

  PASSWORD PROTECTION (Settings > Security): a password is now required to
    - turn OFF "Preview only" or "Verify both sides", and
    - select the destructive Mirror or Two-way sync modes.
  The default password is 'appn'. Set your own (or reset it back to the
  default) under Settings > Security. Changing it asks for the current
  password first.

  STORAGE PROGRESS: copy, cut, paste, delete and rename/move on the Storage
  tab now show a live progress bar (X/N items and a percentage) plus streamed
  output, so you can see the operation is actually working.

New in v2.0
-----------
  RENAMED: the app is now "Pawsey Data Management App (PDMA)" (formerly
  "Pawsey Uploader"). Your saved projects, keys and history carry over
  unchanged.

  PERMANENT PUBLIC LINKS for publishing datasets: the "Share / Publish
  link..." button now offers a PERMANENT option alongside the temporary
  (expiring) one. Permanent links never expire and are ideal for datasets you
  cite or publish. See "SHARE OR PUBLISH A LINK" below for details and the
  safety precautions.

New in v1.5
-----------
  ORGANISE ON PAWSEY (Storage tab) - works just like Windows Explorer:
    - Select a file/folder, click COPY or CUT, then select the destination
      bucket/folder and click PASTE HERE. Within the same remote this is a
      server-side copy/move on Pawsey - no download or re-upload. A note is
      required for the paste. A copy stays on the clipboard (paste again
      elsewhere); a cut is cleared once pasted.

  SHARE OR PUBLISH A LINK (Storage tab -> "Share / Publish link..."):
    Select a file, folder or bucket, then choose ONE of two kinds of link.
    Every link works from ANY browser, anywhere - the recipient needs no
    Pawsey account.

    1. TEMPORARY link (expiring) - the default, for day-to-day sharing.
       - Choose how long it works (minutes / hours / days; S3's maximum is
         7 days). After that the link stops working.
       - A FILE gives one link (copied to your clipboard).
       - A FOLDER or BUCKET builds a small file-browser web page (expand
         folders, view/download any file, or download a whole folder at
         once), uploads it into the bucket under "_shares/", and gives you
         ONE link to the page.
       - These are S3 "presigned URLs" built from your stored keys - rclone's
         own `link` command does not support expiring links on Pawsey's S3.

    2. PERMANENT link (public, never expires) - NEW in v2.0, for PUBLISHING
       datasets that must stay reachable forever (e.g. cited in a paper).
       - The app gives the object(s) a "public-read" ACL and hands back the
         plain, unsigned URL (https://endpoint/bucket/key), which never
         expires. A FOLDER/BUCKET is published as a browseable index page
         whose links are all permanent.
       - DOWNLOAD THE WHOLE DATASET, A FOLDER, OR SINGLE FILES: the published
         page has a "Download whole dataset" button plus a "Download folder"
         button on every folder. In Chrome or Edge the recipient picks a
         destination folder once and the ORIGINAL SUBFOLDER STRUCTURE is
         recreated on their disk (no ZIP needed); other browsers save the files
         individually into Downloads with the folder path kept in each name.
       - OPTIONAL SINGLE ZIP: when publishing a folder/bucket the app also
         offers to build ONE ZIP of the whole dataset. If you say yes, the page
         shows a "Download entire dataset - one ZIP" button too — a one-file
         download that works in EVERY browser. The ZIP is streamed together on
         your machine and uploaded into the bucket (extra storage ~= dataset
         size) and is a snapshot (re-publish to refresh).
       - This makes the data PUBLIC on the internet with no expiry. Anyone
         with the link can read it. Only publish data that is meant to be
         open - never anything personal, sensitive or embargoed. The app
         asks you to confirm before publishing.
       - Requires the bucket to ALLOW public access. If Pawsey has public
         access disabled for the bucket, the app tells you; ask Pawsey
         support to enable public read for that bucket.
       - To UN-publish later, remove or overwrite the object, or ask Pawsey
         to reset the bucket/object ACL to private.

    Folder downloads save files individually (the folder path is kept in each
    file's name). Tidy up old share/index pages from "_shares/" any time.

  COMMAND CONSOLE (Console tab):
    - Run any rclone command (tick 'rclone' and type just the arguments,
      e.g.  lsd pawsey:  ) or untick it to run any other command. Output
      streams live; use Stop to cancel. Up/Down arrows recall history, and
      the Quick buttons fill in common commands. Commands that can delete
      data ask for confirmation first.

Sync options (apply to the modes above)
---------------------------------------
  - "If the same file differs on both sides":
       * Keep newer version  - the more recently modified copy wins
         (the older one is removed). Recommended.
       * Keep both copies     - nothing is lost. In two-way sync, both
         versions are kept and numbered. In one-way Copy, the version
         being overwritten on Pawsey is moved into a timestamped
         _backups/ folder in the same bucket first.
       * Local always wins    - the local copy is authoritative.
       * Pawsey always wins    - the Pawsey copy is authoritative.
  - "Verify contents with checksums": compares files by MD5 hash rather
    than size+modtime. Slower but catches silent corruption. When on,
    the app also keeps S3 checksums during upload so later verification
    is reliable.
  - "Require matching folder names": before syncing, the app checks that
    the last folder name of the source matches the last folder name of
    the destination (e.g. local .../2026/Northam  <->  appn/2026/Northam).
    If they differ, the sync is BLOCKED (not just warned) - you must fix
    the source/destination so the names match, or untick this option to
    deliberately sync differently-named folders. This stops you
    accidentally syncing the wrong project into the wrong place.
  - "Verify both sides (check)" button: compares the current source and
    destination and reports whether they really hold the same content -
    without transferring anything. It does two passes:
      * FILES via `rclone check`, comparing checksums wherever Pawsey has
        them and falling back to size for the rest. If some files could
        only be compared by size, it says so and tells you how many -
        it never claims a full content match it did not make.
      * FOLDERS by listing both directory trees, which is the only way to
        catch an EMPTY folder present on one side and missing on the other
        (`rclone check` looks at files only and cannot see this).
    Tick "Verify contents with checksums" in Sync options and re-upload if
    you want every file checked by content rather than by size.

Two-way sync notes (important)
------------------------------
  - The FIRST two-way sync of a given local<->Pawsey pair needs a
    baseline. The app detects this and offers to run it (--resync).
    During the baseline, if a file exists on both sides the LOCAL copy
    is treated as authoritative. After that, both sides are equal
    partners and changes flow both ways.
  - Use the SAME local folder and SAME Pawsey path each time for a given
    project. The app remembers which pairs are initialised in
    bisync_pairs.json. Resuming a two-way sync never re-runs the
    baseline (that would discard remote-only changes).
  - IF THE BASELINE GOES MISSING: rclone keeps its own record of what both
    sides looked like after the last successful run, and it refuses to sync
    without it - otherwise it could not tell "you deleted this file" apart
    from "the other side gained it". That record can disappear if a run was
    interrupted, the rclone cache was cleared, or you moved to another
    machine. The app now recognises this specific failure, explains it, and
    offers to rebuild the baseline for you (the rebuild merges both sides
    and deletes nothing; where the same file differs, the local copy wins).
  - AFTER A TWO-WAY SYNC REPORTS "no changes", you can confirm it with the
    "Verify both sides (check)" button. The run summary only reports what
    rclone actually did; the verify button is what proves the two sides
    genuinely match.
  - A deletion safety cap protects against accidental mass-deletion in
    two-way sync. By default a sync that would delete more than 50% of the
    files on either side is aborted before anything is removed. You can
    change the percentage, or untick it entirely, in Sync options
    ("Abort two-way sync if it would delete more than N% of files"). If a
    sync stops on this cap and the deletions are intended (e.g. you cleaned
    up files), raise the percentage or untick the box, then run again.
  - Renames/moves on EITHER side: with "Detect renamed/moved files"
    ticked, a folder or file renamed locally OR on Pawsey is applied to
    the other side as a server-side move (no re-upload). Works in both
    Two-way sync and Mirror sync.
  - Seeing what changed: after each run the app reports a summary
    (added / updated / renamed / deleted counts) in the output and in
    the History log. Click "Show last changes" in Sync options for the
    full per-file list.
  - Auto-sync: tick "Auto-sync (Two-way) every N min" to keep a pair
    continuously in sync, OneDrive-style. After your first successful
    two-way sync, the app re-runs it on that interval automatically
    until you untick the box or close the app. Only Two-way sync is
    auto-repeated.
  - Bandwidth limit: set a value like "10M" in Settings -> Advanced to
    cap transfer speed (e.g. during work hours); blank = unlimited.

Recycle bin & version history
-----------------------------
Tick "Recycle bin: soft-delete & keep replaced versions" in Sync options
(on by default) to protect against accidental loss:

  - During Mirror sync, files that would be DELETED on Pawsey are instead
    moved into the bucket's _recycle_bin/<timestamp>/ prefix. During
    Two-way sync the same protection applies to the Pawsey side
    (--backup-dir2).
  - Files that are OVERWRITTEN (a new version uploaded) have their
    previous version moved into the same dated folder - so the recycle
    bin doubles as version history. Each sync run gets its own timestamp.
  - On the Storage tab, 'Delete selected' becomes a SOFT delete: the
    item is moved (server-side) into _recycle_bin/<timestamp>/ instead of
    being purged. Items already inside the recycle bin are always hard-
    deleted.

Managing the recycle bin:
  - Browse it like any folder: expand _recycle_bin in the Storage tree.
  - RESTORE: select an item inside _recycle_bin and use 'Rename / Move…'
    to move it back to where it belongs (server-side, no download).
  - EMPTY: Storage tab -> 'Recycle bin…' shows how much space the bin
    is using for the selected bucket and gives an 'Empty recycle bin'
    button. Emptying permanently purges it and frees the storage - this
    cannot be undone.
  - The recycle bin lives inside the same bucket, so it counts toward
    your Pawsey quota until you empty it. Empty it periodically.

3. If the computer shuts down mid-transfer, just open the app again,
   go to the TRANSFER tab, choose 'Resume previous transfer…', and
   click 'Start transfer'. A dialog lists all unfinished transfers
   from the past 50; pick one. rclone skips files that already
   finished uploading.

Long-running transfers (days / weeks / TB-scale)
------------------------------------------------
This version is designed to be left running unattended for long
periods. Built-in safeguards:

  - Sleep prevention: while a transfer is active on Windows, the
    app calls SetThreadExecutionState to keep the system awake.
    Released automatically when the transfer ends or you exit.
    (Linux: no portable equivalent in stdlib; use `systemd-inhibit`
    or `caffeine` for the same effect on desktop Linux. Servers
    generally don't sleep.)

  - Auto-restart on rclone crash: if rclone exits with a non-zero
    code (transient network error, etc.), the app waits 30s and
    automatically resumes - up to 10 attempts by default. Auto-
    restart is ABORTED immediately on:
      * Authentication failure (UserSuspended, AccessDenied, etc.) -
        a clear error dialog appears and you must fix credentials
        in Settings before resuming.
      * You clicking 'Stop' - the transfer becomes resumable but
        is not restarted automatically.
    Tunables: auto_restart, max_restart_attempts, restart_backoff_seconds
    in config.json.

  - Bounded output buffer: the on-screen log shows at most 10,000
    lines; older lines are trimmed. The COMPLETE verbose rclone
    output for every transfer is preserved on disk in:
        {LOG_DIR}
    One log file per transfer, named transfer_<id>.log.

  - Multiple resume slots: instead of remembering only the most
    recent transfer, the app keeps the last 50 transfers and their
    status. 'Resume previous transfer…' opens a picker so you can
    choose which incomplete transfer to continue.

  - Heartbeat file: every 15 seconds during a transfer, the app
    writes liveness info to:
        {HEARTBEAT_FILE}
    Contains the current source/destination, last progress line,
    restart count, and a timestamp. You can read this file from
    another shell / RDP / SSH session to confirm the transfer is
    still alive without touching the GUI.

Buckets tab
-----------
- Lists all buckets on the selected remote.
- 'Create bucket': prompts for a name (lowercase letters, digits and
  hyphens, 3-63 chars). Uses `rclone mkdir`.
- 'Delete bucket': requires you to TYPE the bucket name to confirm.
  Acacia has no recycle bin - deletion is permanent.

Storage tab
-----------
A live file-manager view of your Pawsey storage.

  - Pick a remote and click 'Refresh tree'. Buckets appear as expandable
    rows; click the arrow (or double-click) to drill into any folder.
    Children load on demand, so large buckets open quickly.
  - Each bucket row shows the total size of that bucket. Folder sizes
    fill in automatically as you expand them.
  - 'Tree last loaded N min ago' indicator shows how stale the view is.
  - The 'Search' box searches for files by name. If a bucket or item is
    selected, the search is limited to that bucket; otherwise it scans
    every bucket on the remote. Search results have a 'Reveal in tree'
    button that expands the main tree to the matching file.
  - Action buttons (Upload file, Upload folder, New folder, Rename/Move,
    Download, Delete) operate on the selected row. For uploads and 'New
    folder', if you have a file selected the action targets the file's
    parent folder.
  - 'Rename / Move…' renames or moves a file/folder on BOTH sides. On
    Pawsey it's a server-side move (no data transfer). If the item is part
    of a known local<->Pawsey sync pair and the matching local folder
    exists, the LOCAL folder is renamed the same way at the same time, so
    both sides stay identical immediately - the next two-way sync sees no
    changes (no re-upload, no re-download, no duplicate folders). The local
    rename happens first; if it fails, Pawsey is left unchanged so the two
    sides never diverge. You can rename in place (2024/old -> 2024/new) or
    move (2024/X -> 2025/X).
  - Any upload / delete / new-folder / rename action REQUIRES you to
    write a note in the 'Action note' box first. The note is saved to
    the transfer log permanently.
  - Delete actions show a permanent-deletion warning. There is no
    recycle bin on Pawsey.
  - The tree refreshes automatically after a transfer completes on
    the Transfer tab.

Folder renames without re-uploading (important for TB-scale)
------------------------------------------------------------
Object storage has no native "rename". If you rename a folder locally
and then run a plain Mirror sync, rclone sees the old folder as deleted
and the new folder as new - so it deletes the old one on Pawsey and
re-uploads the whole new one. Two ways to avoid that:

  1. SERVER-SIDE RENAME (most reliable): rename the folder on Pawsey to
     match, using Storage tab -> 'Rename / Move…'. The data never leaves
     Pawsey. Then your next sync sees both sides matching and transfers
     nothing.

  2. AUTOMATIC DETECTION during Mirror sync: tick "Detect renamed/moved
     files" in Sync options. rclone then matches files that moved and
     performs server-side moves instead of delete+re-upload
     (--track-renames). This uses modtime+size matching, which works for
     files uploaded by this app (rclone stores modification time). Note:
     this only applies to Mirror sync mode - in Copy mode a rename simply
     adds the new path and leaves the old one in place (nothing is
     deleted), so there's nothing to "track".

History tab
-----------
- Shows every transfer and storage action along with the notes you
  wrote, the source and destination, and the final status. The raw
  log is a JSON-Lines file at:
      {LOG_FILE}
- 'Open log folder' opens that directory in your OS file manager.

Configuration & data files
--------------------------
Stored in: {APP_DIR}
  - config.json          : your defaults and credentials (chmod 600
                           recommended on Linux)
  - transfer_log.jsonl   : permanent audit log of all transfers
  - resume.json          : rolling list of recent transfers
                           (used by 'Resume previous transfer…')
  - heartbeat.json       : liveness file for unattended runs
  - logs/                : per-transfer full rclone output

Tips & troubleshooting
----------------------
- "rclone not found": activate the conda env that has rclone before
  launching the app, or install rclone system-wide.
- 403 UserSuspended: your Pawsey access key has been disabled. Contact
  help@pawsey.org.au; no app setting can fix this. The app detects
  this and stops auto-restarting.
- 403 Forbidden on a HeadObject: the credentials don't have access to
  that bucket. Check that the remote you're using owns the bucket.
- Stop button: terminates rclone cleanly AND disables auto-restart
  for that transfer. Files uploaded so far stay on Pawsey; use
  'Resume previous transfer…' to finish what's left.
- Large files: if a single huge file is interrupted, rclone restarts
  THAT file from byte 0 (it cannot byte-resume one file). But all
  other already-completed files are skipped.
- Pending Windows updates: Windows may force-reboot for updates even
  with sleep prevention active. Defer update reboots from Windows
  settings if you're running a multi-day transfer.

Cross-platform notes
--------------------
- Works on Windows 10/11 and Ubuntu 20.04+. macOS is not specifically
  tested but should also work with a modern Python.
- rclone is invoked via `rclone` on PATH. Inside a conda env this
  is automatic.
- Sleep prevention only works on Windows out of the box. On Linux,
  run the app from a `systemd-inhibit` wrapper or install `caffeine`.

Author / source
---------------
Built for Pawsey Acacia uploads. Free to adapt.
"""
        txt.insert("1.0", help_text)
        txt.configure(state="disabled")

    # ------------------------------------------------------------- Close
    def _on_close(self) -> None:
        if self.transfer_active:
            if not messagebox.askyesno(
                APP_NAME,
                "A transfer is still running.\nStop it and exit?\n\n"
                "(Already-uploaded files stay on Pawsey; you can resume\n"
                "this transfer next time you open the app.)"):
                return
            self.user_stopped = True
            # Cancel pending restart, if any
            if self.restart_after_id:
                try:
                    self.root.after_cancel(self.restart_after_id)
                except Exception:
                    pass
            stop_process(self.proc)
            # Mark resume entry as stopped so it shows up in the picker
            if self.current_resume_id:
                ResumeStore.update(self.current_resume_id, status="stopped")
        # Warn about an in-app (foreground) project->project copy in flight.
        if getattr(self, "_send_fg_active", 0) > 0:
            if not messagebox.askyesno(
                APP_NAME,
                "A copy to another project is still running in-app.\n"
                "Close anyway?\n\n"
                "(It will stop, but already-copied files stay on the destination "
                "and you can resume it from Storage → “Background copies…”. Tip: "
                "tick “Keep running after I close the app” next time to let it "
                "continue in the background.)"):
                return
        # Stop any console command still running.
        if getattr(self, "console_proc", None) is not None:
            try:
                stop_process(self.console_proc)
            except Exception:
                pass
        # Always release sleep lock and finalise heartbeat on exit
        self._cancel_autosync()
        prevent_sleep(False)
        try:
            self._write_heartbeat()
        except Exception:
            pass
        self.root.destroy()


def _quote(s: str) -> str:
    """Shell-style quoting for display only."""
    if not s or any(c in s for c in ' \t"\'\\'):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def main() -> int:
    root = tk.Tk()
    PawseyApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
