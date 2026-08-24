"""CLI to create/list/delete admin accounts.

Run inside the container so the password never touches .env or shell
history, e.g.:

    docker compose exec web-assurance-agent python -m triage_agent.manage_users create alice
    docker compose exec web-assurance-agent python -m triage_agent.manage_users list
    docker compose exec web-assurance-agent python -m triage_agent.manage_users delete alice
"""

import argparse
import getpass
import os
import sys

from triage_agent.auth_store import UserStore
from triage_agent.settings import Settings


def _store() -> UserStore:
    settings = Settings.from_mapping(os.environ)
    if settings.users_database_path is None:
        print(
            "TRIAGE_STATE_DATABASE_PATH is not set; there is nowhere to store accounts.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return UserStore(settings.users_database_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m triage_agent.manage_users")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create a new admin account")
    create.add_argument("username")

    reset = subparsers.add_parser("reset-password", help="Change an existing account's password")
    reset.add_argument("username")

    delete = subparsers.add_parser("delete", help="Delete an admin account")
    delete.add_argument("username")

    subparsers.add_parser("list", help="List admin usernames")

    args = parser.parse_args(argv)
    store = _store()
    try:
        if args.command == "create":
            password = _prompt_password()
            store.create_user(args.username, password)
            print(f"Created user {args.username!r}.")
        elif args.command == "reset-password":
            password = _prompt_password()
            store.set_password(args.username, password)
            print(f"Updated password for {args.username!r}.")
        elif args.command == "delete":
            store.delete_user(args.username)
            print(f"Deleted user {args.username!r}.")
        elif args.command == "list":
            for username in store.list_usernames():
                print(username)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


def _prompt_password() -> str:
    password = getpass.getpass("Password: ")
    if getpass.getpass("Confirm password: ") != password:
        print("Error: passwords did not match", file=sys.stderr)
        raise SystemExit(1)
    return password


if __name__ == "__main__":
    raise SystemExit(main())
