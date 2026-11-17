"""筛选实际出口地区及 Ticketmaster 活动页可用性；不读取或提交账号。"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from common import atomic_write
from proxy_pool import DEFAULT_PROXY_API, ProxyPool


DEFAULT_CAMPAIGN = "https://signup.ticketmaster.com/noahkahan"
GEO_URL = "https://www.cloudflare.com/cdn-cgi/trace"
FALLBACK_GEO_URL = "https://ipinfo.io/json"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:155.0) Gecko/20100101 Firefox/155.0"


def curl_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'


def check_cancelled(stop_event):
    if stop_event and stop_event.is_set():
        raise InterruptedError("代理检测已取消。")


def request(pool, node, url, stop_event=None):
    """A fresh, cookieless GET through the named SOCKS route. Never follow redirects."""
    with tempfile.TemporaryDirectory(prefix="tm-proxy-check-") as temporary:
        directory = Path(temporary)
        host = f"[{pool.host}]" if ":" in pool.host else pool.host
        options = {"proxy": f"socks5h://{host}:{pool.port}",
                   "proxy-user": node["username"] + ":" + pool.password,
                   "noproxy": "", "url": url, "proto": "=https",
                   "connect-timeout": 10, "max-time": 25,
                   "dump-header": directory / "headers", "output": directory / "body",
                   "write-out": "%{http_code} %{time_total}", "user-agent": USER_AGENT}
        config = "\n".join(f"{key} = {curl_quote(value)}" for key, value in options.items())
        config += '\nheader = "Accept-Language: zh-CN"\n'
        environment = {k: v for k, v in os.environ.items()
                       if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
                       and k not in ("TM_TEST_EMAIL", "TM_TEST_PASS")}
        check_cancelled(stop_event)
        with subprocess.Popen(["curl", "-q", "--silent", "--show-error", "--config", "-"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=environment) as process:
            deadline = time.monotonic() + 30
            pending_input = config
            try:
                while True:
                    check_cancelled(stop_event)
                    if time.monotonic() >= deadline:
                        process.kill()
                        process.communicate()
                        return {"status":0, "error":"timeout"}, ""
                    try:
                        output, _ = process.communicate(input=pending_input, timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        pending_input = None
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        fields = output.strip().split()
        result = {"status": int(fields[0]) if fields and fields[0].isdigit() else 0,
                  "elapsedSeconds": float(fields[1]) if len(fields) == 2 else None}
        if process.returncode:
            result["error"] = "timeout" if process.returncode == 28 else "transport_error"
            result["curlExitCode"] = process.returncode
        if (directory / "headers").exists():
            for line in (directory / "headers").read_text(errors="replace").splitlines():
                key, separator, value = line.partition(":")
                if separator and key.lower() == "location":
                    result["location"] = value.strip()
        body = (directory / "body").read_text(errors="replace") if (directory / "body").exists() else ""
        return result, body


def exit_location(pool, node, stop_event=None):
    attempts = []
    for url in (GEO_URL, FALLBACK_GEO_URL):
        check_cancelled(stop_event)
        response, body = request(pool, node, url, **({"stop_event":stop_event} if stop_event else {}))
        attempts.append({"source": url, **response})
        if response["status"] != 200 or response.get("error"):
            continue
        try:
            if url == GEO_URL:
                data = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)
                country, address = data.get("loc"), data.get("ip")
            else:
                data = json.loads(body)
                country, address = data.get("country"), data.get("ip")
            if not isinstance(country, str) or not re.fullmatch("[A-Z]{2}", country):
                continue
            ipaddress.ip_address(address)
        except (ValueError, TypeError, AttributeError):
            continue
        return {"country": country, "ip": address, "source": url, "attempts": attempts}
    return {"country": None, "attempts": attempts}


def inspect_node(pool, node, country, campaign, stop_event=None):
    check_cancelled(stop_event)
    options = {"stop_event":stop_event} if stop_event else {}
    result = {"node": {key: node[key] for key in ("id", "name", "protocol", "health")},
              "checkedAtUTC": datetime.now(timezone.utc).isoformat(), "accepted": False}
    result["exit"] = exit_location(pool, node, **options)
    actual_country = result["exit"]["country"]
    if not actual_country or (country is not None and actual_country != country):
        result["reason"] = "country_mismatch" if result["exit"]["country"] else "country_unverified"
        return result
    response, body = request(pool, node, campaign, **options)
    # HTTP 200 alone may also be an error page. Require recognizable campaign HTML.
    response["hasSignIn"] = "Sign In" in body
    response["hasNextApp"] = "self.__next_f.push" in body
    response["hasServerRedirect"] = "NEXT_REDIRECT" in body
    result["campaign"] = response
    if (response["status"] != 200 or response.get("error") or response.get("location")
            or response["hasServerRedirect"] or not response["hasSignIn"] or not response["hasNextApp"]):
        result["reason"] = "campaign_unavailable"
        return result
    # A second connection catches balancers that switch between countries.
    result["exitAfterCampaign"] = exit_location(pool, node, **options)
    if result["exitAfterCampaign"]["country"] != actual_country:
        result["reason"] = "country_changed_or_unverified"
        return result
    result.update(accepted=True, reason="passed")
    return result


def filtered_pool_from_api(nodes_url, campaign, stop_event=None, on_progress=None):
    """Load every region with a valid recorded exit IP; do not probe the network."""
    check_cancelled(stop_event)
    pool = ProxyPool.from_api(nodes_url)
    check_cancelled(stop_event)
    total = len(pool.nodes)
    accepted = []
    for node in pool.nodes:
        check_cancelled(stop_event)
        location = node.get("reported_exit", {})
        if location.get("country") and location.get("ip"):
            accepted.append({**node, "screening":{"country":location["country"], "exitIP":location["ip"],
                "checkedAtUTC":location.get("checkedAtUTC"), "campaignURL":campaign, "method":"api_json"}})
    if on_progress:
        on_progress({"total":total, "checked":total, "accepted":len(accepted)})
    if not accepted:
        raise ValueError("接口暂无有效的 IP 检测结果，请先在代理管理端更新 IP 检测后再加载。")
    pool.nodes = accepted
    return pool


def fresh_pool(nodes_url, campaign, stop_event=None, on_progress=None, workers=8):
    """Verify reachable COM campaign exits across all regions."""
    check_cancelled(stop_event)
    if not shutil.which("curl"):
        raise ValueError("检测代理需要 curl，请先安装 curl。")
    pool = ProxyPool.from_api(nodes_url)
    total, checked, accepted = len(pool.nodes), 0, []
    def progress():
        if on_progress:
            on_progress({"total":total, "checked":checked, "accepted":len(accepted)})
    progress()
    with ThreadPoolExecutor(max_workers=min(workers, total), thread_name_prefix="proxy-check") as executor:
        futures = {executor.submit(inspect_node, pool, node, None, campaign, stop_event=stop_event):node for node in pool.nodes}
        try:
            for future in as_completed(futures):
                check_cancelled(stop_event)
                node = futures[future]
                result = future.result()
                checked += 1
                if result["accepted"]:
                    accepted.append({**node, "screening":{"country":result["exitAfterCampaign"].get("country"), "exitIP":result["exitAfterCampaign"].get("ip"),
                                     "checkedAtUTC":result["checkedAtUTC"], "campaignURL":campaign}})
                progress()
        finally:
            for future in futures:
                future.cancel()
    check_cancelled(stop_event)
    if not accepted:
        raise ValueError("本次检测没有可用的代理出口，请更新代理后重新刷新。")
    pool.nodes = accepted
    return pool


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-api", default=DEFAULT_PROXY_API)
    parser.add_argument("--country", help="可选的实际出口国家代码；默认不限地区")
    parser.add_argument("--url", default=DEFAULT_CAMPAIGN)
    parser.add_argument("--workers", type=int, default=4, help="并行检测数，1–8，默认 4")
    parser.add_argument("--out", type=Path, required=True, help="独立代理检测报告；网页会自动检测，无需读取此文件")
    args = parser.parse_args(argv)
    if (args.country is not None and not re.fullmatch("[A-Z]{2}", args.country)) or not 1 <= args.workers <= 8:
        raise ValueError("国家代码必须是两个大写字母，workers 必须为 1–8。")
    from urllib.parse import urlsplit
    target = urlsplit(args.url)
    if (target.scheme != "https" or target.netloc != "signup.ticketmaster.com"
            or target.query or target.fragment or target.path in ("", "/")):
        raise ValueError("--url 必须是官方 signup.ticketmaster.com 活动地址。")
    if not shutil.which("curl"):
        raise ValueError("检测代理需要 curl，请先安装 curl。")
    pool = ProxyPool.from_api(args.proxy_api)
    report = {"version": 1, "nodesAPI": args.proxy_api, "campaignURL": args.url,
              "country": args.country, "checkedAtUTC": datetime.now(timezone.utc).isoformat(),
              "complete": False, "accountSubmissions": 0, "nodeIds": [], "results": []}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(args.out, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(inspect_node, pool, node, args.country, args.url) for node in pool.nodes]
        for future in as_completed(futures):
            result = future.result()
            report["results"].append(result)
            if result["accepted"]:
                report["nodeIds"].append(result["node"]["id"])
            atomic_write(args.out, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({"checked": len(report["results"]), "total": len(pool.nodes),
                              "name": result["node"]["name"], "country": result["exit"]["country"],
                              "accepted": result["accepted"], "reason": result["reason"]}, ensure_ascii=False), flush=True)
    report["complete"] = True
    report["finishedAtUTC"] = datetime.now(timezone.utc).isoformat()
    atomic_write(args.out, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"total": len(pool.nodes), "accepted": len(report["nodeIds"]),
                      "report": str(args.out.resolve())}, ensure_ascii=False))
    return 0 if report["nodeIds"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        import sys
        print(str(error) if isinstance(error, ValueError) else "代理检测失败，请检查网络和输出路径。", file=sys.stderr)
        raise SystemExit(1)
