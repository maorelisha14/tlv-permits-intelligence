"""Verify that every column in db_schema.py exists in Supabase (read-only).

Selects each expected column one by one (PostgREST rejects unknown columns), so all
missing columns are reported at once. Nothing is inserted or modified.
Exit code 0 = 100% match, 1 = mismatch, 2 = could not connect.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

from db_schema import PERMITS_COLUMNS, SCANNED_REQUESTS_COLUMNS, SCANNED_STATUSES

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8")

load_dotenv(Path(__file__).resolve().parent / ".env")

TABLES = {
    "scanned_requests": SCANNED_REQUESTS_COLUMNS,
    "tel_aviv_permits": PERMITS_COLUMNS,
}


def main() -> int:
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/").removesuffix("/rest/v1")
    key = (os.getenv("SUPABASE_KEY") or "").strip()
    if not (url and key):
        print("❌ חסרים SUPABASE_URL / SUPABASE_KEY ב-.env")
        return 2
    sb = create_client(url, key)

    ok = True
    for table, columns in TABLES.items():
        print(f"\n📋 {table}")
        for col in columns:
            try:
                sb.table(table).select(col).limit(1).execute()
                print(f"  ✅ {col}")
            except Exception as exc:  # noqa: BLE001
                code = getattr(exc, "code", None)
                msg = getattr(exc, "message", None) or str(exc)
                if code in ("42703", "PGRST204", "PGRST200") or "does not exist" in msg or "schema cache" in msg:
                    print(f"  ❌ {col}  — חסרה ({msg})")
                    ok = False
                elif code == "42P01" or "relation" in msg and "does not exist" in msg:
                    print(f"  ❌ הטבלה {table} אינה קיימת ({msg})")
                    ok = False
                    break
                else:
                    print(f"  ⚠️ {col}  — שגיאה לא צפויה: {msg}")
                    return 2

    # status values accepted by the CHECK constraint can't be probed read-only; list what the code may write
    print(f"\nℹ️ סטטוסים שהקוד עשוי לכתוב: {', '.join(SCANNED_STATUSES)} "
          "(אילוץ ה-CHECK נבדק ב-schema.sql, לא ניתן לבדיקה במצב קריאה בלבד)")
    print("\n" + ("✅ הסכמה תואמת ב-100%" if ok else "❌ הסכמה אינה תואמת – הרץ את schema.sql ב-Supabase SQL editor"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
