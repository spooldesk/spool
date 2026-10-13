# Зависимости production-сборки

Целевая среда выпуска: CPython 3.12, Linux x86_64 (Docker). Локальные тесты
также выполняются на Windows. Изменение версии Python или архитектуры сервера
требует отдельной проверки установки и запуска.

## Какие файлы поддерживать

| Входной файл | Сгенерированный lock | Назначение |
|---|---|---|
| `requirements.in` | `requirements.txt` | Runtime и поставляемый в venv pip |
| `requirements-dev.in` | `requirements-dev.txt` | pytest; устанавливается вместе с runtime |
| `requirements-tools.in` | `requirements-tools.txt` | uv и pip-audit в отдельном окружении |

В `.in` перечислены прямые зависимости и обоснованные ограничения. В `.txt`
закреплены полные деревья зависимостей, условия платформ и SHA-256 артефактов.
Большая часть размера lock-файлов — хеши wheels для разных платформ.
Dev-lock ограничен runtime-lock, поэтому тестовые зависимости не могут
незаметно обновить production-пакеты.

### Исправление безопасности от 2026-10-13

Аудит обнаружил [CVE-2026-104874 в multidict](https://github.com/aio-libs/multidict/security/advisories/GHSA-54p9-h82j-f925).
В runtime-lock обновлён только `multidict`: `6.9.0` → `6.9.1`, с новыми хешами
артефактов. В `requirements.in` добавлена нижняя граница исправленной версии,
dev-lock повторно разрешён с ограничениями runtime. Остальные версии сохранены.

Отдельно в maintenance-tools обновлён `uv`: `0.12.17` → `0.12.18` для исправления
[CVE-2026-104843](https://github.com/astral-sh/uv/security/advisories/GHSA-2cv4-cqwr-gwf7)
при установке wheels на Windows. `uv` не входит в runtime Docker-образа.

Ранее опубликованный ZIP/образ `v0.1.0-rc.5` содержит прежний lock. Изменение
исходников не обновляет этот образ или уже работающие установки. Для новой
установки использовать сборку исправленных исходников; для поставки готового
образа нужен новый проверенный кандидат. Существующие архивы не заменять
под прежним тегом.

Обычная установка не требует uv:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes --only-binary=:all: -r requirements.txt -r requirements-dev.txt
python -m pip check
python -m pytest -q
```

На Windows использовать Python 3.12 и `.venv\Scripts\Activate.ps1`.
Для production не устанавливать `requirements-dev.txt` и `requirements-tools.txt`.
Для проверки обновления создавать чистую venv: установка поверх старой не удаляет
лишние пакеты. Существующую рабочую venv не нужно удалять до приёмки новой.

Docker и CI используют `--require-hashes --only-binary=:all:`. Неизвестный хеш,
отсутствие wheel или конфликт зависимостей останавливают установку. Компиляция
случайного sdist не является запасным путём. Runtime содержит закреплённую
версию pip, чтобы не сохранять устаревший installer из базового образа.

## Обновление lock-файлов

Инструменты устанавливаются только в отдельную venv проекта:

```bash
python3.12 -m venv .venv-tools
.venv-tools/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-tools.txt
```

Windows-эквиваленты: `.venv-tools\Scripts\python.exe` и
`.venv-tools\Scripts\uv.exe`. Не менять глобальный Python/PATH.

Изменить нужный `.in`, затем выполнить из корня репозитория:

```bash
.venv-tools/bin/uv pip compile requirements.in --python-version 3.12 --universal --generate-hashes --no-build --no-config --no-python-downloads --custom-compile-command 'uv pip compile requirements.in --python-version 3.12 --universal --generate-hashes --no-build -o requirements.txt' -o requirements.txt --quiet
.venv-tools/bin/uv pip compile requirements-dev.in --python-version 3.12 --universal --generate-hashes --no-build --no-config --no-python-downloads --custom-compile-command 'uv pip compile requirements-dev.in --python-version 3.12 --universal --generate-hashes --no-build -o requirements-dev.txt' -o requirements-dev.txt --quiet
.venv-tools/bin/uv pip compile requirements-tools.in --python-version 3.12 --universal --generate-hashes --no-build --no-config --no-python-downloads --custom-compile-command 'uv pip compile requirements-tools.in --python-version 3.12 --universal --generate-hashes --no-build -o requirements-tools.txt' -o requirements-tools.txt --quiet
```

uv сохраняет совместимые версии из существующего lock. Для исправления
транзитивной зависимости добавить `--upgrade-package NAME` к соответствующей
команде. Обновлять все пакеты через `--upgrade` только как отдельное проверяемое
изменение. После изменения runtime-lock пересобрать dev-lock. После изменения
tools-lock переустановить инструменты из него в чистую venv.

Принятие обновления:

1. Проверить diff входов, версий, платформенных условий и источник артефактов.
2. Установить оба app-lock в чистое окружение; выполнить `pip check` и тесты.
3. Выполнить аудит runtime/dev и отдельно инструментов:

   ```bash
   .venv-tools/bin/python -m pip_audit --require-hashes -r requirements.txt -r requirements-dev.txt --progress-spinner off
   .venv-tools/bin/python -m pip_audit --require-hashes -r requirements-tools.txt --progress-spinner off
   ```

4. Собрать контейнер, проверить `/healthz`, запуск без Telegram и PostgreSQL job.
   Сам факт наличия Linux wheels не доказывает работоспособность контейнера.
5. Зафиксировать `.in` и `.txt` вместе. CI повторно генерирует locks и отклоняет
   несовпадение, конфликт, найденную уязвимость или недоступность сервиса аудита.

Аудит передаёт сервису PyPI названия и версии публичных пакетов. `.env`, код и
данные клиентов для него не нужны. Не использовать `--fix` или подавление
уязвимостей вместо ревью обновления. Результат «не найдено» относится к известным
уязвимостям базы на момент проверки и не заменяет ревью приложения.

## Границы воспроизводимости

Locks фиксируют Python-пакеты, но не весь образ побайтно: `python:3.12-slim`,
Debian-пакеты, образы Caddy/PostgreSQL и версии GitHub Actions имеют собственный
цикл обновлений. Перед публикацией нужно собрать, проверить и сохранить digest
конкретного образа выпуска. Этот этап не подменяет проверку Linux/Docker/БД.

Основания: [uv: locking environments](https://docs.astral.sh/uv/pip/compile/),
[pip: secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/),
[pip-audit](https://github.com/pypa/pip-audit).
