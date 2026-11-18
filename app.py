import time
import json
import re
import fnmatch
import threading
import logging
import requests
from flask import Flask, render_template, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

STATUS_CACHE = {"groups": {}, "last_updated": None}
CACHE_LOCK = threading.Lock()

def load_config():
    try:
        with open("config.json", "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed loading config.json: %s", e)
        return {}

def parse_prometheus_pdu_data(text):
    pdu_data = {}
    
    # Updated regex to explicitly extract sensor_descr if present
    temp_pattern = re.compile(
        r'pdu_temperature_celsius\s*\{\s*pdu_name=["\']([^"\']+)["\'](?:,\s*sensor_descr=["\']([^"\']+)["\'])?\s*\}\s+([0-9.]+)'
    )
    health_pattern = re.compile(
        r'pdu_health_status\s*\{\s*pdu_name=["\']([^"\']+)["\']\s*\}\s+([0-9.]+)'
    )

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        temp_match = temp_pattern.search(line)
        if temp_match:
            pdu_name, sensor_descr, temp_val = temp_match.groups()
            temp_float = float(temp_val)
            
            pdu_entry = pdu_data.setdefault(pdu_name, {})
            current_sensor = pdu_entry.get("_temp_sensor")
            if temp_float <= 0:
                logger.debug("Ignoring non-positive temperature reading for PDU '%s' (sensor: '%s'): %f", pdu_name, sensor_descr, temp_float)
                continue
            # Prioritization logic:
            # 1. If temperature isn't set yet, set it.
            # 2. If the current reading is NOT 'Ambient', but the new one IS 'Ambient', overwrite it.
            if "temperature" not in pdu_entry or (current_sensor != "Ambient" and sensor_descr == "Ambient" ):
                pdu_entry["temperature"] = temp_float
                pdu_entry["_temp_sensor"] = sensor_descr

        health_match = health_pattern.search(line)
        if health_match:
            pdu_name, health_val = health_match.groups()
            pdu_data.setdefault(pdu_name, {})["health_status"] = float(health_val)

    # Clean up internal metadata key before returning
    for entry in pdu_data.values():
        entry.pop("_temp_sensor", None)

    return pdu_data

DEFAULT_TIMEOUT = (3.0, 3.0)  # (connect, read) seconds

def parse_timeout(value, fallback):
    """Accepts a number (applied to connect and read) or a [connect, read] pair."""
    if value is None:
        return fallback
    try:
        if isinstance(value, (list, tuple)):
            if len(value) != 2:
                raise ValueError("expected [connect, read]")
            return (float(value[0]), float(value[1]))
        return (float(value), float(value))
    except (TypeError, ValueError) as e:
        logger.warning("Invalid timeout %r (%s); using %s", value, e, fallback)
        return fallback

def evaluate_service_filter(key, name, original_group, config):
    filters = config.get("filters", {})
    exclude_patterns = filters.get("exclude", [])
    include_rules = filters.get("include", [])

    targets = [key.lower(), name.lower()]

    for pat in exclude_patterns:
        pat_str = pat.lower() if isinstance(pat, str) else str(pat).lower()
        if any(fnmatch.fnmatch(t, pat_str) for t in targets):
            return False, None

    for rule in include_rules:
        pat = rule.get("pattern", "").lower()
        target_group = rule.get("group")
        if pat and any(fnmatch.fnmatch(t, pat) for t in targets):
            return True, target_group or original_group

    return True, original_group

def normalize_contact(contact_raw):
    if not contact_raw:
        return None

    phones = contact_raw.get("phones") or contact_raw.get("phone") or []
    emails = contact_raw.get("emails") or contact_raw.get("email") or []

    if isinstance(phones, str):
        phones = [phones]
    if isinstance(emails, str):
        emails = [emails]

    return {
        "name": contact_raw.get("name", "Support Contact"),
        "phones": phones,
        "emails": emails
    }

def get_effective_contact(key, group, config):
    service_contacts = config.get("service_contacts", {})
    if key in service_contacts:
        return normalize_contact(service_contacts[key])
    
    group_contacts = config.get("default_group_contacts", {})
    if group in group_contacts:
        return normalize_contact(group_contacts[group])

    return normalize_contact(group_contacts.get("General", None))

def evaluate_status(service_data):
    results = service_data.get("results", [])
    if not results:
        return "unknown"
    
    latest = results[-1]
    success_state = latest.get("success")

    if success_state is True:
        return "ok"
    elif success_state is False:
        return "critical"
        
    return "unknown"

def background_fetcher():
    logger.info("Background polling worker thread initialized")
    
    while True:
        config = load_config()
        interval = config.get("refresh_interval_seconds", 60)
        temp_unit = config.get("temperature_unit", "C")
        
        # Build case-insensitive priority map
        group_priorities = config.get("group_priority", {})
        priorities_lower = {k.strip().lower(): v for k, v in group_priorities.items()}
        
        fetched_services = []
        prom_pdu_metrics = {}
        endpoints = config.get("endpoints", [])
        default_timeout = parse_timeout(config.get("default_timeout_seconds"), DEFAULT_TIMEOUT)
        if not endpoints:
            logger.warning("No endpoints configured in config.json; nothing to poll")

        cycle_start = time.monotonic()
        endpoints_ok = 0

        for endpoint in endpoints:
            url = endpoint if isinstance(endpoint, str) else endpoint.get("url")
            verify_ssl = endpoint.get("verify_ssl", True) if isinstance(endpoint, dict) else True
            endpoint_type = endpoint.get("type", "gatus") if isinstance(endpoint, dict) else "gatus"
            timeout = parse_timeout(endpoint.get("timeout_seconds"), default_timeout) if isinstance(endpoint, dict) else default_timeout

            if not url:
                logger.warning("Skipping endpoint entry with no 'url': %r", endpoint)
                continue

            started = time.monotonic()
            try:
                resp = requests.get(url, timeout=timeout, verify=verify_ssl)
                elapsed = time.monotonic() - started

                if resp.status_code != 200:
                    logger.error(
                        "Failed loading %s status from %s: HTTP %s after %.2fs (body: %.200s)",
                        endpoint_type, url, resp.status_code, elapsed, resp.text.strip()
                    )
                    continue

                content_type = resp.headers.get("Content-Type", "")
                if endpoint_type == "prometheus" or "text/plain" in content_type:
                    metrics = parse_prometheus_pdu_data(resp.text)
                    if not metrics:
                        logger.warning("No PDU metrics parsed from %s (%.2fs)", url, elapsed)
                    prom_pdu_metrics.update(metrics)
                    logger.debug("Loaded %d PDU metrics from %s in %.2fs", len(metrics), url, elapsed)
                else:
                    data = resp.json()
                    if isinstance(data, list):
                        if not data:
                            logger.warning("Gatus endpoint %s returned an empty status list (%.2fs)", url, elapsed)
                        fetched_services.extend(data)
                        count = len(data)
                    elif isinstance(data, dict):
                        fetched_services.append(data)
                        count = 1
                    else:
                        logger.error(
                            "Unexpected Gatus payload from %s: expected list or object, got %s",
                            url, type(data).__name__
                        )
                        continue
                    logger.debug("Loaded %d services from %s in %.2fs", count, url, elapsed)

                endpoints_ok += 1

            except requests.exceptions.ConnectTimeout:
                logger.error(
                    "Timeout connecting to %s: no connection within %.1fs (connect timeout)",
                    url, timeout[0]
                )
            except requests.exceptions.ReadTimeout:
                logger.error(
                    "Timeout reading from %s: no response within %.1fs (read timeout; consider raising 'timeout_seconds')",
                    url, timeout[1]
                )
            except requests.exceptions.SSLError as e:
                logger.error("TLS/SSL error for %s (verify_ssl=%s): %s", url, verify_ssl, e)
            except requests.exceptions.ConnectionError as e:
                logger.error("Connection error for %s: %s", url, e)
            except requests.exceptions.JSONDecodeError as e:
                logger.error(
                    "Invalid JSON from %s (Content-Type %r): %s (body: %.200s)",
                    url, resp.headers.get("Content-Type"), e, resp.text.strip()
                )
            except requests.exceptions.RequestException as e:
                logger.error("Request to %s failed: %s", url, e)
            except Exception:
                logger.exception("Unexpected error loading status from %s", url)

        cycle_elapsed = time.monotonic() - cycle_start
        failed = len(endpoints) - endpoints_ok
        log_cycle = logger.warning if failed else logger.info
        log_cycle(
            "Poll cycle finished in %.2fs: %d/%d endpoints OK, %d services, %d PDU metrics",
            cycle_elapsed, endpoints_ok, len(endpoints), len(fetched_services), len(prom_pdu_metrics)
        )
        if cycle_elapsed > interval:
            logger.warning(
                "Poll cycle took %.2fs, longer than refresh_interval_seconds (%ss); "
                "reduce endpoint timeouts or raise the interval", cycle_elapsed, interval
            )

        grouped_services = {}

        for svc in fetched_services:
            try:
                key = svc.get("key", "")
                name = svc.get("name", "Unknown Service")
                raw_group = svc.get("group", "General")

                allowed, assigned_group = evaluate_service_filter(key, name, raw_group, config)
                if not allowed:
                    continue

                if assigned_group not in grouped_services:
                    grouped_services[assigned_group] = []

                contact = get_effective_contact(key, assigned_group, config)
                matched_pdu = prom_pdu_metrics.get(name) or prom_pdu_metrics.get(key)
                temperature = matched_pdu.get("temperature") if matched_pdu else None
                if temperature is not None and temperature == 0:
                    logger.warning("Temperature reading for service '%s' is 0, which may indicate a sensor issue.", name)
                if temperature is not None and temp_unit == "F":
                    temperature = (temperature * 9/5) + 32

                status_val = evaluate_status(svc)
                if matched_pdu and matched_pdu.get("health_status") == 0.0:
                    status_val = "critical"

                # Case-insensitive importance extraction
                importance = priorities_lower.get(assigned_group.strip().lower(), 0)

                grouped_services[assigned_group].append({
                    "name": name,
                    "group": assigned_group,
                    "key": key,
                    "status": status_val,
                    "importance": importance,
                    "temperature": temperature,
                    "temperature_unit": temp_unit,
                    "details": svc.get("results", [])[-1] if svc.get("results") else {},
                    "contact": contact
                })
            except Exception as e:
                logger.exception("Failed parsing service object: %s", e)

        with CACHE_LOCK:
            if grouped_services or not STATUS_CACHE["groups"]:
                STATUS_CACHE["groups"] = grouped_services
            else:
                logger.warning(
                    "No services loaded this cycle; keeping previous data from %s",
                    STATUS_CACHE["last_updated"]
                )
            STATUS_CACHE["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")

        time.sleep(interval)

poller = threading.Thread(target=background_fetcher, name="PollerThread", daemon=True)
poller.start()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    with CACHE_LOCK:
        return jsonify(STATUS_CACHE)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)