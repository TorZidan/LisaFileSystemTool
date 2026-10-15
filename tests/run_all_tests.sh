#!/bin/sh
# Run the full test suite for LisaFileSystemTool.py and LisaFileSystemToolPerFile.py.
#
# Usage:
#   tests/run_all_tests.sh                 # run all five disk images
#   tests/run_all_tests.sh <image> [...]   # run only the given image file(s),
#                                          #   e.g. tests/run_all_tests.sh profile-LOS3.1.image
#
# The tests run every supported command of both tools against fresh copies of
# the image files in /tmp/LisaFileSystemToolTests/ (the originals in this
# folder are never modified). See run_tests.py for details.
exec python3 "$(dirname "$0")/run_tests.py" "$@"
