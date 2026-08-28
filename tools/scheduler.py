"""CLI for the durable local market scheduler."""

from __future__ import annotations

import argparse
import json
import signal
import os
from datetime import datetime
from pathlib import Path

from scheduler import JOB_TYPES, ROOT, Scheduler, Worker, run_offline_drill, verify_job_checkpoint, write_dead_letter_report
from security import AuditLogger, SecurityError, authorize


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _scheduler() -> Scheduler:
    return Scheduler(ROOT)


def _require_mutation(args: argparse.Namespace, action: str) -> None:
    decision = authorize(actor=args.actor, role=args.role, capability="scheduler.mutate", reason=args.reason, approve=args.approve)
    AuditLogger().append("scheduler." + action, actor=args.actor, outcome="allowed" if decision["allowed"] else "denied", details={"code": decision["code"]}, reason=args.reason)
    if not decision["allowed"]:
        raise SecurityError(decision["code"], "authorization denied")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Asia/Tokyo durable scheduler and checkpoint recovery")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="enqueue due jobs and run one worker loop")
    run.add_argument("--once", action="store_true")
    run.add_argument("--worker-id", default=None)
    run.add_argument("--interval", type=float, default=None)

    worker = sub.add_parser("worker", help="run worker only")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--worker-id", default=None)
    worker.add_argument("--interval", type=float, default=None)

    sub.add_parser("enqueue-today")
    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--status")
    show = sub.add_parser("show")
    show.add_argument("job_id")
    trigger = sub.add_parser("trigger")
    trigger.add_argument("--job-type", choices=JOB_TYPES, required=True)
    trigger.add_argument("--now", default=None, help="fixed Asia/Tokyo ISO timestamp for deterministic operation")
    recover = sub.add_parser("recover")
    recover.add_argument("--now", default=None)
    retry = sub.add_parser("retry")
    retry.add_argument("job_id")
    retry.add_argument("--reason", required=True)
    retry.add_argument("--actor", default=os.environ.get("USER", ""))
    retry.add_argument("--role", default="operator")
    retry.add_argument("--approve", action="store_true")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("job_id")
    cancel.add_argument("--reason", default="manual_cancel")
    cancel.add_argument("--actor", default=os.environ.get("USER", ""))
    cancel.add_argument("--role", default="operator")
    cancel.add_argument("--approve", action="store_true")
    rerun = sub.add_parser("rerun")
    rerun.add_argument("job_id")
    rerun.add_argument("--reason", required=True)
    rerun.add_argument("--force", action="store_true")
    rerun.add_argument("--actor", default=os.environ.get("USER", ""))
    rerun.add_argument("--role", default="operator")
    rerun.add_argument("--approve", action="store_true")
    checkpoint = sub.add_parser("checkpoint")
    checkpoint_sub = checkpoint.add_subparsers(dest="checkpoint_command", required=True)
    verify = checkpoint_sub.add_parser("verify")
    verify.add_argument("job_id")
    locks = sub.add_parser("locks")
    locks_sub = locks.add_subparsers(dest="locks_command", required=True)
    lock_repair = locks_sub.add_parser("repair")
    lock_repair.add_argument("--actor", default=os.environ.get("USER", ""))
    lock_repair.add_argument("--role", default="maintainer")
    lock_repair.add_argument("--reason", required=True)
    lock_repair.add_argument("--approve", action="store_true")
    dead = sub.add_parser("dead-letter")
    dead_sub = dead.add_subparsers(dest="dead_command", required=True)
    dead_sub.add_parser("list")
    dead_show = dead_sub.add_parser("show")
    dead_show.add_argument("job_id")
    dead_retry = dead_sub.add_parser("retry")
    dead_retry.add_argument("job_id")
    dead_retry.add_argument("--reason", required=True)
    dead_retry.add_argument("--actor", default=os.environ.get("USER", ""))
    dead_retry.add_argument("--role", default="operator")
    dead_retry.add_argument("--approve", action="store_true")
    sub.add_parser("status")
    sub.add_parser("drill")

    args = parser.parse_args(argv)
    scheduler = _scheduler()

    if args.command == "enqueue-today":
        _print(scheduler.enqueue_today())
        return 0
    if args.command == "list":
        _print(scheduler.store.list_jobs(args.status))
        return 0
    if args.command == "show":
        job = scheduler.store.get(args.job_id)
        if job is None:
            parser.error(f"job_not_found:{args.job_id}")
        _print({"job": job, "executions": scheduler.store.executions(args.job_id), "events": scheduler.store.events(args.job_id)})
        return 0
    if args.command == "trigger":
        _print(scheduler.trigger(args.job_type, _dt(args.now)))
        return 0
    if args.command == "recover":
        _print(scheduler.recovery_scan(_dt(args.now)))
        return 0
    if args.command == "retry":
        _require_mutation(args, "retry")
        _print(scheduler.store.retry_dead_letter(args.job_id, args.reason))
        return 0
    if args.command == "cancel":
        _require_mutation(args, "cancel")
        _print(scheduler.store.cancel(args.job_id, args.reason))
        return 0
    if args.command == "rerun":
        _require_mutation(args, "rerun")
        _print(scheduler.store.rerun(args.job_id, args.reason, force=args.force))
        return 0
    if args.command == "checkpoint":
        _print(verify_job_checkpoint(scheduler, args.job_id))
        return 0
    if args.command == "locks":
        _require_mutation(args, "locks_repair")
        _print(scheduler.store.repair_locks())
        return 0
    if args.command == "dead-letter":
        if args.dead_command == "list":
            _print(write_dead_letter_report(scheduler))
        elif args.dead_command == "show":
            _print({"job": scheduler.store.get(args.job_id), "executions": scheduler.store.executions(args.job_id), "events": scheduler.store.events(args.job_id)})
        else:
            _require_mutation(args, "dead_letter_retry")
            _print(scheduler.store.retry_dead_letter(args.job_id, args.reason))
        return 0
    if args.command == "status":
        _print(scheduler.status_report())
        return 0
    if args.command == "drill":
        report = run_offline_drill(ROOT)
        _print(report)
        return 0 if report.get("passed") else 1

    worker = Worker(scheduler, args.worker_id if hasattr(args, "worker_id") else None)
    def stop(_signum, _frame):
        worker.request_stop()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if args.command == "run" and getattr(args, "once", False):
        scheduler.enqueue_today()
    if getattr(args, "once", False):
        _print(worker.run_once())
    elif args.command == "run":
        worker.run_scheduler_forever(getattr(args, "interval", None))
    else:
        worker.run_forever(getattr(args, "interval", None))
    scheduler.status_report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
