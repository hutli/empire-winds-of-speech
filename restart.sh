#!/bin/sh
isort src
docker-compose down
docker-compose up --build -d
docker logs -f empire-winds-of-speech_winds-of-speech_1
#docker logs -f empire-winds-of-speech_winds-of-speech-podcast_1
