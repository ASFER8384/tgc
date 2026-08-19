"""Make a console account.

    python -m scripts.make_user aleena@thegroupcompany.sa

Prints the `email:hash` line to add to SCA_CONSOLE_USERS in .env. The password
is asked for rather than passed as an argument, so it does not end up in a shell
history or in the process list of a shared machine.

There is no matching `delete_user`: an account is removed by deleting its entry
from the environment, which is the same edit and one fewer thing to get wrong.
"""

import getpass
import sys

from sca.auth import hash_password


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    email = sys.argv[1].strip().lower()
    if "@" not in email:
        print(f"'{email}' does not look like an email address")
        return 2

    password = getpass.getpass("Password: ")
    # Eight is not a strong password and this does not pretend otherwise. It is
    # the floor under a mistake — an empty line, or a stray keystroke accepted
    # in silence and never reproducible at the sign-in screen.
    if len(password) < 8:
        print("Too short: at least 8 characters.")
        return 1
    if password != getpass.getpass("Again: "):
        print("They do not match.")
        return 1

    print()
    print("Add this to SCA_CONSOLE_USERS in .env (comma separated for several):")
    print()
    print(f"{email}:{hash_password(password)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
