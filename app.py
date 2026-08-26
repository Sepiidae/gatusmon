import time
import json
import fnmatch
import threading
import requests
from flask import Flask, render_template, jsonify

app = Flask(__name__)

STATUS_CACHE = {"groups": {}, "last_updated": None}
CACHE_LOCK = threading.Lock()

def load_config():
    with open("config.json", "r") as f:
        return json.load(f)

CONFIG = load_config()

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
    """Ensures phones and emails are always returned as list structures."""
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
    results = service_data.get("results", [])
    if not results:
        return "unknown"
    
    latest = results[-1]
    if latest.get("status") != 200:
        return "critical"
    
    conditions = latest.get("conditionResults", [])
    all_passed = all(c.get("success", False) for c in conditions)
    
    return "ok" if (latest.get("success", False) and all_passed) else "warning"

def background_fetcher():
    while True:
        interval = CONFIG.get("refresh_interval_seconds", 60)
        fetched_services = []

        for url in CONFIG.get("endpoints", []):
            try:
                resp = requests.get(url, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        fetched_services.extend(data)
                    else:
                        fetched_services.append(data)
            except Exception as e:
                print(f"[Worker Error] Fetch failed for {url}: {e}")

        grouped_services = {}

        for svc in fetched_services:
            key = svc.get("key", "")
            name = svc.get("name", "Unknown Service")
            raw_group = svc.get("group", "General")

            allowed, assigned_group = evaluate_service_filter(key, name, raw_group)
            if not allowed:
                continue

            if assigned_group not in grouped_services:
                grouped_services[assigned_group] = []

            contact = get_effective_contact(key, assigned_group)

            grouped_services[assigned_group].append({
                "name": name,
                "group": assigned_group,
                "key": key,
                "status": evaluate_status(svc),
                "details": svc.get("results", [])[-1] if svc.get("results") else {},
                "contact": contact
            })

        with CACHE_LOCK:
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