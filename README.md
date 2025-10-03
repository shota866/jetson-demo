# A-Frame Manager Demo

Unified workspace containing the Web UI and Python manager used for Sora data channel experiments.

## Project layout

- `ui/` – A-Frame based web client.
- `server/` – Python manager and helper scripts.

## Getting started

### Web UI

```
cd ui
npm install
npm run start
```

The `start` script launches a simple static server on http://localhost:8000. Use `npm run lint` to check formatting with Prettier.

### Manager

```
python -m venv .venv
source .venv/bin/activate
pip install -r server/requirements.txt
python server/manager.py --help
```
