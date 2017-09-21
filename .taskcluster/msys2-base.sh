#!/bin/sh

set -e
set -x

THIS_DIR=$(cd $(dirname $0) && pwd)

TMP=$(mktemp -d)
[ -z "${TMP}" ] && exit 1

CPU=$1
[ -z "${CPU}" ] && exit 1

case ${CPU} in
i686)
  MSYS=msys32
  MINGW=MINGW32
  ;;
x86_64)
  MSYS=msys64
  MINGW=MINGW64
  ;;
*)
  exit 1
  ;;
esac

VERSION=$2
[ -z "${VERSION}" ] && exit 1

cd "$TMP"

curl -L http://repo.msys2.org/distrib/${CPU}/msys2-base-${CPU}-${VERSION}.tar.xz | tar -Jxf -

cat > ${MSYS}/run-script.cmd <<EOF
set PATH=%CD%\\${MSYS}\\${MINGW}\\bin;%CD%\\${MSYS}\\usr\\bin;%PATH%
set MSYSTEM=${MINGW}
set script=%1
shift
bash %script% %*
EOF

cp ${THIS_DIR}/docker-base/run-task.sh ${MSYS}/

cat > ${MSYS}/run-task.cmd <<EOF
%~dp0\\run-script.cmd %~dp0\\run-task.sh %*
EOF

tar -jcf /tmp/msys-base-${CPU}.tar.bz2 ${MSYS}
