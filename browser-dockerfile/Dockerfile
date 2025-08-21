FROM --platform=linux/amd64 registry.digitalocean.com/resourceloop/coder:devlaptop-v1.6.0

# Switch to root to install packages
USER root

# Update package lists and install base dependencies including GTK3 libraries
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    x11vnc \
    xvfb \
    fluxbox \
    xterm \
    wget \
    curl \
    gpg \
    libgtk-3-0 \
    libdbus-glib-1-2 \
    libxt6 \
    libwayland-client0 \
    lynx \
    w3m \
    && rm -rf /var/lib/apt/lists/*

# Download and install Firefox directly
RUN wget -O firefox.tar.xz "https://download.mozilla.org/?product=firefox-latest&os=linux64&lang=en-US" && \
    tar -xJf firefox.tar.xz -C /opt/ && \
    ln -sf /opt/firefox/firefox /usr/local/bin/firefox-real && \
    rm firefox.tar.xz

# Create browser wrapper script that tries different browsers
RUN echo '#!/bin/bash\n\
export DISPLAY=${DISPLAY:-:99}\n\
\n\
# Check for available browsers in order of preference\n\
BROWSERS=(\n\
    "/opt/firefox/firefox"\n\
    "/usr/local/bin/firefox-real"\n\
    "/usr/bin/firefox-esr"\n\
    "/usr/bin/chromium-browser"\n\
    "/usr/bin/lynx"\n\
)\n\
\n\
BROWSER_CMD=""\n\
for browser in "${BROWSERS[@]}"; do\n\
    if [ -x "$browser" ]; then\n\
        BROWSER_CMD="$browser"\n\
        break\n\
    fi\n\
done\n\
\n\
if [ -z "$BROWSER_CMD" ]; then\n\
    echo "No browser found"\n\
    exit 1\n\
fi\n\
\n\
# For GUI browsers, ensure X display is available\n\
if [[ "$BROWSER_CMD" != *"lynx"* ]]; then\n\
    if ! xdpyinfo -display $DISPLAY >/dev/null 2>&1; then\n\
        echo "X11 display $DISPLAY not available"\n\
        exit 1\n\
    fi\n\
fi\n\
\n\
# Launch browser based on type with stealth configuration\n\
if [[ "$BROWSER_CMD" == *"firefox"* ]]; then\n\
    # Create a clean profile directory\n\
    PROFILE_DIR="/tmp/firefox-profile-$$"\n\
    mkdir -p "$PROFILE_DIR"\n\
    \n\
    # Configure Firefox for stealth browsing\n\
    cat > "$PROFILE_DIR/user.js" << EOF\n\
// Disable WebRTC to prevent IP leaks\n\
user_pref("media.peerconnection.enabled", false);\n\
user_pref("media.peerconnection.ice.default_address_only", true);\n\
user_pref("media.peerconnection.ice.no_host", true);\n\
\n\
// Spoof user agent and platform\n\
user_pref("general.useragent.override", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/109.0");\n\
user_pref("general.platform.override", "Win32");\n\
user_pref("general.oscpu.override", "Windows NT 10.0; Win64; x64");\n\
\n\
// Disable telemetry and tracking\n\
user_pref("toolkit.telemetry.enabled", false);\n\
user_pref("toolkit.telemetry.unified", false);\n\
user_pref("datareporting.healthreport.service.enabled", false);\n\
user_pref("datareporting.healthreport.uploadEnabled", false);\n\
user_pref("datareporting.policy.dataSubmissionEnabled", false);\n\
\n\
// Privacy settings\n\
user_pref("privacy.trackingprotection.enabled", true);\n\
user_pref("privacy.donottrackheader.enabled", true);\n\
user_pref("privacy.resistFingerprinting", true);\n\
user_pref("privacy.clearOnShutdown.everything", true);\n\
\n\
// Disable geolocation\n\
user_pref("geo.enabled", false);\n\
user_pref("geo.provider.network.url", "");\n\
\n\
// DNS settings\n\
user_pref("network.dns.disablePrefetch", true);\n\
user_pref("network.prefetch-next", false);\n\
\n\
// Disable automatic connections\n\
user_pref("network.http.speculative-parallel-limit", 0);\n\
user_pref("browser.safebrowsing.enabled", false);\n\
user_pref("browser.safebrowsing.malware.enabled", false);\n\
\n\
// Canvas and WebGL fingerprinting protection\n\
user_pref("webgl.disabled", true);\n\
user_pref("privacy.resistFingerprinting.block_mozAddonManager", true);\n\
\n\
// Timezone spoofing\n\
user_pref("privacy.resistFingerprinting.jsmloglevel", "Warn");\n\
\n\
// Disable service workers\n\
user_pref("dom.serviceWorkers.enabled", false);\n\
EOF\n\
    \n\
    exec "$BROWSER_CMD" --profile "$PROFILE_DIR" --no-remote "$@"\n\
elif [[ "$BROWSER_CMD" == *"chromium"* ]]; then\n\
    exec "$BROWSER_CMD" --no-sandbox --disable-dev-shm-usage --disable-gpu --disable-web-security --disable-features=VizDisplayCompositor --user-agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36" "$@"\n\
else\n\
    exec "$BROWSER_CMD" "$@"\n\
fi\n\
' > /usr/local/bin/browser && chmod +x /usr/local/bin/browser

# Switch back to the original user
USER 1000