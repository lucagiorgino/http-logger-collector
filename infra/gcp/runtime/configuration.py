from urllib.parse import parse_qs, urlsplit

from pymongo.uri_parser import parse_uri

from app.core.config import settings


def validate_cloud_connections() -> None:
    redis = urlsplit(settings.redis_url)
    options = parse_qs(redis.query)
    if (
        redis.scheme != "rediss"
        or not redis.hostname
        or options.get("ssl_cert_reqs", ["required"]) != ["required"]
        or options.get("ssl_check_hostname") != ["true"]
    ):
        raise ValueError("Cloud Redis requires TLS, certificate and hostname verification")
    mongo = parse_uri(settings.mongo_uri)["options"]
    if (
        not mongo.get("tls")
        or mongo.get("tlsAllowInvalidCertificates")
        or mongo.get("tlsAllowInvalidHostnames")
    ):
        raise ValueError("Cloud MongoDB requires verified TLS")
