FROM python:3.10-slim

WORKDIR /workspace

RUN pip install --no-cache-dir pytest==9.1.1 requests==2.34.2 psycopg2-binary==2.9.10

COPY pytest.ini .
COPY conftest.py .
COPY tier1_regression_test.py .
COPY tier2_chaos_test.py .
COPY tests/ tests/

ENTRYPOINT ["pytest", "-v"]
