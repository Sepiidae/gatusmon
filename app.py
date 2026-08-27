import time
import json
import re
import fnmatch
import threading
import requests
from flask import Flask, render_template, jsonify

app = Flask(__name__)

STATUS_CACHE = {"groups": {}, "last_updated": None}
CACHE_LOCK = threading.Lock()

def load_config():
    try:
        with open("config.json", "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"[Config Error] Failed to load config.json: {e}")
        return {}

CONFIG = load_config()

def parse_prometheus_pdu_data(text):
    """Parses Prometheus text format to extract PDU temperature and health status."""
    pdu_data = {}
    
    temp_pattern = re.compile(
        r'pdu_temperature_celsius\s*\{\s*pdu_name=["\']([^"\']+)["\']\s*\}\s+([0-9.]+)'
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
            pdu_name, temp_val = temp_match.groups()
            pdu_data.setdefault(pdu_name, {})["temperature"] = float(temp_val)

        health_match = health_pattern.search(line)
        if health_match:
            pdu_name, health_val = health_match.groups()
            pdu_data.setdefault(pdu_name, {})["health_status"] = float(health_val)

    return pdu_data

def evaluate_service_filter(key, name, original_group):
    filters = CONFIG.get("filters", {})
    exclude_patterns = filters.get("exclude", [])
    include_rules = filters.get("include", [])

    targets = [key.lower(), name.lower()]

    excluded = False
    for pat in exclude_patterns:
        pat_str = pat.lower() if isinstance(pat, str) else str(pat).lower()
        if any(fnmatch.fnmatch(t, pat_str) for t in targets):
            excluded = True
            break

    for rule in include_rules:
        pat = rule.get("pattern", "").lower()
        target_group = rule.get("group")
        if pat and any(fnmatch.fnmatch(t, pat) for t in targets):
            return True, target_group or original_group

    if excluded:
        return False, None

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

def get_effective_contact(key, group):
    service_contacts = CONFIG.get("service_contacts", {})
    if key in service_contacts:
        return normalize_contact(service_contacts[key])
    
    group_contacts = CONFIG.get("default_group_contacts", {})
    if group in group_contacts:
        return normalize_contact(group_contacts[group])

    return normalize_contact(group_contacts.get("General", None))

def evaluate_status(service_data):
    """Trust Gatus's pre-calculated health determination."""
    # 1. Use top-level Gatus status if provided (e.g. "SUCCESS", "HEALTHY", or boolean success)
    results = service_data.get("results", [])
    if not results:
        return "unknown"
    
    latest = results[-1]
    
    # 2. Check Gatus's built-in success flag
    if latest.get("success") is True:
        return "ok"
    elif latest.get("success") is False:
        return "critical"
        
    return "unknown"

def background_fetcher():
    """Background loop with strict per-request timeouts and error isolation."""
    while True:
        # Reload config on each iteration so changes apply automatically
        config = load_config()
        interval = config.get("refresh_interval_seconds", 60)
        
        fetched_services = []
        prom_pdu_metrics = {}

        for endpoint in config.get("endpoints", []):
            url = endpoint if isinstance(endpoint, str) else endpoint.get("url")
            verify_ssl = endpoint.get("verify_ssl", True) if isinstance(endpoint, dict) else True
            endpoint_type = endpoint.get("type", "gatus") if isinstance(endpoint, dict) else "gatus"
            
            if not url:
                continue

            try:
                # STRICT TIMEOUT: (3s connect timeout, 3s read timeout)
                # Prevents any hung server from blocking the thread
                resp = requests.get(url, timeout=(3.0, 3.0), verify=verify_ssl)
                
                if resp.status_code == 200:
                    if endpoint_type == "prometheus" or "text/plain" in resp.headers.get("Content-Type", ""):
                        metrics_parsed = parse_prometheus_pdu_data(resp.text)
                        prom_pdu_metrics.update(metrics_parsed)
                    else:
                        data = resp.json()
                        if isinstance(data, list):
                            fetched_services.extend(data)
                        else:
                            fetched_services.append(data)
                else:
                    print(f"[Worker Warning] HTTP {resp.status_code} from {url}")

            except requests.exceptions.Timeout:
                print(f"[Worker Timeout] Endpoint timed out after 3s: {url}")
            except requests.exceptions.RequestException as e:
                print(f"[Worker Error] Connection failed for {url}: {e}")
            except Exception as e:
                print(f"[Worker Error] Unexpected error processing {url}: {e}")

        # Group valid services
        grouped_services = {}

        for svc in fetched_services:
            try:
                key = svc.get("key", "")
                name = svc.get("name", "Unknown Service")
                raw_group = svc.get("group", "General")

                allowed, assigned_group = evaluate_service_filter(key, name, raw_group)
                if not allowed:
                    continue

                if assigned_group not in grouped_services:
                    grouped_services[assigned_group] = []

                contact = get_effective_contact(key, assigned_group)

                # Match PDU metrics
                matched_pdu = prom_pdu_metrics.get(name) or prom_pdu_metrics.get(key)
                temperature = matched_pdu.get("temperature") if matched_pdu else None

                status_val = evaluate_status(svc)
                if matched_pdu and matched_pdu.get("health_status") == 0.0:
                    status_val = "critical"

                grouped_services[assigned_group].append({
                    "name": name,
                    "group": assigned_group,
                    "key": key,
                    "status": status_val,
                    "temperature": temperature,
                    "details": svc.get("results", [])[-1] if svc.get("results") else {},
                    "contact": contact
                })
            except Exception as e:
                print(f"[Worker Error] Failed parsing service object: {e}")

        # Safely update memory cache
        with CACHE_LOCK:
            # Only overwrite group data if we retrieved results, otherwise retain last known good state
            if grouped_services or not STATUS_CACHE["groups"]:
                STATUS_CACHE["groups"] = grouped_services
            STATUS_CACHE["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")

        time.sleep(interval)

# Start Polling Thread
poller = threading.Thread(target=background_fetcher, daemon=True)
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