"""
GeoInbox — QGIS 4 / Qt6 plugin entry point.

A QGIS plugin for email-based GIS data exchange, field data versioning,
and GISimple server integration.

Requires QGIS 4.x (Qt6). Not compatible with QGIS 3.x or Qt5.
"""

from __future__ import annotations

import os

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction


class GeoInbox:
    """QGIS plugin class — registered via classFactory(iface)."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.menu = "&GeoInbox"
        self.toolbar = None
        self.actions = []

    def initGui(self):
        """Create menu and toolbar — called by QGIS when the plugin is loaded."""
        self.toolbar = self.iface.addToolBar("GeoInbox")
        self.toolbar.setObjectName("GeoInboxToolbar")

        self._geoinbox_action = self._add_action(
            icon_path=os.path.join(self.plugin_dir, "icons", "plugin.png"),
            text="GeoInbox",
            callback=self.open_geoinbox,
            add_to_toolbar=True,
            status_tip="Open the GeoInbox window",
            whats_this="Email-based GIS exchange, versioning, and GISimple tools",
            checkable=True,
        )

    def unload(self):
        """Remove menu, toolbar, and UI — called by QGIS when the plugin is unloaded."""
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            if self.toolbar is not None:
                self.toolbar.removeAction(action)

        if self.toolbar is not None:
            self.toolbar.deleteLater()
            self.toolbar = None

        self.actions.clear()

    def open_geoinbox(self):
        """Launch the main GeoInbox dialog (implemented in main.py)."""
        # Import lazily so plugin load stays light and safe.
        from .main import GeoInboxDialog

        GeoInboxDialog.show_dialog(self._geoinbox_action)

    def _add_action(
        self,
        icon_path,
        text,
        callback,
        enabled_flag=True,
        add_to_menu=True,
        add_to_toolbar=True,
        status_tip=None,
        whats_this=None,
        parent=None,
        checkable=False,
    ):
        if parent is None:
            parent = self.iface.mainWindow()

        icon = QIcon(icon_path) if icon_path else QIcon()
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)
        action.setCheckable(checkable)

        if status_tip is not None:
            action.setStatusTip(status_tip)
        if whats_this is not None:
            action.setWhatsThis(whats_this)

        if add_to_toolbar and self.toolbar is not None:
            self.toolbar.addAction(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)

        self.actions.append(action)
        return action


def classFactory(iface):  # pylint: disable=invalid-name
    """QGIS plugin entry point — required name."""
    return GeoInbox(iface)
