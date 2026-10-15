"""Scheduled work (spec 6.3, 7.5). Run as its own process (`python -m app.scheduler`) so jobs are not
duplicated when the API runs several workers."""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import delete, select, update

from app.audit.service import record
from app.db import now
from app.errors import AppError
from app.models import Resource, User, View, ViewRun
from app.resources import service
from app.views import engine

log = logging.getLogger("scheduler")
RESULT_RETENTION_DAYS = 30
META_RETENTION_DAYS = 365


def check_all_resources(factory, workers: int = 8) -> dict:
    """Self-test every resource and store status/last_error (feeds the home heat-map)."""
    with factory() as db:
        ids = list(db.scalars(select(Resource.id)))

    def one(rid):
        with factory() as db:
            r = db.get(Resource, rid)
            return service.test_resource(db, r)[0] if r else None

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(one, ids))
    return {"checked": len(ids), "up": sum(1 for r in results if r), "down": sum(1 for r in results if r is False)}


def run_scheduled_view(factory, view_id) -> str:
    """Run a view as its owner using default variable values. Failures are recorded, never swallowed."""
    with factory() as db:
        view = db.get(View, view_id)
        if view is None:
            return "gone"
        owner = db.get(User, view.owner_id)
        try:
            engine.run_view(db, owner, view, {}, fresh=True, triggered_by="schedule")
            return "ok"
        except AppError as e:
            log.warning("scheduled view %s failed: %s", view_id, e.code)
            record(db, "view.run_failed", actor_id=owner.id, target_type="view", target_id=view_id,
                   detail={"code": e.code, "message": e.message[:300]})
            return e.code


def purge(factory, result_days: int = RESULT_RETENTION_DAYS, meta_days: int = META_RETENTION_DAYS) -> dict:
    """Drop stored result payloads after `result_days`, whole run rows after `meta_days`."""
    with factory() as db:
        t = now()
        stripped = db.execute(update(ViewRun).where(ViewRun.started_at < t - timedelta(days=result_days),
                                                    ViewRun.result.is_not(None)).values(result=None)).rowcount
        deleted = db.execute(delete(ViewRun).where(ViewRun.started_at < t - timedelta(days=meta_days))).rowcount
        db.commit()
    return {"results_dropped": stripped, "runs_deleted": deleted}


def sync_view_jobs(scheduler: BaseScheduler, factory) -> None:
    """Make APScheduler jobs match the views that have a cron schedule (add, change, remove)."""
    with factory() as db:
        wanted = {f"view:{v.id}": (v.id, v.schedule_cron) for v in db.scalars(
            select(View).where(View.schedule_cron.is_not(None)))}
    existing = {j.id: j for j in scheduler.get_jobs() if j.id.startswith("view:")}
    for jid in set(existing) - set(wanted):
        scheduler.remove_job(jid)
    for jid, (vid, cron) in wanted.items():
        if jid in existing and existing[jid].name == cron:
            continue
        if jid in existing:  # cron changed; explicit remove works whether or not the scheduler is running
            scheduler.remove_job(jid)
        scheduler.add_job(run_scheduled_view, CronTrigger.from_crontab(cron), args=[factory, vid], id=jid,
                          name=cron, max_instances=1, coalesce=True, misfire_grace_time=60)


def configure(scheduler: BaseScheduler, factory, status_minutes: int = 5) -> BaseScheduler:
    scheduler.add_job(check_all_resources, "interval", minutes=status_minutes, jitter=30, args=[factory],
                      id="resource-status", max_instances=1, coalesce=True)
    scheduler.add_job(sync_view_jobs, "interval", seconds=60, args=[scheduler, factory], id="sync-views")
    scheduler.add_job(purge, CronTrigger(hour=3, minute=30), args=[factory], id="purge")
    sync_view_jobs(scheduler, factory)
    return scheduler
