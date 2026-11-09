import re

# local part ≤ 64 (RFC 5321 §4.5.3.1.1) + "@" + domain ≤ 48 + "." + TLD ≤ 14 = 128
EMAIL_MAX = 128
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,48}\.[A-Za-z]{2,14}")

SSN_MAX = 11
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
