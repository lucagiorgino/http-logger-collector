from typing import Annotated

from fastapi import Depends, Request

from app.core.encryption import EncryptionKeys
from app.core.storage_encryption import StorageEncryption, get_storage_encryption
from app.db.mongo import MongoRepository


async def get_mongo_repository(request: Request) -> MongoRepository:
    return request.app.state.mongo_repository


MongoRepositoryDep = Annotated[MongoRepository, Depends(get_mongo_repository)]


async def get_request_encryption_keys(request: Request) -> EncryptionKeys:
    return request.app.state.encryption_keys


EncryptionKeysDep = Annotated[EncryptionKeys, Depends(get_request_encryption_keys)]
StorageEncryptionDep = Annotated[StorageEncryption, Depends(get_storage_encryption)]
