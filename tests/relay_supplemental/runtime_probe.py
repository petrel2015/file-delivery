"""Controller-only read boundary instrumentation. Does not implement delivery.

Inject one file change after the first nonempty read. Supports builtins/pathlib,
readinto/file_digest and descriptor reads without requiring a business API or a
hashing algorithm implementation. Unsupported readers produce a harness error,
not evidence of an AC failure. This is not an OS sandbox.
"""
import argparse
import builtins
import io
import json
import os
from pathlib import Path
import runpy
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--mode', choices=('unchanged', 'grow', 'same-size', 'no-network'), required=True)
    parser.add_argument('--self-reader', choices=('open', 'pathlib', 'readinto', 'file-digest', 'descriptor', 'fileio'))
    args = parser.parse_args()
    target = args.input.resolve()
    initial = target.stat()
    identity = (initial.st_dev, initial.st_ino)
    original_open, original_io_open = builtins.open, io.open
    original_read, original_fileio = os.read, io.FileIO
    report = {'read_observed': False, 'mutation_performed': False, 'network_events': []}

    def is_target(handle):
        try:
            fd = handle if isinstance(handle, int) else handle.fileno()
            st = os.fstat(fd)
            return (st.st_dev, st.st_ino) == identity
        except (AttributeError, OSError, ValueError):
            return False

    def after_read(handle, nonempty):
        if not nonempty or not is_target(handle):
            return
        report['read_observed'] = True
        if args.mode not in ('grow', 'same-size') or report['mutation_performed']:
            return
        # Mutate the same inode, outside the application's control. No sleeps,
        # polling, worker threads, or assumptions about hash chunk sizes.
        report['mutation_performed'] = True
        with original_open(target, 'r+b') as writer:
            if args.mode == 'grow':
                writer.seek(0, os.SEEK_END)
                writer.write(b'controller-change')
            else:
                first = writer.read(1)
                writer.seek(0)
                writer.write(bytes([first[0] ^ 0x01]))
            writer.flush()
            os.fsync(writer.fileno())
        os.utime(target, ns=(initial.st_atime_ns, initial.st_mtime_ns + 2_000_000_000))

    class Reader:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *exc):
            return self.wrapped.__exit__(*exc)

        def __iter__(self):
            return self

        def __next__(self):
            value = next(self.wrapped)
            after_read(self.wrapped, bool(value))
            return value

        def read(self, *a, **kw):
            value = self.wrapped.read(*a, **kw)
            after_read(self.wrapped, bool(value))
            return value

        def readinto(self, *a, **kw):
            value = self.wrapped.readinto(*a, **kw)
            after_read(self.wrapped, bool(value))
            return value

        def read1(self, *a, **kw):
            value = self.wrapped.read1(*a, **kw)
            after_read(self.wrapped, bool(value))
            return value

        def readinto1(self, *a, **kw):
            value = self.wrapped.readinto1(*a, **kw)
            after_read(self.wrapped, bool(value))
            return value

        def readline(self, *a, **kw):
            value = self.wrapped.readline(*a, **kw)
            after_read(self.wrapped, bool(value))
            return value

    def wrap_opener(opener):
        def open_read(*a, **kw):
            handle = opener(*a, **kw)
            return Reader(handle) if is_target(handle) else handle
        return open_read

    def read_fd(fd, *a, **kw):
        value = original_read(fd, *a, **kw)
        after_read(fd, bool(value))
        return value

    class ObservedFileIO(original_fileio):
        def read(self, *a, **kw):
            value = super().read(*a, **kw)
            after_read(self, bool(value))
            return value

        def readinto(self, *a, **kw):
            value = super().readinto(*a, **kw)
            after_read(self, bool(value))
            return value

    builtins.open = wrap_opener(original_open)
    io.open = wrap_opener(original_io_open)
    io.FileIO = ObservedFileIO
    os.read = read_fd

    # Observe/block network operations, including DNS, even when the application
    # catches the exception. Merely constructing a socket is not a violation.
    network_events = {'socket.connect', 'socket.connect_ex', 'socket.bind',
                      'socket.sendto', 'socket.sendmsg', 'socket.getaddrinfo',
                      'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.getnameinfo'}

    def audit(event, unused_args):
        if event in network_events:
            report['network_events'].append(event)
            raise RuntimeError('CONTROLLER_NETWORK_FORBIDDEN')
    sys.addaudithook(audit)
    try:
        if args.self_reader:
            if args.self_reader == 'pathlib':
                target.read_bytes()
            elif args.self_reader == 'descriptor':
                fd = os.open(target, os.O_RDONLY)
                try:
                    os.read(fd, 4096)
                finally:
                    os.close(fd)
            elif args.self_reader == 'fileio':
                with io.FileIO(target, 'rb') as reader:
                    reader.read(4096)
            else:
                with open(target, 'rb') as reader:
                    if args.self_reader == 'readinto':
                        reader.readinto(bytearray(4096))
                    elif args.self_reader == 'file-digest':
                        import hashlib
                        hashlib.file_digest(reader, 'sha256')
                    else:
                        reader.read()
        else:
            sys.path.insert(0, str(args.project / 'src'))
            sys.argv = ['file-delivery', 'plan', str(target), '--root', str(target.parent), '--json']
            runpy.run_module('file_delivery', run_name='__main__')
    finally:
        with original_open(args.report, 'w', encoding='utf-8') as handle:
            json.dump(report, handle)


if __name__ == '__main__':
    main()
