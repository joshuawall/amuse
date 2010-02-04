import time
import urlparse
import threading
import traceback
import json
import nose
import sys
import linecache
import inspect
import os.path
import BaseHTTPServer
import SocketServer
import Queue as queue
import webbrowser

from mpi4py import MPI
from nose.plugins.capture import Capture
from nose.plugins.skip import Skip, SkipTest
from nose.plugins.doctests import Doctest
from nose.core import TestProgram
from multiprocessing import Process, Queue

from optparse import OptionParser
from subprocess import call, Popen, PIPE
from StringIO import StringIO

import webserver
import monitor

def number_str(number, singular, plural = None):
    if plural == None:
        plural = singular + 's'
    return str(number) + ' ' + (singular if number == 1 else plural)

def find_method_in_class(name_of_the_method, code, class_to_search):
    if name_of_the_method in class_to_search.__dict__:
        member =  class_to_search.__dict__[name_of_the_method]
        if inspect.isfunction(member):
            if member.func_code == code:
                return member
        if inspect.ismethoddescriptor(member):
            pass
    return None

def extract_tb(tb, limit = None):
    list = []
    n = 0
    while tb is not None and (limit is None or n < limit):
        f = tb.tb_frame
        lineno = tb.tb_lineno
        co = f.f_code
        filename = co.co_filename
        name = co.co_name
        linecache.checkcache(filename)
        line = ""
        if '__file__' in f.f_globals:
            for global_name, x in f.f_globals.iteritems():
                if global_name.startswith('_'):
                   continue
                   
                if inspect.isfunction(x):
                    if global_name == name and x.func_code == co:
                        args, varargs, varkw, defaults = inspect.getargspec(x)
                        name += inspect.formatargspec(args, varargs, varkw, defaults)
                elif inspect.isclass(x):
                        method = find_method_in_class(name,co,x)
                        if not method is None:
                            args, varargs, varkw, defaults = inspect.getargspec(method)
                            name += inspect.formatargspec(args, varargs, varkw, defaults)
                            name = x.__name__ + '.' + name
                            
        if line:
            line = line.strip()
        else: 
            line = None
        list.append((filename, lineno, name, line))
        tb = tb.tb_next
        n = n+1
    return list


class TestCaseReport(object):
    def __init__(self, test):
        self.id = test.id()
        self.address = test.address()
        
        self.start_time = 0.0
        self.end_time = 0.0
        self.total_time = 0.0
        self.number_of_runs = 0.0
        
        self.number_of_suite_runs = 0.0
        
        
        if hasattr(test.test, "_dt_test"):
            self.lineno = test.test._dt_test.lineno
        elif hasattr(test.test, "_testMethodName"):
            method = getattr(test.test, getattr(test.test, "_testMethodName"))
            self.lineno = method.func_code.co_firstlineno
        else:
            self.lineno = test.test.descriptor.compat_co_firstlineno
            
        self.failed = False
        self.errored = False
        self.skipped = False
        self.found = False
    
    def __str__(self):
        return str(self.id)
    
    def start(self):
        self.start_time = time.time()
        self.found = True
    
    def end(self):
        self.end_time = time.time()
        self.number_of_runs += 1.0
        self.total_time += (self.end_time - self.start_time)
        self.failed = False
        self.errored = False
        self.skipped = False
        
    def mean_time(self):
        if self.number_of_runs == 0:
            return 0.0
        return self.total_time / self.number_of_runs

    def add_syntax_error_file_to_traceback(self, error_type, error_value):
        if not issubclass(error_type, SyntaxError):
            return
            
        try:
            msg, (filename, lineno, offset, badline) = error_value.args
        except Exception:
            pass
        
        self.traceback.insert(0, (filename, lineno, "", ""))
        
    def end_with_error(self, error_tuple):
        self.end_time = time.time()
        error_type, error_value, error_traceback = error_tuple        
        self.error_string = traceback.format_exception_only(error_type, error_value)
        self.traceback = list(reversed(extract_tb(error_traceback)))
        
        self.add_syntax_error_file_to_traceback(error_type, error_value)
        
        self.reset_timing()
        
        self.failed = False
        self.errored = True
        self.skipped = False
        
    def end_with_failure(self, error_tuple):
        self.end_time = time.time()
        error_type, error_value, error_traceback = error_tuple        
        self.error_string = traceback.format_exception_only(error_type, error_value)
        self.traceback = list(reversed(traceback.extract_tb(error_traceback)))
        self.add_syntax_error_file_to_traceback(error_type, error_value)
        
        self.reset_timing()
        
        self.failed = True
        self.errored = False
        self.skipped = False
        
    def end_with_skip(self):
        self.end_time = time.time()
        
        self.skipped = True
        self.failed = False
        self.errored = False
        
    def reset_timing(self):
        self.number_of_runs = 0
        self.total_time = 0.0
            
    def to_dict(self):
        result = self.__dict__.copy()
        result['mean_time'] = self.mean_time()
        return result
        
        
class MakeAReportOfATestRun(object):
    score = 2000
    
    def __init__(self, previous_report = None, reports_queue = None):
        self.errors = 0
        self.failures = 0
        self.tests = 0
        self.start_time = 0
        self.end_time = 0
        self.skipped = 0
        self.report_id = -1
        
        if reports_queue is None:
            self._queue_report = self.ignore_report
        else:
            self._queue_report = self.store_report
            self._reports_queue = reports_queue
        
        if previous_report is None:
            self.address_to_report = {}
        else:
            self.address_to_report = previous_report.address_to_report
           
            
        self.name = 'report on a test'
        self.enabled = True

    def __getstate__(self):
        result = {}
        for key, value in self.__dict__.iteritems():
            if not key.startswith('_'):
                result[key] = value
        return result
        
        
    def ignore_report(self, report):
        pass
        
    def store_report(self, report):
        self._reports_queue.put(('unit-report',report.to_dict()))
        
    
    def prepareTest(self, suite):
        pass
        #all_tests = list(suite)
        #number_of_tests = 0
        #self._reports_queue.put(('start-report',{"number_of_tests": number_of_tests}))
        
    def addSuccess(self,test):
        report = self.get_report(test)
        report.end()
        self._queue_report(report)
        
    def addError(self,test,error_tuple):
        report = self.get_report(test)
        error_class, ignore_1, ignore_2 = error_tuple
        if issubclass(error_class, SkipTest):
            self.skipped += 1
            self.tests -= 1
            report.end_with_skip()
        else: 
            report.end_with_error(error_tuple)
            self.errors += 1
        
        self._queue_report(report)
    
    def addFailure(self, test, error_tuple):
        self.failures += 1
        report = self.get_report(test)
        report.end_with_failure(error_tuple)
        
        self._queue_report(report)
           
    def get_report(self, test):
        address = test.address()
        if not address in self.address_to_report:
            self.address_to_report[address] = TestCaseReport(test)
        return self.address_to_report[address] 
        
    def options(self, parser, env):
        pass
        
    def configure(self, parser, env):
        pass
        
        
    def beforeTest(self,test):
        self.tests += 1
        x = self.get_report(test)
        x.start()
        x.number_of_suite_runs += 1
    
    def begin(self):
        self.start_time = time.time()
        for x in self.address_to_report.values():
            x.errored = False
            x.failed = False
            x.skipped = False
            x.found = False
        
    def finalize(self, x):    
        self.end_time = time.time()
        for key, report in list(self.address_to_report.iteritems()):
            if not report.found:
                del self.address_to_report[key]        

    def startTest(self, test):
        if self.is_test_able_to_run(test):
           return
        else:
           raise SkipTest

    def is_test_able_to_run(self, test):
        report = self.get_report(test)
        time_taken = report.mean_time() 
        if time_taken < 0.1:
            return True
        if time_taken < 1.0:
            return (report.number_of_suite_runs % 5) == 0
        if time_taken < 3.0:
            return (report.number_of_suite_runs % 15) == 0
            
        return (report.number_of_suite_runs % 30) == 0
    
    def has_errors(self):
        return self.errors > 0
    
    def has_failures(self):
        return self.failures > 0
        
    def has_skipped_tests(self):
        return self.skipped > 0
        
    def total_number_of_tests(self):
        return self.tests + self.skipped  + self.failures + self.errors
        
    def title_string(self):
        delta_time = self.end_time - self.start_time
        title = '';
        if self.has_failures() or self.has_errors():
            title += "FAIL "
        else:
            title += "OK "

        
        if self.has_errors():
            title += 'E' + str(self.errors) + ' '
        
        if self.has_failures():
            title += 'F' + str(self.failures) + ' '
        
        
        title += '+' + str(self.tests) + ' '
        
        if self.has_skipped_tests():
            title += 'S' + str(self.skipped) + ' '
        
        title += 'T' + str(self.total_number_of_tests()) + ' '
        title += ("%10.3f" % delta_time) + 's';
        title += ' ';
        title += time.strftime("%H:%M:%S", time.gmtime(self.start_time))
        return title;

    def to_information_dict(self):
        result = {}
        result['report_id'] = self.report_id
        result['title'] = self.title_string()
        result['success'] = not self.has_errors() and not self.has_failures()
        return result
    
    def to_dict(self):
        result = self.to_information_dict()
        
        for x in [ 
            'errors', 'failures', 'tests' , 
            'start_time', 'end_time', 'skipped',
            ]:
            result[x] = getattr(self, x)
        
        testcases = list(self.address_to_report.values())
        for x in testcases:
            if x.address[0] is None:
                x.address = list(x.address)
                x.address[0] = ''
                
        testcases.sort(key=lambda x: os.path.basename(x.address[0]))
        result['testcases'] = map(lambda x: x.to_dict(),testcases )
        
        return result  



class SelectOneTestAndStoreOutput(object):
    name = 'select one test'
    enabled = True
    score = 10
    
    def __init__(self, address):
        self.address = address
        self.capture_stdout = None
        self.stdout = None
        
    def options(self, parser, env):
        pass
        
    def configure(self, parser, env):
        pass
    
    def afterTest(self, test):
        if not self.stdout is None:
            sys.stdout = self.stdout
            self.stdout = None
            
    def startTest(self, test):
        if test.address() == self.address:
           self.stdout = sys.stdout
           self.capture_stdout = StringIO()
           sys.stdout = self.capture_stdout 
           return
        else:
           raise SkipTest
        
    @property
    def buffer(self):
        if self.capture_stdout:
            return self.capture_stdout.getvalue()
        else:
            return "none"

class RunTests(object):
    
    def __init__(self):
        self.test_is_running = False
        self.report_queue = queue.Queue()
        
    def _perform_the_testrun(self, directories, results_queue, previous_report = None):
        try:
            null_device = open('/dev/null')
            os.stdin = null_device
            report = MakeAReportOfATestRun(previous_report, results_queue)
            doctest = Doctest()
            doctest.enabled = True
            plugins = [doctest, report , Skip(), Capture()] 
            argv = ['nose']
            argv.extend( directories)
            argv.extend(['--with-doctest', '--doctest-extension=txt'])
           
            result = TestProgram(exit = False, argv=argv, plugins=plugins);
            results_queue.put(('test-report', report,) )
        except :
            results_queue.put(('test-error', 'Exception happened: ' + str(sys.exc_info()[0]) + " - " + str(sys.exc_info()[1]), ))
        finally:
            results_queue.put(None)
            MPI.Finalize()


    def _perform_one_test(self, directories, results_queue, address):
        try:
            print "start test run"
            null_device = open('/dev/null')
            os.stdin = null_device
            select = SelectOneTestAndStoreOutput(address)
            plugins = [select, Skip()]  
            argv = ['nose']
            argv.extend( directories)
            result = TestProgram(
                exit = False, 
                argv=argv, 
                plugins=plugins)
            success = result.success
            if success:
                result = 'Success'
            else:
                result = 'Failure'
            
            if select.buffer:
                result += ' - '
                if len(select.buffer) > 1000:
                    result += select.buffer[:min(1000, len(result) - 1)] 
                    result += ' ' + str(result - 1000) + ' more ...'
                else:
                    result += select.buffer
            
            results_queue.put(('test-output',result))
        except:
            results_queue.put(('test-error', 'Exception happened: ' + str(sys.exc_info()[0]) + " - " + str(sys.exc_info()[1]), ))
        finally:
            results_queue.put(None)
            MPI.Finalize()
            print "calling finalize done"

    def run_test_with_address(self, address):
        typestring, result = self.run_in_another_process(self._perform_one_test, address)
        return result
         
    def run_tests(self, previous_report):
        typestring, result = self.run_in_another_process(self._perform_the_testrun, previous_report)
        if typestring == 'test-error':
            print result
            raise Exception(result)
        else:
            return result  
            
    def can_start_a_test(self):
        return not self.test_is_running
        
    def run_in_another_process(self, method, argument):
        if self.test_is_running:
            raise Exception("Test is already running")
        
        self.clear_reports_queue()
        self.test_is_running = True
        try:
            result_queue = Queue()
            cwd = os.getcwd()
            paths = [os.path.join(cwd, x) for x in RunAllTestsWhenAChangeHappens.DIRECTORIES]  
            process = Process(
                target=method, 
                args=(
                    paths, 
                    result_queue, 
                    argument
                ))
            process.start()
            
            last_message = None
            while True:
                message = result_queue.get()
                if message is None:
                    break;
                if message[0] == 'unit-report':
                    self.report_queue.put(message[1])
                if message[0] == 'start-report':
                    print message[1]
                last_message = message
            result = last_message 
        finally:
            self.test_is_running = False
            self.clear_reports_queue()
            
        process.join(2)
        del result_queue
        return result
     
    def clear_reports_queue(self):
        
        must_clear = not self.report_queue.empty()
        while must_clear:
            try:
                self.report_queue.get_nowait()
                must_clear = not self.report_queue.empty()
            except queue.Empty:
                must_clear = False
                
    def get_reports(self):
        result = []
        must_clear = not self.report_queue.empty()
        while must_clear:
            try:
                result.append(self.report_queue.get_nowait())
                must_clear = not self.report_queue.empty()
            except queue.Empty:
                must_clear = False
        return result
        
            
RunTests.instance = RunTests()

class RunAllTestsWhenAChangeHappens(object):
    DIRECTORIES = ['doc', 'src', 'test']
    
    def __init__(self, server):
        self.must_run = False
        self.server = server
        
    def start(self):
        self.must_run = True;
        self.thread = threading.Thread(target=self.run)
        self.thread.daemon = True;
        self.thread.start()
    
    def stop(self):
        self.must_run = False;
        
    def run(self):
        cwd = os.getcwd()
        paths = [os.path.join(cwd, x) for x in self.DIRECTORIES] 

        monitor_directories = monitor.MonitorDirectories(paths)
        monitor_directories.check()
        monitor_directories.changed = True
        while self.must_run:
            if monitor_directories.changed:
                if not RunTests.instance.can_start_a_test():
                    monitor_directories.check()
                    time.sleep(0.5)
                    continue
                
                if not self.server.last_report is None:
                    for element in monitor_directories.updated_elements:
                        if not element.is_file():
                            continue
                        for x in self.server.last_report.address_to_report.values():
                            path, module, testcase =  x.address
                            if path == element.path:
                                x.reset_timing()
                                print "will rerun: ", module, testcase
                        
                
                report = RunTests.instance.run_tests(self.server.last_report)
                
                self.server.set_last_report(report)
             
                monitor_directories.check()
            else:
                time.sleep(0.5)
                monitor_directories.check()
                
                
    
class HandleRequest(webserver.HandleRequest):
    
    def do_start(self):
        self.server.restart_testrunner()
        string = 'null'
        content_type = 'text/javascript'
        return string, content_type
    
    
    def do_pause(self):
        self.server.stop_testrunner()
        return 'null', 'text/javascript' 
    
    def do_get_last_report(self):
        string = json.dumps(self.server.get_last_report_as_dict())
        content_type = 'text/javascript'
        return string, content_type

    def do_get_last_report_information(self):
        string = json.dumps(self.server.get_last_report_information())
        content_type = 'text/javascript'
        return string, content_type
    
    def do_run_test(self):
        parameters = urlparse.parse_qs(self.parsed_path.query)
        a0 = parameters['a0'][0]
        a1 = parameters['a1'][0]
        a2 = parameters['a2'][0]
        address = (a0, a1, a2)
        result = RunTests.instance.run_test_with_address(address)
        string = json.dumps(result)
        content_type = 'text/javascript'
        self.server.continue_testrunner()
        return string, content_type
        
    def index_file(self):
        base = os.path.split(__file__)[0]
        filename = os.path.join(base, "realtime_test.html")
        with open(filename, "r") as file:
            contents = file.read()
            return contents, 'text/html'
            

class ContinuosTestWebServer(webserver.WebServer):
    
    def __init__(self, port):
        webserver.WebServer.__init__(self,  port, HandleRequest)
        self.last_report = None
        self.run_all_tests = RunAllTestsWhenAChangeHappens(self)
        self.run_all_tests.start()
        self.report_id = 0
        
        
    def stop(self):
        self.run_all_tests.stop()
        self.shutdown()
        
    def restart_testrunner(self):
        self.run_all_tests.stop()
        self.last_report = None
        self.run_all_tests = RunAllTestsWhenAChangeHappens(self)
        self.run_all_tests.start()
        
    def stop_testrunner(self):
        self.run_all_tests.stop()        
    
    def continue_testrunner(self):
        if not self.run_all_tests.must_run:
            self.restart_testrunner()
        
    def get_last_report_as_dict(self):
        if self.last_report is None:
            return None
        else:
            return self.last_report.to_dict()
            
    def get_last_report_information(self):
        if self.last_report is None:
            result = {'report_id':-1, 'title':'running'} 
        else:
            result =  self.last_report.to_information_dict()
        result['reports'] = self.get_live_reports();
        return result
    
    def get_live_reports(self):
        return RunTests.instance.get_reports()
        
    def set_last_report(self, report):
        self.last_report = report
        self.report_id += 1
        self.last_report.report_id = self.report_id
        #self.events_queue.put('done')
        

def start_browser(serverport):
    time.sleep(2.0)
    webbrowser.open("http://localhost:{0}/".format(serverport))
            
if __name__ == '__main__':
    parser = OptionParser() 
    
    
    parser.add_option("-p", "--port", 
      dest="serverport",
      help="start serving on PORT", 
      metavar="PORT", 
      default=9070,
      type="int")
      
    parser.add_option("-e", "--editor", 
      dest="editor",
      help="preferred EDITOR for editing the files", 
      metavar="EDITOR", 
      default="geany",
      type="string")
      
    (options, args) = parser.parse_args()
    
    print "starting server on port: ", options.serverport
    print "will use editor: ", options.editor
    webserver.EDITOR = options.editor
    
    thread = threading.Thread(target = start_browser, args = (options.serverport,))
    thread.start()
    
    server = ContinuosTestWebServer(options.serverport)
    server.start()
    
