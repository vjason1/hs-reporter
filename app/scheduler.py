"""Scheduled reports, driven by APScheduler with standard 5-field cron strings."""
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import reports, store

TZ = os.environ.get("TZ", "UTC")
scheduler = AsyncIOScheduler(timezone=TZ)


def trigger_for(cron: str) -> CronTrigger:
    try:
        return CronTrigger.from_crontab(cron.strip(), timezone=TZ)
    except ValueError as e:
        raise ValueError(f"Invalid cron expression: {e}")


async def run_schedule(schedule_id: str) -> None:
    sched = store.schedules.get(schedule_id)
    if not sched or not sched.get("enabled", True):
        return
    defn = store.definitions.get(sched["definition_id"])
    if not defn:
        sched.update(last_status="failed", last_error="Report definition was deleted")
        store.schedules.put(sched)
        return
    run = reports.create_run(defn, trigger="schedule", schedule_id=schedule_id)
    run = await reports.execute(run, export=bool(sched.get("export_csv")))
    sched.update(last_run_id=run["id"], last_run_at=run["started"],
                 last_status=run["status"], last_error=run.get("error"))
    store.schedules.put(sched)
    prune(sched)


def prune(sched: dict) -> None:
    keep = int(sched.get("keep_last") or 0)
    if keep <= 0:
        return
    mine = [r for r in store.runs.summaries() if r.get("schedule_id") == sched["id"]]
    for r in mine[keep:]:  # summaries are newest first
        store.runs.delete(r["id"])


def sync(sched: dict) -> None:
    job_id = f"sched-{sched['id']}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if sched.get("enabled", True):
        scheduler.add_job(run_schedule, trigger_for(sched["cron"]), args=[sched["id"]],
                          id=job_id, max_instances=1, coalesce=True, misfire_grace_time=3600)


def remove(schedule_id: str) -> None:
    job = scheduler.get_job(f"sched-{schedule_id}")
    if job:
        job.remove()


def next_run(schedule_id: str):
    job = scheduler.get_job(f"sched-{schedule_id}")
    return job.next_run_time.timestamp() if job and job.next_run_time else None


def start() -> None:
    for s in store.schedules.all():
        try:
            sync(s)
        except ValueError as e:
            print(f"[hs-reporter] schedule {s.get('name')} skipped: {e}")
    scheduler.start()
