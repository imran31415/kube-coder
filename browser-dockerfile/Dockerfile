FROM --platform=linux/amd64 registry.digitalocean.com/resourceloop/coder:devlaptop-v1.6.0

# Switch to root to install packages
USER root

# Update package lists and install base dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    x11vnc \
    xvfb \
    fluxbox \
    xterm \
    wget \
    curl \
    gpg \
    && rm -rf /var/lib/apt/lists/*

# Download and install Firefox directly
RUN wget -O firefox.tar.xz "https://download.mozilla.org/?product=firefox-latest&os=linux64&lang=en-US" && \
    tar -xJf firefox.tar.xz -C /opt/ && \
    ln -s /opt/firefox/firefox /usr/local/bin/firefox && \
    rm firefox.tar.xz

# Install lightweight text browsers as backup
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    lynx \
    w3m \
    && rm -rf /var/lib/apt/lists/*

# Create browser wrapper script that tries different browsers
RUN echo '#!/bin/bash\n\
export DISPLAY=${DISPLAY:-:99}\n\
\n\
# Try browsers in order of preference\n\
for browser in "firefox --safe-mode" "chromium-browser --no-sandbox --disable-gpu" lynx w3m; do\n\
    cmd=$(echo $browser | cut -d" " -f1)\n\
    if which $cmd >/dev/null 2>&1; then\n\
        echo "Starting $browser"\n\
        exec $browser "$@"\n\
    fi\n\
done\n\
\n\
echo "No supported browser found"\n\
exit 1\n\
' > /usr/local/bin/browser && chmod +x /usr/local/bin/browser

# Switch back to the original user
USER 1000

# Test that browser works
RUN browser --version || echo "Browser installed but may need X11"