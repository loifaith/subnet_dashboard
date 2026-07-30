#!/usr/bin/env python3
"""CLI to manage dashboard login users (auth_users.json).

Usage:
  python3 manage_users.py add <username>
  python3 manage_users.py remove <username>
  python3 manage_users.py list
"""
import getpass
import sys

import auth


def cmd_add(username: str) -> None:
    password = getpass.getpass(f"Password for {username}: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.")
        sys.exit(1)
    if len(password) < 8:
        print("Password must be at least 8 characters.")
        sys.exit(1)
    users = auth.load_users()
    users[username] = auth.hash_password(password)
    auth.save_users(users)
    print(f"User '{username}' saved.")


def cmd_remove(username: str) -> None:
    users = auth.load_users()
    if username not in users:
        print(f"No such user: {username}")
        sys.exit(1)
    del users[username]
    auth.save_users(users)
    print(f"User '{username}' removed.")


def cmd_list() -> None:
    users = auth.load_users()
    if not users:
        print("No users configured.")
        return
    for name in sorted(users):
        print(name)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    action = sys.argv[1]
    if action == "add" and len(sys.argv) >= 3:
        cmd_add(sys.argv[2])
    elif action == "remove" and len(sys.argv) >= 3:
        cmd_remove(sys.argv[2])
    elif action == "list":
        cmd_list()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
