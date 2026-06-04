# Pawsey Uploader

A cross-platform (Windows / Ubuntu) GUI for managing rclone transfers to Pawsey
Acacia object storage. Single-file Python 3 + Tkinter app — no third-party
Python packages required to **run**. PyInstaller is only needed to **build** a
standalone `.exe`.

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

## What's included in the app

| Feature | Where to find it |
|---|---|
| Configure / edit Pawsey remote (access key, secret, endpoint) | Settings tab |
| Browse to source folder, default = app's own folder | Transfer tab |
| Mandatory notes field written to a permanent log | Transfer tab — empty notes = no transfer |
| Normal copy with live progress | Mode: Copy |
| Mirror sync (exact replica, deletes extras) | Mode: Mirror sync (extra warning) |
| Resume after computer restart | Mode: Resume last transfer |
| Editable default project / bucket / source | Settings tab → Defaults |
| Create / delete buckets; deletion needs typed confirmation | Buckets tab |
| Storage usage bar chart | Storage tab |
| In-app help | Help tab |

## Data files

Everything user-specific lives in `~/.pawsey_uploader/` (i.e.
`%USERPROFILE%\.pawsey_uploader\` on Windows):

* `config.json` — saved defaults and credentials
* `transfer_log.jsonl` — permanent audit log (one JSON object per line)
* `last_transfer.json` — last transfer's parameters, used by Resume

On Linux, restrict permissions: `chmod 600 ~/.pawsey_uploader/config.json`

## Troubleshooting

* **`rclone executable not found`** — activate the conda env or install rclone.
* **`403 UserSuspended`** — your Pawsey access keys are disabled. Contact
  `help@pawsey.org.au`; no app setting can fix this.
* **`403 Forbidden`** — credentials lack access to that bucket. Check which
  remote owns it.
* **Transfer interrupted** — relaunch the app, choose *Resume last transfer*,
  click Start. rclone skips files already on Pawsey.
* **`.exe` is flagged by antivirus** — PyInstaller bundles can trigger
  occasional false positives. Whitelist it, or build it yourself locally so
  your AV sees that you produced it.

## Files in this folder

```
pawsey_uploader.py    # the app
build_exe.bat         # one-click Windows build script
build_linux.sh        # one-click Linux build script
README.md             # this file
```
