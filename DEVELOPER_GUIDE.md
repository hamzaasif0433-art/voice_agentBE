# BlenSpark Voice Agent - Developer Onboarding & Status Guide

This document serves as the primary technical onboarding guide for any developer or AI agent continuing work on the BlenSpark Voice Agent. It outlines the architecture, networking quirks, what has been completed, and what remains pending.

---

## 1. System Architecture

The core objective of this application is to maintain a sub-600ms latency voice loop between a caller (via SIP trunk or softphone) and the Google Gemini Multimodal Live API.

### The Pipeline:
`[ Multinet SIP Trunk / MicroSIP ]  <--SIP/UDP-->  [ Asterisk (Docker) ]  <--SIP/UDP-->  [ Python PyVoIP ]  <--WebSockets-->  [ Gemini Live API ]`

We successfully decoupled the architecture into two dedicated layers:
1. **The SIP Boundary (Asterisk 20)**
   * **Role:** Handles complex telecom behaviors, Carrier IP-Auth, NAT traversal, and SIP signaling.
   * **State:** Runs inside a Docker container (`pjsip.conf`, `extensions.conf`).
2. **The App Boundary (Django / PyVoIP)**
   * **Role:** Natively answers local SIP calls from Asterisk on Port `5061`. It transcodes G.711 µ-law to 16kHz PCM and handles the Gemini WebSockets natively without ARI or Django Channels WebSocket overhead.
   * **State:** Run via the management command `python manage.py run_sip_server`.

---

## 2. Configuration & Ports

| Component | Network Location | Port | Description |
| :--- | :--- | :--- | :--- |
| **Asterisk Gateway** | Docker Container | `5060 (UDP)` | External SIP listening port facing the public internet / Multinet. |
| **Asterisk RTP** | Docker Container | `10000-20000 (UDP)` | External audio media ports facing the public internet. |
| **Python PyVoIP** | Windows Host (Local) | `5061 (UDP)` | Internal Python SIP Server. Asterisk `Dial`s to this port. |
| **Django Backend** | Windows Host (Local) | `8000 (TCP)` | Main Web/Dashboard API handling databases and analytics. |

### Known Network Gotcha: "The Docker UDP Trap"
When developing on Windows/WSL2, Windows Defender Firewall aggressively blocks inbound UDP packets coming from Docker containers to the host. 
**If audio drops or is entirely silent locally:** You must explicitly allow UDP ports `5061` and `10000-20000` through the Windows Firewall, OR temporarily disable the firewall during local testing.

---

## 3. Project Status: Done vs. Remaining

### ✅ What is Completed (Current State)
* **Architecture Shift:** Replaced a highly unstable WebSocket (Django Channels) + Asterisk ARI architecture with a vastly superior native PJSIP-to-PyVoIP architecture.
* **SIP Deadlocks Resolved:** Bypassed the Asterisk `simple_bridge` silence deadlock by allowing native SIP SDP negotiation directly with the Python code.
* **Gemini Live Integrated:** The streaming engine directly captures byte audio, transcodes it bi-directionally, and interacts seamlessly with Gemini Multimodal Live.
* **Dashboard API Intact:** All non-voice logic, analytics, and tool executions have been preserved in the Django codebase.
* **Multinet Prep:** Asterisk `extensions.conf` and `pjsip.conf` are pre-configured to handle inbound Multinet calls perfectly and route them to `PyVoIP`.

### ⏳ What is Remaining (Future Work)
The primary remaining tasks are strictly physical deployment and final carrier testing.

1. **Multinet Carrier Registration**
   * *Task:* Receive final credentials (IP/Username/Password) from Multinet.
   * *Action:* Update the `[multinet-registration]` and `[multinet-auth]` blocks inside `pjsip.conf` on the production server.
2. **End-to-End Carrier Voice Testing**
   * *Task:* Validate that inbound calls from actual mobile phones do not drop after 32 seconds (SIP ACK timeout check) and that audio is properly bidirectionally transmitted.
3. **Production Deployment (Linux)**
   * *Task:* Currently, the system builds on Windows with WSL2 Docker. Moving to a native Linux environment (Ubuntu VM) for production will entirely eliminate the Windows/Docker UDP firewall mapping issues.
   * *Action:* Ensure `UDP_EXT_HOST` or PyVoIP binding IPs correctly target the new host adapter.
4. **Agent Reliability & Prompt Engineering**
   * *Task:* Ensure Gemini Live properly cuts off (interrupts) when the user speaks over the agent. Check parameters around VAD (Voice Activity Detection) in the Gemini SDK setup.
