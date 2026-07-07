# Shared investigation/tooling container for the agent's run_bash.
# Runs on the same compose network as the archetype stack so the agent can
# reach services by name (e.g. http://ledger-api:8080, postgres:5432) with a
# real Linux shell — fixing the Windows-host shell confound that made
# Archetype D Test 3 uninterpretable in Phase 5.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        postgresql-client \
        ca-certificates \
        procps \
        iproute2 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir requests psycopg2-binary

WORKDIR /workspace
CMD ["sleep", "infinity"]
