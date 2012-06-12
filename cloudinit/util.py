# vi: ts=4 expandtab
#
#    Copyright (C) 2012 Canonical Ltd.
#    Copyright (C) 2012 Hewlett-Packard Development Company, L.P.
#    Copyright (C) 2012 Yahoo! Inc.
#
#    Author: Scott Moser <scott.moser@canonical.com>
#    Author: Juerg Haefliger <juerg.haefliger@hp.com>
#    Author: Joshua Harlow <harlowja@yahoo-inc.com>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License version 3, as
#    published by the Free Software Foundation.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

from StringIO import StringIO

import contextlib
import copy
import errno
import glob
import grp
import gzip
import os
import platform
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import urlparse

import yaml

from cloudinit import log as logging
from cloudinit import url_helper as uhelp

from cloudinit.settings import (CFG_BUILTIN, CLOUD_CONFIG)


try:
    import selinux
    HAVE_LIBSELINUX = True
except ImportError:
    HAVE_LIBSELINUX = False


LOG = logging.getLogger(__name__)

# Helps cleanup filenames to ensure they aren't FS incompatible
FN_REPLACEMENTS = {
    os.sep: '_',
}

# Helper utils to see if running in a container
CONTAINER_TESTS = ['running-in-container', 'lxc-is-container']


class ProcessExecutionError(IOError):

    MESSAGE_TMPL = ('%(description)s\nCommand: %(cmd)s\n'
                    'Exit code: %(exit_code)s\nStdout: %(stdout)r\n'
                    'Stderr: %(stderr)r')

    def __init__(self, stdout=None, stderr=None,
                 exit_code=None, cmd=None,
                 description=None, reason=None):
        if not cmd:
            self.cmd = '-'
        else:
            self.cmd = cmd

        if not description:
            self.description = 'Unexpected error while running command.'
        else:
            self.description = description

        if not isinstance(exit_code, (long, int)):
            self.exit_code = '-'
        else:
            self.exit_code = exit_code

        if not stderr:
            self.stderr = ''
        else:
            self.stderr = stderr

        if not stdout:
            self.stdout = ''
        else:
            self.stdout = stdout

        message = self.MESSAGE_TMPL % {
            'description': self.description,
            'cmd': self.cmd,
            'exit_code': self.exit_code,
            'stdout': self.stdout,
            'stderr': self.stderr,
        }
        IOError.__init__(self, message)
        self.reason = reason


class SeLinuxGuard(object):
    def __init__(self, path, recursive=False):
        self.path = path
        self.recursive = recursive
        self.engaged = False
        if HAVE_LIBSELINUX and selinux.is_selinux_enabled():
            self.engaged = True

    def __enter__(self):
        return self.engaged

    def __exit__(self, excp_type, excp_value, excp_traceback):
        if self.engaged:
            LOG.debug("Disengaging selinux mode for %s: %s",
                      self.path, self.recursive)
            selinux.restorecon(self.path, recursive=self.recursive)


class MountFailedError(Exception):
    pass


def translate_bool(val):
    if not val:
        return False
    if val is isinstance(val, bool):
        return val
    if str(val).lower().strip() in ['true', '1', 'on', 'yes']:
        return True
    return False


def read_conf(fname):
    try:
        return load_yaml(load_file(fname), default={})
    except IOError as e:
        if e.errno == errno.ENOENT:
            return {}
        else:
            raise


def clean_filename(fn):
    for (k, v) in FN_REPLACEMENTS.items():
        fn = fn.replace(k, v)
    return fn.strip()


def decomp_str(data):
    try:
        buf = StringIO(str(data))
        with contextlib.closing(gzip.GzipFile(None, "rb", 1, buf)) as gh:
            return gh.read()
    except:
        return data


def find_modules(root_dir):
    entries = dict()
    for fname in glob.glob(os.path.join(root_dir, "*.py")):
        if not os.path.isfile(fname):
            continue
        modname = os.path.basename(fname)[0:-3]
        modname = modname.strip()
        if modname and modname.find(".") == -1:
            entries[fname] = modname
    return entries


def is_ipv4(instr):
    """ determine if input string is a ipv4 address. return boolean"""
    toks = instr.split('.')
    if len(toks) != 4:
        return False

    try:
        toks = [x for x in toks if (int(x) < 256 and int(x) > 0)]
    except:
        return False

    return (len(toks) == 4)


def merge_base_cfg(cfgfile, cfg_builtin=None):
    syscfg = read_conf_with_confd(cfgfile)

    kern_contents = read_cc_from_cmdline()
    kerncfg = {}
    if kern_contents:
        kerncfg = load_yaml(kern_contents, default={})

    # kernel parameters override system config
    combined = mergedict(kerncfg, syscfg)
    if cfg_builtin:
        fin = mergedict(combined, cfg_builtin)
    else:
        fin = combined

    return fin


def get_cfg_option_bool(yobj, key, default=False):
    if key not in yobj:
        return default
    return translate_bool(yobj[key])


def get_cfg_option_str(yobj, key, default=None):
    if key not in yobj:
        return default
    return yobj[key]


def system_info():
    return {
        'platform': platform.platform(),
        'release': platform.release(),
        'python': platform.python_version(),
        'uname': platform.uname(),
    }


def get_cfg_option_list_or_str(yobj, key, default=None):
    """
    Gets the C{key} config option from C{yobj} as a list of strings. If the
    key is present as a single string it will be returned as a list with one
    string arg.

    @param yobj: The configuration object.
    @param key: The configuration key to get.
    @param default: The default to return if key is not found.
    @return: The configuration option as a list of strings or default if key
        is not found.
    """
    if not key in yobj:
        return default
    if yobj[key] is None:
        return []
    if isinstance(yobj[key], (list)):
        return yobj[key]
    return [yobj[key]]


# get a cfg entry by its path array
# for f['a']['b']: get_cfg_by_path(mycfg,('a','b'))
def get_cfg_by_path(yobj, keyp, default=None):
    cur = yobj
    for tok in keyp:
        if tok not in cur:
            return(default)
        cur = cur[tok]
    return cur


def obj_name(obj):
    if isinstance(obj, (types.TypeType,
                        types.ModuleType,
                        types.FunctionType,
                        types.LambdaType)):
        return str(obj.__name__)
    return obj_name(obj.__class__)


def mergedict(src, cand):
    """
    Merge values from C{cand} into C{src}.
    If C{src} has a key C{cand} will not override.
    Nested dictionaries are merged recursively.
    """
    if isinstance(src, dict) and isinstance(cand, dict):
        for k, v in cand.iteritems():
            if k not in src:
                src[k] = v
            else:
                src[k] = mergedict(src[k], v)
    else:
        if not isinstance(src, dict):
            raise TypeError(("Attempting to merge a non dictionary "
                             "source type: %s") % (obj_name(src)))
        if not isinstance(cand, dict):
            raise TypeError(("Attempting to merge a non dictionary "
                             "candidate type: %s") % (obj_name(cand)))
    return src


@contextlib.contextmanager
def umask(n_msk):
    old = os.umask(n_msk)
    try:
        yield old
    finally:
        os.umask(old)


@contextlib.contextmanager
def tempdir(**kwargs):
    # This seems like it was only added in python 3.2
    # Make it since its useful...
    # See: http://bugs.python.org/file12970/tempdir.patch
    tdir = tempfile.mkdtemp(**kwargs)
    try:
        yield tdir
    finally:
        del_dir(tdir)


def center(text, fill, max_len):
    return '{0:{fill}{align}{size}}'.format(text, fill=fill,
                                            align="^", size=max_len)


def del_dir(path):
    LOG.debug("Recursively deleting %s", path)
    shutil.rmtree(path)


# get gpg keyid from keyserver
def getkeybyid(keyid, keyserver):
    # TODO fix this...
    shcmd = """
    k=${1} ks=${2};
    exec 2>/dev/null
    [ -n "$k" ] || exit 1;
    armour=$(gpg --list-keys --armour "${k}")
    if [ -z "${armour}" ]; then
       gpg --keyserver ${ks} --recv $k >/dev/null &&
          armour=$(gpg --export --armour "${k}") &&
          gpg --batch --yes --delete-keys "${k}"
    fi
    [ -n "${armour}" ] && echo "${armour}"
    """
    args = ['sh', '-c', shcmd, "export-gpg-keyid", keyid, keyserver]
    (stdout, _stderr) = subp(args)
    return stdout


def runparts(dirp, skip_no_exist=True):
    if skip_no_exist and not os.path.isdir(dirp):
        return

    failed = 0
    attempted = 0
    for exe_name in sorted(os.listdir(dirp)):
        exe_path = os.path.join(dirp, exe_name)
        if os.path.isfile(exe_path) and os.access(exe_path, os.X_OK):
            attempted += 1
            try:
                subp([exe_path])
            except ProcessExecutionError as e:
                LOG.exception("Failed running %s [%s]", exe_path, e.exit_code)
                failed += 1

    if failed and attempted:
        raise RuntimeError('Runparts: %s failures in %s attempted commands'
                           % (failed, attempted))


# read_optional_seed
# returns boolean indicating success or failure (presense of files)
# if files are present, populates 'fill' dictionary with 'user-data' and
# 'meta-data' entries
def read_optional_seed(fill, base="", ext="", timeout=5):
    try:
        (md, ud) = read_seeded(base, ext, timeout)
        fill['user-data'] = ud
        fill['meta-data'] = md
        return True
    except OSError as e:
        if e.errno == errno.ENOENT:
            return False
        raise


def read_file_or_url(url, timeout, retries, file_retries):
    if url.startswith("/"):
        url = "file://%s" % url
    if url.startswith("file://"):
        retries = file_retries
    return uhelp.readurl(url, timeout=timeout, retries=retries)


def load_yaml(blob, default=None, allowed=(dict,)):
    loaded = default
    try:
        blob = str(blob)
        LOG.debug(("Attempting to load yaml from string "
                 "of length %s with allowed root types %s"), 
                 len(blob), allowed)
        converted = yaml.load(blob)
        if not isinstance(converted, allowed):
            # Yes this will just be caught, but thats ok for now...
            raise TypeError("Yaml load allows %s types, but got %s instead" %
                            (allowed, obj_name(converted)))
        loaded = converted
    except (yaml.YAMLError, TypeError, ValueError) as exc:
        LOG.exception("Failed loading yaml due to: %s", exc)
    return loaded


def read_seeded(base="", ext="", timeout=5, retries=10, file_retries=0):
    if base.startswith("/"):
        base = "file://%s" % base

    # default retries for file is 0. for network is 10
    if base.startswith("file://"):
        retries = file_retries

    if base.find("%s") >= 0:
        ud_url = base % ("user-data" + ext)
        md_url = base % ("meta-data" + ext)
    else:
        ud_url = "%s%s%s" % (base, "user-data", ext)
        md_url = "%s%s%s" % (base, "meta-data", ext)

    (md_str, msc) = read_file_or_url(md_url, timeout, retries, file_retries)
    md = None
    if md_str and uhelp.ok_http_code(msc):
        md = load_yaml(md_str, default={})

    (ud_str, usc) = read_file_or_url(ud_url, timeout, retries, file_retries)
    ud = None
    if ud_str and uhelp.ok_http_code(usc):
        ud = ud_str

    return (md, ud)


def read_conf_d(confd):
    # get reverse sorted list (later trumps newer)
    confs = sorted(os.listdir(confd), reverse=True)

    # remove anything not ending in '.cfg'
    confs = [f for f in confs if f.endswith(".cfg")]

    # remove anything not a file
    confs = [f for f in confs if os.path.isfile(os.path.join(confd, f))]

    cfg = {}
    for conf in confs:
        cfg = mergedict(cfg, read_conf(os.path.join(confd, conf)))

    return cfg


def read_conf_with_confd(cfgfile):
    cfg = read_conf(cfgfile)

    confd = False
    if "conf_d" in cfg:
        confd = cfg['conf_d']
        if confd:
            if not isinstance(confd, (str, basestring)):
                raise TypeError(("Config file %s contains 'conf_d' "
                                 "with non-string type %s") %
                                 (cfgfile, obj_name(confd)))
            else:
                confd = str(confd).strip()
    elif os.path.isdir("%s.d" % cfgfile):
        confd = "%s.d" % cfgfile

    if not confd or not os.path.isdir(confd):
        return cfg

    return mergedict(read_conf_d(confd), cfg)


def read_cc_from_cmdline(cmdline=None):
    # this should support reading cloud-config information from
    # the kernel command line.  It is intended to support content of the
    # format:
    #  cc: <yaml content here> [end_cc]
    # this would include:
    # cc: ssh_import_id: [smoser, kirkland]\\n
    # cc: ssh_import_id: [smoser, bob]\\nruncmd: [ [ ls, -l ], echo hi ] end_cc
    # cc:ssh_import_id: [smoser] end_cc cc:runcmd: [ [ ls, -l ] ] end_cc
    if cmdline is None:
        cmdline = get_cmdline()

    tag_begin = "cc:"
    tag_end = "end_cc"
    begin_l = len(tag_begin)
    end_l = len(tag_end)
    clen = len(cmdline)
    tokens = []
    begin = cmdline.find(tag_begin)
    while begin >= 0:
        end = cmdline.find(tag_end, begin + begin_l)
        if end < 0:
            end = clen
        tokens.append(cmdline[begin + begin_l:end].lstrip().replace("\\n",
                                                                    "\n"))

        begin = cmdline.find(tag_begin, end + end_l)

    return '\n'.join(tokens)


def dos2unix(contents):
    # find first end of line
    pos = contents.find('\n')
    if pos <= 0 or contents[pos - 1] != '\r':
        return contents
    return contents.replace('\r\n', '\n')


def get_hostname_fqdn(cfg, cloud):
    # return the hostname and fqdn from 'cfg'.  If not found in cfg,
    # then fall back to data from cloud
    if "fqdn" in cfg:
        # user specified a fqdn.  Default hostname then is based off that
        fqdn = cfg['fqdn']
        hostname = get_cfg_option_str(cfg, "hostname", fqdn.split('.')[0])
    else:
        if "hostname" in cfg and cfg['hostname'].find('.') > 0:
            # user specified hostname, and it had '.' in it
            # be nice to them.  set fqdn and hostname from that
            fqdn = cfg['hostname']
            hostname = cfg['hostname'][:fqdn.find('.')]
        else:
            # no fqdn set, get fqdn from cloud.
            # get hostname from cfg if available otherwise cloud
            fqdn = cloud.get_hostname(fqdn=True)
            if "hostname" in cfg:
                hostname = cfg['hostname']
            else:
                hostname = cloud.get_hostname()
    return (hostname, fqdn)


def get_fqdn_from_hosts(hostname, filename="/etc/hosts"):
    """
    For each host a single line should be present with
      the following information:
    
	     IP_address canonical_hostname [aliases...]
    
      Fields of the entry are separated by any number of  blanks  and/or  tab
      characters.  Text  from	a "#" character until the end of the line is a
      comment, and is ignored.	 Host  names  may  contain  only  alphanumeric
      characters, minus signs ("-"), and periods (".").  They must begin with
      an  alphabetic  character  and  end  with  an  alphanumeric  character.
      Optional aliases provide for name changes, alternate spellings, shorter
      hostnames, or generic hostnames (for example, localhost).
    """
    fqdn = None
    try:
        for line in load_file(filename).splitlines():
            hashpos = line.find("#")
            if hashpos >= 0:
                line = line[0:hashpos]
            line = line.strip()
            if not line:
                continue

            # If there there is less than 3 entries 
            # (IP_address, canonical_hostname, alias)
            # then ignore this line
            toks = line.split()
            if len(toks) < 3:
                continue

            if hostname in toks[2:]:
                fqdn = toks[1]
                break
    except IOError:
        pass
    return fqdn


def get_cmdline_url(names=None, starts=None, cmdline=None):
    if cmdline is None:
        cmdline = get_cmdline()
    if not names:
        names = ('cloud-config-url', 'url')
    if not starts:
        starts = "#cloud-config"

    data = keyval_str_to_dict(cmdline)
    url = None
    key = None
    for key in names:
        if key in data:
            url = data[key]
            break

    if not url:
        return (None, None, None)

    (contents, sc) = uhelp.readurl(url)
    if contents.startswith(starts) and uhelp.ok_http_code(sc):
        return (key, url, contents)

    return (key, url, None)


def is_resolvable(name):
    """ determine if a url is resolvable, return a boolean """
    try:
        socket.getaddrinfo(name, None)
        return True
    except socket.gaierror:
        return False


def get_hostname():
    hostname = socket.gethostname()
    return hostname


def is_resolvable_url(url):
    """ determine if this url is resolvable (existing or ip) """
    return (is_resolvable(urlparse.urlparse(url).hostname))


def search_for_mirror(candidates):
    """ Search through a list of mirror urls for one that works """
    for cand in candidates:
        try:
            if is_resolvable_url(cand):
                return cand
        except Exception:
            pass
    return None


def close_stdin():
    """
    reopen stdin as /dev/null so even subprocesses or other os level things get
    /dev/null as input.

    if _CLOUD_INIT_SAVE_STDIN is set in environment to a non empty or '0' value
    then input will not be closed (only useful potentially for debugging).
    """
    if os.environ.get("_CLOUD_INIT_SAVE_STDIN") in ("", "0", 'False'):
        return
    with open(os.devnull) as fp:
        os.dup2(fp.fileno(), sys.stdin.fileno())


def find_devs_with(criteria=None):
    """
    find devices matching given criteria (via blkid)
    criteria can be *one* of:
      TYPE=<filesystem>
      LABEL=<label>
      UUID=<uuid>
    """
    try:
        blk_id_cmd = ['blkid']
        if criteria:
            # Search for block devices with tokens named NAME that 
            # have the value 'value' and display any devices which are found.
            # Common values for NAME include  TYPE, LABEL, and UUID.
            # If there are no devices specified on the command line,
            # all block devices will be searched; otherwise, 
            # only search the devices specified by the user.
            blk_id_cmd.append("-t%s" % (criteria))
        # Only print the device name
        blk_id_cmd.append('-odevice')
        (out, _err) = subp(blk_id_cmd)
        entries = []
        for line in out.splitlines():
            line = line.strip()
            if line:
                entries.append(line)
        return entries
    except ProcessExecutionError:
        return []


def load_file(fname, read_cb=None):
    LOG.debug("Reading from %s", fname)
    with open(fname, 'rb') as fh:
        ofh = StringIO()
        pipe_in_out(fh, ofh, chunk_cb=read_cb)
        ofh.flush()
        contents = ofh.getvalue()
        LOG.debug("Read %s bytes from %s", len(contents), fname)
        return contents


def get_cmdline():
    if 'DEBUG_PROC_CMDLINE' in os.environ:
        cmdline = os.environ["DEBUG_PROC_CMDLINE"]
    else:
        try:
            cmdline = load_file("/proc/cmdline").strip()
        except:
            cmdline = ""
    return cmdline


def pipe_in_out(in_fh, out_fh, chunk_size=1024, chunk_cb=None):
    bytes_piped = 0
    LOG.debug(("Transferring the contents of %s "
             "to %s in chunks of size %sb"), in_fh, out_fh, chunk_size)
    while True:
        data = in_fh.read(chunk_size)
        if data == '':
            break
        else:
            out_fh.write(data)
            bytes_piped += len(data)
            if chunk_cb:
                chunk_cb(bytes_piped)
    out_fh.flush()
    return bytes_piped


def chownbyid(fname, uid=None, gid=None):
    if uid == None and gid == None:
        return
    LOG.debug("Changing the ownership of %s to %s:%s", fname, uid, gid)
    os.chown(fname, uid, gid)


def chownbyname(fname, user=None, group=None):
    uid = -1
    gid = -1
    if user:
        uid = pwd.getpwnam(user).pw_uid
    if group:
        gid = grp.getgrnam(group).gr_gid
    chownbyid(fname, uid, gid)


def ensure_dirs(dirlist, mode=0755):
    for d in dirlist:
        ensure_dir(d, mode)


def ensure_dir(path, mode=0755):
    if not os.path.isdir(path):
        # Make the dir and adjust the mode
        LOG.debug("Ensuring directory exists at path %s", path)
        os.makedirs(path)
        chmod(path, mode)
    else:
        # Just adjust the mode
        chmod(path, mode)


def get_base_cfg(cfg_path=None):
    if not cfg_path:
        cfg_path = CLOUD_CONFIG
    return merge_base_cfg(cfg_path, get_builtin_cfg())


@contextlib.contextmanager
def unmounter(umount):
    try:
        yield umount
    finally:
        if umount:
            umount_cmd = ["umount", '-l', umount]
            subp(umount_cmd)


def mounts():
    mounted = {}
    try:
        # Go through mounts to see if it was already mounted
        mount_locs = load_file("/proc/mounts").splitlines()
        for mpline in mount_locs:
            # Format at: http://linux.die.net/man/5/fstab
            try:
                (dev, mp, fstype, _opts, _freq, _passno) = mpline.split()
            except:
                continue
            # If the name of the mount point contains spaces these 
            # can be escaped as '\040', so undo that..
            mp = mp.replace("\\040", " ")
            mounted[dev] = (dev, fstype, mp, False)
    except (IOError, OSError):
        pass
    return mounted


def mount_cb(device, callback, data=None, rw=False):
    """
    Mount the device, call method 'callback' passing the directory
    in which it was mounted, then unmount.  Return whatever 'callback'
    returned.  If data != None, also pass data to callback.
    """
    mounted = mounts()
    with tempdir() as tmpd:
        umount = False
        if device in mounted:
            mountpoint = "%s/" % mounted[device][2]
        else:
            try:
                mountcmd = ['mount', "-o"]
                if rw:
                    mountcmd.append('rw')
                else:
                    mountcmd.append('ro')
                mountcmd.append(device)
                mountcmd.append(tmpd)
                subp(mountcmd)
                umount = tmpd
            except (IOError, OSError) as exc:
                raise MountFailedError(("Failed mounting %s "
                                        "to %s due to: %s") %
                                       (device, tmpd, exc))
            mountpoint = "%s/" % tmpd
        with unmounter(umount):
            if data is None:
                ret = callback(mountpoint)
            else:
                ret = callback(mountpoint, data)
            return ret


def get_builtin_cfg():
    # Deep copy so that others can't modify
    return copy.deepcopy(CFG_BUILTIN)


def sym_link(source, link):
    LOG.debug("Creating symbolic link from %r => %r" % (link, source))
    os.symlink(source, link)


def del_file(path):
    LOG.debug("Attempting to remove %s", path)
    try:
        os.unlink(path)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise e


def ensure_file(path):
    write_file(path, content='', omode="ab")


def chmod(path, mode):
    real_mode = None
    try:
        real_mode = int(mode)
    except (ValueError, TypeError):
        pass
    if path and real_mode:
        LOG.debug("Adjusting the permissions of %s (perms=%o)",
                 path, real_mode)
        os.chmod(path, real_mode)


def write_file(filename, content, mode=0644, omode="wb"):
    """
    Writes a file with the given content and sets the file mode as specified.
    Resotres the SELinux context if possible.

    @param filename: The full path of the file to write.
    @param content: The content to write to the file.
    @param mode: The filesystem mode to set on the file.
    @param omode: The open mode used when opening the file (r, rb, a, etc.)
    """
    ensure_dir(os.path.dirname(filename))
    LOG.debug("Writing to %s - %s, %s bytes", filename, omode, len(content))
    with open(filename, omode) as fh:
        with SeLinuxGuard(filename):
            fh.write(content)
            fh.flush()
            chmod(filename, mode)


def delete_dir_contents(dirname):
    """
    Deletes all contents of a directory without deleting the directory itself.

    @param dirname: The directory whose contents should be deleted.
    """
    for node in os.listdir(dirname):
        node_fullpath = os.path.join(dirname, node)
        if os.path.isdir(node_fullpath):
            del_dir(node_fullpath)
        else:
            del_file(node_fullpath)


def subp(args, input_data=None, allowed_rc=None, env=None):
    if allowed_rc is None:
        allowed_rc = [0]
    try:
        LOG.debug("Running command %s with allowed return codes %s",
                  args, allowed_rc)
        sp = subprocess.Popen(args, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, stdin=subprocess.PIPE,
            env=env)
        (out, err) = sp.communicate(input_data)
    except OSError as e:
        raise ProcessExecutionError(cmd=args, reason=e)
    rc = sp.returncode
    if rc not in allowed_rc:
        raise ProcessExecutionError(stdout=out, stderr=err,
                                         exit_code=rc,
                                         cmd=args)
    # Just ensure blank instead of none??
    if not out:
        out = ''
    if not err:
        err = ''
    return (out, err)


# shellify, takes a list of commands
#  for each entry in the list
#    if it is an array, shell protect it (with single ticks)
#    if it is a string, do nothing
def shellify(cmdlist, add_header=True):
    content = ''
    if add_header:
        content += "#!/bin/sh\n"
    escaped = "%s%s%s%s" % ("'", '\\', "'", "'")
    for args in cmdlist:
        # if the item is a list, wrap all items in single tick
        # if its not, then just write it directly
        if isinstance(args, list):
            fixed = []
            for f in args:
                fixed.append("'%s'" % str(f).replace("'", escaped))
            content = "%s%s\n" % (content, ' '.join(fixed))
        else:
            content = "%s%s\n" % (content, str(args))
    return content


def is_container():
    """
    Checks to see if this code running in a container of some sort
    """

    for helper in CONTAINER_TESTS:
        try:
            # try to run a helper program. if it returns true/zero
            # then we're inside a container. otherwise, no
            cmd = [helper]
            subp(cmd, allowed_rc=[0])
            return True
        except (IOError, OSError):
            pass

    # this code is largely from the logic in
    # ubuntu's /etc/init/container-detect.conf
    try:
        # Detect old-style libvirt
        # Detect OpenVZ containers
        pid1env = get_proc_env(1)
        if "container" in pid1env:
            return True
        if "LIBVIRT_LXC_UUID" in pid1env:
            return True
    except (IOError, OSError):
        pass

    # Detect OpenVZ containers
    if os.path.isdir("/proc/vz") and not os.path.isdir("/proc/bc"):
        return True

    try:
        # Detect Vserver containers
        lines = load_file("/proc/self/status").splitlines()
        for line in lines:
            if line.startswith("VxID:"):
                (_key, val) = line.strip().split(":", 1)
                if val != "0":
                    return True
    except (IOError, OSError):
        pass

    return False


def get_proc_env(pid):
    """
    Return the environment in a dict that a given process id was started with.
    """

    env = {}
    fn = os.path.join("/proc/", str(pid), "environ")
    try:
        contents = load_file(fn)
        toks = contents.split("\0")
        for tok in toks:
            if tok == "":
                continue
            (name, val) = tok.split("=", 1)
            if name:
                env[name] = val
    except (IOError, OSError):
        pass
    return env


def keyval_str_to_dict(kvstring):
    ret = {}
    for tok in kvstring.split():
        try:
            (key, val) = tok.split("=", 1)
        except ValueError:
            key = tok
            val = True
        ret[key] = val
    return ret
