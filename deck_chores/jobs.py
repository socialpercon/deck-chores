import logging
from typing import List, Tuple

from apscheduler import events
from apscheduler.job import Job
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler

from deck_chores.config import cfg
from deck_chores.utils import generate_id


####

CAPTURED_OPENER = '== BEGIN of captured stdout & stderr =='
CAPTURED_CLOSER = '== END of captured stdout & stderr ===='
CAPTURED_SURROUNDING_LENGTH = len(CAPTURED_OPENER)


####


log = logging.getLogger('deck_chores')


####


scheduler = BackgroundScheduler()


def start_scheduler():
    logger = log if cfg.debug else None
    scheduler.configure(logger=logger, timezone=cfg.timezone)
    scheduler.add_listener(on_error, events.EVENT_JOB_ERROR)
    scheduler.add_listener(on_executed, events.EVENT_JOB_EXECUTED)
    scheduler.add_listener(on_max_instances, events.EVENT_JOB_MAX_INSTANCES)
    scheduler.add_listener(on_missed, events.EVENT_JOB_MISSED)
    scheduler.start()


####


def on_max_instances(event: events.JobSubmissionEvent) -> None:
    job = scheduler.get_job(event.job_id)
    job_name = job.kwargs['job_name']
    container_name = job.kwargs['container_name']
    max_inst = job.max_instances
    log.info(
        f'Not running {job_name} in {container_name}, '
        f'maximum instances of {max_inst} are still running.'
    )


def on_executed(event: events.JobExecutionEvent) -> None:
    job = scheduler.get_job(event.job_id)
    if job is None or job.id == 'container_inspection':
        return

    definition = job.kwargs
    exit_code, response_lines = event.retval
    response_lines = response_lines.decode().splitlines()

    log.log(
        logging.INFO if exit_code == 0 else logging.CRITICAL,
        f'Command {definition["command"]} in {definition["container_name"]} '
        f'finished with exit code {exit_code}',
    )
    if response_lines:
        longest_line = max(len(x) for x in response_lines)
        log.info(CAPTURED_OPENER + '=' * (longest_line - CAPTURED_SURROUNDING_LENGTH))
        for line in response_lines:
            log.info(line)
        log.info(CAPTURED_CLOSER + '=' * (longest_line - CAPTURED_SURROUNDING_LENGTH))


def on_error(event: events.JobExecutionEvent) -> None:
    definition = scheduler.get_job(event.job_id).kwargs
    log.critical(
        f'An exception in deck-chores occured while executing {definition["job_name"]} '
        f'in {definition["container_name"]}:'
    )
    log.exception(event.exception)


def on_missed(event: events.JobExecutionEvent) -> None:
    definition = scheduler.get_job(event.job_id).kwargs
    log.warning(
        f'Missed execution of {definition["job_name"]} in '
        f'{definition["container_name"]} at {event.scheduled_run_time}'
    )


####


def exec_job(**definition) -> Tuple[int, bytes]:
    job_id = definition['job_id']

    container_id = definition['container_id']
    command = definition['command']

    log.info(f"Executing '{definition['job_name']}' in {definition['container_name']}")

    # some sanity checks, to be removed eventually
    assert scheduler.get_job(job_id) is not None
    if cfg.client.containers.list(filters={'id': container_id, 'status': 'paused'}):
        raise AssertionError('Container is paused.')

    if not cfg.client.containers.list(
        filters={'id': container_id, 'status': 'running'}
    ):
        scheduler.remove_job(job_id)
        assert scheduler.get_job(job_id) is None
        raise AssertionError('Container is not running.')
    # end of sanity checks

    # TODO allow to set environment and workdir in options
    return cfg.client.containers.get(container_id).exec_run(
        cmd=command, user=definition['user']
    )


####


def add(container_id: str, definitions: dict) -> None:
    container_name = cfg.client.containers.get(container_id).name
    log.debug(f'Adding jobs for {container_name}.')
    for job_name, definition in definitions.items():
        job_id = generate_id(container_id, job_name)
        trigger = definition['trigger']
        definition.update(
            {
                'job_name': job_name,
                'job_id': job_id,
                'container_id': container_id,
                'container_name': container_name,
            }
        )
        scheduler.add_job(
            func=exec_job,
            trigger=trigger[0](*trigger[1], timezone=definition['timezone']),
            kwargs=definition,
            id=job_id,
            max_instances=definition['max'],
            replace_existing=True,
        )
        log.info(f"Added '{job_name}' for {container_name}")


def remove(job_id: str) -> None:
    try:
        scheduler.remove_job(job_id)
    except JobLookupError as e:
        log.critical(str(e))


####


def get_jobs_for_container(container_id: str) -> List[Job]:
    # TODO make that an index
    result = []
    for job in scheduler.get_jobs():
        if job.kwargs['container_id'] == container_id:
            result.append(job)
    return result
