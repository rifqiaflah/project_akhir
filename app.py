from flask import Flask, jsonify, send_from_directory
from elasticsearch import Elasticsearch
import requests
import urllib3
from datetime import datetime, timedelta
import pytz
import threading
import time
from config import *

urllib3.disable_warnings()
app = Flask(__name__)

# =========================
# ELASTIC CONNECTION
# =========================
es = Elasticsearch(
    ELASTIC_HOST,
    basic_auth=(ELASTIC_USER, ELASTIC_PASS),
    verify_certs=False
)

# =========================
# SAFE INDEX CREATE
# =========================
def safe_index_create(index_name):
    try:
        if not es.indices.exists(index=index_name):
            es.indices.create(index=index_name)
    except Exception as e:
        print("Index create error:", e)

safe_index_create(INDEX_HOST)
safe_index_create(INDEX_PROBLEM)

# =========================
# ZABBIX LOGIN
# =========================
def zabbix_login():
    try:
        payload = {
            "jsonrpc": "2.0",
            "method": "user.login",
            "params": {
                "username": ZABBIX_USER,
                "password": ZABBIX_PASSWORD
            },
            "id": 1
        }
        r = requests.post(ZABBIX_URL, json=payload, timeout=10)
        return r.json().get("result")
    except Exception as e:
        print("Zabbix login error:", e)
        return None

# =========================
# GET HOST FROM ZABBIX
# =========================
def get_hosts():
    token = zabbix_login()
    if not token:
        return []

    try:
        payload = {
            "jsonrpc": "2.0",
            "method": "host.get",
            "params": {
                "output": ["hostid", "host"],
                "selectInterfaces": ["available", "ip"],
                "selectItems": ["name", "lastvalue"]
            },
            "auth": token,
            "id": 2
        }

        r = requests.post(ZABBIX_URL, json=payload, timeout=15)
        return r.json().get("result", [])
    except Exception as e:
        print("Zabbix host error:", e)
        return []

# =========================
# BACKGROUND SYNC LOOP
# =========================
def sync_loop():
    while True:
        try:
            hosts = get_hosts()

            for h in hosts:
                cpu = ram = net_in = net_out = 0
                status = 2
                ip = "-"

                interfaces = h.get("interfaces", [])
                if interfaces:
                    status = int(interfaces[0].get("available", 2))
                    ip = interfaces[0].get("ip", "-")

                for item in h.get("items", []):
                    name = item.get("name", "").lower()
                    value = item.get("lastvalue", 0)

                    try:
                        value = float(value)
                    except:
                        value = 0

                    if "cpu" in name:
                        cpu = value
                    elif "memory" in name:
                        ram = value
                    elif "incoming" in name:
                        net_in = value
                    elif "outgoing" in name:
                        net_out = value

                doc = {
                    "host": h.get("host", "unknown"),
                    "ip": ip,
                    "available": status,
                    "cpu": cpu,
                    "ram": ram,
                    "net_in": net_in,
                    "net_out": net_out,
                    "timestamp": datetime.utcnow()
                }

                es.index(index=INDEX_HOST, id=h.get("hostid"), document=doc)

            print("Host sync OK")

        except Exception as e:
            print("Background sync error:", e)

        time.sleep(CACHE_TTL)

# =========================
# SAFE COUNT
# =========================
def safe_count(index_name, body=None):
    try:
        if es.indices.exists(index=index_name):
            if body:
                return es.count(index=index_name, body=body)["count"]
            return es.count(index=index_name)["count"]
    except:
        pass
    return 0

# =========================
# GET LOGS
# =========================
def get_logs():
    logs = []
    try:
        if es.indices.exists(index=INDEX_LOG):
            result = es.search(
                index=INDEX_LOG,
                size=50,
                sort=[{"@timestamp": {"order": "desc"}}]
            )
            for h in result.get("hits", {}).get("hits", []):
                src = h.get("_source", {})
                logs.append({
                    "time": src.get("@timestamp", ""),
                    "message": src.get("message", "")
                })
    except Exception as e:
        print("Log fetch error:", e)

    return logs

# =========================
# DASHBOARD API
# =========================
@app.route("/api/dashboard")
def dashboard():

    hosts = []
    total = up = down = unknown = 0

    try:
        if es.indices.exists(index=INDEX_HOST):
            result = es.search(index=INDEX_HOST, size=1000)
            hits = result.get("hits", {}).get("hits", [])

            for h in hits:
                data = h.get("_source", {})
                status = data.get("available", 2)

                if status == 1:
                    up += 1
                elif status == 0:
                    down += 1
                else:
                    unknown += 1

                hosts.append(data)

            total = len(hits)

    except Exception as e:
        print("Host read error:", e)

    percent_up = round((up / total) * 100, 2) if total > 0 else 0

    now = datetime.utcnow()
    yesterday = now - timedelta(hours=24)

    time_filter = {
        "range": {
            "@timestamp": {
                "gte": yesterday.isoformat(),
                "lte": now.isoformat()
            }
        }
    }

    bruteforce_query = {
        "query": {
            "bool": {
                "must": [time_filter],
                "should": [
                    {"wildcard": {"message": "*Failed password*"}},
                    {"wildcard": {"message": "*authentication failure*"}}
                ],
                "minimum_should_match": 1
            }
        }
    }

    ddos_query = {
        "query": {
            "bool": {
                "must": [time_filter],
                "filter": [{"match": {"message": "HTTP"}}]
            }
        }
    }

    bruteforce = safe_count(INDEX_LOG, bruteforce_query)
    ddos = safe_count(INDEX_LOG, ddos_query)
    problems = safe_count(INDEX_PROBLEM)
    logs = get_logs()

    try:
        tz = pytz.timezone(TIMEZONE)
        local_time = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    except:
        local_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    return jsonify({
        "total": total,
        "up": up,
        "down": down,
        "unknown": unknown,
        "daily_uptime": percent_up,
        "hosts": hosts,
        "bruteforce": bruteforce,
        "ddos": ddos,
        "problems": problems,
        "logs": logs,
        "time": local_time
    })

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

# =========================
# START APP
# =========================
if __name__ == "__main__":
    thread = threading.Thread(target=sync_loop)
    thread.daemon = True
    thread.start()

    app.run(host=APP_HOST, port=APP_PORT, debug=DEBUG)
