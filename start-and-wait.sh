#!/bin/bash

docker-compose up --build -d yfinance_app

echo "Waiting for debug port 5678..."
while ! (echo > /dev/tcp/localhost/5678) >/dev/null 2>&1; do
  sleep 0.5
done

echo "Debug port is open."
