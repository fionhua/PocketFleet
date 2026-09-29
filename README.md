# PocketFleet 🚀
> **"Stop babysitting your CLI. Ship code from Telegram."**  
> *The lightweight Telegram remote cockpit for Claude Code & Aider.*

[![GitHub Release](https://img.shields.io/github/v/release/fionhua/PocketFleet?color=brightgreen)](https://github.com/fionhua/PocketFleet/releases)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Zero Dependency](https://img.shields.io/badge/Core%20Dependencies-ZERO-brightgreen.svg)](#)

---

## ⚡ Three Killer Features

1. **100% Local & Private — Zero Data Leakage**  
   *Your code never touches third-party relay servers.* PocketFleet operates strictly as a lightweight harness on your own hardware or self-hosted VPS. Your proprietary codebase, local environment variables, and API keys stay entirely on your own metal.

2. **True Fire-and-Forget Asynchronous Dispatch**  
   *Review PRs from the coffee shop, not your desk.* Fire a quick bug fix or feature request from Telegram while you're away. PocketFleet drives Claude Code / Aider through code editing, test execution, and git commits, then pings you back with a crisp diff summary and a ready-to-merge PR.

3. **Zero Bloat, Zero Workflow Rewrites**  
   *No heavy Docker stacks, no intrusive IDE lock-in.* PocketFleet acts as a drop-in execution bridge that natively wraps around your existing CLI tools. Keep using your favorite terminal setups, aliases, shell configs, and git repositories without changing a single line of your workflow.

---

## 🛠 Supported Coding Agents

- [x] **Claude Code** (Anthropic's cutting-edge CLI)
- [x] **Aider** (The battle-tested open-source pair programmer)
- [x] **OpenAI / Custom LLMs** via Aider backend

---

## 🚀 Quick Start

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/fionhua/PocketFleet.git
cd PocketFleet

# Install in editable mode (Zero external core dependencies)
pip install -e .
```

### 2. 60-Second Setup Wizard

Run the interactive setup wizard to pair your Telegram Bot and lock your Chat ID:

```bash
pocketfleet --init
```

1. Enter your Telegram Bot Token (from `@BotFather`).
2. Send `/start` to your bot from your phone to automatically bind your private Chat ID.
3. PocketFleet verifies that `claude` (Claude Code) or `aider` CLI is installed.

### 3. Launch Daemon

```bash
pocketfleet
```

Now you're free! Send a message from your phone anywhere, and let your local machine do the coding.

---

## 📜 Core Philosophy

- **Zero Lock-in**: Run locally with your own API keys.
- **Echo-Proof Architecture**: Strict single-direction DAG flow ensures AI agents never get caught in infinite reply loops.
- **Privacy-First**: No central servers sniffing your prompts or proprietary code.
