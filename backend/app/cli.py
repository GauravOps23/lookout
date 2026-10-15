"""Admin bootstrap: python -m app.cli create-admin <login> [--name "Display Name"]"""
import argparse
import getpass
import sys

from sqlalchemy import select

from app.audit.service import record
from app.auth import passwords
from app.db import session_factory
from app.models import User


def create_admin(login: str, display_name: str, password: str) -> None:
    if (err := passwords.validate_strength(password)):
        sys.exit(err)
    with session_factory()() as db:
        if db.scalar(select(User).where(User.login == login.lower())):
            sys.exit(f"User '{login}' already exists")
        u = User(login=login.lower(), display_name=display_name, is_admin=True,
                 password_hash=passwords.hash_password(password), must_change_pw=False)
        db.add(u)
        db.commit()
        record(db, "user.create", target_type="user", target_id=u.id, detail={"login": u.login, "via": "cli"})
    print(f"Admin '{login}' created.")


def main() -> None:
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-admin")
    c.add_argument("login")
    c.add_argument("--name", default="")
    sub.add_parser("gen-master-key")
    args = p.parse_args()
    if args.cmd == "gen-master-key":
        from app.secrets.vault import Vault

        print(Vault.generate_key())
        return
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Repeat: "):
        sys.exit("Passwords do not match")
    create_admin(args.login, args.name or args.login, pw)


if __name__ == "__main__":
    main()
