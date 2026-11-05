"""Central API translations; audit/user content remains recorded source data."""

from starlette.requests import Request

ERRORS: dict[str, tuple[str, str]] = {
    "request_revision_required": (
        "Откройте заявку и проверьте последние данные перед подтверждением.",
        "Open the application and review its current details before approving.",
    ),
    "request_changed": (
        "Данные заявки изменились. Откройте её заново и проверьте соответствие.",
        "The application changed. Reopen it and check the match again.",
    ),
    "student_claim_number_unavailable": (
        "Заявка с этим номером уже существует. "
        "Проверьте свою заявку или обратитесь к администратору.",
        "An application with this student number exists. "
        "Check your request or contact an administrator.",
    ),
    "invalid_student_claim_group": (
        "Выберите учебную группу из списка.",
        "Choose a study group from the list.",
    ),
    "student_email_required": (
        "Укажите адрес электронной почты учебного заведения.",
        "Enter your institutional email address.",
    ),
    "student_email_domain": (
        "Укажите адрес электронной почты разрешённого учебного домена.",
        "Use an email address from the permitted institutional domain.",
    ),
    "student_match_confirmation_required": (
        "Проверьте и подтвердите соответствие выбранного студента заявке.",
        "Check and confirm that the selected student matches the application.",
    ),
    "student_claim_mismatch": (
        "Данные заявки отличаются от записи студента. Проверьте и отдельно подтвердите различия.",
        "The application differs from the student record. "
        "Review and explicitly confirm the differences.",
    ),
    "student_claim_locked": (
        "Подтверждённую связь со студентом нельзя изменить самостоятельно.",
        "You cannot change a verified student link yourself.",
    ),
    "deadline_in_past": (
        "Укажите будущий срок сдачи. Дата и время должны быть позже текущего времени.",
        "Choose a future deadline. Its date and time must be later than now.",
    ),
    "invalid_search_filter": (
        "Проверьте явно указанные логин, IP, действие или объект. "
        "Для каждого поля укажите одно значение.",
        "Check the explicit login, IP, action or object. Use one valid value for each field.",
    ),
    "approval_pending": (
        "Заявка ожидает одобрения администратора.",
        "Your application is awaiting administrator approval.",
    ),
    "approval_rejected": (
        "Заявка отклонена. Причина указана на странице статуса.",
        "Your application was rejected. See the reason on your status page.",
    ),
    "request_reviewed": (
        "Заявка уже рассмотрена. Обновите список.",
        "This application has already been reviewed. Refresh the list.",
    ),
    "invalid_student_link": (
        "Выберите существующего студента для аккаунта с ролью «Студент».",
        "Select an existing student for an account with the student role.",
    ),
    "student_already_linked": (
        "Эта запись студента уже связана с другим аккаунтом.",
        "This student record is already linked to another account.",
    ),
    "student_unlinked": (
        "Администратор должен связать аккаунт с записью студента.",
        "An administrator must link your account to a student record.",
    ),
    "version_conflict": (
        "Запись изменилась. Обновите страницу перед сохранением.",
        "The record has changed. Refresh the page before saving.",
    ),
    "scope_locked": (
        "Нельзя изменить учебную область задания после сдачи работ.",
        "A task's teaching scope cannot change after work has been submitted.",
    ),
    "invalid_task": (
        "Проверьте предмет, группу, семестр и срок после публикации.",
        "Check the subject, group, term and deadline after publication.",
    ),
    "invalid_submission": (
        "Введите текст работы или прикрепите файл.",
        "Enter submission text or attach a file.",
    ),
    "unauthenticated": ("Войдите в систему.", "Sign in to continue."),
    "invalid_credentials": ("Неверный логин или пароль.", "Incorrect login or password."),
    "forbidden": (
        "У вас нет доступа к этому действию или этим данным.",
        "You do not have access to this action or data.",
    ),
    "csrf_failed": (
        "Проверка безопасности запроса не пройдена. Обновите страницу.",
        "Request security check failed. Refresh the page.",
    ),
    "session_expired": (
        "Срок сессии истёк. Войдите снова.",
        "Your session expired. Sign in again.",
    ),
    "session_blocked": (
        "Сессия заблокирована администратором.",
        "This session was blocked by an administrator.",
    ),
    "session_conflict": (
        "Состояние сессии не позволяет это действие. Обновите список.",
        "The session state does not allow this action. Refresh the list.",
    ),
    "rate_limited": (
        "Слишком много попыток входа. Повторите позже.",
        "Too many sign-in attempts. Try again later.",
    ),
    "invalid_request": (
        "Проверьте формат и допустимые значения полей.",
        "Check the format and allowed values of the fields.",
    ),
    "server_error": (
        "Сервер не смог обработать запрос. Повторите позже.",
        "The server could not process the request. Try again later.",
    ),
    "not_found": ("Запись или файл не найдены.", "The record or file was not found."),
    "login_unavailable": ("Этот логин недоступен.", "This login is unavailable."),
    "invalid_role": ("Выберите допустимую роль.", "Choose a valid role."),
    "invalid_permission": (
        "Неизвестное или недопустимое право доступа.",
        "Unknown or unsupported permission.",
    ),
    "last_admin": (
        "Нельзя отключить последнего активного администратора.",
        "The last active administrator cannot be disabled.",
    ),
    "admin_permissions": (
        "Администратор должен сохранить права управления.",
        "The administrator must retain management permissions.",
    ),
    "approval_required": (
        "Заявка ожидает одобрения администратора. Проверьте статус заявки.",
        "The application requires administrator approval. Check the application status.",
    ),
    "approval_conflict": (
        "Состояние заявки изменилось. Обновите список.",
        "The application state changed. Refresh the list.",
    ),
    "invalid_student": (
        "Выберите существующую запись студента.",
        "Select an existing student record.",
    ),
    "student_link_conflict": (
        "Эта запись студента уже связана с другим аккаунтом.",
        "This student record is already linked to another account.",
    ),
    "student_link_required": (
        "Администратор должен связать аккаунт с записью студента.",
        "An administrator must link this account to a student record.",
    ),
    "student_claim_required": (
        "Сначала заполните данные в заявке студента.",
        "Complete the student application details first.",
    ),
    "invalid_student_group": (
        "Выберите существующую учебную группу из справочника.",
        "Select an existing study group from the catalogue.",
    ),
    "student_group_confirmation_required": (
        "Проверьте и выберите правильную группу из справочника.",
        "Review and select the correct group from the catalogue.",
    ),
    "student_selection_required": (
        "Найдена возможная запись студента. Выберите её или подтвердите, что однофамильцы "
        "с разными кодами — разные люди. Совпадающий код требует выбора существующей записи.",
        "A possible student record exists. Select it or verify that same-name students with "
        "different codes are distinct people. A matching code requires an existing record.",
    ),
    "invalid_student_creation": (
        "Для роли студента выберите создание записи или связь с существующей записью.",
        "For a student role, choose either record creation or an existing record link.",
    ),
    "invalid_assignment": (
        "Проверьте преподавателя, предмет, группу и семестр.",
        "Check the teacher, subject, group and term.",
    ),
    "assignment_conflict": (
        "Такое сочетание преподавателя, предмета, группы и периода уже назначено.",
        "This teacher, subject, group and term combination is already assigned.",
    ),
    "teacher_not_found": (
        "Преподаватель или заявка не найдены.",
        "The teacher or application was not found.",
    ),
    "teacher_request_required": ("Выберите заявку преподавателя.", "Select a teacher application."),
    "teacher_role_required": (
        "Заявку преподавателя можно одобрить только с ролью преподавателя.",
        "A teacher application can only be approved with the teacher role.",
    ),
    "invalid_teacher_filter": (
        "Выберите допустимый статус, предмет или группу.",
        "Choose a valid status, subject or group.",
    ),
    "catalog_conflict": (
        "Такая запись справочника уже существует.",
        "This catalogue entry already exists.",
    ),
    "invalid_catalog": (
        "Проверьте название и учебный год справочника.",
        "Check the catalogue name and academic year.",
    ),
    "inactive_catalog": (
        "Для нового действия выберите активные записи справочника.",
        "Choose active catalogue entries for a new action.",
    ),
    "grade_conflict": (
        "Оценка уже изменена. Обновите страницу перед сохранением.",
        "The grade has changed. Refresh the page before saving.",
    ),
    "invalid_file": (
        "Недопустимое имя, тип или содержимое файла. Допустимы PDF, TXT, PNG и JPG.",
        "Invalid filename, type or content. Allowed: PDF, TXT, PNG and JPG.",
    ),
    "file_too_large": ("Файл превышает допустимый размер.", "The file exceeds the size limit."),
    "invalid_document": (
        "Некорректный файл или область доступа документа.",
        "The document file or access scope is invalid.",
    ),
    "deadline_passed": (
        "Срок сдачи истёк. Поздняя сдача закрыта.",
        "The deadline has passed. Late submission is closed.",
    ),
    "task_closed": (
        "Задание ещё не опубликовано или сдача закрыта.",
        "The task is not published or submissions are closed.",
    ),
    "task_conflict": (
        "Задание изменилось. Обновите страницу.",
        "The task has changed. Refresh the page.",
    ),
    "submission_conflict": (
        "Работа изменена. Обновите страницу перед проверкой.",
        "The submission has changed. Refresh before reviewing.",
    ),
    "model_unavailable": (
        "Локальная модель AI недоступна. Остальные разделы продолжают работать.",
        "The local AI model is unavailable. Other sections remain available.",
    ),
    "invalid_model_output": (
        "Ответ модели не соответствует схеме. Уточните запрос и повторите.",
        "The model response did not match the schema. Refine the question.",
    ),
    "unclear_question": (
        "Уточните время, действие, логин, IP или тему причины события.",
        "Specify a time, action, login, IP or recorded reason topic.",
    ),
    "no_evidence": (
        "Нет записей аудита для этого рассмотрения.",
        "No audit evidence is available for this review.",
    ),
}


def request_locale(request: Request) -> str:
    for part in request.headers.get("accept-language", "kk").split(","):
        language = part.split(";", 1)[0].strip().split("-", 1)[0].lower()
        if language in {"kk", "ru", "en"}:
            return language
    return "kk"


def error_message(code: str, original: str, locale: str) -> str:
    if locale == "kk":
        return original
    default = (
        "Не удалось выполнить запрос. Проверьте поля и повторите.",
        "The request could not be completed. Check the fields and try again.",
    )
    return ERRORS.get(code, default)[0 if locale == "ru" else 1]


ACTION_TRANSLATIONS = {
    "login": ("вход", "sign-in"),
    "logout": ("выход", "sign-out"),
    "password_change": ("изменение пароля", "password change"),
    "record_view": ("просмотр записи", "record view"),
    "record_update": ("изменение записи", "record update"),
    "export": ("экспорт", "export"),
    "grade_change": ("изменение оценки", "grade change"),
    "assignment_change": ("изменение учебной области", "teaching scope change"),
    "catalog_create": ("добавление в учебный справочник", "academic catalogue creation"),
    "catalog_update": ("изменение учебного справочника", "academic catalogue change"),
    "catalog_archive": ("архивирование учебного справочника", "academic catalogue archiving"),
    "document_upload": ("загрузка документа", "document upload"),
    "document_view": ("просмотр документа", "document view"),
    "document_delete": ("удаление документа", "document deletion"),
    "user_register": ("подача заявки", "registration request"),
    "user_change": ("изменение аккаунта", "account change"),
    "role_change": ("изменение роли", "role change"),
    "permission_change": ("изменение прав", "permission change"),
    "user_approve": ("одобрение заявки", "application approval"),
    "user_reject": ("отклонение заявки", "application rejection"),
    "user_needs_review": ("заявка требует уточнения", "application needs further review"),
    "student_claim_update": ("уточнение данных студента", "student claim correction"),
    "student_create": ("создание записи студента", "student record creation"),
    "student_link": ("связь аккаунта студента", "student account link"),
    "session_block": ("блокировка сессии", "session block"),
    "session_unblock": ("снятие блокировки сессии", "session unblock"),
    "session_terminate": ("завершение сессии", "session termination"),
    "session_expired": ("истечение сессии", "session expiry"),
    "session_flagged": ("сигнал правила по сессии", "session rule flag"),
    "session_dismiss": ("ложное срабатывание", "false positive decision"),
    "session_ai_review": ("AI предложение для проверки", "AI review suggestion"),
    "access_denied": ("отказ в доступе", "access denial"),
    "admin_bootstrap": ("создание первого администратора", "first administrator creation"),
    "task_create": ("создание задания", "task creation"),
    "task_update": ("изменение задания", "task update"),
    "task_material_upload": ("загрузка материала", "task material upload"),
    "task_material_view": ("просмотр материала", "task material view"),
    "submission_submit": ("сдача работы", "work submission"),
    "submission_resubmit": ("повторная сдача", "work resubmission"),
    "submission_view": ("просмотр работы", "submission view"),
    "submission_file_view": ("просмотр файла работы", "submission file view"),
    "submission_review": ("оценка и отзыв", "submission grade and feedback"),
}
