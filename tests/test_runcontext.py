import os
import time
import shutil
import unittest
from datetime import datetime
from datetime import timedelta
from cStringIO import StringIO

import networkx

import anadama
import anadama.deps
import anadama.runcontext
import anadama.util
import anadama.backends

from .util import capture


class TestRunContext(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ[anadama.backends.ENV_VAR] = "/tmp/anadamatest"
    
    @classmethod
    def tearDownClass(cls):
        if os.path.isdir("/tmp/anadamatest"):
            shutil.rmtree("/tmp/anadamatest")


    def setUp(self):
        self.ctx = anadama.runcontext.RunContext()
        self.workdir = "/tmp/anadama_testdir"
        if not os.path.isdir(self.workdir):
            os.mkdir(self.workdir)

    def tearDown(self):
        if self.ctx._backend:
            self.ctx._backend.close()
            del self.ctx._backend
            self.ctx._backend = None
            anadama.backends._default_backend = None
            
        if os.path.isdir(self.workdir):
            shutil.rmtree(self.workdir)
        

    def test_hasattributes(self):
        self.assertIsInstance(self.ctx.dag,
                              networkx.classes.digraph.DiGraph)
        self.assertIs(type(self.ctx.tasks), list)
        self.assertTrue(hasattr(self.ctx, "task_counter"))

    def test_do_simple(self):
        t1 = self.ctx.do("echo true", track_cmd=False, track_binaries=False)
        self.assertTrue(isinstance(t1, anadama.Task))
        self.assertIs(t1, self.ctx.tasks[0])
        self.assertEqual(len(t1.depends), 0)
        self.assertEqual(len(t1.targets), 0)
        self.assertEqual(len(t1.actions), 1)

    def test_do_track_cmd(self):
        t1 = self.ctx.do("echo true", track_binaries=False)
        self.assertEqual(len(t1.depends), 1)
        self.assertEqual(len(t1.targets), 0)
        self.assertEqual(len(t1.actions), 1)
        self.assertTrue(isinstance(t1.depends[0],
                                   anadama.deps.StringDependency))
        
    def test_do_track_binaries(self):
        t1 = self.ctx.do("echo true", track_cmd=False)
        self.assertEqual(len(t1.depends), 2)
        self.assertEqual(len(t1.targets), 0)
        self.assertEqual(len(t1.actions), 1)
        self.assertTrue(isinstance(t1.depends[0],
                                   anadama.deps.ExecutableDependency))
        self.assertTrue(isinstance(t1.depends[1],
                                   anadama.deps.ExecutableDependency))


    def test_discover_binaries(self):
        bash_script = os.path.join(self.workdir, "test.sh")
        with open(bash_script, 'w') as f:
            print >> f, "#!/bin/bash"
            print >> f, "echo hi"
        os.chmod(bash_script, 0o755)
        plain_file = os.path.join(self.workdir, "blah.txt")
        with open(plain_file, 'w') as f:
            print >> f, "nothing to see here"
        ret = anadama.runcontext.discover_binaries("echo hi")
        self.assertGreater(len(ret), 0, "should find /bin/echo")
        self.assertTrue(isinstance(ret[0], anadama.deps.ExecutableDependency))
        self.assertEqual(str(ret[0]), "/bin/echo")

        ret2 = anadama.runcontext.discover_binaries("/bin/echo foo")
        self.assertIs(ret[0], ret2[0], "should discover the same dep")
        
        ret = anadama.runcontext.discover_binaries(
            bash_script+" arguments dont matter")
        self.assertEqual(len(ret), 1, "should just find one dep")
        self.assertTrue(isinstance(ret[0], anadama.deps.ExecutableDependency))
        self.assertEqual(str(ret[0]), bash_script)

        ret = anadama.runcontext.discover_binaries(plain_file+" blah blah")
        self.assertEqual(len(ret), 0, "shouldn't discover unexecutable files")
        

    def test_do_targets(self):
        def closure(*args, **kwargs):
            return args, kwargs
        self.ctx.add_task = closure
        args, kws = self.ctx.do("echo true > @{true.txt}")
        self.assertIn("true.txt", args[0],
                      "target shouldn't be removed from command string")
        self.assertNotIn(args[0], "@{true.txt}", "target metachar not removed")
        self.assertEqual(len(args[2]), 1, "Should only be one target")
        self.assertEqual(args[2][0], "true.txt",
                         "the target should be a filedependency, true.txt")

    def test_do_deps(self):
        def closure(*args, **kwargs):
            return args, kwargs
        self.ctx.add_task = closure
        args, kws = self.ctx.do("cat #{/etc/hosts} > @{hosts.txt}",
                                track_cmd=False, track_binaries=False)
        self.assertNotIn("#{/etc/hosts}", args[1],
                         "dep metachar not removed")
        self.assertIn("/etc/hosts", args[0],
                      "dep shouldn't be removed from command string")
        self.assertEqual(len(args[1]), 1, "Should only be one dep")
        self.assertEqual(args[1][0], "/etc/hosts",
                         "the dep should be a filedependency, /etc/hosts")


    
    def test_add_task(self):
        t1 = self.ctx.add_task(anadama.util.noop)
        self.assertIsInstance(t1, anadama.Task)
        self.assertIs(t1.actions[0], anadama.util.noop)
        self.assertEqual(len(t1.depends), 0)
        self.assertEqual(len(t1.targets), 0)
        

    def test_add_task_deps(self):
        self.ctx.already_exists("/etc/hosts")
        t1 = self.ctx.add_task(anadama.util.noop, depends=["/etc/hosts"])
        self.assertEqual(len(t1.depends), 1)
        self.assertEqual(len(t1.targets), 0)
        self.assertIs(t1.depends[0], anadama.deps.FileDependency("/etc/hosts"),
                      "the dep should be a filedependency, /etc/hosts")


    def test_add_task_targs(self):
        t1 = self.ctx.add_task(anadama.util.noop, targets=["/tmp/test.txt"])
        self.assertEqual(len(t1.depends), 0)
        self.assertEqual(len(t1.targets), 1)
        self.assertIs(t1.targets[0],
                      anadama.deps.FileDependency("/tmp/test.txt"),
                      "the target should be a filedependency, /tmp/test.txt")

    def test_add_task_decorator(self):
        ctx = self.ctx
        @ctx.add_task(targets=["/tmp/test.txt"])
        def closure(*args, **kwargs):
            return "testvalue"

        self.assertEqual(len(ctx.tasks), 1, "decorator should add one task")
        self.assertIsInstance(
            ctx.tasks[0], anadama.Task,
            "decorator should add a task instance to context.tasks")
        self.assertEqual(len(ctx.tasks[0].targets), 1,
                         "The created task should have one target")
        self.assertIs(ctx.tasks[0].targets[0],
                      anadama.deps.FileDependency("/tmp/test.txt"),
                      "the target should be a filedependency, /tmp/test.txt")
        ret = ctx.tasks[0].actions[0]()
        self.assertEqual(ret, "testvalue",
                         "the action should be the same function I gave it")


    def test_go(self):
        self.ctx.already_exists("/etc/hosts")
        outf = os.path.join(self.workdir, "wordcount.txt")
        self.ctx.add_task("wc -l {depends[0]} > {targets[0]}",
                          depends=["/etc/hosts"], targets=[outf] )
        
        with capture(stderr=StringIO()):
            self.ctx.go()
        self.assertTrue(os.path.exists(outf), "should create wordcount.txt")
        
        
    def test_go_parallel(self):
        for _ in range(10):
            self.ctx.add_task("sleep 0.5")
        earlier = datetime.now()
        with capture(stderr=StringIO()):
            self.ctx.go(n_parallel=10)
        later = datetime.now()
        self.assertLess(later-earlier, timedelta(seconds=5))

    def test_go_quit_early(self):
        outf = os.path.join(self.workdir, "blah.txt")
        out2 = os.path.join(self.workdir, "shouldntexist.txt")
        self.ctx.add_task("echo blah > {targets[0]}; exit 1", targets=[outf])
        self.ctx.add_task("cat {depends[0]} > {targets[0]}",
                          depends=[outf], targets=[outf])

        with capture(stderr=StringIO()):
            with self.assertRaises(anadama.runcontext.RunFailed):
                self.ctx.go(quit_early=True)

        self.assertFalse(
            os.path.exists(out2),
            "quit_early failed to stop before the second task was run")

    def test_go_skip(self):
        outf = os.path.join(self.workdir, "blah.txt")
        self.ctx.add_task("touch {targets[0]}", targets=[outf])
        with capture(stderr=StringIO()):
            self.ctx.go()
        ctime = os.stat(outf).st_ctime
        time.sleep(1)
        with capture(stderr=StringIO()):
            self.ctx.go()
        self.assertEqual(ctime, os.stat(outf).st_ctime)
       

if __name__ == "__main__":
    unittest.main()
