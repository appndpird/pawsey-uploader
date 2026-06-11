# Pawsey Uploader

A cross-platform (Windows / Ubuntu) GUI for managing rclone transfers to Pawsey
Acacia object storage. Single-file Python 3 + Tkinter app — no third-party
Python packages required to **run**. PyInstaller is only needed to **build** a
standalone `.exe`.

It does one-way copies, exact mirrors, and OneDrive-style **two-way sync**, with
a soft-delete recycle bin, conflict resolution, scheduled auto-sync, and
crash-resilient unattended operation for transfers that run for days.

## Two ways to use it

### A) Run as a Python script (simplest)
```
conda activate pawsey
python pawsey_uploader.py
```

### B) Build a standalone Windows .exe (no Python needed on the target machine)
On any Windows machine that has Python (your conda `pawsey` env is perfect):

1. Open **Anaconda Prompt** or a regular `cmd` with Python on PATH.
2. `cd` into the folder containing `pawsey_uploader.py` and `build_exe.bat`.
3. Double-click `build_exe.bat`, or run `build_exe.bat` from the prompt.
4. After 1–2 minutes you'll have `dist\PawseyUploader.exe`.

That single `.exe` can be copied to any Windows machine and run without
installing Python.

> **Important:** the `.exe` still calls the `rclone` command, so `rclone` must
> be on the PATH of the machine where the `.exe` runs. The easiest way is to
> run it from inside an activated conda env that has rclone, or install rclone
> system-wide from <https://rclone.org/downloads/>.

### Linux equivalent
```
chmod +x build_linux.sh
./build_linux.sh
```
Produces `dist/PawseyUploader` (ELF binary).

## Requirements

* Python 3.9+ (3.11 in your conda env is fine)
* `rclone` on PATH (already in your `pawsey` conda env)
* Tkinter — bundled with Python on Windows; on Ubuntu: `sudo apt install python3-tk`
* PyInstaller (only for building the .exe; the build scripts install it
  automatically)

## Transfer modes

Pick a mode on the **Transfer** tab:

| Mode | What it does |
|---|---|
| **Copy (one-way, additive)** | Uploads new/changed files to Pawsey. Never deletes; cannot detect renames (a renamed file is re-uploaded). |
| **Mirror (one-way, exact)** | Makes Pawsey an exact replica of the source, **deleting** anything on Pawsey that's no longer local. Extra confirmation required. |
| **Two-way sync (OneDrive-style)** | Reconciles both sides (rclone `bisync`). Changes and deletions propagate in both directions; renames move server-side instead of re-uploading. |
| **Resume previous…** | Re-runs a saved transfer after an interruption or restart. Multi-slot — each source/bucket pair is remembered separately. |

### Sync options

* **Conflict resolution** (two-way sync) — choose how clashes are settled:
  *Keep newer version*, *Keep both copies*, *Local always wins*, or
  *Pawsey always wins*.
* **Detect renamed/moved files** — moves files server-side on Pawsey instead of
  deleting and re-uploading.
* **Recycle bin: soft-delete & keep replaced versions** — instead of purging,
  files a sync would delete or overwrite are moved into a timestamped
  `_recycle_bin/` prefix **inside the same bucket**. This doubles as version
  history. Manage or empty it from the **Storage** tab.
* **Verify contents with checksums** — slower, but catches partial/corrupt
  uploads.
* **Safety abort** — refuse a two-way sync that would delete more than a set
  number of files (guards against an accidental mass deletion).
* **Auto-sync (Two-way) every N minutes** — re-runs the sync on a timer, like
  OneDrive, after the first successful run.

## Unattended / long-running transfers

The app is designed to be left running for days:

* **Auto-restart on transient crashes** — if rclone dies on a network blip, the
  app retries with backoff (capped attempts) and keeps the transfer alive.
* **Smart failure detection** — authentication failures (suspended keys, bad
  credentials, `403`) and non-retryable errors (missing bucket, bisync needing
  `--resync`) stop the transfer instead of retrying pointlessly.
* **Heartbeat file** — `~/.pawsey_uploader/heartbeat.json` is refreshed
  periodically so external monitoring scripts can confirm the app is alive.
* **Per-transfer logs** — full verbose rclone output is written to disk under
  `~/.pawsey_uploader/logs/` even though the on-screen view is trimmed.

## Other features

| Feature | Where to find it |
|---|---|
| Configure / edit Pawsey remote (access key, secret, endpoint) | Settings tab |
| Editable default project / bucket / source folder | Settings tab → Defaults |
| Performance tuning (transfers, checkers, S3 chunk size, upload concurrency) | Settings tab |
| Browse to source folder, default = app's own folder | Transfer tab |
| Mandatory notes field written to a permanent log | Transfer tab — empty notes = no transfer |
| Create / delete buckets; deletion needs typed confirmation | Buckets tab |
| Storage usage bar chart + recycle-bin management | Storage tab |
| Transfer audit log (browse `transfer_log.jsonl`, open log folder) | History tab |
| In-app help | Help tab |

## Data files

Everything user-specific lives in `~/.pawsey_uploader/` (i.e.
`%USERPROFILE%\.pawsey_uploader\` on Windows):

* `config.json` — saved defaults and credentials
* `transfer_log.jsonl` — permanent audit log (one JSON object per line)
* `resume.json` — saved parameters per source/bucket pair, used by Resume
* `bisync_pairs.json` — which two-way-sync pairs have been initialised
* `heartbeat.json` — liveness timestamp for external monitoring
* `logs/` — full per-transfer verbose rclone output
* `last_transfer.json` — legacy single-slot resume (migrated automatically)

The recycle bin / version history is **not** local — it lives inside each Pawsey
bucket under the `_recycle_bin/` prefix.

On Linux, restrict permissions: `chmod 600 ~/.pawsey_uploader/config.json`

## Troubleshooting

* **`rclone executable not found`** — activate the conda env or install rclone.
* **`403 UserSuspended`** — your Pawsey access keys are disabled. Contact
  `help@pawsey.org.au`; no app setting can fix this.
* **`403 Forbidden`** — credentials lack access to that bucket. Check which
  remote owns it.
* **Transfer interrupted** — relaunch the app, choose *Resume previous…*,
  pick the pair, click Start. rclone skips files already on Pawsey.
* **`Bisync aborted` / `Must run --resync to recover`** — a two-way sync's
  baseline was lost or the safety abort triggered. Re-run the pair to rebuild
  the baseline; the app handles the `--resync` so remote-only changes aren't
  discarded.
* **`.exe` is flagged by antivirus** — PyInstaller bundles can trigger
  occasional false positives. Whitelist it, or build it yourself locally so
  your AV sees that you produced it.

## Files in this folder

```
pawsey_uploader.py     # the app
build_exe.bat          # one-click Windows build script
build_linux.sh         # one-click Linux build script
PawseyUploader.spec    # PyInstaller build spec
dist/PawseyUploader.exe # prebuilt Windows executable
README.md              # this file
```
