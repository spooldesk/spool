# Чистая установка Spooldesk v0.1.0-rc.6

Self-hosted: одна организация на одной установке. Для первого локального теста
VPS и домен не нужны. Будут запущены два сервиса: приложение `dashboard`
(включая обработчики каналов) и PostgreSQL. Лендинг устанавливать не требуется.

## 1. Подготовить компьютер

| ОС | Что установить самостоятельно | Архитектура образа |
|---|---|---|
| Windows с поддержкой Docker Desktop | [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) в режиме Linux containers, Python 3.12+ | Обычно amd64; ARM64 — если поддерживается установленным Docker |
| macOS, Intel | [Docker Desktop для Intel](https://docs.docker.com/desktop/setup/install/mac-install/), Python 3.12+ | amd64 |
| macOS, Apple Silicon | [Docker Desktop для Apple Silicon](https://docs.docker.com/desktop/setup/install/mac-install/), Python 3.12+ | arm64 |
| Linux x86-64 / ARM64 | [Docker Engine](https://docs.docker.com/engine/install/) и [Compose plugin](https://docs.docker.com/compose/install/linux/), Python 3.12+, unzip | amd64 / arm64 |

Запусти Docker Desktop либо службу Docker. Команда `docker version` должна
показывать и Client, и Server; `docker compose version` — Compose v2.
На Linux настрой доступ своего пользователя к Docker по инструкции Docker.
Готовые образы — **Linux/amd64 и Linux/arm64**, не нативные Windows-контейнеры.
32-битные ОС не поддерживаются этим пакетом. ARM64 проверяется в CI под QEMU;
проверка на твоём реальном устройстве остаётся частью установочной приёмки.

Нужен интернет для загрузки пакета и PostgreSQL: это не offline-установщик.
Python нужен только для мастера настройки, библиотеки проекта на хост ставить
не нужно. На Windows ниже используется PowerShell, на macOS/Linux — Bash или zsh.
Команды рассчитаны на **новую установку**; если проект Docker `spooldesk` на
компьютере уже существует, остановись и используй процедуру обновления/backup.

## 2. Скачать пакет

**[Скачать Spooldesk v0.1.0-rc.6 одним ZIP](https://github.com/spooldesk/spool/releases/download/v0.1.0-rc.6/spooldesk-v0.1.0-rc.6-transfer.zip)**

Рядом скачай [SHA-256 ZIP](https://github.com/spooldesk/spool/releases/download/v0.1.0-rc.6/spooldesk-v0.1.0-rc.6-transfer.zip.sha256).
Ссылки также находятся наверху главной страницы репозитория. Пока репозиторий
приватный, нужен вход в GitHub под аккаунтом с доступом. Не выбирай стандартный
GitHub «Source code»: он не содержит готовых Docker-образов.

Альтернатива — [GitHub CLI](https://cli.github.com/). Авторизуйся один раз
через `gh auth login`, затем выполни:

```sh
gh release download v0.1.0-rc.6 --repo spooldesk/spool --pattern 'spooldesk-v0.1.0-rc.6-transfer.zip*' --dir spooldesk-download
cd spooldesk-download
```

Если скачивал браузером, открой терминал в папке с ZIP и `.sha256`.
Пакет содержит исходники, оба образа, их ID/теги, `SOURCE_COMMIT`,
`VALIDATION.json`, `SHA256SUMS` и автономную копию этой инструкции `INSTALL-RU.md`.

## 3. Установить — выбери раздел для своей ОС

Не переноси `.env`, базы, тома, ключи и AI TEST данные с основного компьютера.
Не запускай `docker-compose.preview.yml` или скрипты заполнения демо.

### Windows / PowerShell

Выполняй блоки по порядку **в одном окне PowerShell**. При любой ошибке
не переходи к следующему блоку.

Проверь ZIP, распакуй пакет и проверь его внутренние файлы:

```powershell
$ErrorActionPreference = 'Stop'
$package = 'spooldesk-v0.1.0-rc.6-transfer.zip'
$expected = ((Get-Content "$package.sha256" -Raw).Trim() -split '\s+')[0]
$actual = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $expected) { throw 'Package checksum mismatch' }
if (Test-Path -LiteralPath '.\spooldesk-install') { throw 'Choose a new empty destination' }
Expand-Archive -LiteralPath $package -DestinationPath '.\spooldesk-install'
Set-Location '.\spooldesk-install'
Get-Content .\SHA256SUMS | ForEach-Object {
    $parts = $_ -split '  ', 2
    $actual = (Get-FileHash -LiteralPath $parts[1] -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $parts[0]) { throw "Checksum mismatch: $($parts[1])" }
}
docker version
if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop first' }
docker compose version
if ($LASTEXITCODE -ne 0) { throw 'Docker Compose v2 is required' }
python --version
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12 or newer first' }
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12+ is required' }
```

Выбери образ по архитектуре **Docker**, загрузи и сверь его ID:

```powershell
$dockerArch = (docker info --format '{{.Architecture}}').Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot read Docker architecture' }
switch ($dockerArch) {
    { $_ -in 'x86_64', 'amd64' } { $suffix = ''; $imageFile = 'spooldesk-image.tar.gz' }
    { $_ -in 'aarch64', 'arm64' } { $suffix = '_ARM64'; $imageFile = 'spooldesk-image-arm64.tar.gz' }
    default { throw "Unsupported Docker architecture: $dockerArch" }
}
$releaseImage = (Get-Content "IMAGE_TAG$suffix" -Raw).Trim()
$expectedImageId = (Get-Content "IMAGE_ID$suffix" -Raw).Trim()
docker load --input $imageFile
if ($LASTEXITCODE -ne 0) { throw 'Image load failed' }
$loadedImageId = (docker image inspect $releaseImage --format '{{.Id}}').Trim()
if ($LASTEXITCODE -ne 0 -or $loadedImageId -ne $expectedImageId) { throw 'Image ID mismatch' }
Expand-Archive -LiteralPath .\spooldesk-source.zip -DestinationPath .
Set-Location .\spooldesk
python scripts/setup.py
if ($LASTEXITCODE -ne 0) { throw 'Setup did not finish' }
Add-Content -LiteralPath .env -Value "`nSPOOLDESK_IMAGE=$releaseImage" -Encoding UTF8
```

Мастер спросит пароль. Нажми Enter для генерации и сохрани показанный пароль
в менеджере паролей. Логин владельца — **`admin@local`**.

Запусти установку:

```powershell
docker pull postgres:16-alpine
if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL download failed' }
docker compose -p spooldesk up -d --no-build --pull never dashboard
if ($LASTEXITCODE -ne 0) { throw 'Startup failed; inspect the logs' }
docker compose -p spooldesk ps
```

### macOS / Linux — Bash или zsh

Выполняй блоки по порядку в одном терминале. При любой ошибке остановись.
Ниже `set -e` прекращает выполнение при ошибке; контрольные проверки
используют `exit 1`, поэтому могут закрыть текущую оболочку.

Проверь пакет и инструменты:

```sh
set -e
package=spooldesk-v0.1.0-rc.6-transfer.zip
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum --check "$package.sha256"
else
  shasum -a 256 --check "$package.sha256"
fi
if [ -e spooldesk-install ]; then echo 'Choose a new empty destination'; exit 1; fi
unzip -q "$package" -d spooldesk-install
cd spooldesk-install
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum --check SHA256SUMS
else
  shasum -a 256 --check SHA256SUMS
fi
docker version
docker compose version
python3 --version
python3 -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ is required"'
```

Загрузи соответствующий образ и создай собственные настройки:

```sh
docker_arch=$(docker info --format '{{.Architecture}}')
case "$docker_arch" in
  x86_64|amd64) suffix=''; image_file=spooldesk-image.tar.gz ;;
  aarch64|arm64) suffix=_ARM64; image_file=spooldesk-image-arm64.tar.gz ;;
  *) echo "Unsupported Docker architecture: $docker_arch"; exit 1 ;;
esac
release_image=$(cat "IMAGE_TAG$suffix")
expected_image_id=$(cat "IMAGE_ID$suffix")
docker load --input "$image_file"
loaded_image_id=$(docker image inspect "$release_image" --format '{{.Id}}')
if [ "$loaded_image_id" != "$expected_image_id" ]; then echo 'Image ID mismatch'; exit 1; fi
unzip -q spooldesk-source.zip
cd spooldesk
python3 scripts/setup.py
printf '\nSPOOLDESK_IMAGE=%s\n' "$release_image" >> .env
chmod 600 .env
docker pull postgres:16-alpine
docker compose -p spooldesk up -d --no-build --pull never dashboard
docker compose -p spooldesk ps
```

На вопрос мастера о пароле можно нажать Enter для генерации. Сохрани пароль;
логин первого владельца — **`admin@local`**. Если команда `python3` запускает
старую версию, используй установленную `python3.12` или новее в обоих блоках.

## 4. Открыть и проверить

Открой **http://localhost:8000** на том же компьютере, где работает Docker.
Первый запуск может занять минуту. Войди новым паролем. Если страница не
открывается, проверь `docker compose -p spooldesk ps` и
`docker compose -p spooldesk logs --tail 100 dashboard postgres` из папки `spooldesk`.
`http://localhost:8000/healthz` должен возвращать успешный ответ.

Для первой приёмки:

1. Убедись, что база чистая; стандартная команда и категории создаются автоматически.
2. Создай оператора, включи виджет в **Интеграциях** и пройди обращение → ответ.
3. Проверь загрузку/скачивание вложения, смену статуса и сохранение после перезапуска.
4. Настрой нужные каналы в **Интеграциях**, ИИ — отдельно в **Настройках ИИ**.
   Пройди входящее сообщение → тикет → ответ → получение клиентом. Настройки
   провайдеров на новой машине проверяются отдельно от уже пройденных тестов кода.

Не подключай один рабочий mailbox/Telegram bot одновременно к двум активным
установкам: обработчики могут перехватывать входящие сообщения. Используй
отдельные тестовые подключения либо отключи канал на прежней установке.
Входящие Slack HTTP events и виджет на внешнем сайте требуют публичного HTTPS;
`localhost` снаружи недоступен. Почта IMAP/SMTP работает через исходящие подключения.
Доступ с других устройств/из интернета настраивается отдельно по `DEPLOY.md`.

## 5. Остановка и следующий запуск

Всегда открывай терминал в той же папке `spooldesk` с созданным `.env` и
используй то же имя проекта `-p spooldesk`:

```sh
docker compose -p spooldesk stop
docker compose -p spooldesk start
docker compose -p spooldesk restart dashboard
docker compose -p spooldesk logs --tail 100 dashboard
```

При обычном запуске не нужны пересборка и мастер настройки. Docker должен
работать; сон/выключение компьютера останавливает доступность сервиса.
**Не выполняй `down -v`**: флаг удаляет данные в томах. Не запускай setup повторно:
смена пароля PostgreSQL в `.env` не меняет его в уже созданной БД, а потеря
`SECRETS_KEY` делает зашифрованные данные нечитаемыми. Храни ключ отдельно от backup.

## Сборка из исходников вместо готового образа

Это альтернативный путь для новой установки, не следующий шаг после инструкции
выше. Распакуй проверенный `spooldesk-source.zip` в другую пустую папку, перейди
в `spooldesk`, выполни `python scripts/setup.py` (Windows) либо
`python3 scripts/setup.py` (macOS/Linux), затем:

```sh
docker compose -p spooldesk up -d --build dashboard
```

Не задавай `SPOOLDESK_IMAGE` из готового пакета. Сборка создаёт другой локальный
образ; его ID не обязан совпадать с CI. Для установочной приёмки rc.6 используй
готовый образ из основного сценария. Не запускай два варианта на одном проекте
Docker и не используй этот путь поверх существующей БД без процедуры обновления.

## Когда тест считать завершённым

Запиши версию rc.6, ОС/архитектуру, ID выбранного образа, результат входа,
обращения/ответа, вложения и перезапуска. Пришли результат без паролей и `.env`.
До этого шага кандидат остаётся **prerelease**, не принятой стабильной версией.
CI проверяет код на Windows/Linux и запуск обоих образов; это не утверждение,
что ручная установка на каждом варианте macOS/Windows/Linux уже выполнена.

Для клиентской эксплуатации также нужны HTTPS, backup/restore и ответственный
за поддержку. Документы находятся внутри распакованных исходников:
`docs/SELF_HOSTED_RELEASE.md`, `docs/BACKUP_RESTORE.md` и `DEPLOY.md`.
