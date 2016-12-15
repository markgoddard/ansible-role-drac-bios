#!/usr/bin/python

import collections
import syslog
import time

from ansible.module_utils.basic import *

# Store a list of import errors to report to the user.
IMPORT_ERRORS = []
try:
    import dracclient.client as drac
except Exception as e:
    IMPORT_ERRORS.append(e)


DOCUMENTATION = """
WM
"""

EXAMPLES = """
WM
"""


class UnknownSetting(Exception):
    """A configuration option was not found."""


class Timeout(Exception):
    """A timeout occurred."""


BIOSActions = collections.namedtuple('BIOSActions',
                                     ['abandon', 'flush', 'apply', 'commit'])
"""Which actions are required in order to apply the desired configuration.

abandon: Abandon uncommitted pending changes.
flush: Flush committed pending changes.
apply: Apply settings.
commit: Commit settings.
"""


def debug(module, message):
    """Log a debug message.

    :param module: The AnsibleModule instance
    :param message: The message to log
    """
    log_args = {"PRIORITY": syslog.LOG_DEBUG, "MODULE": "drac_bios",
                "CODE_FILE": "drac_bios.py"}
    module.log(message, log_args)


def build_client(module):
    """Build a DRAC client instance.

    :param module: The AnsibleModule instance
    :returns: dracclient.client.DRACClient instance
    """
    return drac.DRACClient(module.params['address'],
                           module.params['username'],
                           module.params['password'])


def has_committed_config_job(module, bmc):
    """Determine whether there are any pending ConfigBIOS jobs.

    :param module: The AnsibleModule instance
    :param bmc: A dracclient.client.DRACClient instance
    :returns: Whether there are any pending ConfigBIOS jobs
    """
    try:
        jobs = bmc.list_jobs(only_unfinished=True)
    except Exception as e:
        module.fail_json(msg="Failed to check unfinished jobs: %s" %
                         repr(e))
    return any({job.name.startswith('ConfigBIOS') for job in jobs})


def wait_complete(module, bmc):
    """Poll BMC state until there are no unfinished jobs.

    :param module: The AnsibleModule instance
    :param bmc: A dracclient.client.DRACClient instance
    :raises Timeout: If the job queue did not empty before the timeout
    """
    timeout = module.params['timeout']
    interval = module.params['interval']
    end = time.time() + timeout if timeout > 0 else None
    while True:
        try:
            jobs = bmc.list_jobs(only_unfinished=True)
        except Exception as e:
            module.fail_json(msg="Failed to check unfinished jobs: %s" %
                             repr(e))
        if len(jobs) == 0:
            debug(module, "No pending jobs")
            return

        job_descs = [repr(job) for job in jobs]
        # Check for timeouts.
        if end and time.time() > end:
            raise Timeout("Timed out after %s seconds waiting for BMC to "
                          "complete pending jobs: %s" %
                          (timeout, ", ".join(job_descs)))

        debug(module, "Waiting for pending jobs to complete: %s" %
              ", ".join(job_descs))
        time.sleep(interval)


def check_settings(module, bios_settings):
    """Check the current BIOS settings against the desired configuration.

    :param module: The AnsibleModule instance
    :param bios_settings: The current BIOS settings
    :returns: A 3-tuple containing the settings to be changed, whether there
        are any non-conflicting pending changes and whether there are any
        conflicting pending changes
    """
    goal_settings = module.params['config']
    unknown = set(goal_settings) - set(bios_settings)
    if unknown:
        module.fail_json(msg="BIOS setting(s) do not exist: %s" %
                         ", ".join(unknown))

    changing_settings = {}
    pending = False
    conflicting = False
    for key, goal_setting in goal_settings.items():
        bios_setting = bios_settings[key]
        # If there is a pending change and it is correct, we do not need to
        # apply this setting.
        if bios_setting.pending_value is not None:
            if bios_setting.pending_value == goal_setting:
                pending = True
                continue
            else:
                conflicting = True

        # We need to apply this setting if the current value is not the
        # desired value or there is a pending change.
        if (bios_setting.current_value != goal_setting or
                bios_setting.pending_value is not None):
            changing_settings[key] = goal_setting

    return changing_settings, pending, conflicting


"""
Committed, pending, not changing: flush
Uncommitted, pending, not changing: commit
"""
def get_actions(module, bmc, changing, pending, conflicting):
    """Get the required actions based on the current and desired BIOS config.

    :param module: The AnsibleModule instance
    :param bmc: A dracclient.client.DRACClient instance
    :param changing: Whether there are any changes to be applied
    :param pending: Whether there are any non-conflicting pending changes
    :param conflicting: Whether there are any conflicting pending changes
    :returns: A BIOSActions instance with the required actions
    """
    # Determine whether there is an existing ConfigBIOS job that would conflict
    # with any changes we wish to apply.
    committed_job = has_committed_config_job(module, bmc)

    # Whether we only have pending changes we wish to apply.
    pending_only = not changing and pending
    do_reboot = module.params['reboot']

    actions = BIOSActions(
        # If there are uncommitted, conflicting changes, then these must be
        # abandonded before applying further changes.
        abandon=not committed_job and conflicting,
        # If there are committed changes and we wish to apply either those
        # changes or further changes then the node must be rebooted to flush
        # the committed changes.
        flush=committed_job and (changing or (pending_only and do_reboot)),
        # If we have changes to apply, then we must apply them...
        apply=changing,
        # If we have changes to apply or there are uncommitted pending changes
        # that we are interested in then we must commit them.
        commit=changing or (not committed_job and pending_only),
    )

    if actions.flush and not do_reboot:
        # Flushing committed pending changes requires a reboot. Ensure that
        # we are able to do it.
        module.fail_json(msg="Not rebooting to resolve conflicting "
                         "pending BIOS settings due to reboot module "
                         "argument.")

    return actions


def check(module, bmc):
    """Check for any configuration changes and actions required to apply them.

    :param module: The AnsibleModule instance
    :param bmc: A dracclient.client.DRACClient instance
    :returns: A 3-tuple containing a dict of settings that will be changed by
        this operation, a dict of settings to be applied and a BIOSActions
        instance containing actions required to apply the configuration
    """
    debug(module, "Checking BIOS settings")
    try:
        bios_settings = bmc.list_bios_settings()
    except Exception as e:
        module.fail_json(msg="Failed to list BIOS settings: %s" % repr(e))

    # Check the current BIOS settings against the desired settings.
    changing_settings, pending, conflicting = check_settings(
        module, bios_settings)

    if changing_settings:
        debug(module, "Changing settings: %s" % ", ".join(changing_settings))
    else:
        debug(module, "Not changing any settings")
    debug(module, "pending: %s conflicting: %s" % (pending, conflicting))

    # Determine which actions need to be taken to apply the configuration.
    changing = bool(changing_settings)
    actions = get_actions(module, bmc, changing, pending, conflicting)

    debug(module, "Actions: %s" % repr(actions))

    apply_settings = changing_settings.copy()
    if actions.abandon:
        # After abandoning pending changes we need to ensure that we will
        # apply all changes which were previously pending, including those we
        # are not specifically changing.
        all_pending = {key: bios_setting.pending_value
                       for key, bios_setting in bios_settings.items()
                       if bios_setting.pending_value is not None}
        apply_settings.update(all_pending)

    return changing_settings, apply_settings, actions


def abandon(module, bmc):
    """Abandon uncommitted pending configuration changes.

    :param module: The AnsibleModule instance
    :param bmc: A dracclient.client.DRACClient instance
    """
    debug(module, "Abandoning pending BIOS configuration changes")
    try:
        bmc.abandon_pending_bios_changes()
    except Exception as e:
        module.fail_json(msg="Failed to abandon pending BIOS jobs: %s" %
                         repr(e))


def flush(module, bmc):
    """Flush any committed pending BIOS configuration changes by rebooting.

    :param module: The AnsibleModule instance
    :param bmc: A dracclient.client.DRACClient instance
    """
    debug(module, "Flushing BIOS settings by rebooting")
    # Reboot the node.
    try:
        bmc.set_power_state('REBOOT')
    except Exception as e:
        module.fail_json(msg="Failed to reboot to apply pending BIOS "
                         "settings: %s" % repr(e))

    # Wait for the reboot to flush pending jobs.
    try:
        wait_complete(module, bmc)
    except Timeout as e:
        module.fail_json(msg="Failed to waiting for reboot to flush "
                         "pending BIOS settings: %s" % repr(e))


def apply(module, bmc, settings):
    """Apply BIOS configuration changes.

    :param module: The AnsibleModule instance
    :param bmc: A dracclient.client.DRACClient instance
    """
    debug(module, "Applying BIOS settings; %s" % settings)
    try:
        bmc.set_bios_settings(settings)
    except Exception as e:
        module.fail_json(msg="Failed while applying BIOS settings: %s" %
                         repr(e))

    # Wait for applied changes to be processed.
    try:
        wait_complete(module, bmc)
    except Timeout as e:
        module.fail_json(msg="Failed while waiting for BIOS setting "
                         "application to complete: %s" % repr(e))


def commit(module, bmc):
    """Commit pending BIOS configuration changes.

    :param module: The AnsibleModule instance
    :param bmc: A dracclient.client.DRACClient instance
    """
    do_reboot = module.params['reboot']
    debug(module, "Committing pending BIOS settings %s reboot" %
          ("with" if do_reboot else "without"))
    try:
        bmc.commit_pending_bios_changes(do_reboot)
    except Exception as e:
        module.fail_json(msg="Failed while committing BIOS settings: %s" %
                         repr(e))

    # If rebooting to apply the changes, wait for the job to complete.
    if do_reboot:
        try:
            wait_complete(module, bmc)
        except Timeout as e:
            module.fail_json(msg="Failed while waiting for node reboot to "
                             "complete: %s" % repr(e))


def configure_bios(module):
    """Configure a node's BIOS via DRAC.

    :param module: The AnsibleModule instance
    :returns: A dict containing settings which have been changed (or if running
        in check mode, those that would have been changed)
    """
    bmc = build_client(module)

    # Gather required changes.
    changing, applying, actions = check(module, bmc)

    if not any(actions) or module.check_mode:
        return changing

    # Abandon pending changes if required.
    if actions.abandon:
        abandon(module, bmc)

    # Reboot to flush previously committed configuration if required.
    if actions.flush:
        flush(module, bmc)

    # If there are any changes to apply, then apply them.
    if actions.apply:
        apply(module, bmc, applying)

    # Commit pending BIOS configuration changes.
    if actions.commit:
        commit(module, bmc)

    return changing


def main():
    """Module entry point."""
    module = AnsibleModule(
        argument_spec=dict(
            address=dict(required=True, type='str'),
            username=dict(required=True, type='str'),
            password=dict(required=True, type='str'),
            config=dict(required=True, type='dict'),
            reboot=dict(default=False, type='bool'),
            timeout=dict(default=0, type='int'),
            interval=dict(default=5, type='int'),
        ),
        supports_check_mode=True,
    )

    # Fail if there were any exceptions when importing modules.
    if IMPORT_ERRORS:
        module.fail_json(msg="Import errors: %s" %
                         ", ".join([repr(e) for e in IMPORT_ERRORS]))

    try:
        changed = configure_bios(module)
    except Exception as e:
        module.fail_json(msg="Failed to configure BIOS: %s" % repr(e))
    else:
        module.exit_json(changed=bool(changed), changed_settings=changed)


if __name__ == '__main__':
    main()
