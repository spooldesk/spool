"""Append a labelled, offline AI practice dataset to the canonical local preview.

Explicit invocation only: pipe this file into the preview container's Python with
--apply. Never rebuilds a database, modifies existing records or calls channels/AI.
The manifest makes a second invocation a no-op and records every inserted row ID.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MARKER = "preview_ai_practice_v1"
PREFIX = "[AI TEST]"
NAMES = ["Анна Миронова", "Денис Волков", "Мария Соколова", "Илья Орлов",
         "Ольга Лебедева", "Алексей Морозов", "София Белова", "Михаил Павлов",
         "Елена Громова", "Роман Ковалёв", "Дарья Новикова", "Никита Власов",
         "Виктория Зорина", "Павел Медведев", "Алина Фомина", "Артём Лазарев",
         "Ирина Серова", "Максим Крылов", "Nora Reed", "Liam Stone",
         "Emma Brooks", "Lucas Gray", "Mia Grant", "Noah Hill"]

# Synthetic case facts, useful for search, summaries, drafts and recurring issues.
CASES = [
    ("Network", "VPN обрывается после обновления",
     "После обновления ноутбука VPN отключается каждые 15 минут. Без VPN работа с CRM невозможна. На домашней сети ошибка повторяется.",
     "Проверим версию клиента и DNS. Пришлите время последнего обрыва и версию приложения; пароли и ключи не нужны.",
     "Причиной был старый профиль VPN. После импорта нового профиля связь стабильна."),
    ("Access", "Приглашение в команду не открывается",
     "Коллега переслал приглашение, но при входе вижу, что ссылка истекла. Нужен доступ к проекту Atlas для нового сотрудника.",
     "Попросите администратора отправить новое приглашение на ваш адрес. Доступ выдаётся после подтверждения заявки владельцем команды.",
     "Администратор выдал новое приглашение, сотрудник вошёл и подтвердил доступ."),
    ("Software", "Экспорт отчёта зависает на 90%",
     "Отчёт за месяц с 12000 строк зависает на 90%. Маленький отчёт за день скачивается. Формат CSV, воспроизводится в двух браузерах.",
     "Пока выгрузите отчёт по неделям. Передали разработчикам размер выборки и время ошибки; новый срок сообщим после диагностики.",
     "Исправлена обработка больших выборок. Повторный CSV-экспорт 12000 строк завершился."),
    ("Hardware", "Не работает второй монитор через док-станцию",
     "Ноутбук видит только один монитор. После отключения док-станции проблема исчезает. Замена HDMI-кабеля не помогла.",
     "Проверим питание док-станции и обновление её прошивки. Укажите модель устройства; временно подключите монитор напрямую.",
     "После обновления прошивки док-станции оба монитора определяются."),
    ("Software", "Webhook доставляется дважды",
     "После timeout один заказ приходит в нашу CRM дважды. Идентификатор события одинаковый. Нужен способ избежать дублей.",
     "При повторной доставке проверяйте event_id. Получатель должен сохранить обработанный ID и вернуть 2xx без повторной обработки.",
     "Добавлена дедупликация по event_id; повторная доставка больше не создаёт второй заказ."),
    ("Other", "Не сходится количество лицензий в счёте",
     "В учебном счёте INV-DEMO-104 указаны 12 мест, но в команде 10 активных сотрудников. Два аккаунта отключены вчера.",
     "Сверим дату отключения с расчётным периодом. Подготовим расчёт; изменение счёта и возврат требуют отдельного согласования.",
     "Разница объясняется датой отключения после начала периода. Расчёт согласован с заказчиком."),
    ("Access", "Письмо для восстановления доступа не приходит",
     "Запрашивал восстановление доступа три раза. Во входящих письма нет. Почта работает, другие письма получаю.",
     "Проверьте папку спама и правильность адреса. Укажите время последней попытки; пароль и одноразовый код отправлять не нужно.",
     "Письмо попало под правило фильтрации. После исправления правила восстановление прошло успешно."),
    ("Network", "В офисе медленно открывается CRM",
     "Страницы CRM открываются 20 секунд. Через мобильный интернет быстро. Проблема у нескольких сотрудников одного офиса.",
     "Сравним задержки и DNS офисной сети. Укажите примерное время начала проблемы и затронутые рабочие места.",
     "Устранена перегрузка офисного шлюза, время открытия страниц вернулось к норме."),
    ("Software", "Поиск не находит недавно добавленный документ",
     "Документ есть в папке и открывается по прямой ссылке, но поиск его не находит. Загружен сегодня, права проверены.",
     "Проверим очередь индексации. Пока используйте прямую ссылку; пришлите название документа и время загрузки без его содержимого.",
     "Очередь индексации восстановлена, документ появился в результатах поиска."),
    ("Other", "Нужен отчёт по обращениям команды",
     "Для еженедельной встречи нужен отчёт по новым и закрытым обращениям, медиане ответа и незавершённой очереди.",
     "Уточните период и команду. Новые обращения считаем по дате создания, закрытые — по дате закрытия; текущую очередь показываем отдельно.",
     "Подготовлен отчёт с указанным периодом и отдельной текущей очередью."),
    ("Access", "New colleague cannot open the project",
     "Our new colleague can sign in, but project Atlas is not visible. The invitation was accepted yesterday. Please check team membership.",
     "Please ask the team owner to confirm the intended project and access level. We will check membership without requesting passwords.",
     "The team owner approved membership and the project is now visible."),
    ("Software", "CSV export changes the date format",
     "Dates in the exported CSV appear in US format although the dashboard uses day/month/year. We need a consistent format for our import.",
     "Please confirm the expected date format and timezone. Meanwhile use ISO dates in the import mapping to avoid ambiguous day/month values.",
     "The import mapping now uses ISO dates; the sample file was checked successfully."),
]


async def seed(session, *, now=None):
    """One transaction, inserts only; the caller owns commit/rollback."""
    from sqlalchemy import select
    from src.database.models import (AdminUser, AppSetting, Article, Message,
                                     Team, Ticket, TicketCategory, User)
    from src.database.workspace_models import CustomerProfile

    existing = await session.get(AppSetting, MARKER)
    if existing:
        return {**json.loads(existing.value), "already_present": True}
    now = now or datetime.utcnow()
    teams = list((await session.execute(select(Team).order_by(Team.id).limit(3))).scalars())
    categories = list((await session.execute(select(TicketCategory.name)
                      .where(TicketCategory.is_active.is_(True)))).scalars())
    owner = (await session.execute(select(AdminUser).where(AdminUser.role == "owner",
             AdminUser.is_active.is_(True)).order_by(AdminUser.id).limit(1))).scalar_one_or_none()
    if not teams or not categories or owner is None:
        raise ValueError("Preview must already have an owner, team and active category")
    manifest = {"version": 1, "created_at_utc": now.isoformat(), "users": [],
                "profiles": [], "tickets": [], "messages": [], "articles": [],
                "teams_reused": [t.id for t in teams], "examples": {}}
    customers = []
    for index, name in enumerate(NAMES):
        created = now - timedelta(days=75-index)
        profile = CustomerProfile(name=f"{PREFIX} {name}", created_at=created)
        session.add(profile)
        await session.flush()
        # Random widget bearer tokens are discarded. No externally routable identity.
        user = User(customer_profile_id=profile.id, tg_id=-secrets.randbelow(2**60)-1,
            channel="widget", channel_user_id=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
            username=f"{PREFIX} {name}", email=f"ai-practice-{index+1}@example.test",
            external_id=f"ai-practice-v1-{index+1}", language="en" if index >= 16 else "ru",
            team_id=teams[index % len(teams)].id if index < 18 else None,
            is_verified=index < 18, identity_verified=False, is_active=True, created_at=created)
        session.add(user)
        await session.flush()
        customers.append(user)
        manifest["profiles"].append(profile.id)
        manifest["users"].append(user.id)

    for index in range(72):
        user = customers[index % 18]
        category, title, question, reply, resolution = CASES[index % len(CASES)]
        category = category if category in categories else categories[0]
        closed = index < 48
        active_index = index - 48
        stage = "new" if closed or active_index < 8 else "in_progress" if active_index < 16 else "waiting"
        if closed:
            days = 31 + index if index < 24 else 1 + (index-24)
            created = now - timedelta(days=days, hours=8)
        elif active_index < 4:
            created = now - timedelta(minutes=2+active_index)
        else:
            created = now - timedelta(hours=5+active_index*3)
        answered = closed or stage != "new"
        reply_at = created + timedelta(minutes=15 + (index % 6)*25)
        ended = created + timedelta(hours=3 + index % 6) if closed else None
        breached = not closed and stage != "waiting" and active_index >= 4
        ticket = Ticket(user_id=user.id, team_id=user.team_id, category=category,
            status="closed" if closed else "active", stage=stage,
            conversation_key=f"ai-practice-v1-{index+1}", ticket_code=f"AI-TEST-{index+1:03d}",
            assigned_admin_id=owner.id if answered else None, created_at=created, closed_at=ended,
            notification_sent=True, telegram_id=user.tg_id,
            sla_breached_at=created+timedelta(hours=2) if breached else None,
            custom_reason=f"{PREFIX} {resolution}" if closed else None,
            processing_time_seconds=int((ended-created).total_seconds()) if closed else None,
            csat_rating=(5, 4, 5, 3, 4, 2)[index % 6] if closed else None,
            csat_rated_at=ended+timedelta(minutes=10) if closed else None)
        session.add(ticket)
        await session.flush()
        manifest["tickets"].append(ticket.id)
        if index in (48, 52, 56, 64):
            manifest["examples"][{48:"unassigned", 52:"overdue", 56:"in_progress", 64:"waiting"}[index]] = ticket.id

        messages = [Message(ticket_id=ticket.id, sender_type="user", sender_id=user.tg_id,
                    text=f"{PREFIX} {title}\n{question}\nУчебное обращение {index+1:03d}.", created_at=created)]
        if answered:
            first_reply = Message(ticket_id=ticket.id, sender_type="admin", sender_id=owner.id,
                operator_id=owner.id, sender_label="Учебный оператор", text=reply,
                created_at=reply_at, delivered_at=reply_at, delivery_status="delivered")
            messages.append(first_reply)
            messages.append(Message(ticket_id=ticket.id, sender_type="admin", sender_id=owner.id,
                operator_id=owner.id, is_internal=True, text=f"{PREFIX} Учебный кейс: {title}. Проверить факты до обещания сроков.",
                created_at=reply_at+timedelta(minutes=1), delivery_status="delivered"))
        if closed:
            messages.extend([
                Message(ticket_id=ticket.id, sender_type="admin", sender_id=owner.id, operator_id=owner.id,
                    text=resolution, created_at=ended-timedelta(minutes=10), delivered_at=ended-timedelta(minutes=10)),
                Message(ticket_id=ticket.id, sender_type="user", sender_id=user.tg_id,
                    text="Проверил, проблема решена. Спасибо!", created_at=ended-timedelta(minutes=5))])
        elif stage == "in_progress":
            messages.append(Message(ticket_id=ticket.id, sender_type="user", sender_id=user.tg_id,
                text="Проверил рекомендации: проблема сохраняется. Когда ждать следующий шаг?",
                created_at=reply_at+timedelta(minutes=30)))
        session.add_all(messages)
        await session.flush()
        manifest["messages"].extend(m.id for m in messages)

    for index, (category, title, _, reply, resolution) in enumerate(CASES[:5]):
        article = Article(slug=f"ai-practice-v1-{index+1}", title=f"{PREFIX} {title}",
            summary="Учебный материал для проверки AI-ассистента.",
            body=f"Тестовый сценарий, не инструкция для реальных клиентов.\n\nДиагностика: {reply}\n\nПример решения: {resolution}",
            category=category if category in categories else categories[0], is_published=False,
            created_by=owner.id, created_at=now, updated_at=now)
        session.add(article)
        await session.flush()
        manifest["articles"].append(article.id)
    session.add(AppSetting(key=MARKER, value=json.dumps(manifest), updated_by=0))
    await session.flush()
    return manifest


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Append the labelled practice dataset")
    args = parser.parse_args()
    if not args.apply:
        parser.error("Nothing changed. Use --apply only for the requested local preview dataset.")
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from src.database.db import db_session, settings
    url = make_url(settings.database_url)
    if os.environ.get("DEMO_MODE") != "1" or url.get_backend_name() != "postgresql" or url.host != "postgres" or url.database != "spooldesk_preview":
        raise RuntimeError("Refusing to seed anything except the canonical local preview")
    async with db_session(write=True) as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(782491601)"))
        manifest = await seed(session)
    print(json.dumps({"marker": MARKER, "already_present": manifest.get("already_present", False),
        "counts": {k:len(manifest[k]) for k in ("users", "profiles", "tickets", "messages", "articles")},
        "examples": manifest["examples"], "ticket_range": [min(manifest["tickets"]), max(manifest["tickets"])]}))


if __name__ == "__main__":
    asyncio.run(main())
