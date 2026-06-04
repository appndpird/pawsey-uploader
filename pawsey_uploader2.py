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
APP_VERSION = "1.0"

APP_DIR = Path.home() / ".pawsey_uploader"
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "transfer_log.jsonl"
LAST_TRANSFER_FILE = APP_DIR / "last_transfer.json"

IS_WINDOWS = platform.system() == "Windows"

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
        t.rowconfigure(6, weight=1)

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

        # --- Notes (mandatory) ---
        ttk.Label(t, text="Data description / notes  (required)", style="Big.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w", padx=10, pady=(14, 2))
        self.notes_text = scrolledtext.ScrolledText(t, height=4, wrap="word")
        self.notes_text.grid(row=5, column=0, columnspan=2, sticky="ew", padx=10)

        # --- Mode + buttons ---
        mode_frame = ttk.Frame(t)
        mode_frame.grid(row=6, column=0, columnspan=2, sticky="ew", padx=10, pady=10)

        ttk.Label(mode_frame, text="Mode:", style="Big.TLabel").pack(side="left")
        self.mode_var = tk.StringVar(value="copy")
        ttk.Radiobutton(mode_frame, text="Copy (resumable)",
                        variable=self.mode_var, value="copy").pack(side="left", padx=(8, 2))
        ttk.Radiobutton(mode_frame, text="Mirror sync (delete extras)",
                        variable=self.mode_var, value="sync").pack(side="left", padx=2)
        ttk.Radiobutton(mode_frame, text="Resume last transfer",
                        variable=self.mode_var, value="resume").pack(side="left", padx=2)

        ttk.Button(mode_frame, text="Start transfer", command=self._start_transfer).pack(
            side="right", padx=(8, 0))
        self.stop_btn = ttk.Button(mode_frame, text="Stop", command=self._stop_transfer,
                                   state="disabled")
        self.stop_btn.pack(side="right")

        # --- Progress + output ---
        prog_frame = ttk.Frame(t)
        prog_frame.grid(row=7, column=0, columnspan=2, sticky="nsew", padx=10, pady=(0, 10))
        prog_frame.columnconfigure(0, weight=1)
        prog_frame.rowconfigure(2, weight=1)
        t.rowconfigure(7, weight=1)

        self.progress = ttk.Progressbar(prog_frame, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")

        self.progress_label = ttk.Label(prog_frame, text="Idle")
        self.progress_label.grid(row=1, column=0, sticky="w", pady=(4, 4))

        self.output_text = scrolledtext.ScrolledText(prog_frame, height=12, wrap="word",
                                                    font=("Consolas" if IS_WINDOWS else "Monospace", 9))
        self.output_text.grid(row=2, column=0, sticky="nsew")
        self.output_text.configure(state="disabled")

        self._refresh_remotes()

    def _browse_source(self) -> None:
        d = filedialog.askdirectory(initialdir=self.src_var.get() or str(Path.home()),
                                    title="Choose source folder")
        if d:
            self.src_var.set(d)

    def _refresh_remotes(self) -> None:
        remotes = RcloneRemote.list_remotes()
        self.remote_combo["values"] = remotes
        if self.remote_var.get() not in remotes and remotes:
            self.remote_var.set(remotes[0])
        self.status_var.set(f"Remotes: {', '.join(remotes) or 'none configured'}")

    # ------------------------------------------------------ Transfer logic
    def _build_rclone_cmd(self, mode: str, src: str, dest: str) -> list[str]:
        verb = "sync" if mode == "sync" else "copy"
        cmd = [
            RCLONE_EXE, verb, src, dest,
            "--s3-no-check-bucket",
            "--s3-disable-checksum",
            f"--s3-chunk-size={self.cfg.get('s3_chunk_size', '64M')}",
            f"--s3-upload-concurrency={self.cfg.get('s3_upload_concurrency', 4)}",
            "--multi-thread-streams=0",
            f"--transfers={self.cfg.get('transfers', 1)}",
            f"--checkers={self.cfg.get('checkers', 16)}",
            f"--retries={self.cfg.get('retries', 5)}",
            f"--low-level-retries={self.cfg.get('low_level_retries', 10)}",
            "--progress",
            "--stats=5s",
            "-v",
        ]
        return cmd

    def _start_transfer(self) -> None:
        if self.transfer_active:
            messagebox.showinfo(APP_NAME, "A transfer is already running.")
            return

        mode = self.mode_var.get()

        if mode == "resume":
            if not LAST_TRANSFER_FILE.exists():
                messagebox.showwarning(APP_NAME, "No previous transfer to resume.")
                return
            try:
                with open(LAST_TRANSFER_FILE) as f:
                    last = json.load(f)
            except Exception as e:
                messagebox.showerror(APP_NAME, f"Could not read last transfer:\n{e}")
                return
            src = last["src"]
            dest = last["dest"]
            notes = last.get("notes", "") + f"\n[Resumed at {datetime.now().isoformat(timespec='seconds')}]"
            actual_mode = last.get("mode", "copy")
        else:
            src = self.src_var.get().strip()
            bucket = self.bucket_var.get().strip()
            path = self.path_var.get().strip().lstrip("/")
            remote = self.remote_var.get().strip()
            notes = self.notes_text.get("1.0", "end").strip()
            actual_mode = mode

            # Validation
            if not src or not Path(src).exists():
                messagebox.showerror(APP_NAME, f"Source folder is missing or invalid:\n{src}")
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

            dest = f"{remote}:{bucket}"
            if path:
                dest = f"{dest}/{path}"

            if actual_mode == "sync":
                if not messagebox.askyesno(
                    "Mirror sync warning",
                    f"Mirror sync will DELETE any files at\n  {dest}\n"
                    f"that are not present in the source folder.\n\n"
                    f"Proceed?",
                    icon="warning",
                ):
                    return

        # Save last-transfer info for future Resume
        try:
            with open(LAST_TRANSFER_FILE, "w") as f:
                json.dump({"src": src, "dest": dest, "mode": actual_mode,
                           "notes": notes,
                           "started_at": datetime.now().isoformat(timespec="seconds")},
                          f, indent=2)
        except Exception:
            pass

        # Write notes to log file
        self._append_log({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "mode": actual_mode,
            "source": src,
            "destination": dest,
            "notes": notes,
            "status": "started",
        })

        cmd = self._build_rclone_cmd(actual_mode, src, dest)
        self._append_output(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
        self._append_output(f"$ {' '.join(_quote(a) for a in cmd)}\n\n")

        try:
            self.proc = subprocess.Popen(cmd, **_subprocess_kwargs())
        except FileNotFoundError:
            messagebox.showerror(APP_NAME, "rclone executable not found on PATH.")
            return
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Failed to start rclone:\n{e}")
            return

        self.transfer_active = True
        self.stop_btn.configure(state="normal")
        self.status_var.set("Transfer running...")
        self.progress["value"] = 0
        self.progress_label.configure(text="Starting...")

        # Reader thread
        self.proc_thread = threading.Thread(
            target=self._reader_thread, args=(self.proc,), daemon=True)
        self.proc_thread.start()

    def _stop_transfer(self) -> None:
        if not self.transfer_active:
            return
        if not messagebox.askyesno(APP_NAME,
                                   "Stop the current transfer?\n"
                                   "Already-uploaded files remain on Pawsey; "
                                   "partial files will be re-sent if you Resume."):
            return
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
                    self._append_output(line + "\n")
        except queue.Empty:
            pass
        self.root.after(120, self._drain_output)

    def _handle_progress_line(self, line: str) -> None:
        m = PROGRESS_RE.search(line)
        if m:
            done, total, pct = m.group(1), m.group(2), int(m.group(3))
            self.progress["value"] = pct
            self.progress_label.configure(text=f"{done} / {total}  ({pct}%)")

    def _append_output(self, text: str) -> None:
        self.output_text.configure(state="normal")
        self.output_text.insert("end", text)
        self.output_text.see("end")
        self.output_text.configure(state="disabled")

    def _on_transfer_finished(self, rc: int) -> None:
        self.transfer_active = False
        self.stop_btn.configure(state="disabled")
        status = "completed" if rc == 0 else f"failed (exit {rc})"
        self.progress_label.configure(text=f"Transfer {status}.")
        self.status_var.set(f"Transfer {status}.")
        self._append_output(f"\n=== Transfer {status} ===\n")
        self._append_log({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "status": "finished",
            "exit_code": rc,
        })
        if rc == 0:
            messagebox.showinfo(APP_NAME, "Transfer completed successfully.")
        else:
            messagebox.showwarning(APP_NAME,
                                   f"Transfer ended with exit code {rc}.\n"
                                   f"See log output for details.\n"
                                   f"You can use 'Resume last transfer' to retry.")
        self._refresh_history()
        # If the Storage tab was previously loaded, refresh it so the new
        # content is visible. Tree state survives because refresh is lazy.
        if rc == 0 and hasattr(self, "storage_tree") \
                and self.storage_tree.get_children():
            self._storage_refresh_root()

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
            text="Click ▶ to expand a bucket or folder. Select an item, then "
                 "use the buttons below to upload, create, download or delete. "
                 "Notes are required for any change.",
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
            selectmode="browse",
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

        # ----- Selection label -----
        self.storage_selected_label = ttk.Label(
            t, text="No selection.", foreground="#444")
        self.storage_selected_label.grid(row=4, column=0, sticky="w", padx=10)

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
        ttk.Button(btns, text="Download…",
                   command=self._storage_download_selected).pack(side="left", padx=12)
        ttk.Button(btns, text="Delete selected",
                   command=self._storage_delete_selected,
                   style="Danger.TButton").pack(side="left", padx=12)

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

        self._bg_call(work, done)

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
        sel = self._storage_selection()
        if not sel:
            messagebox.showerror(APP_NAME, "Select a file or folder first.")
            return
        remote, iid, type_ = sel
        full = f"{remote}:{iid}"

        if type_ == "bucket":
            messagebox.showinfo(
                APP_NAME,
                "Use the Buckets tab to delete an entire bucket "
                "(it requires typing the bucket name to confirm).")
            return

        warn = (
            f"⚠  PERMANENT DELETION  ⚠\n\n"
            f"This will delete the {type_}:\n"
            f"  {full}\n\n"
            f"There is no recycle bin on Pawsey. This cannot be undone.\n\n"
            f"Continue?"
        )
        if not messagebox.askyesno("Confirm deletion", warn, icon="warning"):
            return

        self._append_log({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "operation": f"delete_{type_}",
            "target": full,
            "notes": notes,
            "status": "started",
        })
        self.status_var.set(f"Deleting {iid}…")

        def work():
            if type_ == "file":
                return run_rclone_capture(["deletefile", full], timeout=120)
            return run_rclone_capture(["purge", full], timeout=86400)

        def done(result, err):
            if err:
                messagebox.showerror(APP_NAME, f"Error:\n{err}")
                return
            rc, out = result
            if rc == 0:
                self._append_log({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "operation": f"delete_{type_}",
                    "target": full,
                    "status": "completed",
                })
                # Remove the row, then refresh ancestor sizes
                parent_iid = "/".join(iid.split("/")[:-1])
                if self.storage_tree.exists(iid):
                    self.storage_tree.delete(iid)
                self._tree_sizes.pop(iid, None)
                if parent_iid and self.storage_tree.exists(parent_iid):
                    self._compute_size_async(remote, parent_iid, parent_iid)
                else:
                    # bucket-level child removed: re-size the bucket
                    bucket = iid.split("/")[0]
                    if self.storage_tree.exists(bucket):
                        self._compute_size_async(remote, bucket, bucket)
                self.storage_notes.delete("1.0", "end")
                self.status_var.set(f"Deleted {iid}.")
                messagebox.showinfo(APP_NAME, f"Deleted: {iid}")
                self._refresh_history()
            else:
                messagebox.showerror(APP_NAME, f"Delete failed:\n{out}")
                self.status_var.set("Delete failed.")

        self._bg_call(work, done)

    def _storage_download_selected(self) -> None:
        sel = self._storage_selection()
        if not sel:
            messagebox.showerror(APP_NAME, "Select a file or folder first.")
            return
        remote, iid, type_ = sel
        full = f"{remote}:{iid}"
        name = iid.split("/")[-1] or iid

        if type_ == "file":
            local = filedialog.asksaveasfilename(initialfile=name,
                                                 title="Save file as")
            if not local:
                return
            cmd = ["copyto", full, local, "--s3-disable-checksum"]
        else:
            base = filedialog.askdirectory(title="Choose download destination")
            if not base:
                return
            local = str(Path(base) / name)
            cmd = ["copy", full, local, "--s3-disable-checksum", "--transfers=4"]

        self.status_var.set(f"Downloading {iid}…")

        def work():
            return run_rclone_capture(cmd, timeout=86400 * 2)

        def done(result, err):
            if err:
                messagebox.showerror(APP_NAME, f"Download error:\n{err}")
                return
            rc, out = result
            if rc == 0:
                self.status_var.set(f"Downloaded {iid}.")
                messagebox.showinfo(APP_NAME, f"Downloaded to:\n{local}")
            else:
                messagebox.showerror(APP_NAME, f"Download failed:\n{out}")
                self.status_var.set("Download failed.")

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

        adv_rows = [
            ("S3 chunk size", self.s_chunk),
            ("Upload concurrency", self.s_conc),
            ("Transfers", self.s_transfers),
            ("Checkers", self.s_checkers),
            ("Retries", self.s_retries),
            ("Low-level retries", self.s_llr),
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
   - WRITE A NOTE about the data - this is mandatory and goes into
     the transfer log file.
   - Choose a mode:
       * Copy            - new/changed files only (resumable)
       * Mirror sync     - exact replica; deletes extras on Pawsey
       * Resume last     - re-run the previous transfer
   - Click 'Start transfer'.

3. If the computer shuts down mid-transfer, just open the app again,
   go to the TRANSFER tab, choose 'Resume last transfer', and click
   'Start transfer'. rclone will skip files that already finished
   uploading.

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
  - Selecting an item displays its full path under the tree.
  - The 'Search' box searches for files by name. If a bucket or item is
    selected, the search is limited to that bucket; otherwise it scans
    every bucket on the remote. Search results have a 'Reveal in tree'
    button that expands the main tree to the matching file.
  - Action buttons (Upload file, Upload folder, New folder, Download,
    Delete) operate on the selected row. For uploads and 'New folder',
    if you have a file selected the action targets the file's parent
    folder.
  - Any upload / delete / new-folder action REQUIRES you to write a
    note in the 'Action note' box first. The note is saved to the
    transfer log permanently.
  - Delete actions show a permanent-deletion warning. There is no
    recycle bin on Pawsey.
  - The tree refreshes automatically after a transfer completes on
    the Transfer tab.

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
  - last_transfer.json   : info about the most recent transfer
                           (used by 'Resume last transfer')

Tips & troubleshooting
----------------------
- "rclone not found": activate the conda env that has rclone before
  launching the app, or install rclone system-wide.
- 403 UserSuspended: your Pawsey access key has been disabled. Contact
  help@pawsey.org.au; no app setting can fix this.
- 403 Forbidden on a HeadObject: the credentials don't have access to
  that bucket. Check that the remote you're using owns the bucket.
- Stop button: terminates rclone cleanly. Already-uploaded files stay
  on Pawsey; use 'Resume last transfer' to finish what's left.
- Large files: if a single huge file is interrupted, rclone restarts
  THAT file from byte 0 (it cannot byte-resume one file). But all
  other already-completed files are skipped.

Cross-platform notes
--------------------
- Works on Windows 10/11 and Ubuntu 20.04+. macOS is not specifically
  tested but should also work with a modern Python.
- rclone is invoked via `rclone` on PATH. Inside a conda env this
  is automatic.

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
                "A transfer is still running.\nStop it and exit?"):
                return
            stop_process(self.proc)
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
