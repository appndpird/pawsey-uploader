# Pawsey Uploader

A cross-platform (Windows / Ubuntu) GUI for managing rclone transfers to Pawsey
Acacia object storage. Single-file Python 3 + Tkinter app — no third-party
Python packages required to **run**. PyInstaller is only needed to **build** a
standalone `.exe`.

It does one-way copies, exact mirrors, and OneDrive-style **two-way sync**, with
a soft-delete recycle bin, conflict resolution, scheduled auto-sync, and
crash-resilient unattended operation for transfers that run for days.

It also gives you Windows-style **copy / cut / paste** of files and folders on
Pawsey, expiring **shareable links**, a built-in **command console**, and a
**dry-run preview** mode — all behind a light, professional UI branded for
**DPIRD** (primary, left) and **APPN** (top-right).

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
* **Preview only (dry-run)** — tick this to have rclone report exactly what it
  *would* copy, change or delete **without transferring or deleting anything**.
  Ideal for sanity-checking a Mirror or two-way sync before committing. (For
  the record: with this box unticked, every transfer is a real, live run — the
  app never silently does a dry-run.)

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
| **Multiple Pawsey projects** — save several projects (each its own keys), mark one active, switch any time | Settings tab → Pawsey projects |
| **Copy / Cut / Paste** files & folders on Pawsey (Windows-style; server-side move/copy, no re-upload) | Storage tab |
| **Send to another project** — copy selected buckets/folders/files into a *different* Pawsey project's bucket (copy-only, nothing deleted) | Storage tab → "📤 Send to another project…" |
| **Generate shareable link** with a user-chosen expiry (file → one link; folder/bucket → an HTML page of links) | Storage tab → "🔗 Generate link…" |
| **Command console** — run any rclone (or other) command and watch live output | Console tab |
| **Preview (dry-run)** — show changes without transferring | Transfer tab → Sync options |
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
pawsey_uploader.py          # the app
logo_data.py                # embedded DPIRD + APPN logos (base64 PNG); bundled automatically
build_exe.bat               # one-click Windows build script
build_linux.sh              # one-click Linux build script
PawseyUploader.spec         # PyInstaller build spec
dist/PawseyUploader.exe     # prebuilt Windows executable — always the LATEST version
dist/PawseyUploader-v1.8.exe # retained previous version
dist/PawseyUploader-v1.7.exe # retained previous version
dist/PawseyUploader-v1.6.exe # retained previous version
dist/PawseyUploader-v1.4.exe # retained older version
README.md                   # this file
```

## Versions — which `.exe` to download

Previous releases are kept alongside the latest so you can always roll back or
compare. `PawseyUploader.exe` (no version suffix) is **always the latest build**;
the running app shows its version in the title bar and on the Help tab.

| File | Version | Notes |
|---|---|---|
| `dist/PawseyUploader.exe` | **v1.9 (latest)** | Background (survives app close) + resumable "Send to another project" |
| `dist/PawseyUploader-v1.8.exe` | v1.8 | "Send to another project" (copy data to another Pawsey project's bucket) |
| `dist/PawseyUploader-v1.7.exe` | v1.7 | DPIRD-primary header (larger DPIRD logo left, APPN top-right) |
| `dist/PawseyUploader-v1.6.exe` | v1.6 | Multi-project management, presigned share pages |
| `dist/PawseyUploader-v1.4.exe` | v1.4 | Two-way sync, recycle bin, unattended operation |

`PawseyUploader.exe` (no version suffix) is **always the latest build**. The
running app shows its version in the title bar and on the Help tab, so you can
confirm which one you launched.

## Multiple Pawsey projects

Each Pawsey project is its own set of Acacia keys. On the **Settings → Pawsey
projects** panel you can save several projects (each is an rclone S3 remote),
**Test** any of them, and **Set as active** so all tabs default to it (the
active project is marked ★). You'll usually work in one project at a time, but
having more than one lets you **copy data from one project to another**:

* On the **Storage** tab — select what you want to send, click **"📤 Send to
  another project…"**, pick the destination project + bucket, and copy. This is
  **copy-only** (nothing on the destination is ever deleted) and is the
  recommended way to hand a dataset to a collaborator who wants to analyse it in
  the cloud on Acacia rather than downloading it. (A collaborator who only needs
  to *download* the data, or has no Pawsey account, is better served by a
  shareable link — see below.)
  * **Keep running after I close the app** (ticked by default) runs the copy as
    a **background job**: it survives closing the app or the app crashing, logs
    to a file, and can be **resumed**. It still stops if the computer sleeps or
    shuts down — for genuinely long, unattended transfers run rclone on a
    Pawsey/Nimbus VM. Untick it to run in-app with a live result instead (which
    stops if you close the app).
  * **Resuming** — every cross-project copy is remembered. If one is interrupted
    (close, crash, or restart), reopen the app and use **"Background copies…"**
    (next to the Send button) to **Resume** it. Resuming re-runs `rclone copy`,
    which skips files already present at the destination, so it continues rather
    than starting over — no duplicates, nothing deleted.
* Or, the manual route — Copy/Cut in one project, switch the Remote dropdown to
  the other, then Paste.
* Or on the **Console** — `copy projectA:bucket projectB:bucket -P`.

If the two projects use different keys, the data streams **through your
machine** (download then upload), not server-side — so run large migrations on
a Pawsey/Nimbus VM. To access **someone else's** project you need an access
key + secret with permission on their bucket(s); the cleanest route is to be
added to their project (make your own keys) or have them grant your existing
key bucket access via a policy (which also enables fast server-side copies).

Existing single-remote setups are migrated into this panel automatically on
first launch — nothing to redo.

## Sharing links — how it works

Pawsey Acacia is Ceph S3 storage, and rclone's `link` command does **not**
support expiring public links on S3. So "🔗 Generate link…" instead builds a
**presigned URL**: a normal HTTPS link with a time-limited signature baked in,
generated locally from your stored access key/secret (no third-party packages).
Every link **works from any browser, anywhere — no Pawsey account needed** by
the recipient.

* **A file** → one presigned URL, copied to your clipboard. Send it to anyone.
* **A folder or bucket** → the app builds a small **file-browser web page**
  (expand folders/subfolders, view or download any file, or download a whole
  folder with one click), **uploads that page into the bucket** under a
  `_shares/` prefix, and gives you a **single presigned link to the page**.
  Send that one link — it opens the browser for the recipient. The page and
  every link inside it expire together.

You choose the validity (minutes / hours / days). S3's hard maximum is **7 days**;
longer requests are capped to that. Anyone with a link can access that data
until it expires, so don't post them publicly unless that's intended.

> Folder downloads: the browser saves each file individually into the
> recipient's Downloads folder (the original folder path is preserved in each
> file's name). A single static link can't recreate directories or zip large
> research datasets reliably, so per-file downloads are used instead. Share
> pages accumulate under `_shares/` in the bucket — delete them from the
> Storage tab whenever you like.
