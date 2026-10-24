from typing import Annotated

from fastapi import Depends, Request

from app.core.encryption import EncryptionKeys, get_encryption_keys
from app.db.mongo import MongoRepository
from app.services.ingestion import IngestionPublisher, MongoIngestionPublisher


def get_mongo_repository(request: Request) -> MongoRepository:
    return request.app.state.mongo_repository


MongoRepositoryDep = Annotated[MongoRepository, Depends(get_mongo_repository)]
EncryptionKeysDep = Annotated[EncryptionKeys, Depends(get_encryption_keys)]


def get_ingestion_publisher(repository: MongoRepositoryDep) -> IngestionPublisher:
    return MongoIngestionPublisher(repository)


IngestionPublisherDep = Annotated[IngestionPublisher, Depends(get_ingestion_publisher)]
