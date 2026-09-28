"""One-off import of the accounts the two control towers held separately.

Both apps used the same PBKDF2-HMAC-SHA256 format with a per-user salt and
240,000 iterations, so the stored hashes move across as they are and nobody has
to choose a new password.

Each person existed twice, once per app, with a different password in each.
Identity holds one account per person, so Finance wins by default: it is the
newer of the two installations. Pass --prefer purchasing to reverse that.
"""
import argparse
import json
import sys
from pathlib import Path

import permissions
import store


def load(path):
    try:
        return {u["username"].lower(): u for u in json.loads(Path(path).read_text())["users"]}
    except (OSError, ValueError, KeyError) as exc:
        print(f"  ! could not read {path}: {exc}")
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--finance", default="/import/finance/users.json")
    ap.add_argument("--purchasing", default="/import/purchasing/users.json")
    ap.add_argument("--prefer", choices=["finance", "purchasing"], default="finance")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store.connect()
    fin, pur = load(args.finance), load(args.purchasing)
    first, second = (fin, pur) if args.prefer == "finance" else (pur, fin)
    merged = {**second, **first}          # the preferred app's row wins

    if not merged:
        print("Nothing to import.")
        return 1

    known_roles = {r["name"] for r in store.q("SELECT name FROM roles")}
    imported = skipped = 0

    for username, u in sorted(merged.items()):
        if store.one("SELECT username FROM users WHERE username=?", (username,)):
            print(f"  = {username}: already in identity, left alone")
            skipped += 1
            continue

        role = permissions.LEGACY_ROLE_MAP.get(u.get("role"), "Viewer")
        if role not in known_roles:
            role = "Viewer"
        both = username in fin and username in pur
        source = args.prefer if both else ("finance" if username in fin else "purchasing")

        print(f"  + {username}: role {u.get('role')} -> {role}  (password from {source}"
              + (", the other copy is dropped" if both else "") + ")")
        if args.dry_run:
            continue

        store.run("""INSERT INTO users(username,name,email,role,salt,hash,active,must_change,
                                       created_at,created_by,last_login,password_set_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (username, u.get("name") or username, "", role, u["salt"], u["hash"],
                   1 if u.get("active", True) else 0,
                   1 if u.get("must_change") else 0,
                   u.get("created_at") or store.now(), "migration",
                   u.get("last_login"), u.get("created_at") or store.now()))
        store.audit("user imported", "migration", username,
                    {"from": source, "role": role, "was": u.get("role")}, "")
        imported += 1

    print(f"\n{'Would import' if args.dry_run else 'Imported'} {imported}, skipped {skipped}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
