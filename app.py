import time
import json
import re
import fnmatch
import threading
import logging
import requests
from flask import Flask, render_template, jsonify

# Configure Python logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

STATUS_CACHE = {"groups": {}, "last_updated": None}
CACHE_LOCK = threading.Lock()

def load_config():
    logger.debug("Attempting to load config.json")
    try:
        with open("config.json", "r") as f:
            config = json.load(f)
            logger.info("Successfully loaded config.json")
            return config
    except FileNotFoundError:
        logger.error("config.json not found. Returning empty configuration.")
        return {}
    except json.JSONDecodeError as e:
        logger.error("Failed to parse config.json (invalid JSON): %s", e)
        return {}
    except Exception as e:
        logger.exception("Unexpected error loading config.json: %s", e)
        return {}

CONFIG = load_config()

def parse_prometheus_pdu_data(text):
    """Parses Prometheus text format to extract PDU temperature and health status."""
    logger.debug("Parsing Prometheus PDU metrics text (%d characters)", len(text))
    pdu_data = {}
    
    temp_pattern = re.compile(
        r'pdu_temperature_celsius\s*\{\s*pdu_name=["\']([^"\']+)["\']\s*\}\s+([0-9.]+)'
    )
    health_pattern = re.compile(
        r'pdu_health_status\s*\{\s*pdu_name=["\']([^"\']+)["\']\s*\}\s+([0-9.]+)'
    )

    lines_processed = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        lines_processed += 1
        temp_match = temp_pattern.search(line)
        if temp_match:
            pdu_name, temp_val = temp_match.groups()
            pdu_data.setdefault(pdu_name, {})["temperature"] = float(temp_val)
            logger.debug("Parsed temp metric for PDU '%s': %s C", pdu_name, temp_val)

        health_match = health_pattern.search(line)
        if health_match:
            pdu_name, health_val = health_match.groups()
            pdu_data.setdefault(pdu_name, {})["health_status"] = float(health_val)
            logger.debug("Parsed health metric for PDU '%s': %s", pdu_name, health_val)

    logger.info("Prometheus parsing completed. Extracted metrics for %d PDUs from %d lines", len(pdu_data), lines_processed)
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
            logger.debug("Service '%s' (%s) matched exclude pattern '%s'", name, key, pat_str)
            break

    for rule in include_rules:
        pat = rule.get("pattern", "").lower()
        target_group = rule.get("group")
        if pat and any(fnmatch.fnmatch(t, pat) for t in targets):
            final_group = target_group or original_group
            logger.debug("Service '%s' (%s) explicitly included via pattern '%s' -> group '%s'", name, key, pat, final_group)
            return True, final_group

    if excluded:
        logger.info("Service '%s' (%s) filtered out by exclusion rule", name, key)
        return False, None

    logger.debug("Service '%s' (%s) retained under original group '%s'", name, key, original_group)
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
        logger.debug("Using service-specific contact for key '%s'", key)
        return normalize_contact(service_contacts[key])
    
    group_contacts = CONFIG.get("default_group_contacts", {})
    if group in group_contacts:
        logger.debug("Using group-level contact for group '%s'", group)
        return normalize_contact(group_contacts[group])

    logger.debug("Falling back to General contact for key '%s'", key)
    return normalize_contact(group_contacts.get("General", None))

def evaluate_status(service_data):
    """Trust Gatus's pre-calculated health determination."""
    results = service_data.get("results", [])
    if not results:
        logger.debug("No results vector found in service payload, marking as 'unknown'")
        return "unknown"
    
    latest = results[-1]
    success_state = latest.get("success")

    if success_state is True:
        return "ok"
    elif success_state is False:
        return "critical"
        
    logger.debug("Service state ambiguous (%s), falling back to 'unknown'", success_state)
    return "unknown"

def background_fetcher():
    """Background loop with strict per-request timeouts and error isolation."""
    logger.info("Background polling worker thread initialized and starting loop")
    
    while True:
        logger.info("Starting background fetch iteration...")
        config = load_config()
        interval = config.get("refresh_interval_seconds", 60)
        
        fetched_services = []
        prom_pdu_metrics = {}
        endpoints = config.get("endpoints", [])
        
        logger.info("Found %d endpoint(s) to fetch", len(endpoints))

        for endpoint in endpoints:
            url = endpoint if isinstance(endpoint, str) else endpoint.get("url")
            verify_ssl = endpoint.get("verify_ssl", True) if isinstance(endpoint, dict) else True
            endpoint_type = endpoint.get("type", "gatus") if isinstance(endpoint, dict) else "gatus"
            
            if not url:
                logger.warning(f"Encountered empty or invalid endpoint target in config, skipping {endpoint}")
                continue

            logger.info("Fetching data from endpoint: %s (Type: %s, Verify SSL: %s)", url, endpoint_type, verify_ssl)

            try:
                resp = requests.get(url, timeout=(3.0, 3.0), verify=verify_ssl)
                logger.debug("HTTP response %d received from %s", resp.status_code, url)
                
                if resp.status_code == 200:
                    content_type = resp.headers.get("Content-Type", "")
                    if endpoint_type == "prometheus" or "text/plain" in content_type:
                        logger.info("Parsing Prometheus data from %s", url)
                        metrics_parsed = parse_prometheus_pdu_data(resp.text)
                        prom_pdu_metrics.update(metrics_parsed)
                    else:
                        logger.info("Parsing JSON service response from %s", url)
                        data = resp.json()
                        if isinstance(data, list):
                            fetched_services.extend(data)
                            logger.debug("Received list of %d service items from %s", len(data), url)
                        else:
                            fetched_services.append(data)
                            logger.debug("Received single service object from %s", url)
                else:
                    logger.warning("Non-200 HTTP response (%d) received from %s", resp.status_code, url)

            except requests.exceptions.Timeout:
                logger.error("Request timeout (3.0s limit) reaching endpoint: %s", url)
            except requests.exceptions.SSLError as e:
                logger.error("SSL Verification error for %s: %s", url, e)
            except requests.exceptions.RequestException as e:
                logger.error("HTTP request exception connecting to %s: %s", url, e)
            except Exception as e:
                logger.exception("Unexpected error processing response from %s: %s", url, e)

        logger.info("Processing and grouping %d total fetched service record(s)", len(fetched_services))
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

                matched_pdu = prom_pdu_metrics.get(name) or prom_pdu_metrics.get(key)
                temperature = matched_pdu.get("temperature") if matched_pdu else None

                status_val = evaluate_status(svc)
                if matched_pdu and matched_pdu.get("health_status") == 0.0:
                    logger.warning("PDU critical condition detected for %s! Overriding status to 'critical'", name)
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
                logger.exception("Failed parsing service object: %s", e)

        # Update cache safely
        with CACHE_LOCK:
            if grouped_services or not STATUS_CACHE["groups"]:
                STATUS_CACHE["groups"] = grouped_services
                logger.info("STATUS_CACHE updated with %d group(s)", len(grouped_services))
            else:
                logger.warning("Fetch yielded no services. Retaining last known good cache state.")
            
            STATUS_CACHE["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            logger.debug("Cache updated timestamp set to %s", STATUS_CACHE["last_updated"])

        logger.info("Iteration completed. Sleeping for %d seconds...", interval)
        time.sleep(interval)

# Start Polling Thread
logger.info("Starting background poller daemon thread...")
poller = threading.Thread(target=background_fetcher, name="PollerThread", daemon=True)
poller.start()

@app.route("/")
def index():
    logger.debug("Route GET / hit")
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    logger.debug("Route GET /api/status hit")
    with CACHE_LOCK:
        return jsonify(STATUS_CACHE)

if __name__ == "__main__":
    logger.info("Starting Flask server on 0.0.0.0:5000...")
    app.run(host="0.0.0.0", port=5000, debug=False)