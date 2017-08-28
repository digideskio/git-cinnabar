#!/usr/bin/env python2.7
import base64
import datetime
import hashlib
import json
import numbers
import os
import requests
import sys
import uuid

from collections import OrderedDict
from distutils.version import StrictVersion as Version
from itertools import chain
from string import Formatter

BASE_DIR = os.path.dirname(__file__)
sys.path.append(os.path.join(BASE_DIR, '..'))

from cinnabar.cmd.util import (
    helper_hash,
    tree_hash,
)
from cinnabar.git import Git
from cinnabar.util import one


if 'TASK_ID' in os.environ:
    PROXY_INDEX_URL = 'http://taskcluster/index/v1/task/{}'
else:
    PROXY_INDEX_URL = 'https://index.taskcluster.net/v1/task/{}'
ARTIFACT_URL = 'https://queue.taskcluster.net/v1/task/{}/artifacts/{}'

GITHUB_HEAD_USER = os.environ.get('GITHUB_HEAD_USER', 'glandium')
GITHUB_HEAD_USER_EMAIL = os.environ.get('GITHUB_HEAD_USER_EMAIL', 'glandium@')
GITHUB_HEAD_REPO_NAME = os.environ.get('GITHUB_HEAD_REPO_NAME', 'git-cinnabar')
GITHUB_HEAD_REPO_URL = os.environ.get(
    'GITHUB_HEAD_REPO_URL',
    'https://github.com/{}/{}'.format(GITHUB_HEAD_USER, GITHUB_HEAD_REPO_NAME))
GITHUB_HEAD_SHA = os.environ.get('GITHUB_HEAD_SHA', 'HEAD')
GITHUB_HEAD_BRANCH = os.environ.get('GITHUB_HEAD_BRANCH', 'HEAD')
GITHUB_BASE_USER = os.environ.get('GITHUB_BASE_USER', GITHUB_HEAD_USER)
GITHUB_BASE_REPO_NAME = os.environ.get('GITHUB_BASE_REPO_NAME',
                                       GITHUB_HEAD_REPO_NAME)


def slugid():
    rawBytes = uuid.uuid4().bytes
    # Ensure base64-encoded bytes start with [A-Za-f]
    first = ord(rawBytes[0])
    if first >= 0xd0:
        rawBytes = chr(first & 0x7f) + rawBytes[1:]
    return base64.urlsafe_b64encode(rawBytes)[:-2]  # Drop '==' padding


timedelta = datetime.timedelta


class datetime(datetime.datetime):
    def format(self, no_usec=True):
        if no_usec:
            return self.replace(microsecond=0).isoformat() + 'Z'
        if self.microsecond == 0:
            return self.isoformat() + '.000000Z'
        return self.isoformat() + 'Z'

    def __add__(self, other):
        if isinstance(other, numbers.Number):
            other = timedelta(seconds=other)
        d = super(datetime, self).__add__(other)
        return self.combine(d.date(), d.timetz())


task_group_id = os.environ.get('TASK_ID') or slugid()
now = datetime.utcnow()


class Index(dict):
    class Existing(str):
        pass

    def __init__(self, requests=requests):
        super(Index, self).__init__()
        self.requests = requests

    def __missing__(self, key):
        result = None
        if (GITHUB_BASE_USER != GITHUB_HEAD_USER or
                GITHUB_BASE_REPO_NAME != GITHUB_HEAD_REPO_NAME):
            result = self.try_key('github.{}.{}.{}'.format(
                GITHUB_HEAD_USER, GITHUB_HEAD_REPO_NAME, key))
        if not result:
            result = self.try_key('github.{}.{}.{}'.format(
                GITHUB_BASE_USER, GITHUB_BASE_REPO_NAME, key), create=True)
        self[key] = result
        return result

    def try_key(self, key, create=False):
        response = self.requests.get(PROXY_INDEX_URL.format(key))
        result = None
        if response.status_code >= 400:
            # Consume content before returning, so that the connection
            # can be reused.
            response.content
        else:
            data = response.json()
            try:
                expires = datetime.strptime(data['expires'].rstrip('Z'),
                                            '%Y-%m-%dT%H:%M:%S.%f')
            except (KeyError, ValueError):
                expires = now
            # Only consider tasks that aren't expired or won't expire
            # within the hour.
            if expires >= now + 3600:
                result = data.get('taskId')
        if result:
            print 'Found task "{}" for "{}"'.format(result, key)
            return self.Existing(result)
        elif not create:
            return None
        return slugid()


class DockerRegistry(object):
    def __init__(self, base_dir):
        self.data_hash = {}
        self.base = {}
        for f in os.listdir(base_dir or '.'):
            prefix = 'docker-'
            if not f.startswith(prefix):
                continue
            d = os.path.join(base_dir, f)
            if not os.path.isdir(d):
                continue
            name = f[len(prefix):]
            docker_file = os.path.join(d, 'Dockerfile')
            with open(docker_file) as fh:
                base = one(l for l in fh.readlines() if l.startswith('FROM '))
            if base:
                _, base = base.split(None, 1)
                prefix = '${REPO_NAME}-'
                if base.startswith(prefix):
                    base = base[len(prefix):].strip()
                    self.base[name] = base
            self.data_hash[name] = tree_hash(os.listdir(d), d)

    def __iter__(self):
        emitted = set()
        for i in set(self.data_hash) - set(self.base):
            yield i
            emitted.add(i)
        by_base = {}
        for k, v in self.base.iteritems():
            by_base.setdefault(v, []).append(k)
        while len(emitted) < len(self.data_hash):
            for i in tuple(emitted):
                for j in by_base.get(i, []):
                    if j not in emitted:
                        yield j
                        emitted.add(j)

    def __contains__(self, key):
        return key in self.data_hash

    def hash(self, name):
        base = self.base.get(name)
        if base:
            h = hashlib.sha1(self.hash(base))
            h.update(self.data_hash[name])
            return h.hexdigest()
        else:
            return self.data_hash[name]


session = requests.Session()


class Task(object):
    index = Index(session)
    by_id = OrderedDict()

    class Resolver(Formatter):
        def __init__(self):
            self._used = set()

        def get_value(self, key, args, kwargs):
            task = Task.by_id.get(key)
            if task:
                self._used.add(task)
                return task
            raise KeyError()

        def used(self):
            for u in self._used:
                yield u

    def __init__(self, **kwargs):
        index = kwargs.get('index')
        if index:
            self.id = Task.index[index]
        else:
            self.id = slugid()

        task = {
            'created': now.format(),
            'deadline': (now + 3600).format(),
            'expires': (now + 86400).format(),
            'retries': 0,
            'provisionerId': 'aws-provisioner-v1',
            'workerType': 'github-worker',
            'schedulerId': 'taskcluster-github',
            'taskGroupId': task_group_id,
            'metadata': {
                'owner': GITHUB_HEAD_USER_EMAIL,
                'source': GITHUB_HEAD_REPO_URL,
            },
            'payload': {
                'maxRunTime': 1800,
            },
        }
        dependencies = [task_group_id]
        self.artifacts = []

        for k, v in kwargs.iteritems():
            if k in ('provisionerId', 'workerType'):
                task[k] = v
            elif k == 'description':
                task['metadata'][k] = task['metadata']['name'] = v
            elif k == 'index':
                task['routes'] = ['index.github.{}.{}.{}'.format(
                    GITHUB_HEAD_USER, GITHUB_HEAD_REPO_NAME, v)]
            elif k == 'command':
                resolver = Task.Resolver()
                task['payload']['command'] = [
                    resolver.format(a)
                    for a in v
                ]
                for t in resolver.used():
                    dependencies.append(t.id)

            elif k in ('artifact', 'artifacts'):
                if k[-1] == 's':
                    assert 'artifact' not in kwargs
                else:
                    assert 'artifacts' not in kwargs
                    v = [v]
                artifacts = {
                    'public/{}'.format(os.path.basename(a)): {
                        'path': a,
                        'type': 'file',
                    }
                    for a in v
                }
                urls = [
                    ARTIFACT_URL.format(self.id, a)
                    for a in artifacts
                ]
                if kwargs.get('workerType') == 'dummy-worker-packet':
                    artifacts = [
                        a.update(name=name) or a
                        for name, a in artifacts.iteritems()
                    ]
                task['payload']['artifacts'] = artifacts
                if k[-1] == 's':
                    self.artifacts = urls
                else:
                    self.artifact = urls[0]
                    self.artifacts = [self.artifact]
            elif k == 'env':
                task['payload']['env'] = v
            elif k == 'image':
                if isinstance(v, Task):
                    v = {
                        'path': 'public/{}'.format(
                            os.path.basename(v.artifact)),
                        'taskId': v.id,
                        'type': 'task-image',
                    }
                    dependencies.append(v['taskId'])
                task['payload']['image'] = v
            elif k == 'scopes':
                task[k] = v
                for s in v:
                    if s.startswith('secrets:'):
                        features = task['payload'].setdefault('features', {})
                        features['taskclusterProxy'] = True
            else:
                raise Exception("Don't know how to handle {}".format(k))
        task['dependencies'] = sorted(dependencies)
        self.task = task
        Task.by_id[self.id] = self

    def __str__(self):
        return self.id

    @classmethod
    def submit(cls):
        for task in cls.by_id.itervalues():
            if isinstance(task.id, Index.Existing):
                continue
            print('Submitting task "{}":'.format(task.id))
            print json.dumps(task.task, indent=4, sort_keys=True)
            if 'TASK_ID' not in os.environ:
                continue
            url = 'http://taskcluster/queue/v1/task/{}'.format(task.id)
            res = session.put(url, data=json.dumps(task.task))
            try:
                res.raise_for_status()
            except Exception:
                print(res.headers)
                print(res.content)
                raise
            print(res.json())


registry = DockerRegistry(BASE_DIR)
images = {}
for name in registry:
    base = registry.base.get(name) or []
    if base:
        base = ['{{{}.artifact}}'.format(images[base])]
    images[name] = Task(
        provisionerId='test-dummy-provisioner',
        workerType='dummy-worker-packet',
        description='docker image: {}'.format(name),
        index='docker-image.{}'.format(registry.hash(name)),
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

MERCURIAL_VERSIONS = ('1.9', '2.5', '2.6.2', '2.7.2', '3.0', '3.6', '4.3.1')
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
        download = 'curl -sOL {{{}.artifact}} && ' \
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
            'curl -sOL {{{}.artifacts[0]}} && '
            'chmod +x git-cinnabar-helper && '
            'curl -sL {{{}.artifact}} | tar -Jxf - && '
            'make -f CI.mk script{}'.format(helpers[helper], clone, postcmd)
        ],
        **kwargs
    )

upload_coverage = ' && '.join(
    'curl -sL {{{}.artifacts[0]}} | tar -Jxf - && codecov --name "{}"'
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
        'curl -sL {{{}.artifacts[1]}} | tar -Jxf - && '
        '{}'.format(helpers['coverage'], upload_coverage)
    ]
)

Task.submit()
