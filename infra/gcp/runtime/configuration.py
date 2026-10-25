from pymongo.uri_parser import parse_uri

from app.core.config import settings


def validate_cloud_connections() -> None:
    mongo = parse_uri(settings.mongo_uri)["options"]
    if (
        not mongo.get("tls")
        or mongo.get("tlsAllowInvalidCertificates")
        or mongo.get("tlsAllowInvalidHostnames")
    ):
        raise ValueError("Cloud MongoDB requires verified TLS")
