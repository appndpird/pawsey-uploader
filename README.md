# Pawsey Data Management App (PDMA)

> **v2.0** — formerly *Pawsey Uploader*. Renamed and now able to create
> **permanent public links** for publishing datasets (alongside the existing
> temporary expiring links). Your saved projects, keys and history carry over
> unchanged (the on-disk folder `~/.pawsey_uploader` is deliberately kept).

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

All three transfer modes carry **empty folders** across, in both directions —
see [Empty folders](#empty-folders) below.

### Empty folders

Every mode (Copy, Mirror, Two-way sync) transfers empty folders, and so do the
Storage tab's upload / download / copy / paste / rename actions. Placeholder
folders with nothing in them yet will exist on Pawsey after the transfer.

This needs special handling because Acacia is **object storage**: a "folder" is
not a real object, it is merely implied by the keys of the files inside it. An
empty folder has no files to imply it, so it has nowhere to exist. The app
therefore runs rclone with `--create-empty-src-dirs --s3-directory-markers`,
which writes a zero-byte marker object named `<folder>/` to hold the folder
open. rclone and the app's Storage browser both show those markers as ordinary
folders, never as stray files, and folder deletes/moves pass the marker flag so
no ghost folders are left behind.

**The folder itself, not just what is inside it (v2.3).** rclone's
`--create-empty-src-dirs` replicates the empty folders *inside* a source folder
but never the source folder itself, so up to v2.2 a folder that was **itself
empty** — a placeholder `Documents/` or `code/` uploaded, pasted or moved on its
own — was "nothing to transfer" for rclone: it left no marker on Pawsey, showed
nothing on the Storage tab, and a *move* of such a folder made it disappear
outright (rclone moved nothing, then the old folder marker was cleared). Since
v2.3 every folder operation creates the destination folder first (`rclone mkdir
--s3-directory-markers`, one zero-byte marker, no data): Transfer-tab Copy /
Mirror / Two-way sync, Storage-tab **Upload folder**, **Paste** (copy and cut),
**Rename/move**, **Send to another project** and **Download** (the local folder
is created). **New folder** now writes the same marker instead of a visible
`.keep` file. A preview (dry-run) still writes nothing.

Zero-byte **files** were never affected: rclone copies them like any other file
and they show on the Storage tab with size 0 B.

> **Already uploaded a dataset without its empty folders?** Just run the same
> transfer again. Copy adds the missing folders and re-uploads no files. For a
> folder that is itself empty, upload it again with **Upload folder** (v2.3+).

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
  app never silently does a dry-run.) A preview's summary is worded in the
  conditional — *"WOULD add 12, update 3, …"* — so a preview can never be
  mistaken for a real run.
* **Verify both sides (check)** — compares source and destination and reports
  whether they really hold the same content, transferring nothing. Two passes:
  **files** via `rclone check` (checksums wherever Pawsey has them, size for
  the rest — and it tells you how many could only be size-compared rather than
  claiming a content match it didn't make), and **folders** by comparing both
  directory trees, which is the only way to spot an empty folder present on one
  side and missing on the other. `rclone check` looks at files only and is
  blind to that.

## Unattended / long-running transfers

The app is designed to be left running for days:

* **Auto-restart on transient crashes** — if rclone dies on a network blip, the
  app retries with backoff (capped attempts) and keeps the transfer alive.
* **Smart failure detection** — authentication failures (suspended keys, bad
  credentials, `403`) and non-retryable errors (missing bucket, bisync needing
  `--resync`) stop the transfer instead of retrying pointlessly.
* **Two-way baseline recovery** — rclone keeps its own record of what both sides
  looked like after the last successful two-way sync, and refuses to run without
  it (otherwise it couldn't tell *"you deleted this"* from *"the other side
  gained it"*). If that record goes missing — an interrupted run, a cleared
  rclone cache, a different machine — the app recognises the specific failure,
  explains it, and offers to rebuild the baseline. The rebuild merges both sides
  and deletes nothing; where the same file differs, the local copy wins.
  A brand-new Pawsey destination folder is also created automatically, since
  two-way sync will not start against a prefix that does not exist yet.
* **Heartbeat file** — `~/.pawsey_uploader/heartbeat.json` is refreshed
  periodically so external monitoring scripts can confirm the app is alive.
* **Per-transfer logs** — full verbose rclone output is written to disk under
  `~/.pawsey_uploader/logs/` even though the on-screen view is trimmed.

## Other features

| Feature | Where to find it |
|---|---|
| **Rename / Match** — folders renamed locally after upload are matched to their Pawsey copy by *content* (file names + sizes, MD5 spot-checks) and renamed server-side instead of re-uploaded; also spots partial duplicate copies | Rename / Match tab |
| **Multiple Pawsey projects** — save several projects (each its own keys), mark one active, switch any time | Settings tab → Pawsey projects |
| **Copy / Cut / Paste** files & folders on Pawsey (Windows-style; server-side move/copy, no re-upload) | Storage tab |
| **Send to another project** — copy selected buckets/folders/files into a *different* Pawsey project's bucket (copy-only, nothing deleted) | Storage tab → "📤 Send to another project…" |
| **Share / Publish link** — **temporary** (user-chosen expiry, max 7 days) *or* **permanent** (public, never expires — for publishing datasets). File → one link; folder/bucket → an HTML index page of links | Storage tab → "🔗 Share / Publish link…" |
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

**A folder shows as empty / "Failed to list …: Command timed out" (fixed in v2.4):** older versions fetched per-object metadata while listing, so folders with thousands of files could not be listed within 2 minutes and appeared empty. Upgrade to v2.4.

**Start here (v2.3.1+):** open `%USERPROFILE%\.pawsey_uploaderpp_errors.log`.
It records each app start (Windows user, rclone path and version, config file),
every rclone call that failed and why, and any internal error. Each Windows
account has its own `.pawsey_uploader` folder, config and rclone remotes, so a
colleague launching the same `.exe` on the same PC does **not** share your
settings or your transfer history.

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
build_exe.bat               # one-click Windows build script — WARNING: wipes dist/
build_linux.sh              # one-click Linux build script
PDMA-v2.4.spec              # PyInstaller spec for the current release
PDMA-v2.3.1.spec            # spec for the previous release
PDMA-v2.3.spec              # spec for an older release
PDMA-v2.2.spec              # spec for an older release
PDMA-v2.1.spec              # spec for an older release
PawseyUploader.spec         # spec from the pre-PDMA naming
dist/PDMA-v2.4.exe          # prebuilt Windows executable — the LATEST version
dist/PDMA-v2.3.1.exe        # retained previous version
dist/PDMA-v2.3.exe          # retained previous version
dist/PDMA-v2.2.exe          # retained previous version
dist/PDMA-v2.1.exe          # retained previous version
dist/PDMA-v2.0.exe          # retained previous version
dist/PawseyUploader.exe     # retained v1.9 (last build under the old name)
dist/PawseyUploader-v1.8.exe # retained previous version
dist/PawseyUploader-v1.7.exe # retained previous version
dist/PawseyUploader-v1.6.exe # retained previous version
dist/PawseyUploader-v1.4.exe # retained older version
README.md                   # this file
```

## Versions — which `.exe` to download

Every release is kept alongside the latest so you can always roll back or
compare. **Download the highest-numbered `PDMA-v*.exe`** — that is the latest
build. The running app shows its version in the title bar and on the Help tab,
so you can confirm which one you launched.

| File | Version | Notes |
|---|---|---|
| `dist/PDMA-v2.5.1.exe` | **v2.5.1 (latest)** | Rename / Match: **live progress** while a folder is moved on Pawsey (a rename there is a server-side copy of every object - about 1 min per 6 GB - not an instant metadata change), the confirm dialog says how long to expect and to keep the app open, closing mid-move asks first, and a move that was cut short shows up on the next scan as a **"Finish rename on Pawsey"** row that moves the rest across |
| `dist/PDMA-v2.5.exe` | v2.5 | **Rename / Match tab.** A project/site/date folder renamed on the PC after upload no longer re-uploads: the tab lists both sides once, pairs folders by contents (file names + sizes; MD5 spot-checks against Pawsey's stored hashes), and applies the renames as server-side moves on Pawsey (or renames the local folders to match). Flags partial duplicate copies left by an interrupted sync, lists folders only on one side, deletions honour the recycle-bin setting, everything is logged with a note |
| `dist/PDMA-v2.4.exe` | v2.4 | **Storage tab lists big folders in seconds instead of timing out.** Plain `rclone lsjson` does one HEAD request per object on S3 to fetch the original mtime and MIME type; a folder of 8,400 images took 4.5 min, exceeded the browser's 2-min limit and was shown as *empty* ("Failed to list … Command timed out"). Listings now use `--use-server-modtime --no-mimetype` (2 s for the same folder; the Modified column shows the upload time) and the limit is 15 min |
| `dist/PDMA-v2.3.1.exe` | v2.3.1 | **Diagnostics log** `~/.pawsey_uploader/app_errors.log`: every failing rclone call, every internal error (now also shown in a dialog instead of vanishing), and each app start with user, rclone path and version — send this file when reporting a problem |
| `dist/PDMA-v2.3.exe` | v2.3 | **Empty folders that are themselves empty** (an empty `Documents/` uploaded, pasted, moved, sent or synced on its own) now exist on Pawsey and show on the Storage tab — previously rclone saw "nothing to transfer" and wrote no marker; a move of such a folder no longer makes it vanish; folder downloads create the local folder; **New folder** writes a folder marker instead of a `.keep` file; real runs now count folder creations ("Making directory") in the change summary |
| `dist/PDMA-v2.2.exe` | v2.2 | **Empty folders** now transfer in every mode and both directions; **change detection fixed** — a preview no longer reports "no changes" when it has thousands of files to move; verify compares folders too and no longer passes same-size/different-content files; two-way baseline recovery |
| `dist/PDMA-v2.1.exe` | v2.1 | Preview-by-default, password-gated destructive modes, verify-both-sides, adjustable delete cap |
| `dist/PDMA-v2.0.exe` | v2.0 | Renamed to PDMA; **permanent public links** for publishing datasets (temporary links retained) |
| `dist/PawseyUploader.exe` | v1.9 | Background (survives app close) + resumable "Send to another project" |
| `dist/PawseyUploader-v1.8.exe` | v1.8 | "Send to another project" (copy data to another Pawsey project's bucket) |
| `dist/PawseyUploader-v1.7.exe` | v1.7 | DPIRD-primary header (larger DPIRD logo left, APPN top-right) |
| `dist/PawseyUploader-v1.6.exe` | v1.6 | Multi-project management, presigned share pages |
| `dist/PawseyUploader-v1.4.exe` | v1.4 | Two-way sync, recycle bin, unattended operation |

> Note: `PawseyUploader.exe` (no version suffix) is **not** the newest build —
> it is the last release made under the old name (v1.9). Use `PDMA-v2.5.1.exe`.

### Rebuilding the `.exe` yourself

> **Do not use `build_exe.bat` if you want to keep the older releases** — it
> runs `rmdir /s /q dist`, which deletes every executable in `dist/`.

Build a single target from its spec instead, which touches only that one file:

```bat
python -m PyInstaller PDMA-v2.5.1.spec --noconfirm --distpath dist
```

Use a **python.org / system Python**, not a conda env: PyInstaller in a conda
environment can fail to bundle Tcl/Tk, and the resulting `.exe` dies at startup
with `ImportError: DLL load failed while importing _tkinter`. A healthy build is
~10.9 MB; a broken one is noticeably smaller (~7.8 MB) because the Tk runtime is
missing.

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

## Rename / Match — fixing renamed folders without re-uploading  *(new in v2.5)*

Symptom: you renamed `2026_WonganHill_F` to `2026WonganHill_F` (or `2026-07-08`
to `20260708`) on the PC after it was uploaded. The next Copy puts a second
full copy on Pawsey; the next Mirror deletes the old one and uploads it all
again. Either way ~50 GB of identical raw data moves for a name change.

The **Rename / Match** tab compares a local project folder with its Pawsey
counterpart (e.g. `F:\...6_AcidTolerance_I_DPIRD` ⟷
`pawsey:dpird-appn-2026/2026_AcidTolerance_I_DPIRD`) and pairs folders by
**what is inside them**: same file names with the same sizes = same folder,
whatever it is called. If the *project* folder itself was renamed, **Find on
Pawsey by content…** scans the bucket and picks the folder holding the same
files. A few files per pair are MD5-checked against the hash
Pawsey stores for each object. The result is a table of actions you tick:

| Row | Meaning | Default |
|---|---|---|
| Rename on Pawsey / Rename locally | same data, different name | ticked |
| Remove partial copy on Pawsey | a copy under the *new* name whose files are all in the real folder (interrupted sync) | unticked |
| Delete from Pawsey? | folder with no counterpart locally | unticked |
| Only on this PC | new data — your next Transfer uploads it | info only |
| Conflict | two copies that differ — sort out by hand | info only |

Renames on Pawsey are server-side moves: object storage has no rename, so the
storage copies every object to its new key and deletes the old one. Nothing is
uploaded or downloaded, but it takes about 1 minute per 6 GB (a 110 GB site
folder ~20 min); the progress log shows the running totals. Keep the app open
until it reports done - a move cut short leaves the folder split between the
two names, and the next scan offers a "Finish rename on Pawsey" row. If you
want an instant result, choose the other direction: renaming the *local*
folders to match Pawsey is immediate.
Deletions obey the recycle-bin setting on the Transfer tab. Each action is
logged with your note. Afterwards, scan again, then run the normal Transfer
for the genuinely new files; a two-way-sync pair needs its baseline rebuilt
once.

## Sharing & publishing links — how it works

"🔗 Share / Publish link…" offers **two kinds** of link. Both work from any
browser, anywhere — **no Pawsey account needed** by the recipient.

### Temporary link (expiring) — everyday sharing

Pawsey Acacia is Ceph S3 storage, and rclone's `link` command does **not**
support expiring public links on S3. So the temporary option builds a
**presigned URL**: a normal HTTPS link with a time-limited signature baked in,
generated locally from your stored access key/secret (no third-party packages).

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

### Permanent link (public, never expires) — publishing a dataset  *(new in v2.0)*

A presigned URL can never outlive S3's 7-day signature limit, which is useless
for a **published** dataset that must stay reachable forever (e.g. cited in a
paper). The permanent option instead makes the object itself **publicly
readable** — it sends an S3 `PUT ?acl` request setting a `public-read` ACL
(SigV4-signed with the standard library, no third-party packages) — and hands
back the plain, unsigned object URL `https://endpoint/bucket/key`, which **never
expires**.

* **A file** → its public URL (copied to your clipboard).
* **A folder or bucket** → every object is made public and an **index page** is
  published whose links are all permanent. The page lets the recipient download
  the **whole dataset**, any **folder**, or **individual files**. When
  publishing, the app also **offers to build a single ZIP** of the whole
  dataset — if you say yes, the page shows a **"Download entire dataset — one
  ZIP"** button as well (a one-file download that works in every browser). The
  ZIP is streamed together on your machine and uploaded into the bucket (extra
  storage ≈ dataset size); it's a snapshot, so re-publish to refresh it.

> **How downloads work.** Published objects are stored with a
> `Content-Disposition: attachment` header (set via a server-side
> metadata-only copy at publish time), so every link *downloads* rather than
> opening in the browser.
>
> **Preserving the folder structure.** "Download whole dataset" / "Download
> folder" recreate the original subfolders and files on the recipient's disk —
> in **Chrome/Edge** (via the File System Access API) they pick a destination
> folder once and the page streams every object into it, rebuilding the tree.
> Because the share page and the data objects are on the **same host**
> (`projects.pawsey.org.au`), the page can fetch them directly — no CORS setup
> needed. On **Firefox/Safari** (no File System Access API) it falls back to
> saving each file individually into Downloads, with the folder path kept in
> each file's name.

> **Precautions.** This makes the data **public to anyone on the internet, with
> no expiry**. The app asks you to confirm first. Only publish data that is
> meant to be open — **never** anything personal, sensitive or embargoed. It
> requires the bucket to **allow public access**; if Pawsey has public access
> disabled for the bucket, the app tells you — ask `help@pawsey.org.au` to
> enable public read. To un-publish, remove/overwrite the object or ask Pawsey
> to reset the ACL to private.

> Folder downloads: the browser saves each file individually into the
> recipient's Downloads folder (the original folder path is preserved in each
> file's name). A single static link can't recreate directories or zip large
> research datasets reliably, so per-file downloads are used instead. Share
> pages accumulate under `_shares/` in the bucket — delete them from the
> Storage tab whenever you like.
