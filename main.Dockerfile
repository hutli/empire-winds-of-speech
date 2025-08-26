FROM python:3.13-rc-slim
WORKDIR /app/

# install binaries - ffmpeg (or avconv) needed by pydub (to be fast)
RUN apt-get update -y
RUN apt-get install ffmpeg gcc portaudio19-dev -y

# install dependencies
COPY ./requirements.txt /tmp/
RUN pip install -r /tmp/requirements.txt

# copy entrypoint files
COPY ./log_conf.json /app/

# copy in app source
COPY ./src/main.py /app/src/main.py
COPY ./src/utils.py /app/src/utils.py

# test application
COPY ./mypy.ini /app/
RUN mypy src --config-file mypy.ini

# run application
ENTRYPOINT ["uvicorn", "src.main:APP", "--host", "0.0.0.0", "--port", "80", "--use-colors", "--log-config", "log_conf.json"]