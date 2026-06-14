from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QCheckBox, QHBoxLayout, QPushButton, QSpinBox, QTimeEdit, QWidget


class FocusWheelSpinBox(QSpinBox):
    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
            return
        event.ignore()


class FocusWheelTimeEdit(QTimeEdit):
    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
            return
        event.ignore()


def create_centered_checkbox_widget(
    *,
    checked: bool,
    enabled: bool,
    on_state_changed,
) -> tuple[QWidget, QCheckBox]:
    checkbox = QCheckBox()
    checkbox.setChecked(bool(checked))
    checkbox.setEnabled(bool(enabled))
    checkbox.stateChanged.connect(on_state_changed)
    wrapper = QWidget()
    layout = QHBoxLayout(wrapper)
    layout.setContentsMargins(4, 2, 4, 2)
    layout.addWidget(checkbox)
    layout.setAlignment(Qt.AlignCenter)
    return wrapper, checkbox


def create_profile_action_buttons_widget(
    *,
    launch_text: str,
    launch_enabled: bool,
    launch_tooltip: str,
    on_launch_clicked,
    keepalive_text: str,
    keepalive_enabled: bool,
    keepalive_tooltip: str,
    keepalive_style: str,
    on_keepalive_clicked,
) -> tuple[QWidget, QPushButton, QPushButton]:
    launch_button = QPushButton(str(launch_text or ""))
    launch_button.setEnabled(bool(launch_enabled))
    launch_button.setToolTip(str(launch_tooltip or ""))
    launch_button.clicked.connect(on_launch_clicked)

    keepalive_button = QPushButton(str(keepalive_text or ""))
    keepalive_button.setEnabled(bool(keepalive_enabled))
    keepalive_button.setToolTip(str(keepalive_tooltip or ""))
    keepalive_button.setStyleSheet(str(keepalive_style or ""))
    keepalive_button.clicked.connect(on_keepalive_clicked)

    wrapper = QWidget()
    layout = QHBoxLayout(wrapper)
    layout.setContentsMargins(4, 2, 4, 2)
    layout.setSpacing(4)
    layout.addWidget(launch_button)
    layout.addWidget(keepalive_button)
    layout.setAlignment(Qt.AlignCenter)
    return wrapper, launch_button, keepalive_button
