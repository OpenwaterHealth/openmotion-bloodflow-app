"""Names shared by every connector module."""
import logging

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
