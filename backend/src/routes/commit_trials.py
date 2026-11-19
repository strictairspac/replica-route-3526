import json
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from src.config.database import db_session
from src.utils.security import current, writer
from src.schemas.trial import CommitTrialInput
from src.services import commit_trials as service

router = APIRouter()
POLICIES = [
    {'value': 'wait_then_stale', 'label': '等待副本追齐，超时退回已齐旧视点'},
    {'value': 'wait_then_reject', 'label': '等待副本追齐，超时拒绝读取'},
    {'value': 'stale_first', 'label': '立即读取已齐旧视点，不等待'},
]


@router.get('/commit_trials/policies')
def policies(user=Depends(current)):
    return POLICIES


@router.get('/commit_trials')
def listing(db=Depends(db_session), user=Depends(current)):
    return service.listing(db)


@router.post('/commit_trials', status_code=201)
def create(data: CommitTrialInput, db=Depends(db_session), user=Depends(writer)):
    return service.create(db, data, user)


@router.get('/commit_trials/{ident}')
def detail(ident: int, db=Depends(db_session), user=Depends(current)):
    return service.detail(db, ident)


@router.get('/commit_trials/{ident}/export')
def export(ident: int, db=Depends(db_session), user=Depends(current)):
    return Response(json.dumps(service.detail(db, ident), ensure_ascii=False, indent=2),
                    media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="commit-{ident}.json"'})
