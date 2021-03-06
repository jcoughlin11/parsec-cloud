# Parsec Cloud (https://parsec.cloud) Copyright (c) AGPLv3 2019 Scille SAS

from PyQt5.QtWidgets import QWidget, QApplication

from structlog import get_logger

from parsec.core.local_device import get_key_file, change_device_password, LocalDeviceCryptoError

from parsec.core.gui.custom_dialogs import show_error, show_info, GreyedDialog
from parsec.core.gui.lang import translate as _

from parsec.core.gui.ui.password_change_widget import Ui_PasswordChangeWidget


logger = get_logger()


class PasswordChangeWidget(QWidget, Ui_PasswordChangeWidget):
    def __init__(self, core, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setupUi(self)
        self.core = core
        self.dialog = None
        self.widget_new_password.info_changed.connect(self.check_infos)
        self.button_change.clicked.connect(self.change_password)
        self.button_change.setDisabled(True)

    def check_infos(self):
        if self.widget_new_password.is_valid():
            self.button_change.setDisabled(False)
        else:
            self.button_change.setDisabled(True)

    def change_password(self):
        key_file = get_key_file(self.core.config.config_dir, self.core.device)
        try:
            change_device_password(
                key_file, self.line_edit_old_password.text(), self.widget_new_password.password
            )
            show_info(self, _("TEXT_CHANGE_PASSWORD_SUCCESS"))
            if self.dialog:
                self.dialog.accept()
            elif QApplication.activeModalWidget():
                QApplication.activeModalWidget().accept()
            else:
                logger.warning("Cannot close dialog when changing password info")
        except LocalDeviceCryptoError as exc:
            show_error(self, _("TEXT_CHANGE_PASSWORD_INVALID_PASSWORD"), exception=exc)

    @classmethod
    def show_modal(cls, core, parent, on_finished):
        w = cls(core=core)
        d = GreyedDialog(w, title=_("TEXT_CHANGE_PASSWORD_TITLE"), parent=parent)
        w.dialog = d

        if on_finished:
            d.finished.connect(on_finished)
        # Unlike exec_, show is asynchronous and works within the main Qt loop
        d.show()
        return w
