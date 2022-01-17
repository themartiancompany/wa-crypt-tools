#!/usr/bin/env python
"""
This script decrypts WhatsApp's encrypted DB file.
"""

from __future__ import annotations

# noinspection PyPackageRequirements
# This is from pycryptodome

from Crypto.Cipher import AES

from io import DEFAULT_BUFFER_SIZE
from re import findall
from sys import exit

import argparse
import zlib

__author__ = 'TripCode, ElDavo'
__copyright__ = 'Copyright (C) 2022'
__license__ = 'GPLv3'
__status__ = 'Production'
__version__ = '2.1'

# Key file format:
# fixed header (27 bytes)

KEY_HEADER = b'\xac\xed\x00\x05\x75\x72\x00\x02\x5b\x42\xac\xf3\x17\xf8' \
             b'\x06\x08\x54\xe0\x02\x00\x00\x78\x70\x00\x00\x00\x83'
# Dynamic header (Multiple variations), like 00 00 01, 00 01 01, 00 01 02 ...
KEY_DYN_HEADERS = [
    b'\x00\x00\x01',
    b'\x00\x01\x01',
    b'\x00\x01\x02'
]
# t1 (32 bytes)
# random IV (unused) + married key (useless for us) (total: 48 bytes)
# 16 bytes of zeroes (padding)
# key (32 bytes)
# total length = 158 bytes
KEY_LENGTH = 158

# zlib magic header is 78 01 (Low Compression).
# The first two bytes of the decrypted data should be those.
ZIP_HEADERS = [
    b'x\x01'
]

# Size of header (number chosen arbitrarily, but values less than ~310 makes test_decompression fail)
HEADER_SIZE = 512

DEFAULT_DATA_OFFSET = 191
DEFAULT_IV_OFFSET = 67


class Log:
    """Simple logger class. Supports 4 verbosity levels."""

    def __init__(self, verbose: bool, force: bool):
        self.verbose = verbose
        self.force = force

    def v(self, msg: str):
        """Will only print message if verbose mode is enabled."""
        if self.verbose:
            print('[V] {}'.format(msg))

    @staticmethod
    def i(msg: str):
        """Always prints message."""
        print('[I] {}'.format(msg))

    def e(self, msg: str):
        """Prints message and exit, unless force is enabled."""
        print('[E] {}'.format(msg))
        if not self.force:
            print("To bypass checks, use the \"--force\" parameter")
            exit(1)

    @staticmethod
    def f(msg: str):
        """Always prints message and exit."""
        print('[F] {}'.format(msg))
        exit(1)


def oscillate(n: int, n_min: int, n_max: int):
    """Yields n, n-1, n+1, n-2, n+2..., with constraints:
    - n is in [min, max]
    - n is never negative
    Reverts to range() when n touches min or max. Example:
    oscillate(8, 2, 10) => 8, 7, 9, 6, 10, 5, 4, 3, 2
    """

    if n_min < 0:
        n_min = 0

    i = n
    c = 1

    # First phase (n, n-1, n+1...)
    while True:

        if i == n_max:
            break
        yield i
        i = i - c
        c = c + 1

        if i == 0 or i == n_min:
            break
        yield i
        i = i + c
        c = c + 1

    # Second phase (range of remaining numbers)
    # n != i/2 fixes a bug where we would yield min and max two times if n == (max-min)/2
    if i == n_min and n != i / 2:

        yield i
        i = i + c
        for j in range(i, n_max + 1):
            yield j

    if i == n_max and n != i / 2:

        yield n_max
        i = i - c
        for j in range(i, n_min - 1, -1):
            yield j


def parsecmdline():
    """Sets up the argument parser"""
    parser = argparse.ArgumentParser(description='Decrypts WhatsApp encrypted database backup files')
    parser.add_argument('keyfile', nargs='?', type=argparse.FileType('rb', bufsize=KEY_LENGTH), default="key",
                        help='The WhatsApp keyfile. Default: key')
    parser.add_argument('encrypted', nargs='?', type=argparse.FileType('rb'), default="msgstore.db.crypt14",
                        help='The encrypted crypt14 database. Default: msgstore.db.crypt14')
    parser.add_argument('decrypted', nargs='?', type=argparse.FileType('wb'), default="msgstore.db",
                        help='The decrypted output database file. Default: msgstore.db')
    parser.add_argument('-f', '--force', action='store_true', help='Makes errors non fatal.'
                                                                   'Default: false')
    parser.add_argument('-nm', '--no-mem', action='store_true', help='Does not load files in RAM, '
                                                                     'stresses the disk more.'
                                                                     'Default: load files into RAM')
    parser.add_argument('-v', '--verbose', action='store_true', help='Prints all offsets and messages')

    return parser.parse_args()


def get_t1_and_key(key_file_stream) -> tuple[bytes, bytes]:
    """Extracts t1 and key from the keyfile (a file stream)."""

    # Assign variables to suppress warnings
    keyfile: bytes = b''

    log.v("Reading keyfile...")

    try:
        keyfile = key_file_stream.read()
    except OSError as e:
        log.f("Couldn't read keyfile: {}".format(e))

    # Check if the keyfile is big enough
    if len(keyfile) != KEY_LENGTH:
        log.f(
            "Invalid keyfile: Smaller than expected (wanted {} bytes, got {} bytes)".format(KEY_LENGTH, len(keyfile)))

    # Check if the keyfile is small enough
    try:
        if key_file_stream.read(1) != b'':
            log.e("Invalid keyfile: Expected a file of {} bytes, got more.\n\t"
                  "Did you swap the keyfile and the database by mistake?".format(KEY_LENGTH))
    except OSError as e:
        log.f("Couldn't check keyfile size: {}".format(e))
    finally:
        key_file_stream.close()

    # Check if the keyfile has the correct header
    if keyfile[:len(KEY_HEADER)] != KEY_HEADER:
        log.e('Invalid keyfile: Invalid header\n\tExpected:\t{}\n\tGot:\t\t{}'
              .format(KEY_HEADER.hex(), keyfile[:len(KEY_HEADER)].hex()))

    # TODO check the "married key" (whatever that is)

    # Check if the keyfile has the correct dynamic header
    padding_found: bool = False
    for p in KEY_DYN_HEADERS:
        if p == keyfile[len(KEY_HEADER):len(KEY_HEADER) + len(KEY_DYN_HEADERS[0])]:
            padding_found = True
            break

    if not padding_found:
        log.e('Invalid keyfile: Invalid dynamic header {}'
              .format(keyfile[len(KEY_HEADER):len(KEY_HEADER) + len(KEY_DYN_HEADERS[0])].hex()))
        for p in KEY_DYN_HEADERS:
            print('\t{}'.format(p.hex()))

    t1 = keyfile[30:62]

    padding = keyfile[110:126]

    # Check if the padding is correct
    for byte in padding:
        if byte:
            log.e("Invalid keyfile: Padding is not padding: {}".format(padding.hex()))
            break

    key = keyfile[126:]

    log.v("Keyfile loaded")

    return t1, key


def test_decompression(test_data: bytes) -> bool:
    """Returns true if the SQLite header is valid.
    It is assumed that the data are valid.
    (If it is valid, it also means the decryption and decompression were successful.)"""

    try:
        zlib_obj = zlib.decompressobj().decompress(test_data)
        # These two errors should never happen
        if len(zlib_obj) < 16:
            log.e("Test decompression: chunk too small")
            return False
        if zlib_obj[:15].decode('ascii') != 'SQLite format 3':
            log.e("Test decompression: Decryption and decompression ok but not a valid SQLite database")
            return log.force
        else:
            return True
    except zlib.error:
        return False


def find_data_offset(header: bytes, iv_offset: int, key: bytes) -> int:
    """Tries to find the offset in which the encrypted data starts.
    Returns the offset or -1 if the offset is not found."""

    iv = header[iv_offset:iv_offset + 16]

    # oscillate ensures we try the closest values to the default value first.
    for i in oscillate(n=DEFAULT_DATA_OFFSET, n_min=iv_offset + len(iv), n_max=HEADER_SIZE - 128):

        cipher = AES.new(key, AES.MODE_GCM, iv)

        # We only decrypt the first two bytes.
        test_bytes = cipher.decrypt(header[i:i + 2])

        for zheader in ZIP_HEADERS:

            if test_bytes == zheader:
                # We found a match, but this might also happen by chance.
                # Let's run another test by decrypting some hundreds of bytes.
                # We need to reinitialize the cipher everytime as it has an internal status.
                cipher = AES.new(key, AES.MODE_GCM, iv)
                decrypted = cipher.decrypt(header[i:])
                if test_decompression(decrypted):
                    return i

    return -1


def decrypt14(t1: bytes, key: bytes, encrypted, decrypted, mem_approach: bool):
    """Decrypts an encrypted database file, given t1 and the key."""

    # Assign variables to suppress warnings
    db_header, offset, iv_offset = None, None, None

    log.v("Parsing database header...")

    try:
        db_header = encrypted.read(HEADER_SIZE)
    except OSError as e:
        log.f("Reading encrypted database failed: {}".format(e))

    if len(db_header) < HEADER_SIZE:
        log.f("The encrypted database is too small.\n\t"
              "Did you swap the keyfile and the encrypted database file by mistake?")

    try:
        if db_header[:15].decode('ascii') == 'SQLite format 3':
            log.e("The database file is not encrypted.\n\t"
                  "Did you swap the input and the output files by mistake?")
    except ValueError:
        pass

    result = db_header.find(t1)
    if result == -1:
        log.e("t1 not found in header of crypt14 file.\n\t"
              "This probably means the key does not match the encrypted database.")
    else:
        log.v("t1 found at offset {}".format(result))

    # Finding WhatsApp's version is cool and is another confirmation that the encrypted database is correct
    result = findall(b"\\d(?:\\.\\d{1,3}){3}", db_header)
    if len(result) != 1:
        log.e('WhatsApp version not found')
    else:
        log.v("WhatsApp version: {}".format(result[0].decode()))

    # Determine IV offset and data offset.
    for iv_offset in oscillate(n=DEFAULT_IV_OFFSET, n_min=0, n_max=HEADER_SIZE - 128):
        offset = find_data_offset(db_header, iv_offset, key)
        if offset != -1:
            log.v("IV offset: {}".format(iv_offset))
            log.v("Data offset: {}".format(offset))
            break
    if offset == -1:
        log.f("Could not find IV or data start offset")

    # Now that we have everything we can do the real job
    iv = db_header[iv_offset:iv_offset + 16]
    cipher = AES.new(key, AES.MODE_GCM, iv)
    encrypted.seek(offset)

    z_obj = zlib.decompressobj()

    log.v("Offsets found, decrypting...")

    try:

        if mem_approach:
            # Load the encrypted file into RAM
            # Decrypts into RAM
            # Decompresses into RAM
            # Writes into disk
            # More RAM used (x3), less I/O used
            output_file = z_obj.decompress((cipher.decrypt(encrypted.read())))
            if not z_obj.eof:
                log.e("The encrypted database file is truncated (damaged).")
            decrypted.write(output_file)

        else:
            # Does the thing above but only with DEFAULT_BUFFER_SIZE bytes at a time.
            # Less RAM used, more I/O used
            # TODO use assignment expression, which drops compatibility with 3.7
            # while chunk := encrypted.read(DEFAULT_BUFFER_SIZE):
            while True:
                chunk = encrypted.read(DEFAULT_BUFFER_SIZE)
                if not chunk:
                    break
                decrypted.write(z_obj.decompress(cipher.decrypt(chunk)))
            if not z_obj.eof:
                if not log.force:
                    decrypted.truncate(0)
                log.e("The encrypted database file is truncated (damaged).")

    except OSError as e:
        log.f("I/O error: {}".format(e))

    finally:
        decrypted.close()
        encrypted.close()

    log.i("Decryption successful")



def main():
    args = parsecmdline()
    global log
    log = Log(verbose=args.verbose, force=args.force)
    t1, key = get_t1_and_key(key_file_stream=args.keyfile)
    decrypt14(t1=t1, key=key, encrypted=args.encrypted, decrypted=args.decrypted, mem_approach=not args.no_mem)


if __name__ == "__main__":
    main()
