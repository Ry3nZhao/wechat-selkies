# syntax=docker/dockerfile:1
# WeChat for Linux using Selkies baseimage, plus the LoAgent OneBot v11 gateway (wechat-api).
#
# loagent-gateway branch: WeChat-only ("minimal") build with the WeChat package pinned by
# version and sha256 (no following upstream's automatic updates), wx-cli-again compiled from
# a pinned commit, and wechat-api served under /api/ on the Selkies web port.

# Optional local WeChat packages. LoAgent's compose file overrides this stage with the
# directory holding the vendored .deb (build context `wechat_vendor`); by default it only
# holds build/vendor/ from this repository.
FROM scratch AS wechat_vendor
COPY build/vendor/ /

# wx-cli-again has no releases: build it from a pinned commit, verified by sha256.
FROM rust:1.90-bookworm AS wxcli
ARG WX_CLI_COMMIT=077a54cbfe679bda963cd038d8440422907fc797
ARG WX_CLI_SHA256=e29ff8ec85dd4417d1a8ce8fad68baca49d637352a4ef1b25fc3db03e1b4c615
RUN curl -fsSL --retry 3 -o /tmp/wx-cli.tar.gz \
        "https://codeload.github.com/jackwener/wx-cli-again/tar.gz/${WX_CLI_COMMIT}" && \
    echo "${WX_CLI_SHA256}  /tmp/wx-cli.tar.gz" | sha256sum -c - && \
    mkdir -p /src && tar -xzf /tmp/wx-cli.tar.gz -C /src --strip-components=1
WORKDIR /src
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    cargo build --release --locked && \
    install -D -m 0755 target/release/wx /out/wx

# Base image pinned by digest (ubuntunoble, Python 3.12).
FROM ghcr.io/linuxserver/baseimage-selkies:ubuntunoble@sha256:7cab02f9222937ad1e704cd904534d69a2300b2406af466241817be0ce1d0493

# Metadata labels
LABEL org.opencontainers.image.title="WeChat Selkies (LoAgent gateway)"
LABEL org.opencontainers.image.description="WeChat Linux client in browser via Selkies WebRTC, with a OneBot v11 API"
LABEL org.opencontainers.image.authors="nickrunning"
LABEL org.opencontainers.image.source="https://github.com/RuihanRZhao/wechat-selkies"
LABEL org.opencontainers.image.documentation="https://github.com/RuihanRZhao/wechat-selkies/blob/loagent-gateway/docs/loagent-gateway.md"
LABEL org.opencontainers.image.vendor="WeChat Selkies Project"
LABEL org.opencontainers.image.licenses="MIT"

# Build arguments for multi-arch support
ARG TARGETPLATFORM
ARG BUILDPLATFORM
# Minimal build: WeChat only (no QQ, no file manager).
ARG INSTALL_QQ=false
ARG INSTALL_PCMANFM=false
RUN echo "🏗️ Building WeChat-Selkies on $BUILDPLATFORM, targeting $TARGETPLATFORM"

RUN apt-get update && \
    apt-get install -y fonts-noto-cjk libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-render-util0 libxcb-xkb1 libxkbcommon-x11-0 \
    shared-mime-info desktop-file-utils libxcb1 libxcb-icccm4 libxcb-image0 \
    libxcb-keysyms1 libxcb-randr0 libxcb-render0 libxcb-render-util0 libxcb-shape0 \
    libxcb-shm0 libxcb-sync1 libxcb-util1 libxcb-xfixes0 libxcb-xkb1 libxcb-xinerama0 \
    libxcb-xkb1 libxcb-glx0 libatk1.0-0 libatk-bridge2.0-0 libc6 libcairo2 libcups2 \
    libdbus-1-3 libfontconfig1 libgbm1 libgcc1 libgdk-pixbuf2.0-0 libglib2.0-0 \
    libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 libpangocairo-1.0-0 libstdc++6 libx11-6 \
    libxcomposite1 libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 libxrender1 \
    libxss1 libxtst6 libatomic1 libxcomposite1 libxrender1 libxrandr2 libxkbcommon-x11-0 \
    libfontconfig1 libdbus-1-3 libnss3 libx11-xcb1 stalonetray inotify-tools \
    xdotool xclip

ARG INSTALL_PCMANFM
RUN if [ "$INSTALL_PCMANFM" = "true" ]; then \
        apt-get install -y --no-install-recommends pcmanfm; \
    fi

RUN pip install --no-cache-dir python-xlib

# Install WeChat, pinned. The download URL carries no version and changes whenever Tencent
# ships a new build, so the sha256 is mandatory: a mismatch fails the build instead of
# silently upgrading. A local copy of the pinned package (wechat_vendor context) is used
# first when present. The program directory is made read-only afterwards: WeChat runs as
# abc and cannot replace itself.
ARG WECHAT_VERSION="4.1.13.26"
ARG WECHAT_AMD64_URL="https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_x86_64.deb"
ARG WECHAT_ARM64_URL="https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_arm64.deb"
ARG WECHAT_AMD64_SHA256="a0db8caeee8d1e6cfe0aa5ce58710270f8d74a90ff4920f53a3fc22aa0956fe9"
ARG WECHAT_ARM64_SHA256="374732eda88a6f3a7e4eb315e64e5fac73ef17923d504d242964f19ed1b80c75"
ARG WECHAT_DEB_FILE=""
RUN --mount=from=wechat_vendor,target=/vendor \
    set -e; \
    case "$TARGETPLATFORM" in \
    "linux/amd64") \
        WECHAT_URL="$WECHAT_AMD64_URL"; \
        WECHAT_SHA256="$WECHAT_AMD64_SHA256"; \
        WECHAT_ARCH="x86_64" ;; \
    "linux/arm64") \
        WECHAT_URL="$WECHAT_ARM64_URL"; \
        WECHAT_SHA256="$WECHAT_ARM64_SHA256"; \
        WECHAT_ARCH="arm64" ;; \
    *) \
        echo "❌ Unsupported platform: $TARGETPLATFORM" >&2; \
        echo "Supported platforms: linux/amd64, linux/arm64" >&2; \
        exit 1 ;; \
    esac; \
    [ -n "$WECHAT_SHA256" ] || { echo "❌ WECHAT_*_SHA256 must be set" >&2; exit 1; }; \
    LOCAL_DEB="/vendor/${WECHAT_DEB_FILE:-WeChatLinux_${WECHAT_ARCH}.deb}"; \
    if [ -f "$LOCAL_DEB" ]; then \
        echo "📦 Using local WeChat package $LOCAL_DEB"; \
        cp "$LOCAL_DEB" wechat.deb; \
    else \
        echo "📦 Downloading WeChat for $WECHAT_ARCH architecture..."; \
        curl -fsSL --connect-timeout 30 --max-time 600 --retry 3 --retry-delay 10 --retry-connrefused -o wechat.deb "$WECHAT_URL"; \
    fi; \
    if ! echo "$WECHAT_SHA256  wechat.deb" | sha256sum -c -; then \
        echo "❌ WeChat package sha256 does not match the pinned $WECHAT_VERSION build." >&2; \
        echo "   Tencent has probably replaced the package behind $WECHAT_URL." >&2; \
        echo "   Provide the pinned package locally: put it in build/vendor/ (or the wechat_vendor" >&2; \
        echo "   build context) as WeChatLinux_${WECHAT_ARCH}.deb, or name it with WECHAT_DEB_FILE." >&2; \
        exit 1; \
    fi; \
    echo "ℹ️ Package $(dpkg-deb -f wechat.deb Package) version $(dpkg-deb -f wechat.deb Version), pinned $WECHAT_VERSION"; \
    echo "🔧 Installing WeChat..."; \
    (dpkg -i wechat.deb || (apt-get update && apt-get install -f -y && dpkg -i wechat.deb)); \
    PKG="$(dpkg-deb -f wechat.deb Package)"; \
    apt-mark hold "$PKG"; \
    for dir in $(dpkg -L "$PKG" | grep -E '^/opt/[^/]+$'); do \
        chown -R root:root "$dir" && chmod -R a-w "$dir"; \
    done; \
    mkdir -p /etc/wechat-selkies && \
    printf 'WECHAT_PACKAGE=%s\nWECHAT_VERSION=%s\nWECHAT_SHA256=%s\n' "$PKG" "$WECHAT_VERSION" "$WECHAT_SHA256" > /etc/wechat-selkies/pinned-wechat.env; \
    rm -f wechat.deb; \
    echo "✅ WeChat $WECHAT_VERSION installation completed for $WECHAT_ARCH"

# Install QQ based on target architecture (optional)
ARG INSTALL_QQ
ARG QQ_AMD64_URL="https://qqdl.gtimg.cn/qqfile/QQNT/9.9.33/release/c97651b2/QQ_3.2.32_260730_amd64_01.deb"
ARG QQ_ARM64_URL="https://qqdl.gtimg.cn/qqfile/QQNT/9.9.33/release/c97651b2/QQ_3.2.32_260730_arm64_01.deb"
ARG QQ_AMD64_SHA256
ARG QQ_ARM64_SHA256
RUN if [ "$INSTALL_QQ" = "true" ]; then \
        case "$TARGETPLATFORM" in \
        "linux/amd64") \
            QQ_URL="$QQ_AMD64_URL"; \
            QQ_SHA256="$QQ_AMD64_SHA256"; \
            QQ_ARCH="x86_64" ;; \
        "linux/arm64") \
            QQ_URL="$QQ_ARM64_URL"; \
            QQ_SHA256="$QQ_ARM64_SHA256"; \
            QQ_ARCH="arm64" ;; \
        *) \
            echo "❌ Unsupported platform: $TARGETPLATFORM" >&2; \
            exit 1 ;; \
        esac && \
        echo "📦 Downloading QQ for $QQ_ARCH architecture..." && \
        curl -fsSL --connect-timeout 30 --max-time 600 --retry 3 --retry-delay 10 --retry-connrefused -o qq.deb "$QQ_URL" && \
        if [ -n "$QQ_SHA256" ]; then echo "$QQ_SHA256  qq.deb" | sha256sum -c -; fi && \
        echo "🔧 Installing QQ..." && \
        (dpkg -i qq.deb || (apt-get update && apt-get install -f -y && dpkg -i qq.deb)) && \
        rm -f qq.deb && \
        echo "✅ QQ installation completed for $QQ_ARCH"; \
    else \
        echo "⏭️ Skipping QQ installation (INSTALL_QQ=$INSTALL_QQ)"; \
    fi

# wx-cli-again (read-only use by wechat-api; the user sets it up by hand, see docs).
COPY --from=wxcli /out/wx /usr/local/bin/wx

# wechat-api runtime: its own virtualenv on the system Python, from the locked dependencies.
COPY wechat-api/pyproject.toml wechat-api/uv.lock /opt/wechat-api/
COPY wechat-api/src /opt/wechat-api/src
RUN --mount=from=ghcr.io/astral-sh/uv:0.8.4,source=/uv,target=/usr/local/bin/uv \
    cd /opt/wechat-api && \
    UV_PROJECT_ENVIRONMENT=/opt/wechat-api/.venv UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_NO_CACHE=1 \
    uv sync --frozen --no-dev --no-editable --compile-bytecode && \
    /opt/wechat-api/.venv/bin/python -c "import wechat_api, aiohttp, PIL; print('wechat-api', wechat_api.__version__)" && \
    wx --version

# nginx: forward /api/ to wechat-api, outside the Selkies basic auth (wechat-api checks its
# own access_token). init-nginx strips every '#' when PASSWORD is set, so the snippet has none.
ARG WECHAT_API_PORT=8095
COPY build/nginx-api-location.conf /tmp/nginx-api-location.conf
RUN sed -i "s/WECHAT_API_PORT/${WECHAT_API_PORT}/" /tmp/nginx-api-location.conf && \
    ! grep -q '#' /tmp/nginx-api-location.conf && \
    sed -i '/^  error_page 500 502 503 504/r /tmp/nginx-api-location.conf' /defaults/default.conf && \
    test "$(grep -c 'location SUBFOLDERapi/' /defaults/default.conf)" = 2 && \
    rm /tmp/nginx-api-location.conf

# Clean up
RUN apt-get purge -y --autoremove
RUN apt-get autoclean && \
    rm -rf \
        /config/.cache \
        /config/.npm \
        /var/lib/apt/lists/* \
        /var/tmp/* \
        /tmp/*

# configure openbox dock mode for stalonetray
RUN sed -i '/<dock>/,/<\/dock>/s/<noStrut>no<\/noStrut>/<noStrut>yes<\/noStrut>/' /etc/xdg/openbox/rc.xml

# set app name
ENV TITLE="WeChat-Selkies"
ENV TZ="Asia/Shanghai"
ENV LC_ALL="zh_CN.UTF-8"
ENV AUTO_START_WECHAT="true"
ENV AUTO_START_QQ="false"
ENV ENABLE_WECHAT_NIGHTLY_RESTART="false"
ENV WECHAT_NIGHTLY_STOP_TIME="23:30"
ENV WECHAT_NIGHTLY_START_TIME="01:30"
ENV ENABLE_WECHAT_AUTO_LOGIN="true"
ENV AUTO_LOGIN_DELAY="3"
ENV WECHAT_API_PORT="${WECHAT_API_PORT}"
ENV ONEBOT_REVERSE_URL="ws://astrbot:6199/ws"

# update favicon
RUN cp /usr/share/icons/hicolor/128x128/apps/wechat.png /usr/share/selkies/www/icon.png

# add local files
COPY /root /
