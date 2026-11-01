"""`nebula` command: serve, worker, migrate, and a few admin helpers for setup."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="nebula", description="Nebula: Rocket Learning's memory for Claude")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="Run the MCP server and web routes")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--workers", type=int, default=1)
    sub.add_parser("worker", help="Run the background worker")
    sub.add_parser("migrate", help="Apply database migrations")
    a = sub.add_parser("admin", help="Make someone a Nebula admin, or stop them being one")
    a.add_argument("email")
    a.add_argument("--remove", action="store_true", help="Take admin away instead")
    d = sub.add_parser("connect-drive", help="Connect a Drive folder on someone's behalf (access follows Drive)")
    d.add_argument("email", help="Who manages the connection")
    d.add_argument("folder", help="Drive folder link or id")
    d.add_argument("--name")
    sub.add_parser("run-jobs", help="Run queued jobs until the queue is empty, then exit")
    e = sub.add_parser("eval", help="Check extraction and retrieval against known answers (see nebula.evaluate)")
    e.add_argument("cases", help="JSON file: a list of cases")
    e.add_argument("--as", dest="person", default="", help="Who asks, for cases that don't say")
    e.add_argument("--out", help="Also write the full results to this JSON file")
    sub.add_parser("discover", help="Register anything newly shared with Nebula in Drive, now")
    args = p.parse_args(argv)
    _setup_logging()

    if args.cmd == "serve":
        import uvicorn

        if args.workers < 1:
            p.error("--workers must be at least 1")
        uvicorn.run("nebula.app:build_app", factory=True, workers=args.workers,
                    host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*",
                    timeout_keep_alive=65, timeout_worker_healthcheck=60)
        return
    if args.cmd == "worker":
        from nebula.worker import run_forever

        asyncio.run(run_forever())
        return
    asyncio.run(_admin(args))


async def _admin(args: argparse.Namespace) -> None:
    from nebula import access, db

    await db.migrate()
    try:
        if args.cmd == "migrate":
            print("migrations up to date")
            return
        if args.cmd == "admin":
            from nebula.config import settings

            grant = not args.remove
            await db.execute(
                "insert into people (email, is_admin) values (%s, %s) on conflict (email) do update set is_admin = %s",
                (args.email.lower(), grant, grant),
            )
            if grant:
                print(f"{args.email} is now a Nebula admin")
            elif args.email.lower() in settings().admin_email_set:
                print(f"{args.email} is still an admin through NEBULA_ADMIN_EMAILS; remove them there too")
            else:
                print(f"{args.email} is no longer a Nebula admin")
            return
        if args.cmd == "connect-drive":
            from nebula import sources

            person = await access.ensure_person(args.email)
            print(json.dumps(await sources.connect(person, args.folder, args.name), default=str))
        elif args.cmd == "eval":
            from pathlib import Path

            from nebula import evaluate

            cases = json.loads(Path(args.cases).read_text())
            report = await evaluate.run(cases, args.person)
            if args.out:
                Path(args.out).write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str))
            for r in report["results"]:
                print(f"{r['result']:8} {r['question'][:70]}" + (f"  (first miss: {r['first_miss']})"
                                                                 if r.get("first_miss") else ""))
            print(json.dumps({k: report[k] for k in ("cases", "summary", "first_miss_by_layer")}))
        elif args.cmd == "discover":
            from nebula import sync

            print(json.dumps(await sync.discover(), default=str))
        elif args.cmd == "run-jobs":
            from nebula import jobs
            from nebula import worker  # noqa: F401  registers handlers

            n = 0
            while await jobs.run_one():
                n += 1
            print(f"ran {n} jobs")
    finally:
        await db.close()


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
