
# Uploading Lisa OS source files to a ProFile disk image and compiling them in Lisa Workshop

**Author:** [TorZidan](https://github.com/TorZidan)  
**Last Updated:** Sept 26, 2026  

## Overview

The presented `upload_files.sh` is a Linux shell script that "uploads" the (patched) Lisa OS source files onto the
ProFile disk image **`LOS_Compilation_Base.image`**, so that you can boot the
image in LisaEm (or on a real Lisa with an ESProfile) and compile the Lisa OS
from source with Workshop.

The shell script uses the `../LisaFileSystemToolPerFile.py` tool (the `add` and
`replace` commands — see the project README, §1.3). 

## What the script does

Given the folder that contains the Lisa OS source tree (it must have
`APPS/` and `LISA_OS/` subfolders inside), the script:

1. **Replaces** the `system.os` on the image with the local
   [`system.os`](system.os) from this folder (see the workaround below —
   this is done with the `replace` command).
2. **Adds** the local [`MAKE-ALL_NODISKS2.TEXT`](MAKE-ALL_NODISKS2.TEXT)
   build script, stored on the image as `ALEX/MAKE/ALL_NODISKS2.TEXT`.
3. **Adds 858 selected source files** from the source tree, from the
   `APPS/` subfolders (APBG, APCL, APDM, APEW, APHP, APIN, APLC, APLD,
   APLL, APLP, APLT, APLW, APPW) and from the `LISA_OS/` subfolders
   (BUILD, GUIDE_APIM, LIBS, OS, TKIN, TKALERT).

That is **860 upload operations in total** (1 replace + 859 adds).

The host files are unix-converted Lisa text files named `<name>.TEXT.unix.txt`
(a few have slightly different names — e.g. lowercase `.text.unix.txt`, or a
`.` instead of the usual `-` — which the script maps to the correct names on
the image); on the disk image each one is stored under the Lisa file name
`<name>.TEXT`.

The script works on the image **in place** (it is not idempotent-safe to run
twice expecting "860 added" — on a re-run the already-present files are
simply counted as "already present", and that is *not* an error).

## Why do we have to do this?
Can't you share a disk image file with the Lisa OS source files already "uploaded" onto it?

Answer: I can't share such disk image because of license limitations. At https://info.computerhistory.org/apple-lisa-code we read:
  - You may not, and you agree not to: redistribute, publish, sublicense, sell, rent or transfer the Apple Software.

## The system.os workaround (read this)

LOS has a bug (in `source-fsprim.text.unix.txt`) where **overwriting an
existing file with a *larger* file corrupts the volume** (under certain
conditions). The bug lives in `system.os` itself — and the LOS build process
triggers exactly that: it builds a new `system.os` that is slightly larger
than the current one and overwrites it, which corrupts the file system; the
next boot then fails with error **10730** ("system.os is corrupted").
Ironically, the bug in system.os causes the corruption of the very same file system.os.

The workaround is to pad the original system.os file (of size 185344 bytes) to a new size of 194,000 bytes before running the compilations,
so that the generated system.os file (of size 193,024 bytes) will be smaller than the current file (of size 194,000), so overwriting it would work just fine.
The "replace" command does not have such bug, and is able to successfully replace the file.

## Usage

Prerequisites: a Linux host with `python3`, and this project's
`LisaFileSystemToolPerFile.py` in the parent folder (unmodified relative
layout).

1. Download `lisa-source.zip` from
   <https://info.computerhistory.org/apple-lisa-code> and unzip it.

2. In the folder with the unzipped files there is one subfolder,
   `Lisa_Source` (that is the source root the script expects).

3. Download Alexander McLeod's
   [`patch_files.py`](https://github.com/alexthecat123/LisaSourceCompilation/blob/main/scripts/patch_files.py)
   and run it from inside that folder:

   ```
   python3 patch_files.py Lisa_Source .
   ```

   It should print `Successfully applied 248/248 patches`.

4. Run the script, passing the folder that *contains* `Lisa_Source`:

   ```
   ./upload_files.sh /path/to/folder-containing-Lisa_Source
   ```

   (The script `cd`s into its own folder, so a relative argument is resolved
   against the directory you invoke it from.)

   A successful first run ends with:

   ```
   Done: 860 file(s) added, 0 already present, 0 failure(s).
   ```

5. Mount `LOS_Compilation_Base.image` in the LisaEm emulator, or on a real
   Lisa using an ESProfile hardware emulator; boot into Workshop and run
   `<ALEX/MAKE/ALL_NODISKS2` to build everything except Lisa Guide.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All uploads succeeded (a file that was already on the image counts as "already present", not a failure — so the script can safely be re-run). |
| 1 | A setup problem (missing source folder / `APPS` / `LISA_OS` / image), or at least one file upload failed. |
| 2 | Usage error (no folder argument given). |

Each individual upload uses the tool's exit codes: `0` = added,
`3` = already present, anything else = failure.

## The disk image

`LOS_Compilation_Base.image` is a copy of
["LOS Compilation Base.image"](https://github.com/alexthecat123/LisaSourceCompilation/blob/main/LOS%20Compilation%20Base.image.zip)
from Alexander McLeod's
[LisaSourceCompilation](https://github.com/alexthecat123/LisaSourceCompilation)
repository. It contains a fresh installation of LOS 3.0 and Workshop 3.0
plus his build scripts. It does NOT contain the Lisa OS source files.

## Credits and Thanks
Thanks to [Alexander McLeod](https://github.com/alexthecat123) for preparing the `LOS_Compilation_Base.image` and for
figuring out how to compile the Lisa OS sources in the first place. 

## How to compile the Lisa OS sources on a real Lisa 

You will need an ESProfile hardware ProFile disk emulator on a Lisa with H ROMS and 1.5 MB of RAM or more (2 MB is the max).

Prepare the file `LOS_Compilation_Base.image` using the instructions above, copy it to an SD card,
plug it into your ESProfile, power-on the Lisa and boot from the disk image.

  - If you get a boot error, it is most likely because the disk image was created as Lisa Disk 1 (on a Lisa 2/10 where the Widget hard drive is at boot position 1), but now you are trying to boot up from Disk 2 (the case on a Lisa 2/5).

In the environment selection dialog, click on "Workshop" and then the "Start" button. You are in Workshop 3.0

Chose (R)un, then type `<ALEX/MAKE/ALL_NODISKS2` , enter. This will run "macro" file ALEX/MAKE/ALL_NODISKS2.TEXT .
It will build all Lisa OS sources, except Lisa Guide. Upon success, you will arrive back to the Workshop main menu (otherwise, the the script halts and shows an error).

Your next immediate task is to shut down and restart your Lisa. Why: the build script overwrites the Lisa OS file `system.os` which is basically the file system. So, at this point, a restart is needed, to "pick up the new file".

Easter egg unlocked! Once you build everything and restart your Lisa, at the "environments selection" window there will be a new "UltraDOS" environment. Check it out. Not seeing the "environments selection" window ? Reboot again and press any key while Lisa is booting, it will show up.

And last step: to compile LisaGuide in Workshop, choose (R)un, then type `ALEX/MAKE/APIM(1)` ; here, "(1)" is a macro argument which means "skip creating a LisaGuide floppy disk".

Note: Alex's original instructions at https://github.com/alexthecat123/LisaSourceCompilation use the `<ALEX/MAKE/ALL_NODISKS` command, which includes LisaGuide. I have split it in two above, because, if run in one command, LisaEm (and maybe a real Lisa) crashes, most likely due to out-of-memory.


## How to compile the Lisa OS sources in the LisaEm emulator

Read my https://github.com/arcanebyte/lisaem/blob/master/LisaEmAsASoftwareDevelopmentEnvironment.md . Instead of the "pseudo-tty serial port file upload" described here, we use the subject script to do the file "upload".

Download and run the most recent "continuous" LisaEm build for your platform from https://github.com/arcanebyte/lisaem/releases (the "2.0" release is missing important bug fixes).
Configure LisaEm to use 1.5 MB or RAM, H-ROMs, "I/O ROM of "88". Mount the `LOS_Compilation_Base.image` on the "internal" parallel port. 

Then follow the instructions "on a real Lisa" above.

This process has been verified to works repeatedly and consistently well, with no crashes. On my PC, it takes about a minute to "upload" the Lisa OS source files (using the shell script above), and about 5 minutes to compile everything (except LisaGuide) at top emulation speed (Using the Throttle->512Mhz menu).

This opens the doors for a full "write code -> upload it -> build it-> run it -> test it" automated AI development cycle with LisaEm. Exciting times. Enjoy!
