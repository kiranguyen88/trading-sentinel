"""Create an account from the command line.

Needed because the app has no open sign-up by default — the first account has to
come from somewhere. Run it once with the Supabase service key in the
environment (or in .env):

    python create_user.py nguyenthemy@yahoo.com 123456789 --owner

--owner marks the account as the one scheduled cron jobs run for, and copies the
pre-accounts `app_state` portfolio into it. It is refused if an owner already
exists, so re-running cannot silently move ownership.
"""

import sys

from dotenv import load_dotenv

load_dotenv()

import auth
from trading_bot import _SUPABASE_OK, _sb_load, _sb_save_user


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    as_owner = "--owner" in argv

    if len(args) != 2:
        print(__doc__)
        return 2
    email, password = args[0].strip().lower(), args[1]

    if not _SUPABASE_OK:
        print("ERROR: no Supabase service key. Set SUPABASE_SERVICE_KEY in .env or the\n"
              "       environment (SUPABASE_SERVICE_ROLE_KEY / SUPABASE_KEY also work).")
        return 1

    if len(password) < auth.MIN_PASSWORD_LEN:
        print(f"ERROR: password must be at least {auth.MIN_PASSWORD_LEN} characters.")
        return 1

    if auth.get_user_by_email(email):
        print(f"ERROR: {email} already exists. Nothing changed.")
        return 1

    if as_owner:
        existing = auth.get_owner()
        if existing:
            print(f"ERROR: an owner already exists ({existing['email']}). "
                  f"Re-run without --owner to create a regular account.")
            return 1

    user = auth.create_user(email, password, is_owner=as_owner)
    print(f"Created {email}  (id {user['id']}, owner={as_owner})")

    if as_owner:
        # Raises if Supabase is unreachable rather than leaving the owner with an
        # empty portfolio and no sign that the copy was skipped.
        legacy = _sb_load()
        if legacy:
            _sb_save_user(user["id"], legacy)
            print(f"Copied {len(legacy.get('holdings', []))} holding(s) and "
                  f"{len(legacy.get('watchlist', []))} watchlist ticker(s) from app_state.")
            print("app_state was NOT modified — it stays as a rollback copy.")
        else:
            print("No legacy app_state row found; the account starts empty.")

    print("\nSign in at /login, then use the Password button in the header to change it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
