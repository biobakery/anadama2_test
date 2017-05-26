# -*- coding: utf-8 -*-
from __future__ import print_function
import time
import logging
import itertools
import traceback
import multiprocessing
import pickle as pickle
from collections import namedtuple

import six
from six.moves import queue
import cloudpickle

from .util import istask
from . import tracked
from .helpers import apply_sh

logger = logging.getLogger(__name__)


class TaskResult(namedtuple(
        "TaskResult", ["task_no", "error", "dep_keys", "dep_compares"])):
    """The result of a task's execution.

    :param task_no: The number of the task that produced this result.
    :type: int

    :param error: The error generated by executing the task. None if
      the task was successful.
    :type: str or None

    :param dep_keys: The list of ``.name`` attributes from the objects
      of type :class:`anadama2.tracked.Base` associated with
      this task. These keys are used with the storage backend to save
      successful task results.

    :type dep_keys: list of str

    :param dep_compares: The list of the results of the ``compare()``
      method from the objects of type
      :class:`anadama2.tracked.Base` associated with this
      task. These values are used with the storage backend to save
      successful task results.

    :type dep_keys: list of list of str

    """
    pass



class TaskFailed(Exception):
    def __init__(self, msg, task_no):
        self.task_no = task_no
        super(TaskFailed, self).__init__(msg)


class BaseRunner(object):
    def __init__(self, run_context):
        self.ctx = run_context
        self.quit_early = False

    def run_tasks(self, task_idx_deque):
        raise NotImplementedError()


class DryRunner(BaseRunner):

    _typemap = {
        tracked.TrackedString: "String",
        tracked.TrackedVariable: "Variable",
        tracked.TrackedFile: "File",
        tracked.HugeTrackedFile: "Big File",
        tracked.TrackedDirectory: "Directory",
        tracked.TrackedFilePattern: "File Pattern",
        tracked.TrackedExecutable: "Executable",
        tracked.TrackedFunction: "Python Function",
    }

    def run_tasks(self, task_idx_deque):
        for idx in task_idx_deque:
            task = self.ctx.tasks[idx]
            six.print_("{} - {}".format(task.task_no, task.name))
            six.print_("  Dependencies ({})".format(len(task.depends)))
            for dep in task.depends:
                six.print_(self._depformat(dep))
            six.print_("  Targets ({})".format(len(task.targets)))
            for dep in task.targets:
                six.print_(self._depformat(dep))
            six.print_("------------------")


    def _depformat(self, d):
        if istask(d):
            return "  - Task {} - {}".format(d.task_no, d.name)
        else:
            t = type(d)
            desc = self._typemap.get(t, str(t))
            return "  - {} ({})".format(d.name, desc)



class SerialLocalRunner(BaseRunner):

    def run_tasks(self, task_idx_deque):
        total = len(task_idx_deque)
        logger.debug("Running %i tasks locally and serially", total)
        while task_idx_deque:
            idx = task_idx_deque.pop()

            parents = set(self.ctx.dag.predecessors(idx))
            failed_parents = parents.intersection(self.ctx.failed_tasks)
            if failed_parents:
                self.ctx._handle_task_result(
                    parent_failed_result(idx, next(iter(failed_parents))))
                continue

            self.ctx._handle_task_started(idx)
            self.ctx._reporter.task_running(idx)
            self.ctx._reporter.task_command(idx)
            result = _run_task_locally(self.ctx.tasks[idx])
            self.ctx._handle_task_result(result)

            if self.quit_early and bool(result.error):
                logger.debug("Quitting early")
                break


def worker_run_loop(work_q, result_q, run_task, reporter=None, lock=None):
    logger.debug("Starting worker")
    while True:
        try:
            logger.debug("Getting work")
            pkl, extra = work_q.get()
            logger.debug("Got work")
        except IOError as e:
            logger.debug("Received IOError (%s) errno %s from work_q",
                              e.message, e.errno)
            break
        except EOFError:
            logger.debug("Received EOFError from work_q")
            break
        if type(pkl) is dict and pkl.get("stop", False):
            logger.debug("Received sentinel, stopping")
            break
        try:
            logger.debug("Deserializing task")
            task = pickle.loads(pkl)
            logger.debug("Task deserialized")
        except Exception as e:
            result_q.put_nowait(exception_result(e))
            logger.debug("Failed to deserialize task")
            continue
        logger.debug("Running task %s with %s", task.task_no, run_task)
        if reporter:
            if lock is not None:
                lock.acquire()
            reporter.task_running(task.task_no)
            reporter.task_command(task.task_no)
            if lock is not None:
                lock.release()
        result = run_task(task, extra)
        logger.debug("Finished running task; "
                          "putting results on result_q")
        result_q.put_nowait(result)
        logger.debug("Result put on result_q. Back to get more work.")
    

def _run_task_locally(task, extra=None):
    # convert any command strings to sh actions before running
    for i, action_func in enumerate(apply_sh(task.actions)):
        logger.debug("Executing task %i action %i", task.task_no, i)
        try:
            action_func(task)
        except Exception:
            msg = ("Error executing action {}. "
                   "Original Exception: \n{}")
            return exception_result(
                TaskFailed(msg.format(i, traceback.format_exc()), task.task_no)
                )
        logger.debug("Completed executing task %i action %i", task.task_no, i)

    return _get_task_result(task)

def _get_task_result(task):
    targ_keys, targ_compares = list(), list()
    for target in task.targets:
        targ_keys.append(target.name)
        try:
            targ_compares.append(list(target.compare()))
        except Exception:
            msg = "Failed to produce target `{}'. Original exception: {}"
            return exception_result(
                TaskFailed(msg.format(target, traceback.format_exc()),
                           task.task_no)
                )
            
    return TaskResult(task.task_no, None, targ_keys, targ_compares)

    
class ParallelLocalWorker(multiprocessing.Process):
            
    def __init__(self, work_q, result_q, lock, reporter):
        super(ParallelLocalWorker, self).__init__()
        self.logger = logger
        self.work_q = work_q
        self.result_q = result_q
        self.lock = lock
        self.reporter = reporter


    @staticmethod
    def appropriate_q_class(*args, **kwargs):
        return multiprocessing.Queue(*args, **kwargs)
    
    @staticmethod
    def appropriate_lock():
        return multiprocessing.Lock()

    def run(self):
        return worker_run_loop(self.work_q, self.result_q, _run_task_locally, self.reporter, self.lock)


class ParallelLocalRunner(BaseRunner):

    def __init__(self, run_context, jobs):
        super(ParallelLocalRunner, self).__init__(run_context)
        self.work_q = multiprocessing.Queue()
        self.result_q = multiprocessing.Queue()
        self.lock = multiprocessing.Lock()
        self.reporter = run_context._reporter
        self.workers = [ ParallelLocalWorker(self.work_q, self.result_q, self.lock, self.reporter)
                         for _ in range(jobs) ]
        self.started = False


    def run_tasks(self, task_idx_deque):
        self.task_idx_deque = task_idx_deque
        logger.debug("Running %i tasks in parallel with %i workers locally",
                      len(task_idx_deque), len(self.workers))
        
        self.n_to_do = len(task_idx_deque)
        while True:
            self._fill_work_q()
            logger.debug("Tasks left to do: %s", self.n_to_do)
            if self.n_to_do <= 0:
                logger.debug("No new tasks added to work queues and"
                             " the number of completed tasks equals the"
                             " number of tasks to do. Job's done")
                break
            try:
                result = self.result_q.get()
            except (SystemExit, KeyboardInterrupt):
                logger.info("Terminating due to SystemExit or Ctrl-C")
                self.terminate()
                raise
            except Exception as e:
                logger.error("Terminating due to unhandled exception")
                logger.exception(e)
                self.terminate()
                raise
            else:
                self.n_to_do -= 1
                self.ctx._handle_task_result(result)
            if self.quit_early and result.error:
                logger.debug("Quitting early.")
                self.terminate()
                break

        self.cleanup()


    def _fill_work_q(self):
        logger.debug("Filling work_q")
        for _ in range(len(self.task_idx_deque)):
            idx = self.task_idx_deque.pop()
            parents = set(self.ctx.dag.predecessors(idx))
            failed_parents = parents.intersection(self.ctx.failed_tasks)
            if failed_parents:
                self.ctx._handle_task_result(
                    parent_failed_result(idx, failed_parents.pop()))
                self.n_to_do -= 1
                continue
            elif parents.difference(self.ctx.completed_tasks):
                # has undone parents, come back again later
                self.task_idx_deque.appendleft(idx)
                continue
            try:
                pkl = cloudpickle.dumps(self.ctx.tasks[idx])
            except Exception as e:
                msg = ("Unable to serialize task `{}'. "
                       "Original error was `{}'.")
                raise ValueError(msg.format(self.ctx.tasks[idx], e))
            logger.debug("Adding task %i to work_q", idx)
            self.ctx._handle_task_started(idx)
            self.work_q.put((pkl, None))
            logger.debug("Added task %i to work_q", idx)
        if not self.started:
            logger.debug("Starting up workers")
            for w in self.workers:
                w.start()
            self.started = True


    def terminate(self):
        logger.debug("Terminating all workers")
        self.work_q._rlock.acquire()
        logger.debug("got work_q readlock")
        while self.work_q._reader.poll():
            logger.debug("draining work_q")
            try:
                self.work_q._reader.recv()
            except EOFError:
                break
            time.sleep(0)
        for worker in self.workers:
            logger.debug("terminating worker %s", worker)
            worker.terminate()
        for worker in self.workers:
            logger.debug("joining worker %s", worker)
            worker.join()
        logger.debug("releasing readlock")
        self.work_q._rlock.release()
        logger.debug("termination complete")


    def cleanup(self):
        logger.debug("cleaning up parallellocalrunner")
        for w in self.workers:
            logger.debug("giving stop sentinel to worker %s", w)
            self.work_q.put(({"stop": True}, None))
        for w in self.workers:
            logger.debug("joining worker %s", w)
            w.join()
        logger.debug("successfully cleaned up parallellocalrunner")


class GridRunner(BaseRunner):

    def __init__(self, workflow):
        super(GridRunner, self).__init__(workflow)
        self._worker_config = dict()
        self._worker_qs = dict()
        self.routes = dict() # task_no -> (worker_type_name, extra_args)
        self.workers = list()
        self.started = False
        self.default_worker = None


    def add_worker(self, worker_class, name,
                   rate=1, default=False):
        self._worker_config[name] = (worker_class, rate)
        if default:
            self.default_worker = name


    def run_tasks(self, task_idx_deque):
        self.task_idx_deque = task_idx_deque
        if not self.workers:
            self._init_workers()

        logger.debug("Running %i tasks in parallel with %i workers"
                      " using the grid",
                     len(task_idx_deque), len(self.workers))
        self.n_to_do = len(task_idx_deque)
        while True:
            self._fill_work_qs()
            logger.debug("Tasks left to do: %s", self.n_to_do)
            if self.n_to_do <= 0:
                break
            try:
                result = self._get_result()
            except (SystemExit, KeyboardInterrupt):
                logger.info("Terminating due to SystemExit or Ctrl-C")
                self.terminate()
                raise
            except Exception as e:
                logger.error("Terminating due to unhandled exception")
                logger.exception(e)
                self.terminate()
                raise
            else:
                self.n_to_do -= 1
                self.ctx._handle_task_result(result)
            if self.quit_early and result.error:
                logger.debug("Quitting early.")
                self.terminate()
                break

        self.cleanup()


    def terminate(self):
        for name, (work_q, _) in self._worker_qs.items():
            if hasattr(work_q, "_rlock"):
                self._terminate_mpq(work_q, name)
            elif hasattr(work_q, "mutex"):
                self._terminate_qq(work_q, name)
            else:
                raise Exception
        for worker in self.workers:
            worker.join()


    def cleanup(self):
        for name, (_, n_procs) in self._worker_config.items():
            for _ in range(n_procs):
                self._worker_qs[name][0].put(({"stop": True}, None))
        for w in self.workers:
            w.join()


    def route(self, task_no):
        if task_no in self.routes:
            return self.routes[task_no]
        elif self.default_worker is not None:
            return self.default_worker, None
        else:
            msg = ("GridRunner tried to run task {} but has no "
                   "runner to run it and no default runner is defined")
            raise ValueError(msg.format(task_no))


    def _fill_work_qs(self):
        logger.debug("Filling work_qs")
        for _ in range(len(self.task_idx_deque)):
            idx = self._get_next_task()
            if idx is None:
                continue
            try:
                pkl = cloudpickle.dumps(self.ctx.tasks[idx])
            except Exception as e:
                msg = ("Unable to serialize task `{}'. "
                       "Original error was `{}'.")
                raise ValueError(msg.format(self.ctx.tasks[idx], e))
            name, extra = self.route(idx)
            logger.debug("Adding task %i to `%s' work_q", idx, name)
            self._worker_qs[name][0].put((pkl, extra))
            self.ctx._handle_task_started(idx)
            logger.debug("Added task %i to `%s' work_q", idx, name)
        if not self.started:
            logger.debug("Starting up workers")
            for w in self.workers:
                logger.debug("Starting worker %s", w)
                w.start()
            self.started = True


    def _init_workers(self):
        threads, procs = list(), list()
        for name, (worker_cls, n_procs) in self._worker_config.items():
            work_q = worker_cls.appropriate_q_class()
            result_q = worker_cls.appropriate_q_class()
            lock = worker_cls.appropriate_lock()
            self._worker_qs[name] = (work_q, result_q)
            isproc = issubclass(worker_cls, multiprocessing.Process) 
            l = procs if isproc else threads
            for _ in range(n_procs):
                l.append(worker_cls(work_q, result_q, lock, self.ctx._reporter))
        self.workers = procs+threads # http://stackoverflow.com/a/13115499
        self._qcycle = itertools.cycle(val[1]
                                       for val in self._worker_qs.values())


    def _get_next_task(self):
        idx = self.task_idx_deque.pop()
        parents = set(self.ctx.dag.predecessors(idx))
        failed_parents = parents.intersection(self.ctx.failed_tasks)
        if failed_parents:
            self.ctx._handle_task_result(
                parent_failed_result(idx, next(iter(failed_parents)))
                )
            self.n_to_do -= 1
            return None
        elif parents.difference(self.ctx.completed_tasks):
            # has undone parents, come back again later
            self.task_idx_deque.appendleft(idx)
            return None
        if idx is None:
            raise Exception
        return idx


    def _get_result(self):
        while True:
            for _ in range(len(self.workers)):
                try:
                    ret = next(self._qcycle).get(False)
                except queue.Empty:
                    continue
                return ret
            time.sleep(0.05)


    def _terminate_mpq(self, q, name):
        q._rlock.acquire()
        while q._reader.poll():
            try:
                q._reader.recv()
            except EOFError:
                break
            time.sleep(0)
        worker_type = self._worker_config[name][0]
        for worker in self.workers:
            if isinstance(worker, worker_type):
                worker.terminate()
        q._rlock.release()


    def _terminate_qq(self, q, name):
        q.mutex.acquire()
        while q.queue:
            q.queue.pop()
        worker_type = self._worker_config[name][0]
        for worker in self.workers:
            if isinstance(worker, worker_type):
                worker.join()
        q.mutex.release()
        


def default(run_context, jobs):
    if jobs < 2:
        return SerialLocalRunner(run_context)
    else:
        return ParallelLocalRunner(run_context, jobs)


_current_grid_runner = None
def current_grid_runner(context):
    global _current_grid_runner
    if _current_grid_runner is None:
        _current_grid_runner = GridRunner(context)
    return _current_grid_runner

    
def exception_result(exc):
    return TaskResult(getattr(exc, "task_no", None), str(exc), None, None)


def parent_failed_result(idx, parent_idx):
    return TaskResult(
        idx, "Task failed because parent task `{}' failed".format(parent_idx),
        None, None)


