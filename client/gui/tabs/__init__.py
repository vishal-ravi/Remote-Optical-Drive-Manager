"""Tab widgets for the Remote Optical Drive Manager client."""

from .browse_tab import BrowseTab
from .burn_tab import BurnTab
from .image_tab import ImageTab
from .log_tab import LogTab, QtLogHandler
from .queue_tab import QueueTab

__all__ = ["BrowseTab", "BurnTab", "ImageTab", "LogTab", "QtLogHandler", "QueueTab"]
