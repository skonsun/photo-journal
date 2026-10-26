FROM python:3.13-slim
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG VERSION=0.1.0
LABEL org.opencontainers.image.title="Photo Journal" \
      org.opencontainers.image.description="A self-hosted, folder-synced photo gallery" \
      org.opencontainers.image.source="https://github.com/skonsun/photo-journal" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --disable-pip-version-check --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt
COPY --chmod=0644 server.py gallery.html admin.html /app/
# Some NAS filesystems preserve restrictive POSIX ACLs during COPY.
RUN python -c "import os; files=['/app/server.py','/app/gallery.html','/app/admin.html']; [(os.removexattr(p,'system.posix_acl_access') if 'system.posix_acl_access' in os.listxattr(p) else None,os.chmod(p,0o644)) for p in files]" \
    && mkdir -p /data /photos && chown 1000:1000 /data /photos
USER 1000:1000
EXPOSE 8789
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8789')+'/health',timeout=3)"
CMD ["python", "server.py"]
