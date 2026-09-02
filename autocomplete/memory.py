"""How much memory this process is using, without a third-party dependency.

The index is memory-mapped by default, so the interesting number is resident
size: how much of it the kernel has actually paged in, which grows as searches
touch more of the suffix array. There is no portable way to read that, so each
platform is handled separately and anything unrecognised reports ``None``
rather than a wrong number.

Peak resident size comes from :func:`resource.getrusage`, which is available
everywhere the program runs and needs no platform code.
"""

from __future__ import annotations

import ctypes
import os
import resource
import sys
from pathlib import Path

__all__ = ["describe", "format_gb", "peak_bytes", "resident_bytes"]

#: Sizes are reported in decimal gigabytes, matching the MB figures main.py
#: already prints for the cache and the corpus.
_BYTES_PER_GB = 1e9

_STATM = Path("/proc/self/statm")


def resident_bytes() -> int | None:
    """Resident set size right now, or ``None`` if it cannot be read here."""
    if sys.platform.startswith("linux"):
        return _resident_linux()
    if sys.platform == "darwin":
        return _resident_darwin()
    return None


def peak_bytes() -> int | None:
    """The largest resident set size reached so far, or ``None``.

    ``ru_maxrss`` is kilobytes on Linux and bytes on macOS. Getting that unit
    wrong is a factor of a thousand, so it is decided by platform rather than
    guessed from the magnitude.
    """
    try:
        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (OSError, ValueError):  # pragma: no cover - not seen in practice
        return None
    if raw <= 0:
        return None
    if sys.platform == "darwin":
        return int(raw)
    return int(raw) * 1024


def format_gb(size: int | None) -> str:
    """Render a byte count in gigabytes, or ``"unknown"`` for ``None``."""
    if size is None:
        return "unknown"
    return f"{size / _BYTES_PER_GB:.2f} GB"


def describe() -> str:
    """One line of memory use, for the summary the command line prints.

    Reads as ``1.24 GB resident (peak 1.31 GB)``, dropping whichever half this
    platform cannot supply instead of printing a placeholder.
    """
    resident = resident_bytes()
    peak = peak_bytes()
    if resident is None and peak is None:
        return "unknown on this platform"
    if resident is None:
        return f"peak {format_gb(peak)}"
    if peak is None:
        return f"{format_gb(resident)} resident"
    return f"{format_gb(resident)} resident (peak {format_gb(peak)})"


def _resident_linux() -> int | None:
    """Read the resident pages from ``/proc/self/statm``.

    The second field is resident pages. Reading one small pseudo-file is cheap
    enough to do after every search.
    """
    try:
        fields = _STATM.read_text(encoding="ascii").split()
        return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


class _ProcTaskInfo(ctypes.Structure):
    """The prefix of macOS ``struct proc_taskinfo`` that holds the sizes.

    The fields after these are timings and counters; they are declared so that
    the structure is the size ``proc_pidinfo`` expects, since it refuses to
    write into a buffer smaller than the flavour's own size.
    """

    _fields_ = [
        ("pti_virtual_size", ctypes.c_uint64),
        ("pti_resident_size", ctypes.c_uint64),
        ("pti_total_user", ctypes.c_uint64),
        ("pti_total_system", ctypes.c_uint64),
        ("pti_threads_user", ctypes.c_uint64),
        ("pti_threads_system", ctypes.c_uint64),
        ("pti_policy", ctypes.c_int32),
        ("pti_faults", ctypes.c_int32),
        ("pti_pageins", ctypes.c_int32),
        ("pti_cow_faults", ctypes.c_int32),
        ("pti_messages_sent", ctypes.c_int32),
        ("pti_messages_received", ctypes.c_int32),
        ("pti_syscalls_mach", ctypes.c_int32),
        ("pti_syscalls_unix", ctypes.c_int32),
        ("pti_csw", ctypes.c_int32),
        ("pti_threadnum", ctypes.c_int32),
        ("pti_numrunning", ctypes.c_int32),
        ("pti_priority", ctypes.c_int32),
    ]


#: ``PROC_PIDTASKINFO`` from ``<libproc.h>``.
_PROC_PIDTASKINFO = 4

_libproc = None


def _resident_darwin() -> int | None:
    """Ask the kernel for this process's task info through ``libproc``.

    macOS has no ``/proc``. ``proc_pidinfo`` lives in libSystem, which is
    already loaded, so this costs a system call rather than a subprocess.
    """
    global _libproc
    if _libproc is None:
        try:
            _libproc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        except OSError:
            return None

    info = _ProcTaskInfo()
    try:
        written = _libproc.proc_pidinfo(
            os.getpid(),
            _PROC_PIDTASKINFO,
            ctypes.c_uint64(0),
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
    except (AttributeError, OSError):
        return None

    # A short write means the structure this was compiled against no longer
    # matches the kernel's, in which case the bytes read are not trustworthy.
    if written != ctypes.sizeof(info):
        return None
    return int(info.pti_resident_size)
