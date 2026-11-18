"""隔离的浏览器子进程；只向调用者提供临时查询结果。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from proxy_pool import NETWORK_MODE

ROOT = Path(__file__).resolve().parent
DEFAULT_URL = "https://signup.ticketmaster.com/noahkahan"
BROWSER_CONFIG = ROOT / "data/browser.json"


def selected_browser():
    name = os.environ.get("TM_BROWSER_OVERRIDE")
    if name is None:
        config_path = Path(os.environ.get("TM_BROWSER_CONFIG", BROWSER_CONFIG))
        try:
            config = json.loads(config_path.read_text())
            name = config["browser"]
            if not isinstance(name, str):
                raise ValueError()
        except FileNotFoundError:
            name = os.environ.get("TM_BROWSER", "firefox")
        except (ValueError, KeyError, TypeError):
            raise ValueError("浏览器配置文件无效，请检查 data/browser.json。") from None
    name = name.strip().lower()
    if name not in ("cloakbrowser", "firefox", "chromium", "chrome"):
        raise ValueError("TM_BROWSER / 浏览器配置必须是 cloakbrowser、firefox、chromium 或 chrome。")
    return name


def browser_command(script, display):
    browser = selected_browser()
    node = shutil.which("node")
    if not node:
        raise ValueError("未找到 Node.js；需要 Node.js 20 或更高版本。")
    package = subprocess.run([node, "-e", "require.resolve('playwright')"], cwd=ROOT,
                             capture_output=True, text=True, timeout=15)
    if package.returncode:
        raise ValueError("未安装 Playwright 依赖，请先在本目录运行 npm ci。")
    installed = subprocess.run([node, "-e",
        "try { require('./src/browser.cjs').assertBrowserInstalled(process.argv[1]); } catch (e) { process.stderr.write(e.message); process.exitCode=1; }", browser],
        cwd=ROOT, capture_output=True, text=True, timeout=15)
    if installed.returncode:
        raise ValueError(installed.stderr.strip() or f"浏览器 {browser} 不可用。")
    command = [node, str(ROOT / "src" / script)]
    use_xvfb = display == "xvfb" or (
        display == "auto" and sys.platform.startswith("linux")
        and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    )
    if use_xvfb:
        xvfb = shutil.which("xvfb-run")
        if not xvfb:
            raise ValueError("无显示服务；请安装 Xvfb，或在桌面运行并使用 --display desktop。")
        command = [xvfb, "-a", "-s", "-screen 0 1440x1000x24", *command]
    return command, use_xvfb


def child_environment(use_xvfb, debug=False):
    environment = os.environ.copy()
    # Browser networking is selected explicitly through stdin, not shell proxies.
    for key in list(environment):
        if key.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            environment.pop(key)
    environment.pop("MOZ_HEADLESS", None)
    if use_xvfb:
        environment.pop("DISPLAY", None)
        environment.pop("WAYLAND_DISPLAY", None)
    # Keep credentials on stdin only, including when the parent used env vars.
    environment.pop("TM_TEST_EMAIL", None)
    environment.pop("TM_TEST_PASS", None)
    environment.pop("DEBUG", None)
    if debug:
        environment["DEBUG"] = "pw:browser"
    return environment


def stop_process_group(process):
    if process.poll() is not None:
        return
    if os.name == "posix":
        os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()


def browser_failure(out, stderr, exit_code, proxy=None):
    """Preserve observed progress after a child crash without claiming authentication."""
    websocket_assertion = ("Error: Assertion error" in stderr and "FFPage._onWebSocketOpened" in stderr)
    failure = {"authenticated": False, "outputDirectory": str(out),
               "failureStage": "browser_driver" if websocket_assertion else "browser_process",
               "errorType": "PlaywrightWebSocketAssertion" if websocket_assertion else "BrowserResultInvalid",
               "childExitCode": exit_code}
    try:
        progress = json.loads((out / "progress.json").read_text(encoding="utf-8"))
        if isinstance(progress, dict):
            if isinstance(progress.get("stage"), str):
                failure["lastStage"] = progress["stage"]
            for key in ("emailStatus", "passwordStatus"):
                value = progress.get(key)
                if isinstance(value, int) and 100 <= value <= 599:
                    failure[key] = value
            if isinstance(progress.get("exchangeCOMObserved"), bool):
                failure["exchangeCOMObserved"] = progress["exchangeCOMObserved"]
    except (ValueError, OSError):
        pass
    if proxy:
        failure.update(proxyNode=proxy.node, networkMode=NETWORK_MODE)
    (out / "runner-error.json").write_text(json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return failure


def run_account(email, password, out, command, use_xvfb, url, metadata=None, stop_file=None, proxy=None, on_progress=None, timeout=900, stop_event=None):
    browser = selected_browser()
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("临时浏览器目录非空，拒绝混用查询文件。")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    out.chmod(0o700)
    with (out / "login.reserved").open("x", encoding="utf-8") as handle:
        handle.write("one browser account attempt\n")
    source_hashes = {}
    for name in ("runner.py", "jobs.py", "results.py", "proxy_pool.py", "src/login.cjs", "src/har.cjs", "src/capture.cjs", "src/session-export.cjs", "src/login-request.cjs", "src/passkey.cjs", "src/browser.cjs", "src/login-navigation.cjs", "src/proxy-bridge.cjs", "src/persona.cjs", "verify.py"):
        source = ROOT / name
        source_hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    context = {"browser": browser, "headless": False, "usesXvfb": use_xvfb,
               "networkMode": "existing_system_network", "networkConfigurationChanged": False,
               "maxEmailPosts": 1, "maxPasswordPosts": 1, "maxNormalLoginReloads": 1,
               "sourceSHA256": source_hashes}
    if metadata:
        context.update(metadata)
    if proxy:
        context.update(networkMode=NETWORK_MODE, proxyNode=proxy.node)
    (out / "run-context.json").write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")
    data = {"email": email, "password": password, "outputDirectory": str(out),
            "startURL": url, "usesXvfb": use_xvfb, "browser": browser,
            "locale": "zh-CN", "timezoneId": "Europe/London"}
    if stop_file:
        data["stopFile"] = str(stop_file)
    if proxy:
        data.update(proxy=proxy.upstream, proxyNode=proxy.node, networkMode=NETWORK_MODE)
    stopped_result = {"authenticated": False, "stopped": True, "outputDirectory": str(out)}
    if proxy:
        stopped_result.update(proxyNode=proxy.node, networkMode=NETWORK_MODE)
    if (stop_event and stop_event.is_set()) or (stop_file and Path(stop_file).exists()):
        return stopped_result
    process = subprocess.Popen(command, cwd=ROOT, env=child_environment(use_xvfb, debug=True),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=os.name == "posix")
    deadline = time.monotonic() + timeout
    payload = json.dumps(data)
    interrupted = False
    last_progress = None
    next_progress_check = 0
    try:
        while True:
            if on_progress and time.monotonic() >= next_progress_check:
                next_progress_check = time.monotonic() + 1
                try:
                    progress = json.loads((out / "progress.json").read_text())
                    current_progress = {key: progress.get(key) for key in (
                        "stage", "emailStatus", "passwordStatus", "exchangeCOMObserved", "requiresHumanInteraction")}
                    if current_progress != last_progress:
                        on_progress(progress)
                        last_progress = current_progress
                except (OSError, ValueError):
                    pass
            if ((stop_event and stop_event.is_set()) or (out / "STOP").exists() or (stop_file and Path(stop_file).exists())
                    or time.monotonic() >= deadline):
                interrupted = True
                break
            try:
                stdout, stderr = process.communicate(payload, timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                # communicate() buffers partial output and resumes with no new stdin.
                payload = None
    except KeyboardInterrupt:
        interrupted = True
    except BaseException:
        stop_process_group(process)
        raise
    if interrupted:
        (out / "STOP").touch(mode=0o600)
        stop_process_group(process)
        stdout, stderr = process.communicate()
        (out / "browser-private.log").write_text(stderr, encoding="utf-8")
        if time.monotonic() >= deadline:
            return {"authenticated": False, "failureStage": "timeout", "errorType": "AccountTimeout"}
        return stopped_result
    (out / "browser-private.log").write_text(stderr, encoding="utf-8")
    if process.returncode:
        return browser_failure(out, stderr, process.returncode, proxy)
    try:
        result = json.loads(stdout)
    except (ValueError, TypeError):
        return browser_failure(out, stderr, process.returncode, proxy)
    if not isinstance(result, dict):
        return browser_failure(out, stderr, process.returncode, proxy)
    keys = ("authenticated", "browser", "browserVersion", "emailStatus", "passwordStatus",
            "exchangeCOMObserved", "fanIsLoggedIn", "fanAccountMatches", "fanEntryRecordPresent",
            "requiresHumanInteraction", "verificationRequired", "personaVerificationRequired", "inquiryDataObserved", "failureStage", "errorType", "loginReloads",
            "proxyNode", "networkMode", "errorMessage", "loginCompleted", "screenshotFullPage", "screenshotHeight", "screenshotWidth")
    summary = {key: result[key] for key in keys if key in result}
    summary["outputDirectory"] = str(out)
    if result.get("authenticated"):
        summary["cookieFile"] = str(out / "cookie-header.txt")
    return summary
