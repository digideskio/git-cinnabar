FROM debian:jessie-20170907
COPY sources.list /etc/apt
RUN apt-get update -o Acquire::Check-Valid-Until=false
RUN apt-get install -y --no-install-recommends ca-certificates curl python-pip python-wheel xz-utils && apt-get clean
COPY run-task.sh /bin
