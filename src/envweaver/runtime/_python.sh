set -eu

python=$1
if command -v "$python" >/dev/null 2>&1; then
    exec "$python" -c 'import sys; assert sys.version_info.major == 3, "Python 3 is required"'
fi
if [ "$python" != python3 ]; then
    echo "Configured Python executable not found: $python" >&2
    exit 1
fi

if [ "${mode-}" != finish ] && [ -n "${3-}" ]; then
    /bin/sh -c "$3"
fi

if [ -n "${2-}" ]; then
    /bin/sh -c "$2"
    exec "$python" -c 'import sys; assert sys.version_info.major == 3, "Python 3 is required"'
fi

if [ "$(id -u)" = 0 ]; then
    set --
elif command -v sudo >/dev/null 2>&1; then
    set -- sudo -n
else
    echo "Installing Python 3 requires root or passwordless sudo inside the container" >&2
    exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
    if [ "${mode-}" != finish ]; then
        "$@" apt-get update
    fi
    if [ "${mode-}" = plan ]; then
        echo ENVWEAVER_APT_DOWNLOADS
        "$@" apt-get -o Acquire::ForceHash=sha256 --print-uris install -y --no-install-recommends python3 ca-certificates
        exit
    fi
    trap '"$@" rm -rf -- "$archives"' EXIT
    "$@" env DEBIAN_FRONTEND=noninteractive apt-get -o "Dir::Cache::archives=$archives" install -y --no-install-recommends python3 ca-certificates
elif command -v apk >/dev/null 2>&1; then
    "$@" apk add --no-cache python3 ca-certificates
elif command -v dnf >/dev/null 2>&1; then
    "$@" dnf install -y python3 ca-certificates
elif command -v yum >/dev/null 2>&1; then
    "$@" yum install -y python3 ca-certificates
else
    echo "Python 3 is missing; automatic installation requires apt-get, apk, dnf or yum" >&2
    exit 1
fi

"$python" -c 'import sys; assert sys.version_info.major == 3, "Python 3 is required"'
