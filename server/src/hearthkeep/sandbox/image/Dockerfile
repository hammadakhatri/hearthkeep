# Hearthkeep's Python sandbox. Built by `hearthkeep sandbox build --data <data>`; the tag
# is a hash of this folder, so any change here names a new image.
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

RUN useradd --uid 10001 --create-home hk

COPY hk_entry.py /opt/hk/hk_entry.py
COPY hk /opt/hk/hk
COPY tests /opt/hk/tests
RUN chown -R root:root /opt/hk && chmod -R u=rwX,go=rX /opt/hk

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/hk PYTHONUNBUFFERED=1 LANG=C.UTF-8
WORKDIR /tmp
USER 10001:10001
ENTRYPOINT ["python", "-I", "-B", "/opt/hk/hk_entry.py"]
