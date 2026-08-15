# LitFin — always-on deployment.
#
# Two processes share this image: a scheduler running the pipeline on cron,
# and the web panel. They are separate services in compose so the panel can be
# restarted without interrupting a collection run, and so the panel can be
# given --read-only while the scheduler keeps full rights.
FROM python:3.13-slim AS base

# tini: the container's PID 1 must reap zombies, or a few weeks of cron jobs
# leaves a process table full of defunct children.
# cron: the scheduler service uses it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini cron ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a source edit does not re-resolve the whole tree.
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir . openpyxl

COPY litfin.toml ./
COPY deploy/ ./deploy/

# Data lives on a mounted volume, NEVER in the image layer. A container
# rebuild must not lose the corpus, the watermarks, or the extraction spend
# they represent.
ENV LITFIN_DATA_ROOT=/data
RUN mkdir -p /data

# Run as a non-root user. The pipeline needs no privileges, and the panel is
# reachable from the network.
RUN useradd --create-home --uid 10001 litfin \
    && chown -R litfin:litfin /app /data

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["litfin", "--help"]
