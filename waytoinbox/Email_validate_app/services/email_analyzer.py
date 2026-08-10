import re
from email import message_from_string
from urllib.parse import urlparse


SUSPICIOUS_TLDS = [
    ".xyz", ".top", ".click", ".ru", ".tk", ".gq", ".ml", ".cf"
]


def extract_email_domain(email_value):
    if not email_value:
        return ""
    if "@" in email_value:
        return email_value.split("@")[-1].lower().strip(" >")
    return ""


def clean_domain(domain):
    return domain.lower().replace("www.", "").strip()


def is_private_ip(ip):
    if ip.startswith("10.") or ip.startswith("127.") or ip.startswith("192.168."):
        return True
    if ip.startswith("172."):
        try:
            second = int(ip.split(".")[1])
            return 16 <= second <= 31
        except Exception:
            pass
    return False


class ProfessionalEmailAnalyzer:

    def __init__(self, raw_email, ip_blacklist_checker=None, domain_blacklist_checker=None):
        self.raw_email = raw_email
        self.msg = message_from_string(raw_email)
        self.ip_blacklist_checker = ip_blacklist_checker
        self.domain_blacklist_checker = domain_blacklist_checker

    def basic_fields(self):
        return {
            "from": self.msg.get("From", "Not Found"),
            "to": self.msg.get("To", "Not Found"),
            "subject": self.msg.get("Subject", "Not Found"),
            "date": self.msg.get("Date", "Not Found"),
        }

    def get_body(self):
        text = ""
        html = ""
        if self.msg.is_multipart():
            for part in self.msg.walk():
                content_type = part.get_content_type()
                try:
                    payload = part.get_payload(decode=True)
                    payload = payload.decode(errors="ignore") if payload else ""
                except Exception:
                    payload = ""
                if content_type == "text/plain":
                    text += payload
                elif content_type == "text/html":
                    html += payload
        else:
            try:
                text = self.msg.get_payload()
            except Exception:
                text = ""
        return text, html

    def extract_links(self, content):
        return re.findall(r'https?://[^\s"\'<>]+', content)

    def extract_link_domains(self, links):
        domains = []
        for link in links:
            try:
                d = urlparse(link).netloc.lower()
                d = clean_domain(d)
                if d:
                    domains.append(d)
            except Exception:
                pass
        return list(dict.fromkeys(domains))

    def image_count(self, html):
        return len(re.findall(r"<img[^>]+src=", html, re.I))

    def email_size(self):
        size_bytes = len(self.raw_email.encode("utf-8"))
        size_kb = round(size_bytes / 1024, 2)
        return f"{size_bytes} bytes ({size_kb} KB)"

    def text_image_ratio(self, text, html, images):
        text_len = len(text) + len(html)
        image_weight = images * 5000
        total = text_len + image_weight
        if total == 0:
            return 0, 0
        text_percent = round((text_len / total) * 100, 2)
        image_percent = round((image_weight / total) * 100, 2)
        return text_percent, image_percent

    def auth_results(self):
        header = self.raw_email.lower()
        result = {"spf": "unknown", "dkim": "unknown", "dmarc": "unknown"}
        if "spf=pass" in header or "received-spf: pass" in header:
            result["spf"] = "pass"
        elif "spf=fail" in header:
            result["spf"] = "fail"
        if "dkim=pass" in header:
            result["dkim"] = "pass"
        elif "dkim=fail" in header:
            result["dkim"] = "fail"
        if "dmarc=pass" in header:
            result["dmarc"] = "pass"
        elif "dmarc=fail" in header:
            result["dmarc"] = "fail"
        return result

    def classify_ips(self):
        unfolded = re.sub(r'\r?\n[ \t]+', ' ', self.raw_email)
        headers = re.findall(r"Received:.*", unfolded)
        found = []
        for h in headers:
            ips = re.findall(r'\[(\d{1,3}(?:\.\d{1,3}){3})\]', h)
            if not ips:
                ips = re.findall(r'\((\d{1,3}(?:\.\d{1,3}){3})\)', h)
            if not ips:
                raw = re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', h)
                ips = [ip for ip in raw if all(0 <= int(o) <= 255 for o in ip.split('.'))]
            for ip in ips:
                if ip not in found:
                    found.append(ip)
        final = []
        for i, ip in enumerate(found):
            if is_private_ip(ip):
                label = "Private/Internal"
            elif i == len(found) - 1:
                label = "Sender IP"
            elif i == 0:
                label = "Receiver Server"
            else:
                label = "Relay Server"
            final.append({"ip": ip, "type": label})
        return final

    def origin_ip(self, ip_list):
        for row in reversed(ip_list):
            if row["type"] == "Sender IP":
                return row["ip"]
        for row in reversed(ip_list):
            if row["type"] != "Private/Internal":
                return row["ip"]
        return "Not Found"

    def domain_analysis(self):
        from_header = self.msg.get("From", "")
        return_path = self.msg.get("Return-Path", "")
        dkim_header = self.msg.get("DKIM-Signature", "")
        from_email_match = re.search(r'[\w\.-]+@[\w\.-]+', from_header)
        return_match = re.search(r'[\w\.-]+@[\w\.-]+', return_path)
        from_email = from_email_match.group(0) if from_email_match else ""
        return_email = return_match.group(0) if return_match else ""
        from_domain = extract_email_domain(from_email)
        return_domain = extract_email_domain(return_email)
        dkim_match = re.search(r'd=([\w\.-]+)', dkim_header)
        dkim_domain = dkim_match.group(1).lower() if dkim_match else ""
        mismatch = bool(from_domain and return_domain and from_domain != return_domain)
        return {
            "from_email": from_email,
            "from_domain": from_domain,
            "return_path_domain": return_domain,
            "dkim_domain": dkim_domain,
            "mismatch": mismatch,
        }

    def blacklist_result(self, ip, domain):
        ip_result = {}
        domain_result = "N/A"
        try:
            if self.ip_blacklist_checker and ip != "Not Found":
                ip_result = self.ip_blacklist_checker(ip)
        except Exception:
            ip_result = {"error": "ERROR"}
        try:
            if self.domain_blacklist_checker and domain:
                domain_result = self.domain_blacklist_checker(domain)
        except Exception:
            domain_result = "ERROR"
        return ip_result, domain_result

    def spam_score(self, links, images, auth, domain_info,
                   ip_blacklist, domain_blacklist, text_percent):
        score = 0
        reasons = []
        if len(links) > 5:
            score += 2
            reasons.append("Too many links")
        for link in links:
            for tld in SUSPICIOUS_TLDS:
                if tld in link.lower():
                    score += 2
                    reasons.append("Suspicious TLD")
                    break
        if auth["spf"] != "pass":
            score += 2
            reasons.append("SPF Fail")
        if auth["dkim"] != "pass":
            score += 2
            reasons.append("DKIM Fail")
        if auth["dmarc"] != "pass":
            score += 2
            reasons.append("DMARC Fail")
        if images > 3:
            score += 1
            reasons.append("Too many images")
        if text_percent < 20:
            score += 1
            reasons.append("Low text content")
        if domain_info["mismatch"]:
            score += 2
            reasons.append("Domain mismatch")
        if isinstance(ip_blacklist, dict) and any(v == "Listed" for v in ip_blacklist.values()):
            score += 3
            reasons.append("IP Blacklisted")
        if domain_blacklist == "LISTED":
            score += 3
            reasons.append("Domain Blacklisted")
        if score <= 3:
            risk = "SAFE"
        elif score <= 7:
            risk = "RISKY"
        else:
            risk = "DANGEROUS"
        return score, risk, reasons

    def analyze(self):
        result = {}
        result.update(self.basic_fields())
        text, html = self.get_body()
        content = text + html
        links = self.extract_links(content)
        domains = self.extract_link_domains(links)
        images = self.image_count(html)
        result["size"] = self.email_size()
        text_percent, image_percent = self.text_image_ratio(text, html, images)
        result["text_percent"] = text_percent
        result["image_percent"] = image_percent
        auth = self.auth_results()
        result.update(auth)
        ip_list = self.classify_ips()
        result["ips"] = ip_list
        origin_ip = self.origin_ip(ip_list)
        result["origin_ip"] = origin_ip
        domain_info = self.domain_analysis()
        result["domain_info"] = domain_info
        ip_bl, domain_bl = self.blacklist_result(origin_ip, domain_info["from_domain"])
        result["origin_ip_blacklist"] = ip_bl
        result["domain_blacklist"] = domain_bl
        result["links"] = links
        result["domains"] = domains
        result["images"] = images
        score, risk, reasons = self.spam_score(
            links, images, auth, domain_info, ip_bl, domain_bl, text_percent
        )
        result["score"] = score
        result["risk"] = risk
        result["reasons"] = reasons
        return result
