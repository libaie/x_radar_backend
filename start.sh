#!/bin/bash
cd /opt/x-backend
source .venv/bin/activate
export PYTHONPATH=/opt/x-backend
exec uvicorn app.main:app --host 0.0.0.0 --port 15001 --ws websockets
