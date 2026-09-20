"""Check that the Supabase service key works and the tables exist.

Run after putting SUPABASE_SERVICE_KEY in .env, before create_user.py:

    python check_supabase.py

Prints only the key's length and last 4 characters, never the key itself, so the
output is safe to paste into a chat or an issue.

HTTP 200 = reachable. 401/403 = wrong key. 404 = table missing (apply the
migration in supabase/migrations/). Connection error = the project may be paused;
free-tier projects pause when idle and need restoring from the dashboard.
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

TABLES = ("app_state", "users", "user_state", "chat_messages", "journal_entries")


def main() -> int:
    url = (os.getenv("SUPABASE_URL") or "https://fcwpjsezrwnjxrqpuwvc.supabase.co").rstrip("/")
    key = (os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
           or os.getenv("SUPABASE_KEY"))

    if not key:
        print("FAIL: no Supabase key found.")
        print("      Add SUPABASE_SERVICE_KEY=... to .env (see .env.example).")
        return 1

    print(f"URL: {url}")
    print(f"Key: {len(key)} chars, ends ...{key[-4:]}\n")

    failures = 0
    for table in TABLES:
        try:
            r = requests.get(f"{url}/rest/v1/{table}",
                             params={"select": "*", "limit": "1"},
                             headers={"apikey": key, "authorization": f"Bearer {key}"},
                             timeout=15)
            if r.ok:
                rows = len(r.json())
                print(f"  {table:18} OK      ({'has rows' if rows else 'empty'})")
            else:
                failures += 1
                print(f"  {table:18} HTTP {r.status_code}  {r.text[:90]}")
        except Exception as e:
            failures += 1
            print(f"  {table:18} UNREACHABLE  {e}")

    print()
    if failures:
        print(f"{failures} check(s) failed — see the notes at the top of this file.")
        return 1
    print("All good. Next: python create_user.py <email> <password> --owner")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
