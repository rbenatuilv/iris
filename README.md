# Iris — personal security & alerts agent

Runs 24/7 on a Raspberry Pi. Watches earthquakes, tsunamis, weather warnings, global disasters and local news
(Milan, Santiago, Talca), verifies what matters, and alerts you on Telegram.

- Spec / design notes: see the Iris project (`claude/iris-spec.md`).
- Build the installer: `./build.sh` → `dist/iris-install.sh` (copy to the Pi, run `bash iris-install.sh`).
- Tests: `pip install httpx pyyaml defusedxml pytest respx && python -m pytest -q`
- On the Pi: `iris state | logs | selftest | restart | config`

Layout: `iris/` code · `config/defaults.yaml` settings (override in `config/local.yaml` on the Pi) ·
`deploy/install.sh` installer template · `tests/` unit + replay tests.
