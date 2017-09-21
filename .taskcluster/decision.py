#!/usr/bin/env python2.7
import hashlib
import os
import pipes
import sys

from distutils.version import StrictVersion as Version
from itertools import chain

BASE_DIR = os.path.dirname(__file__)
sys.path.append(BASE_DIR)
sys.path.append(os.path.join(BASE_DIR, '..'))

from docker import DockerRegistry
from tasks import Task
from variables import *

from cinnabar.cmd.util import helper_hash
from cinnabar.git import Git
from cinnabar.util import one


MSYS_VERSION = '20161025'
INFRA_EXPIRY = '2 hours'


def run_task(wrapper, command):
    return [
        wrapper,
        '--repo',
        GITHUB_HEAD_REPO_URL,
        '--checkout',
        GITHUB_HEAD_SHA,
        '--',
        'sh',
        '-x',
        '-c',
        command
    ]

def run_task_sh(command):
    return run_task('run-task.sh', command)

def run_task_cmd(command):
    return [' '.join(pipes.quote(a)
                     for a in run_task('*\\run-task.cmd', command))]


registry = DockerRegistry(BASE_DIR)
for name in registry:
    base = registry.base.get(name) or []
    if base:
        base = ['{{{}.artifact}}'.format(
            Task.by_index_prefix('docker-image.{}'.format(base)))]
    Task(
        provisionerId='test-dummy-provisioner',
        workerType='dummy-worker-packet',
        description='docker image: {}'.format(name),
        index='docker-image.{}.{}'.format(name, registry.hash(name)),
        expireIn=INFRA_EXPIRY,
        image='https://s3-us-west-2.amazonaws.com/public-qemu-images'
              '/repository/github.com/taskcluster/taskcluster-worker'
              '/ubuntu-worker.tar.zst',
        command=[
            'clone-and-exec.sh',
            '.taskcluster/docker-image.sh',
            name,
        ] + base,
        artifact='/tmp/{}-{}.tar.zst'.format(GITHUB_HEAD_REPO_NAME, name),
        env={
            'REPOSITORY': GITHUB_HEAD_REPO_URL,
            'REVISION': GITHUB_HEAD_SHA,
            'GITHUB_HEAD_REPO_NAME': GITHUB_HEAD_REPO_NAME,
        }
    )

for cpu in ('i686', 'x86_64'):
    command = (
        'curl -L http://repo.msys2.org/distrib/{cpu}'
        '/msys2-base-{cpu}-{version}.tar.xz | xz -cd | bzip2 -c'
        ' > /tmp/msys-base-{cpu}.tar.bz2'.format(cpu=cpu, version=MSYS_VERSION)
    )
    msys2_base_hash = hashlib.sha1(command).hexdigest()
    Task(
        description='msys2 image: base {}'.format(cpu),
        index='msys2-image.base.{}.{}'.format(cpu, msys2_base_hash),
        expireIn=INFRA_EXPIRY,
        image=Task.by_index_prefix('docker-image.build'),
        command=run_task_sh(
            '.taskcluster/msys2-base.sh {} {}'.format(cpu, MSYS_VERSION),
        ),
        artifact='/tmp/msys-base-{}.tar.bz2'.format(cpu)
    )

    packages = [
        'mingw-w64-{}-{}'.format(cpu, pkg) for pkg in (
            'curl',
            'gcc',
            'make',
            'pcre',
            'perl',
            'python2',
            'python2-pip',
        )
    ] + [
        'patch',
    ]
    Task(
        description='msys2 image: build {}'.format(cpu),
        workerType='win2012r2',
        index='msys2-image.build.{}.{}'.format(
            cpu, hashlib.sha1(msys2_base_hash + '  ' +
                              (' '.join(packages))).hexdigest()),
        command=run_task_cmd(
            'pacman-key --init && '
            'pacman-key --populate msys2 && '
            'pacman --noconfirm -Sy --force --asdeps pacman-mirrors && '
            'pacman --noconfirm -Sy tar {packages} && '
            'rm -rf /var/cache/pacman/pkg && '
            'pip install wheel && '
            'tar -jcf msys2-{cpu}.tar.bz2 msys*'.format(
                cpu=cpu,
                packages=' '.join(packages))),
        mounts=(Task.by_index_prefix('msys2-image.base.{}'.format(cpu)),),
        artifact='msys2-{}.tar.bz2'.format(cpu),
    )

Task.submit()
sys.exit(0)

for cpu in ('i686', 'x86_64'):

    bits = {
        'i686': 32,
        'x86_64': 64,
    }[cpu]
    command = (
        'cd /tmp && '
        'curl -OL https://github.com/git-for-windows/git/releases/download'
        '/v2.14.1.windows.1/MinGit-2.14.1-{}-bit.zip && '
        'mkdir git && '
        'cd git && '
        'apt-get install unzip && '
        'unzip ../MinGit-2.14.1-{}-bit.zip && '
        'cd .. && '
        'tar -jcf git-{}.tar.bz2 git/'
    ).format(bits, bits, cpu)

    git = Task(
        description='git {}'.format(cpu),
        provisionerId='test-dummy-provisioner',
        workerType='dummy-worker-packet',
        image='https://s3-us-west-2.amazonaws.com/public-qemu-images'
              '/repository/github.com/taskcluster/taskcluster-worker'
              '/ubuntu-worker.tar.zst',
        command=[
            'sh',
            '-x',
            '-c',
            command,
        ],
        artifact='/tmp/git-{}.tar.bz2'.format(cpu)
    )


    mingw = {
        'i686': 'MINGW32',
        'x86_64': 'MINGW64',
    }.get(cpu)
    msys = {
        'i686': 'msys32',
        'x86_64': 'msys64',
    }.get(cpu)
    packages = [
        'mingw-w64-{}-{}'.format(cpu, pkg) for pkg in (
            'curl',
            'gcc',
            'make',
            'pcre',
            'perl',
            'python2',
            'python2-pip',
        )
    ] + [
        'patch',
    ]
    msys2_build = Task(
        description='msys2 build {}'.format(cpu),
        workerType='win2012r2',
        index='msys2.build.{}.{}'.format(
            cpu, hashlib.sha1(msys2_base_hash + '  ' +
                              (' '.join(packages))).hexdigest()),
        command=[
            'set PATH=%CD%\\{msys}\\{mingw}\\bin;%CD%\\{msys}\\usr\\bin'
            ';%PATH%'.format(msys=msys, mingw=mingw),
            'bash /usr/bin/pacman-key --init',
            'bash /usr/bin/pacman-key --populate msys2',
            'pacman --noconfirm -Sy --force --asdeps pacman-mirrors',
            'pacman --noconfirm -Sy tar {}'.format(' '.join(packages)),
            'rm -rf /var/cache/pacman/pkg',
            'pip install wheel',
            'tar -jcf msys2-{}.tar.bz2 {}'.format(cpu, msys),
        ],
        env={
            'MSYSTEM': mingw,
        },
        mounts=(msys2_base,),
        artifact='msys2-{}.tar.bz2'.format(cpu),
    )
    packages = [
        'mingw-w64-{}-{}'.format(cpu, pkg) for pkg in (
            'curl',
            'make',
            'pcre',
            'python2',
            'python2-pip',
        )
    ] + [
        'diffutils',
        'git',
    ]
    msys2_test = Task(
        description='msys2 test {}'.format(cpu),
        workerType='win2012r2',
        index='msys2.test.{}.{}'.format(
            cpu, hashlib.sha1(msys2_base_hash + '  ' +
                              (' '.join(packages))).hexdigest()),
        command=[
            'set PATH=%CD%\\{msys}\\{mingw}\\bin;%CD%\\{msys}\\usr\\bin'
            ';%PATH%'.format(msys=msys, mingw=mingw),
            'bash /usr/bin/pacman-key --init',
            'bash /usr/bin/pacman-key --populate msys2',
            'pacman --noconfirm -Sy --force --asdeps pacman-mirrors',
            'pacman --noconfirm -Sy tar {}'.format(' '.join(packages)),
            'rm -rf /var/cache/pacman/pkg',
            'tar -jcf msys2-{}.tar.bz2 {}'.format(cpu, msys),
        ],
        env={
            'MSYSTEM': mingw,
        },
        mounts=(msys2_base,),
        artifact='msys2-{}.tar.bz2'.format(cpu),
    )

    version = '4.3.2'
    mercurial = Task(
        description='hg v{} {}'.format(version, cpu),
        index='{}.hg.v{}'.format(msys2_build, version),
        workerType='win2012r2',
        command=[
            'set PATH=%CD%\\{msys}\\{mingw}\\bin;%CD%\\{msys}\\usr\\bin'
            ';%PATH%'.format(msys=msys, mingw=mingw),
            'sed -i "1s,.*,#!python2.exe,"'
            ' {}/{}/bin/pip-script.py'.format(msys, mingw),
            'pip wheel -v --build-option -b --build-option %CD%\\wheel'
            ' mercurial=={}'.format(version),
        ],
        mounts=(msys2_build,),
        env={
            'MSYSTEM': mingw,
        },
        artifact='mercurial-{}-cp27-cp27m-mingw.whl'.format(version),
    )

    helper_cpu = {
        'i686': 'x86'
    }.get(cpu, cpu)
    helper = Task(
        description='helper {}'.format(cpu),
        index='helper.{}.windows.{}'.format(helper_hash(), helper_cpu),
        workerType='win2012r2',
        command=[
            'set PATH=%CD%\\{msys}\\{mingw}\\bin;%CD%\\{msys}\\usr\\bin'
            ';%PATH%'.format(msys=msys, mingw=mingw),
            'git clone -n {} {}'.format(GITHUB_HEAD_REPO_URL,
                                        GITHUB_HEAD_REPO_NAME),
            'cd {}'.format(GITHUB_HEAD_REPO_NAME),
            'git checkout {}'.format(GITHUB_HEAD_SHA),
            'bash -c "mingw32-make -j$(nproc) helper"',
        ],
        mounts=(msys2_build,),
        env={
            'MSYSTEM': mingw,
        },
        artifact='git-cinnabar/git-cinnabar-helper.exe',
    )

    Task(
        description='test {}'.format(cpu),
        workerType='win2012r2',
        command=[
            'set PATH=%CD%\\{msys}\\{mingw}\\bin;%CD%\\{msys}\\usr\\bin'
            ';%PATH%'.format(msys=msys, mingw=mingw),
            'sed -i "1s,.*,#!python2.exe,"'
            ' {}/{}/bin/pip-script.py'.format(msys, mingw),
            'pip install {{{}.artifact}}'.format(mercurial),
            'git clone -n {} {}'.format(GITHUB_HEAD_REPO_URL,
                                        GITHUB_HEAD_REPO_NAME),
            'cd {}'.format(GITHUB_HEAD_REPO_NAME),
            'git checkout {}'.format(GITHUB_HEAD_SHA),
            'curl --compressed -OL {{{}.artifact}}'.format(helper),
            'bash -c "for postinst in /etc/post-install/*.post; do'
            ' test -e $postinst && . $postinst;'
            ' done"',
            'set PATH=%CD%;%PATH%',
            'git --version',
            'git -c fetch.prune=true clone -n hg::%REPO% hg.old.git',
            'mingw32-make -f CI.mk script',
        ],
        mounts=(msys2_test,),
        env={
            'REPO': 'https://hg.mozilla.org/users/mh_glandium.org/jqplot',
            'MSYSTEM': mingw,
        },
    )

    Task(
        description='test 2 {}'.format(cpu),
        workerType='win2012r2',
        command=[
            'set PATH=%CD%\\git\\cmd;%CD%\\{msys}\\{mingw}\\bin;%CD%\\{msys}\\usr\\bin'
            ';%PATH%'.format(msys=msys, mingw=mingw),
            'sed -i "1s,.*,#!python2.exe,"'
            ' {}/{}/bin/pip-script.py'.format(msys, mingw),
            'pip install {{{}.artifact}}'.format(mercurial),
            'git clone -n {} {}'.format(GITHUB_HEAD_REPO_URL,
                                        GITHUB_HEAD_REPO_NAME),
            'cd {}'.format(GITHUB_HEAD_REPO_NAME),
            'git checkout {}'.format(GITHUB_HEAD_SHA),
            'curl --compressed -OL {{{}.artifact}}'.format(helper),
            'bash -c "for postinst in /etc/post-install/*.post; do'
            ' test -e $postinst && . $postinst;'
            ' done"',
            'set PATH=%CD%;%PATH%',
            'git --version',
            'git -c fetch.prune=true clone -n hg::%REPO% hg.old.git',
            'mingw32-make -f CI.mk script',
        ],
        mounts=(msys2_test, git),
        env={
            'REPO': 'https://hg.mozilla.org/users/mh_glandium.org/jqplot',
            'MSYSTEM': mingw,
        },
    )

GIT_VERSIONS = ('1.8.5', '2.7.4', '2.14.1')
git = {}
for version in GIT_VERSIONS:
    git[version] = Task(
        description='git v{}'.format(version),
        index='{}.git.v{}'.format(images['build'], version),
        image=images['build'],
        command=[
            'run-task.sh',
            '--repo',
            'git://git.kernel.org/pub/scm/git/git.git',
            '--checkout',
            'v{}'.format(version),
            '--',
            'sh',
            '-x',
            '-c',
            'make -j$(nproc) install prefix=/usr'
            ' NO_GETTEXT=1 NO_OPENSSL=1 NO_TCLTK=1'
            ' DESTDIR=/tmp/git-install && '
            'tar -C /tmp/git-install -Jcf /tmp/git-{}.tar.xz .'.format(version)
        ],
        artifact='/tmp/git-{}.tar.xz'.format(version),
    )

MERCURIAL_VERSIONS = ('1.9', '2.5', '2.6.2', '2.7.2', '3.0', '3.6', '4.3.1',
                      '4.4')
mercurial = {}
for version in MERCURIAL_VERSIONS:
    # 2.6.2 is the first version available on pypi
    if Version('2.6.2') <= version:
        source = 'mercurial=={}'
    else:
        source = 'https://mercurial-scm.org/release/mercurial-{}.tar.gz'

    mercurial[version] = Task(
        description='hg v{}'.format(version),
        index='{}.hg.v{}'.format(images['build'], version),
        image=images['build'],
        command=[
            'pip',
            'wheel',
            '-w',
            '/tmp',
            source.format(version),
        ],
        artifact='/tmp/mercurial-{}-cp27-none-linux_x86_64.whl'.format(version)
    )

Task(
    description='python lint & tests',
    image=images['test'],
    command=[
        'run-task.sh',
        '--repo',
        GITHUB_HEAD_REPO_URL,
        '--checkout',
        GITHUB_HEAD_SHA,
        '--install',
        '{{{}.artifact}}'.format(git[GIT_VERSIONS[-1]]),
        '--install',
        '{{{}.artifact}}'.format(mercurial[MERCURIAL_VERSIONS[-1]]),
        '--',
        'sh',
        '-x',
        '-c',
        'make -f CI.mk script && '
        'tar -Jcf coverage.tar.xz .coverage'
    ],
    artifact='/tmp/git-cinnabar/coverage.tar.xz',
    env={
        'PYTHON_CHECKS': 1,
        'VARIANT': 'coverage',
    }
)


def old_helper_head():
    from cinnabar.helper import GitHgHelper
    version = GitHgHelper.VERSION
    return list(Git.iter('log', 'HEAD', '--format=%H', '--pickaxe-regex',
                         '-S', '#define CMD_VERSION {}'.format(version)))[-1]


def old_helper_hash(head):
    from cinnabar.git import split_ls_tree
    return split_ls_tree(one(Git.iter('ls-tree', head, 'helper')))[2]


helpers = {}
for variant, flags in (
    ('', ''),
    ('.asan', ' CFLAGS="-O2 -g -fsanitize=address"'
              ' LDFLAGS=-static-libasan'),
    ('.coverage', ' CFLAGS=-coverage &&'
                  ' mv git-core/hg*.gcno git-core/cinnabar*.gcno helper/ &&'
                  ' tar -Jcf coverage.tar.xz'
                  ' $(find . -name hg\\*.gcno -o -name cinnabar\\*.gcno)'),
    ('.old', ''),
):
    if variant == '.coverage':
        kwargs = {
            'artifacts': [
                '/tmp/git-cinnabar/git-cinnabar-helper',
                '/tmp/git-cinnabar/coverage.tar.xz',
            ],
        }
    else:
        kwargs = {
            'artifact': '/tmp/git-cinnabar/git-cinnabar-helper',
        }

    helper = variant.lstrip('.')
    head = GITHUB_HEAD_SHA
    hash = helper_hash()
    desc = variant.lstrip('.')
    if variant == '.old':
        head = old_helper_head()
        hash = old_helper_hash(head)
        desc = hash

    helpers[helper] = Task(
        description='helper {}'.format(desc).rstrip(),
        index='helper.{}.linux.x86_64{}'.format(hash, variant),
        image=images['build'],
        command=[
            'run-task.sh',
            '--repo',
            GITHUB_HEAD_REPO_URL,
            '--checkout',
            head,
            '--',
            'sh',
            '-x',
            '-c',
            'make -j$(nproc) helper prefix=/usr{}'.format(flags),
        ],
        **kwargs
    )

UPGRADE_FROM = ('0.3.0', '0.3.2', '0.4.0', '0.5.0b2')
clones = {}
for version in (GITHUB_HEAD_SHA,) + UPGRADE_FROM:
    sha1 = one(Git.iter('rev-parse', version))
    if version == GITHUB_HEAD_SHA:
        download = 'curl --compressed -OL {{{}.artifact}} && ' \
                   'chmod +x git-cinnabar-helper && '.format(helpers[''])
    elif Version('0.4.0') <= version:
        download = './git-cinnabar download && '
    else:
        download = ''
    clones[version] = Task(
        description='clone w/ {}'.format(version),
        index='clone.{}'.format(sha1),
        image=images['test'],
        command=[
            'run-task.sh',
            '--repo',
            GITHUB_HEAD_REPO_URL,
            '--checkout',
            sha1,
            '--install',
            '{{{}.artifact}}'.format(git[GIT_VERSIONS[-1]]),
            '--install',
            '{{{}.artifact}}'.format(mercurial[MERCURIAL_VERSIONS[-1]]),
            '--',
            'sh',
            '-x',
            '-c',
            download +
            'git -c fetch.prune=true clone -n hg::$REPO hg.old.git && '
            'tar -Jcf /tmp/clone.tar.xz hg.old.git'
        ],
        artifact='/tmp/clone.tar.xz',
        env={
            'REPO': 'https://hg.mozilla.org/users/mh_glandium.org/jqplot',
        },
    )


for git_ver, hg_ver, variants in chain(
    ((GIT_VERSIONS[-1], h, ()) for h in MERCURIAL_VERSIONS),
    ((GIT_VERSIONS[-1], h, ('no-bundle2',))
     for h in MERCURIAL_VERSIONS if Version('3.4') <= h),
    ((g, MERCURIAL_VERSIONS[-1], '') for g in GIT_VERSIONS),
    ((GIT_VERSIONS[-1], MERCURIAL_VERSIONS[-1], v) for v in (
        ('asan', 'experiments'),
        ('asan', 'experiments', 'no-bundle2'),
        ('coverage', 'graft'),
        ('coverage', 'experiments'),
        ('coverage', 'experiments', 'no-bundle2'),
        ('old helper',),
    )),
    ((GIT_VERSIONS[-1], MERCURIAL_VERSIONS[-1], ('coverage',
                                                 'upgrade from v{}'.format(v)))
     for v in UPGRADE_FROM),
):
    env = {}
    kwargs = {}
    helper = ''
    postcmd = ''
    clone = clones[GITHUB_HEAD_SHA]
    for v in variants:
        if v == 'no-bundle2':
            env['NO_BUNDLE2'] = '1'
        elif v == 'asan':
            helper = v
        elif v == 'coverage':
            helper = v
            env['VARIANT'] = 'coverage'
            kwargs['artifact'] = '/tmp/git-cinnabar/coverage.tar.xz'
            postcmd = (
                ' && '
                'mv git-core/hg*.gcda git-core/cinnabar*.gcda helper/ && '
                'tar -Jcf coverage.tar.xz $(find . -name .coverage '
                '-o -name hg\*.gcda -o -name cinnabar\*.gcda)'
            )
        elif v == 'graft':
            env['GRAFT'] = '1'
        elif v == 'experiments':
            env['GIT_CINNABAR_EXPERIMENTS'] = 'true'
        elif v.startswith('upgrade from v'):
            v = v[len('upgrade from v'):]
            env['UPGRADE_FROM'] = v
            clone = clones[v]
        elif v == 'old helper':
            helper = 'old'
        else:
            raise Exception("Don't know how to handle {}'.format(v)")
    if env:
        kwargs['env'] = env
    Task(
        description='test w/ git v{}, mercurial v{}{}'.format(
            git_ver, hg_ver, ''.join(', {}'.format(v) for v in variants)),
        image=images['test'],
        command=[
            'run-task.sh',
            '--repo',
            GITHUB_HEAD_REPO_URL,
            '--checkout',
            GITHUB_HEAD_SHA,
            '--install',
            '{{{}.artifact}}'.format(git[git_ver]),
            '--install',
            '{{{}.artifact}}'.format(mercurial[hg_ver]),
            '--',
            'sh',
            '-x',
            '-c',
            'curl --compressed -OL {{{}.artifacts[0]}} && '
            'chmod +x git-cinnabar-helper && '
            'curl -L {{{}.artifact}} | tar -Jxf - && '
            'make -f CI.mk script{}'.format(helpers[helper], clone, postcmd)
        ],
        **kwargs
    )

upload_coverage = ' && '.join(
    'curl -L {{{}.artifacts[0]}} | tar -Jxf - && codecov --name "{}"'
    ' --commit {} --branch {} && '
    'find . \( -name .coverage -o -name coverage.xml -o -name \*.gcda'
    ' -o -name \*.gcov \) -delete'.format(
        task, task.task['metadata']['name'],
        GITHUB_HEAD_SHA, GITHUB_HEAD_BRANCH)
    for task in Task.by_id.itervalues()
    if task.artifacts and 'coverage.tar.xz' in task.artifacts[0]
)
Task(
    description='upload coverage',
    image=images['codecov'],
    scopes=['secrets:get:repo:github.com/glandium.git-cinnabar:codecov'],
    command=[
        'run-task.sh',
        '--repo',
        GITHUB_HEAD_REPO_URL,
        '--checkout',
        GITHUB_HEAD_SHA,
        '--',
        'sh',
        '-c',
        'export CODECOV_TOKEN=$(curl -sL http://taskcluster/secrets/v1'
        '/secret/repo:github.com/glandium.git-cinnabar:codecov | '
        'python -c "import json, sys; print(json.load(sys.stdin)'
        '[\\"secret\\"][\\"token\\"])") && '
        'set -x && '
        'curl -L {{{}.artifacts[1]}} | tar -Jxf - && '
        '{}'.format(helpers['coverage'], upload_coverage)
    ]
)

Task(
    description='windows test',
    workerType='win2012r2',
    command=[
        'env',
    ]
)

Task.submit()
