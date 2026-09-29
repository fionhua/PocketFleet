# PocketFleet 🚀
> **"Stop babysitting your CLI. Ship code from Telegram."**  
> *The lightweight Telegram remote cockpit for Claude Code & Aider.*

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

## 🚀 Quick Start (Coming Soon)

```bash
# Clone and run directly
git clone https://github.com/PocketFleet/PocketFleet.git
cd PocketFleet
python -m pocketfleet.launcher
```

---

## 📜 Core Philosophy

- **Zero Lock-in**: Run locally with your own API keys.
- **Echo-Proof Architecture**: Strict single-direction DAG flow ensures AI agents never get caught in infinite reply loops.
- **Privacy-First**: No central servers sniffing your prompts or proprietary code.
