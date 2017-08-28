#!/bin/sh

set -e
set -x

TMP=$(mktemp -d)
[ -z "${TMP}" ] && exit 1

IMAGE_NAME=$1
[ -z "${IMAGE_NAME}" ] && exit 1

BASE_IMAGE=$2

ZSTD_VERSION=1.1.4

curl -L https://github.com/facebook/zstd/archive/v${ZSTD_VERSION}.tar.gz > ${TMP}/zstd.tar.gz
tar -xf ${TMP}/zstd.tar.gz -C ${TMP}
make -j$(grep -c ^processor /proc/cpuinfo) -C ${TMP}/zstd-${ZSTD_VERSION}/programs install
rm -rf ${TMP}

cd "$(dirname $0)"

if [ -n "${BASE_IMAGE}" ]; then
    curl -sL "${BASE_IMAGE}" | zstdcat | docker load
fi

docker build --build-arg REPO_NAME=${GITHUB_HEAD_REPO_NAME} -t ${GITHUB_HEAD_REPO_NAME}-${IMAGE_NAME} docker-${IMAGE_NAME}
docker save ${GITHUB_HEAD_REPO_NAME}-${IMAGE_NAME} | tar --delete manifest.json | zstd > /tmp/${GITHUB_HEAD_REPO_NAME}-${IMAGE_NAME}.tar.zst
