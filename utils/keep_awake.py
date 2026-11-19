"""Keep the PC from sleeping while a firmware update runs (#737).

A bare-metal console or sensor erases its only boot image at the start of a
firmware update and cannot get back into DFU by itself. If the PC sleeps
mid-write, USB stops and the unit is left with incomplete firmware. Then the
operator's natural next step, a power cycle, bricks it.

Windows only: ``SetThreadExecutionState`` with ``ES_CONTINUOUS`` holds the
request for the calling thread until it is cleared or the thread exits, so
use it on the thread that does the work. It blocks idle sleep and display
timeout. It does not stop a sleep the user asks for (Start > Sleep, closing
the lid). On other platforms this is a no-op.
"""
from __future__ import annotations

import contextlib
import ctypes
import logging
import sys
from typing import Iterator

logger = logging.getLogger("openmotion.bloodflow-app.keep-awake")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def _set_thread_execution_state(flags: int) -> int:
    """The Win32 call; returns the previous state, 0 on failure."""
    fn = ctypes.windll.kernel32.SetThreadExecutionState
    fn.argtypes = [ctypes.c_uint32]
    fn.restype = ctypes.c_uint32
    return fn(flags)


@contextlib.contextmanager
def keep_system_awake(reason: str = "") -> Iterator[bool]:
    """Hold off idle sleep (and display sleep, so the progress stays on
    screen) for the calling thread while the block runs. Yields True if the
    request was accepted; never raises over it."""
    if sys.platform != "win32":
        yield False
        return
    try:
        held = _set_thread_execution_state(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED) != 0
    except Exception:
        logger.warning("Could not ask Windows to stay awake%s",
                       f" ({reason})" if reason else "", exc_info=True)
        held = False
    if held:
        logger.info("Keeping the PC awake%s", f": {reason}" if reason else "")
    try:
        yield held
    finally:
        if held:
            try:
                _set_thread_execution_state(ES_CONTINUOUS)
            except Exception:
                logger.warning("Could not release the stay-awake request",
                               exc_info=True)
