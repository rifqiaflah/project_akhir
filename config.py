# ================= ZABBIX =================
ZABBIX_URL = "http://192.168.59.1/zabbix/api_jsonrpc.php"
ZABBIX_USER = "Admin"
ZABBIX_PASSWORD = "zabbix"

# ================= ELASTIC =================
ELASTIC_HOST = "https://192.168.59.1:9200"
ELASTIC_USER = "elastic"
ELASTIC_PASS = "KhWY4qwz1tTT6U9v7ebL"

INDEX_HOST = "soc-host-monitor"
INDEX_PROBLEM = "soc-problem-monitor"

# LOG INDEX (filebeat dynamic index)
INDEX_LOG = ".ds-filebeat-*"

# ================= APP =================
CACHE_TTL = 10
REFRESH_INTERVAL = 5000  # ms frontend
TIMEZONE = "Asia/Jakarta"

APP_HOST = "0.0.0.0"
APP_PORT = 5000
DEBUG = False

