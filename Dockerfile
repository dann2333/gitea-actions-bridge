FROM python:3.13-alpine
WORKDIR /app
COPY app.py .
RUN mkdir /data && chown 65534:65534 /data
ENV SQLITE_PATH=/data/bridge.db
VOLUME ["/data"]
USER 65534:65534
EXPOSE 8080
CMD ["python", "-u", "app.py"]
