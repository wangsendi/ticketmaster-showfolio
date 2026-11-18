"""离线核对 HAR、实际认证结果与导出的 Cookie；不发网络请求。"""
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit


def response_json(entry):
    content = entry["response"].get("content", {})
    text = content.get("text", "")
    if content.get("encoding") == "base64":
        text = base64.b64decode(text).decode("utf-8")
    return json.loads(text)


def contains_account(value, email):
    if isinstance(value, str):
        return value.casefold() == email.casefold()
    if isinstance(value, dict):
        return any(contains_account(item, email) for item in value.values())
    if isinstance(value, list):
        return any(contains_account(item, email) for item in value)
    return False


def load_har(run_directory):
    raw = (Path(run_directory) / "com-login.har").read_bytes()
    return json.loads(raw)["log"]["entries"], hashlib.sha256(raw).hexdigest()


def audit(run_directory, cookie_file=None, expected_email=None, *, har=None):
    directory = Path(run_directory)
    entries, har_hash = load_har(directory) if har is None else har
    email_posts, password_posts, exchanges, fans = [], [], [], []
    ca_requests = 0
    for index, entry in enumerate(entries):
        request, response = entry["request"], entry["response"]
        url = urlsplit(request["url"])
        host = url.hostname or ""
        if host == "ticketmaster.ca" or host.endswith(".ticketmaster.ca"):
            ca_requests += 1
        if host == "auth.ticketmaster.com" and request["method"] == "POST":
            if url.path == "/json/account-status-validation":
                email_posts.append((index, entry))
            elif url.path == "/json/sign-in":
                password_posts.append((index, entry))
        if host == "identity.ticketmaster.com" and url.path == "/exchange" and 200 <= response["status"] < 400:
            exchanges.append(index)
        if host == "signup.ticketmaster.com" and url.path == "/graphql" and response["status"] == 200:
            try:
                fan = response_json(entry).get("data", {}).get("fan", {})
                if isinstance(fan, dict) and fan.get("isLoggedIn") is True:
                    fans.append((index, entry, fan))
            except (ValueError, AttributeError, UnicodeError):
                continue
    if len(email_posts) != 1 or email_posts[0][1]["response"]["status"] != 200:
        raise ValueError("没有唯一且成功的邮箱校验请求。")
    if len(password_posts) != 1 or password_posts[0][1]["response"]["status"] != 200:
        raise ValueError("没有唯一且成功的密码请求。")
    password_data = json.loads(password_posts[0][1]["request"].get("postData", {}).get("text", "{}"))
    token = password_data.get("recaptchaToken")
    if not isinstance(token, str) or not token:
        raise ValueError("密码请求没有非空验证码 token。")
    if not exchanges or not fans:
        raise ValueError("缺少 COM exchange 或已登录 Fan 响应。")
    index, entry, fan = fans[-1]
    fan_email = fan.get("email")
    email_data = json.loads(email_posts[0][1]["request"].get("postData", {}).get("text", "{}"))
    if not isinstance(fan_email, str) or not fan_email or not contains_account(email_data, fan_email):
        raise ValueError("Fan 账号与本轮提交的账号不一致。")
    if expected_email is not None and fan_email.casefold() != expected_email.casefold():
        raise ValueError("Fan 账号与指定账号配置不一致。")
    actual_header = next((item["value"] for item in entry["request"].get("headers", [])
                          if item["name"].lower() == "cookie"), None)
    path = Path(cookie_file) if cookie_file else directory / "cookie-header.txt"
    exported_header = path.read_text(encoding="utf-8").rstrip("\r\n")
    if not actual_header or exported_header != actual_header:
        raise ValueError("Cookie 文件与实际成功 Fan 请求不完全一致。")
    return {"verifiedAtUTC": datetime.now(timezone.utc).isoformat(), "authenticated": True,
            "scope": "https://signup.ticketmaster.com/", "harEntries": len(entries),
            "harSHA256": har_hash,
            "emailPostCount": 1, "passwordPostCount": 1, "emailStatus": 200, "passwordStatus": 200,
            "emailHARIndex": email_posts[0][0], "passwordHARIndex": password_posts[0][0],
            "nonemptyRecaptchaToken": True, "exchangeHARIndices": exchanges, "fanHARIndex": index,
            "fanIsLoggedIn": True, "submittedAccountMatchesFan": True,
            "expectedAccountChecked": expected_email is not None,
            "entryRecordPresent": bool(fan.get("entryRecord")), "caRequestCount": ca_requests,
            "cookieFile": path.name, "cookieMatchesSuccessfulRequestExactly": True,
            "cookieBytes": path.stat().st_size}
