"""Command line entry point: python -m fund_tracker <command>."""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from .config import DATA_DIR, DB_PATH, DOCS_DIR, load_funds, load_settings
from .db import Database
from .http import Http
from .providers import update_fund
from .providers.manual_csv import import_manual_files
from .report import build_model, export_csv, render_html

log = logging.getLogger("fund_tracker")


def cmd_update(args) -> int:
    funds = load_funds()
    settings = load_settings()
    db = Database(args.db)
    db.upsert_funds(funds)
    http = Http()
    log.info("HTTP backend: %s", http.backend)
    run_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    selected = [f for f in funds if not args.isin or f.isin in {i.upper() for i in args.isin}]
    import_manual_files(DATA_DIR / "manual", db, {f.isin for f in funds}, run_at)
    results = []
    for fund in selected:
        log.info("=== %s (%s) ===", fund.name, fund.isin)
        results.append(update_fund(db, http, fund, settings, run_at, full=args.full))
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] != "ok"]
    print(f"\nUpdated {len(ok)}/{len(results)} funds.")
    for r in failed:
        print(f"  FAILED {r['isin']}: " + " | ".join(r["errors"]))
    export_csv(db, funds)
    db.close()
    if args.strict and failed:
        return 1
    return 0


def cmd_report(args) -> int:
    funds = load_funds()
    settings = load_settings()
    db = Database(args.db)
    db.upsert_funds(funds)
    model = build_model(db, funds, settings)
    path = render_html(model, DOCS_DIR / "index.html")
    print(f"Report written: {path}")
    db.close()
    return 0


def cmd_status(args) -> int:
    funds = load_funds()
    db = Database(args.db)
    print(f"{'ISIN':14} {'first':10} {'last':10} {'rows':>6}  sources")
    for f in funds:
        first, last, n = db.nav_range(f.isin)
        src = ", ".join(f"{k}:{v}" for k, v in db.nav_sources(f.isin).items())
        print(f"{f.isin:14} {str(first or '-'):10} {str(last or '-'):10} {n:>6}  {src}")
    db.close()
    return 0


def cmd_reset(args) -> int:
    db = Database(args.db)
    for isin in args.isin:
        n = db.delete_fund_navs(isin.upper())
        db.conn.execute("DELETE FROM provider_ids WHERE isin=?", (isin.upper(),))
        db.conn.commit()
        print(f"{isin}: deleted {n} rows")
    db.close()
    return 0


def cmd_inspect(args) -> int:
    from .inspect_page import inspect

    for url in args.url:
        try:
            print(inspect(url))
        except Exception as exc:
            print(f"URL: {url}\n  ERROR {exc}")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fund_tracker", description="Fund NAV tracker")
    parser.add_argument("--db", default=str(DB_PATH), help="path to the SQLite database")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("update", help="download NAV history for all funds (or selected ISINs)")
    p.add_argument("--isin", nargs="*", help="only these ISINs")
    p.add_argument("--full", action="store_true", help="re-download the full history")
    p.add_argument("--strict", action="store_true", help="exit code 1 if any fund failed")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("report", help="build docs/index.html from the database")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("run", help="update + report")
    p.add_argument("--isin", nargs="*")
    p.add_argument("--full", action="store_true")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=lambda a: cmd_update(a) or cmd_report(a))

    p = sub.add_parser("status", help="show what is in the database")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("reset", help="delete stored NAVs and cached ids for the given ISINs")
    p.add_argument("isin", nargs="+")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("inspect", help="diagnostics: summarise links/forms/tables of web pages")
    p.add_argument("url", nargs="+")
    p.set_defaults(func=cmd_inspect)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
