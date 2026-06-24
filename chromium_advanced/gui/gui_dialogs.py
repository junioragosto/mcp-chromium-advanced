from typing import Dict, Optional

from PyQt5.QtCore import QSize
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QWidget,
)

from chromium_advanced.chromium_profile_lib import (
    get_extension_catalog,
    get_profile_extension_ids,
    get_keepalive_site_icon_path,
    get_keepalive_site_ids,
    get_keepalive_site_label,
)


class ProfileEditDialog(QDialog):
    def __init__(self, profile: Dict, config: Optional[Dict] = None, parent=None, translator=None):
        super().__init__(parent)
        self.translate = translator or (lambda key, fallback="": fallback or key)
        self.config = config or {}
        self.setWindowTitle(self.translate("profile_dialog_title"))
        self.resize(560, 320)
        layout = QFormLayout(self)

        self.profile_name_edit = QLineEdit(profile.get("profile_name", ""))
        self.profile_name_edit.setReadOnly(True)
        self.account_edit = QLineEdit(profile.get("account", ""))
        self.keepalive_enabled = QCheckBox(self.translate("profile_dialog_keepalive"))
        self.keepalive_enabled.setChecked(profile.get("keepalive_enabled", False))
        self.site_flags = dict(profile.get("keepalive_sites", {}) or {})
        self.notes_edit = QTextEdit(profile.get("notes", ""))
        self.notes_edit.setAcceptRichText(False)
        self.notes_edit.setPlaceholderText(self.translate("profile_dialog_notes_placeholder"))
        self.notes_edit.setMaximumHeight(90)
        self.site_box = QWidget()
        self.site_box_layout = QHBoxLayout(self.site_box)
        self.site_box_layout.setContentsMargins(0, 0, 0, 0)
        self.site_box_layout.setSpacing(8)
        self.site_checkboxes = {}
        self.extension_box = QWidget()
        self.extension_box_layout = QHBoxLayout(self.extension_box)
        self.extension_box_layout.setContentsMargins(0, 0, 0, 0)
        self.extension_box_layout.setSpacing(8)
        self.extension_checkboxes = {}
        hydrated_extension_ids = profile.get("extensions", []) if isinstance(profile.get("extensions", []), list) else []
        if not hydrated_extension_ids:
            hydrated_extension_ids = get_profile_extension_ids(self.config, profile.get("profile_name", ""))
        self.extension_ids = list(hydrated_extension_ids if isinstance(hydrated_extension_ids, list) else [])

        for site_name in get_keepalive_site_ids(self.config):
            label = self.translate(f"site_name_{site_name}", get_keepalive_site_label(site_name, self.config))
            checkbox = QCheckBox(label)
            icon_path = get_keepalive_site_icon_path(site_name, self.config, fetch=False)
            if icon_path:
                checkbox.setIcon(QIcon(icon_path))
                checkbox.setIconSize(QSize(16, 16))
            checkbox.setChecked(bool(self.site_flags.get(site_name, False)))
            self.site_checkboxes[site_name] = checkbox
            self.site_box_layout.addWidget(checkbox)
        self.site_box_layout.addStretch()

        for extension_record in get_extension_catalog(self.config):
            extension_id = str(extension_record.get("extension_id", "") or "").strip()
            if not extension_id:
                continue
            label = str(extension_record.get("display_name", "") or extension_id).strip() or extension_id
            checkbox = QCheckBox(label)
            checkbox.setChecked(extension_id in self.extension_ids)
            self.extension_checkboxes[extension_id] = checkbox
            self.extension_box_layout.addWidget(checkbox)
        self.extension_box_layout.addStretch()

        layout.addRow("Profile", self.profile_name_edit)
        layout.addRow("Account", self.account_edit)
        layout.addRow("", self.keepalive_enabled)
        layout.addRow(self.translate("profile_dialog_sites"), self.site_box)
        layout.addRow(self.translate("extensions", "Extensions"), self.extension_box)
        layout.addRow(self.translate("profile_dialog_notes"), self.notes_edit)

        button_row = QHBoxLayout()
        button_row.addStretch()
        save_button = QPushButton(self.translate("common_save"))
        cancel_button = QPushButton(self.translate("common_cancel"))
        save_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        button_row.addWidget(save_button)
        button_row.addWidget(cancel_button)
        layout.addRow(button_row)

    def get_data(self) -> Dict:
        return {
            "profile_name": self.profile_name_edit.text().strip(),
            "account": self.account_edit.text().strip(),
            "keepalive_enabled": self.keepalive_enabled.isChecked(),
            "keepalive_sites": {site_name: checkbox.isChecked() for site_name, checkbox in self.site_checkboxes.items()},
            "extensions": [extension_id for extension_id, checkbox in self.extension_checkboxes.items() if checkbox.isChecked()],
            "notes": self.notes_edit.toPlainText().strip(),
        }


class KeepalivePluginCreateDialog(QDialog):
    def __init__(self, parent=None, translator=None):
        super().__init__(parent)
        self.translate = translator or (lambda key, fallback="": fallback or key)
        self.setWindowTitle(self.translate("plugin_dialog_title"))
        self.resize(420, 180)
        layout = QFormLayout(self)

        self.site_id_edit = QLineEdit()
        self.display_name_edit = QLineEdit()
        self.home_url_edit = QLineEdit()
        self.site_id_edit.setPlaceholderText(self.translate("plugin_dialog_site_id_placeholder"))
        self.display_name_edit.setPlaceholderText(self.translate("plugin_dialog_display_name_placeholder"))
        self.home_url_edit.setPlaceholderText(self.translate("plugin_dialog_home_url_placeholder"))

        layout.addRow(self.translate("plugin_table_site_id"), self.site_id_edit)
        layout.addRow(self.translate("plugin_table_display_name"), self.display_name_edit)
        layout.addRow(self.translate("plugin_detail_home_url"), self.home_url_edit)

        button_row = QHBoxLayout()
        button_row.addStretch()
        save_button = QPushButton(self.translate("common_save"))
        cancel_button = QPushButton(self.translate("common_cancel"))
        save_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        button_row.addWidget(save_button)
        button_row.addWidget(cancel_button)
        layout.addRow(button_row)

    def get_data(self) -> Dict:
        return {
            "site_id": self.site_id_edit.text().strip(),
            "display_name": self.display_name_edit.text().strip(),
            "home_url": self.home_url_edit.text().strip(),
        }


class ExtensionEditDialog(QDialog):
    def __init__(self, extension: Optional[Dict] = None, parent=None, translator=None):
        super().__init__(parent)
        self.translate = translator or (lambda key, fallback="": fallback or key)
        extension = extension or {}
        is_new = not bool(str(extension.get("extension_id", "") or "").strip())
        self.setWindowTitle(
            self.translate("extensions_dialog_title_new", "New Extension")
            if is_new
            else self.translate("extensions_dialog_title_edit", "Edit Extension")
        )
        self.resize(520, 260)
        layout = QFormLayout(self)

        self.extension_id_edit = QLineEdit(str(extension.get("extension_id", "") or ""))
        self.extension_id_edit.setPlaceholderText(
            self.translate("extensions_dialog_id_placeholder", "extension_id")
        )
        self.extension_id_edit.setReadOnly(not is_new)

        self.display_name_edit = QLineEdit(str(extension.get("display_name", "") or ""))
        self.display_name_edit.setPlaceholderText(
            self.translate("extensions_dialog_name_placeholder", "Display Name")
        )

        self.source_type_edit = QLineEdit(str(extension.get("source_type", "dir") or "dir"))
        self.source_type_edit.setPlaceholderText(
            self.translate("extensions_dialog_source_type_placeholder", "dir / zip / crx")
        )

        self.source_path_edit = QLineEdit(str(extension.get("source_path", "") or ""))
        self.source_path_edit.setPlaceholderText(
            self.translate("extensions_dialog_source_path_placeholder", "D:/path/to/extension")
        )

        self.enabled_checkbox = QCheckBox(
            self.translate("extensions_dialog_enabled", "Enabled")
        )
        self.enabled_checkbox.setChecked(bool(extension.get("enabled", True)))

        self.notes_edit = QTextEdit(str(extension.get("notes", "") or ""))
        self.notes_edit.setAcceptRichText(False)
        self.notes_edit.setPlaceholderText(
            self.translate("extensions_dialog_notes_placeholder", "Notes")
        )
        self.notes_edit.setMaximumHeight(90)

        layout.addRow(
            self.translate("extensions_table_id", "Extension ID"),
            self.extension_id_edit,
        )
        layout.addRow(
            self.translate("extensions_table_name", "Display Name"),
            self.display_name_edit,
        )
        layout.addRow(
            self.translate("extensions_table_source_type", "Source Type"),
            self.source_type_edit,
        )
        layout.addRow(
            self.translate("extensions_table_source_path", "Source Path"),
            self.source_path_edit,
        )
        layout.addRow("", self.enabled_checkbox)
        layout.addRow(
            self.translate("profile_dialog_notes", "Notes"),
            self.notes_edit,
        )

        button_row = QHBoxLayout()
        button_row.addStretch()
        save_button = QPushButton(self.translate("common_save"))
        cancel_button = QPushButton(self.translate("common_cancel"))
        save_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        button_row.addWidget(save_button)
        button_row.addWidget(cancel_button)
        layout.addRow(button_row)

    def get_data(self) -> Dict:
        return {
            "extension_id": self.extension_id_edit.text().strip(),
            "display_name": self.display_name_edit.text().strip(),
            "source_type": self.source_type_edit.text().strip().lower() or "dir",
            "source_path": self.source_path_edit.text().strip(),
            "enabled": self.enabled_checkbox.isChecked(),
            "notes": self.notes_edit.toPlainText().strip(),
        }
