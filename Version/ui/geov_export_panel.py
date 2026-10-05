"""Backward-compatible import; use ui.geov_panel.GeovPanel."""

from .geov_panel import GeovDropZone, GeovPanel

__all__ = ["GeovDropZone", "GeovPanel", "GeovExportPanel"]

GeovExportPanel = GeovPanel
