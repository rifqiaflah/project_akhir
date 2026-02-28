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
# SAFE INDEX CREATE (MINIMAL MAPPING)
# =========================
def safe_index_create(index_name, mapping):
    try:
        if not es.indices.exists(index=index_name):
            es.indices.create(
                index=index_name,
                mappings=mapping
            )
            print(f"Index {index_name} created")
    except Exception as e:
        print("Index create error:", e)


safe_index_create(INDEX_HOST, {
    "properties": {
        "host": {"type": "keyword"},
        "ip": {"type": "ip"},
        "available": {"type": "integer"},
        "cpu": {"type": "float"},
        "ram": {"type": "float"},
        "net_in": {"type": "float"},
        "net_out": {"type": "float"},
        "timestamp": {"type": "date"}
    }
})

safe_index_create(INDEX_PROBLEM, {
    "properties": {
        "eventid": {"type": "keyword"},
        "host": {"type": "keyword"},
        "name": {"type": "text"},
        "severity": {"type": "integer"},
        "timestamp": {"type": "date"}
    }
})

# =========================
# FORMAT BANDWIDTH
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
        r = requests.post(ZABBIX_URL, json=payload, timeout=(5, 20))
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
            "selectItems": ["key_", "lastvalue"]
        },
        "auth": token,
        "id": 2
    }

    try:
        r = requests.post(ZABBIX_URL, json=payload, timeout=(5, 20))
        return r.json().get("result", [])
    except:
        return []

# =========================
# GET PROBLEMS
# =========================
def get_problems():
    token = zabbix_login()
    if not token:
        return []

    payload = {
        "jsonrpc": "2.0",
        "method": "problem.get",
        "params": {
            "output": ["eventid", "name", "severity", "clock"],
            "selectHosts": ["host"],
            "recent": True
        },
        "auth": token,
        "id": 3
    }

    try:
        r = requests.post(ZABBIX_URL, json=payload, timeout=(5, 20))
        return r.json().get("result", [])
    except:
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
                    ip = interfaces[0].get("ip", "-")

                for item in h.get("items", []):
                    key = item.get("key_", "")
                    value = item.get("lastvalue", 0)

                    try:
                        value = float(value)
                    except:
                        value = 0

                    if key == "zabbix[host,agent,available]":
                        status = int(value)
                    elif "cpu" in key:
                        cpu = value
                    elif "memory" in key:
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

                es.update(
                    index=INDEX_HOST,
                    id=h.get("hostid"),
                    doc=doc,
                    doc_as_upsert=True
                )

            # SYNC PROBLEMS
            problems = get_problems()
            for p in problems:
                doc = {
                    "eventid": p.get("eventid"),
                    "host": p["hosts"][0]["host"] if p.get("hosts") else "-",
                    "name": p.get("name"),
                    "severity": int(p.get("severity", 0)),
                    "timestamp": datetime.utcfromtimestamp(int(p.get("clock", 0)))
                }

                es.update(
                    index=INDEX_PROBLEM,
                    id=p.get("eventid"),
                    doc=doc,
                    doc_as_upsert=True
                )

            print("Sync OK")

        except Exception as e:
            print("Sync error:", e)
            time.sleep(10)

        time.sleep(CACHE_TTL)

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
            size=1000
        )

        for h in result.get("hits", {}).get("hits", []):
            data = h.get("_source", {})

            net_in_value, net_in_unit = format_bandwidth(data.get("net_in", 0))
            net_out_value, net_out_unit = format_bandwidth(data.get("net_out", 0))

            data["net_in"] = net_in_value
            data["net_in_unit"] = net_in_unit
            data["net_out"] = net_out_value
            data["net_out_unit"] = net_out_unit

            hosts.append(data)

            status = int(data.get("available", 2))
            if status == 1:
                up += 1
            elif status == 0:
                down += 1
            else:
                unknown += 1

    except:
        pass

    # TOTAL SEMUA SERVER
    total = up + down + unknown

    # PERSENTASE HANYA UP VS DOWN
    total_for_percentage = up + down
    percent_up = round((up / total_for_percentage) * 100, 2) if total_for_percentage else 0

    # GET PROBLEMS
    problems = []
    try:
        result = es.search(
            index=INDEX_PROBLEM,
            size=50,
            sort=[{"timestamp": {"order": "desc"}}]
        )
        for h in result.get("hits", {}).get("hits", []):
            problems.append(h.get("_source", {}))
    except:
        pass

    tz = pytz.timezone(TIMEZONE)
    local_time = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

    return jsonify({
        "total": total,
        "up": up,
        "down": down,
        "unknown": unknown,
        "daily_uptime": percent_up,
        "hosts": hosts,
        "problems": problems,
        "time": local_time
    })

# =========================
# FRONTEND
# =========================
@app.route("/")
def index():
    return send_from_directory(".", "index.html")

# =========================
# RUN
# =========================
if __name__ == "__main__":
    thread = threading.Thread(target=sync_loop)
    thread.daemon = True
    thread.start()

    app.run(host=APP_HOST, port=APP_PORT, debug=DEBUG)
