#!/bin/bash
cd /opt/x-backend
source .venv/bin/activate
export PYTHONPATH=/opt/x-backend
exec python -m app.service.worker
