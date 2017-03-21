"""Module for scheduling automatic testing.
"""

import copy
import os
import json
import tempfile
import logging
import datetime
import shlex
import urllib.parse

try: # try to import rq_scheduler and redis but allow other modes if fail
    from rq.job import Job
    from rq import get_failed_queue, Queue
    import rq_scheduler
    from redis import Redis
except Exception as problem:
    msg = 'Could not import rq_scheduler and redis because %s.\n%s' % (
        str(problem), 'Continue with non-rq options.')
    logging.error(msg)

import pytest

from ox_herd.file_cache import cache_utils

class OxHerdArgs(object):

    def __init__(self, name):
        FIXME

class OxHerdTask(object):

    def __init__(self, ox_herd_args):
        self.task_args = ox_herd_args

class TestingTask(OxHerdTask):

    def __init__(self, ox_herd_args=None):
        OxHerdTask.__init__(self, ox_herd_args)

    def __call__(self, ox_test_args):
        test_file = ox_test_args.json_file if ox_test_args.json_file else (
            tempfile.mktemp(suffix='.json'))
        url, cmd_line = self.do_test(ox_test_args, test_file)
        self.make_report(ox_test_args, test_file, url, cmd_line)
        if not ox_test_args.json_file:
            logging.debug('Removing temporary json report %s', test_file)
            os.remove(test_file) # remove temp file

    @staticmethod
    def do_test(ox_test_args, test_file):
        url = urllib.parse.urlparse(ox_test_args.url)
        if url.scheme == 'file':
            cmd_line = [url.path, '--json', test_file, '-v']
            pta = ox_test_args.pytest
            if isinstance(pta, str):
                pta = shlex.split(pta)
            cmd_line.extend(pta)
            logging.info('Running pytest with command arguments of: %s', 
                         str(cmd_line))
            pytest.main(cmd_line)
            return url, cmd_line
        else:
            raise ValueError('URL scheme of %s not handled yet.' % url.scheme)

    @staticmethod
    def make_report(ox_test_args, test_json, url, cmd_line):
        test_data = json.load(open(test_json))['report']
        test_data['url'] = url
        test_data['cmd_line'] = cmd_line
        test_time = datetime.datetime.strptime(
            test_data['created_at'].split('.')[0], '%Y-%m-%d %H:%M:%S')
        rep_name = ox_test_args.name + test_time.strftime('_%Y%m%d_%H%M%S.pkl')
        cache_utils.pickle_with_name(test_data, 'test_results/%s' % rep_name)


class SimpleScheduler(object):

    @classmethod
    def add_to_schedule(cls, args):
        name = 'schedule_via_%s' % args.manager
        func = getattr(cls, name)
        task = TestingTask()
        return func(args, task)

    @staticmethod
    def schedule_via_instant(args, task):
        return task(ox_test_args=args)
        
    @staticmethod
    def schedule_via_rq(args, task):
        queue_name = args.queue_name
        scheduler = rq_scheduler.Scheduler(
            connection=Redis(), queue_name=queue_name)
        if args.cron_string:
            return scheduler.cron(
                args.cron_string, func=task, timeout=args.timeout,
                kwargs={'ox_test_args' : args}, queue_name=queue_name)
        else:
            raise ValueError('No scheduling method for rq task.')

    @staticmethod
    def cancel_job(job):
        scheduler = rq_scheduler.Scheduler(connection=Redis())
        return scheduler.cancel(job)

    @staticmethod
    def cleanup_job(job_id):
        conn = Redis()
        failed_queue = get_failed_queue(conn)
        failed_queue.remove(job_id)
        return 'Removed job %s' % str(job_id)


    @staticmethod
    def requeue_job(job_id):
        conn = Redis()
        failed_queue = get_failed_queue(conn)
        result = failed_queue.requeue(job_id)
        return result

    @classmethod
    def launch_job(cls, job_id):
        logging.warning('Preparing to launch job with id %s', str(job_id))
        my_args = cls.jobid_to_argrec(job_id)
        task = TestingTask()
        queue_name = my_args.queue_name
        scheduler = rq_scheduler.Scheduler(
            connection=Redis(), queue_name=queue_name)
        # RQ schedular kind of lame in not accepting queue_name to enqueue_in
        # but should look at scheduler.queue_name; assert to verify that
        new_job = scheduler.enqueue_in(
            datetime.timedelta(0), func=task, ox_test_args=my_args)
        assert new_job.origin == queue_name
        logging.warning('Launching new job with args' + str(my_args))
        return new_job

    @staticmethod
    def jobid_to_argrec(job_id):
        scheduler = rq_scheduler.Scheduler(connection=Redis())
        old_job = Job.fetch(job_id, connection=scheduler.connection)
        ox_test_args = old_job.kwargs['ox_test_args']
        my_args = copy.deepcopy(ox_test_args)
        return my_args



    @staticmethod
    def find_job(target_job):
        scheduler = rq_scheduler.Scheduler(connection=Redis())
        job_list = scheduler.get_jobs()
        for job in job_list:
            if job.id == target_job:
                return job
        return None

    @staticmethod
    def get_failed_jobs():
        results = []
        conn = Redis()
        failed = get_failed_queue(conn)
        failed_jobs = failed.jobs
        for item in failed_jobs:
            kwargs = getattr(item, 'kwargs', {})
            if 'ox_herd_args' in kwargs or 'ox_test_args' in kwargs:
                results.append(item)
        return results
            
    @staticmethod
    def get_scheduled_tests():
        results = []
        scheduler = rq_scheduler.Scheduler(connection=Redis())
        jobs = scheduler.get_jobs()
        for item in jobs:
            try:
                ox_test_args = item.kwargs.get('ox_test_args', None)
                if ox_test_args is not None:
                    cron_string = item.meta.get('cron_string',None)
                    if cron_string:
                        my_item = copy.deepcopy(ox_test_args)
                        my_item.schedule = item.meta.get('cron_string','')
                        my_item.jid = item.id
                        results.append(my_item)
                    else:
                        logging.info('Skipping task without cron_string.'
                                     'Probably was just a one-off launch.')
            except Exception as problem:
                logging.warning(
                    'Skipping job %s in get_scheduled_tests due to exception %s',
                    str(item), problem)

        return results

    @staticmethod
    def get_queued_jobs(allowed_queues=None):
        queue = Queue(connection=Redis())
        all_jobs = queue.jobs
        if not allowed_queues:
            return all_jobs
        else:
            return [j for j in all_jobs if j.origin in allowed_queues]
