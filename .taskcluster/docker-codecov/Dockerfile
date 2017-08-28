ARG REPO_NAME=git-cinnabar
FROM ${REPO_NAME}-base
RUN apt-get install -y --no-install-recommends gcc git python-coverage && apt-get clean
RUN ln -s /usr/bin/python-coverage /usr/local/bin/coverage
RUN pip install codecov
