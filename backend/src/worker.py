import json
import os
import traceback
from pathlib import Path
from src.config.database import Session
from src.models.entities import CommitTrial, Trial
from src.services.trials import queue
from src.services.routing import simulate
from src.services.commit_routing import simulate as simulate_commit

ARTIFACT_DIR = Path(os.getenv('ARTIFACT_DIR', '/artifacts'))


def run_classic(ident):
    with Session() as db:
        obj = db.get(Trial, ident)
        if not obj:
            return
        obj.status = 'running'
        db.commit()
        snapshot, delay, sticky = obj.input_snapshot, obj.max_delay_ms, obj.sticky_ms
    result = simulate(snapshot['nodes'], snapshot['request_set']['events'], delay, sticky)
    with Session() as db:
        obj = db.get(Trial, ident)
        obj.result = result
        obj.status = 'completed'
        db.commit()
    write_artifact(f'route-{ident}.json', result)


def run_commit(ident):
    with Session() as db:
        obj = db.get(CommitTrial, ident)
        if not obj:
            return
        obj.status = 'running'
        db.commit()
        snapshot = obj.input_snapshot
        delay, wait, policy = obj.max_delay_ms, obj.wait_ms, obj.primary_down_policy
    result = simulate_commit(snapshot['nodes'], snapshot['request_set']['events'],
                             delay, wait, policy)
    with Session() as db:
        obj = db.get(CommitTrial, ident)
        obj.result = result
        obj.status = 'completed'
        db.commit()
    write_artifact(f'commit-{ident}.json', result)


def write_artifact(name, result):
    """证据已写入 MySQL；文件产物缺失不应把已完成的演练改判为失败。"""
    try:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        (ARTIFACT_DIR / name).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    except OSError:
        traceback.print_exc()


def fail(model, ident, error):
    with Session() as db:
        obj = db.get(model, ident)
        if obj:
            obj.status = 'failed'
            obj.error = error
            db.commit()


def handle(raw):
    if raw.startswith('commit:'):
        ident = int(raw.split(':', 1)[1])
        try:
            run_commit(ident)
        except Exception as e:
            traceback.print_exc()
            fail(CommitTrial, ident, str(e))
        return
    ident = int(raw)
    try:
        run_classic(ident)
    except Exception as e:
        traceback.print_exc()
        fail(Trial, ident, str(e))


if __name__ == '__main__':
    while True:
        Path('/tmp/worker-heartbeat').touch()
        item = queue.blpop('replicaroute:queue', timeout=2)
        if not item:
            continue
        handle(item[1])
