#!/usr/bin/env bash
#
# dump_all_profile_images.sh
#
# Invokes the LisaFileSystemTool.py tool for every file in the /tmp folder:
#     python3 LisaFileSystemTool.py dump <filename>
#
# Usage:
#     ./dump_all_profile_images.sh [folder]
#
#     folder  defaults to /tmp
#
# All output goes straight to the console (no log files).
#
# The tool's stdin is passed through unchanged, so if remove_file_protection()
# finds a protected file and asks "Continue? [y/N]", the question appears here
# and your answer (typed at the terminal, or piped in, e.g.
#     yes | ./dump_all_profile_images.sh
#     yes n | ./dump_all_profile_images.sh   # answer 'n' to the first prompt, then EOF)
# is read from stdin. Note that answers are consumed one line per prompt, in
# the order the files are processed.
#
# Notes:
#  * Hidden files (dotfiles) are skipped.
#  * Files the tool refuses or errors on (e.g. larger than its 100MB limit)
#    are reported and the script continues with the next file.

set -u

# FOLDER="${1:-/tmp}"
FOLDER="${1:-/tmp}"
DUMPER="python3 ../LisaFileSystemTool.py"

if [ ! -d "$FOLDER" ]; then
    echo "ERROR: folder not found: $FOLDER" >&2
    exit 1
fi

shopt -s nullglob

total=0
ok=0
failed=0

for file in "$FOLDER"/*; do
    [ -f "$file" ] || continue
    total=$((total + 1))
    name="$(basename "$file")"
    echo "=== [$total] $name"
    if $DUMPER deserialize "$file"; then
        ok=$((ok + 1))
        echo "    OK"
    else
        rc=$?
        failed=$((failed + 1))
        echo "    FAILED (exit code $rc)"
    fi
    echo ""
    echo ""
    echo ""
    echo ""
    echo ""
done

echo
echo "Done: $ok/$total file(s) succeeded, $failed failed."
