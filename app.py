from flask import Flask, jsonify, send_from_directory
from elasticsearch import Elasticsearch
import requests
import urllib3
from datetime import datetime
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
# FORMAT BANDWIDTH AUTO UNIT
# =========================
def format_bandwidth(value):
    try:
        value = float(value)

        if value >= 1_000_000_000:
            return round(value / 1_000_000_000, 2), "Gbps"
        elif value >= 1_000_000:
            return round(value / 1_000_000, 2), "Mbps"
        elif value >= 1_000:
            return round(value / 1_000, 2), "Kbps"
        else:
            return round(value, 2), "bps"
    except:
        return 0, "bps"

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
    except:
        return None

# =========================
# GET HOSTS
# =========================
def get_hosts():
    token = zabbix_login()
    if not token:
        return []

    payload = {
        "jsonrpc": "2.0",
        "method": "host.get",
        "params": {
            "output": ["hostid", "host"],
            "selectInterfaces": ["available", "ip"],
            "selectItems": ["name", "key_", "lastvalue"]
        },
        "auth": token,
        "id": 2
    }

    r = requests.post(ZABBIX_URL, json=payload, timeout=15)
    return r.json().get("result", [])

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
                    ip = interfaces[0].get("ip", "-")

                for item in h.get("items", []):
                    name = item.get("name", "").lower()
                    key = item.get("key_", "")
                    value = item.get("lastvalue", 0)

                    try:
                        value = float(value)
                    except:
                        value = 0

                    if key == "zabbix[host,agent,available]":
                        status = int(value)
                    elif "cpu" in name:
                        cpu = value
                    elif "memory" in name:
                        ram = value
                    elif "net.if.in" in key:
                        net_in += value
                    elif "net.if.out" in key:
                        net_out += value

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
            print("Sync error:", e)

        time.sleep(CACHE_TTL)

# =========================
# GET LOGS
# =========================
def get_logs():
    logs = []
    try:
        result = es.search(
            index=INDEX_LOG,
            size=50,
            sort=[{"@timestamp": {"order": "desc"}}],
            query={"range": {"@timestamp": {"gte": "now-24h"}}}
        )

        for h in result.get("hits", {}).get("hits", []):
            src = h.get("_source", {})
            logs.append({
                "time": src.get("@timestamp", "")[:19],
                "message": src.get("message", "")
            })
    except:
        pass
    return logs

# =========================
# SAFE COUNT
# =========================
def safe_count(index_name, body):
    try:
        return es.count(index=index_name, body=body)["count"]
    except:
        return 0

# =========================
# DASHBOARD API
# =========================
@app.route("/api/dashboard")
def dashboard():

    hosts = []
    up = down = unknown = 0

    try:
        result = es.search(
            index=INDEX_HOST,
            size=1000,
            sort=[{"timestamp": {"order": "desc"}}]
        )

        latest_hosts = {}

        for h in result.get("hits", {}).get("hits", []):
            data = h.get("_source", {})
            host_name = data.get("host")
            if host_name not in latest_hosts:
                latest_hosts[host_name] = data

        for h in latest_hosts.values():
            net_in_value, net_in_unit = format_bandwidth(h.get("net_in", 0))
            net_out_value, net_out_unit = format_bandwidth(h.get("net_out", 0))

            h["net_in"] = net_in_value
            h["net_in_unit"] = net_in_unit
            h["net_out"] = net_out_value
            h["net_out_unit"] = net_out_unit

            hosts.append(h)

            status = int(h.get("available", 2))
            if status == 1:
                up += 1
            elif status == 0:
                down += 1
            else:
                unknown += 1

    except:
        pass

    total = len(hosts)
    percent_up = round((up / total) * 100, 2) if total else 0

    bruteforce_query = {
        "query": {
            "bool": {
                "must": [{"range": {"@timestamp": {"gte": "now-5m"}}}],
                "should": [
                    {"match_phrase": {"message": "Failed password"}},
                    {"match_phrase": {"message": "authentication failure"}},
                    {"match_phrase": {"message": "Invalid user"}},
                    {"match_phrase": {"message": "Failed publickey"}}
                ],
                "minimum_should_match": 1
            }
        }
    }

    bruteforce = safe_count(INDEX_LOG, bruteforce_query)

    traffic_query = {
        "query": {"range": {"@timestamp": {"gte": "now-1m"}}}
    }

    request_count = safe_count(INDEX_LOG, traffic_query)
    DDOS_THRESHOLD = 200
    ddos = request_count if request_count > DDOS_THRESHOLD else 0

    logs = get_logs()

    tz = pytz.timezone(TIMEZONE)
    local_time = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

    return jsonify({
        "total": total,
        "up": up,
        "down": down,
        "unknown": unknown,
        "daily_uptime": percent_up,
        "hosts": hosts,
        "bruteforce": bruteforce,
        "ddos": ddos,
        "logs": logs,
        "time": local_time
    })

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

if __name__ == "__main__":
    thread = threading.Thread(target=sync_loop)
    thread.daemon = True
    thread.start()

    app.run(host=APP_HOST, port=APP_PORT, debug=DEBUG)
