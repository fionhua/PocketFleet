# PocketFleet 🚀
> **Your 24/7 AI Engineering Squad in Your Pocket.**  
> Dispatch tasks from Telegram on your phone → Let Claude Code & Aider do the heavy lifting on your machine → Get PRs and diffs back in seconds.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Zero Dependency](https://img.shields.io/badge/Core%20Dependencies-ZERO-brightgreen.svg)](#)

---

## ⚡ Why PocketFleet?

You are walking your dog, commuting, or grabbing coffee, and an urgent bug report or a flash of inspiration hits you.  
Instead of rushing back to your desk:
1. Open **Telegram** on your phone.
2. Send: `/fix auth token expired on branch dev`
3. **PocketFleet** (running quietly on your home PC or cloud VPS) wakes up **Claude Code** or **Aider**, searches your codebase, fixes the bug, runs tests, and sends back the git diff or PR link.

**Your code NEVER leaves your machine. No cloud storage. Pure local-first dispatch.**

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
