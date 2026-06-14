from typing import List, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from chromium_advanced.chromium_profile_lib import KeepAliveStopController, run_keepalive_job
from chromium_advanced.gui.gui_runtime import describe_keepalive_source


class KeepAliveWorker(QThread):
    log_signal = pyqtSignal(str, str)
    payload_signal = pyqtSignal(str, object)

    def __init__(
        self,
        config_path: str,
        selected_profiles: Optional[List[str]],
        source: str,
        parent=None,
        translator=None,
    ):
        super().__init__(parent)
        self.config_path = config_path
        self.selected_profiles = selected_profiles or []
        self.source = source
        self.translate = translator or (lambda key, fallback="": fallback or key)
        self.task_prefix = describe_keepalive_source(source, self.selected_profiles, self.translate)
        self.stop_controller = KeepAliveStopController()

    def request_stop(self):
        self.stop_controller.request_stop()

    def run(self):
        try:
            summary = run_keepalive_job(
                config_path=self.config_path,
                selected_profiles=self.selected_profiles,
                logger=lambda message: self.log_signal.emit(self.task_prefix, message),
                source=self.source,
                stop_controller=self.stop_controller,
                progress_callback=lambda kind, payload: self.payload_signal.emit(f"__{kind.upper()}__", payload),
            )
            self.payload_signal.emit("__SUMMARY__", summary)
        except Exception as exc:
            self.log_signal.emit(self.task_prefix, self.translate("keepalive_thread_error").format(error=exc))
            self.payload_signal.emit("__ERROR__", {"message": str(exc)})
