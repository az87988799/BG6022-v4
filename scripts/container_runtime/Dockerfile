ARG BASE_IMAGE
ARG COMPILER_IMAGE
FROM ${COMPILER_IMAGE} AS compiler
USER 0:0
COPY sandbox.c /build/runtime-sandbox.c
RUN cc -O2 -Wall -Wextra -Werror -shared -fPIC \
      -o /build/sandbox.so /build/runtime-sandbox.c

FROM ${BASE_IMAGE} AS install
USER 0:0
COPY wheelhouse /build/wheelhouse
COPY requirements-linux-cp311.lock wheelhouse-manifest.json install_runtime.py orca_task_sdk.py /build/
RUN /usr/bin/python3.11 -I /build/install_runtime.py

FROM ${BASE_IMAGE} AS runtime
USER 0:0
COPY --from=install /opt/orca-agent-runtime/venv /opt/orca-agent-runtime/venv
COPY --from=compiler /build/sandbox.so /opt/orca-agent-runtime/sandbox.so
COPY helper.py python_task.py /opt/orca-agent-runtime/
COPY license-manifest.json wheel-license-evidence.json /opt/orca-agent-runtime/provenance/
COPY wheelhouse-manifest.json write_runtime_manifest.py sandbox.c /build/
RUN mkdir -p /work/task /work/requests /work/native /work/control /work/sealed /work/responses /work/gates \
    && chown 10001:10001 /work/task /work/requests \
    && chown 10002:10002 /work/native \
    && chown 10003:10003 /work/control \
    && chmod 0700 /work/task /work/native /work/control \
    && chmod 0755 /work /work/requests /work/sealed /work/responses /work/gates \
    && chown root:10002 /opt/orca \
    && chmod 0750 /opt/orca \
    && chmod 0555 /opt/orca-agent-runtime/helper.py /opt/orca-agent-runtime/python_task.py /opt/orca-agent-runtime/sandbox.so \
    && dpkg-query -W > /opt/orca-agent-runtime/provenance/debian-packages.txt \
    && /usr/bin/python3.11 -I /build/write_runtime_manifest.py \
    && rm /build/write_runtime_manifest.py /build/wheelhouse-manifest.json /build/sandbox.c
ENV PATH=/usr/bin:/bin HOME=/tmp TMPDIR=/tmp
USER 10003:10003
WORKDIR /work/control
ENTRYPOINT ["/usr/bin/python3.11", "-I", "/opt/orca-agent-runtime/helper.py", "keeper"]
