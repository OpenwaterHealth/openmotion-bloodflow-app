"""The QML bridge. ``MotionConnector`` is registered in main.py as the
``MotionInterface`` singleton; see core.py for how it is composed."""
from connector.core import MotionConnector

__all__ = ["MotionConnector"]
