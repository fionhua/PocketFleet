# PocketFleet Architecture Whitepaper

## Overview

PocketFleet is a lightweight, zero-dependency execution bridge that connects mobile messaging channels (starting with Telegram) to local terminal coding agents (Claude Code, Aider).

```
                      +-----------------------+
                      | Mobile Telegram App   |
                      | (Developer on-the-go) |
                      +-----------+-----------+
                                  |
                                  | HTTPS / Long-Polling
                                  v
                      +-----------------------+
                      |  TelegramTransport    |
                      |  (Watermark & Dedup)  |
                      +-----------+-----------+
                                  |
                                  v
+-------------------------------------------------------------+
| DispatchLoop (Single-Direction DAG Gate)                    |
|                                                             |
|  [Security Gate 1] Role Gating (is_bot == False)            |
|  [Security Gate 2] Allowed Chat ID Whitelist                |
|  [Security Gate 3] Circuit Breaker (Throttle runaway tasks) |
+-----------------------------+-------------------------------+
                              |
                              v
             +----------------+----------------+
             |                                 |
             v                                 v
+------------------------+        +------------------------+
| ClaudeCodeExecutor     |        | AiderExecutor          |
| (Anthropic Claude CLI) |        | (Open-Source Multi-LLM)|
+------------+-----------+        +------------+-----------+
             |                                 |
             +----------------+----------------+
                              |
                              | Subprocess stdout & git diff
                              v
                  +-----------------------+
                  | Direct Outbound Reply |
                  | (Single-direction)    |
                  +-----------+-----------+
                              |
                              v
                      +-----------------------+
                      | Developer's Telegram  |
                      | (PR link & summary)   |
                      +-----------------------+
```

## Core Design Principles

### 1. Echo-Proof Single-Direction DAG
Unlike multi-agent group chats where bots talk to bots and risk catastrophic infinite loops, PocketFleet enforces a strict single-direction Directed Acyclic Graph (DAG):
- Inbound: Only authenticated human messages are accepted.
- Execution: Subprocess execution is sandboxed with configurable timeouts.
- Outbound: Execution results are sent directly back to the human. Results are never recycled back into the dispatcher.

### 2. Zero Central Infrastructure
PocketFleet operates strictly on your own hardware, home workstation, or private VPS.
- No intermediary proxy server.
- No cloud storage of code or prompts.
- All git credentials, SSH keys, and environment variables stay on your own metal.

### 3. Lightweight & Crash-Proof
The core dispatch engine requires zero third-party Python packages (`urllib`, `json`, `subprocess`, `threading` only). It starts in under 50 milliseconds and runs reliably on minimal VPS resources (e.g. 1 vCPU, 512MB RAM).
