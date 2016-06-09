import logging

# conditionally import ansible libraries
import ansible
import ansible.constants
import ansible.utils
import ansible.errors

from pkg_resources import parse_version
has_ansible_v2 = parse_version(ansible.__version__) >= parse_version('2.0.0')

if not has_ansible_v2:
    raise ImportError("Only supported with ansible-2.* and newer")

from ansible.plugins.callback import CallbackBase  # NOQA
from ansible.executor.task_queue_manager import TaskQueueManager  # NOQA
from ansible.playbook.play import Play  # NOQA
from ansible.cli import CLI  # NOQA

from pytest_ansible.module_dispatcher import BaseModuleDispatcher  # NOQA
from pytest_ansible.results import AdHocResult  # NOQA

try:
    from logging import NullHandler
except ImportError:
    from logging import Handler

    class NullHandler(Handler):

        def emit(self, record):
            pass

log = logging.getLogger(__name__)
log.addHandler(NullHandler())


class ResultAccumulator(CallbackBase):

    def __init__(self, *args, **kwargs):
        super(ResultAccumulator, self).__init__(*args, **kwargs)
        self.contacted = {}
        self.unreachable = {}

    def v2_runner_on_failed(self, result, *args, **kwargs):
        self.contacted[result._host.get_name()] = result._result

    v2_runner_on_ok = v2_runner_on_failed

    def v2_runner_on_unreachable(self, result):
        self.unreachable[result._host.get_name()] = result._result

    @property
    def results(self):
        return dict(contacted=self.contacted, unreachable=self.unreachable)


class ModuleDispatcherV2(BaseModuleDispatcher):

    '''Pass.'''

    def _run(self, *module_args, **complex_args):
        '''
        The API provided by ansible is not intended as a public API.
        '''

        # Assemble module argument string
        if module_args:
            complex_args.update(dict(_raw_params=' '.join(module_args)))

        # Assert hosts matching the provided pattern exist
        hosts = self.inventory_manager.list_hosts(self.host_pattern)
        if len(hosts) == 0:
            raise Exception("No hosts match:'%s'" % self.host_pattern)

        # Log the module and parameters
        log.debug("[%s] %s: %s" % (self.host_pattern, self.module_name, complex_args))

        parser = CLI.base_parser(
            runas_opts=True,
            async_opts=True,
            output_opts=True,
            connect_opts=True,
            check_opts=True,
            runtask_opts=True,
            vault_opts=True,
            fork_opts=True,
            module_opts=True,
        )
        (options, args) = parser.parse_args([])

        # Pass along cli options
        options.verbosity = 5
        options.connection = self.options.get('connection')
        options.remote_user = self.options.get('user')
        options.become = self.options.get('become')
        options.become_method = self.options.get('become_method')
        options.become_user = self.options.get('become_user')
        options.module_path = self.options.get('module_path')

        # Initialize callback to capture module JSON responses
        cb = ResultAccumulator()

        kwargs = dict(
            inventory=self.inventory_manager,
            variable_manager=self.variable_manager,
            loader=self.loader,
            options=options,
            stdout_callback=cb,
            passwords=dict(conn_pass=None, become_pass=None),
        )

        # create a pseudo-play to execute the specified module via a single task
        play_ds = dict(
            name="pytest-ansible",
            hosts=self.host_pattern,
            gather_facts='no',
            tasks=[
                dict(
                    action=dict(
                        module=self.module_name, args=complex_args
                    )
                ),
            ]
        )
        log.debug("__run - Building Play() object - %s", play_ds)
        play = Play().load(play_ds, variable_manager=self.variable_manager, loader=self.loader)

        # now create a task queue manager to execute the play
        tqm = None
        try:
            log.debug("__run - TaskQueueManager(%s)", kwargs)
            tqm = TaskQueueManager(**kwargs)
            tqm.run(play)
        finally:
            if tqm:
                tqm.cleanup()

        # Log the results
        log.debug(cb.results)

        # FIXME - should command failures raise an exception, or return?
        # If we choose to raise, callers will need to adapt accordingly
        # Catch any failures in the response
        # for host in results['contacted'].values():
        #     if 'failed' in host or host.get('rc', 0) != 0:
        #         raise Exception("Command failed: %s" % self.module_name, results)

        # Raise exception if host(s) unreachable
        # FIXME - if multiple hosts were involved, should an exception be raised?
        if cb.unreachable:
            # FIXME - unreachable hosts should be included in the exception message
            raise Exception("Host unreachable", dark=cb.unreachable, contacted=cb.contacted)

        # No hosts contacted
        # if not cb.contacted:
        #     raise ansible.errors.AnsibleConnectionFailed("Provided hosts list is empty")

        # Success!
        return AdHocResult(contacted=cb.contacted)