#!/usr/bin/env python
import re
import os
import sys
import time
import urllib
import urllib2
import json
import shutil
import string
import difflib
import logging
import optparse
import tempfile
import fileinput
import traceback
from virttest import common
from virttest import utils_libvirtd, utils_selinux
from virttest import data_dir
from virttest import virsh
from virttest.staging import service
from autotest.client import utils
from virttest.utils_misc import mount, umount
from autotest.client.tools import JUnit_api as api
from autotest.client.shared import error
from datetime import date


class Report():

    """
    This is a wrapper of autotest.client.tools.JUnit_api
    """

    class testcaseType(api.testcaseType):

        def __init__(self, classname=None, name=None, time=None, error=None,
                     failure=None, skip=None):
            api.testcaseType.__init__(self, classname, name, time, error,
                                      failure)
            self.skip = skip
            self.system_out = None
            self.system_err = None

        def exportChildren(self, outfile, level, namespace_='',
                           name_='testcaseType', fromsubclass_=False):
            api.testcaseType.exportChildren(
                self, outfile, level, namespace_, name_, fromsubclass_)
            if self.skip is not None:
                self.skip.export(outfile, level, namespace_, name_='skipped')
            if self.system_out is not None:
                outfile.write(
                    '<%ssystem-out><![CDATA[%s]]></%ssystem-out>\n' % (
                        namespace_,
                        self.system_out,
                        namespace_))
            if self.system_err is not None:
                outfile.write(
                    '<%ssystem-err><![CDATA[%s]]></%ssystem-err>\n' % (
                        namespace_,
                        self.system_err,
                        namespace_))

        def hasContent_(self):
            if (
                self.system_out is not None or
                self.system_err is not None or
                self.error is not None or
                self.failure is not None or
                self.skip is not None
            ):
                return True
            else:
                return False

    class failureType(api.failureType):

        def exportAttributes(self, outfile, level, already_processed, namespace_='', name_='failureType'):
            if self.message is not None and 'message' not in already_processed:
                already_processed.append('message')
                outfile.write(' message="%s"' % self.message)
            if self.type_ is not None and 'type_' not in already_processed:
                already_processed.append('type_')
                outfile.write(' type="%s"' % self.type_)

    class errorType(api.errorType):

        def exportAttributes(self, outfile, level, already_processed, namespace_='', name_='errorType'):
            if self.message is not None and 'message' not in already_processed:
                already_processed.append('message')
                outfile.write(' message="%s"' % self.message)
            if self.type_ is not None and 'type_' not in already_processed:
                already_processed.append('type_')
                outfile.write(' type="%s"' % self.type_)

    class skipType(api.failureType):
        pass

    class testsuite(api.testsuite):

        def __init__(self, name=None, skips=None):
            api.testsuite.__init__(self, name=name)
            self.skips = api._cast(int, skips)

        def exportAttributes(
                self, outfile, level, already_processed,
                namespace_='', name_='testsuite'):
            api.testsuite.exportAttributes(self,
                                           outfile, level, already_processed,
                                           namespace_, name_)
            if self.skips is not None and 'skips' not in already_processed:
                already_processed.append('skips')
                outfile.write(' skipped="%s"' %
                              self.gds_format_integer(self.skips,
                                                      input_name='skipped'))

    def __init__(self, fail_diff=False):
        self.ts_dict = {}
        self.fail_diff = fail_diff

    def save(self, filename):
        """
        Save current state of report to files.
        """
        testsuites = api.testsuites()
        for ts_name in self.ts_dict:
            ts = self.ts_dict[ts_name]
            testsuites.add_testsuite(ts)
        with open(filename, 'w') as fp:
            testsuites.export(fp, 0)

    def update(self, testname, ts_name, result, log, error_msg, duration):
        """
        Insert a new item into report.
        """
        def escape_str(inStr):
            """
            Escape a string for HTML use.
            """
            s1 = (isinstance(inStr, basestring) and inStr or
                  '%s' % inStr)
            s1 = s1.replace('&', '&amp;')
            s1 = s1.replace('<', '&lt;')
            s1 = s1.replace('>', '&gt;')
            s1 = s1.replace('"', "&quot;")
            return s1

        if ts_name not in self.ts_dict:
            self.ts_dict[ts_name] = self.testsuite(name=ts_name)
            ts = self.ts_dict[ts_name]
            ts.failures = 0
            ts.skips = 0
            ts.tests = 0
            ts.errors = 0
        else:
            ts = self.ts_dict[ts_name]

        tc = self.testcaseType()
        tc.name = testname
        tc.time = duration

        # Filter non-printable characters in log
        log = ''.join(s for s in unicode(log, errors='ignore')
                      if s in string.printable)
        tc.system_out = log

        tmp_msg = []
        for line in error_msg:
            # Filter non-printable characters in error message
            line = ''.join(s for s in unicode(line, errors='ignore')
                           if s in string.printable)
            tmp_msg.append(escape_str(line))
        error_msg = tmp_msg


        if 'FAIL' in result:
            error_msg.insert(0, 'Test %s has failed' % testname)
            tc.failure = self.failureType(
                message='&#10;'.join(error_msg),
                type_='Failure')
            ts.failures += 1
        elif 'TIMEOUT' in result:
            error_msg.insert(0, 'Test %s has timed out' % testname)
            tc.failure = self.failureType(
                message='&#10;'.join(error_msg),
                type_='Timeout')
            ts.failures += 1
        elif 'ERROR' in result or 'INVALID' in result:
            error_msg.insert(0, 'Test %s has encountered error' % testname)
            tc.error = self.errorType(
                message='&#10;'.join(error_msg),
                type_='Error')
            ts.errors += 1
        elif 'SKIP' in result:
            error_msg.insert(0, 'Test %s is skipped' % testname)
            tc.skip = self.skipType(
                message='&#10;'.join(error_msg),
                type_='Skip')
            ts.skips += 1
        elif 'DIFF' in result and self.fail_diff:
            error_msg.insert(0, 'Test %s results dirty environment' % testname)
            tc.failure = self.failureType(
                message='&#10;'.join(error_msg),
                type_='DIFF')
            ts.failures += 1
        ts.add_testcase(tc)
        ts.tests += 1
        ts.timestamp = date.isoformat(date.today())


class State():
    permit_keys = []
    permit_re = []

    def get_names(self):
        raise NotImplementedError('Function get_names not implemented for %s.'
                                  % self.__class__.__name__)

    def get_info(self, name):
        raise NotImplementedError('Function get_info not implemented for %s.'
                                  % self.__class__.__name__)

    def remove(self, name):
        raise NotImplementedError('Function remove not implemented for %s.'
                                  % self.__class__.__name__)

    def restore(self, name):
        raise NotImplementedError('Function restore not implemented for %s.'
                                  % self.__class__.__name__)

    def get_state(self):
        names = self.get_names()
        state = {}
        for name in names:
            state[name] = self.get_info(name)
        return state

    def backup(self):
        """
        Backup current state
        """
        self.backup_state = self.get_state()

    def check(self, recover=False):
        """
        Check state changes and recover to specified state.
        Return a result.
        """
        def diff_dict(dict_old, dict_new):
            created = set(dict_new) - set(dict_old)
            deleted = set(dict_old) - set(dict_new)
            shared = set(dict_old) & set(dict_new)
            return created, deleted, shared

        def lines_permitable(diff, permit_re):
            """
            Check whether the diff message is in permitable list of regexs.
            """
            diff_lines = set()
            for line in diff[2:]:
                if re.match(r'^[-+].*', line):
                    diff_lines.add(line)

            for line in diff_lines:
                permit = False
                for r in permit_re:
                    if re.match(r, line):
                        permit = True
                        break
                if not permit:
                    return False
            return True

        self.current_state = self.get_state()
        diff_msg = []
        new_items, del_items, unchanged_items = diff_dict(
            self.backup_state, self.current_state)
        if new_items:
            diff_msg.append('Created %s(s):' % self.name)
            for item in new_items:
                diff_msg.append(item)
                if recover:
                    try:
                        self.remove(self.current_state[item])
                    except Exception, e:
                        traceback.print_exc()
                        diff_msg.append('Remove is failed:\n %s' % e)

        if del_items:
            diff_msg.append('Deleted %s(s):' % self.name)
            for item in del_items:
                diff_msg.append(item)
                if recover:
                    try:
                        self.restore(self.backup_state[item])
                    except Exception, e:
                        traceback.print_exc()
                        diff_msg.append('Recover is failed:\n %s' % e)

        for item in unchanged_items:
            cur = self.current_state[item]
            bak = self.backup_state[item]
            item_changed = False
            new_keys, del_keys, unchanged_keys = diff_dict(bak, cur)
            if new_keys:
                item_changed = True
                diff_msg.append('Created key(s) in %s %s:' % (self.name, item))
                for key in new_keys:
                    diff_msg.append(key)
            if del_keys:
                for key in del_keys:
                    if type(key) is str:
                        if key not in self.permit_keys:
                            item_changed = True
                            diff_msg.append('Deleted key(s) in %s %s:' % (self.name, item))
                    else:
                        item_changed = True
                        diff_msg.append('Deleted key(s) in %s %s:' % (self.name, item))
            for key in unchanged_keys:
                if type(cur[key]) is str:
                    if key not in self.permit_keys and cur[key] != bak[key]:
                        item_changed = True
                        diff_msg.append('%s %s: %s changed: %s -> %s' % (
                            self.name, item, key, bak[key], cur[key]))
                elif type(cur[key]) is list:
                    diff = difflib.unified_diff(
                        bak[key], cur[key], lineterm="")
                    tmp_msg = []
                    for line in diff:
                        tmp_msg.append(line)
                    if tmp_msg and not lines_permitable(tmp_msg,
                                                        self.permit_re):
                        item_changed = True
                        diff_msg.append('%s %s: "%s" changed:' %
                                        (self.name, item, key))
                        diff_msg += tmp_msg
                else:
                    diff_msg.append('%s %s: %s: Invalid type %s.' % (
                        self.name, item, key, type(cur[key])))
            if item_changed and recover:
                try:
                    self.restore(self.backup_state[item])
                except Exception, e:
                    traceback.print_exc()
                    diff_msg.append('Recover is failed:\n %s' % e)
        return diff_msg


class DomainState(State):
    name = 'domain'
    permit_keys = ['id', 'cpu time', 'security label']

    def remove(self, name):
        dom = name
        if dom['state'] != 'shut off':
            res = virsh.destroy(dom['name'])
            if res.exit_status:
                raise Exception(str(res))
        if dom['persistent'] == 'yes':
            # Make sure the domain is remove anyway
            res = virsh.undefine(
                dom['name'], options='--snapshots-metadata --managed-save')
            if res.exit_status:
                raise Exception(str(res))

    def restore(self, name):
        dom = name
        name = dom['name']
        doms = self.current_state
        if name in doms:
            self.remove(doms[name])

        domfile = tempfile.NamedTemporaryFile(delete=False)
        fname = domfile.name
        domfile.writelines(dom['inactive xml'])
        domfile.close()

        try:
            if dom['persistent'] == 'yes':
                res = virsh.define(fname)
                if res.exit_status:
                    raise Exception(str(res))
                if dom['state'] != 'shut off':
                    res = virsh.start(name)
                    if res.exit_status:
                        raise Exception(str(res))
            else:
                res = virsh.create(fname)
                if res.exit_status:
                    raise Exception(str(res))
        finally:
            os.remove(fname)

        if dom['autostart'] == 'enable':
            res = virsh.autostart(name, '')
            if res.exit_status:
                raise Exception(str(res))

    def get_info(self, name):
        infos = {}
        for line in virsh.dominfo(name).stdout.strip().splitlines():
            key, value = line.split(':', 1)
            infos[key.lower()] = value.strip()
        infos['inactive xml'] = virsh.dumpxml(
            name, extra='--inactive').stdout.splitlines()
        return infos

    def get_names(self):
        return virsh.dom_list(options='--all --name').stdout.splitlines()


class NetworkState(State):
    name = 'network'

    def remove(self, name):
        """
        Remove target network _net_.

        :param net: Target net to be removed.
        """
        net = name
        if net['active'] == 'yes':
            res = virsh.net_destroy(net['name'])
            if res.exit_status:
                raise Exception(str(res))
        if net['persistent'] == 'yes':
            res = virsh.net_undefine(net['name'])
            if res.exit_status:
                raise Exception(str(res))

    def restore(self, name):
        """
        Restore networks from _net_.

        :param net: Target net to be restored.
        :raise CalledProcessError: when restore failed.
        """
        net = name
        name = net['name']
        nets = self.current_state
        if name in nets:
            self.remove(nets[name])

        netfile = tempfile.NamedTemporaryFile(delete=False)
        fname = netfile.name
        netfile.writelines(net['inactive xml'])
        netfile.close()

        try:
            if net['persistent'] == 'yes':
                res = virsh.net_define(fname)
                if res.exit_status:
                    raise Exception(str(res))
                if net['active'] == 'yes':
                    res = virsh.net_start(name)
                    if res.exit_status:
                        res = virsh.net_start(name)
                        if res.exit_status:
                            raise Exception(str(res))
            else:
                res = virsh.net_create(fname)
                if res.exit_status:
                    raise Exception(str(res))
        finally:
            os.remove(fname)

        if net['autostart'] == 'yes':
            res = virsh.net_autostart(name)
            if res.exit_status:
                raise Exception(str(res))

    def get_info(self, name):
        infos = {}
        for line in virsh.net_info(name).stdout.strip().splitlines():
            key, value = line.split()
            if key.endswith(':'):
                key = key[:-1]
            infos[key.lower()] = value.strip()
        infos['inactive xml'] = virsh.net_dumpxml(
            name, '--inactive').stdout.splitlines()
        return infos

    def get_names(self):
        lines = virsh.net_list('--all').stdout.strip().splitlines()[2:]
        return [line.split()[0] for line in lines]


class PoolState(State):
    name = 'pool'
    permit_keys = ['available', 'allocation']
    permit_re = [r'^[-+]\s*\<(capacity|allocation|available).*$']

    def remove(self, name):
        """
        Remove target pool _pool_.

        :param pool: Target pool to be removed.
        """
        pool = name
        if pool['state'] == 'running':
            res = virsh.pool_destroy(pool['name'])
            if not res:
                raise Exception(str(res))
        if pool['persistent'] == 'yes':
            res = virsh.pool_undefine(pool['name'])
            if res.exit_status:
                raise Exception(str(res))

    def restore(self, name):
        pool = name
        name = pool['name']
        pools = self.current_state
        if name in pools:
            self.remove(pools[name])

        pool_file = tempfile.NamedTemporaryFile(delete=False)
        fname = pool_file.name
        pool_file.writelines(pool['inactive xml'])
        pool_file.close()

        try:
            if pool['persistent'] == 'yes':
                res = virsh.pool_define(fname)
                if res.exit_status:
                    raise Exception(str(res))
                if pool['state'] == 'running':
                    res = virsh.pool_start(name)
                    if res.exit_status:
                        raise Exception(str(res))
            else:
                res = virsh.pool_create(fname)
                if res.exit_status:
                    raise Exception(str(res))
        except Exception, e:
            raise e
        finally:
            os.remove(fname)

        if pool['autostart'] == 'yes':
            res = virsh.pool_autostart(name)
            if res.exit_status:
                raise Exception(str(res))

    def get_info(self, name):
        infos = {}
        for line in virsh.pool_info(name).stdout.strip().splitlines():
            key, value = line.split(':', 1)
            infos[key.lower()] = value.strip()
        infos['inactive xml'] = virsh.pool_dumpxml(
            name, '--inactive').splitlines()
        infos['volumes'] = virsh.vol_list(name).stdout.strip().splitlines()[2:]
        return infos

    def get_names(self):
        lines = virsh.pool_list('--all').stdout.strip().splitlines()[2:]
        return [line.split()[0] for line in lines]


class SecretState(State):
    name = 'secret'
    permit_keys = []
    permit_re = []

    def remove(self, name):
        secret = name
        res = virsh.secret_undefine(secret['uuid'])
        if res.exit_status:
            raise Exception(str(res))

    def restore(self, name):
        uuid = name
        cur = self.current_state
        bak = self.backup_state

        if uuid in cur:
            self.remove(name)

        secret_file = tempfile.NamedTemporaryFile(delete=False)
        fname = secret_file.name
        secret_file.writelines(bak[name]['xml'])
        secret_file.close()

        try:
            res = virsh.secret_define(fname)
            if res.exit_status:
                raise Exception(str(res))
        except Exception, e:
            raise e
        finally:
            os.remove(fname)

    def get_info(self, name):
        infos = {}
        infos['uuid'] = name
        infos['xml'] = virsh.secret_dumpxml(name).stdout.splitlines()
        return infos

    def get_names(self):
        lines = virsh.secret_list().stdout.strip().splitlines()[2:]
        return [line.split()[0] for line in lines]


class MountState(State):
    name = 'mount'
    permit_keys = []
    permit_re = []
    info = {}

    def remove(self, name):
        info = name
        # ugly workaround for nfs which unable to umount
        #os.system('systemctl restart nfs')
        if not umount(info['src'], info['mount_point'], info['fstype'],
                      verbose=False):
            raise Exception("Failed to unmount %s" % info['mount_point'])

    def restore(self, name):
        info = name
        if not mount(info['src'], info['mount_point'], info['fstype'],
                     info['options'], verbose=False):
            raise Exception("Failed to mount %s" % info['mount_point'])

    def get_info(self, name):
        return self.info[name]

    def get_names(self):
        """
        Get all mount infomations from /etc/mtab.

        :return: A dict using mount point as keys and 6-element dict as value.
        """
        lines = file('/etc/mtab').read().splitlines()
        names = []
        for line in lines:
            values = line.split()
            if len(values) != 6:
                print 'Warning: Error parsing mountpoint: %s' % line
                continue
            keys = ['src', 'mount_point', 'fstype', 'options', 'dump', 'order']
            mount_entry = dict(zip(keys, values))
            mount_point = mount_entry['mount_point']
            names.append(mount_point)
            self.info[mount_point] = mount_entry
        return names


class ServiceState(State):
    name = 'service'
    libvirtd = utils_libvirtd.Libvirtd()
    permit_keys = []
    permit_re = []

    def remove(self, name):
        raise Exception('It is meaningless to remove service %s' % name)

    def restore(self, name):
        info = name
        if info['name'] == 'libvirtd':
            if info['status'] == 'running':
                if not self.libvirtd.start():
                    raise Exception('Failed to start libvirtd')
            elif info['status'] == 'stopped':
                if not self.libvirtd.stop():
                    raise Exception('Failed to stop libvirtd')
            else:
                raise Exception('Unknown libvirtd status %s' % info['status'])
        elif info['name'] == 'selinux':
            utils_selinux.set_status(info['status'])
        else:
            raise Exception('Unknown service %s' % info['name'])

    def get_info(self, name):
        if name == 'libvirtd':
            if self.libvirtd.is_running():
                status = 'running'
            else:
                status = 'stopped'
        if name == 'selinux':
            status = utils_selinux.get_status()
        return {'name': name, 'status': status}

    def get_names(self):
        return ['libvirtd', 'selinux']


class DirState(State):
    name = 'directory'
    permit_keys = ['aexpect']
    permit_re = []

    def remove(self, name):
        raise Exception('It is not wise to remove a dir %s' % name)

    def restore(self, name):
        dirname = name['dir-name']
        cur = self.current_state[dirname]
        bak = self.backup_state[dirname]
        created_files = set(cur) - set(bak)
        if created_files:
            for fname in created_files:
                fpath = os.path.join(name['dir-name'], fname)
                if os.path.isfile(fpath):
                    os.remove(fpath)
                elif os.path.isdir(fpath):
                    shutil.rmtree(fpath)
        deleted_files = set(bak) - set(cur)
        if deleted_files:
            for fname in deleted_files:
                fpath = os.path.join(name['dir-name'], fname)
                open(fpath, 'a').close()
        # TODO: record file/dir info and recover them separately

    def get_info(self, name):
        infos = {}
        infos['dir-name'] = name
        for f in os.listdir(name):
            infos[f] = f
        return infos

    def get_names(self):
        return ['/tmp',
                data_dir.get_tmp_dir(),
                os.path.join(data_dir.get_root_dir(), 'shared'),
                os.path.join(data_dir.get_data_dir(), 'images'),
                '/var/lib/libvirt/images']


class FileState(State):
    name = 'file'
    permit_keys = []
    permit_re = []

    def remove(self, name):
        raise Exception('It is not wise to remove a system file %s' % name)

    def restore(self, name):
        file_path = name['file-path']
        cur = self.current_state[file_path]
        bak = self.backup_state[file_path]
        if cur['content'] != bak['content']:
            with open(file_path, 'w') as f:
                f.write(bak['content'])

    def get_info(self, name):
        infos = {}
        infos['file-path'] = name
        with open(name) as f:
            infos['content'] = f.read()
        return infos

    def get_names(self):
        return ['/etc/exports',
                '/etc/libvirt/libvirtd.conf',
                '/etc/libvirt/qemu.conf']


class LibvirtCI():

    def parse_args(self):
        parser = optparse.OptionParser(
            description='Continuouse integration of '
            'virt-test libvirt test provider.')
        parser.add_option('--list', dest='list', action='store_true',
                          help='List all the test names')
        parser.add_option('--no', dest='no', action='store', default='',
                          help='Exclude specified tests.')
        parser.add_option('--only', dest='only', action='store', default='',
                          help='Run only for specified tests.')
        parser.add_option('--no-check', dest='no_check', action='store_true',
                          help='Disable checking state changes after each test.')
        parser.add_option('--no-recover', dest='no_recover', action='store_true',
                          help='Disable recover state changes after each test.')
        parser.add_option('--connect-uri', dest='connect_uri', action='store',
                          default='', help='Run tests using specified uri.')
        parser.add_option('--additional-vms', dest='add_vms', action='store',
                          default='', help='Additional VMs for testing')
        parser.add_option('--smoke', dest='smoke', action='store_true',
                          help='Run one test for each script.')
        parser.add_option('--slice', dest='slice', action='store',
                          default='', help='Specify a URL to slice tests.')
        parser.add_option('--report', dest='report', action='store',
                          default='xunit_result.xml',
                          help='Exclude specified tests.')
        parser.add_option('--white', dest='whitelist', action='store',
                          default='', help='Whitelist file contains '
                          'specified test cases to run.')
        parser.add_option('--black', dest='blacklist', action='store',
                          default='', help='Blacklist file contains '
                          'specified test cases to be excluded.')
        parser.add_option('--config', dest='config', action='store',
                          default='', help='Specify a custom Cartesian cfg '
                          'file')
        parser.add_option('--img-url', dest='img_url', action='store',
                          default='', help='Specify a URL to a custom image '
                          'file')
        parser.add_option('--os-variant', dest='os_variant', action='store',
                          default='', help='Specify the --os-variant option '
                          'when doing virt-install.')
        parser.add_option('--password', dest='password', action='store',
                          default='', help='Specify a password for logging '
                          'into guest')
        parser.add_option('--pull-virt-test', dest='virt_test_pull',
                          action='store', default='',
                          help='Merge specified virt-test pull requests. '
                          'Multiple pull requests are separated by ",", '
                          'example: --pull-virt-test 175,183')
        parser.add_option('--pull-libvirt', dest='libvirt_pull',
                          action='store', default='',
                          help='Merge specified tp-libvirt pull requests. '
                          'Multiple pull requests are separated by ",", '
                          'example: --pull-libvirt 175,183')
        parser.add_option('--with-dependence', dest='with_dependence',
                          action='store_true',
                          help='Merge virt-test pull requests depend on')
        parser.add_option('--no-restore-pull', dest='no_restore_pull',
                          action='store_true', help='Do not restore repo '
                          'to branch master after test.')
        parser.add_option('--only-change', dest='only_change',
                          action='store_true', help='Only test tp-libvirt '
                          'test cases related to changed files.')
        parser.add_option('--fail-diff', dest='fail_diff',
                          action='store_true', help='Report tests who do '
                          'not clean up environment as a failure')
        parser.add_option('--retain-vm', dest='retain_vm',
                          action='store_true', help='Do not reinstall VM '
                          'before tests')
        parser.add_option('--pre-cmd', dest='pre_cmd',
                          action='store', help='Run a command line after '
                          'fetch the source code and before running the test.')
        parser.add_option('--post-cmd', dest='post_cmd',
                          action='store', help='Run a command line after '
                          'running the test')
        parser.add_option('--timeout', dest='timeout',
                          action='store', default='1200',
                          help='Maximum run time for one test case')
        self.args, self.real_args = parser.parse_args()

    def prepare_tests(self, whitelist='whitelist.test',
                      blacklist='blacklist.test'):
        """
        Get all tests to be run.

        When a whitelist is given, only tests in whitelist will be run.
        When a blacklist is given, tests in blacklist will be excluded.
        """
        def read_tests_from_file(file_name):
            """
            Read tests from a file
            """
            try:
                tests = []
                with open(file_name) as fp:
                    for line in fp:
                        if not line.strip().startswith('#'):
                            tests.append(line.strip())
                return tests
            except IOError:
                return None

        def get_all_tests():
            """
            Get all libvirt tests.
            """
            if type(self.onlys) == set and not self.onlys:
                return []

            cmd = './run -t libvirt --list-tests'
            if self.args.connect_uri:
                cmd += ' --connect-uri %s' % self.args.connect_uri
            if self.nos:
                cmd += ' --no %s' % ','.join(self.nos)
            if self.onlys:
                cmd += ' --tests %s' % ','.join(self.onlys)
            if self.args.config:
                cmd += ' -c %s' % self.args.config
            res = utils.run(cmd)
            out, err, exitcode = res.stdout, res.stderr, res.exit_status
            tests = []
            class_names = set()
            for line in out.splitlines():
                if line:
                    if line[0].isdigit():
                        test = re.sub(r'^[0-9]+ (.*) \(requires root\)$',
                                      r'\1', line)
                        if self.args.smoke:
                            class_name, _ = self.split_name(test)
                            if class_name in class_names:
                                continue
                            else:
                                class_names.add(class_name)
                        tests.append(test)
            return tests

        def change_to_only(change_list):
            """
            Transform the content of a change file to a only set.
            """
            onlys = set()
            for line in change_list:
                filename = line.strip()
                res = re.match('libvirt/tests/(cfg|src)/(.*).(cfg|py)',
                               filename)
                if res:
                    cfg_path = 'libvirt/tests/cfg/%s.cfg' % res.groups()[1]
                    tp_dir = data_dir.get_test_provider_dir(
                        'io-github-autotest-libvirt')
                    cfg_path = os.path.join(tp_dir, cfg_path)
                    try:
                        with open(cfg_path) as fcfg:
                            only = fcfg.readline().strip()
                            only = only.lstrip('-').rstrip(':').strip()
                            onlys.add(only)
                    except:
                        pass
            return onlys

        self.nos = set(['io-github-autotest-qemu'])
        self.onlys = set()

        if self.args.only:
            self.onlys = set(self.args.only.split(','))

        if self.args.slice:
            slices = {}
            slice_opts = self.args.slice.split(',')
            slice_url = slice_opts[0]
            slice_opts = slice_opts[1:]
            config = urllib2.urlopen(slice_url)
            for line in config:
                key, val = line.split()
                slices[key] = val
            for slice_opt in slice_opts:
                if slice_opt in slices:
                    if self.onlys is None:
                        self.onlys = set(slices[slice_opt].split(','))
                    else:
                        self.onlys |= set(slices[slice_opt].split(','))
                elif slice_opt == 'other':
                    for key in slices:
                        self.nos |= set(slices[key].split(','))

        if self.args.no:
            self.nos |= set(self.args.no.split(','))
        if self.args.only_change:
            if self.onlys is not None:
                self.onlys &= change_to_only(self.libvirt_file_changed)
            else:
                self.onlys = change_to_only(self.libvirt_file_changed)

        if self.args.whitelist:
            tests = read_tests_from_file(whitelist)
        else:
            tests = get_all_tests()

        if self.args.blacklist:
            black_tests = read_tests_from_file(blacklist)
            tests = [t for t in tests if t not in black_tests]

        with open('run.test', 'w') as fp:
            for test in tests:
                fp.write(test + '\n')
        return tests

    def split_name(self, name):
        """
        Try to return the module name of a test.
        """
        if name.startswith('type_specific.io-github-autotest-libvirt'):
            name = name.split('.', 2)[2]

        if name.split('.')[0] in ['virsh']:
            package_name, name = name.split('.', 1)
        else:
            package_name = ""

        names = name.split('.', 1)
        if len(names) == 2:
            name, test_name = names
        else:
            name = names[0]
            test_name = name
        if package_name:
            class_name = '.'.join((package_name, name))
        else:
            class_name = name

        return class_name, test_name

    def bootstrap(self):
        class _Options(object):
            pass

        from virttest import bootstrap

        logging.info('Bootstrapping')
        sys.stdout.flush()
        base_dir = data_dir.get_data_dir()
        if os.path.exists(base_dir):
            if os.path.islink(base_dir) or os.path.isfile(base_dir):
                os.unlink(base_dir)
            elif os.path.isdir(base_dir):
                shutil.rmtree(base_dir)
        os.mkdir(base_dir)

        options = _Options()
        options.vt_type = 'libvirt'
        options.vt_selinux_setup = True
        options.vt_no_downloads = True
        options.vt_keep_image = True
        options.vt_verbose = True
        options.vt_update_providers = False
        options.vt_update_config = True
        options.vt_guest_os = None
        options.vt_config = None

        bootstrap.bootstrap(options=options, interactive=False)
        os.chdir(data_dir.get_root_dir())

    def prepare_env(self):
        """
        Prepare the environment before all tests.
        """

        def replace_pattern_in_file(file, search_exp, replace_exp):
            prog = re.compile(search_exp)
            for line in fileinput.input(file, inplace=1):
                match = prog.search(line)
                if match:
                    line = prog.sub(replace_exp, line)
                sys.stdout.write(line)

        utils_libvirtd.Libvirtd().restart()
        service.Factory.create_service("nfs").restart()

        if self.args.password:
            replace_pattern_in_file(
                "shared/cfg/guest-os/Linux.cfg",
                r'password = \S*',
                r'password = %s' % self.args.password)

        if self.args.os_variant:
            replace_pattern_in_file(
                "shared/cfg/guest-os/Linux/JeOS/19.x86_64.cfg",
                r'os_variant = \S*',
                r'os_variant = %s' % self.args.os_variant)

        if self.args.add_vms:
            vms_string = "virt-tests-vm1 " + " ".join(self.args.add_vms.split(','))
            replace_pattern_in_file(
                "shared/cfg/base.cfg",
                r'^\s*vms = .*\n',
                r'vms = %s\n' % vms_string)

        print 'Running bootstrap'
        sys.stdout.flush()
        self.bootstrap()

        restore_image = True
        if self.args.img_url:
            def progress_callback(count, block_size, total_size):
                #percent = count * block_size * 100 / total_size
                #sys.stdout.write("\rDownloaded %2.2f%%" % percent)
                #sys.stdout.flush()
                pass
            print 'Downloading image from %s.' % self.args.img_url
            sys.stdout.flush()
            img_dir = os.path.join(
                os.path.realpath(data_dir.get_data_dir()), 'images/jeos-19-64.qcow2')
            urllib.urlretrieve(self.args.img_url, img_dir, progress_callback)
            restore_image = False

        if self.args.retain_vm:
            return

        print 'Removing VM\n',  # TODO: use virt-test api remove VM
        sys.stdout.flush()
        if self.args.connect_uri:
            virsh.destroy('virt-tests-vm1',
                          ignore_status=True,
                          uri=self.args.connect_uri)
            virsh.undefine('virt-tests-vm1',
                           '--snapshots-metadata --managed-save',
                           ignore_status=True,
                           uri=self.args.connect_uri)
        else:
            virsh.destroy('virt-tests-vm1', ignore_status=True)
            virsh.undefine('virt-tests-vm1', '--snapshots-metadata', ignore_status=True)
        if self.args.add_vms:
            for vm in self.args.add_vms.split(','):
                virsh.destroy(vm, ignore_status=True)
                virsh.undefine(vm, '--snapshots-metadata', ignore_status=True)

        print 'Installing VM',
        sys.stdout.flush()
        if 'lxc' in self.args.connect_uri:
            cmd = 'virt-install --connect=lxc:/// --name virt-tests-vm1 --ram 500 --noautoconsole'
            try:
                utils.run(cmd)
            except error.CmdError, e:
                raise Exception('   ERROR: Failed to install guest \n %s' % e)
        else:
            status, res, err_msg = self.run_test(
                'unattended_install.import.import.default_install.aio_native',
                restore_image=restore_image, check=False, recover=False)
            if 'PASS' not in status:
                raise Exception('   ERROR: Failed to install guest \n %s' %
                                res.stderr)
            virsh.destroy('virt-tests-vm1')
        if self.args.add_vms:
            for vm in self.args.add_vms.split(','):
                cmd = 'virt-clone '
                if self.args.connect_uri:
                    cmd += '--connect=%s ' % self.args.connect_uri
                cmd += '--original=virt-tests-vm1 '
                cmd += '--name=%s ' % vm
                cmd += '--auto-clone'
                utils.run(cmd)

    def run_test(self, test, restore_image=False, check=True, recover=True):
        """
        Run a specific test.
        """
        img_str = '' if restore_image else 'k'
        down_str = '' if restore_image else '--no-downloads'
        cmd = './run -v%st libvirt --keep-image-between-tests %s --tests %s' % (
            img_str, down_str, test)
        if self.args.connect_uri:
            cmd += ' --connect-uri %s' % self.args.connect_uri
        status = 'INVALID'
        try:
            res = utils.run(cmd, timeout=int(self.args.timeout),
                            ignore_status=True)
            lines = res.stdout.splitlines()
            for line in lines:
                if line.startswith('(1/1)'):
                    status = line.split()[2]
        except error.CmdError, e:
            res = e.result_obj
            status = 'TIMEOUT'
            res.duration = int(self.args.timeout)
        except Exception, e:
            print "Exception when parsing stdout.\n%s" % res
            raise e

        os.chdir(data_dir.get_root_dir())  # Check PWD

        err_msg = []

        if check:
            diff = False
            for state in self.states:
                diffmsg = state.check(recover=recover)
                if diffmsg:
                    if not diff:
                        diff = True
                        status += ' DIFF'
                    for line in diffmsg:
                        err_msg.append('   DIFF|%s' % line)

        print 'Result: %s %.2f s' % (status, res.duration)

        if 'FAIL' in status or 'ERROR' in status:
            for line in res.stderr.splitlines():
                if 'ERROR' in line:
                    err_msg.append('  %s' % line[9:])
        if status == 'INVALID' or status == 'TIMEOUT':
            for line in res.stdout.splitlines():
                err_msg.append(line)
        if err_msg:
            for line in err_msg:
                print line
        sys.stdout.flush()
        return status, res, err_msg

    def prepare_repos(self):
        """
        Prepare repos for the tests.
        """
        def merge_pulls(repo_name, pull_nos):
            branch_name = ','.join(pull_nos)
            cmd = 'git checkout -b %s' % branch_name
            res = utils.run(cmd, ignore_status=True)
            if res.exit_status:
                print res
                raise Exception('Failed to create branch %s' % branch_name)

            for pull_no in pull_nos:
                if pr_open(repo_name, pull_no):
                    patch_url = ('https://github.com/autotest'
                                 '/%s/pull/%s.patch' % (repo_name, pull_no))
                    patch_file = "/tmp/%s.patch" % pull_no
                    with open(patch_file, 'w') as pf:
                        pf.write(urllib2.urlopen(patch_url).read())
                    with open(patch_file, 'r') as pf:
                        if not pf.read().strip():
                            print 'WARING: empty content for PR #%s' % pull_no
                    try:
                        print 'Patching %s PR #%s' % (repo_name, pull_no)
                        cmd = 'git am -3 %s' % patch_file
                        res = utils.run(cmd)
                    except error.CmdError, e:
                        print e
                        raise Exception('Failed applying patch %s.' % pull_no)
                    finally:
                        os.remove(patch_file)
            return branch_name

        def file_changed(repo_name):
            cmd = 'git diff master --name-only'
            res = utils.run(cmd, ignore_status=True)
            if res.exit_status:
                print res
                raise Exception("Failed to get diff info against master")

            return res.stdout.strip().splitlines()

        def search_dep(line):
            pattern1 = r'autotest/virt-test#([0-9]*)'
            pattern2 = (r'https?://github.com/autotest/virt-test/(?:pull|issues)/([0-9]*)')
            res = set()
            match = re.findall(pattern1, line)
            res |= set(match)
            match = re.findall(pattern2, line)
            res |= set(match)
            return res

        def pr_open(repo_name, pr_number):
            oauth = ('?client_id=b6578298435c3eaa1e3d&client_secret'
                     '=59a1c828c6002ed4e8a9205486cf3fa86467a609')
            issues_url = 'https://api.github.com/repos/autotest/%s/issues/' % repo_name
            issue_url = issues_url + pr_number + oauth
            issue_u = urllib2.urlopen(issue_url)
            issue = json.load(issue_u)
            return issue['state'] == 'open'

        def libvirt_pr_dep(pr_numbers):
            oauth = ('?client_id=b6578298435c3eaa1e3d&client_secret'
                     '=59a1c828c6002ed4e8a9205486cf3fa86467a609')
            dep = set()
            for pr_number in pr_numbers:
                # Find PR's first comment for dependencies.
                issues_url = 'https://api.github.com/repos/autotest/tp-libvirt/issues/'
                issue_url = issues_url + pr_number + oauth
                issue_u = urllib2.urlopen(issue_url)
                issue = json.load(issue_u)
                for line in issue['body'].splitlines():
                    dep |= search_dep(line)

                # Find PR's other comments for dependencies.
                comments_url = issues_url + '%s/comments' % pr_number + oauth
                comments_u = urllib2.urlopen(comments_url)
                comments = json.load(comments_u)
                for comment in comments:
                    for line in comment['body'].splitlines():
                        dep |= search_dep(line)

            # Remove closed dependences:
            pruned_dep = set()
            for pr_number in dep:
                if pr_open('virt-test', pr_number):
                    pruned_dep.add(pr_number)

            return pruned_dep

        self.virt_branch_name, self.libvirt_branch_name = None, None

        libvirt_pulls = set()
        virt_test_pulls = set()

        if self.args.libvirt_pull:
            libvirt_pulls = set(self.args.libvirt_pull.split(','))

        if self.args.with_dependence:
            virt_test_pulls = libvirt_pr_dep(libvirt_pulls)

        if self.args.virt_test_pull:
            virt_test_pulls |= set(self.args.virt_test_pull.split(','))

        if virt_test_pulls:
            os.chdir(data_dir.get_root_dir())
            self.virt_branch_name = merge_pulls("virt-test", virt_test_pulls)
            if self.args.only_change:
                self.virt_file_changed = file_changed("virt-test")

        if libvirt_pulls:
            os.chdir(data_dir.get_test_provider_dir(
                'io-github-autotest-libvirt'))
            self.libvirt_branch_name = merge_pulls("tp-libvirt", libvirt_pulls)
            if self.args.only_change:
                self.libvirt_file_changed = file_changed("tp-libvirt")

        os.chdir(data_dir.get_root_dir())

    def restore_repos(self):
        """
        Checkout master branch and remove test branch.
        """
        def restore_repo(branch_name):
            cmd = 'git checkout master'
            res = utils.run(cmd, ignore_status=True)
            if res.exit_status:
                print res
            cmd = 'git branch -D %s' % branch_name
            res = utils.run(cmd, ignore_status=True)
            if res.exit_status:
                print res

        if self.virt_branch_name:
            os.chdir(data_dir.get_root_dir())
            restore_repo(self.virt_branch_name)

        if self.libvirt_branch_name:
            os.chdir(data_dir.get_test_provider_dir(
                'io-github-autotest-libvirt'))
            restore_repo(self.libvirt_branch_name)
        os.chdir(data_dir.get_root_dir())

    def prepare_test(self, test):
        """
        Action to perform before a test
        """
        from virttest import virsh
        res = virsh.dumpxml('virt-tests-vm1',
                            ignore_status=True,
                            uri=self.args.connect_uri)
        if not res.exit_status:
            domxml = res.stdout
            fname = '/var/lib/libvirt/qemu/nvram/virt-tests-vm1_VARS.fd'
            if not os.path.exists(fname) and fname in domxml:
                logging.warning(
                    'nvram in XML, but file %s do not exists. '
                    'Removing nvram line. XML:\n%s' % (fname, domxml))
                domxml = re.sub('<nvram>.*</nvram>', '', domxml)
                virsh.destroy('virt-tests-vm1',
                              ignore_status=True,
                              uri=self.args.connect_uri)
                virsh.undefine('virt-tests-vm1',
                               '--snapshots-metadata --managed-save',
                               ignore_status=True,
                               uri=self.args.connect_uri)

                xml_path = '/tmp/virt-test-ci.xml'
                with open(xml_path, 'w') as fp:
                    fp.write(domxml)
                res = virsh.define(xml_path)
                if res.exit_status:
                    logging.error('Define command result:\n%s', res)
                    raise Exception('Failed to define domain for XML:\n%s' % domxml)
                try:
                    os.remove(xml_path)
                except OSError:
                    pass
        else:
            logging.warning('Failed to dumpxml from virt-tests-vm1\n%s', res)

    def run(self):
        """
        Run continuous integrate for virt-test test cases.
        """
        self.parse_args()
        report = Report(self.args.fail_diff)
        try:
            self.prepare_repos()
            if self.args.pre_cmd:
                print 'Running command line "%s" before test.' % self.args.pre_cmd
                res = utils.run(self.args.pre_cmd, ignore_status=True)
                print 'Result:'
                for line in str(res).splitlines():
                    print line
            # service must put at first, or the result will be wrong.
            self.states = [FileState(), ServiceState(), DirState(),
                           DomainState(), NetworkState(), PoolState(),
                           SecretState(), MountState()]
            tests = self.prepare_tests()

            if self.args.list:
                for test in tests:
                    short_name = test.split('.', 2)[2]
                    print short_name
                exit(0)

            self.prepare_env()
            for state in self.states:
                state.backup()

            for idx, test in enumerate(tests):
                short_name = test.split('.', 2)[2]
                print '%s (%d/%d) %s ' % (time.strftime('%X'), idx + 1,
                                          len(tests), short_name),
                sys.stdout.flush()

                self.prepare_test(test)

                status, res, err_msg = self.run_test(
                    test,
                    check=not self.args.no_check,
                    recover=not self.args.no_recover)

                class_name, test_name = self.split_name(test)

                report.update(test_name, class_name, status,
                              res.stderr, err_msg, res.duration)
                report.save(self.args.report)
            if self.args.post_cmd:
                print 'Running command line "%s" after test.' % self.args.post_cmd
                res = utils.run(self.args.post_cmd, ignore_status=True)
                print 'Result:'
                for line in str(res).splitlines():
                    print line
        except Exception:
            traceback.print_exc()
        finally:
            if not self.args.no_restore_pull:
                self.restore_repos()
            report.save(self.args.report)


def state_test():
    states = [FileState(), ServiceState(), DirState(), DomainState(),
              NetworkState(), PoolState(), SecretState(), MountState()]
    for state in states:
        state.backup()
    utils.run('echo hello > /etc/exports')
    virsh.start('virt-tests-vm1')
    virsh.net_autostart('default', '--disable')
    virsh.pool_destroy('mount')
    utils.run('rm /var/lib/virt_test/images/hello')
    utils.run('mkdir /var/lib/virt_test/images/hi')
    utils_libvirtd.Libvirtd().stop()
    utils_selinux.set_status('permissive')
    for state in states:
        lines = state.check(recover=True)
        for line in lines:
            print line


if __name__ == '__main__':
    ci = LibvirtCI()
    ci.run()

# vi:set ts=4 sw=4 expandtab:
