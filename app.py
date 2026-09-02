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

            # Prioritization logic:
            # 1. If temperature isn't set yet, set it.
            # 2. If the current reading is NOT 'Ambient', but the new one IS 'Ambient', overwrite it.
            if "temperature" not in pdu_entry or (current_sensor != "Ambient" and sensor_descr == "Ambient"):
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

        for endpoint in endpoints:
            url = endpoint if isinstance(endpoint, str) else endpoint.get("url")
            verify_ssl = endpoint.get("verify_ssl", True) if isinstance(endpoint, dict) else True
            endpoint_type = endpoint.get("type", "gatus") if isinstance(endpoint, dict) else "gatus"
            
            if not url:
                continue

            try:
                resp = requests.get(url, timeout=(3.0, 3.0), verify=verify_ssl)
                if resp.status_code == 200:
                    content_type = resp.headers.get("Content-Type", "")
                    if endpoint_type == "prometheus" or "text/plain" in content_type:
                        prom_pdu_metrics.update(parse_prometheus_pdu_data(resp.text))
                    else:
                        data = resp.json()
                        if isinstance(data, list):
                            fetched_services.extend(data)
                        else:
                            fetched_services.append(data)
            except Exception as e:
                logger.error("Error connecting to %s: %s", url, e)

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