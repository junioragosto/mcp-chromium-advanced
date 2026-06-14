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

        layout.addRow("Profile", self.profile_name_edit)
        layout.addRow("Account", self.account_edit)
        layout.addRow("", self.keepalive_enabled)
        layout.addRow(self.translate("profile_dialog_sites"), self.site_box)
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

