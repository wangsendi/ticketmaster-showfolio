"""Extract the account's actual selections from observed, verified responses."""
import base64
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from datetime import datetime

from verify import audit, load_har, response_json


def credentials_rejected(result):
    return bool(result.get("credentialsRejected") or any(
        item.get("path") == "/json/sign-in" and re.search(
            r"\bcredentials?\s+mismatch\b|\binvalid\s+credentials\b|\bincorrect\s+(?:email\s+(?:or|and)\s+)?password\b|账号或密码错误|邮箱或密码错误|密码不正确",
            str((item.get("businessError") or {}).get("message", "")), re.I)
        for item in result.get("responses", [])))


ACCOUNT_ISSUES = {
    "account-not-found": "账号不存在",
    "password-reset-required": "需要重置密码",
    "password-submit-disabled": "密码仅7位，登录按钮不可用，请核对密码",
    "password-review-required": "密码仅7位，请核对密码",
    "password-format-invalid": "密码格式不符合网站要求，请核对密码",
}


def account_issue(result):
    code = result.get("accountIssue")
    if code in ACCOUNT_ISSUES:
        return code, ACCOUNT_ISSUES[code]
    errors = [result.get("emailBusinessError") or {}] + [
        item.get("businessError") or {} for item in result.get("responses", [])
        if item.get("path") == "/json/account-status-validation"]
    for error in errors:
        message = str(error.get("message", ""))
        if re.search(r"\b(?:the\s+)?user\s+is\s+not\s+found\b|\baccount\s+(?:does\s+not\s+exist|not\s+found)\b", message, re.I):
            return "account-not-found", ACCOUNT_ISSUES["account-not-found"]
        if re.search(r"password\s+reset\s+(?:is\s+)?required|reset\s+your\s+password", message, re.I):
            return "password-reset-required", ACCOUNT_ISSUES["password-reset-required"]
    if result.get("passwordResetRequired") or any(item.get("passwordResetVisible") for item in result.get("uiObservations", [])):
        return "password-reset-required", ACCOUNT_ISSUES["password-reset-required"]
    form = result.get("passwordFormBeforeSubmit") or {}
    if form.get("submitEnabled") is False:
        if form.get("tooShort") or form.get("patternMismatch"):
            return "password-format-invalid", ACCOUNT_ISSUES["password-format-invalid"]
        if form.get("valueLength") == 7:
            return "password-submit-disabled", ACCOUNT_ISSUES["password-submit-disabled"]
    return None


def has_persona_evidence(result):
    observations = [result.get("humanVerificationObservation") or {}, *(result.get("uiObservations") or [])]
    return bool(result.get("personaVerificationRequired") or any(
        item.get("personaPageVisible") or item.get("personaPromptVisible") for item in observations))


def verification_requirement(result):
    if not (result.get("requiresHumanInteraction") or has_persona_evidence(result)):
        return None
    observation = result.get("humanVerificationObservation") or {}
    if result.get("browsingActivityPaused") or observation.get("browsingActivityPaused"):
        return "访问已暂停（网站风控拦截）"
    if has_persona_evidence(result):
        return "未进行身份验证"
    requirements = []
    if observation.get("otpInputVisible"):
        requirements.append("需要邮箱／短信验证码")
    if observation.get("imageChallengeVisible"):
        requirements.append("需要图片验证码")
    if observation.get("checkboxChallengeVisible"):
        requirements.append("需要勾选人机验证")
    return "；".join(requirements) or "需要人工验证（网站未明确认证类型）"


def verification_status(result):
    observation = result.get("humanVerificationObservation") or {}
    if result.get("browsingActivityPaused") or observation.get("browsingActivityPaused"):
        return "failed"
    if has_persona_evidence(result):
        return "success"
    return "needs-human"


def selections_from_har(directory, email, *, entries=None):
    if entries is None:
        entries, _ = load_har(directory)
    markets, records = {}, []

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("id"), str) and isinstance(value.get("event"), dict) and "name" in value:
                markets[value["id"]] = value
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for entry in entries:
        url = urlsplit(entry["request"]["url"])
        if url.hostname != "signup.ticketmaster.com" or entry["response"]["status"] != 200:
            continue
        content = entry["response"].get("content", {})
        body = content.get("text", "")
        if content.get("encoding") == "base64":
            body = base64.b64decode(body).decode("utf-8", errors="replace")
        if url.path == "/graphql":
            try:
                payload = response_json(entry)
                fan = (payload.get("data") or {}).get("fan")
                if (not payload.get("errors") and isinstance(fan, dict) and fan.get("isLoggedIn") is True
                        and str(fan.get("email", "")).casefold() == email.casefold() and "entryRecord" in fan):
                    records.append(fan["entryRecord"])
                walk(payload)
            except (ValueError, AttributeError):
                pass
        # Next.js serializes campaign data into Flight records in the HTML.
        chunks = []
        for raw in re.findall(r'self\.__next_f\.push\((.*?)\)</script>', body, re.S):
            try:
                value = json.loads(raw)
                if len(value) > 1 and isinstance(value[1], str):
                    chunks.append(value[1])
            except (ValueError, TypeError):
                continue
        flight = "".join(chunks) if chunks else body
        for line in flight.splitlines():
            try:
                walk(json.loads(line.split(":", 1)[1]))
            except (ValueError, IndexError, RecursionError):
                continue
    if not records:
        raise ValueError("已登录，但响应中没有可核对的报名记录字段。")
    record = records[-1]
    if record is None:
        return [], None
    fields = record.get("fields")
    if isinstance(fields, str):
        fields = json.loads(fields)
    ids = fields.get("markets") if isinstance(fields, dict) else None
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise ValueError("报名记录的场次字段格式发生变化，不能判断是否已选场次。")
    selected = []
    for market_id in dict.fromkeys(ids):
        market = markets.get(market_id)
        if market is None:
            selected.append({"id": market_id, "name": "场次信息未解析", "resolved": False})
            continue
        event = market["event"]
        date = event.get("date", "")
        try:
            date = datetime.fromisoformat(date.replace("Z", "+00:00")).astimezone(ZoneInfo(market["timezone"])).date().isoformat()
        except (ValueError, KeyError, TypeError):
            date = date[:10]
        selected.append({"id": market_id, "name": market["name"], "city": market.get("city", ""),
                         "state": market.get("state", ""), "date": date,
                         "venue": event.get("venue", {}).get("name", ""), "resolved": True})
    return selected, record


def collect(directory, email, summary):
    directory = Path(directory)
    result = dict(summary)
    try:
        result = {**json.loads((directory / "result.json").read_text()), **summary}
    except (OSError, ValueError):
        pass
    for key in ("outputDirectory", "cookieFile"):
        result.pop(key, None)
    data = {"result": result, "selections": None, "cookie_header": None, "error": None,
            "status": "cancelled" if summary.get("stopped") else "failed"}
    requirement = verification_requirement(result)
    if requirement and not summary.get("stopped"):
        data["status"] = "needs-human"
        result["verificationRequirement"] = requirement
    if summary.get("authenticated") and not requirement:
        try:
            har = load_har(directory)
            result["verification"] = audit(directory, expected_email=email, har=har)
            data["cookie_header"] = (directory / "cookie-header.txt").read_text().rstrip("\r\n")
        except (OSError, ValueError, KeyError, TypeError):
            data["error"] = "登录结果核对未通过。"
        else:
            # The verified account screenshot is the deliverable; selections are optional.
            if (directory / "com-result.png").is_file():
                data["status"] = "success"
            else:
                data["status"] = "partial"
                data["error"] = "已登录，但整页截图未生成。"
            try:
                data["selections"], result["entryRecord"] = selections_from_har(directory, email, entries=har[0])
                if any(not item["resolved"] for item in data["selections"]):
                    result["selectionWarning"] = "已读取场次 ID，部分日期和场馆未解析。"
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                result["selectionWarning"] = "已登录但场次解析失败。"
    if requirement and not summary.get("stopped"):
        data["status"] = verification_status(result)
        data["error"] = None
    if not summary.get("stopped") and not summary.get("authenticated") and credentials_rejected(result):
        data["status"] = "success"
        result["credentialsRejected"] = True
        result["verificationRequirement"] = "账号密码错误"
        data["error"] = None
    issue = account_issue(result)
    if issue and not summary.get("stopped") and not summary.get("authenticated"):
        result["accountIssue"], result["verificationRequirement"] = issue
        data["status"] = "success"
        data["error"] = None
    if data["status"] in ("failed", "needs-human", "cancelled"):
        if requirement and verification_status(result) == "failed":
            message = "网站已暂停访问（Browsing Activity Paused），可更换代理重试。"
        elif summary.get("errorType") == "PlaywrightWebSocketAssertion":
            message = "Firefox 的 Playwright 驱动崩溃（WebSocket 事件断言），不是等待时间不足。"
        elif result.get("passwordStatus") == 403:
            errors = [item.get("businessError", {}) for item in result.get("responses", []) if item.get("path") == "/json/sign-in" and item.get("status") == 403]
            detail = errors[-1].get("errorType") or errors[-1].get("message") if errors else None
            message = "网站拒绝密码步骤（HTTP 403）" + (f"：{detail}" if detail else "；返回未说明具体原因。")
        elif result.get("emailStatus") and result["emailStatus"] != 200 and data["status"] == "failed":
            errors = [item.get("businessError") or {} for item in result.get("responses", [])
                      if item.get("path") == "/json/account-status-validation" and item.get("status") == result["emailStatus"]]
            detail = (errors[-1].get("message") or errors[-1].get("errorType")) if errors else None
            message = f"网站拒绝邮箱步骤（HTTP {result['emailStatus']}）" + (f"：{detail}" if detail else "；未记录具体业务原因。")
        elif data["status"] == "needs-human":
            message = ("网站已暂停此网络或浏览器的访问（Browsing Activity Paused），需人工处理，已停止自动重试。"
                       if result.get("browsingActivityPaused") else "网站要求人工验证，本次查询已停止。")
        elif data["status"] == "cancelled":
            message = "查询已取消。"
        else:
            message = result.get("errorMessage") or f"查询未完成，阶段：{result.get('failureStage', 'browser')}。"
        data["error"] = message
    for key, filename in (("cookies", "cookies.json"), ("storage_state", "storage-state.json")):
        try:
            data[key] = json.loads((directory / filename).read_text())
        except (OSError, ValueError):
            data[key] = None
    candidates = [directory / name for name in ("com-result.png", "failure.png", "observed-verification.png", "final-page.png")]
    shot = next((p for p in candidates if p.is_file()), None)
    data["screenshot"] = shot.read_bytes() if shot else None
    data["screenshot_kind"] = ("full-page" if result.get("screenshotFullPage") else "legacy-viewport") if shot and shot.name == "com-result.png" else "failure"
    log = directory / "browser-private.log"
    data["diagnostic_log"] = log.read_text(errors="replace")[-80000:] if log.is_file() else None
    return data
