#!/bin/sh

set -e

while [ $# -gt 0 ]; do
  case "$1" in
  --repo)
    repo_url=$2
    tmp=${repo_url%/}
    tmp=${tmp%.git}
    repo_name=$(basename $tmp)
    shift
    ;;
  --checkout)
    repo_checkout=$2
    shift
    ;;
  --install)
    case "$2" in
    *.tar.xz)
      set -x
      curl -sL $2 | tar -C / -Jxf -
      set +x
      ;;
    *.whl)
      set -x
      pip install $2
      set +x
      ;;
    *)
      echo Do not know how to install $2 >&2
      ;;
    esac
    shift
    ;;
  --)
    shift
    break
    ;;
  *)
    echo Unknown option: $1 >&2
    exit 1
    ;;
  esac

  shift
done

check_arg() {
  if [ -z "$(eval echo \$$2)" ]; then
    echo $1 must be passed >&2
    exit 1
  fi
}

check_arg --repo repo_url
check_arg --checkout repo_checkout

set -x

cd /tmp
git clone -n "$repo_url" "$repo_name"
cd "$repo_name"
git checkout "$repo_checkout"

exec "$@"
