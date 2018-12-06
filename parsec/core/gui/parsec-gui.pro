# Used only for translations

SOURCES += app.py \
           main_window.py \
           files_widget.py \
           login_widget.py \
           settings_widget.py \
           users_widget.py \
           file_size.py \
           custom_widgets.py \
           devices_widget.py \
           workspaces_widget.py


TRANSLATIONS += tr/parsec_fr.ts


FORMS += forms/main_window.ui \
         forms/files_widget.ui \
         forms/users_widget.ui \
         forms/settings_widget.ui \
         forms/login_widget.ui \
         forms/register_device.ui \
         forms/devices_widget.ui \
         forms/login_login_widget.ui \
         forms/login_register_device_widget.ui \
         forms/login_register_user_widget.ui \
         forms/mount_widget.ui \
         forms/workspaces_widget.ui \
         forms/workspace_button.ui \
         forms/message_dialog.ui \
         forms/user_button.ui \
         forms/input_dialog.ui \
         forms/question_dialog.ui \
         forms/shared_dialog.ui \
         forms/device_button.ui

RESOURCES += rc/resources.qrc
