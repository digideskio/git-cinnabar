import hashlib
import os

from cinnabar.cmd.util import tree_hash
from cinnabar.util import one


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
