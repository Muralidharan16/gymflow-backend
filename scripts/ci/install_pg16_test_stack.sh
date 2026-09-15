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

# P6-F compatibility bridge: the inherited P5-D/P5-W2 jobs deliberately spawn
# real production-profile Celery workers while their pytest controllers retain
# the original local broker fault surface. Replace only those jobs' disposable
# Redis service with a dual-port TLS/auth + replicated topology. A test-only
# sitecustomize then switches only the production worker subprocess to rediss.
#
# P6-W deliberately carries P5W2_PROCESS_FAULTS=1 to reuse inherited W2
# contracts, but it provisions and verifies its own P6 TLS Redis topology later
# in the workflow. Never activate this inherited-P5 bridge for the P6-W job.
if [[ "${P6W_PROCESS_FAULTS:-0}" != "1" ]] && \
   [[ "${P5D_PROCESS_FAULTS:-0}" == "1" || "${P5W2_PROCESS_FAULTS:-0}" == "1" ]]; then
  bash "$(dirname "${BASH_SOURCE[0]}")/provision_p6_fault_redis.sh"
fi
