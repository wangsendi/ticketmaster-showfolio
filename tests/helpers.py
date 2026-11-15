import base64
import json
from pathlib import Path

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=")


def successful_run(directory, email, markets=None, record="selected"):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cookie = "SID=" + email.split("@")[0]
    if markets is None:
        markets = [{"id": "m1", "name": "5/19 - Santa Clara, CA - Stadium", "city": "Santa Clara", "state": "CA",
                    "event": {"date": "2027-05-19T07:00:00.000Z", "venue": {"name": "Stadium"}}, "timezone": "America/Los_Angeles"}]

    def entry(host, path, status, method="GET", request_body=None, response_body=None, headers=None):
        request = {"url": "https://" + host + path, "method": method, "headers": headers or []}
        if request_body is not None:
            request["postData"] = {"text": json.dumps(request_body)}
        return {"request": request, "response": {"status": status, "content": {"text": json.dumps(response_body)}}}

    fan = {"isLoggedIn": True, "email": email}
    if record != "missing":
        fan["entryRecord"] = None if record is None else {"fields": json.dumps({"markets": ["m1"]}), "attributes": "{}"}
    entries = [
        entry("auth.ticketmaster.com", "/json/account-status-validation", 200, "POST", {"emailAddress": email}),
        entry("auth.ticketmaster.com", "/json/sign-in", 200, "POST", {"recaptchaToken": "fixture-token"}),
        entry("identity.ticketmaster.com", "/exchange", 302),
        entry("signup.ticketmaster.com", "/graphql", 200, "POST", response_body={"data": {"fan": fan}}, headers=[{"name": "Cookie", "value": cookie}]),
    ]
    flight = "4:" + json.dumps({"markets": markets}) + "\n"
    page = entry("signup.ticketmaster.com", "/noahkahan", 200)
    page["response"]["content"]["text"] = "<script>self.__next_f.push(" + json.dumps([1, flight]) + ")</script>"
    entries.insert(0, page)
    (directory / "com-login.har").write_text(json.dumps({"log": {"entries": entries}}))
    (directory / "cookie-header.txt").write_text(cookie + "\n")
    (directory / "com-result.png").write_bytes(PNG)
    return {"authenticated": True, "emailStatus": 200, "passwordStatus": 200, "screenshotFullPage": True}
