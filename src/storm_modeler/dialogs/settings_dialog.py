"""Settings dialog — live-filtered, type-aware editing of every tunable.

Layout (top to bottom):

* a search box pinned at the top that filters the list live as you type
  (case-insensitive over label / key / description) via a
  :class:`QSortFilterProxyModel`;
* a scrolling :class:`QTableView` — column 1 the label, column 2 a type-aware
  editable value (spin box for int/float with min/max, checkbox for bool, combo
  for choices, line edit for str);
* a "Save and Reprocess" button pinned at the bottom that validates against
  min/max/choices, writes only changed keys to ``app_settings``, reloads the
  resolver, and emits :data:`SettingsDialog.settingsChanged` so the host can
  re-run detection on the current warning with the new settings.

Edits also apply *live*: a short debounce timer restarts on every value change,
so once the user stops typing/clicking the changed keys are persisted and
:data:`SettingsDialog.settingsChanged` fires — the host reprocesses the selected
volume in near real time without closing the dialog.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
)

from ..settings.registry import REGISTRY, SettingSpec, get_spec
from ..settings.resolver import resolve

KEY_ROLE = Qt.UserRole + 1
SPEC_ROLE = Qt.UserRole + 2
SEARCH_ROLE = Qt.UserRole + 3  # concatenated label/key/description for filtering


class _ValueDelegate(QStyledItemDelegate):
    """Type-aware editors driven by each row's :class:`SettingSpec`."""

    def _spec(self, index) -> SettingSpec | None:
        return index.data(SPEC_ROLE)

    def createEditor(self, parent, option, index):
        spec = self._spec(index)
        if spec is None:
            return super().createEditor(parent, option, index)
        if spec.type == "int":
            w = QSpinBox(parent)
            w.setRange(int(spec.min if spec.min is not None else -10**9),
                       int(spec.max if spec.max is not None else 10**9))
            w.valueChanged.connect(lambda *_: self.commitData.emit(w))
            return w
        if spec.type == "float":
            w = QDoubleSpinBox(parent)
            w.setDecimals(2)
            w.setRange(spec.min if spec.min is not None else -1e12,
                       spec.max if spec.max is not None else 1e12)
            w.valueChanged.connect(lambda *_: self.commitData.emit(w))
            return w
        if spec.type == "bool":
            w = QCheckBox(parent)
            w.toggled.connect(lambda *_: self.commitData.emit(w))
            return w
        if spec.type == "choice":
            w = QComboBox(parent)
            w.addItems(list(spec.choices or ()))
            w.currentIndexChanged.connect(lambda *_: self.commitData.emit(w))
            return w
        w = QLineEdit(parent)
        w.textChanged.connect(lambda *_: self.commitData.emit(w))
        return w

    def setEditorData(self, editor, index):
        spec = self._spec(index)
        val = index.data(Qt.EditRole)
        if spec is None:
            return super().setEditorData(editor, index)
        # Block signals while populating so the initial value doesn't fire the
        # commit-on-change handler (which would emit a spurious commitData).
        editor.blockSignals(True)
        if spec.type in ("int", "float"):
            editor.setValue(spec.coerce(val))
        elif spec.type == "bool":
            editor.setChecked(bool(spec.coerce(val)))
        elif spec.type == "choice":
            editor.setCurrentText(str(val))
        else:
            editor.setText(str(val))
        editor.blockSignals(False)

    def setModelData(self, editor, model, index):
        spec = self._spec(index)
        if spec is None:
            return super().setModelData(editor, model, index)
        if spec.type in ("int", "float"):
            value = editor.value()
        elif spec.type == "bool":
            value = editor.isChecked()
        elif spec.type == "choice":
            value = editor.currentText()
        else:
            value = editor.text()
        model.setData(index, value, Qt.EditRole)
        model.setData(index, _display(spec, value), Qt.DisplayRole)


def _display(spec: SettingSpec, value: Any) -> str:
    if spec.type == "bool":
        return "true" if spec.coerce(value) else "false"
    return str(value)


class _Filter(QSortFilterProxyModel):
    def filterAcceptsRow(self, row, parent):
        idx = self.sourceModel().index(row, 0, parent)
        hay = (idx.data(SEARCH_ROLE) or "").lower()
        return self.filterRegularExpression().pattern().lower() in hay


class SettingsDialog(QDialog):
    settingsChanged = Signal()

    def __init__(self, dsn: str | None = None, local_path=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(640, 520)
        self.dsn = dsn
        self.local_path = local_path

        layout = QVBoxLayout(self)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter settings…")
        layout.addWidget(self.search)

        self.model = QStandardItemModel(0, 2)
        self.model.setHorizontalHeaderLabels(["Setting", "Value"])
        self.proxy = _Filter()
        self.proxy.setSourceModel(self.model)
        self.search.textChanged.connect(self.proxy.setFilterFixedString)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setItemDelegateForColumn(1, _ValueDelegate(self.table))
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QTableView.AllEditTriggers)
        layout.addWidget(self.table)

        self.save_btn = QPushButton("Save and Reprocess")
        self.save_btn.clicked.connect(self._save)
        layout.addWidget(self.save_btn)

        # Live apply: every edit (re)starts a short debounce; when it lapses we
        # persist + reprocess without closing. ``_loading`` suppresses the model
        # churn from ``_load`` so a refresh never re-triggers the timer.
        self._loading = False
        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(500)
        self._apply_timer.timeout.connect(self._apply_live)
        self.model.dataChanged.connect(self._on_data_changed)

        self._load()

    # --- model population -------------------------------------------------

    def _load(self) -> None:
        self._loading = True
        self.model.removeRows(0, self.model.rowCount())
        self._resolved = resolve(self.dsn, self.local_path).values
        for spec in REGISTRY:
            label = QStandardItem(spec.label)
            label.setEditable(False)
            label.setToolTip(spec.description)
            label.setData(spec.key, KEY_ROLE)
            label.setData(
                f"{spec.label} {spec.key} {spec.description} {spec.group}", SEARCH_ROLE
            )
            value = self._resolved.get(spec.key, spec.default)
            valitem = QStandardItem(_display(spec, value))
            valitem.setData(spec, SPEC_ROLE)
            valitem.setData(value, Qt.EditRole)
            self.model.appendRow([label, valitem])
        self._loading = False

    # --- save -------------------------------------------------------------

    def collect_changes(self) -> dict[str, Any]:
        """Validated {key: value} for rows whose value differs from resolved."""
        changes: dict[str, Any] = {}
        for r in range(self.model.rowCount()):
            key = self.model.item(r, 0).data(KEY_ROLE)
            spec = get_spec(key)
            raw = self.model.item(r, 1).data(Qt.EditRole)
            clean = spec.validate(raw)  # raises on out-of-range / bad choice
            if clean != spec.coerce(self._resolved.get(key, spec.default)):
                changes[key] = clean
        return changes

    def _persist(self, changes: dict[str, Any]) -> None:
        """Write changed keys and advance the in-memory baseline.

        Updates ``self._resolved`` rather than calling ``_load`` so the active
        editor isn't torn down mid-edit; subsequent ``collect_changes`` then
        sees these values as the new baseline and won't re-save them.
        """
        if not changes:
            return
        from ..settings.store import open_store

        store = open_store(self.dsn, self.local_path)
        if store is not None:
            with store as s:
                s.set_many(changes)
        self._resolved.update(changes)

    def _on_data_changed(self, *args) -> None:
        # Restart the debounce on every edit; ignore the churn from _load.
        if not self._loading:
            self._apply_timer.start()

    def _apply_live(self) -> None:
        """Debounced live apply: persist + reprocess without closing the dialog."""
        try:
            changes = self.collect_changes()
        except ValueError:
            return  # mid-edit value is out of range / not yet valid — wait
        if not changes:
            return
        self._persist(changes)
        self.settingsChanged.emit()

    def _save(self) -> None:
        self._apply_timer.stop()
        self._persist(self.collect_changes())
        self.settingsChanged.emit()
        self.accept()
