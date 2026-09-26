#!/usr/bin/env python3
###################################################################################################
# LisaSerialNumberTool.py is a standalone Python tool for decoding Apple Lisa serial numbers,     #
# as found in the LisaEm emulator. Read below for more info.                                      #
# Usage: python LisaSerialNumberTool.py <serial number>                                           #
# Example: python LisaSerialNumberTool.py FF000000000000FF0010000100205D2C                        #
#                                                                                                 #
# Author: TorZidan                                                                                #
# Date: Sept 25, 2026                                                                             #
# License: Published under the GNU General Public License v3.0.                                   #
###################################################################################################

import sys


def decode_16_byte_serial(serial_str: str):
    """Decode an Apple Lisa VSROM (Video state ROM) serial number given as a 32-character hex string (e.g. "ff000000000000ff0000000000000000") (also known as 32 "nibbles") (which represents 16 bytes of data).

    This is the format that the LisaEm emulator uses (In the File->Preferences menu).

    In reality, this 16-byte code is stored as 32 bytes in the Apple Lisa Lisa VSROM (Video state ROM) at address 0x240. Here is how:
    each "nibble" from the serial_str (e.g. "F") appears in the ROM as a separate byte (e.g. 0x0F). So the 32 "nibbles" become 32 bytes in the VSROM.

    Here's how to retrieve the serial number encoded into your Lisa's Video ROM:
    Put your Lisa in Service Mode: When the memory is being tested, press the shift key.
    Then, try to boot up from a blank floppy drive (with no floppy disk inserted), the Lisa will respond with an error.
    Then type Apple-S and you'll get to service mode.
    Click on "Display Mem". When prompted for "Address?", type 240, and click enter. When prompted for "Count?", type 20. You will then receive two lines of code e.g.
    00000240: 0F0F 0002 0803 0008 0100 0400 0500 0F0F
    00000250: 0000 0100 0106 0305 0004 0700 0000 0000
    , which gives us 32 bytes. Notice that each byte starts with "0", so you can compress these "in your brain" into 16 bytes : FF028308104050FF0010163504700000.
    Finally, If you feed this string to this function, you will get what you were looking for :)

    As we can see from the code, the serial number contains a checksum. Interesting.
    Trying random serial numbers in LisaEm reveals that the boot ROM does check the serial, and gives a CPU error 42 if it's checksum is invalid,
    but you can still click on the "Continue" button, and it will let you continue to the boot menu.
    The tool checks the serial's checksum and if it does not match it prints a "corrected" serial whose checksum matches. How cool is that?
    """
    serial_str = serial_str.strip().lower()
    if (
        not serial_str
        or len(serial_str) != 32
        or not all(c in "0123456789abcdef" for c in serial_str)
    ):
        raise ValueError(
            f"invalid serial number {serial_str!r}: expected exactly 32 hex characters, e.g. ff000000000000ff0000000000000000"
        )

    # The 32 bytes at offset 240 in the Lisa VSROM (one byte per nibble):
    # We take each "nibble" from the serial_str (e.g. "F") and covert it to a byte (e.g. 0x0F). So the 32 "nibbles" become 32 bytes.
    serial_as_32_bytes: bytes = bytes(int(c, 16) for c in serial_str)
    if len(serial_as_32_bytes) != 32:
        # Just a sanity check.
        raise ValueError(
            f"serial_as_32_bytes must be 32 bytes, got {len(serial_as_32_bytes)}"
        )

    plant: int = ((serial_as_32_bytes[0x02] & 0x0F) << 4) | (
        serial_as_32_bytes[0x03] & 0x0F
    )
    year: int = ((serial_as_32_bytes[0x04] & 0x0F) << 4) | (
        serial_as_32_bytes[0x05] & 0x0F
    )
    day: int = (
        ((serial_as_32_bytes[0x06] & 0x0F) << 8)
        | ((serial_as_32_bytes[0x07] & 0x0F) << 4)
        | (serial_as_32_bytes[0x08] & 0x0F)
    )

    # Serial number possible range: 0..65535 (0xFFFF)
    serial_number: int = (
        ((serial_as_32_bytes[0x09] & 0x0F) << 12)
        | ((serial_as_32_bytes[0x0A] & 0x0F) << 8)
        | ((serial_as_32_bytes[0x0B] & 0x0F) << 4)
        | (serial_as_32_bytes[0x0C] & 0x0F)
    )

    # Applenet number:
    prefix: int = (
        ((serial_as_32_bytes[0x10] & 0x0F) << 8)
        | ((serial_as_32_bytes[0x11] & 0x0F) << 4)
        | (serial_as_32_bytes[0x12] & 0x0F)
    )
    net: int = (
        ((serial_as_32_bytes[0x13] & 0x0F) << 16)
        | ((serial_as_32_bytes[0x14] & 0x0F) << 12)
        | ((serial_as_32_bytes[0x15] & 0x0F) << 8)
        | ((serial_as_32_bytes[0x16] & 0x0F) << 4)
        | (serial_as_32_bytes[0x17] & 0x0F)
    )
    # the 8-digit BCD AppleNet serial number: 3-digit prefix + 5-digit number
    applenet: str = "".join(f"{v}" for v in serial_as_32_bytes[0x10:0x18])
    # machine_id = first3 * 65536 + last5 (see source-SERNUM.TEXT.unix.txt)
    machine_id: int = int(applenet[:3]) * 65536 + int(applenet[3:])

    # Formatted in 2-byte chunks, e.g. "0F0F 0002 0803 0008 0100 0400 0500 0F0F"
    serial_at_address_240: str = " ".join(
        f"{serial_as_32_bytes[i]:02X}{serial_as_32_bytes[i + 1]:02X}"
        for i in range(0, 16, 2)
    )
    serial_at_address_250: str = " ".join(
        f"{serial_as_32_bytes[i]:02X}{serial_as_32_bytes[i + 1]:02X}"
        for i in range(16, 32, 2)
    )

    # Checksum, replicated from the SERNUM routine (CHKSUM) in
    # source-SERNUM.TEXT.unix.txt. The video bitstream carries 24 data nibbles:
    # VSROM[0x02:0x0e] (SN1) and VSROM[0x10:0x18] (SN2); the two 2-nibble 0xff
    # sync bytes at VSROM[0x00:0x02] and VSROM[0x0e:0x10] are skipped. The
    # checksum is stored in BCD in the last three data nibbles (VSROM[0x18..0x1a])
    # and must equal the sum of the first 20 data nibbles plus the 24th
    # (VSROM[0x1b]).
    stored_checksum = (
        serial_as_32_bytes[0x18] * 100
        + serial_as_32_bytes[0x19] * 10
        + serial_as_32_bytes[0x1A]
    )
    computed_checksum = (
        sum(serial_as_32_bytes[0x02:0x0E])
        + sum(serial_as_32_bytes[0x10:0x18])
        + serial_as_32_bytes[0x1B]
    )
    checksum_ok = stored_checksum == computed_checksum

    corrected_serial_str: str = ""
    if not checksum_ok:
        # Generate a "corrected" serial number whose checksum passes: rewrite the
        # 3 BCD checksum nibbles (VSROM[0x18..0x1a]) with the BCD digits of the
        # computed checksum. The computed checksum is at most 315 (21 nibbles of
        # at most 15 each), so it always fits in 3 BCD digits, and the checksum
        # nibbles themselves are not part of the computed sum.
        corrected: list[str] = list(serial_str)
        corrected[0x18] = str(computed_checksum // 100)
        corrected[0x19] = str((computed_checksum // 10) % 10)
        corrected[0x1A] = str(computed_checksum % 10)
        corrected_serial_str = "".join(corrected)

    checksum_report = (
        f"Checksum: stored={stored_checksum} calculated={computed_checksum} -> "
        f"{'OK' if checksum_ok else 'INVALID'}"
    )
    if corrected_serial_str:
        checksum_report += (
            f"\nCorrected serial number (checksum now passes): {corrected_serial_str}"
        )

    return (
        "\nIf you were to print this serial number on a Lisa in service mode, it would look like this:\n"
        f"00000240: {serial_at_address_240}\n"
        f"00000250: {serial_at_address_250}\n\n"
        # Note: we print these values in hex format below, and yey they come out just right:
        f"Decoded: Your Lisa was built in Apple Plant #{plant:x} on the {day:x} day of 19{year:x} with serial #{serial_number:04x}\n\n"
        f"AppleNet Number: {prefix:03x}-{net:05x} (machine_id {machine_id} / {machine_id:#010x})\n\n"
        f"{checksum_report}"
    )


def main():
    """CLI entry point: decode the serial number given as the first command-line argument."""
    if len(sys.argv) != 2:
        print("Usage: python LisaSerialNumberTool.py <Apple Lisa serial number>")
        print(
            "The serial number is the 'compressed' 16-byte (32 hex character), as used by the LisaEm emulator, e.g. ff000000000000ff0000000000000000. See more info in the code."
        )
        sys.exit(1)

    try:
        print(decode_16_byte_serial(sys.argv[1]))
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
