from pathlib import Path
import secrets

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import Base, engine
from app.core.security import hash_password, password_hash_needs_upgrade
from app.models.entities import UserAccount
from app.services.knowledge import KnowledgeService


def create_schema() -> None:
    Base.metadata.create_all(bind=engine)


def seed_data(db: Session) -> None:
    settings = get_settings()
    if db.query(UserAccount).count() == 0:
        admin = UserAccount(
            username="admin",
            display_name="Counselor Admin",
            password_hash=hash_password(settings.seed_admin_password or secrets.token_urlsafe(32)),
        )
        admin.roles = {"ROLE_ADMIN", "ROLE_USER"}
        student = UserAccount(
            username="student",
            display_name="Demo Student",
            password_hash=hash_password(settings.seed_student_password or secrets.token_urlsafe(32)),
        )
        student.roles = {"ROLE_USER"}
        db.add_all([admin, student])
        db.commit()
    else:
        credentials = {
            "admin": settings.seed_admin_password,
            "student": settings.seed_student_password,
        }
        upgraded = False
        for username, password in credentials.items():
            account = db.query(UserAccount).filter(UserAccount.username == username).first()
            if password and account is not None and password_hash_needs_upgrade(account.password_hash):
                account.password_hash = hash_password(password)
                upgraded = True
        if upgraded:
            db.commit()

    service = KnowledgeService(db, get_settings())
    root = Path(__file__).resolve().parents[1]
    for file in sorted((root / "knowledge").glob("*.md")):
        service.ensure_source(file.name, file.read_text(encoding="utf-8"))
