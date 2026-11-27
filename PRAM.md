# The Lisa parameter memory (PRAM), the boot device position, and command `change-boot-id`

This file explains why a LOS 3.x boot volume (e.g. a ProFile/ESProfile disk image) often boots only
from the position where it was set up, and fails with boot error **10738** after being moved to
another position, e.g. from the internal parallel port of a Lisa 2/10 (I/O ROM 88) to the external
parallel port of a Lisa 2/5 (I/O ROM A8). It also describes how the `change-boot-id` command of
`LisaFileSystemTool.py` fixes such a volume, and what to watch out for in LisaEm.

The findings come from the LOS sources (`LISA_OS/OS/` in the Apple Lisa source code release of the
Computer History Museum); the source file and routine names are given in parentheses.

## 1. The PRAM and its snapshot in the MDDF

The Lisa has 64 bytes of battery-backed parameter memory (PRAM). LOS 3.x keeps in it the
Preferences settings **and the device configuration**: which driver controls which
slot/channel/device position. The PRAM lives at the odd addresses `$FCC181`..`$FCC1FF` of the I/O
board's shared memory (`INIT_READ_PM`/`INIT_WRITE_PM` in `source-STARASM2.TEXT`).

The OS also keeps a *snapshot* of the PRAM in the MDDF of the boot volume: field `pmem` at MDDF
offset 208 is `alarm_ref` (2 bytes) followed by the 64 PRAM bytes (MDDF offsets 210..273). When LOS
writes the PRAM (e.g. from Preferences, `Write_PMem` in `SOURCE-PMEM.TEXT`), it updates the timestamp
and checksum and writes both the PRAM and the snapshot (`PMSnapshot` in `source-fsui3.text`).

The PRAM layout (`pmem` in `SOURCE-STARTUP.TEXT`):

| PRAM offset | Field |
|-------------|-------|
| 0-1 | `Version` (4 for LOS 3.x; LOS 1.x/2.x use version 2, without a device configuration) |
| 2-3 | `TimeStamp` |
| 4-8 | `BootVol` (high nibble of byte 4), contrast, beep volume, mouse, repeat-rate, ... settings |
| 9 | `CDcount`: the number of device entries |
| 10-59 | `DevConfig`: the packed device entries, unused bytes are `0xFF` |
| 60-61 | `MemLoss` |
| 62-63 | `Checksum`: over the 32 big-endian words, `acc = rol16(acc + word)` must end up 0 (boot ROM routine at `$FE00BC`; verified on ~60 disk images) |

### 1.1 Valid and invalid PRAM

At boot, the OS decides whether the PRAM of the machine is *valid* with two tests (`INIT_CONFIG` in
`SOURCE-STARTUP.TEXT`: `pm_good := (its_cksum = 0) and (pm_image.version = cd_pm_version)`):

1. **The checksum is correct**: the checksum rule in the table above gives 0.
2. **The version is 4** (`cd_pm_version`, the LOS 3.x format).

If either test fails, the PRAM is *invalid*, and the OS does not use what is in it. The same two
tests decide whether the snapshot in the MDDF is valid.

Typical causes of an invalid PRAM:

* **It lost its power.** The PRAM keeps its contents only while it has power: from the battery, or
  from the standby power while the Lisa is plugged in. With a dead or removed battery, every time the
  Lisa is unplugged the contents are lost, and come back as garbage or zeros. Most surviving Lisas no
  longer have a working battery, so this is the common case.
* **It was written by an older system**, e.g. LOS 1.x/2.x, which uses version 2 of the format.
* **It was cleared on purpose**, e.g. the Zap PRAM button of LisaEm, or deleted PRAM lines in
  LisaEm's config file (see §5). An all-zero PRAM passes the checksum test (zeros add up to 0), but
  fails the version test, so it is invalid.

"Valid" says nothing about the contents: a valid PRAM can still lack the entry for the position the
Lisa boots from. This is the trap described in §4: a valid PRAM always wins over the snapshot.

What the OS does with each combination (`INIT_CONFIG`):

| Machine PRAM | Snapshot in the MDDF | What the OS does |
|--------------|----------------------|------------------|
| valid | valid and in sync (same timestamp, and written by this machine: `pmem_id` = its serial number) | uses the PRAM |
| valid | invalid, or not in sync | uses the PRAM, and later writes it over the snapshot |
| invalid | valid | copies the snapshot into the PRAM (`INIT_WRITE_PM`), then uses it |
| invalid | invalid | uses default settings with an empty device list (`DEFAULTPM`), so only built-in positions (e.g. the Lisa 2/10 internal disk) can be found |

LOS also reports the result to programs that read the PRAM (`Read_PMem` in `SOURCE-PMEM.TEXT`):
warning -620 means the PRAM was valid but the snapshot was not, -621 the other way round, and -622
both were invalid.

## 2. The device entries

Each device entry (`GetNxtConfig`/`CRAK_PM` in `SOURCE-PMEM.TEXT` and `SOURCE-CDCONFIGASM.TEXT`)
is 3 to 10 bytes: `slot:4 chan:3 IDsize:1`, `dev:5 nExtWords:2 idbit:1`, 1 or 2 more bytes of the
driver id, then `nExtWords` 16-bit words. The stored slot/chan/dev are the public numbers minus 1
(15/7/31 mean "none"). A stored slot of 15 ends the list.

Public slot numbers (`source-syscall.text`):

| Slot | What it is |
|------|------------|
| 1, 2, 3 | expansion slots 1-3 (generic: whatever card is plugged in) |
| 10 | serial ports (SCC): `#10#1` = port A, `#10#2` = port B |
| 11 | built-in (external) parallel port, Lisa 2/5 |
| 12 | internal disk, Lisa 2/10 (built in, never stored in the PRAM) |
| 13 | Sony floppy (built in) |
| 14 | Twiggy floppy (built in) |
| 15 | console (built in) |

Driver ids (from `SYSTEM.CDD`; the driver code is in the file `SYSTEM.CD_<name>`):
32 Serial Cable, 33 Parallel Cable (a printer on a parallel port), 34 2 Port Card, 35 Profile,
36 Sony, 37 Priam Card, 38 Priam Disk, 39 Archive Tape, 40 Console, 42 Modem A.

For example `ae f8 23` is "Profile (35) at #11, the built-in parallel port", and `92 f8 20` is
"Serial Cable (32) at #10#2, serial port B".

## 3. Why a disk image boots only from some positions

At boot (`INIT_CONFIG`, `INIT_BOOT_CDS`, `FIND_BOOT` and `FIND_PM_IDS` in `SOURCE-STARTUP.TEXT`):

1. If the PRAM of the machine is invalid (bad checksum or version, see §1.1), the OS copies the snapshot from
   the MDDF into the PRAM. This is the usual case on real Lisas with a dead or removed PRAM battery,
   and in LisaEm when the PRAM lines of the config file are missing or zeroed (see §5). If the PRAM is
   valid, it wins, and the OS later copies it over the snapshot.
2. The OS derives the boot position from the boot ROM's boot device byte (`$1B3`) and the I/O
   board type: on a Lisa 2/10 (I/O ROM 88) the disk is the *internal disk* (#12); on a Lisa 2/5
   (I/O ROM A8) it is the *built-in parallel port* (#11); for a 2-port parallel card it is
   the card (#N) plus its port (#N#M). A Lisa cannot boot from a serial port.
3. It looks the position up in the PRAM device configuration. The Lisa 2/10 internal disk is a
   built-in position (always known, Profile driver), so it needs no entry. The others need entries.
   If an entry is missing, the boot fails with **10738** (`stup_find_boot`, "can't find boot cd").

So a volume set up on a Lisa 2/10 (or in LisaEm with I/O ROM 88) has no #11 entry, and fails with
10738 on a Lisa 2/5 (I/O ROM A8). This was the case of `LisaSourceCompilation/LOS_Compilation_Base.image`,
whose only entry was the serial port B one.

Extra entries are harmless for the boot itself: on a Lisa 2/10 the OS rejects a #11 entry
(`cdnoparaport` in `NEW_DEVICE`, `SOURCE-CD.TEXT`) and goes on, so a fixed image still boots from
the 2/10 internal disk.

## 4. The `change-boot-id` command

```
python3 LisaFileSystemTool.py change-boot-id <disk image file name> [parallel|internal|all-parallel|slotN-portM]
```

Without a target it only prints the PRAM snapshot and its device entries. With a target it adds the
entries the OS needs for that boot position, keeps all other entries, recomputes the PRAM checksum,
and fixes the MDDF tag checksum and the DC42 header checksums (after a y/N confirmation):

| Target | Entries added |
|--------|---------------|
| `parallel` | `Profile` at #11 (external parallel port, Lisa 2/5) |
| `internal` | none: the Lisa 2/10 internal disk is a built-in position |
| `slotN-portM` | `2 Port Card` at #N and `Profile` at #N#M (N = 1..3, M = 1..2) |
| `all-parallel` | all of the above: #11, and #N, #N#1, #N#2 for slots 1-3 (10 entries, 30 bytes) |

A parallel port (#11, or a port of a 2-port card) that is configured with another device, e.g. a
printer ("Parallel Cable"), gets the Profile driver instead (set the printer up again in Preferences
if needed). An expansion slot configured with another card (e.g. a Priam card) has no parallel ports:
`slotN-portM` stops with an error, and `all-parallel` skips that slot. Serial ports and other devices
are never touched. Running the command again with the same target reports that there is nothing to do.

Not yet verified: whether a `2 Port Card` entry for an empty slot is harmless at boot (test that in LisaEm).

**The fix works only when the OS actually uses the snapshot**, i.e. when the PRAM of the machine is
invalid. If the machine has a valid PRAM without the needed entry, the PRAM wins (and overwrites the
fixed snapshot at boot). Then either clear the PRAM (on a real Lisa: remove the battery and unplug the
Lisa for a while; for LisaEm see §5), or boot once from the old position and add the device in LOS
Preferences, which writes a valid PRAM with the entry.

Once the OS has booted from a fixed snapshot, it has copied it into the PRAM, so the PRAM is valid and
has the new entries from then on (as long as it keeps its power: the battery, or the standby power
while the Lisa is plugged in).

## 5. LisaEm notes

* **Where LisaEm keeps the PRAM:** in the `[pram]` section of its config file (~/lisaem.conf on Linux). The section holds the
  1 KB of shared memory as 32 lines `pram0000` .. `pram03e0` of 32 bytes each; the PRAM is exactly the
  two lines **`pram00c0` and `pram00e0`** (`floppy_ram[0xc0..0xff]` in LisaEm = `$FCC181`..`$FCC1FF`).
  The other `pram` lines are other shared memory, not PRAM.
* **To clear it:** quit LisaEm completely, then manually delete the `pram00c0=` and `pram00e0=` lines (or set them
  to zeros) in lisaem.conf. At the next boot the OS sees "the pram is invalid", and loads the PRAM from the snapshot on
  the disk (which we can edit with the `change-boot-id` command).
* **When LisaEm reads and writes the file:** it reads the `pram` lines only at startup (or when you open
  another config from the menu). It writes all 32 `pram` lines (`save_pram()`) whenever the emulated
  Lisa reboots or powers off. So editing the file while LisaEm is running has no effect, and is undone at
  the next reboot.
* **The Zap PRAM button** (Preferences) only zeroes the 64 PRAM bytes in memory; it does not change the
  config file until the next `save_pram()`. Zap and then power on in the same session works too: LisaEm
  does not reload the file at power-on. After that boot the file gets the PRAM that the OS loaded from
  the disk, not zeros.
* **Which config file is used:** a relative path given to `-c` (and the default `lisaconfigfile=./lisaem.conf`
  in `~/.lisaem`) is resolved by wxWidgets' `wxFileConfig` against the home directory, not the current
  folder. So `lisaem -c ./lisaem.conf` uses `~/lisaem.conf`, wherever LisaEm is started from. Use an
  absolute path to make LisaEm use another config file.
