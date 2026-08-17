# Standard library only, so there is nothing to install and no build stage.
# Pinned to a minor version rather than :3 so an image rebuild cannot quietly
# move Python underneath a running deployment.
FROM python:3.12-slim

# Telemetry is timestamped in local time throughout -- ranges, schedules and
# degree-days are all local-midnight aligned -- so the container needs the real
# zone rather than UTC, or "today" means something different inside and out.
ENV TZ=America/New_York
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Unbuffered so the action log reaches `docker logs` as it happens rather than
# in 8 KB bursts; no .pyc litter in a layer that is read-only anyway.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY windmill/ /app/windmill/

# State lives here and nowhere else: database, backups, .env. Mount it.
ENV WINDMILL_DATA=/data \
    WINDMILL_BIND=0.0.0.0 \
    WINDMILL_RELOAD=0
RUN mkdir -p /data

# Not root. The only thing this writes is /data, so that is the only thing it
# needs to own.
RUN useradd --system --uid 10001 --home /app windmill \
 && chown -R windmill:windmill /app /data
USER windmill
VOLUME ["/data"]
EXPOSE 8787

# Asks the app a question only a working app can answer -- the sampler and the
# database both have to be alive for /api/summary to return.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python3 -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/summary?range=today',timeout=8).status==200 else 1)"

CMD ["python3", "-m", "windmill"]
