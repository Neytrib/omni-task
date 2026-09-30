"""One TLS URL policy shared by Redis clients and Celery, without exposing credentials."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def normalize_redis_url(value: str) -> str:
    """Celery requires an explicit certificate policy; redis-py uses lowercase values."""
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"redis", "rediss"}
            and parsed.hostname
            and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("REDIS_URL must be a valid Redis connection URL")
    if parsed.scheme != "rediss":
        return value

    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    for name, accepted in (
        ("ssl_cert_reqs", {"required", "CERT_REQUIRED"}),
        ("ssl_check_hostname", {"true", "True", "1", "yes"}),
    ):
        supplied = [item for key, item in parameters if key == name]
        if len(supplied) > 1 or (supplied and supplied[0] not in accepted):
            raise ValueError("Redis TLS requires certificate and hostname verification")
    parameters = [
        (key, item)
        for key, item in parameters
        if key not in {"ssl_cert_reqs", "ssl_check_hostname"}
    ]
    parameters.extend([("ssl_cert_reqs", "required"), ("ssl_check_hostname", "true")])
    return urlunsplit(parsed._replace(query=urlencode(parameters)))
