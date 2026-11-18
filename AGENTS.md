# AGENTS.md

Guidance for AI coding agents and contributors working on **faumon**. This is a Flask dashboard that polls Gatus and Prometheus endpoints and shows service health, contacts, and PDU temperatures for FAU.

## Project map

| Path | Purpose |
|------|---------|
| `app.py` | The whole backend: config loading, the background poller thread, the parsers, and two routes (`/`, `/api/status`). |
| `templates/index.html` | Single-page UI. It polls `/api/status` every 10s and renders everything client-side. |
| `static/css/style.css`, `custom.css` | Base styles and theme overrides. |
| `config-example.json` | Public example config. The Docker image copies it in as `config.json`. |
| `startDevelop` / `startProd` | Run with the Flask dev server, or with Gunicorn (gthread, port 5001). |
| `charts/fau-monitor/` | Helm chart. The real config is mounted from the `faumon-config-secrets` Secret. |
| `.github/workflows/docker-build.yml` | Every push to `main` builds and pushes `ghcr.io/<repo>:latest`. |

## Secrets and private data

- **Never commit real config.** `config.json`, `values-*.yaml`, and `charts/fau-monitor/templates/secret-faumon-config.yaml` are gitignored on purpose. They hold internal FAU hostnames, staff names, phone numbers, and emails.
- `config-example.json` must use only placeholder data: `example.com` hosts and `555` phone numbers. Before committing a change to it, check it for real `fau.edu` hosts or personal contacts.
- Don't paste config contents into issues, PRs, or external tools.

## Configuration rules

- **Every new option must be optional, with a safe default.** Read it with `config.get(key, default)`. A missing key must behave exactly like the old behavior, because the deployed Secret is updated separately from the image.
- **Bad config values must never crash the poller.** Log a warning and fall back to the default; `parse_timeout()` is the model to follow.
- Endpoint entries can be either a plain string or a dict. Always handle both forms; see the `isinstance(endpoint, dict)` checks.
- **When you add an option, update all three places:** the README's Configuration section, `config-example.json`, and (when it applies) the Helm Secret template the user deploys.
- `config.json` is re-read on every poll cycle, so config changes take effect without a restart.

### Endpoint keys

| Key | Default | Notes |
|-----|---------|-------|
| `url` | required | |
| `type` | `gatus` | Set it to `prometheus` for `/metrics` sources. A `text/plain` response is also treated as Prometheus. |
| `verify_ssl` | `true` | Several internal HPC hosts need `false`. |
| `timeout_seconds` | `default_timeout_seconds` (or `[3, 3]`) | A single number, or a `[connect, read]` pair. |

## Poller behavior

- Endpoints are polled **one after another** in a single loop. The worst-case cycle time is the sum of the endpoints' timeouts. Keep that sum well under `refresh_interval_seconds`, and consider it before raising any timeout.
- If a poll cycle returns no services at all, the previous cache is kept. That way, a total outage of upstream monitors doesn't blank the dashboard. Don't remove this guard.
- Each failure is logged per endpoint and the loop continues. Never let one endpoint, or one malformed service object, stop the whole cycle.
- **The poller starts when the module is imported,** so every Gunicorn worker runs its own copy. More workers means more load on the upstream endpoints. Keep this in mind before moving to a shared cache or changing the worker count.
- Shared state lives in `STATUS_CACHE`. Always read or write it while holding `CACHE_LOCK`.

## Data contract (`/api/status`)

- The frontend reads these fields on each service: `name`, `group`, `key`, `status` (`ok`/`critical`/`unknown`), `importance`, `temperature`, `temperature_unit`, `details`, `contact`. Renaming or removing any of them breaks the UI, so change `app.py` and `index.html` together.
- `temperature` is `null` when no PDU matches. Readings of 0 or below are dropped as sensor errors. A PDU's `health_status == 0` forces the service to `critical`.
- PDU metrics are matched to services by exact `name` or `key`.
- Group priority lookups are case-insensitive. Filter patterns are case-insensitive `fnmatch` globs, checked against both the key and the name. Excludes always win over includes.

## Code style

- Keep the app small and dependency-light: plain Flask plus `requests`, and vanilla JS/CSS in the frontend. Don't add a build step or a frontend framework.
- Use the module `logger` (`logger.info/warning/error/exception`), not `print`.
- Always pass an explicit `timeout=` to `requests` calls.
- Follow the existing style: small pure helper functions (`parse_*`, `evaluate_*`, `normalize_*`) called from `background_fetcher`.

## Testing changes

There is no automated test suite yet. Before you commit:

1. Run `python3 -m py_compile app.py`.
2. Validate the JSON: `python3 -c "import json; json.load(open('config-example.json'))"`.
3. If you edited the Helm Secret, check that its embedded JSON parses. Load the YAML, then run `json.loads` on `stringData["config.json"]`.
4. Run `./startDevelop` (it expects a `.venv`), open `http://localhost:5001`, and check the `/api/status` output.
5. For a helper function, exercise it directly with valid, edge-case, and invalid inputs.

## Build and deploy

- **A push to `main` publishes the `latest` image right away,** and the chart pulls `latest` with `pullPolicy: Always`. Treat every merge to `main` as a production release.
- When the image and the config both change (for example, a new config key), roll out the new image together with the updated Secret. Old images silently ignore unknown keys.
- Changes to only `README.md` or `.gitignore` don't trigger a build.
- Gunicorn's `--timeout 120` limits HTTP request handling only. It doesn't affect the background poller.

## Git

- Write short commit messages in the imperative mood that describe the change.
- Don't commit from a dirty tree that includes gitignored config. Run `git status` and confirm that no secrets are staged.
