"""
Pawsey Uploader
A cross-platform (Windows / Ubuntu) GUI for managing rclone transfers to
Pawsey Acacia object storage.

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

import json
import os
import platform
import queue
import re
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

# ---------------------------------------------------------------------------
# Constants & defaults
# ---------------------------------------------------------------------------

APP_NAME = "Pawsey Uploader"
APP_VERSION = "1.4"

APP_DIR = Path.home() / ".pawsey_uploader"
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "transfer_log.jsonl"
LAST_TRANSFER_FILE = APP_DIR / "last_transfer.json"   # legacy; migrated
RESUME_FILE = APP_DIR / "resume.json"                  # new: multi-slot resume
HEARTBEAT_FILE = APP_DIR / "heartbeat.json"            # liveness for external tools
TRANSFER_LOG_DIR = APP_DIR / "logs"                    # per-transfer verbose logs
BISYNC_STATE_FILE = APP_DIR / "bisync_pairs.json"      # which pairs are initialised

# Soft-delete recycle bin. When enabled, files that a sync would delete or
# overwrite, and items deleted from the Storage tab, are moved into this
# prefix (timestamped) inside the SAME bucket instead of being purged.
# Because rclone's --backup-dir also catches overwritten files, this prefix
# doubles as version history (previous versions land here, dated).
RECYCLE_PREFIX = "_recycle_bin"

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
    "too many deletes",
    "Safety abort",
)

# Default app config (merged with on-disk config on load)
DEFAULT_CONFIG = {
    "remote_name": "pawsey",
    "access_key_id": "",
    "secret_access_key": "",
    "endpoint": "https://projects.pawsey.org.au",
    "provider": "Ceph",
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
}

# Regex to parse rclone --progress lines
PROGRESS_RE = re.compile(
    r"Transferred:\s+([\d.]+\s*\w+)\s*/\s*([\d.]+\s*\w+),\s*(\d+)%"
)


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
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except FileNotFoundError:
        return 127, "rclone executable not found"
    except subprocess.TimeoutExpired:
        return 124, "Command timed out"
    except Exception as e:
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
    def upsert_pawsey_remote(cfg: ConfigManager) -> tuple[bool, str]:
        """Create or update the Pawsey remote based on app config."""
        name = cfg.get("remote_name", "pawsey")
        endpoint = cfg.get("endpoint")
        access = cfg.get("access_key_id")
        secret = cfg.get("secret_access_key")
        provider = cfg.get("provider", "Ceph")

        if not (access and secret and endpoint and name):
            return False, "Remote name, endpoint, access key, and secret are all required."

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
            "provider",          provider,
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
        return True, f"Remote '{name}' {verb} successfully."

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
        return [t for t in ResumeStore.list_all()
                if t.get("status") not in ("completed",)]

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
        self.proc: Optional[subprocess.Popen] = None
        self.proc_queue: queue.Queue[str] = queue.Queue()
        self.proc_thread: Optional[threading.Thread] = None
        self.transfer_active = False
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
        self.restart_count = 0
        self.restart_after_id: Optional[str] = None
        self._heartbeat_after_id: Optional[str] = None
        self._last_progress_text = ""
        self._transfer_log_fh = None        # per-transfer disk log file
        # Change tracking for the current transfer
        self._change_counts = {"new": 0, "updated": 0, "deleted": 0, "renamed": 0}
        self._change_details: list[str] = []
        self._rename_sources: set[str] = set()
        # Scheduled auto-sync
        self._autosync_after_id: Optional[str] = None
        self._autosync_params: Optional[dict] = None

        # Migrate any legacy last_transfer.json into the new resume store
        ResumeStore.migrate_legacy()
        TRANSFER_LOG_DIR.mkdir(parents=True, exist_ok=True)

        # Resolve rclone executable BEFORE any rclone calls happen.
        # This also writes the discovered path back into the config so
        # the next launch is instant.
        self._resolve_rclone_executable(prompt_if_missing=True)

        root.title(f"{APP_NAME} v{APP_VERSION}")
        root.geometry("1050x760")
        root.minsize(900, 640)

        # Use a clean ttk theme
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Danger.TButton", foreground="#a40000")
        style.configure("Big.TLabel", font=("TkDefaultFont", 11, "bold"))

        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Periodically drain subprocess output
        root.after(100, self._drain_output)
        # Periodically drain background-task results (Storage tab, etc.)
        root.after(120, self._drain_bg_queue)

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

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_transfer = ttk.Frame(nb)
        self.tab_buckets = ttk.Frame(nb)
        self.tab_storage = ttk.Frame(nb)
        self.tab_history = ttk.Frame(nb)
        self.tab_settings = ttk.Frame(nb)
        self.tab_help = ttk.Frame(nb)

        nb.add(self.tab_transfer, text="  Transfer  ")
        nb.add(self.tab_buckets, text="  Buckets  ")
        nb.add(self.tab_storage, text="  Storage  ")
        nb.add(self.tab_history, text="  History  ")
        nb.add(self.tab_settings, text="  Settings  ")
        nb.add(self.tab_help, text="  Help  ")

        # Status bar - must be created BEFORE the tabs because some tab
        # builders (e.g. Transfer -> _refresh_remotes) write to status_var.
        self.status_var = tk.StringVar(value="Ready")
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", side="bottom")
        ttk.Separator(bar, orient="horizontal").pack(fill="x")
        ttk.Label(bar, textvariable=self.status_var, anchor="w", padding=(8, 4)).pack(fill="x")

        self._build_transfer_tab()
        self._build_buckets_tab()
        self._build_storage_tab()
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

        ttk.Button(mode_frame, text="Start transfer", command=self._start_transfer).pack(
            side="right", padx=(8, 0))
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

    def _on_mode_change(self) -> None:
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
                     "button.")
        elif mode == "sync":
            self.mode_hint.configure(
                text="Mirror makes Pawsey exactly match the source (it deletes "
                     "extras on Pawsey). With 'Detect renamed/moved files' on, "
                     "renames become server-side moves (no re-upload).")
        elif mode == "bisync":
            self.mode_hint.configure(
                text="Two-way sync reconciles both sides. Renames propagate as "
                     "renames when 'Detect renamed/moved files' is on.")
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
        bwlimit = str(self.cfg.get("bwlimit", "")).strip()

        if mode == "bisync":
            # Two-way sync. PATH1 = local source, PATH2 = pawsey dest.
            cmd = [RCLONE_EXE, "bisync", src, dest]
            cmd += self._s3_flags(verify)
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
            cmd += ["--progress", "--stats=5s", "-v"]
            return cmd

        # One-way copy or mirror sync
        verb = "sync" if mode == "sync" else "copy"
        cmd = [RCLONE_EXE, verb, src, dest]
        cmd += self._s3_flags(verify)

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
    def _verify_check(self) -> None:
        """Run `rclone check` between the current source and destination and
        report whether the two sides hold identical content."""
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
            f"\n=== Verify (rclone check{' --checksum' if use_checksum else ''}) "
            f"{datetime.now():%H:%M:%S} ===\n{src}  <->  {dest}\n\n")

        def work():
            args = ["check", src, dest, "--one-way=false"]
            if use_checksum:
                args.append("--checksum")
            else:
                args.append("--size-only")
            return run_rclone_capture(args, timeout=86400)

        def done(result, err):
            if err:
                messagebox.showerror(APP_NAME, f"Verify error:\n{err}")
                return
            rc, out = result
            self._append_output(out + "\n")
            if rc == 0:
                self.status_var.set("Verify: both sides match.")
                messagebox.showinfo(APP_NAME,
                                    "Verification passed: the two sides hold "
                                    "identical content.")
            else:
                self.status_var.set("Verify: differences found.")
                messagebox.showwarning(
                    APP_NAME,
                    "Verification found differences between the two sides.\n"
                    "See the output log for the list. Run a sync to reconcile.")

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
        self.restart_count = 0
        # Reset change tracking for this logical transfer (not on auto-restart)
        self._change_counts = {"new": 0, "updated": 0, "deleted": 0, "renamed": 0}
        self._change_details = []
        self._rename_sources = set()
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
        """Classify a real per-file operation from rclone's INFO log lines.

        Only the actual operation lines are counted (e.g. 'name: Copied (new)',
        'name: Deleted', 'name: Renamed from "old"'). The periodic stats blocks
        ('Deleted:  1 (files)', 'Renamed:  1', 'Transferred: …') and bisync's
        planning lines ('- Path1 File is new', 'Queue copy …', 'N changes:')
        are skipped - otherwise the same change gets counted many times.
        A rename is logged by rclone as a server-side copy + a delete + a
        'Renamed from' line; we count it once (as renamed) and skip the
        delete of the rename's source so it isn't double-counted."""
        if "INFO" not in line:
            return  # stats blocks / progress have no INFO prefix
        # Skip bisync planning + summary lines (they duplicate the operations)
        if ("- Path1" in line or "- Path2" in line or "Queue " in line
                or "File is new" in line or "File was deleted" in line
                or "changes:" in line or "Making map" in line
                or "checking for diffs" in line):
            return
        after = line.split("INFO", 1)[1].lstrip(": ").strip()
        low = after.lower()

        def record(kind: str, text: str):
            self._change_counts[kind] += 1
            if len(self._change_details) < 2000:
                tag = {"new": "ADD", "updated": "UPD",
                       "deleted": "DEL", "renamed": "REN"}[kind]
                self._change_details.append(f"[{tag}] {text}")

        # Server-side copy is the 'copy' half of a track-renames rename.
        # Record the source name so its later 'Deleted' isn't counted, and
        # don't count the copy itself (the 'Renamed from' line counts it).
        if "copied (server-side copy) to:" in low:
            src = after.split(":", 1)[0].strip()
            if src:
                self._rename_sources.add(src)
            return
        if "renamed from" in low or "moved to:" in low or "moved (server-side)" in low:
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

    def _handle_progress_line(self, line: str) -> None:
        m = PROGRESS_RE.search(line)
        if m:
            done, total, pct = m.group(1), m.group(2), int(m.group(3))
            self.progress["value"] = pct
            text = f"{done} / {total}  ({pct}%)"
            self.progress_label.configure(text=text)
            self._last_progress_text = text

    def _sniff_auth_failure(self, line: str) -> None:
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
        if ("too many deletes" in line.lower()
                or "safety abort" in line.lower()):
            self.delete_cap_seen = True
        if "empty current path" in line.lower() and "listing" in line.lower():
            self.empty_listing_seen = True

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

            # Build + record the change summary
            c = self._change_counts
            summary = (f"Added {c['new']}, updated {c['updated']}, "
                       f"renamed/moved {c['renamed']}, deleted {c['deleted']}")
            self._append_output(f"\n=== Changes this run: {summary} ===\n")
            self._append_log({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "operation": "change_summary",
                "resume_id": self.current_resume_id,
                "destination": self.current_dest,
                "added": c["new"], "updated": c["updated"],
                "renamed": c["renamed"], "deleted": c["deleted"],
            })

            total_changes = sum(c.values())
            self._last_change_summary = summary
            self._last_change_details = list(self._change_details)

            # Schedule next auto-sync if enabled and this was a two-way sync
            scheduled = self._maybe_schedule_autosync()

            extra = ("\n\nNo changes - both sides were already in sync."
                     if total_changes == 0 else f"\n\nChanges: {summary}")
            if scheduled:
                extra += (f"\n\nAuto-sync is ON - next run in "
                          f"{self.cfg.get('autosync_interval_min', 15)} min.")
            messagebox.showinfo(APP_NAME, "Transfer completed successfully." + extra)
            # Refresh the storage tree if loaded
            if hasattr(self, "storage_tree") and self.storage_tree.get_children():
                self._storage_refresh_root()

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
        self.restart_count = 0
        self._change_counts = {"new": 0, "updated": 0, "deleted": 0, "renamed": 0}
        self._change_details = []
        self._rename_sources = set()
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
        rc, out = run_rclone_capture(["purge", f"{remote}:{name}"], timeout=600)
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
        except queue.Empty:
            pass
        self.root.after(80, self._drain_bg_queue)

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
                 "below to upload, download or delete. Notes are required for "
                 "any change.",
            foreground="#555", wraplength=1000, justify="left",
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

        btns = ttk.Frame(bottom)
        btns.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))
        ttk.Button(btns, text="Upload file…",
                   command=self._storage_upload_file).pack(side="left", padx=2)
        ttk.Button(btns, text="Upload folder…",
                   command=self._storage_upload_folder).pack(side="left", padx=2)
        ttk.Button(btns, text="New folder…",
                   command=self._storage_new_folder).pack(side="left", padx=2)
        ttk.Button(btns, text="Rename / Move…",
                   command=self._storage_rename_move).pack(side="left", padx=2)
        ttk.Button(btns, text="Download…",
                   command=self._storage_download_selected).pack(side="left", padx=12)
        ttk.Button(btns, text="Delete selected",
                   command=self._storage_delete_selected,
                   style="Danger.TButton").pack(side="left", padx=12)
        ttk.Button(btns, text="Recycle bin…",
                   command=self._storage_recycle_manager).pack(side="left", padx=2)

        # ----- State -----
        self._tree_loaded: set[str] = set()      # iids whose children are populated
        self._tree_sizes: dict[str, int] = {}    # iid (incl. nested) -> bytes

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
            rc, out = run_rclone_capture(
                ["lsjson", f"{remote}:{path}"], timeout=120)
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
            return run_rclone_capture(
                ["copy", src, full_dest,
                 "--s3-no-check-bucket", "--s3-disable-checksum",
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
        marker = f"{remote}:{new_full_path}/.keep"

        self._append_log({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "operation": "create_folder",
            "destination": f"{remote}:{new_full_path}",
            "notes": notes,
            "status": "started",
        })

        def work():
            # On S3 there are no real folders, so create a placeholder
            # `.keep` object to make the folder visible in listings.
            return run_rclone_capture(
                ["touch", marker, "--s3-no-check-bucket"], timeout=60)

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
        self._process_next_delete()

    def _process_next_delete(self) -> None:
        if not self._delete_queue:
            # All done
            msg = (f"Deleted {self._delete_done} item(s)."
                   if not self._delete_failed else
                   f"Deleted {self._delete_done} item(s); "
                   f"{self._delete_failed} failed (see log).")
            self.status_var.set(msg)
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
                return run_rclone_capture(
                    ["move", full, trash_dest, "--s3-no-check-bucket",
                     "--delete-empty-src-dirs"], timeout=86400)
            if type_ == "file":
                return run_rclone_capture(["deletefile", full], timeout=120)
            return run_rclone_capture(["purge", full], timeout=86400)

        def done(result, err):
            ok = (not err) and result and result[0] == 0
            if ok:
                self._delete_done += 1
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
                self._append_output(f"\n[delete failed] {full}: {out}\n")
            self.status_var.set(
                f"Deleting… {self._delete_done + self._delete_failed}"
                f"/{self._delete_total}")
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
                return run_rclone_capture(["purge", trash], timeout=86400)

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

        # Rename the local side first (fast, local). If it fails we stop and
        # do NOT touch Pawsey, so the two sides never silently diverge.
        local_done = False
        if do_local:
            import shutil
            try:
                Path(local_new).parent.mkdir(parents=True, exist_ok=True)
                if Path(local_new).exists():
                    raise FileExistsError(
                        f"Target already exists locally:\n{local_new}")
                shutil.move(local_old, local_new)
                local_done = True
            except Exception as e:
                messagebox.showerror(
                    APP_NAME,
                    f"Local rename failed - Pawsey was NOT changed so the two "
                    f"sides stay consistent:\n\n{e}")
                self.status_var.set("Rename aborted (local step failed).")
                return

        def work():
            # moveto for a single file, move for a directory prefix.
            if type_ == "file":
                args = ["moveto", src_full, dest_full]
            else:
                args = ["move", src_full, dest_full,
                        "--delete-empty-src-dirs"]
            args += ["--s3-no-check-bucket"]
            return run_rclone_capture(args, timeout=86400)

        def done(result, err):
            if err:
                messagebox.showerror(APP_NAME, f"Rename/move error:\n{err}")
                return
            rc, out = result
            if rc == 0:
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
                messagebox.showerror(APP_NAME, f"Rename/move failed on Pawsey:\n{out}{extra}")
                self.status_var.set("Rename/move failed on Pawsey.")

        self._bg_call(work, done)

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
            cmd = ["copy", full, local, "--s3-disable-checksum", "--transfers=4"]

        def work():
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
                ["lsjson", target, "--recursive", "--files-only"],
                timeout=600)
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
                    ["lsjson", f"{remote}:{current}"], timeout=120)
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

        # Remote section
        rs = ttk.LabelFrame(t, text="Pawsey remote (rclone)")
        rs.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=6)
        for i in range(2):
            rs.columnconfigure(i, weight=1)

        self.s_name = tk.StringVar(value=self.cfg.get("remote_name"))
        self.s_endpoint = tk.StringVar(value=self.cfg.get("endpoint"))
        self.s_access = tk.StringVar(value=self.cfg.get("access_key_id"))
        self.s_secret = tk.StringVar(value=self.cfg.get("secret_access_key"))
        self.s_provider = tk.StringVar(value=self.cfg.get("provider"))

        rows = [
            ("Remote name", self.s_name, False),
            ("Endpoint URL", self.s_endpoint, False),
            ("Provider", self.s_provider, False),
            ("Access key ID", self.s_access, False),
            ("Secret access key", self.s_secret, True),
        ]
        for r, (label, var, secret) in enumerate(rows):
            ttk.Label(rs, text=label).grid(row=r, column=0, sticky="w", padx=8, pady=4)
            entry = ttk.Entry(rs, textvariable=var, show="•" if secret else "")
            entry.grid(row=r, column=1, sticky="ew", padx=8, pady=4)

        btns = ttk.Frame(rs)
        btns.grid(row=len(rows), column=0, columnspan=2, sticky="e", padx=8, pady=8)
        ttk.Button(btns, text="Save & apply to rclone",
                   command=self._save_remote).pack(side="left", padx=4)
        ttk.Button(btns, text="Test connection",
                   command=self._test_remote).pack(side="left", padx=4)

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

        ttk.Button(t, text="Save defaults",
                   command=self._save_defaults).grid(row=4, column=1, sticky="e",
                                                    padx=10, pady=(0, 10))

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

    def _save_remote(self) -> None:
        # Copy widget state into config and persist
        self.cfg.set("remote_name", self.s_name.get().strip())
        self.cfg.set("endpoint", self.s_endpoint.get().strip())
        self.cfg.set("provider", self.s_provider.get().strip())
        self.cfg.set("access_key_id", self.s_access.get().strip())
        self.cfg.set("secret_access_key", self.s_secret.get().strip())
        self.cfg.save()

        ok, msg = RcloneRemote.upsert_pawsey_remote(self.cfg)
        if ok:
            messagebox.showinfo(APP_NAME, msg)
        else:
            messagebox.showerror(APP_NAME, msg)
        self._refresh_remotes()

    def _test_remote(self) -> None:
        name = self.s_name.get().strip()
        if not name:
            messagebox.showerror(APP_NAME, "Set a remote name first.")
            return
        ok, out = RcloneRemote.test_remote(name)
        if ok:
            messagebox.showinfo(APP_NAME,
                                f"Connection OK.\n\nBuckets:\n{out or '(none)'}")
        else:
            messagebox.showerror(APP_NAME, f"Connection failed:\n{out}")

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
  - "Verify both sides (check)" button: runs `rclone check` between the
    current source and destination and reports whether they hold
    identical content - without transferring anything.

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
