"""Settings dialog — live-filtered, type-aware editing of every tunable.

Layout (top to bottom):

* a search box pinned at the top that filters the list live as you type
  (case-insensitive over label / key / description) via a
  :class:`QSortFilterProxyModel`;
* a scrolling :class:`QTableView` — column 1 the label, column 2 a type-aware
  editable value (spin box for int/float with min/max, checkbox for bool, combo
  for choices, line edit for str);
* a Save button pinned at the bottom that validates against min/max/choices,
  writes only changed keys to ``app_settings``, reloads the resolver, and emits
  :data:`SettingsDialog.settingsChanged`.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
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
            return w
        if spec.type == "float":
            w = QDoubleSpinBox(parent)
            w.setDecimals(2)
            w.setRange(spec.min if spec.min is not None else -1e12,
                       spec.max if spec.max is not None else 1e12)
            return w
        if spec.type == "bool":
            return QCheckBox(parent)
        if spec.type == "choice":
            w = QComboBox(parent)
            w.addItems(list(spec.choices or ()))
            return w
        return QLineEdit(parent)

    def setEditorData(self, editor, index):
        spec = self._spec(index)
        val = index.data(Qt.EditRole)
        if spec is None:
            return super().setEditorData(editor, index)
        if spec.type in ("int", "float"):
            editor.setValue(spec.coerce(val))
        elif spec.type == "bool":
            editor.setChecked(bool(spec.coerce(val)))
        elif spec.type == "choice":
            editor.setCurrentText(str(val))
        else:
            editor.setText(str(val))

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

    def __init__(self, dsn: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(640, 520)
        self.dsn = dsn

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

        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._save)
        layout.addWidget(self.save_btn)

        self._load()

    # --- model population -------------------------------------------------

    def _load(self) -> None:
        self.model.removeRows(0, self.model.rowCount())
        self._resolved = resolve(self.dsn).values
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

    def _save(self) -> None:
        changes = self.collect_changes()
        if changes and self.dsn:
            from ..settings.store import SettingsStore

            with SettingsStore(self.dsn) as store:
                store.set_many(changes)
        self._load()  # reflect persisted state
        self.settingsChanged.emit()
        self.accept()
