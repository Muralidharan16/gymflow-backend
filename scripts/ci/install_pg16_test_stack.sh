#!/usr/bin/env bash
set -euo pipefail

sudo apt-get update
sudo apt-get install -y \
  postgresql-16 \
  postgresql-client-16 \
  postgresql-contrib \
  postgresql-16-partman \
  postgresql-16-postgis-3
sudo systemctl start postgresql
pg_config --version
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres -c 'SELECT version();'
test -f /usr/share/postgresql/16/extension/pg_partman.control
