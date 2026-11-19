from typing import Literal
from pydantic import BaseModel, Field, model_validator

class Event(BaseModel):
    at_ms: int = Field(ge=0, le=86400000)
    kind: Literal['write', 'read', 'commit', 'query', 'replicate', 'node_down', 'node_up']
    session: str = Field(default='default', min_length=1, max_length=60)
    node: str | None = Field(default=None, max_length=80)
    position: int | None = Field(default=None, ge=0, le=1000000000)
    delay_ms: int | None = Field(default=None, ge=0, le=3600000)
    order: str | None = Field(default=None, min_length=1, max_length=80)
    table: Literal['orders', 'order_lines'] | None = None
    batch: str | None = Field(default=None, min_length=1, max_length=80)
    required_position: int | None = Field(default=None, ge=0, le=1000000000)

    @model_validator(mode='after')
    def valid_event(self):
        if self.kind in ('replicate', 'node_down', 'node_up') and not self.node:
            raise ValueError('节点事件需要node')
        if self.kind == 'replicate':
            if self.position is None:
                raise ValueError('replicate需要position')
            if self.table and not self.batch:
                raise ValueError('提交视点复制需要batch幂等键')
            if self.batch and not self.table:
                raise ValueError('提交视点复制需要table=orders或order_lines')
            if not self.table and self.delay_ms is None:
                raise ValueError('会话位点路由的replicate需要delay_ms')
        if self.kind in ('commit', 'query') and not self.order:
            raise ValueError('提交与查询事件需要order')
        return self


class RequestSetInput(BaseModel):
    code: str = Field(min_length=2, max_length=80)
    title: str = Field(min_length=2, max_length=160)
    events: list[Event] = Field(min_length=1, max_length=100)
    note: str = Field(default='', max_length=300)

    @model_validator(mode='after')
    def chronological(self):
        if any(a.at_ms > b.at_ms for a, b in zip(self.events, self.events[1:])):
            raise ValueError('事件必须按at_ms非递减排列')
        return self
