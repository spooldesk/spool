# Installation troubleshooting / Помощь с установкой

Keep the extracted folder and Docker volumes. Do not share `.env`, passwords or
API keys. A failed setup step is not a reason to reset Docker or delete databases.

| Message or symptom | What to do |
| --- | --- |
| Docker not found / cannot connect | Install or start Docker; wait for the engine to be ready and open the launcher again. Use Linux containers. |
| Package checksum mismatch / missing file | Extract a fresh complete installer ZIP into a new folder. Do not edit its scripts or identity files. |
| Loaded image differs | Use rc.9 or newer. rc.7 did not handle containerd IDs. If the error persists, report the version and the displayed IDs; do not bypass verification. |
| Password rejected | Follow the character and length hint. A length, unsupported character or confirmation mismatch now has a separate explanation. Enter generates a password. |
| Confirmation not recognized | Enter `1` or press Enter to install; `0` cancels. Check the keyboard layout. Other unrecognized input is requested again. |
| Cancelled | No installation starts. Open the launcher again when ready. No cleanup is required. |
| Port already in use | Stop the application using that port, then run the installer again. Do not remove existing database volumes. |
| Existing installation / storage belongs to another installation | Open the launcher in the original installation folder. For an older release follow the backup/upgrade guide; do not rename volumes or reset credentials. |
| Configuration changed | The wizard refuses to overwrite modified settings. Follow the deployment procedure for your edited configuration. |
| Download interrupted | Restore internet access and run the same launcher from the same folder. Saved settings are retained. |
| Dashboard is not ready | Inspect `dashboard` and `postgres` logs in Docker Desktop, resolve the reported cause, then retry. Health checks do not prove external email/Slack/AI delivery. |
| Another installer is running | Close duplicate installers. After a power failure, confirm no installer is active, then remove only the stopped `spooldesk-installer-lock` container. |
| Browser did not open | Open the localhost URL printed by the launcher. A browser-launch failure does not erase or stop a ready installation. |

## Русский

Сохрани папку установки и тома Docker. Не отправляй пароли, ключи и содержимое
`.env`. Не сбрасывай Docker и не удаляй базу для исправления ошибки установщика.

- **Пароль:** сначала показаны требования. Ввод скрыт. Если не хочешь подбирать
  пароль, нажми Enter — мастер создаст его. Ошибки длины, символов и несовпадения
  повторного ввода объясняются отдельно.
- **Подтверждение:** введи **1** или нажми Enter для установки, **0** для отмены.
  Нераспознанный ответ не отменяет установку — мастер спросит ещё раз.
- **Отмена:** это нормальный выход. Чтобы начать заново, открой тот же файл запуска.
- **Docker недоступен:** запусти Docker Desktop, дождись готовности и повтори запуск.
- **Порт занят:** останови приложение, которое занимает выбранный порт, и повтори запуск. Не удаляй тома и не меняй ключи базы.
- **Архив повреждён или неполный:** скачай и полностью распакуй новый installer ZIP.
  Не редактируй проверки SHA256SUMS и IMAGE_IDS вручную.
- **Ошибка ID образа в rc.7 или отмена в rc.8:** используй rc.9 в новой папке, если
  прежний запуск остановился до создания настроек и базы. Работающая установка
  с данными требует процедуры обновления, а не создания новой базы.
- **Другая установка уже существует:** запускай её из исходной папки. Мастер
  специально не переключает базу и не перезаписывает чужие настройки.
- **Кабинет не готов:** посмотри логи dashboard/postgres в Docker Desktop. После
  исправления причины повтори запуск из той же папки.

When asking for help, include the release version, operating system, Docker
version, failed step and error text. Remove credentials from screenshots.
