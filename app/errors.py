"""Public errors never include model output, prompts or exception internals."""


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def invalid_output() -> AppError:
    return AppError(
        "invalid_model_output",
        "Модель жауабы рұқсат етілген JSON схемасына сай емес. Сұрақты нақтылап қайталаңыз.",
        502,
    )
