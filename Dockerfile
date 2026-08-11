# The data API (server.py) as a container, for a managed host.
#
# The database is NOT in here: on a free tier a container's filesystem does not
# survive a restart, so MySQL is a managed service and this image holds only
# the stateless part. See DEPLOY.md.
FROM python:3.12-slim

# mysqldump and mysql, which dbdump.py drives for `db:export` / `db:import`.
# Without them the Database page's export and restore stop working.
RUN apt-get update \
 && apt-get install -y --no-install-recommends default-mysql-client \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first: this layer is rebuilt only when requirements.txt changes,
# not on every code edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 0.0.0.0 so the platform's router can reach us -- the default 127.0.0.1 would
# pass no health check. 7860 is Hugging Face Spaces' default port; Render, Koyeb
# and Fly inject their own $PORT, which server.py prefers over this.
ENV HOST=0.0.0.0 \
    PORT=7860 \
    PYTHONUNBUFFERED=1 \
    MYSQL_SSL=1

EXPOSE 7860

# Not `npm run start:server`: there is no Node in this image, and nothing here
# starts a database -- server.py connects to the managed one.
CMD ["python", "-u", "server.py"]
