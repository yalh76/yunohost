# -*- coding: utf-8 -*-

""" License

    Copyright (C) 2013 YunoHost

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program; if not, see http://www.gnu.org/licenses

"""

""" yunohost_hook.py

    Manage hooks
"""
import os
import re
import sys
import tempfile
import mimetypes
from glob import iglob
from importlib import import_module

from moulinette import m18n, msettings
from yunohost.utils.error import YunohostError
from moulinette.utils import log
from moulinette.utils.filesystem import read_json

HOOK_FOLDER = '/usr/share/yunohost/hooks/'
CUSTOM_HOOK_FOLDER = '/etc/yunohost/hooks.d/'

logger = log.getActionLogger('yunohost.hook')


def hook_add(app, file):
    """
    Store hook script to filsystem

    Keyword argument:
        app -- App to link with
        file -- Script to add (/path/priority-file)

    """
    path, filename = os.path.split(file)
    priority, action = _extract_filename_parts(filename)

    try:
        os.listdir(CUSTOM_HOOK_FOLDER + action)
    except OSError:
        os.makedirs(CUSTOM_HOOK_FOLDER + action)

    finalpath = CUSTOM_HOOK_FOLDER + action + '/' + priority + '-' + app
    os.system('cp %s %s' % (file, finalpath))
    os.system('chown -hR admin: %s' % HOOK_FOLDER)

    return {'hook': finalpath}


def hook_remove(app):
    """
    Remove hooks linked to a specific app

    Keyword argument:
        app -- Scripts related to app will be removed

    """
    try:
        for action in os.listdir(CUSTOM_HOOK_FOLDER):
            for script in os.listdir(CUSTOM_HOOK_FOLDER + action):
                if script.endswith(app):
                    os.remove(CUSTOM_HOOK_FOLDER + action + '/' + script)
    except OSError:
        pass


def hook_info(action, name):
    """
    Get information about a given hook

    Keyword argument:
        action -- Action name
        name -- Hook name

    """
    hooks = []
    priorities = set()

    # Search in custom folder first
    for h in iglob('{:s}{:s}/*-{:s}'.format(
            CUSTOM_HOOK_FOLDER, action, name)):
        priority, _ = _extract_filename_parts(os.path.basename(h))
        priorities.add(priority)
        hooks.append({
            'priority': priority,
            'path': h,
        })
    # Append non-overwritten system hooks
    for h in iglob('{:s}{:s}/*-{:s}'.format(
            HOOK_FOLDER, action, name)):
        priority, _ = _extract_filename_parts(os.path.basename(h))
        if priority not in priorities:
            hooks.append({
                'priority': priority,
                'path': h,
            })

    if not hooks:
        raise YunohostError('hook_name_unknown', name=name)
    return {
        'action': action,
        'name': name,
        'hooks': hooks,
    }


def hook_list(action, list_by='name', show_info=False):
    """
    List available hooks for an action

    Keyword argument:
        action -- Action name
        list_by -- Property to list hook by
        show_info -- Show hook information

    """
    result = {}

    # Process the property to list hook by
    if list_by == 'priority':
        if show_info:
            def _append_hook(d, priority, name, path):
                # Use the priority as key and a dict of hooks names
                # with their info as value
                value = {'path': path}
                try:
                    d[priority][name] = value
                except KeyError:
                    d[priority] = {name: value}
        else:
            def _append_hook(d, priority, name, path):
                # Use the priority as key and the name as value
                try:
                    d[priority].add(name)
                except KeyError:
                    d[priority] = set([name])
    elif list_by == 'name' or list_by == 'folder':
        if show_info:
            def _append_hook(d, priority, name, path):
                # Use the name as key and a list of hooks info - the
                # executed ones with this name - as value
                l = d.get(name, list())
                for h in l:
                    # Only one priority for the hook is accepted
                    if h['priority'] == priority:
                        # Custom hooks overwrite system ones and they
                        # are appended at the end - so overwite it
                        if h['path'] != path:
                            h['path'] = path
                        return
                l.append({'priority': priority, 'path': path})
                d[name] = l
        else:
            if list_by == 'name':
                result = set()

            def _append_hook(d, priority, name, path):
                # Add only the name
                d.add(name)
    else:
        raise YunohostError('hook_list_by_invalid')

    def _append_folder(d, folder):
        # Iterate over and add hook from a folder
        for f in os.listdir(folder + action):
            if f[0] == '.' or f[-1] == '~' or f.endswith(".pyc"):
                continue
            path = '%s%s/%s' % (folder, action, f)
            priority, name = _extract_filename_parts(f)
            _append_hook(d, priority, name, path)

    try:
        # Append system hooks first
        if list_by == 'folder':
            result['system'] = dict() if show_info else set()
            _append_folder(result['system'], HOOK_FOLDER)
        else:
            _append_folder(result, HOOK_FOLDER)
    except OSError:
        logger.debug("No default hook for action '%s' in %s",
                     action, HOOK_FOLDER)

    try:
        # Append custom hooks
        if list_by == 'folder':
            result['custom'] = dict() if show_info else set()
            _append_folder(result['custom'], CUSTOM_HOOK_FOLDER)
        else:
            _append_folder(result, CUSTOM_HOOK_FOLDER)
    except OSError:
        logger.debug("No custom hook for action '%s' in %s",
                     action, CUSTOM_HOOK_FOLDER)

    return {'hooks': result}


def hook_callback(action, hooks=[], args=None, no_trace=False, chdir=None,
                  env=None, pre_callback=None, post_callback=None):
    """
    Execute all scripts binded to an action

    Keyword argument:
        action -- Action name
        hooks -- List of hooks names to execute
        args -- Ordered list of arguments to pass to the scripts
        no_trace -- Do not print each command that will be executed
        chdir -- The directory from where the scripts will be executed
        env -- Dictionnary of environment variables to export
        pre_callback -- An object to call before each script execution with
            (name, priority, path, args) as arguments and which must return
            the arguments to pass to the script
        post_callback -- An object to call after each script execution with
            (name, priority, path, succeed) as arguments

    """
    result = {}
    hooks_dict = {}

    # Retrieve hooks
    if not hooks:
        hooks_dict = hook_list(action, list_by='priority',
                               show_info=True)['hooks']
    else:
        hooks_names = hook_list(action, list_by='name',
                                show_info=True)['hooks']

        # Add similar hooks to the list
        # For example: Having a 16-postfix hook in the list will execute a
        # xx-postfix_dkim as well
        all_hooks = []
        for n in hooks:
            for key in hooks_names.keys():
                if key == n or key.startswith("%s_" % n) \
                        and key not in all_hooks:
                    all_hooks.append(key)

        # Iterate over given hooks names list
        for n in all_hooks:
            try:
                hl = hooks_names[n]
            except KeyError:
                raise YunohostError('hook_name_unknown', n)
            # Iterate over hooks with this name
            for h in hl:
                # Update hooks dict
                d = hooks_dict.get(h['priority'], dict())
                d.update({n: {'path': h['path']}})
                hooks_dict[h['priority']] = d
    if not hooks_dict:
        return result

    # Validate callbacks
    if not callable(pre_callback):
        pre_callback = lambda name, priority, path, args: args
    if not callable(post_callback):
        post_callback = lambda name, priority, path, succeed: None

    # Iterate over hooks and execute them
    for priority in sorted(hooks_dict):
        for name, info in iter(hooks_dict[priority].items()):
            state = 'succeed'
            path = info['path']
            try:
                hook_args = pre_callback(name=name, priority=priority,
                                         path=path, args=args)
                hook_return = hook_exec(path, args=hook_args, chdir=chdir, env=env,
                          no_trace=no_trace, raise_on_error=True)[1]
            except YunohostError as e:
                state = 'failed'
                hook_return = {}
                logger.error(e.strerror, exc_info=1)
                post_callback(name=name, priority=priority, path=path,
                              succeed=False)
            else:
                post_callback(name=name, priority=priority, path=path,
                              succeed=True)
            if not name in result:
                result[name] = {}
            result[name][path] = {'state' : state, 'stdreturn' : hook_return }
    return result


def hook_exec(path, args=None, raise_on_error=False, no_trace=False,
              chdir=None, env=None, user="root", return_format="json"):
    """
    Execute hook from a file with arguments

    Keyword argument:
        path -- Path of the script to execute
        args -- Ordered list of arguments to pass to the script
        raise_on_error -- Raise if the script returns a non-zero exit code
        no_trace -- Do not print each command that will be executed
        chdir -- The directory from where the script will be executed
        env -- Dictionnary of environment variables to export
        user -- User with which to run the command

    """

    # Validate hook path
    if path[0] != '/':
        path = os.path.realpath(path)
    if not os.path.isfile(path):
        raise YunohostError('file_does_not_exist', path=path)

    # Define output loggers and call command
    loggers = (
        lambda l: logger.debug(l.rstrip() + "\r"),
        lambda l: logger.warning(l.rstrip()) if "invalid value for trace file descriptor" not in l.rstrip() else logger.debug(l.rstrip()),
        lambda l: logger.info(l.rstrip())
    )

    # Check the type of the hook (bash by default)
    # For now we support only python and bash hooks.
    hook_type = mimetypes.MimeTypes().guess_type(path)[0]
    if hook_type == 'text/x-python':
        returncode, returndata = _hook_exec_python(path, args, env, loggers)
    else:
        returncode, returndata = _hook_exec_bash(path, args, no_trace, chdir, env, user, return_format, loggers)

    # Check and return process' return code
    if returncode is None:
        if raise_on_error:
            raise YunohostError('hook_exec_not_terminated', path=path)
        else:
            logger.error(m18n.n('hook_exec_not_terminated', path=path))
            return 1, {}
    elif raise_on_error and returncode != 0:
        raise YunohostError('hook_exec_failed', path=path)

    return returncode, returndata


def _hook_exec_bash(path, args, no_trace, chdir, env, user, return_format, loggers):

    from moulinette.utils.process import call_async_output

    # Construct command variables
    cmd_args = ''
    if args and isinstance(args, list):
        # Concatenate escaped arguments
        cmd_args = ' '.join(shell_quote(s) for s in args)
    if not chdir:
        # use the script directory as current one
        chdir, cmd_script = os.path.split(path)
        cmd_script = './{0}'.format(cmd_script)
    else:
        cmd_script = path

    # Add Execution dir to environment var
    if env is None:
        env = {}
    env['YNH_CWD'] = chdir

    env['YNH_INTERFACE'] = msettings.get('interface')

    stdinfo = os.path.join(tempfile.mkdtemp(), "stdinfo")
    env['YNH_STDINFO'] = stdinfo

    stdreturn = os.path.join(tempfile.mkdtemp(), "stdreturn")
    with open(stdreturn, 'w') as f:
        f.write('')
    env['YNH_STDRETURN'] = stdreturn

    # Construct command to execute
    if user == "root":
        command = ['sh', '-c']
    else:
        command = ['sudo', '-n', '-u', user, '-H', 'sh', '-c']

    if no_trace:
        cmd = '/bin/bash "{script}" {args}'
    else:
        # use xtrace on fd 7 which is redirected to stdout
        cmd = 'BASH_XTRACEFD=7 /bin/bash -x "{script}" {args} 7>&1'

    # prepend environment variables
    cmd = '{0} {1}'.format(
        ' '.join(['{0}={1}'.format(k, shell_quote(v))
                  for k, v in env.items()]), cmd)
    command.append(cmd.format(script=cmd_script, args=cmd_args))

    if logger.isEnabledFor(log.DEBUG):
        logger.debug(m18n.n('executing_command', command=' '.join(command)))
    else:
        logger.debug(m18n.n('executing_script', script=path))

    logger.debug("About to run the command '%s'" % command)

    returncode = call_async_output(
        command, loggers, shell=False, cwd=chdir,
        stdinfo=stdinfo
    )

    raw_content = None
    try:
        with open(stdreturn, 'r') as f:
            raw_content = f.read()
        returncontent = {}

        if return_format == "json":
            if raw_content != '':
                try:
                    returncontent = read_json(stdreturn)
                except Exception as e:
                    raise YunohostError('hook_json_return_error',
                                        path=path, msg=str(e),
                                        raw_content=raw_content)

        elif return_format == "plain_dict":
            for line in raw_content.split("\n"):
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    returncontent[key] = value

        else:
            raise YunohostError("Expected value for return_format is either 'json' or 'plain_dict', got '%s'" % return_format)
    finally:
        stdreturndir = os.path.split(stdreturn)[0]
        os.remove(stdreturn)
        os.rmdir(stdreturndir)

    return returncode, returncontent


def _hook_exec_python(path, args, env, loggers):

    dir_ = os.path.dirname(path)
    name = os.path.splitext(os.path.basename(path))[0]

    if not dir_ in sys.path:
        sys.path = [dir_] + sys.path
    module = import_module(name)

    ret = module.main(args, env, loggers)
    # # Assert that the return is a (int, dict) tuple
    assert isinstance(ret, tuple) \
            and len(ret) == 2 \
            and isinstance(ret[0],int) \
            and isinstance(ret[1],dict), \
            "Module %s did not return a (int, dict) tuple !" % module
    return ret


def _extract_filename_parts(filename):
    """Extract hook parts from filename"""
    if '-' in filename:
        priority, action = filename.split('-', 1)
    else:
        priority = '50'
        action = filename

    # Remove extension if there's one
    action = os.path.splitext(action)[0]
    return priority, action


# Taken from Python 3 shlex module --------------------------------------------

_find_unsafe = re.compile(r'[^\w@%+=:,./-]', re.UNICODE).search


def shell_quote(s):
    """Return a shell-escaped version of the string *s*."""
    s = str(s)
    if not s:
        return "''"
    if _find_unsafe(s) is None:
        return s

    # use single quotes, and put single quotes into double quotes
    # the string $'b is then quoted as '$'"'"'b'
    return "'" + s.replace("'", "'\"'\"'") + "'"
