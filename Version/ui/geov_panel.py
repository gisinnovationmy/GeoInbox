"""Geov Format tab: drag-and-drop import and export to Geov."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable, List, Optional

from qgis.core import QgsMapLayerType, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QDragEnterEvent, QDropEvent, QGuiApplication, QKeySequence
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QCheckBox,
    QLineEdit,
    QMenu,
)
from qgis.utils import iface

from ..export.geov_export import (
    GEOV_REQUIRES_VECTOR_MESSAGE,
    build_geov_zip_bytes,
    geov_download_file_name,
)
from ..export.geov_import import import_geov_file
from ..utils.download_folder import get_download_folder
from ..utils.mime_drop import mime_might_contain_geov, resolve_geov_paths_from_mime


class GeovDropZone(QFrame):
    """Drop target for .geov files from email, WhatsApp, Explorer, etc."""

    geov_dropped = pyqtSignal(str)
    geov_paste_failed = pyqtSignal()

    def __init__(
        self,
        scratch_dir_resolver: Optional[Callable[[], Optional[Path]]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._scratch_dir_resolver = scratch_dir_resolver
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumHeight(120)
        self.setStyleSheet(
            """
            GeovDropZone {
                border: 2px dashed #74c0fc;
                border-radius: 8px;
                background: #f8f9fa;
            }
            GeovDropZone[dragActive="true"] {
                border-color: #1864ab;
                background: #e7f5ff;
            }
            """
        )
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("Drop Geov Format file here")
        title.setStyleSheet("font-size: 15px; font-weight: bold; color: #1864ab;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        hint = QLabel(
            "Drag a <b>.geov</b> file here, or copy it elsewhere and "
            "<b>right-click → Paste</b> (or focus this box and press <b>Ctrl+V</b>). "
            "Works well when WhatsApp or other apps do not support drag-and-drop. "
            "GeoInbox saves to your download folder and loads layers with symbology."
        )
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color: #495057; padding: 0 16px;")
        layout.addWidget(hint)
        self._status = QLabel("")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet("color: #868e96; font-size: 11px;")
        layout.addWidget(self._status)

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        paste_action = menu.addAction("Paste Geov Format file")
        paste_action.setShortcut(QKeySequence.StandardKey.Paste)
        chosen = menu.exec(event.globalPos())
        if chosen == paste_action:
            self.paste_from_clipboard()

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Paste):
            self.paste_from_clipboard()
            event.accept()
            return
        super().keyPressEvent(event)

    def paste_from_clipboard(self) -> bool:
        """Import from system clipboard (Explorer copy, Outlook, etc.)."""
        mime = QGuiApplication.clipboard().mimeData()
        paths = self._resolve_drop_paths(mime)
        if paths:
            self.geov_dropped.emit(str(paths[0]))
            return True
        self.geov_paste_failed.emit()
        return False

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if mime_might_contain_geov(event.mimeData()):
            self.setProperty("dragActive", True)
            self.style().unpolish(self)
            self.style().polish(self)
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self.setProperty("dragActive", False)
        self.style().unpolish(self)
        self.style().polish(self)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self.setProperty("dragActive", False)
        self.style().unpolish(self)
        self.style().polish(self)
        paths = self._resolve_drop_paths(event.mimeData())
        if paths:
            self.geov_dropped.emit(str(paths[0]))
            event.acceptProposedAction()
        else:
            event.ignore()

    def _resolve_drop_paths(self, mime) -> List[Path]:
        scratch = self._scratch_dir()
        return resolve_geov_paths_from_mime(mime, scratch)

    def _scratch_dir(self) -> Optional[Path]:
        if not self._scratch_dir_resolver:
            return None
        base = self._scratch_dir_resolver()
        if not base:
            return None
        return Path(base) / "geov" / "_drop_scratch"


class GeovPanel(QWidget):
    """Import (drop) and export Geov Format packages."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        resolve_download_folder: Optional[Callable[[], Optional[Path]]] = None,
    ):
        super().__init__(parent)
        self._resolve_download_folder = resolve_download_folder
        self.setAcceptDrops(True)
        self._setup_ui()
        self.refresh_layer_list()

    def _download_folder(self) -> Optional[Path]:
        if self._resolve_download_folder:
            folder = self._resolve_download_folder()
            if folder:
                return Path(folder)
        return get_download_folder()

    def _setup_ui(self):
        grid = 8
        layout = QVBoxLayout(self)
        layout.setContentsMargins(grid, grid, grid, grid)
        layout.setSpacing(grid * 2)

        header = QLabel("Geov Format")
        header.setStyleSheet("font-size: 18px; font-weight: bold; color: #1864ab;")
        layout.addWidget(header)

        import_group = QGroupBox("Import Geov Format")
        import_layout = QVBoxLayout(import_group)
        self._drop_zone = GeovDropZone(scratch_dir_resolver=self._download_folder)
        self._drop_zone.geov_dropped.connect(self._import_geov_path)
        self._drop_zone.geov_paste_failed.connect(self._on_geov_paste_failed)
        import_layout.addWidget(self._drop_zone)

        import_btn_row = QHBoxLayout()
        browse_import = QPushButton("Browse for .geov…")
        browse_import.clicked.connect(self._browse_import_geov)
        import_btn_row.addWidget(browse_import)
        paste_btn = QPushButton("Paste from clipboard")
        paste_btn.setToolTip("Same as right-click Paste on the box above (Ctrl+V)")
        paste_btn.clicked.connect(self._drop_zone.paste_from_clipboard)
        import_btn_row.addWidget(paste_btn)
        import_btn_row.addStretch()
        import_layout.addLayout(import_btn_row)

        self._import_folder_label = QLabel("Download folder: (not set in Settings)")
        self._import_folder_label.setStyleSheet("color: #868e96; font-size: 11px;")
        self._import_folder_label.setWordWrap(True)
        import_layout.addWidget(self._import_folder_label)
        layout.addWidget(import_group)

        export_group = QGroupBox("Export to Geov")
        export_layout = QVBoxLayout(export_group)
        export_intro = QLabel(
            "Save the current map view as Geov Format (MiniGISimple-compatible ZIP)."
        )
        export_intro.setWordWrap(True)
        export_intro.setStyleSheet("color: #495057;")
        export_layout.addWidget(export_intro)

        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Project name:"))
        self._project_name = QLineEdit()
        self._project_name.setPlaceholderText("Project name")
        self._project_name.setText(f"GeoInbox {date.today().isoformat()}")
        name_layout.addWidget(self._project_name)
        export_layout.addLayout(name_layout)

        self._visible_only = QCheckBox("Only layers checked visible in the layer tree")
        self._visible_only.setChecked(True)
        export_layout.addWidget(self._visible_only)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh layers")
        refresh_btn.clicked.connect(self.refresh_layer_list)
        btn_row.addWidget(refresh_btn)
        select_all_btn = QPushButton("Select all")
        select_all_btn.clicked.connect(self._select_all_layers)
        btn_row.addWidget(select_all_btn)
        btn_row.addStretch()
        export_layout.addLayout(btn_row)

        self._layer_list = QListWidget()
        self._layer_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._layer_list.setMaximumHeight(160)
        export_layout.addWidget(self._layer_list)

        self._extent_label = QLabel("Map extent: (open a map view)")
        self._extent_label.setStyleSheet("color: #868e96; font-size: 11px;")
        export_layout.addWidget(self._extent_label)

        export_btn = QPushButton("Export to Geov…")
        export_btn.clicked.connect(self._export_geov)
        export_layout.addWidget(export_btn)
        layout.addWidget(export_group)

        layout.addStretch()
        self._refresh_folder_hint()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_layer_list()
        self._update_extent_hint()
        self._refresh_folder_hint()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if mime_might_contain_geov(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = resolve_geov_paths_from_mime(
            event.mimeData(), self._drop_zone._scratch_dir()
        )
        if paths:
            self._import_geov_path(str(paths[0]))
            event.acceptProposedAction()
        else:
            QMessageBox.warning(
                self,
                "Import Geov Format",
                "Could not read a Geov Format file from this drop.\n\n"
                "Outlook: drag the attachment itself (not the message). "
                "The file should be a .geov ZIP package.",
            )
            event.ignore()

    def _refresh_folder_hint(self) -> None:
        folder = self._download_folder()
        if folder:
            self._import_folder_label.setText(f"Download folder: {folder}")
        else:
            self._import_folder_label.setText(
                "Download folder: set in Settings → Storage (required for import)."
            )

    def _on_geov_paste_failed(self) -> None:
        QMessageBox.warning(
            self,
            "Import Geov Format",
            "The clipboard does not contain a Geov Format file.\n\n"
            "• In File Explorer: click the .geov file and press Ctrl+C, then paste here.\n"
            "• From WhatsApp: use “Save as…” first, then copy the saved file in Explorer.\n"
            "• Outlook: copy the attachment, or save it and copy from Explorer.",
        )

    def _browse_import_geov(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Geov Format",
            "",
            "Geov Format (*.geov);;ZIP archive (*.zip)",
        )
        if path:
            self._import_geov_path(path)

    def _import_geov_path(self, path: str) -> None:
        folder = self._download_folder()
        if not folder:
            QMessageBox.warning(
                self,
                "Import Geov Format",
                "Set a download folder in Settings before importing Geov Format files.",
            )
            return

        self._drop_zone.set_status("Importing…")
        try:
            result = import_geov_file(Path(path), folder)
        except Exception as exc:  # noqa: BLE001
            self._drop_zone.set_status("")
            QMessageBox.critical(self, "Import Geov Format", f"Import failed:\n{exc}")
            return

        self._drop_zone.set_status(
            f"Imported {result.layer_count} layer(s) → {result.extract_dir}"
        )
        if iface and iface.messageBar():
            iface.messageBar().pushSuccess(
                "GeoInbox",
                f"Geov Format: {result.layer_count} layer(s) from “{result.project_name}”",
            )

    def _select_all_layers(self):
        self._layer_list.selectAll()

    def refresh_layer_list(self):
        self._layer_list.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if layer.type() != QgsMapLayerType.VectorLayer:
                continue
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                continue
            item = QListWidgetItem(layer.name())
            item.setData(Qt.ItemDataRole.UserRole, layer.id())
            self._layer_list.addItem(item)
            item.setSelected(True)
        self._update_extent_hint()

    def _update_extent_hint(self):
        if not iface:
            return
        canvas = iface.mapCanvas()
        ext = canvas.extent()
        crs = canvas.mapSettings().destinationCrs().authid()
        self._extent_label.setText(
            f"Map extent ({crs}): "
            f"{ext.xMinimum():.4f}, {ext.yMinimum():.4f} → "
            f"{ext.xMaximum():.4f}, {ext.yMaximum():.4f}"
        )

    def _selected_layer_ids(self) -> List[str]:
        ids: List[str] = []
        for item in self._layer_list.selectedItems():
            lid = item.data(Qt.ItemDataRole.UserRole)
            if lid:
                ids.append(lid)
        return ids

    def _export_geov(self):
        if not iface:
            QMessageBox.warning(self, "Export to Geov", "QGIS map interface is not available.")
            return

        layer_ids = self._selected_layer_ids()
        if not layer_ids:
            QMessageBox.warning(self, "Export to Geov", "Select at least one vector layer.")
            return

        project_name = self._project_name.text().strip() or "GeoV export"
        suggested = geov_download_file_name(project_name)

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export to Geov",
            suggested,
            "Geov Format (*.geov);;ZIP archive (*.zip)",
        )
        if not path:
            return
        if not path.lower().endswith(".geov"):
            path = f"{path}.geov"

        canvas = iface.mapCanvas()
        extent = canvas.extent()
        map_crs = canvas.mapSettings().destinationCrs()

        try:
            data = build_geov_zip_bytes(
                project_name,
                extent,
                map_crs,
                layer_ids=layer_ids,
                visible_layers_only=self._visible_only.isChecked(),
            )
        except ValueError as exc:
            msg = str(exc) or GEOV_REQUIRES_VECTOR_MESSAGE
            QMessageBox.warning(self, "Export to Geov", msg)
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export to Geov", f"Export failed:\n{exc}")
            return

        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError as exc:
            QMessageBox.critical(self, "Export to Geov", f"Could not write file:\n{exc}")
            return

        import json
        import zipfile

        with zipfile.ZipFile(path, "r") as zf:
            desc = json.loads(zf.read("project.json"))
            layer_count = len(desc.get("layers", []))

        if iface.messageBar():
            iface.messageBar().pushSuccess(
                "GeoInbox",
                f"Geov Format saved ({layer_count} layer(s)): {path}",
            )
        QMessageBox.information(
            self,
            "Export to Geov",
            f"Saved {layer_count} layer(s) in Geov Format to:\n{path}",
        )
