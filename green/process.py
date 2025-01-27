import logging
import multiprocessing
from multiprocessing.pool import Pool
import os
import random
import shutil
import sys
import tempfile
import traceback

try: # pragma: no cover
    import coverage
except: # pragma: no cover
    coverage = None

from green.exceptions import InitializerOrFinalizerError
from green.loader import loadTargets
from green.result import proto_test, ProtoTest, ProtoTestResult



# Super-useful debug function for finding problems in the subprocesses, and it
# even works on windows
def ddebug(msg, err=None): # pragma: no cover
    """
    err can be an instance of sys.exc_info() -- which is the latest traceback
    info
    """
    if err:
        err = ''.join(traceback.format_exception(*err))
    else:
        err = ''
    sys.__stdout__.write("({}) {} {}".format(os.getpid(), msg, err)+'\n')
    sys.__stdout__.flush()



class ProcessLogger(object):
    """
    I am used by LoggingDaemonlessPool to get crash output out to the logger,
    instead of having process crashes be silent
    """


    def __init__(self, callable):
        self.__callable = callable


    def __call__(self, *args, **kwargs):
        try:
            result = self.__callable(*args, **kwargs)
        except Exception:
            # Here we add some debugging help. If multiprocessing's
            # debugging is on, it will arrange to log the traceback
            logger = multiprocessing.get_logger()
            if not logger.handlers:
                logger.addHandler(logging.StreamHandler())
            logger.error(traceback.format_exc())
            logger.handlers[0].flush()
            # Re-raise the original exception so the Pool worker can
            # clean up
            raise

        # It was fine, give a normal answer
        return result



class DaemonlessProcess(multiprocessing.Process):
    """
    I am used by LoggingDaemonlessPool to make pool workers NOT run in
    daemon mode (daemon mode process can't launch their own subprocesses)
    """


    def _get_daemon(self):
        return False


    def _set_daemon(self, value):
        pass


    # 'daemon' attribute needs to always return False
    daemon = property(_get_daemon, _set_daemon)



class LoggingDaemonlessPool(Pool):
    """
    I use ProcessLogger and DaemonlessProcess to make a pool of workers.
    """

    Process = DaemonlessProcess


    def apply_async(self, func, args=(), kwds={}, callback=None):
        return Pool.apply_async(
                self, ProcessLogger(func), args, kwds, callback)

#-------------------------------------------------------------------------------
# START of Worker Finalization Monkey Patching
#
# I started with code from cpython/Lib/multiprocessing/pool.py from version
# 3.5.0a4+ of the main python mercurial repository.  Then altered it to run on
# 2.7+ and added the finalizer/finalargs parameter handling.
    _wrap_exception = True

    def __init__(self, processes=None, initializer=None, initargs=(),
                 maxtasksperchild=None, context=None, finalizer=None,
                 finalargs=()):
        self._finalizer = finalizer
        self._finalargs = finalargs
        super(LoggingDaemonlessPool, self).__init__(processes, initializer,
                initargs, maxtasksperchild)


    def _repopulate_pool(self):
        """
        Bring the number of pool processes up to the specified number, for use
        after reaping workers which have exited.
        """
        for i in range(self._processes - len(self._pool)):
            w = self.Process(target=worker,
                             args=(self._inqueue, self._outqueue,
                                   self._initializer,
                                   self._initargs, self._maxtasksperchild,
                                   self._wrap_exception,
                                   self._finalizer,
                                   self._finalargs)
                            )
            self._pool.append(w)
            w.name = w.name.replace('Process', 'PoolWorker')
            w.daemon = True
            w.start()
            util.debug('added worker')


import platform
import multiprocessing.pool
from multiprocessing import util
from multiprocessing.pool import MaybeEncodingError

# Python 2 and 3 raise a different error when they exit
if platform.python_version_tuple()[0] == '2': # pragma: no cover
    PortableOSError = IOError
else: # pragma: no cover
    PortableOSError = OSError


def worker(inqueue, outqueue, initializer=None, initargs=(), maxtasks=None,
        wrap_exception=False, finalizer=None, finalargs=()): # pragma: no cover
    assert maxtasks is None or (type(maxtasks) == int and maxtasks > 0)
    put = outqueue.put
    get = inqueue.get
    if hasattr(inqueue, '_writer'):
        inqueue._writer.close()
        outqueue._reader.close()

    if initializer is not None:
        try:
            initializer(*initargs)
        except InitializerOrFinalizerError as e:
            print(str(e))

    completed = 0
    while maxtasks is None or (maxtasks and completed < maxtasks):
        try:
            task = get()
        except (EOFError, PortableOSError):
            util.debug('worker got EOFError or OSError -- exiting')
            break

        if task is None:
            util.debug('worker got sentinel -- exiting')
            break

        job, i, func, args, kwds = task
        try:
            result = (True, func(*args, **kwds))
        except Exception as e:
            if wrap_exception:
                e = ExceptionWithTraceback(e, e.__traceback__)
            result = (False, e)
        try:
            put((job, i, result))
        except Exception as e:
            wrapped = MaybeEncodingError(e, result[1])
            util.debug("Possible encoding error while sending result: %s" % (
                wrapped))
            put((job, i, (False, wrapped)))
        completed += 1

    if finalizer:
        try:
            finalizer(*finalargs)
        except InitializerOrFinalizerError as e:
            print(str(e))

    util.debug('worker exiting after %d tasks' % completed)



# Unmodified (see above)
class RemoteTraceback(Exception): # pragma: no cover
    def __init__(self, tb):
        self.tb = tb
    def __str__(self):
        return self.tb


# Unmodified (see above)
class ExceptionWithTraceback: # pragma: no cover
    def __init__(self, exc, tb):
        tb = traceback.format_exception(type(exc), exc, tb)
        tb = ''.join(tb)
        self.exc = exc
        self.tb = '\n"""\n%s"""' % tb
    def __reduce__(self):
        return rebuild_exc, (self.exc, self.tb)


# Unmodified (see above)
def rebuild_exc(exc, tb): # pragma: no cover
    exc.__cause__ = RemoteTraceback(tb)
    return exc

multiprocessing.pool.worker = worker
# END of Worker Finalization Monkey Patching
#-------------------------------------------------------------------------------


def poolRunner(target, queue, coverage_number=None, omit_patterns=[]): # pragma: no cover
    """
    I am the function that pool worker processes run.  I run one unit test.
    """
    # Each pool worker gets his own temp directory, to avoid having tests that
    # are used to taking turns using the same temp file name from interfering
    # with eachother.  So long as the test doesn't use a hard-coded temp
    # directory, anyway.
    saved_tempdir = tempfile.tempdir
    tempfile.tempdir = tempfile.mkdtemp()

    # Each pool starts its own coverage, later combined by the main process.
    if coverage_number and coverage:
        cov = coverage.coverage(
                data_file='.coverage.{}_{}'.format(
                    coverage_number, random.randint(0, 10000)),
                omit=omit_patterns)
        cov._warn_no_data = False
        cov.start()

    # What to do each time an individual test is started
    def start_callback(test):
        # Let the main process know what test we are starting
        queue.put(proto_test(test))

    def stop_callback(test_result):
        # Let the main process know what happened with the test run
        queue.put(test_result)

    result = ProtoTestResult(start_callback, stop_callback)
    test = None
    try:
        test = loadTargets(target)
    except:
        err = sys.exc_info()
        t             = ProtoTest()
        t.module      = 'green.loader'
        t.class_name  = 'N/A'
        t.description = 'Green encountered an error loading the unit test.'
        t.method_name = 'poolRunner'
        result.startTest(t)
        result.addError(t, err)
        result.stopTest(t)

    if getattr(test, 'run', False):
        # Loading was successful, lets do this
        try:
            test.run(result)
        except:
            # Some frameworks like testtools record the error AND THEN let it
            # through to crash things.  So we only need to manufacture another error
            # if the underlying framework didn't, but either way we don't want to
            # crash.
            if not result.errors:
                err = sys.exc_info()
                t             = ProtoTest()
                t.module      = 'green.runner'
                t.class_name  = 'N/A'
                t.description = 'Green encountered an exception not caught by the underlying test framework.'
                t.method_name = 'poolRunner'
                result.startTest(t)
                result.addError(t, err)
                result.stopTest(t)
    else:
        # loadTargets() returned an object without a run() method, probably None
        description = 'Test loader returned an un-runnable object: {} of type {} with dir {}'.format(
                str(test), type(test), dir(test))
        err = (TypeError, TypeError(description), None)
        t             = ProtoTest()
        t.module      = '.'.join(target.split('.')[:-2])
        t.class_name  = target.split('.')[-2]
        t.description = description
        t.method_name = target.split('.')[-1]
        result.startTest(t)
        result.addError(t, err)
        result.stopTest(t)

    # Finish coverage
    if coverage_number and coverage:
        cov.stop()
        cov.save()

    # Restore the state of the temp directory
    shutil.rmtree(tempfile.tempdir)
    tempfile.tempdir = saved_tempdir
    queue.put(None)
    return None
