from typing import Annotated

from fastapi import Depends, Request

from app.db.mongo import MongoRepository


def get_mongo_repository(request: Request) -> MongoRepository:
    return request.app.state.mongo_repository


MongoRepositoryDep = Annotated[MongoRepository, Depends(get_mongo_repository)]
