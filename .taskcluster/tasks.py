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

from variables import *


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

    def search_local_with_prefix(self, prefix):
        matches = [k for k in self.keys() if k.startswith(prefix)]
        if len(matches) > 1:
            raise Exception("Multiple matches for prefix {}".format(prefix))
        if not matches:
            raise Exception("No match for prefix {}".format(prefix))
        return self[matches[0]]


session = requests.Session()


class Task(object):
    index = Index(session)
    by_id = OrderedDict()

    @classmethod
    def by_index_prefix(cls, prefix):
        return cls.by_id[cls.index.search_local_with_prefix(prefix)]

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
            elif k == 'expireIn':
                value = v.split()
                if len(value) == 1:
                    value, multiplier = value, 1
                elif len(value) == 2:
                    value, unit = value
                    value = long(value)
                    unit = unit.rstrip('s')
                    multiplier = 1
                    if unit == 'year':
                        multiplier *= 365
                        unit = 'day'
                    if unit == 'day':
                        multiplier *= 24
                        unit = 'hour'
                    if unit == 'hour':
                        multiplier *= 60
                        unit = 'minute'
                    if unit == 'minute':
                        multiplier *= 60
                        unit = 'second'
                    if unit == 'second':
                        unit = ''
                    if unit:
                        raise Exception(
                            "Don't know how to handle {}".format(uint))
                else:
                    raise Exception("Don't know how to handle {}".format(v))
                task['expires'] = (now + value * multiplier).format()
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
                if kwargs.get('workerType') in ('dummy-worker-packet',
                                                'win2012r2'):
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
            elif k == 'mounts':
                def file_format(url):
                    for ext in ('rar', 'tar.bz2', 'tar.gz', 'zip'):
                        if url.endswith('.{}'.format(ext)):
                            return ext
                    raise Exception(
                        'Unsupported/unknown format for {}'.format(url))

                task['payload'][k] = [
                    {
                        'content': {
                            'artifact': '/'.join(
                                t.artifact.rsplit('/', 2)[-2:]),
                            'taskId': t.id,
                        },
                        'directory': '.',
                        'format': file_format(t.artifact),
                    }
                    for t in v
                ]
                dependencies.extend(t.id for t in v)
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
