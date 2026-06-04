#!/usr/bin/env bash
# ----------------------------------------------------------------------
#  Build PawseyUploader binary for Linux (Ubuntu, etc.)
#  Produces a single-file ELF executable in ./dist/
# ----------------------------------------------------------------------
set -e

cd "$(dirname "$0")"

echo
echo "=== Pawsey Uploader - Linux build script ==="
echo

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found on PATH."
    echo "Activate your conda env first:  conda activate pawsey"
    exit 1
fi

python3 --version

echo
echo "Installing/updating PyInstaller..."
python3 -m pip install --upgrade pyinstaller >/dev/null

# Clean previous artefacts
rm -rf build dist PawseyUploader.spec

echo
echo "Building PawseyUploader (this takes 1-2 minutes)..."
echo
python3 -m PyInstaller \
    --onefile \
    --windowed \
    --name PawseyUploader \
    --clean \
    pawsey_uploader.py

echo
echo "======================================================================"
echo " Build complete."
echo " Your binary is at:   $(pwd)/dist/PawseyUploader"
echo "======================================================================"
echo
echo "On the target machine, rclone must be on PATH for the app to work."
echo "On Ubuntu also ensure Tk is installed:  sudo apt install python3-tk"
echo
