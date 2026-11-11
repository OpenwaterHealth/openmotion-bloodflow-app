"""Names shared by every connector module."""
import contextlib
import logging
import threading

logger = logging.getLogger("openmotion.bloodflow-app.connector")

# The SDK's stable device handles, by MotionInterface attribute name.
DEVICES = ("console", "left", "right")
SIDES = ("left", "right")

# System states (the QML ``state`` property).
DISCONNECTED = 0
SENSOR_CONNECTED = 1
CONSOLE_CONNECTED = 2
READY = 3
RUNNING = 4


def hms(seconds: float) -> str:
    """Whole seconds as HH:MM:SS."""
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def spawn(target, *args, name=None) -> None:
    """Run ``target(*args)`` on a daemon thread."""
    kwargs = {"name": name} if name else {}
    threading.Thread(target=target, args=args, daemon=True, **kwargs).start()


@contextlib.contextmanager
def scan_db(interface):
    """Open the scan DB for one block. Yields None when the interface has
    no ``scan_db_path``; the caller handles whatever the block raises."""
    path = getattr(interface, "scan_db_path", None)
    if not path:
        yield None
        return
    from omotion.ScanDatabase import ScanDatabase
    db = ScanDatabase(path)
    try:
        yield db
    finally:
        db.close()
