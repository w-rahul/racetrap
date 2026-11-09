FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/w-rahul/racetrap"
LABEL org.opencontainers.image.description="Finds race conditions in web APIs by forcing the worst order of their SQL statements."
LABEL org.opencontainers.image.licenses="MIT"

COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
RUN pip install --no-cache-dir /src && rm -rf /src

WORKDIR /work
ENTRYPOINT ["racetrap"]
