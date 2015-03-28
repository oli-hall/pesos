from collections import defaultdict
from getpass import getuser
import functools
import logging
import threading
import time
import socket
import sys

from .detector import MasterDetector
from .util import camel_call, timed, unique_suffix
from .vendor.mesos import mesos_pb2
from .vendor.mesos.internal import messages_pb2 as internal

from compactor.context import Context
from compactor.pid import PID
from compactor.process import Process, ProtobufProcess
from mesos.interface import SchedulerDriver

log = logging.getLogger(__name__)


class SchedulerProcess(ProtobufProcess):
  MASTER_DETECTION_RETRY_SECONDS = 10
  MASTER_INITIAL_BACKOFF_SECONDS = 2
  MASTER_MAX_BACKOFF_SECONDS = 60

  def __init__(self, driver, scheduler, framework, credential=None, detector=None, clock=time):
    self.driver = driver
    self.scheduler = scheduler
    self.framework = framework
    self.master = None

    # events
    self.connected = threading.Event()
    self.aborted = threading.Event()
    self.failover = threading.Event()

    if framework.HasField('id'):
      self.failover.set()

    # credentials
    self.credential = credential
    self.authenticating = threading.Event()
    self.authenticated = threading.Event()

    # master detection
    self.detector = detector

    # saved state
    self.saved_offers = defaultdict(dict)
    self.saved_slaves = {}

    # clock
    self.clock = clock

    super(SchedulerProcess, self).__init__(unique_suffix('scheduler'))

  def initialize(self):
    super(SchedulerProcess, self).initialize()
    self.context.dispatch(self.pid, 'detect')

  def detect(self):
    self.detector.detect(previous=self.master).add_done_callback(self.detected)

  def ignore_if_aborted(method):
    @functools.wraps(method)
    def _wrapper(self, from_pid, *args, **kwargs):
      if self.aborted.is_set():
        log.info('Ignoring message from %s because the scheduler driver is aborted.' % from_pid)
        return
      return method(self, from_pid, *args, **kwargs)
    return _wrapper

  def ignore_if_disconnected(method):
    @functools.wraps(method)
    def _wrapper(self, *args, **kwargs):
      if not self.connected.is_set():
        log.info('Ignoring message from because the scheduler driver is disconnected.')
        return
      return method(self, *args, **kwargs)
    return _wrapper

  def valid_origin(self, from_pid):
    if self.master != from_pid:
      log.warning('Ignoring message from non-leading master %s' % from_pid)
      return False
    return True

  @ignore_if_aborted
  def detected(self, master_future):
    try:
      master_pid = master_future.result()
    except Exception as e:
      log.warning('Experienced an error detecting master: %s, retrying...' % e)
      self.context.delay(self.MASTER_DETECTION_RETRY_SECONDS, self.pid, 'detect')
      return

    if self.connected.is_set():
      self.connected.clear()
      with timed(log.debug, 'scheduler::disconnected'):
        camel_call(self.scheduler, 'disconnected', self.driver)

    # TODO(wickman) Implement authentication.
    if master_pid:
      log.info('New master detected: %s' % master_pid)
      self.master = master_pid
      self.link(self.master)
    else:
      log.info('Master disconnected.')
      self.master = None

    self._do_registration()

    log.info('Setting transition watch from previous master: %s' % master_pid)
    self.context.dispatch(self.pid, 'detect')

  def _do_registration(self, backoff=MASTER_INITIAL_BACKOFF_SECONDS):
    if self.connected.is_set() or self.master is None:
      log.info('Skipping registration because we are either connected or '
               'there is no appointed master.')
      return

    # We have never registered before
    if not self.framework.id.value:
      message = internal.RegisterFrameworkMessage(framework=self.framework)
      log.info('Registering framework: %s' % message)
    else:
      message = internal.ReregisterFrameworkMessage(
          framework=self.framework, failover=self.failover.is_set())
      log.info('Reregistering framework: %s' % message)

    self.send(self.master, message)

    # run a backoff loop
    self.context.delay(
        backoff,
        self.pid,
        '_do_registration',
        max(backoff * 2, self.MASTER_MAX_BACKOFF_SECONDS))

  @ProtobufProcess.install(internal.FrameworkRegisteredMessage)
  @ignore_if_aborted
  def registered(self, from_pid, message):
    if self.connected.is_set():
      log.info('Ignoring registered message as we are already connected.')
      return
    if not self.valid_origin(from_pid):
      return
    self.framework.id.value = message.framework_id.value
    self.connected.set()
    self.failover.clear()

    with timed(log.debug, 'scheduler::registered'):
      camel_call(self.scheduler, 'registered',
          self.driver, message.framework_id, message.master_info)

  @ProtobufProcess.install(internal.FrameworkReregisteredMessage)
  @ignore_if_aborted
  def reregistered(self, from_pid, message):
    if self.connected.is_set():
      log.info('Ignoring registered message as we are already connected.')
      return
    if not self.valid_origin(from_pid):
      return
    assert self.framework.id == message.framework_id
    self.connected.set()
    self.failover.clear()

    with timed(log.debug, 'scheduler::reregistered'):
      camel_call(self.scheduler, 'reregistered', self.driver, message.master_info)

  @ProtobufProcess.install(internal.ResourceOffersMessage)
  @ignore_if_disconnected
  @ignore_if_aborted
  def resource_offers(self, from_pid, message):
    assert self.master is not None
    if not self.valid_origin(from_pid):
      return
    for offer, pid in zip(message.offers, message.pids):
      offer_id = offer.id.value
      slave_id = offer.slave_id.value
      self.saved_offers[offer_id][slave_id] = PID.from_string(pid)
    with timed(log.debug, 'scheduler::resource_offers'):
      camel_call(self.scheduler, 'resource_offers', self.driver, message.offers)

  @ProtobufProcess.install(internal.RescindResourceOfferMessage)
  @ignore_if_disconnected
  @ignore_if_aborted
  def rescind_offer(self, from_pid, message):
    assert self.master is not None
    if not self.valid_origin(from_pid):
      return
    log.info('Rescinding offer %s' % message.offer_id.value)
    if not self.saved_offers.pop(message.offer_id.value, None):
      log.warning('Offer %s not found.' % message.offer_id.value)
    with timed(log.debug, 'scheduler::offer_rescinded'):
      camel_call(self.scheduler, 'offer_rescinded', self.driver, message.offer_id)

  @ProtobufProcess.install(internal.StatusUpdateMessage)
  @ignore_if_disconnected
  @ignore_if_aborted
  def status_update(self, from_pid, message):
    if not self.valid_origin(from_pid):
      return
    if self.master:
      self.status_update_acknowledgement(message.update, self.master)
    with timed(log.debug, 'scheduler::status_update'):
      camel_call(self.scheduler, 'status_update', self.driver, message.update.status)

  @ignore_if_aborted
  def status_update_acknowledgement(self, update, pid):
    message = internal.StatusUpdateAcknowledgementMessage(
        framework_id=self.framework.id,
        slave_id=update.slave_id,
        task_id=update.status.task_id,
        uuid=update.uuid,
    )
    self.send(pid, message)

  @ProtobufProcess.install(internal.LostSlaveMessage)
  @ignore_if_disconnected
  @ignore_if_aborted
  def lost_slave(self, from_pid, message):
    assert self.master is not None
    if not self.valid_origin(from_pid):
      return
    self.saved_slaves.pop(message.slave_id.value)
    with timed(log.debug, 'scheduler::slave_lost'):
      camel_call(self.scheduler, 'slave_lost', self.driver, message.slave_id)

  @ProtobufProcess.install(internal.ExecutorToFrameworkMessage)
  @ignore_if_aborted
  def framework_message(self, from_pid, message):
    with timed(log.debug, 'scheduler::framework_message'):
      camel_call(self.scheduler, 'framework_message',
          self.driver,
          message.executor_id,
          message.slave_id,
          message.data
      )

  @ProtobufProcess.install(internal.FrameworkErrorMessage)
  @ignore_if_aborted
  def error(self, from_pid, message):
    with timed(log.debug, 'scheduler::error'):
      camel_call(self.scheduler, 'error', self.driver, message.message)

  @ignore_if_aborted
  def stop(self, failover=False):
    if not failover:
      self.connected.clear()
      self.failover.set()
      self.send(self.master, internal.UnregisterFrameworkMessage(
          framework_id=self.framework.id
      ))

  @ignore_if_aborted
  def abort(self):
    self.connected.clear()
    self.aborted.set()

  @ignore_if_disconnected
  def kill_task(self, task_id):
    assert self.master is not None
    message = internal.KillTaskMessage(framework_id=self.framework.id, task_id=task_id)
    self.send(self.master, message)

  @ignore_if_disconnected
  def request_resources(self, requests):
    assert self.master is not None
    message = internal.ResourceRequestMessage(
        framework_id=self.framework.id,
        requests=requests,
    )
    self.send(self.master, message)

  def _local_lost(self, task, reason):
    update = mesos_pb2.StatusUpdate(
        framework_id=self.framework.id,
        status=mesos.TaskStatus(
            task_id=task.id,
            state=mesos.TASK_LOST,
            message=reason,
            timestamp=now,
            uuid=uuid.uuid4().get_bytes(),
        )
    )
    self.send(self.pid, update)

  def launch_tasks(self, offer_ids, tasks, filters=None):

    if not self.connected.is_set():
      for task in tasks:
        self._local_lost(task, 'Master Disconnected')
      return

    filters = filters or mesos_pb2.Filters()

    # Perform some sanity checking on the tasks before launching them
    for task in tasks:
      if task.HasField('executor') == task.HasField('command'):
        self._local_lost(task, 'Malformed: A task must have either an executor or command')
        continue
      if task.HasField('executor') and task.executor.HasField('framework_id'):
        if task.executor.framework_id.value != self.framework.id.value:
          self._local_lost(task, 'Malformed: Executor has an invalid framework ID')
          continue
      if task.HasField('executor') and not task.executor.HasField('framework_id'):
        # XXX we should not be mutating input
        task.executor.framework_id.value = self.framework.id.value

    message = internal.LaunchTasksMessage(
        framework_id=self.framework.id,
        tasks=tasks,
        filters=filters,
    )

    for offer_id in offer_ids:
      field = message.offer_ids.add()
      field.value = offer_id.value

      for task in tasks:
        if offer_id.value in self.saved_offers:
          if task.slave_id.value in self.saved_offers[offer_id.value]:
            self.saved_slaves[task.slave_id.value] = \
                self.saved_offers[offer_id.value][task.slave_id.value]
          else:
            log.warning("Attempting to launch task %s with the wrong slave %s",
                        task.task_id.value, task.slave_id.value)
        else:
          log.warning("Attempting to launch task %s with an unknown offer %s",
                      task.task_id.value, offer_id.value)
      self.saved_offers.pop(offer_id.value)

    self.send(self.master, message)

  @ignore_if_disconnected
  def revive_offers(self):
    assert self.master is not None
    message = internal.ReviveOffersMessage(framework_id=self.framework.id)
    self.send(self.master, message)

  @ignore_if_disconnected
  def send_framework_message(self, executor_id, slave_id, data):
    assert executor_id is not None
    assert slave_id is not None
    assert data is not None

    try:
      pid = self.saved_slaves[slave_id]
    except KeyError:
      pid = self.master
      log.warning("Cannot send directly to slave %s, sending through master",
                  slave_id)

    message = internal.FrameworkToExecutorMessage(
        framework_id=self.framework.id,
        executor_id=executor_id,
        slave_id=slave_id,
        data=data,
    )
    self.send(pid, message)

  @ignore_if_disconnected
  def reconcile_tasks(self, statuses):
    assert self.master is not None
    message = internal.ReconcileTasksMessage(framework_id=self.framework.id, statuses=statuses)
    self.send(self.master, message)

  del ignore_if_aborted
  del ignore_if_disconnected


class PesosSchedulerDriver(SchedulerDriver):
  def __init__(self, scheduler, framework, master_uri, credential=None, context=None):
    self.context = context or Context.singleton()
    self.scheduler = scheduler
    self.scheduler_process = None
    self.master_uri = master_uri
    self.framework = framework
    self.lock = threading.Condition()
    self.status = mesos_pb2.DRIVER_NOT_STARTED
    self.detector = None
    self.credential = credential

    # Fill out any missing FrameworkInfo values
    if not self.framework.hostname:
      self.framework.hostname = socket.getfqdn()
    if not self.framework.user:
      self.framework.user = getuser()

  def locked(method):
    @functools.wraps(method)
    def _wrapper(self, *args, **kw):
      with self.lock:
        return method(self, *args, **kw)
    return _wrapper

  @locked
  def start(self):
    if self.status is not mesos_pb2.DRIVER_NOT_STARTED:
      return self.status

    try:
      self.detector = MasterDetector.from_uri(self.master_uri)
    except MasterDetector.Error as e:
      self.status = mesos_pb2.DRIVER_ABORTED
      log.error('Failed to construct master detector: %s' % e)
      return

    assert self.scheduler_process is None
    self.scheduler_process = SchedulerProcess(
        self,
        self.scheduler,
        self.framework,
        self.credential,
        self.detector,
    )
    self.context.spawn(self.scheduler_process)
    self.status = mesos_pb2.DRIVER_RUNNING
    return self.status

  @locked
  def stop(self, failover=False):
    if self.status not in (mesos_pb2.DRIVER_RUNNING, mesos_pb2.DRIVER_ABORTED):
      return self.status

    if self.scheduler_process is not None:
      self.context.dispatch(self.scheduler_process.pid, 'stop', failover)

    aborted = self.status == mesos_pb2.DRIVER_ABORTED
    self.status = mesos_pb2.DRIVER_STOPPED
    self.lock.notify()
    return mesos_pb2.DRIVER_ABORTED if aborted else self.status

  @locked
  def abort(self):
    if self.status is not mesos_pb2.DRIVER_RUNNING:
      return self.status

    assert self.scheduler_process is not None
    self.scheduler_process.aborted.set()
    self.context.dispatch(self.scheduler_process.pid, 'abort')
    self.status = mesos_pb2.DRIVER_ABORTED
    self.lock.notify()
    return self.status

  @locked
  def join(self):
    if self.status is not mesos_pb2.DRIVER_RUNNING:
      return self.status

    while self.status is mesos_pb2.DRIVER_RUNNING:
      self.lock.wait()  # Wait until the driver notifies us to break

    log.info("Scheduler driver finished with status %d", self.status)
    assert self.status in (mesos_pb2.DRIVER_ABORTED, mesos_pb2.DRIVER_STOPPED)
    return self.status

  @locked
  def run(self):
    self.status = self.start()
    return self.status if self.status is not mesos_pb2.DRIVER_RUNNING else self.join()

  @locked
  def requestResources(self, requests):
    if self.status is not mesos_pb2.DRIVER_RUNNING:
      return self.status
    assert self.scheduler_process is not None
    self.context.dispatch(self.scheduler_process.pid, 'request_resources', requests)
    return self.status

  @locked
  def launchTasks(self, offer_ids, tasks, filters=None):
    if self.status is not mesos_pb2.DRIVER_RUNNING:
      return self.status
    assert self.scheduler_process is not None
    self.context.dispatch(self.scheduler_process.pid, 'launch_tasks', offer_ids, tasks, filters)
    return self.status

  @locked
  def killTask(self, task_id):
    if self.status is not mesos_pb2.DRIVER_RUNNING:
      return self.status
    assert self.scheduler_process is not None
    self.context.dispatch(self.scheduler_process.pid, 'kill_task', task_id)
    return self.status

  @locked
  def declineOffer(self, offer_id, filters=None):
    return self.launch_tasks([offer_id], [], filters)

  @locked
  def reviveOffers(self):
    if self.status is not mesos_pb2.DRIVER_RUNNING:
      return self.status
    assert self.scheduler_process is not None
    self.context.dispatch(self.scheduler_process.pid, 'revive_offers')
    return self.status

  @locked
  def sendFrameworkMessage(self, executor_id, slave_id, data):
    if self.status is not mesos_pb2.DRIVER_RUNNING:
      return self.status
    assert self.scheduler_process is not None
    self.context.dispatch(
        self.scheduler_process.pid,
        'send_framework_message',
        executor_id,
        slave_id,
        data,
    )
    return self.status

  @locked
  def reconcileTasks(self, statuses):
    if self.status is not mesos_pb2.DRIVER_RUNNING:
      return self.status
    assert self.scheduler_process is not None
    self.context.dispatch(self.scheduler_process.pid, 'reconcile_tasks', statuses)
    return self.status

  # idiomatic snake_case aliases.
  request_resources = requestResources
  launch_tasks = launchTasks
  kill_task = killTask
  decline_offer = declineOffer
  revive_offers = reviveOffers
  send_framework_message = sendFrameworkMessage
  reconcile_tasks = reconcileTasks
