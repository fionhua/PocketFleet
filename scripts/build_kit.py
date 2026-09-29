import os
import zipfile
import shutil

dist_dir = r"d:\workSpace\PocketFleet\dist"
kit_dir = os.path.join(dist_dir, "PocketFleet-v0.2.0-Solo-Hacker-Kit")
if os.path.exists(kit_dir):
    try:
        shutil.rmtree(kit_dir)
    except Exception:
        pass
os.makedirs(kit_dir, exist_ok=True)

# 1. Copy Wheel & Standalone EXE
whl_src = os.path.join(dist_dir, "pocketfleet-0.2.0-py3-none-any.whl")
shutil.copy2(whl_src, os.path.join(kit_dir, "pocketfleet-0.2.0-py3-none-any.whl"))

exe_src = os.path.join(dist_dir, "PocketFleet-Control-Panel.exe")
if os.path.isfile(exe_src):
    shutil.copy2(exe_src, os.path.join(kit_dir, "PocketFleet-Control-Panel.exe"))
    print("Copied PocketFleet-Control-Panel.exe into kit.")

# 2. Launch-Cockpit.bat
bat_content = """@echo off
chcp 65001 >nul
title PocketFleet Cockpit - Solo Hacker Edition
echo ========================================================
echo   🚀 PocketFleet v0.2.0 - Solo Hacker Edition
echo   AI StarFleet Communication Cockpit Launcher
echo ========================================================
echo.

if exist "PocketFleet-Control-Panel.exe" (
    echo [*] Launching Desktop Control Panel...
    start "" "PocketFleet-Control-Panel.exe"
    exit /b 0
)

where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python was not found in your system PATH!
    echo Please install Python 3.10+ from https://www.python.org/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo [*] Installing / upgrading PocketFleet package...
python -m pip install --quiet --upgrade pocketfleet-0.2.0-py3-none-any.whl

echo [*] Launching PocketFleet Local Cockpit...
echo [*] Opening your browser at http://127.0.0.1:8765 ...
echo.
python -m pocketfleet.launcher --ui

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!] Cockpit exited. Press any key to close this window.
    pause
)
"""
with open(os.path.join(kit_dir, "Launch-Cockpit.bat"), "w", encoding="utf-8") as f:
    f.write(bat_content)

# 3. launch-cockpit.sh
sh_content = """#!/usr/bin/env bash
set -e

echo "========================================================"
echo "  🚀 PocketFleet v0.2.0 - Solo Hacker Edition"
echo "  AI StarFleet Communication Cockpit Launcher"
echo "========================================================"
echo ""

if ! command -v python3 &> /dev/null; then
    echo "[ERROR] python3 could not be found."
    echo "Please install Python 3.10+."
    exit 1
fi

echo "[*] Installing / upgrading PocketFleet package..."
python3 -m pip install --quiet --upgrade pocketfleet-0.2.0-py3-none-any.whl

echo "[*] Launching PocketFleet Local Cockpit..."
python3 -m pocketfleet --ui
"""
with open(os.path.join(kit_dir, "launch-cockpit.sh"), "w", encoding="utf-8") as f:
    f.write(sh_content)

# 4. LICENSE_SOLO_HACKER.txt
license_content = """================================================================================
                    POCKETFLEET SOLO HACKER LICENSE CERTIFICATE
================================================================================

License Tier : Solo Hacker Edition (Commercial & Perpetual)
Version      : v0.2.0
Issued by    : PocketFleet Engineering & StarFleet Core
Holder       : Valid License Purchaser via Lemon Squeezy Store

GRANT OF LICENSE:
Subject to the terms of this license, the holder is granted a perpetual, 
worldwide, non-exclusive license to:
1. Deploy PocketFleet across any number of personal machines, remote VPS, 
   homelabs, or cloud servers owned or leased by the licensee.
2. Bridge personal Telegram accounts to Claude Code, Aider, OpenHands, 
   or custom CLI agents for commercial work and freelance coding.
3. Access priority updates, local cockpit visualizers, and cloud relay 
   configurations.

RESTRICTIONS:
- You may NOT resell, sub-license, or redistribute this delivery package or 
  its source modifications as a competing SaaS or standalone package.
- Community edition remains MIT. This Solo Hacker tier provides the official 
  commercial guarantee, standalone cockpit assets, and future priority relay support.

SUPPORT & COMMUNITY:
- Official GitHub: https://github.com/fionhua/PocketFleet
- Landing & Docs:  https://fionhua.github.io/PocketFleet/
- Direct Support:  hi.yihua@gmail.com

Thank you for backing independent sovereign developer tooling!
Command Your AI Fleet From Your Pocket.
================================================================================
"""
with open(os.path.join(kit_dir, "LICENSE_SOLO_HACKER.txt"), "w", encoding="utf-8") as f:
    f.write(license_content)

# 5. QUICK_START.html
html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>PocketFleet - Solo Hacker Edition Quick Start</title>
    <style>
        :root {
            --bg: #090d16;
            --card-bg: rgba(16, 24, 40, 0.85);
            --border: rgba(99, 102, 241, 0.25);
            --accent: #6366f1;
            --accent-glow: #818cf8;
            --success: #10b981;
            --text-main: #f1f5f9;
            --text-sub: #94a3b8;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background: radial-gradient(circle at 50% 0%, #171b30, var(--bg) 80%);
            color: var(--text-main);
            padding: 40px 20px;
            line-height: 1.6;
        }
        .container { max-width: 840px; margin: 0 auto; }
        .header { text-align: center; margin-bottom: 40px; }
        .badge {
            display: inline-block;
            background: rgba(99, 102, 241, 0.15);
            color: var(--accent-glow);
            border: 1px solid var(--border);
            padding: 4px 14px;
            border-radius: 9999px;
            font-size: 13px;
            font-weight: 600;
            margin-bottom: 12px;
            letter-spacing: 0.05em;
        }
        h1 { font-size: 32px; font-weight: 800; margin-bottom: 8px; }
        .subtitle { color: var(--text-sub); font-size: 16px; }
        .card {
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 24px;
            box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5);
        }
        .step-title {
            display: flex;
            align-items: center;
            font-size: 18px;
            font-weight: 700;
            color: #fff;
            margin-bottom: 12px;
        }
        .step-num {
            width: 28px;
            height: 28px;
            border-radius: 50%;
            background: var(--accent);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-size: 14px;
            margin-right: 12px;
            flex-shrink: 0;
        }
        code, pre {
            font-family: 'JetBrains Mono', Consolas, Monaco, monospace;
            background: #040711;
            color: #38bdf8;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 14px;
        }
        pre {
            padding: 14px;
            margin: 10px 0;
            overflow-x: auto;
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 6px;
        }
        .btn-launch {
            display: inline-block;
            background: linear-gradient(135deg, #6366f1, #4f46e5);
            color: #fff;
            padding: 12px 24px;
            border-radius: 8px;
            font-weight: 600;
            text-decoration: none;
            box-shadow: 0 4px 14px rgba(99, 102, 241, 0.4);
            margin-top: 10px;
        }
        .footer { text-align: center; color: var(--text-sub); font-size: 13px; margin-top: 50px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="badge">SOLO HACKER LICENSE &bull; v0.2.0</div>
            <h1>Welcome to PocketFleet</h1>
            <p class="subtitle">Thank you for purchasing! You are now sovereignly empowered to code from your phone.</p>
        </div>

        <div class="card">
            <div class="step-title"><span class="step-num">1</span> 1-Click Launch Desktop Control Panel (Tray Resident)</div>
            <p>You can manage PocketFleet with our standalone desktop app or terminal:</p>
            <ul style="margin-left: 20px; margin-top: 8px; color: var(--text-sub);">
                <li><b>Windows:</b> Simply double-click <code>PocketFleet-Control-Panel.exe</code> or <code>Launch-Cockpit.bat</code>! It features a XAMPP-style GUI that minimizes to the Windows system tray ("Tony icon").</li>
                <li><b>macOS / Linux:</b> Run <code>chmod +x launch-cockpit.sh && ./launch-cockpit.sh</code></li>
            </ul>
            <p style="margin-top: 12px;">Or via terminal:</p>
            <pre>pip install pocketfleet-0.2.0-py3-none-any.whl
pocketfleet --ui</pre>
            <p>Your browser will automatically open the Dark Cockpit at <code>http://127.0.0.1:8765</code>.</p>
        </div>

        <div class="card">
            <div class="step-title"><span class="step-num">2</span> 60-Second Telegram Bot Setup</div>
            <ol style="margin-left: 20px; margin-top: 8px; color: var(--text-sub);">
                <li>Open Telegram and chat with <code>@BotFather</code>. Send <code>/newbot</code>.</li>
                <li>Copy the generated <b>Bot Token</b>.</li>
                <li>Send any message to your new bot, then find your Telegram Numeric ID (via <code>@userinfobot</code>).</li>
                <li>In your cockpit UI or terminal, set your config:</li>
            </ol>
            <pre>pocketfleet init
# Enter your Bot Token and Authorized User ID when prompted</pre>
        </div>

        <div class="card">
            <div class="step-title"><span class="step-num">3</span> Connect Claude Code / Aider & Start Coding</div>
            <p>Run PocketFleet in daemon mode alongside your terminal:</p>
            <pre>pocketfleet run --executor claude_code</pre>
            <p style="margin-top: 8px; color: var(--text-sub);">Now send a message from Telegram on your phone:</p>
            <pre>Fix the pagination bug in users.py and run the test suite</pre>
            <p style="color: var(--success); font-weight: 600; margin-top: 8px;">✔ Watch your machine execute, commit, and report progress back to your pocket in real time!</p>
        </div>

        <div class="footer">
            PocketFleet &copy; 2026. Made with ❤️ for Independent Developers worldwide.<br>
            GitHub: <a href="https://github.com/fionhua/PocketFleet" style="color: #818cf8; text-decoration: none;">github.com/fionhua/PocketFleet</a>
        </div>
    </div>
</body>
</html>
"""
with open(os.path.join(kit_dir, "QUICK_START.html"), "w", encoding="utf-8") as f:
    f.write(html_content)

# 6. Zip it into dist/PocketFleet-v0.2.0-Solo-Hacker-Kit.zip
zip_path = os.path.join(dist_dir, "PocketFleet-v0.2.0-Solo-Hacker-Kit.zip")
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
    for root, dirs, files in os.walk(kit_dir):
        for file in files:
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, kit_dir)
            zipf.write(full_path, arcname=os.path.join("PocketFleet-v0.2.0-Solo-Hacker-Kit", rel_path))

print("ZIP created successfully at:", zip_path)
print("Size in bytes:", os.path.getsize(zip_path))
