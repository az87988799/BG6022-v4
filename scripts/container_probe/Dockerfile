# Development compatibility probe only. Supply a verified official Debian digest.
ARG DEBIAN_IMAGE
FROM ${DEBIAN_IMAGE} AS build
ARG DEBIAN_IMAGE
RUN printf '%s\n' "$DEBIAN_IMAGE" | grep -Eq '^debian:bookworm-slim@sha256:[0-9a-f]{64}$'

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
# Fixed to the version linked by the official ORCA 6.1.1 Linux package.
RUN curl --fail --location --proto '=https' --tlsv1.2 --retry 2 \
      --connect-timeout 30 --max-time 300 \
      https://download.open-mpi.org/release/open-mpi/v4.1/openmpi-4.1.8.tar.gz \
      --output openmpi.tar.gz \
    && printf '%s\n' 'fb41086bbed9300baa2f3d7572491facfe5257412fa524ec5a396aa9101d5c62  openmpi.tar.gz' | sha256sum --check --strict \
    && tar -xzf openmpi.tar.gz \
    && cd openmpi-4.1.8 \
    && ./configure --prefix=/opt/openmpi \
         --disable-mpi-fortran --disable-mpi-cxx --disable-oshmem \
         --with-libevent=internal --with-hwloc=internal --with-pmix=internal \
    && make -j2 \
    && make install \
    && mkdir -p /opt/probe-provenance \
    && cp LICENSE /opt/probe-provenance/openmpi-LICENSE \
    && cp config.log /opt/probe-provenance/openmpi-config.log \
    && /opt/openmpi/bin/ompi_info --all --parsable > /opt/probe-provenance/ompi-info.txt \
    && dpkg-query -W > /opt/probe-provenance/build-packages.txt

# The build context is scripts/container_probe, never the product repository.
COPY mpi_probe.c /build/mpi_probe.c
RUN /opt/openmpi/bin/mpicc -std=c11 -O2 -Wall -Wextra -Werror \
      /build/mpi_probe.c -o /opt/mpi_probe \
    && sha256sum /build/mpi_probe.c /opt/mpi_probe > /opt/probe-provenance/probe-sha256.txt

FROM ${DEBIAN_IMAGE} AS runtime
ARG DEBIAN_IMAGE
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgcc-s1 libstdc++6 zlib1g python3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 probe \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin probe
COPY --from=build /opt/openmpi /opt/openmpi
COPY --from=build /opt/mpi_probe /opt/mpi_probe
COPY --from=build /opt/probe-provenance /opt/probe-provenance
# Separate developer-authored boundary checks; never part of the product runtime.
COPY boundary_probe.py /opt/boundary_probe.py
RUN sha256sum /opt/boundary_probe.py > /opt/probe-provenance/boundary-probe-sha256.txt \
    && dpkg-query -W > /opt/probe-provenance/runtime-packages.txt \
    && printf '%s\n' "$DEBIAN_IMAGE" > /opt/probe-provenance/debian-image.txt \
    && printf '%s\n' 'https://download.open-mpi.org/release/open-mpi/v4.1/openmpi-4.1.8.tar.gz' \
      'sha256:fb41086bbed9300baa2f3d7572491facfe5257412fa524ec5a396aa9101d5c62' \
      > /opt/probe-provenance/openmpi-source.txt
LABEL org.opencontainers.image.title="Local Open MPI compatibility probe" \
      org.opencontainers.image.version="4.1.8" \
      org.opencontainers.image.description="Development probe; no ORCA and no production backend" \
      org.opencontainers.image.base.name="${DEBIAN_IMAGE}"
ENV PATH=/opt/openmpi/bin:/usr/bin:/bin \
    LD_LIBRARY_PATH=/opt/openmpi/lib \
    OMPI_MCA_plm=isolated \
    HOME=/tmp \
    TMPDIR=/tmp
USER 10001:10001
WORKDIR /tmp
# 15 s MPI deadline; an outer TERM/KILL deadline ends the process within 20 s.
# Docker runtime restrictions must also be supplied as documented in README.
ENTRYPOINT ["/usr/bin/timeout", "--signal=TERM", "--kill-after=2s", "18s"]
CMD ["/opt/openmpi/bin/mpirun", "--timeout", "15", "--disable-recovery", "--host", "localhost:4", "--bind-to", "none", "--mca", "pml", "ob1", "--mca", "btl", "self,vader", "--mca", "btl_vader_single_copy_mechanism", "none", "-n", "4", "/opt/mpi_probe"]
