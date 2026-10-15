"""One-off import of serverlist_update + index.html categories into the panel database.

Run from the backend directory so `app` is importable, with DATABASE_URL and MASTER_KEY set:

    python ../tools/migrate_serverlist.py --serverlist /path/serverlist_update \
        --index-html ../index.html --owner alice [--dry-run] [--compare-csv-dir /path/abc]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from sqlalchemy import select  # noqa: E402

from app import legacy_import as li  # noqa: E402
from app.db import session_factory  # noqa: E402
from app.models import User, View  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--serverlist", required=True)
    p.add_argument("--index-html", required=True)
    p.add_argument("--owner", required=True, help="login of the user who will own the imported objects")
    p.add_argument("--schedule", default="*/10 * * * *")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--compare-csv-dir", help="after import, run the view and compare with legacy CSVs")
    a = p.parse_args()

    servers, problems = li.parse_serverlist(Path(a.serverlist).read_text(encoding="utf-8"))
    tags = li.parse_index_tags(Path(a.index_html).read_text(encoding="utf-8"))
    with session_factory()() as db:
        owner = db.scalar(select(User).where(User.login == a.owner.lower()))
        if owner is None:
            print(f"Owner '{a.owner}' not found (create it with: python -m app.cli create-admin)")
            return 1
        rep = li.migrate(db, owner, servers, tags, schedule=a.schedule, dry_run=a.dry_run)
        print(("DRY RUN - nothing written\n" if a.dry_run else "") +
              f"created {len(rep['created'])}, skipped (already present) {len(rep['skipped'])}, "
              f"failed {len(rep['failed'])}, bad lines {len(problems)}; view: {rep['view']}")
        for msg in problems + [f"{f['name']}: {f['reason']}" for f in rep["failed"]]:
            print("  problem:", msg)
        if rep["untagged"]:
            print("  no category in index.html:", ", ".join(rep["untagged"]))
        if rep["unlisted_categories"]:
            print("  in index.html but not in server list:", ", ".join(rep["unlisted_categories"]))
        if a.compare_csv_dir and not a.dry_run:
            from app.views import engine
            view = db.scalar(select(View).where(View.name == li.VIEW_NAME))
            res = engine.run_view(db, owner, view, {}, fresh=True, triggered_by="user")
            for svc, verdict in sorted(li.compare_with_csv(res, Path(a.compare_csv_dir)).items()):
                print(f"  {svc}: {verdict}")
    print("\nNEXT: rotate the old plaintext passwords and delete serverlist_update.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
