"""python -m app.scheduler : the single scheduler process (status checks, cron views, retention)."""
import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from app.db import session_factory
from app.scheduler.jobs import configure

logging.basicConfig(level=logging.INFO)
configure(BlockingScheduler(timezone="UTC"), session_factory()).start()
