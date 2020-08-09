FROM python:3.8

RUN apt-get update && apt-get install -y netcat && rm -rf /var/lib/apt/sources.list.d/*
RUN pip install poetry

WORKDIR /app/

COPY pyproject.toml poetry.lock /app/
RUN poetry install

COPY fetch.py /app/

ENTRYPOINT ["poetry", "run"]

