# Remote Browser Architecture Documentation

## Overview

This document describes the comprehensive architecture for the remote browser functionality integrated into the Kubernetes-based development environment. The system enables users to launch and interact with a full GUI Firefox browser through a web interface, utilizing virtual display technology and VNC for remote access.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Core Components](#core-components)
3. [Technology Stack](#technology-stack)
4. [Implementation Details](#implementation-details)
5. [Data Flow](#data-flow)
6. [Security Considerations](#security-considerations)
7. [Troubleshooting Guide](#troubleshooting-guide)

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                        User Browser                          │
└─────────────────────┬───────────────────────────────────────┘
                      │ HTTPS
┌─────────────────────▼───────────────────────────────────────┐
│                    NGINX Ingress Controller                  │
│  Routes: /browser, /vnc-direct/*, /websockify, /terminal    │
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
**Purpose**: HTTP API for browser control
**Port**: `6080`
**Language**: Python 3

**Endpoints**:
- `GET /browser` - Web interface
- `POST /api/launch-chrome` - Launch browser
- `POST /api/test-chrome` - Test installation
- `POST /api/open-localhost` - Open localhost:8080

## Technology Stack

### Container Base
- **Base Image**: `registry.digitalocean.com/resourceloop/coder:devlaptop-v1.6.0`
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
  - Strategy: RollingUpdate
  
Service:
  - Type: ClusterIP
  - Ports: 8080, 7681, 6080, 6081
  
Ingress:
  - Class: nginx
  - TLS: Enabled with cert-manager
  - Paths: /, /browser, /vnc-direct/*, /terminal
  
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
   ├── Link shared data (SSH keys, git config)
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
    ├── /browser/* → Port 6080 (Browser API)
    ├── /vnc-direct/* → Port 6081 (noVNC)
    ├── /websockify → Port 6081 (WebSocket)
    └── /terminal → Port 7681 (ttyd)

Internal Container:
    Port 6080 → Python HTTP Server
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

## Security Considerations

### Network Security
- **TLS Encryption**: All external traffic uses HTTPS
- **Internal VNC**: x11vnc binds to localhost only
- **No VNC Password**: Relies on ingress authentication
- **WebSocket Security**: Inherits HTTPS security

### Authentication
- **Basic Auth**: Nginx ingress controller
- **Exceptions**: VNC paths (WebSocket compatibility)
- **Secret**: `api-basic-auth` in namespace

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
```yaml
# Add to deployment.yaml
livenessProbe:
  exec:
    command:
    - bash
    - -c
    - "DISPLAY=:99 xdpyinfo >/dev/null 2>&1"
  initialDelaySeconds: 30
  periodSeconds: 10
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