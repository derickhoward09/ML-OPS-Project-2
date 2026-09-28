#!/bin/bash

URL="http://paffenroth-23.dyn.wpi.edu:8001"

if curl \
    --fail \
    --silent \
    --max-time 8 \
    "$URL" \
    >/dev/null
then
    echo "$(date): ResumeLens is healthy."
    exit 0
else
    echo "$(date): ResumeLens is unavailable."
    exit 1
fi
