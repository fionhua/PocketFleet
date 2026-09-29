# Contributing to PocketFleet

Thank you for your interest in contributing to PocketFleet! We welcome contributions from developers worldwide.

## Development Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/fionhua/PocketFleet.git
   cd PocketFleet
   ```

2. **Create a virtual environment**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install in development mode**:
   ```bash
   pip install -e ".[dev]"
   ```

4. **Run the test suite**:
   ```bash
   pytest -v
   ```
   All tests must pass before submitting a pull request.

## Code Standards
- Zero third-party core dependencies: Keep the core engine pure Python (`>=3.10`).
- Strict typing: Use standard typing annotations.
- Defensive programming: Always handle network drops, invalid payloads, and process timeouts gracefully.
- Echo-proof: Never introduce mechanisms that allow automated bot-to-bot recursion.

## Pull Request Guidelines
1. Fork the repo and create your branch from `main`.
2. Add corresponding tests for any new executor or transport feature.
3. Ensure CI matrix passes across Linux, Windows, and macOS.
4. Keep PR descriptions focused and clear.
