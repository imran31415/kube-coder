# Remote Browser Architecture Documentation
<img width="1336" height="848" alt="image" src="https://github.com/user-attachments/assets/c9a557e5-54aa-4915-b848-0d514227f142" />


## Overview

This document describes the comprehensive architecture for the remote browser functionality integrated into the Kubernetes-based development environment. The system enables users to launch and interact with a full GUI Firefox browser through a web interface, utilizing virtual display technology and VNC for remote access.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Core Components](#core-components)
3. [Technology Stack](#technology-stack)
4. [Implementation Details](#implementation-details)
5. [Data Flow](#data-flow)
6. [Agent Display (`:98`)](#agent-display-98)
7. [Security Considerations](#security-considerations)
8. [Troubleshooting Guide](#troubleshooting-guide)

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                        User Browser                          │
└─────────────────────┬───────────────────────────────────────┘
                      │ HTTPS
┌─────────────────────▼───────────────────────────────────────┐
│                    NGINX Ingress Controller                  │
│  Routes: /oauth/*, /browser, /vnc-direct/*, /websockify      │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────┐
│                    Kubernetes Service                        │
│            Ports: 8080, 7681, 6080, 6081                    │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────┐
│                  Kubernetes Pod (ws-{user})                  │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │                  Container: IDE                         │ │
│  │                                                         │ │
│  │  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐ │ │
│  │  │   Xvfb      │  │   x11vnc     │  │   Fluxbox    │ │ │
│  │  │  Display:99 │◄─│  Port: 5900  │  │ Window Mgr   │ │ │
│  │  └─────────────┘  └──────┬───────┘  └──────────────┘ │ │
│  │         ▲                 │                            │ │
│  │         │                 ▼                            │ │
│  │  ┌──────┴──────┐  ┌──────────────┐  ┌──────────────┐ │ │
│  │  │   Firefox   │  │   noVNC      │  │  Browser API │ │ │
│  │  │  Browser    │  │  Port: 6081  │  │  Port: 6080  │ │ │
│  │  └─────────────┘  └──────────────┘  └──────────────┘ │ │
│  └────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

## Core Components

### 1. Virtual Display Server (Xvfb)
**Purpose**: Provides a virtual X11 display for GUI applications without physical hardware

**Configuration**:
- Display number: `:99`
- Resolution: `1280x720x24` (24-bit color depth)
- Process: Runs as background daemon
- Memory: Uses framebuffer in system memory

**How it works**:
- Creates virtual framebuffer in memory
- Implements full X11 protocol
- Applications connect via `DISPLAY=:99` environment variable
- Renders GUI elements to memory instead of physical screen

### 2. VNC Server (x11vnc)
**Purpose**: Captures the virtual display and serves it via VNC protocol

**Configuration**:
- Connects to: Display `:99`
- Listen port: `5900` (localhost only for security)
- Options: `-nopw -forever -shared -ncache 10`

**How it works**:
- Monitors X11 display for changes using X DAMAGE extension
- Captures framebuffer content
- Serves via RFB (Remote Framebuffer) protocol
- Supports multiple concurrent connections (`-shared`)
- Implements client-side pixel caching (`-ncache`)

### 3. Web VNC Client (noVNC)
**Purpose**: Provides browser-based VNC client without plugins

**Configuration**:
- Web port: `6081`
- VNC target: `localhost:5900`
- Files served from: `/tmp/novnc`

**Components**:
- **websockify**: Translates WebSocket ↔ TCP socket
- **HTML5 Canvas**: Renders remote display
- **JavaScript RFB client**: Implements VNC protocol

**How it works**:
1. Browser connects via WebSocket to port 6081
2. websockify proxies to x11vnc on port 5900
3. JavaScript client renders framebuffer to HTML5 canvas
4. Mouse/keyboard events sent back through WebSocket

### 4. Window Manager (Fluxbox)
**Purpose**: Provides desktop environment for GUI applications

**Features**:
- Lightweight (minimal resource usage)
- Window decorations and controls
- Application focus management
- Desktop background and menus

### 5. Browser Application (Firefox)
**Version**: Firefox 142.0 (Latest stable)
**Installation**: Direct download from Mozilla CDN
**Dependencies**:
- GTK3 libraries (`libgtk-3-0`)
- DBus (`libdbus-glib-1-2`)
- X11 libraries (`libxt6`)

### 6. Browser API Server
**Purpose**: HTTP API for browser control, dashboard, system metrics, and GitHub configuration
**Port**: `6080`
**Language**: Python 3

**Endpoints**:
- `GET /` or `/dashboard` - Main workspace dashboard (metrics, GitHub config, service links)
- `GET /browser` - Legacy browser interface (redirects to dashboard)
- `GET /health` - Overall health check (JSON)
- `GET /health/vscode` - VS Code health check
- `GET /health/terminal` - Terminal health check
- `GET /health/browser` - VNC/browser health check
- `GET /metrics` - System metrics (CPU, memory, disk) as JSON
- `GET /metrics/prometheus` - Platform metrics in Prometheus text exposition format (see `docs/prometheus-metrics.md`)
- `GET /api/github/status` - GitHub auth status (SSH, CLI, git config)
- `POST /api/launch-chrome` - Launch browser
- `POST /api/test-chrome` - Test installation
- `POST /api/open-localhost` - Open localhost:8080
- `POST /api/github/config` - Set git user.name/email
- `POST /api/github/ssh/generate` - Generate SSH key
- `POST /api/github/cli/complete-auth` - Check gh CLI auth status

## Technology Stack

### Container Base
- **Base Image**: `registry.digitalocean.com/resourceloop/coder:devlaptop-v1.6.2-browser-stealth`
- **OS**: Ubuntu Noble (24.04)
- **Architecture**: linux/amd64

### Software Packages
```yaml
Core Display:
  - xvfb: Virtual framebuffer X server
  - x11vnc: VNC server for X11
  - fluxbox: Window manager
  
Browser:
  - firefox: Mozilla Firefox 142.0
  - lynx: Text browser (fallback)
  - w3m: Text browser (fallback)
  
Libraries:
  - libgtk-3-0: GTK3 toolkit
  - libdbus-glib-1-2: D-Bus bindings
  - libxt6: X11 toolkit intrinsics
  - libwayland-client0: Wayland protocol
  
Web VNC:
  - noVNC 1.4.0: HTML5 VNC client
  - websockify: WebSocket to TCP proxy
```

### Kubernetes Resources
```yaml
Deployment:
  - Replicas: 1
  - Strategy: Recreate
  
Service:
  - Type: ClusterIP
  - Ports: 8080, 7681, 6080, 6081
  
Ingress:
  - ingressClassName: nginx
  - TLS: Enabled with cert-manager
  - Paths: /, /browser, /vnc-direct/*, /terminal
  - OAuth2 paths: /oauth/*, /oauth/vscode/*, /oauth/terminal/*, /oauth/vnc-direct/*
  
ConfigMap:
  - browser-config: HTML interface and Python server
  
PersistentVolumeClaim:
  - home: User home directory
  - Size: 50Gi
```

## Implementation Details

### Startup Sequence
```bash
1. Container Initialization
   ├── Create directories (/home/dev, ~/.config)
   ├── Link persistent credentials (~/.ssh, ~/.config/git, ~/.config/gh)
   └── Start code-server and ttyd

2. Display Server Setup (3s delay)
   ├── Export DISPLAY=:99
   ├── Start Xvfb on display :99
   └── Wait for X server initialization

3. VNC Server Setup (2s delay)
   ├── Start x11vnc connected to :99
   ├── Listen on localhost:5900
   └── Enable forever mode and sharing

4. noVNC Setup (2s delay)
   ├── Download noVNC v1.4.0
   ├── Extract to /tmp/novnc
   ├── Start websockify proxy
   └── Listen on port 6081

5. Browser Server Setup
   ├── Copy config files from ConfigMap
   ├── Start Python HTTP server
   └── Listen on port 6080

6. Window Manager (on-demand)
   └── Start Fluxbox when needed

7. Agent Display Setup (browser.agentDisplay, #716)
   ├── Start Xvfb on display :98 (no x11vnc attached)
   ├── Start Fluxbox on :98
   ├── Export KC_AGENT_DISPLAY=:98 (DISPLAY stays :99)
   └── Install kc-gui into /home/dev/.local/bin
```

### Browser Launch Process
```python
1. API Request → /api/launch-chrome
2. Server validates X11 display availability
3. Browser wrapper script execution:
   for browser in [firefox-real, firefox, chromium, lynx]:
       if browser exists:
           execute with DISPLAY=:99
4. Process spawned with proper environment
5. Firefox connects to X server
6. Renders to virtual framebuffer
7. x11vnc captures and serves via VNC
8. User views through noVNC web client
```

### Network Flow
```
User Browser → HTTPS → Ingress Controller

Basic Auth Routes:
    ├── /             → Port 6080 (Dashboard)
    ├── /browser/*    → Port 6080 (Browser API)
    ├── /vscode       → Port 8080 (code-server)
    ├── /terminal     → Port 7681 (ttyd)
    ├── /vnc-direct/* → Port 6081 (noVNC)
    └── /websockify   → Port 6081 (WebSocket)

OAuth2 Routes (recommended):
    ├── /oauth/                → Port 6080 (Dashboard)
    ├── /oauth/vscode/*        → Port 8080 (code-server)
    ├── /oauth/terminal/*      → Port 7681 (ttyd)
    ├── /oauth/vnc-direct/*    → Port 6081 (noVNC)
    ├── /oauth/browser/*       → Port 6080 (Browser API)
    ├── /oauth/api/*           → Port 6080 (GitHub/config APIs)
    ├── /oauth/metrics*        → Port 6080 (System metrics)
    └── /oauth/health/*        → Port 6080 (Health checks)

Internal Container:
    Port 6080 → Python HTTP Server (dashboard, APIs, health)
    Port 6081 → websockify → localhost:5900 → x11vnc
    Port 5900 → x11vnc → Display :99 → Xvfb
```

## Data Flow

### Display Rendering Pipeline
```
1. Application Draw Call
   ↓ X11 Protocol
2. Xvfb Virtual Display
   ↓ Framebuffer in Memory
3. x11vnc Screen Capture
   ↓ RFB Protocol (VNC)
4. websockify Translation
   ↓ WebSocket Protocol
5. noVNC JavaScript Client
   ↓ Canvas API
6. User's Browser Display
```

### Input Event Flow
```
1. User Mouse/Keyboard Event
   ↓ JavaScript Event Handler
2. noVNC RFB Client
   ↓ WebSocket Message
3. websockify Translation
   ↓ TCP Socket
4. x11vnc Server
   ↓ X11 XTEST Extension
5. Xvfb Virtual Display
   ↓ X11 Event
6. Firefox Application
```

## Agent Display (`:98`)

`:99` is the human's screen. `x11vnc` streams it to the dashboard's Browser
tab, so before #716 a GUI app an agent launched with `DISPLAY=:99` rendered
into the frame the user was watching, took fluxbox focus from them, and showed
up in their tab — and an agent's screenshot captured whatever the user had
open. There was exactly one display and both parties shared it.

`start.sh` now also runs a **second `Xvfb` on `:98`** with its own fluxbox.
**No `x11vnc` is attached to it**, which is the whole mechanism: what is not
captured cannot be streamed. Agents target it explicitly; `DISPLAY` stays
`:99` for every other process, so a user who never touches `:98` sees no
change at all.

```
   human                                  agent
   ─────                                  ─────
   Browser tab                            DISPLAY=:98 / kc-gui
        │                                      │
   websockify :6081                            │
        │                                      │
   x11vnc :5900                                │
        │                                      │
   Xvfb :99  + fluxbox            Xvfb :98  + fluxbox
   (1920x1280x24)                 (1920x1280x24)
                                  ↑ nothing exports this
```

| | `:99` | `:98` |
|---|---|---|
| Who | the user | agents |
| Visible in the Browser tab | yes | **no** |
| Window manager | fluxbox | fluxbox |
| Framebuffer | 1920x1280x24 | 1920x1280x24 |
| Gate | `browser.enabled` | `browser.enabled` **and** `browser.agentDisplay` |
| Supervised | websockify port probe | `pgrep -f "Xvfb :98 "` |
| Idle cost | ~95MB (whole VNC stack) | ~42MB (Xvfb ~31MB + fluxbox ~11MB) |

### Driving it: `kc-gui`

The image ships neither `xdotool` nor ImageMagick `import`, so `kc-gui`
(`charts/workspace/kc_gui.py`, installed to `/home/dev/.local/bin/kc-gui` on
each boot) talks to libX11/libXtst through `ctypes` and encodes PNGs with
`zlib`. Stdlib only — no packages were added to the image for it.

```bash
kc-gui info                                  # geometry + window list
kc-gui run firefox --new-window https://x.test
kc-gui windows                               # id  WxH+X+Y  title
kc-gui screenshot -o /tmp/shot.png           # whole display
kc-gui screenshot -w 0x40000c -o /tmp/w.png  # one window
kc-gui click 400 300 [-b 1]
kc-gui move 400 300
kc-gui type -w 0x40000c 'make test'
kc-gui key  -w 0x40000c Return ctrl+c alt+F4
kc-gui focus 0x40000c
kc-gui -d :99 windows                        # …or target the human's display
```

### What this is not

- **Not a security boundary.** X11 has no seat isolation: a process that can
  open `:98` can also open `:99`, read its windows and inject into it. `:98`
  buys *visual* separation — the user keeps their screen — nothing more.
- **Not a box per app.** There is one `:98` shared by every agent in the pod.
- **No clipboard isolation.** Selections are per-display here, but nothing
  prevents a process from reading either display's.
- **Not a Playwright replacement.** For anything with a DOM, the Playwright
  MCP's selectors and waits beat pixel-clicking. `:98` is for native GUI apps.

See [#716](https://github.com/imran31415/kube-coder/issues/716) for the
nested-compositor designs (wbox-mcp / a native `kc-gui` compositor) that would
give a real isolation boundary.

## Security Considerations

### Network Security
- **TLS Encryption**: All external traffic uses HTTPS
- **Internal VNC**: x11vnc binds to localhost only
- **No VNC Password**: Relies on ingress authentication
- **WebSocket Security**: Inherits HTTPS security

### Authentication
- **Basic Auth**: Nginx ingress controller (legacy deployments)
- **GitHub OAuth2**: OAuth2 Proxy via `/oauth2/` path (recommended). All `/oauth/*` paths are protected by OAuth2 with GitHub user authorization.
- **Exceptions**: WebSocket paths rely on session/cookie auth for compatibility
- **Secret**: `api-basic-auth` (basic) or `oauth2-proxy-secrets-{user}` (OAuth2)

### Process Isolation
- **User**: Runs as non-root (UID 1000)
- **Namespace**: Isolated Kubernetes namespace
- **Network**: Pod network isolation

### Resource Limits
```yaml
Resources:
  requests:
    cpu: "2"
    memory: 3Gi
  limits:
    cpu: "3"
    memory: 5Gi
```

## Troubleshooting Guide

### Common Issues and Solutions

#### 1. X11 Display Not Available
**Symptom**: "X11 display :99 not available"
**Cause**: Xvfb not started or crashed
**Solution**:
```bash
# Check if Xvfb is running
ps aux | grep Xvfb

# Manually start Xvfb
rm -f /tmp/.X99-lock
DISPLAY=:99 Xvfb :99 -screen 0 1280x720x24 &
```

#### 2. VNC Connection Failed
**Symptom**: "Failed to connect to downstream server"
**Cause**: x11vnc not running or not connected to display
**Solution**:
```bash
# Check x11vnc status
ps aux | grep x11vnc

# Restart x11vnc
DISPLAY=:99 x11vnc -display :99 -nopw -listen localhost -forever -shared &
```

#### 3. Browser Won't Launch
**Symptom**: "Chrome process exited immediately"
**Cause**: Missing dependencies or display issues
**Solution**:
```bash
# Test Firefox directly
DISPLAY=:99 /usr/local/bin/firefox-real --version

# Check for GTK errors
DISPLAY=:99 /usr/local/bin/firefox-real 2>&1 | head -20

# Install missing libraries
sudo apt-get install libgtk-3-0 libdbus-glib-1-2
```

#### 4. Black Screen in VNC
**Symptom**: VNC connects but shows black screen
**Cause**: No window manager running
**Solution**:
```bash
# Start Fluxbox window manager
DISPLAY=:99 fluxbox &
```

#### 5. WebSocket Connection Error
**Symptom**: "Invalid server version" in browser console
**Cause**: Ingress path routing issues
**Solution**:
- Verify ingress annotations for WebSocket support
- Check `/websockify` path is properly configured
- Ensure noVNC is serving on port 6081

### Debugging Commands

```bash
# Check all browser-related processes
kubectl exec -n coder $POD -- ps aux | grep -E "(Xvfb|x11vnc|firefox|fluxbox|websockify)"

# View Xvfb logs
kubectl exec -n coder $POD -- cat /tmp/xvfb.log

# View x11vnc logs
kubectl exec -n coder $POD -- cat /tmp/x11vnc.log

# Test X11 display
kubectl exec -n coder $POD -- bash -c "DISPLAY=:99 xdpyinfo | head"

# Check port listeners
kubectl exec -n coder $POD -- netstat -tlnp 2>/dev/null | grep -E "(5900|6080|6081)"

# Test browser wrapper
kubectl exec -n coder $POD -- bash -c "DISPLAY=:99 /usr/local/bin/browser --version"
```

### Performance Tuning

#### VNC Optimization
```bash
# Enable client-side caching (reduces bandwidth)
x11vnc -ncache 10 -ncache_cr

# Adjust polling rate for slower connections
x11vnc -defer 50 -wait 50

# Disable unnecessary features
x11vnc -noxdamage  # If screen updates are missing
```

#### Browser Optimization
```bash
# Launch Firefox in safe mode (disables extensions)
firefox --safe-mode

# Reduce memory usage
firefox --new-instance --profile /tmp/firefox-profile
```

#### Display Resolution
```bash
# Lower resolution for better performance
Xvfb :99 -screen 0 1024x768x24

# Higher resolution for more workspace
Xvfb :99 -screen 0 1920x1080x24
```

## Maintenance

### Updating Firefox
```dockerfile
# In Dockerfile, update download URL
RUN wget -O firefox.tar.xz "https://download.mozilla.org/?product=firefox-latest&os=linux64&lang=en-US"
```

### Updating noVNC
```bash
# In startup script, change version
wget https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz
```

### Health Checks
The deployment uses HTTP-based probes against the Python dashboard server:
```yaml
readinessProbe:
  httpGet:
    path: /health
    port: 6080
    scheme: HTTP
  initialDelaySeconds: 30
  periodSeconds: 10
  timeoutSeconds: 5
livenessProbe:
  httpGet:
    path: /health
    port: 6080
    scheme: HTTP
  initialDelaySeconds: 60
  periodSeconds: 30
  timeoutSeconds: 10
```

## Future Enhancements

1. **Multi-browser Support**: Add Chrome/Chromium alongside Firefox
2. **Audio Support**: Implement PulseAudio forwarding
3. **Clipboard Sync**: Enable copy/paste between local and remote
4. **Session Recording**: Add ability to record browser sessions
5. **GPU Acceleration**: Utilize GPU for better performance
6. **Scaling**: Support multiple browser instances per user
7. **Automated Testing**: Integration with Selenium/Playwright

## Conclusion

This architecture provides a robust, scalable solution for remote browser access in a Kubernetes environment. The combination of virtual display technology, VNC streaming, and web-based access creates a seamless user experience while maintaining security and resource efficiency.
