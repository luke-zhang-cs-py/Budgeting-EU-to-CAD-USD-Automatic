# A deployment image. Built slim because the whole app is a few hundred KB of
# Python; the OCR engine is deliberately not installed here -- see below.
FROM python:3.12-slim

# curl is a real dependency, not a convenience: fxrates falls back to it when
# urllib cannot verify a certificate chain, which is the case behind some
# corporate proxies. Without it a rate refresh silently has one way to fail
# instead of two ways to succeed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first, so a code change does not reinstall the world.
COPY requirements.txt requirements-serve.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-serve.txt

# The screenshot reader is ~60 MB of ONNX runtime and model. Left out of the
# image on purpose: uploading still works without it and stores the picture,
# and most hosts charge for the image size. Add this line if you want it.
# RUN pip install --no-cache-dir -r requirements-ocr.txt
COPY requirements-ocr.txt ./

COPY . .

# WALLET_DATA must point at a mounted volume. Without one, the ledger and the
# receipts live in the container's own filesystem and are destroyed by the
# next deploy -- which is the single most likely way to lose this data, and
# the reason it is set here rather than left to a default.
ENV WALLET_DATA=/data
VOLUME ["/data"]

ENV HOST=0.0.0.0
ENV PORT=8000
EXPOSE 8000

# Two workers, one thread each, and a generous timeout for the one slow
# request there is: the ECB history download.
#
# --preload is deliberately NOT used. It would run auth.guard once in the
# parent, and a worker that respawns later would skip the check.
CMD ["sh", "-c", "gunicorn wsgi:application \
     --bind 0.0.0.0:${PORT:-8000} \
     --workers 2 --timeout 120 \
     --access-logfile - --error-logfile -"]
