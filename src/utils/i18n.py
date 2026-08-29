"""Internationalization (i18n) support for the bot."""

from typing import Dict

# Internal system marker written to the transcript when a ticket is closed.
# NOT user-facing (the user gets the localized 'ticket_closed_user' text). Kept
# as a STABLE constant because other code detects closure by matching it
# (dashboard last-message logic, AI history filtering). Do not localize.
CLOSURE_SYSTEM_TEXT = "Заявка закрита адміністратором."
CLOSURE_SYSTEM_PREFIX = "Заявка закрита"

# Supported languages
SUPPORTED_LANGUAGES = {
    "ru": "Русский",
    "uk": "Українська",
    "en": "English",
}

# Default language (mutable at runtime via set_runtime_default_language)
DEFAULT_LANGUAGE = "uk"


def set_runtime_default_language(code: str) -> None:
    """Override the process-wide fallback language used by get_text().

    Called once at startup from the configured setting. get_text() stays sync
    and fast; explicit per-user languages are unaffected — this only changes the
    fallback for users who haven't picked a language yet.
    """
    global DEFAULT_LANGUAGE
    if code in SUPPORTED_LANGUAGES:
        DEFAULT_LANGUAGE = code

# Translations dictionary
TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "ru": {
        # Registration and language selection
        "select_language": "Выберите язык:",
        "language_set": "Язык установлен: Русский",
        "account_created": "Ваш аккаунт создан. Ожидайте подтверждения администратора.",
        "account_not_linked": "Ваш аккаунт еще не привязан к команде. Ожидайте подтверждения администратора.",
        "account_blocked": "Ваш аккаунт заблокирован. Обратитесь к администратору.",
        "account_approved": "✅ Администратор подтвердил ваш аккаунт.\n\nТеперь вы можете писать своё обращение.",
        "category_unavailable": "Категория недоступна, выберите снова.",
        "invalid_language": "Неверный язык",
        "attachment_only": "📎 Файл",
        
        # Password verification
        "enter_password": "Введите пароль для доступа:",
        "password_accepted": "Пароль принят. Выберите язык:",
        "wrong_password": "Неверный пароль. Попробуйте еще раз.",
        "verification_success": "Проверка успешна! Нажмите кнопку ниже для создания нового запроса.",
        
        # Main menu
        "new_request": "Новый запрос",
        "create_new_request": "Для создания нового запроса нажмите кнопку 'новый запрос' ниже.",
        "authorized": "Вы успешно авторизованы. Нажмите кнопку ниже для создания нового запроса.",
        
        # Ticket creation
        "select_category": "Какое ваше вопрос? Выберите категорию ниже.",
        "category_selected": "Категория: **{category}**\n\nОпишите ваш вопрос детальнее:",
        "describe_problem": "Опишите вашу проблему — что случилось? Можно текстом, скриншотом или файлом.",
        "send_text_or_file": "Пожалуйста, отправьте текст или файл для описания вопроса.",
        "message_sent": "Ваше сообщение отправлено в {brand}",
        "ticket_closed_user": "Ваш запрос №{id} был закрыт.",
        "csat_invite": "Оцените, пожалуйста, качество поддержки:",
        "csat_thanks": "Спасибо за оценку!",
        "csat_already": "Вы уже оценили этот запрос.",
        "deflect_prefix": "🤖 Возможно, это решит ваш вопрос:",
        "deflect_question": "Помог ли этот ответ?",
        "deflect_solved_btn": "✅ Да, решено",
        "deflect_solved_ack": "Рады помочь! Запрос закрыт — пишите снова, если что-то ещё.",
        "deflect_operator_btn": "🙋 Нужен оператор",
        "deflect_operator_wait": "Принято! Оператор уже видит вашу заявку и скоро ответит.",
        "access_restricted": "Ваш доступ к сервису ограничен администраторами.",
        "access_restored": "Ваш доступ восстановлен. Вы снова можете обращаться в {brand}.",
        "enter_password_first": "Сначала введите пароль",
        "active_ticket_exists": "У вас уже есть активный запрос №{ticket_id}. Пожалуйста, подождите ответа или закройте текущий запрос.",
        
        # Categories
        "category_vpn": "VPN",
        "category_server": "Server",
        "category_password": "1pass/gmail/telegram",
        "category_other": "Другое",
        
        # Team assignment
        "team_assigned": "✅ Ваш аккаунт привязан к команде '{team_name}'.\nВаш ID в команде: {local_user_id}\n\nТеперь вы можете создавать запросы.",
    },
    "uk": {
        # Registration and language selection
        "select_language": "Виберіть мову:",
        "language_set": "Мову встановлено: Українська",
        "account_created": "Ваш обліковий запис створено. Очікуйте підтвердження адміністратора.",
        "account_not_linked": "Ваш обліковий запис ще не прив'язано до команди. Очікуйте підтвердження адміністратора.",
        "account_blocked": "Ваш обліковий запис заблоковано. Зверніться до адміністратора.",
        "account_approved": "✅ Адміністратор підтвердив ваш обліковий запис.\n\nТепер ви можете написати своє звернення.",
        "category_unavailable": "Категорія недоступна, оберіть ще раз.",
        "invalid_language": "Невірна мова",
        "attachment_only": "📎 Файл",
        
        # Password verification
        "enter_password": "Введіть пароль для доступу:",
        "password_accepted": "Пароль прийнято. Виберіть мову:",
        "wrong_password": "Невірний пароль. Спробуйте ще раз.",
        "verification_success": "Перевірка успішна! Натисніть кнопку нижче для створення нового запиту.",
        
        # Main menu
        "new_request": "Новий запит",
        "create_new_request": "Для створення нового запиту натисніть кнопку 'новий запит' нижче.",
        "authorized": "Ви успішно авторизовані. Натисніть кнопку нижче для створення нового запиту.",
        
        # Ticket creation
        "select_category": "Яке ваше питання? Виберіть категорію нижче.",
        "category_selected": "Категорія: **{category}**\n\nОпишіть ваше питання детальніше:",
        "describe_problem": "Опишіть вашу проблему — що сталося? Можна текстом, скриншотом або файлом.",
        "send_text_or_file": "Будь ласка, надішліть текст або файл для опису питання.",
        "message_sent": "Ваше повідомлення відправлено до {brand}",
        "ticket_closed_user": "Ваш запит №{id} було закрито.",
        "csat_invite": "Оцініть, будь ласка, якість підтримки:",
        "csat_thanks": "Дякуємо за оцінку!",
        "csat_already": "Ви вже оцінили цей запит.",
        "deflect_prefix": "🤖 Можливо, це вирішить ваше питання:",
        "deflect_question": "Чи допомогла ця відповідь?",
        "deflect_solved_btn": "✅ Так, вирішено",
        "deflect_solved_ack": "Раді допомогти! Запит закрито — пишіть знову, якщо щось ще.",
        "deflect_operator_btn": "🙋 Потрібен оператор",
        "deflect_operator_wait": "Прийнято! Оператор вже бачить вашу заявку і скоро відповість.",
        "access_restricted": "Ваш доступ до сервісу було обмежено адміністраторами.",
        "access_restored": "Ваш доступ до сервісу було відновлено. Ви знову можете звертатися до {brand}.",
        "enter_password_first": "Спочатку введіть пароль",
        "active_ticket_exists": "У вас вже є активний запит №{ticket_id}. Будь ласка, зачекайте на відповідь або закрийте поточний запит.",
        
        # Categories
        "category_vpn": "VPN",
        "category_server": "Server",
        "category_password": "1pass/gmail/telegram",
        "category_other": "Інше питання",
        
        # Team assignment
        "team_assigned": "✅ Ваш обліковий запис прив'язано до команди '{team_name}'.\nВаш ID в команді: {local_user_id}\n\nТепер ви можете створювати запити.",
    },
    "en": {
        # Registration and language selection
        "select_language": "Select language:",
        "language_set": "Language set: English",
        "account_created": "Your account has been created. Please wait for administrator confirmation.",
        "account_not_linked": "Your account is not yet linked to a team. Please wait for administrator confirmation.",
        "account_blocked": "Your account is blocked. Contact the administrator.",
        "account_approved": "✅ An administrator approved your account.\n\nYou can now describe your issue.",
        "category_unavailable": "That category is no longer available, please pick again.",
        "invalid_language": "Unknown language",
        "attachment_only": "📎 File",
        
        # Password verification
        "enter_password": "Enter password for access:",
        "password_accepted": "Password accepted. Select language:",
        "wrong_password": "Wrong password. Please try again.",
        "verification_success": "Verification successful! Click the button below to create a new request.",
        
        # Main menu
        "new_request": "New Request",
        "create_new_request": "To create a new request, click the 'new request' button below.",
        "authorized": "You are successfully authorized. Click the button below to create a new request.",
        
        # Ticket creation
        "select_category": "What is your question? Select a category below.",
        "category_selected": "Category: **{category}**\n\nDescribe your question in more detail:",
        "describe_problem": "Describe your problem — what happened? Text, screenshot, or file all work.",
        "send_text_or_file": "Please send text or file to describe the issue.",
        "message_sent": "Your message has been sent to {brand}",
        "ticket_closed_user": "Your request #{id} has been closed.",
        "csat_invite": "Please rate the quality of support:",
        "csat_thanks": "Thank you for your feedback!",
        "csat_already": "You have already rated this request.",
        "deflect_prefix": "🤖 This might solve your issue:",
        "deflect_question": "Did this answer help?",
        "deflect_solved_btn": "✅ Yes, solved",
        "deflect_solved_ack": "Glad that helped — this is closed. Write again any time.",
        "deflect_operator_btn": "🙋 Need an operator",
        "deflect_operator_wait": "Got it! An operator can already see your request and will reply soon.",
        "access_restricted": "Your access to the service has been restricted by administrators.",
        "access_restored": "Your access has been restored. You can contact {brand} again.",
        "enter_password_first": "Please enter the password first",
        "active_ticket_exists": "You already have an active request #{ticket_id}. Please wait for a response or close the current request.",
        
        # Categories
        "category_vpn": "VPN",
        "category_server": "Server",
        "category_password": "1pass/gmail/telegram",
        "category_other": "Other",
        
        # Team assignment
        "team_assigned": "✅ Your account has been linked to team '{team_name}'.\nYour ID in the team: {local_user_id}\n\nYou can now create requests.",
    },
}


def get_text(key: str, language: str | None = None, **kwargs) -> str:
    """Get translated text by key.
    
    Args:
        key: Translation key
        language: Language code ('ru', 'uk', 'en') or None for default
        **kwargs: Format arguments for the translation string
    
    Returns:
        Translated and formatted string
    """
    if language is None:
        language = DEFAULT_LANGUAGE
    
    if language not in TRANSLATIONS:
        language = DEFAULT_LANGUAGE
    
    text = TRANSLATIONS[language].get(key, TRANSLATIONS[DEFAULT_LANGUAGE].get(key, key))
    
    # Format string if kwargs provided
    if kwargs:
        try:
            text = text.format(**kwargs)
        except KeyError:
            # If formatting fails, return text as is
            pass
    
    return text


def get_language_keyboard() -> Dict[str, str]:
    """Get language selection keyboard labels."""
    return {
        "ru": "🇷🇺 Русский",
        "uk": "🇺🇦 Українська",
        "en": "🇬🇧 English",
    }

