# hyvv `acceptance` container class: the ordinary bee image plus what CI's
# secret-scan and acceptance-light jobs need, provisioned at build time so the
# jobs run offline. Run it ONLY with seccomp/acceptance.json: no privileged
# mode, no added capabilities, and seccomp/AppArmor confinement left on.
#
#   docker build -t hyvv-acceptance:gl8.21.2-node24-oc1.18.32 images/acceptance
ARG BASE=cua-bee:node24-oc1.18.32
FROM ${BASE}

# gitleaks 8.21.2, sha256 from the official gitleaks_8.21.2_checksums.txt
# (https://github.com/gitleaks/gitleaks/releases/tag/v8.21.2).
ARG GITLEAKS_VERSION=8.21.2
ARG GITLEAKS_SHA256=5bc41815076e6ed6ef8fbecc9d9b75bcae31f39029ceb55da08086315316e3ba

RUN set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    apt-get install -y --no-install-recommends bubblewrap ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*; \
    curl -fsSL -o /tmp/gitleaks.tar.gz \
      "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"; \
    echo "${GITLEAKS_SHA256}  /tmp/gitleaks.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/gitleaks.tar.gz -C /usr/local/bin gitleaks; \
    chmod 0755 /usr/local/bin/gitleaks; \
    rm -f /tmp/gitleaks.tar.gz; \
    gitleaks version; \
    bwrap --version

LABEL org.opencontainers.image.title="hyvv-acceptance" \
      io.hyvv.security-profile="acceptance" \
      io.hyvv.gitleaks.version="${GITLEAKS_VERSION}" \
      io.hyvv.gitleaks.sha256="${GITLEAKS_SHA256}"
