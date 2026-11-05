"""Create the first administrator using a password entered at a secure prompt."""

import argparse
import getpass
import warnings

from pydantic import ValidationError

from app.auth import hash_password, iso_utc, public_user, utc_now
from app.auth_routes import Registration
from app.config import Settings
from app.errors import AppError
from app.store import Store


def create_admin(store: Store, login: str, display_name: str, password: str) -> dict[str, object]:
    payload = Registration(login=login, display_name=display_name, password=password)
    store.initialize()
    encoded = hash_password(payload.password)
    with store.connect(write=True) as connection:
        if (
            connection.execute("SELECT 1 FROM Users WHERE role_code = 'admin' LIMIT 1").fetchone()
            is not None
        ):
            raise AppError(
                "bootstrap_complete", "Әкімші бұрын құрылған. Bootstrap қайта қолданылмайды.", 409
            )
        if (
            connection.execute(
                "SELECT 1 FROM Users WHERE login = ? COLLATE NOCASE", (payload.login,)
            ).fetchone()
            is not None
        ):
            raise AppError(
                "login_unavailable", "Бұл логин тіркелген; bootstrap парольді өзгертпейді.", 409
            )
        cursor = connection.execute(
            "INSERT INTO Users(login,display_name,password_hash,role_code,active,created_at,"
            "approval_status) "
            "VALUES (?,?,?,'admin',1,?,'approved')",
            (payload.login, payload.display_name, encoded, iso_utc(utc_now())),
        )
        store.audit(
            connection,
            None,
            "admin_bootstrap",
            "user",
            str(cursor.lastrowid),
            "success",
            "Бірінші әкімші қауіпсіз bootstrap командасымен құрылды.",
            "local",
        )
        user = connection.execute(
            "SELECT * FROM Users WHERE user_id = ?", (cursor.lastrowid,)
        ).fetchone()
        if user is None:
            raise RuntimeError("Inserted administrator not found")
        return public_user(connection, user)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Бірінші әкімшіні құру; құпиясөз командалық жолда берілмейді."
    )
    parser.add_argument("--login", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Құпиясөз (кемінде 12 таңба): ")
            confirmation = getpass.getpass("Құпиясөзді қайталаңыз: ")
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        parser.exit(1, "Құпиясөзді қауіпсіз енгізу үшін интерактивті терминалды қолданыңыз.\n")
    if password != confirmation:
        parser.exit(1, "Құпиясөздер сәйкес емес.\n")
    try:
        user = create_admin(Store(Settings()), args.login, args.name, password)
    except ValidationError:
        parser.exit(1, "Логин, ат немесе құпиясөз талапқа сай емес (құпиясөз: 12–128 таңба).\n")
    except AppError as exc:
        parser.exit(1, str(exc) + "\n")
    print(f"Әкімші құрылды: {user['login']}. Жүйеге кіруге болады.")


if __name__ == "__main__":
    main()
