import sys
import six
import codecs
from operator import attrgetter

from doit.cmd_run import Run as DoitRun
from doit.cmd_help import Help as DoitHelp
from doit.cmd_list import List as DoitList
from doit.task import Task
from doit.control import TaskControl
from doit.runner import Runner, MRunner, MThreadRunner
from doit.reporter import REPORTERS
from doit.cmd_base import DoitCmdBase
from doit.cmdparse import CmdOption
from doit.exceptions import InvalidCommand

from . import dag
from .runner import RUNNER_MAP

opt_runner = dict(
    name  = "runner",
    long  = "runner",
    default = "MRunner",
    help = ("Runner to use for executing tasks."
            " Choices: "+",".join(RUNNER_MAP.keys()) ),
    type=str
)

opt_tmpfiles = dict(
    name    = "tmpfiledir",
    long    = "tmpfiledir",
    default = "/tmp",
    help    = "Where to save temporary files",
    type=str
)

class AnadamaCmdBase(DoitCmdBase):
    my_base_opts = ()
    my_opts = ()

    def set_options(self):
        opt_list = (self.base_options + self._loader.cmd_options +
                    self.cmd_options + self.my_base_opts + self.my_opts)
        return [CmdOption(opt) for opt in opt_list]


class Run(AnadamaCmdBase, DoitRun):
    my_opts = (opt_runner,)

    def _execute(self, outfile,
                 verbosity=None, always=False, continue_=False,
                 reporter='default', num_process=0, par_type='process',
                 single=False):
        """
        @param reporter: (str) one of provided reporters or ...
                         (class) user defined reporter class 
                                 (can only be specified
                                 from DOIT_CONFIG - never from command line)
                         (reporter instance) - only used in unittests
        """
        # get tasks to be executed
        # self.control is saved on instance to be used by 'auto' command
        self.control = TaskControl(self.task_list)
        self.control.process(self.sel_tasks)

        if single:
            for task_name in self.sel_tasks:
                task = self.control.tasks[task_name]
                if task.has_subtask:
                    for task_name in task.task_dep:
                        sub_task = self.control.tasks[task_name]
                        sub_task.task_dep = []
                else:
                    task.task_dep = []

        # reporter
        if isinstance(reporter, six.string_types):
            if reporter not in REPORTERS:
                msg = ("No reporter named '%s'."
                       " Type 'doit help run' to see a list "
                       "of available reporters.")
                raise InvalidCommand(msg % reporter)
            reporter_cls = REPORTERS[reporter]
        else:
            # user defined class
            reporter_cls = reporter

        # verbosity
        if verbosity is None:
            use_verbosity = Task.DEFAULT_VERBOSITY
        else:
            use_verbosity = verbosity
        show_out = use_verbosity < 2 # show on error report

        # outstream
        if isinstance(outfile, six.string_types):
            outstream = codecs.open(outfile, 'w', encoding='utf-8')
        else: # outfile is a file-like object (like StringIO or sys.stdout)
            outstream = outfile

        # run
        try:
            # FIXME stderr will be shown twice in case of task error/failure
            if isinstance(reporter_cls, type):
                reporter_obj = reporter_cls(outstream, {'show_out':show_out,
                                                        'show_err': True})
            else: # also accepts reporter instances
                reporter_obj = reporter_cls


            run_args = [self.dep_class, self.dep_file, reporter_obj,
                        continue_, always, verbosity]
            if num_process > 0:
                run_args.append(run_args)

            RunnerClass = RUNNER_MAP.get(self.opt_values["runner"])
            if not RunnerClass:
                RunnerClass = self._discover_runner_class(
                    num_process, par_type)

            runner = RunnerClass(*run_args)
            return runner.run_all(self.control.task_dispatcher())
        finally:
            if isinstance(outfile, str):
                outstream.close()

    def _discover_runner_class(self, num_process, par_type):
        if num_process == 0:
            return Runner
        else:
            if par_type == 'process':
                RunnerClass = MRunner
                if MRunner.available():
                    return MRunner
                else:
                    RunnerClass = MThreadRunner
                    sys.stderr.write(
                        "WARNING: multiprocessing module not available, " +
                        "running in parallel using threads.")
            elif par_type == 'thread':
                    return MThreadRunner
            else:
                msg = "Invalid parallel type %s"
                raise InvalidCommand(msg % par_type)



class ListDag(Run):
    my_opts = (opt_runner, opt_tmpfiles)
    name = "dag"
    dog_purpose = "print execution tree"
    doc_usage = "[TASK ...]"

    def _execute(self, outfile, *args, **kwargs):
        self.opt_values['runner'] = 'jenkins'
        dag.TMP_FILE_DIR = self.opt_values["tmpfiledir"]
        return super(ListDag, self)._execute(outfile, *args, **kwargs)


class Help(DoitHelp):

    @staticmethod
    def print_usage(cmds):
        """Print anadama usage instructions"""
        print("AnADAMA -- https://bitbucket.org/biobakery/anadama")
        print('')
        print("Commands")
        for cmd in sorted(six.itervalues(cmds), key=attrgetter('name')):
            six.print_("  anadama %s \t\t %s" % (cmd.name, cmd.doc_purpose))
        print("")
        print("  anadama help              show help / reference")
        print("  anadama help task         show help on task fields")
        print("  anadama help <command>    show command usage")
        print("  anadama help <task-name>  show task usage")


all = (Run, ListDag, Help)
