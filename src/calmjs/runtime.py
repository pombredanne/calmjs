# -*- coding: utf-8 -*-
"""
The calmjs runtime collection
"""

from __future__ import absolute_import

import logging
import re
import sys
from collections import namedtuple
from functools import partial
from argparse import Action
from argparse import SUPPRESS
from inspect import currentframe
from os.path import exists

from pkg_resources import working_set as default_working_set

from calmjs.argparse import ArgumentParser
from calmjs.argparse import HyphenNoBreakFormatter
from calmjs.argparse import StoreRequirementList
from calmjs.argparse import Version
from calmjs.argparse import ATTR_INFO
from calmjs.argparse import ATTR_ROOT_PKG
from calmjs.exc import RuntimeAbort
from calmjs.registry import get
from calmjs.toolchain import Spec
from calmjs.toolchain import ToolchainCancel
from calmjs.toolchain import ADVICE_PACKAGES
from calmjs.toolchain import AFTER_PREPARE
from calmjs.toolchain import BUILD_DIR
from calmjs.toolchain import CALMJS_TOOLCHAIN_ADVICE
from calmjs.toolchain import DEBUG
from calmjs.toolchain import EXPORT_TARGET
from calmjs.toolchain import EXPORT_TARGET_OVERWRITE
from calmjs.toolchain import SETUP
from calmjs.toolchain import WORKING_DIR
from calmjs.ui import prompt_overwrite_json
from calmjs.ui import prompt
from calmjs.utils import pretty_logging
from calmjs.utils import pdb_post_mortem

CALMJS = 'calmjs'
CALMJS_RUNTIME = 'calmjs.runtime'
logger = logging.getLogger(__name__)
DEST_ACTION = 'action'
DEST_RUNTIME = 'runtime'

levels = {
    -2: logging.CRITICAL,
    -1: logging.ERROR,
    0: logging.WARNING,
    1: logging.INFO,
    2: logging.DEBUG,
}

valid_command_name = re.compile('^[0-9a-zA-Z]*$')
_global_runtime_attrs = {}


def _global_runtime_attrs_get(key):
    result = _global_runtime_attrs.get(key)
    if isinstance(result, int):
        return result
    # should probably log this, however due to certain checking in place
    # this message may end up being swallowed.
    return 0


def _reset_global_runtime_attrs():
    _global_runtime_attrs.clear()
    _global_runtime_attrs.update({
        'debug': 0,
        'log_level': 0,
        'verbosity': 0,
    })

_reset_global_runtime_attrs()


def _initialize_global_runtime_attrs(**kwargs):
    debug = kwargs.pop('debug')
    verbosity = min(max(kwargs.pop('verbose') - kwargs.pop('quiet'), -2), 2)
    log_level = levels.get(verbosity)
    _global_runtime_attrs.update({
        'debug': debug,
        'log_level': log_level,
        'verbosity': verbosity,
    })


def norm_args(args):
    return sys.argv[1:] if args is None else (args or [])


class BootstrapRuntime(object):
    """
    calmjs bootstrap runtime.
    """

    def __init__(self, prog=None):
        self.prog = prog
        self.__argparser = None

    @property
    def argparser(self):
        """
        For setting up the argparser for this instance.
        """

        if self.__argparser is None:
            self.__argparser = self.argparser_factory()
            self.init_argparser(self.__argparser)
        return self.__argparser

    def argparser_factory(self):
        """
        Produces argparser for this type of Runtime.
        """

        return ArgumentParser(
            prog=self.prog, description=self.__doc__, add_help=False,
            formatter_class=HyphenNoBreakFormatter,
        )

    def init_argparser(self, argparser):
        self.global_opts = argparser.add_argument_group('global options')
        self.global_opts.add_argument(
            '-d', '--debug', action='count', default=0,
            help="show traceback on error; twice for post_mortem '--debugger' "
                 "when execution cannot continue")
        self.global_opts.add_argument(
            '--debugger', action='store_const', const=2, dest='debug',
            help=SUPPRESS)
        self.global_opts.add_argument(
            '-q', '--quiet', action='count', default=0,
            help="be more quiet")
        self.global_opts.add_argument(
            '-v', '--verbose', action='count', default=0,
            help="be more verbose")

    def prepare_keywords(self, kwargs):
        _initialize_global_runtime_attrs(**kwargs)

    # should be able to not duplicate all these property definitions
    @property
    def debug(self):
        return _global_runtime_attrs_get('debug')

    @property
    def log_level(self):
        return _global_runtime_attrs_get('log_level')

    @property
    def verbosity(self):
        return _global_runtime_attrs_get('verbosity')

    def run(self, argparser=None, **kwargs):
        self.prepare_keywords(kwargs)

    def __call__(self, args):
        parsed, extras = self.argparser.parse_known_args(args)
        kwargs = vars(parsed)
        self.run(argparser=self.argparser, **kwargs)
        return extras


class BaseRuntime(BootstrapRuntime):
    """
    calmjs runtime collection
    """

    def __init__(
            self, logger='calmjs', action_key=DEST_RUNTIME,
            working_set=default_working_set, package_name=None,
            description=None, *a, **kw):
        """
        Keyword Arguments:

        logger
            The logger to enable for pretty logging.

            Default: the calmjs root logger

        action_key
            The destination key where the command will be stored.  Under
            this key the target driver runtime will be stored, and it
            will be popped off first before passing rest of kwargs to
            it.

        working_set
            The working_set to use for this instance.

            Default: pkg_resources.working_set

        package_name
            The package name that this instance of runtime is for.  Used
            for the version flag.

        description
            The description for this runtime.
        """

        self.logger = logger
        self.action_key = action_key
        self.working_set = working_set
        self.description = description or self.__doc__
        self.package_name = package_name
        super(BaseRuntime, self).__init__(*a, **kw)

    def argparser_factory(self):
        argparser = ArgumentParser(
            prog=self.prog, description=self.description,
            formatter_class=HyphenNoBreakFormatter,
        )
        setattr(argparser, ATTR_ROOT_PKG, self.package_name)
        return argparser

    def init_argparser(self, argparser):
        super(BaseRuntime, self).init_argparser(argparser)
        self.global_opts.add_argument(
            '-V', '--version', action=Version, default=0,
            help="print version information")

    def run(self, argparser=None, **kwargs):
        """
        Subclasses should have their own running method.
        """

    def error(self, argparser, target, message):
        """
        This is needed due to how the argparser may fail at deriving the
        correct subcommand to include.  Although this BaseRuntime does
        not implement this functionality, this method is reserved for
        subclasses to handle that.
        """

        self.argparser.error(message)

    def __call__(self, args=None):
        args = norm_args(args)
        # MUST use the bootstrap runtime class to process all the common
        # flags as inconsistent handling of these between different
        # versions of Python make this extremely annoying.
        #
        # For a simple minimum demonstration, see:
        # https://gist.github.com/metatoaster/16bb6046d6363682b4c4497518436fc5

        # While we would love to do this:
        # args = BootstrapRuntime.__call__(self, args)
        # It doesn't work, because we will NOT be using the argparser
        # definition created by BootstrapRuntime... so we ened to do it
        # the long way.
        bootstrap = BootstrapRuntime()
        # Also, remember that we need to strip off all the args that
        # the bootstrap knows, only process any leftovers.
        args = bootstrap(args)

        # NOT using parse_args directly because argparser is dumb when
        # it comes to bad keywords in a subparser - it doesn't invoke
        # its help text.  Nor does it keep track of what or where the
        # extra arguments actually came from.  So we are going to do
        # this manually so the users don't get confused.
        parsed, extras = self.argparser.parse_known_args(args)
        kwargs = vars(parsed)
        target = kwargs.get(self.action_key)

        if extras:
            # first step, figure out where exactly the problem is
            before = args[:args.index(target)] if target in args else args
            # Now, take everything before the target and see that it
            # got consumed
            bootstrap = BootstrapRuntime()
            # get arguments that failed the bootstrap stage, if any
            bootfail = bootstrap(before)
            # msg has no gettext applied as in argparser module version
            msg = 'unrecognized arguments: %s' % ' '.join(bootfail or extras)
            if bootfail:
                # failed on the root parser, deal with this here now.
                self.argparser.error(msg)
            else:
                # let the implementation specific handling deal with it
                self.error(self.argparser, target, msg)

        with pretty_logging(
                logger=self.logger, level=self.log_level, stream=sys.stderr):
            try:
                return self.run(argparser=self.argparser, **kwargs)
            except KeyboardInterrupt:
                logger.critical('termination requested; aborted.')
            except Exception as e:
                if isinstance(e, RuntimeAbort):
                    reason = 'expected unrecoverable condition'
                else:
                    reason = 'unexpected error'

                if self.debug:
                    # ensure debug is handled completely
                    logger.critical(
                        'terminating due to %s', reason, exc_info=1)
                    if self.debug > 1:
                        pdb_post_mortem(sys.exc_info()[2])
                elif isinstance(e, RuntimeAbort):
                    logger.critical('terminating due to %s', reason)
                elif not self.debug:
                    logger.critical(
                        '%s: %s', type(e).__name__, e)
                    logger.critical(
                        'terminating due to %s; for '
                        'details please refer to previous log entries, or by '
                        'retrying with more verbosity (-v) and/or enable '
                        'debug/traceback output (-d)', reason
                    )
            return False


class Runtime(BaseRuntime):
    """
    The main root runtime class.
    """

    def __init__(self, entry_point_group=CALMJS_RUNTIME, *a, **kw):
        """
        The init method takes an additional argument.

        entry_point_group
            The group of entry points that should be checked.
            default: calmjs.runtime
        """

        self.entry_point_group = entry_point_group
        self.argparser_details = {}
        self.ArgumentParserDetails = namedtuple('ArgumentParserDetails', [
            'subparsers', 'runtimes', 'entry_points'])
        super(Runtime, self).__init__(*a, **kw)

    def log_debug_error(self, *a, **kw):
        f = logger.exception if self.debug else logger.error
        f(*a, **kw)

    def entry_point_load_validated(self, entry_point):
        try:
            # load the runtime instance
            inst = entry_point.load()
        except Exception as e:
            self.log_debug_error(
                "bad '%s' entry point '%s' from '%s': %s: %s",
                self.entry_point_group, entry_point, entry_point.dist,
                e.__class__.__name__, e,
            )
            return None

        if not isinstance(inst, BaseRuntime):
            logger.error(
                "bad '%s' entry point '%s' from '%s': "
                "target not a calmjs.runtime.DriverRuntime instance; "
                "not registering ignored entry point",
                self.entry_point_group, entry_point, entry_point.dist,
            )
            return None

        if not valid_command_name.match(entry_point.name):
            logger.error(
                "bad '%s' entry point '%s' from '%s': "
                "entry point name must be a latin alphanumeric string; "
                "not registering ignored entry point",
                self.entry_point_group, entry_point, entry_point.dist,
            )
            return None

        if isinstance(self, type(inst)):
            # this avoids later recursion
            logger.debug(
                'invalidating entry_point %s from Runtime %r, as the instance '
                'made available at the entry_point must not be a sibling or '
                'lower.', entry_point, self,
            )
            return None

        return inst

    def iter_entry_points(self):
        for entry_point in self.working_set.iter_entry_points(
                self.entry_point_group):
            yield entry_point

    def init_argparser(self, argparser):
        """
        This should not be called with an external argparser as it will
        corrupt tracking data if forced.
        """

        def prepare_argparser():
            if argparser in self.argparser_details:
                return False
            result = self.argparser_details[
                argparser] = self.ArgumentParserDetails({}, {}, {})
            return result

        def to_module_attr(ep):
            return '%s:%s' % (ep.module_name, '.'.join(ep.attrs))

        def register(name, runtime, entry_point):
            subparser = commands.add_parser(
                name, help=inst.description,
                formatter_class=HyphenNoBreakFormatter,
            )
            # Have to specify this separately because otherwise the
            # subparser will not have a proper description when it is
            # invoked as the root.
            subparser.description = inst.description

            # Assign values for version reporting system
            setattr(subparser, ATTR_ROOT_PKG, getattr(
                argparser, ATTR_ROOT_PKG, self.package_name))
            subp_info = []
            subp_info.extend(getattr(argparser, ATTR_INFO, []))
            subp_info.append((subparser.prog, entry_point.dist))
            setattr(subparser, ATTR_INFO, subp_info)

            try:
                try:
                    runtime.init_argparser(subparser)
                except RuntimeError as e:
                    # first attempt to filter out recursion errors; also if
                    # the stack frame isn't available the complaint about
                    # bad validation doesn't apply anyway.
                    frame = currentframe()
                    if (not frame or 'maximum recursion depth' not in str(
                            e.args)):
                        raise

                    logger.critical(
                        "Runtime subclass at entry_point '%s' is implemented "
                        "without a proper 'entry_point_load_validated' to "
                        "filter out its higher level Runtime classes; it "
                        "should at least not override that, or override that "
                        "with the checking in place.", entry_point
                    )
            except Exception as e:
                self.log_debug_error(
                    "cannot register entry_point '%s' from '%s' as a "
                    "subcommand to '%s': %s: %s",
                    entry_point, entry_point.dist, argparser.prog,
                    e.__class__.__name__, e
                )
            else:
                # finally record the completely initialized subparser
                # into the structure here if successful.
                subparsers[name] = subparser
                runtimes[name] = runtime
                entry_points[name] = entry_point

        details = prepare_argparser()
        if not details:
            logger.debug(
                'argparser %r has already been initialized against runner %r',
                argparser, self,
            )
            return
        subparsers, runtimes, entry_points = details

        super(Runtime, self).init_argparser(argparser)

        commands = argparser.add_subparsers(
            dest=self.action_key, metavar='<command>')

        for entry_point in self.iter_entry_points():
            inst = self.entry_point_load_validated(entry_point)
            if not inst:
                continue

            if entry_point.name in runtimes:
                reg_ep = entry_points[entry_point.name]
                reg_rt = runtimes[entry_point.name]

                if reg_rt is inst:
                    # this is fine, multiple packages declared the same
                    # thing with the same name.
                    logger.debug(
                        "duplicated registration of command '%s' via entry "
                        "point '%s' ignored; registered '%s', confict '%s'",
                        entry_point.name, entry_point, reg_ep.dist,
                        entry_point.dist,
                    )
                    continue

                logger.error(
                    "a calmjs runtime command named '%s' already registered.",
                    entry_point.name
                )
                logger.info("conflicting entry points are:")
                logger.info(
                    "'%s' from '%s' (registered)", reg_ep, reg_ep.dist)
                logger.info(
                    "'%s' from '%s' (conflict)", entry_point, entry_point.dist)
                # Fall back name should work if the class/instances are
                # stable.
                name = to_module_attr(entry_point)

                if name in runtimes:
                    # Maybe this is the third time this module is
                    # registered.  Test for its identity.
                    if runtimes[name] is not inst:
                        # Okay someone is having a fun time here mucking
                        # with data structures internal to here, likely
                        # (read hopefully) due to testing or random
                        # monkey patching (or module level reload).
                        logger.critical(
                            "'%s' is already registered but points to a "
                            "completely different instance; please try again "
                            "with verbose logging and note which packages are "
                            "reported as conflicted; alternatively this is a "
                            "forced situation where this Runtime instance has "
                            "been used or initialized improperly.",
                            name
                        )
                    else:
                        logger.debug(
                            "fallback command '%s' is already registered.",
                            name
                        )
                    continue

                logger.error(
                    "falling back to using full instance path '%s' as command "
                    "name, also registering alias for registered command", name
                )
                register(to_module_attr(reg_ep), reg_rt, reg_ep)
            else:
                name = entry_point.name

            register(name, inst, entry_point)

    def get_argparser_details(self, argparser):
        details = self.argparser_details.get(argparser)
        if details:
            return details
        logger.error('provided argparser not registered to this runtime.')

    def error(self, argparser, target, message):
        """
        This is needed due to how the argparser may fail at deriving the
        correct subcommand to include.  Although this BaseRuntime does
        not implement this functionality, this method is reserved for
        subclasses to handle that.
        """

        details = self.get_argparser_details(argparser)
        argparser = details.subparsers[target] if details else self.argparser
        argparser.error(message)

    def run(self, argparser=None, **kwargs):
        details = self.get_argparser_details(argparser)
        if not details:
            logger.critical(
                'runtime cannot continue due to missing argparser details')
            return
        action_key = kwargs.pop(self.action_key)
        runtime = details.runtimes.get(action_key)
        subparser = details.subparsers.get(action_key)
        if runtime:
            return runtime.run(argparser=subparser, **kwargs)
        # nothing is going to happen otherwise?


class CalmJSRuntime(Runtime):

    def __init__(self, package_name=CALMJS, *a, **kw):
        """
        The main CalmJS runtime

        package_name is provided a default of 'calmjs'.
        """

        super(CalmJSRuntime, self).__init__(package_name=package_name)


class DriverRuntime(BaseRuntime):
    """
    runtime for driver
    """

    def __init__(self, cli_driver, action_key=DEST_ACTION, *a, **kw):
        self.cli_driver = cli_driver
        super(DriverRuntime, self).__init__(action_key=action_key, *a, **kw)


class ToolchainRuntime(DriverRuntime):
    """
    Specialized runtime for toolchain.
    """

    @property
    def toolchain(self):
        # mostly serving as an alias
        return self.cli_driver

    def init_argparser_export_target(
            self, argparser,
            default=None,
            help='the export target',
            ):
        """
        Subclass could override this by providing alternative keyword
        arguments and call this as its super.  It should not reimplement
        this completely.  Example:

        def init_argparser_export_target(self, argparser):
            super(MyToolchainRuntime, self).init_argparser_export_target(
                argparser, default='my_default.js',
                help="the export target, default is 'my_default.js'",
            )

        Note that the above example will prevent its subclasses from
        directly using the definition of that class, but they _can_
        simply call the exact same super, or invoke ToolchainRuntime's
        init_argparser_* method directly.

        Arguments

        default
            The default value.
        help
            The help text.
        """

        argparser.add_argument(
            '-w', '--overwrite', dest=EXPORT_TARGET_OVERWRITE,
            default=default, action='store_true',
            help='overwrite the export target without any confirmation',
        )

        argparser.add_argument(
            '--export-target', dest=EXPORT_TARGET,
            default=default,
            help=help,
        )

    def init_argparser_working_dir(
            self, argparser,
            explanation='',
            help_template=(
                'the working directory; %(explanation)s'
                'default is current working directory (%(cwd)s)'),
            ):
        """
        Subclass could an extra expanation on how this is used.

        Arguments

        explanation
            Explanation text for the default help template
        help_template
            A standard help message for this option.
        """

        cwd = self.toolchain.join_cwd()
        argparser.add_argument(
            '--working-dir', dest=WORKING_DIR,
            default=cwd,
            help=help_template % {'explanation': explanation, 'cwd': cwd},
        )

    def init_argparser_build_dir(
            self, argparser, help=(
                'the build directory, where all sources will be copied to '
                'as part of the build process; if left unspecified, the '
                'default behavior is to create a new temporary directory '
                'that will be removed upon conclusion of the build; if '
                'specified, it must be an existing directory and all files '
                'for the build will be copied there instead, overwriting any '
                'existing file, with no cleanup done after.'
            )):
        """
        For setting up build directory
        """

        argparser.add_argument(
            '--build-dir', default=None, dest=BUILD_DIR, help=help)

    def init_argparser_optional_advice(
            self, argparser, default=[], help=(
                'a comma separated list of packages to retrieve optional '
                'advice from; the provided packages should have registered '
                'the appropriate entry points for setting up the advices for '
                'the toolchain; refer to documentation for the specified '
                'packages for details'
            )):
        """
        For setting up optional advice.
        """

        argparser.add_argument(
            '--optional-advice', default=default, required=False,
            dest=ADVICE_PACKAGES, action=StoreRequirementList,
            help=help
        )

    def init_argparser(self, argparser):
        """
        Other runtimes (or users of ArgumentParser) can pass their
        subparser into here to collect the arguments here for a
        subcommand.
        """

        super(ToolchainRuntime, self).init_argparser(argparser)

        # it is possible for subclasses to fully override this, but if
        # they are using this as the runtime to drive the toolchain they
        # should be prepared to follow the layout, but if they omit them
        # it should only result in the spec omitting these arguments.
        self.init_argparser_export_target(argparser)
        self.init_argparser_working_dir(argparser)
        self.init_argparser_build_dir(argparser)
        self.init_argparser_optional_advice(argparser)

    def check_export_target_exists(self, spec):
        # to ensure the key is really available.
        export_target = spec.get(EXPORT_TARGET)

        if not export_target:
            logger.warning(
                "spec missing key 'export_target'; no destination check will "
                "be done"
            )
            return
        if not exists(export_target):
            return

        if spec.get(EXPORT_TARGET_OVERWRITE):
            logger.warning(
                "export target location '%(export_target)s' already exists; "
                "it may be overwritten as the overwrite flag is specified.",
                {'export_target': export_target},
            )
            return

        overwrite = prompt(
            u"export target '%(export_target)s' already exists, overwrite?" %
            {'export_target': export_target},
            choices=(
                (u'Yes', True),
                (u'No', False),
            ),
            default_key=1,
        )
        if not overwrite:
            raise ToolchainCancel('cancelation initiated by user')

    def prepare_spec_debug_flag(self, spec, **kwargs):
        spec[DEBUG] = self.debug

    def prepare_spec_export_target_checks(self, spec, **kwargs):
        spec[EXPORT_TARGET_OVERWRITE] = kwargs.get(EXPORT_TARGET_OVERWRITE)
        spec.advise(AFTER_PREPARE, self.check_export_target_exists, spec)

    def prepare_spec_advice_packages(self, spec, **kwargs):
        reg = get(CALMJS_TOOLCHAIN_ADVICE)
        advice_packages = kwargs.get(ADVICE_PACKAGES) or []
        if isinstance(advice_packages, (list, tuple)):
            logger.debug(
                'prepare spec with optional advices from packages %r',
                advice_packages
            )
            for pkg_name in advice_packages:
                reg.process_toolchain_spec_package(
                    self.toolchain, spec, pkg_name)

    def prepare_spec(self, spec, **kwargs):
        """
        Prepare a spec for usage with the generic ToolchainRuntime.

        Subclasses should avoid overriding this; override create_spec
        instead.
        """

        self.prepare_spec_debug_flag(spec, **kwargs)
        self.prepare_spec_export_target_checks(spec, **kwargs)
        # defer the setup till the actual toolchain invocation
        spec.advise(SETUP, self.prepare_spec_advice_packages, spec, **kwargs)

    def create_spec(self, **kwargs):
        """
        Subclasses should override this if they take actual parameters.
        It must produce a ``Spec`` from the given keyword arguments.
        """

        return Spec(**kwargs)

    def kwargs_to_spec(self, **kwargs):
        """
        Turn the provided kwargs into arguments ready for toolchain.
        """

        spec = self.create_spec(**kwargs)
        self.prepare_spec(spec, **kwargs)
        return spec

    def run(self, argparser=None, **kwargs):
        spec = self.kwargs_to_spec(**kwargs)
        self.toolchain(spec)
        return spec


class PackageManagerAction(Action):
    """
    Package manager specific action
    """

    def __init__(self, option_strings, dest, *a, **kw):
        super(PackageManagerAction, self).__init__(
            option_strings=option_strings, dest=dest, nargs=0, *a, **kw)

    def __call__(self, parser, namespace, values, option_string=None):
        priority, f = getattr(namespace, self.dest) or (0, None)
        new_priority, f = self.const
        if new_priority > priority:
            setattr(namespace, self.dest, self.const)
        if new_priority == 1:
            # Yes this is _really_ magical, however this is the way to
            # set attributes the cli later needs through the argparser.
            setattr(namespace, 'stream', sys.stdout)


class PackageManagerRuntime(DriverRuntime):
    """
    A calmjs package manager runtime
    """

    _pkg_manager_options = (
        ('view', None,
         "generate '%(pkgdef_filename)s' for the specified Python package "
         "and write to stdout for viewing [default]"),
        ('init', None,
         "generate and write '%(pkgdef_filename)s' to the "
         "current directory for the specified Python package"),
        # This required implicit step is done, otherwise there are no
        # difference to running ``npm init`` directly from the shell.
        ('install', None,
         "run '%(pkg_manager_bin)s %(install_cmd)s' with generated "
         "'%(pkgdef_filename)s'; implies init; will abort if init fails "
         "to write the generated file"),
        # As far as I know typically setuptools setup.py are not
        # interactive, so we keep it that way unless user explicitly
        # want this.  Consequence is that the generic tool will do the
        # same.
        ('interactive', 'i',
         "enable interactive prompt; if an action requires an explicit "
         "response but none were specified through flags "
         "(i.e. overwrite), prompt for response; disabled by default"),
        ('merge', 'm',
         "merge generated '%(pkgdef_filename)s' with the one in current "
         "directory; if interactive mode is not enabled, implies "
         "overwrite, else the difference will be displayed"),
        ('overwrite', 'w',
         "automatically overwrite any file changes to current directory "
         "without prompting"),
        ('explicit', 'E',
         "explicit mode disables resolution for dependencies; only the "
         "specified Python package will be used."),
        ('production', 'P',
         "explicitly specify production mode for "
         "%(pkg_manager_bin)s %(install_cmd)s"),
        ('development', 'D',
         "explicitly specify development mode for "
         "%(pkg_manager_bin)s %(install_cmd)s"),
    )

    def make_cli_options(self):
        return [
            (full, s, desc % {
                'install_cmd': self.cli_driver.install_cmd,
                'pkgdef_filename': self.cli_driver.pkgdef_filename,
                'pkg_manager_bin': self.cli_driver.pkg_manager_bin,
            }) for full, s, desc in self._pkg_manager_options
        ]

    def __init__(self, *a, **kw):
        super(PackageManagerRuntime, self).__init__(*a, **kw)
        self.default_action = None
        self.pkg_manager_options = self.make_cli_options()

    def init_argparser(self, argparser):
        """
        Other runtimes (or users of ArgumentParser) can pass their
        subparser into here to collect the arguments here for a
        subcommand.
        """

        super(PackageManagerRuntime, self).init_argparser(argparser)

        # Ideally, we could use more subparsers for each action (i.e.
        # init and install).  However, this is complicated by the fact
        # that setuptools has its own calling conventions through the
        # setup.py file, and to present a consistent cli to end-users
        # from both calmjs entry point and setuptools using effectively
        # the same codebase will require a bit of creative handling.

        # provide this for the setuptools command class.
        actions = argparser.add_argument_group('action arguments')
        count = 0

        for full, short, desc in self.pkg_manager_options:
            args = [
                dash + key
                for dash, key in zip(('-', '--'), (short, full))
                if key
            ]
            # default is singular, but for the argparsed version in our
            # runtime permits multiple packages.
            desc = desc.replace('Python package', 'Python package(s)')
            if not short:
                f = getattr(self.cli_driver, '%s_%s' % (
                    self.cli_driver.binary, full), None)
                if callable(f):
                    count += 1
                    actions.add_argument(
                        *args, help=desc, action=PackageManagerAction,
                        dest=self.action_key, const=(count, f)
                    )
                    if self.default_action is None:
                        self.default_action = f
                    continue  # pragma: no cover
            argparser.add_argument(*args, help=desc, action='store_true')

        argparser.add_argument(
            'package_names', help='names of the python package to use',
            metavar='package_names', nargs='+',
        )

    def run(self, argpaser=None, interactive=False, **kwargs):
        # Run the underlying package manager.  As the arguments in this
        # subparser is constructed in a way that maps directly with the
        # underlying actions, it can be invoked directly.
        raw = kwargs.pop(self.action_key)
        if raw:
            count, action = raw
        else:
            action = self.default_action
            kwargs['stream'] = sys.stdout
        if interactive:
            kwargs['callback'] = partial(
                prompt_overwrite_json, dumps=self.cli_driver.dumps)

        kwargs['production'] = True if kwargs.get('production') else None
        kwargs['development'] = True if kwargs.get('development') else None
        return action(**kwargs)


def main(args=None, runtime_cls=CalmJSRuntime):
    import warnings
    bootstrap = BootstrapRuntime()
    # None to distinguish args from unspecified or specified as [], but
    # ultimately the value must be a list.
    args = norm_args(args)
    extras = bootstrap(args)
    if not extras:
        args = args + ['-h']

    # all the minimum arguments acquired, bootstrap the execution.
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        # log down the construction of the bootstrap class.
        with pretty_logging(
                logger='', level=bootstrap.log_level, stream=sys.stderr):
            runtime = runtime_cls()
            # access the argparser property to trigger its construction
            # inside this logger context, so that any messages passed to
            # the logger will be correctly handled.
            runtime.argparser

        # Running this outside of the logger, as the BaseRuntime will do
        # its logging.
        if runtime(args):
            sys.exit(0)
        else:
            sys.exit(1)
