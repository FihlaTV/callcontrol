
"""Implementation of a call control server for OpenSIPS."""

import os
import grp
import re
import cPickle
import time

from application.configuration import ConfigSection, ConfigSetting
from application.process import process
from application import log

from twisted.internet.protocol import Factory
from twisted.protocols.basic import LineOnlyReceiver
from twisted.internet import reactor, defer
from twisted.python import failure

from callcontrol.scheduler import RecurrentCall, KeepRunning
from callcontrol.raddb import RadiusDatabase, RadiusDatabaseError
from callcontrol.sip import Call
from callcontrol.rating import RatingEngineConnections
from callcontrol import configuration_filename, backup_calls_file


class TimeLimit(int):
    """A positive time limit (in seconds) or None"""
    def __new__(typ, value):
        if value.lower() == 'none':
            return None
        try:
            limit = int(value)
        except:
            raise ValueError("invalid time limit value: %r" % value)
        if limit < 0:
            raise ValueError("invalid time limit value: %r. should be positive." % value)
        return limit

class TimeoutDetection(str):
    _values = ('dialog', 'radius')
    def __init__(self, value):
        value = value.lower()
        if value not in self._values:
            raise ValueError("invalid timeout detection value: %r" % value)
        if value == 'radius':
            self.use_radius = True
        else:
            self.use_radius = False

    def __new__(cls, value):
        return str.__new__(cls, value.lower())

class CallControlConfig(ConfigSection):
    __cfgfile__ = configuration_filename
    __section__ = 'CallControl'
    socket            = "%s/socket" % process.runtime_directory
    group             = 'opensips'
    limit             = ConfigSetting(type=TimeLimit, value=None)
    timeout           = 24*60*60 ## timeout calls that are stale for more than 24 hours.
    setupTime         = 90       ## timeout calls that take more than 1'30" to setup.
    checkInterval     = 60       ## check for staled calls and calls that did timeout at every minute.
    timeout_detection = TimeoutDetection('dialog') ## whether or not to use the radius database to find out terminated calls



## Classes

class CommandError(Exception):        pass
class InvalidRequestError(Exception): pass


class CallsMonitor(object):
    """Check for staled calls and calls that did timeout and were closed by external means"""
    def __init__(self, period, application):
        self.application = application
        self.reccall = RecurrentCall(period, self.run)

    def run(self):
        if CallControlConfig.timeout_detection.use_radius:
            ## Find out terminated calls
            deferred1 = self.application.db.getTerminatedCalls(self.application.calls)
            deferred1.addCallbacks(callback=self._clean_calls, errback=self._err_handle, callbackArgs=[self._handle_terminated])
            deferred2 = self.application.db.getTimedoutCalls(self.application.calls)
            deferred2.addCallbacks(callback=self._clean_calls, errback=self._err_handle, callbackArgs=[self._handle_timedout])
            defer.DeferredList([deferred1, deferred2]).addCallback(self._finish_checks)
        else:
            self._finish_checks(None)
        return KeepRunning

    def shutdown(self):
        self.reccall.cancel()

    def _clean_calls(self, calls, clean_function):
        for callid, callinfo in calls.items():
            call = self.application.calls.get(callid)
            if call:
                self.application.clean_call(callid)
                clean_function(call, callinfo)

    def _err_handle(self, fail):
        log.error("Couldn't query database for terminated/timedout calls: %s" % fail.value)

    def _handle_terminated(self, call, callinfo):
        call.end(calltime=callinfo['duration'], reason='calls monitor as terminated')

    def _handle_timedout(self, call, callinfo):
        call.end(reason='calls monitor as timedout', sendbye=True)

    def _finish_checks(self, value):
        ## Also do the rest of the checking
        now = time.time()
        staled = []
        nosetup = []
        for callid, call in self.application.calls.items():
            if not call.complete and (now - call.created >= CallControlConfig.setupTime):
                self.application.clean_call(callid)
                nosetup.append(call)
            elif call.inprogress and call.timer is not None:
                continue ## this call will be expired by its own timer
            elif now - call.created >= CallControlConfig.timeout:
                self.application.clean_call(callid)
                staled.append(call)
        ## Terminate staled
        for call in staled:
            call.end(reason='calls monitor as staled', sendbye=True)
        ## Terminate calls that didn't setup in setupTime
        for call in nosetup:
            call.end(reason="calls monitor as it didn't setup in %d seconds" % CallControlConfig.setupTime)


class CallControlProtocol(LineOnlyReceiver):
    def lineReceived(self, line):
        if line.strip() == "":
            if self.line_buf:
                self._process()
                self.line_buf = []
        else:
            self.line_buf.append(line.strip())

    def _process(self):
        try:
            req = Request(self.line_buf[0], self.line_buf[1:])
        except InvalidRequestError, e:
            self._send_error_reply(failure.Failure(e))
        else:
#            log.debug("Got request: %s" % str(req)) #DEBUG
            def _unknown_handler(req):
                req.deferred.errback(failure.Failure(CommandError(req)))
            try:
                getattr(self, '_CC_%s' % req.cmd, _unknown_handler)(req)
            except Exception, e:
                self._send_error_reply(failure.Failure(e))
            else:
                req.deferred.addCallbacks(callback=self._send_reply, errback=self._send_error_reply)

    def connectionMade(self):
        self.line_buf = []

    def _send_reply(self, msg):
#        log.debug('Sent reply: %s' % msg) #DEBUG
        self.sendLine(msg)

    def _send_error_reply(self, fail):
        log.error(fail.value)
#        log.debug("Sent 'Error' reply") #DEBUG
        self.sendLine('Error')

    def _CC_init(self, req):
        try:
            call = self.factory.application.calls[req.callid]
        except KeyError:
            call = Call(req, self.factory.application)
            if call.billingParty is None:
                req.deferred.callback('Error')
                return
            self.factory.application.calls[req.callid] = call
#            log.debug("Call id %s added to list of controlled calls" % (call.callid)) #DEBUG
        else:
            if call.token != req.call_token:
                log.error("Call id %s is duplicated" % call.callid)
                req.deferred.callback('Duplicated callid')
                return
            # The call was previously setup which means it could be in the the users table
            try:
                user_calls = self.factory.application.users[call.billingParty]
                user_calls.remove(call.callid)
                if len(user_calls) == 0:
                    del self.factory.application.users[call.billingParty]
                    self.factory.application.engines.remove_user(call.billingParty)
            except (ValueError, KeyError):
                pass
        deferred = call.setup(req)
        deferred.addCallbacks(callback=self._CC_finish_init, errback=self._CC_init_failed, callbackArgs=[req], errbackArgs=[req])

    def _CC_finish_init(self, value, req):
        try:
            call = self.factory.application.calls[req.callid]
        except KeyError:
            log.error("Call id %s disappeared before we could finish initializing it" % req.callid)
            req.deferred.callback('Error')
        else:
            if req.call_limit is not None and len(self.factory.application.users.get(call.billingParty, ())) >= req.call_limit:
                self.factory.application.clean_call(req.callid)
                call.end()
                req.deferred.callback('Call limit reached')
            elif call.locked: ## prepaid account already locked by another call
                log.info("Call id %s of %s to %s forbidden because the account is locked" % (req.callid, call.user, call.ruri))
                self.factory.application.clean_call(req.callid)
                call.end()
                req.deferred.callback('Locked')
            elif call.timelimit == 0: ## prepaid account with no credit
                log.info("Call id %s of %s to %s forbidden because credit is too low" % (req.callid, call.user, call.ruri))
                self.factory.application.clean_call(req.callid)
                call.end()
                req.deferred.callback('No credit')
            elif req.call_limit is not None or call.timelimit is not None: ## call limited by credit value, a global time limit or number of calls
                log.info("User %s can make %s concurrent calls" % (call.billingParty, req.call_limit or "unlimited"))
                self.factory.application.users.setdefault(call.billingParty, []).append(call.callid)
                req.deferred.callback('Limited')
            else: ## no limit for call
                log.info("Call id %s of %s to %s is postpaid not limited" % (req.callid, call.user, call.ruri))
                self.factory.application.clean_call(req.callid)
                call.end()
                req.deferred.callback('No limit')

    def _CC_init_failed(self, fail, req):
        self._send_error_reply(fail)
        self.factory.application.clean_call(req.callid)

    def _CC_start(self, req):
        try:
            call = self.factory.application.calls[req.callid]
        except KeyError:
            req.deferred.callback('Not found')
        else:
            call.start(req)
            req.deferred.callback('Ok')

    def _CC_stop(self, req):
        try:
            call = self.factory.application.calls[req.callid]
        except KeyError:
            req.deferred.callback('Not found')
        else:
            self.factory.application.clean_call(req.callid)
            call.end(reason='user')
            req.deferred.callback('Ok')

    def _CC_debug(self, req):
        debuglines = []
        if req.show == 'sessions':
            for callid, call in self.factory.application.calls.items():
                if not req.user or call.user.startswith(req.user):
                    debuglines.append('Call id %s of %s to %s: %s' % (callid, call.user, call.ruri, call.status))
        elif req.show == 'session':
            try:
                call = self.factory.application.calls[req.callid]
            except KeyError:
                debuglines.append('Call id %s does not exist' % req.callid)
            else:
                for key, value in call.items():
                    debuglines.append('%s: %s' % (key, value))
        req.deferred.callback('\r\n'.join(debuglines)+'\r\n')

    def _CC_terminate(self, req):
        try:
            call = self.factory.application.calls[req.callid]
        except KeyError:
            req.deferred.callback('Call id %s does not exist\r\n' % req.callid)
        else:
            self.factory.application.clean_call(req.callid)
            call.end(reason='admin', sendbye=True)
            req.deferred.callback('Ok\r\n')


class CallControlFactory(Factory):
    protocol = CallControlProtocol

    def __init__(self, application):
        self.application = application


class CallControlServer(object):
    def __init__(self, path=None, group=None):
        self.path = path or CallControlConfig.socket
        self.group = group or CallControlConfig.group
        try:
            os.unlink(self.path)
        except OSError:
            pass

        self.listening = None
        self.engines = None
        self.monitor = None
        if CallControlConfig.timeout_detection.use_radius:
            self.db = RadiusDatabase()
        else:
            self.db = None

        self.calls = {}
        self.users = {}
        self._restore_calls()

    def clean_call(self, callid):
        try:
            call = self.calls[callid]
        except KeyError:
            return
        else:
            del self.calls[callid]
            user_calls = self.users.get(call.billingParty, [])
            try:
                user_calls.remove(callid)
            except ValueError:
                pass

            if not user_calls:
                self.users.pop(call.billingParty, None)
                self.engines.remove_user(call.billingParty)
   #      log.debug("Call id %s removed from the list of controlled calls" % callid) #DEBUG

    def run(self):
        ## Add a callback for the startup/shutdown stuff
        reactor.addSystemEventTrigger('before', 'startup', self.on_startup)
        reactor.addSystemEventTrigger('before', 'shutdown', self.on_shutdown)
        ## And start reactor
        reactor.run()
        ## And do the shutdown

    def stop(self):
        reactor.stop()

    def on_startup(self):
        ## First set up listening on the unix socket
        try:
            gid = grp.getgrnam(self.group)[2]
            mode = 0660
        except (KeyError, IndexError):
            gid = -1
            mode = 0666
        self.listening = reactor.listenUNIX(address=self.path, factory=CallControlFactory(self))
        ## Make it writable only to the SIP proxy group members
        try:
            os.chown(self.path, -1, gid)
            os.chmod(self.path, mode)
        except OSError:
            log.warn("Couldn't set access rights for %s" % self.path)
            log.warn("OpenSIPS may not be able to communicate with us!")

        ## Then setup the CallsMonitor
        self.monitor = CallsMonitor(CallControlConfig.checkInterval, self)
        ## Open the connection to the rating engines
        self.engines = RatingEngineConnections()

    def on_shutdown(self):
        should_close = []
        if self.listening is not None:
            self.listening.stopListening()
        if self.engines is not None:
            should_close.append(self.engines.shutdown())
        if self.monitor is not None:
            self.monitor.shutdown()
        if self.db is not None:
            should_close.append(self.db.close())
        d = defer.DeferredList(should_close)
        d.addBoth(self._save_calls)
        return d

    def _save_calls(self, result):
        if self.calls:
            log.info('Saving calls')
            calls_file = '%s/%s' % (process.runtime_directory, backup_calls_file)
            try:
                f = open(calls_file, 'w')
            except:
                pass
            else:
                for call in self.calls.values():
                    call.application = None
                    ## we will mark timers with 'running' or 'idle', depending on their current state,
                    ## to be able to correctly restore them later (Timer objects cannot be pickled)
                    if call.timer is not None:
                        if call.inprogress:
                            call.timer.cancel()
                            call.timer = 'running' ## temporary mark that this timer was running
                        else:
                            call.timer = 'idle'    ## temporary mark that this timer was not running
                failed_dump = False
                try:
                    try:
                        cPickle.dump(self.calls, f)
                    except Exception, why:
                        log.warn("Failed to dump call list: %s" % why)
                        failed_dump = True
                finally:
                    f.close()
                if failed_dump:
                    try:    os.unlink(calls_file)
                    except: pass
                else:
                    log.info("Saved calls: %s" % str(self.calls.keys()))
            self.calls = {}

    def _restore_calls(self):
        calls_file = '%s/%s' % (process.runtime_directory, backup_calls_file)
        try:
            f = open(calls_file, 'r')
        except:
            pass
        else:
            try:
                self.calls = cPickle.load(f)
            except Exception, why:
                log.warn("Failed to load calls saved in the previous session: %s" % why)
            f.close()
            try:    os.unlink(calls_file)
            except: pass
            if self.calls:
                log.info("Restoring calls saved previously: %s" % str(self.calls.keys()))
                ## the calls in the 2 sets below are never overlapping because closed and terminated
                ## calls have different database fingerprints. so the dictionary update below is safe
                try:
                    db = self.db if self.db is not None else RadiusDatabase()
                    try:
                        terminated = db.query(RadiusDatabase.RadiusTask(None, 'terminated', calls=self.calls))   ## calls terminated by caller/called
                        didtimeout = db.query(RadiusDatabase.RadiusTask(None, 'timedout', calls=self.calls))     ## calls closed by mediaproxy after a media timeout
                    finally:
                        if self.db is None:
                            db.close()
                except RadiusDatabaseError, e:
                    log.error("Could not query database: %s" % e)
                else:
                    for callid, call in self.calls.items():
                        callinfo = terminated.get(callid) or didtimeout.get(callid)
                        if callinfo:
                            ## call already terminated or did timeout in mediaproxy
                            del self.calls[callid]
                            callinfo['call'] = call
                            call.timer = None
                            continue
                    ## close all calls that were already terminated or did timeout
                    count = 0
                    for callinfo in terminated.values():
                        call = callinfo.get('call')
                        if call is not None:
                            call.end(calltime=callinfo['duration'])
                            count += 1
                    for callinfo in didtimeout.values():
                        call = callinfo.get('call')
                        if call is not None:
                            call.end(sendbye=True)
                            count += 1
                    if count > 0:
                        log.info("Removed %d already terminated call%s" % (count, 's'*(count!=1)))
                for callid, call in self.calls.items():
                    call.application = self
                    if call.timer == 'running':
                        now = time.time()
                        remain = call.starttime + call.timelimit - now
                        if remain < 0:
                            call.timelimit = int(round(now - call.starttime))
                            remain = 0
                        call._setup_timer(remain)
                        call.timer.start()
                    elif call.timer == 'idle':
                        call._setup_timer()

                    # also restore users table
                    self.users.setdefault(call.billingParty, []).append(callid)

class Request(object):
    """A request parsed into a structure based on request type"""
    __methods = {'init':      ('callid', 'diverter', 'ruri', 'sourceip', 'from'),
                 'start':     ('callid', 'dialogid'),
                 'stop':      ('callid',),
                 'debug':     ('show',),
                 'terminate': ('callid',)}
    def __init__(self, cmd, params):
        if cmd not in self.__methods.keys():
            raise InvalidRequestError("Unknown request: %s" % cmd)
        try:
            parameters = dict([re.split(r':\s+', l, 1) for l in params])
        except ValueError:
            raise InvalidRequestError("Badly formatted request")
        for p in self.__methods[cmd]:
            try:
                parameters[p]
            except KeyError:
                raise InvalidRequestError("Missing %s from request" % p)
        self.cmd = cmd
        self.deferred = defer.Deferred()
        self.__dict__.update(parameters)
        try:
            getattr(self, '_RE_%s' % self.cmd)()
        except AttributeError:
            pass

    def _RE_init(self):
        self.from_ = self.__dict__['from']
        if self.cmd=='init' and self.diverter.lower()=='none':
            self.diverter = None
        try:
            self.prepaid
        except AttributeError:
            self.prepaid = None
        else:
            if self.prepaid.lower() == 'true':
                self.prepaid = True
            elif self.prepaid.lower() == 'false':
                self.prepaid = False
            else:
                self.prepaid = None
        try:
            self.call_limit = int(self.call_limit)
        except (AttributeError, ValueError):
            self.call_limit = None
        else:
            if self.call_limit <= 0:
                self.call_limit = None
        try:
            self.call_token
        except AttributeError:
            self.call_token = None
        else:
            if not self.call_token or self.call_token.lower() == 'none':
                self.call_token = None
        try:
            self.sip_application
        except AttributeError:
            self.sip_application = ''

    def _RE_debug(self):
        if self.show == 'session':
            try:
                if not self.callid:
                    raise InvalidRequestError("Missing callid from request")
            except AttributeError:
                raise InvalidRequestError("Missing callid from request")
        elif self.show == 'sessions':
            try:
                self.user
            except AttributeError:
                self.user = None
        else:
            raise InvalidRequestError("Illegal value for 'show' attribute in request")

    def __str__(self):
        if self.cmd == 'init':
            return "%(cmd)s: callid=%(callid)s from=%(from_)s ruri=%(ruri)s diverter=%(diverter)s sourceip=%(sourceip)s prepaid=%(prepaid)s call_limit=%(call_limit)s" % self.__dict__
        elif self.cmd == 'start':
            return "%(cmd)s: callid=%(callid)s dialogid=%(dialogid)s" % self.__dict__
        elif self.cmd == 'stop':
            return "%(cmd)s: callid=%(callid)s" % self.__dict__
        elif self.cmd == 'debug':
            return "%(cmd)s: show=%(show)s" % self.__dict__
        elif self.cmd == 'terminate':
            return "%(cmd)s: callid=%(callid)s" % self.__dict__
        else:
            return object.__str__(self)

