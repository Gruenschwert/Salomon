FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY pyproject.toml ./
COPY app ./app
RUN pip install .

COPY . .

RUN useradd --create-home --uid 1000 gs
USER gs

CMD ["sh", "-c", "alembic upgrade head && exec python -m app.main"]
