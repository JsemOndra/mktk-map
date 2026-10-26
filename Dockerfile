FROM python:3.12-slim
# unprivileged user the app runs as (high uid so it does not match a user on the host)
RUN useradd --uid 10001 --user-group --home-dir /srv --no-create-home --shell /usr/sbin/nologin app
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh
COPY app/ ./app/
WORKDIR /srv/app
EXPOSE 8080
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
