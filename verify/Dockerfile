FROM python:3.11-slim

WORKDIR /verify

RUN pip install --no-cache-dir requests==2.32.3

COPY run_verify.py .

CMD ["python", "-u", "/verify/run_verify.py"]
