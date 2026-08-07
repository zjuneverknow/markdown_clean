import json
import urllib.request
import urllib.error

BASE_URL = "https://llm-f8bdo8xv72sgr4cu.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
API_KEY = "sk-ws-H.EDIMIMR.ACyF.MEUCIQC198tB5d7LKIKgXFixbIkuuLE_4wl1oR4VOd49z1rvLQIgUhrqIatEcgBuPpZnx2NM3V9qDNtSd9Jf0wvMBzc2PS4"

payload = {
    # gpt-5.6-sol 在公司平台中要求特定客户端通道；通用 OpenAI API
    # 调用请使用已验证可用的文本模型。
    "model": "deepseek-v4-flash",
    "temperature": 0,
    "messages": [
        {"role": "user", "content": "hello"}
    ],
}

req = urllib.request.Request(
    BASE_URL + "/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=120) as response:
        print(response.read().decode())

except urllib.error.HTTPError as e:
    print("HTTP:", e.code)
    print(e.read().decode())
