"""提交视点演练：订单头和商品行分开复制时，一次查询只读同一个提交视点。

模型要点：
- 一次 commit 原子写入 orders 与 order_lines 两表，并分配一个单调递增的提交位点。
- 复制批次按“节点 + 表”到达，可以乱序；同一批次重试只按 batch 键应用一次。
- 节点视点 = min(订单头连续已应用位点, 商品行连续已应用位点)。查询只在该视点
  过滤两表，因此不会把订单头 107、商品行 106 拼成两笔提交。
- 主库下线后按策略选择：等待副本追齐；等待超时退回已齐旧视点；或等待超时拒绝。
  每条查询记录等待时长、实际视点、数据陈旧度，并给出“立即读旧视点”的对照，
  展示时效与等待的代价。
"""
import copy
import hashlib
import json

TABLES = ('orders', 'order_lines')
POLICIES = ('wait_then_stale', 'wait_then_reject', 'stale_first')
POLICY_LABELS = {
    'wait_then_stale': '等待副本追齐，超时退回已齐旧视点',
    'wait_then_reject': '等待副本追齐，超时拒绝读取',
    'stale_first': '立即读取已齐旧视点，不等待',
}
PRIMARY_DOWN_POLICY_DEFAULT = 'wait_then_stale'
CLASSIC_KINDS = ('write', 'read')


def _integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(label + '必须是整数')
    return value


def _table(position):
    return {'watermark': position, 'applied': set()}


def _viewpoint(node):
    return min(node['orders']['watermark'], node['order_lines']['watermark'])


def _node_view(node):
    return {
        'name': node['name'],
        'role': node['role'],
        'status': node['status'],
        'lineage': node['lineage'],
        'delay_ms': node['delay_ms'],
        'orders_watermark': node['orders']['watermark'],
        'lines_watermark': node['order_lines']['watermark'],
        'viewpoint': _viewpoint(node),
        'buffered': {
            'orders': sorted(node['orders']['applied']),
            'order_lines': sorted(node['order_lines']['applied']),
        },
    }


def _version_at(commit_log, order_id, position):
    for pos, committed_order, _session, _at in reversed(commit_log):
        if pos <= position and committed_order == order_id:
            return pos
    return None


def _best_replica(world, lineage, max_delay_ms, required, order_id, target, fresh_only):
    best = None
    for node in world['nodes'].values():
        if node['role'] != 'replica':
            continue
        if node['status'] != 'healthy' or node['lineage'] != lineage:
            continue
        if node['delay_ms'] > max_delay_ms:
            continue
        vp = _viewpoint(node)
        if vp < required or vp < target:
            continue
        if _version_at(world['commit_log'], order_id, vp) is None:
            continue
        if fresh_only and vp < world['committed']:
            continue
        key = (node['delay_ms'], -vp, node['name'])
        if best is None or key < best[0]:
            best = (key, node, vp)
    return (best[1], best[2]) if best else (None, None)


def _apply(world, lineage, event):
    """把事件应用到世界状态；返回 (decision, node, extra)。非法输入抛 ValueError。"""
    kind = event.get('kind')
    if kind in ('node_down', 'node_up'):
        node = world['nodes'].get(event.get('node'))
        if not node:
            raise ValueError('事件引用未知节点：' + str(event.get('node')))
        node['status'] = 'down' if kind == 'node_down' else 'healthy'
        return kind, node, {}
    if kind == 'commit':
        order_id = event.get('order')
        if not order_id:
            raise ValueError('commit 需要 order')
        primary = world['primary']
        if primary['status'] != 'healthy':
            return 'commit_rejected', primary, {'order': order_id}
        world['committed'] += 1
        session = event.get('session', 'default')
        world['sessions'][session] = {'required': world['committed'], 'last_commit_ms': event['at_ms']}
        world['commit_log'].append((world['committed'], order_id, session, event['at_ms']))
        world['commit_time'][world['committed']] = event['at_ms']
        primary['orders']['watermark'] = world['committed']
        primary['order_lines']['watermark'] = world['committed']
        return 'committed', primary, {'order': order_id, 'position': world['committed']}
    if kind == 'replicate':
        node = world['nodes'].get(event.get('node'))
        if not node:
            raise ValueError('事件引用未知节点：' + str(event.get('node')))
        table = event.get('table')
        batch = event.get('batch')
        position = event.get('position')
        if table not in TABLES:
            raise ValueError('replicate 需要 table=orders 或 order_lines')
        if not batch:
            raise ValueError('replicate 需要 batch 幂等键')
        position = _integer(position, '复制位点')
        if node['role'] != 'replica':
            raise ValueError('replicate 仅允许副本节点')
        if node['status'] != 'healthy':
            raise ValueError('停机副本不能应用日志，请先 node_up')
        if node['lineage'] != lineage:
            raise ValueError('日志谱系不匹配，不得复制当前主库日志')
        if position < 0 or position > world['committed']:
            raise ValueError('复制批次不得应用未提交位点')
        key = (table, batch)
        state = node[table]
        if key in node['seen_batches']:
            return 'duplicate', node, {'table': table, 'batch': batch, 'position': position}
        node['seen_batches'].add(key)
        if position <= state['watermark']:
            return 'noop', node, {'table': table, 'batch': batch, 'position': position}
        state['applied'].add(position)
        before = state['watermark']
        while state['watermark'] + 1 in state['applied']:
            state['watermark'] += 1
            state['applied'].remove(state['watermark'])
        return 'applied', node, {
            'table': table, 'batch': batch, 'position': position,
            'advanced': state['watermark'] != before,
        }
    if kind in CLASSIC_KINDS:
        raise ValueError('write/read 属于旧路由演练，请改用会话位点路由页面')
    raise ValueError('未知事件类型：' + str(kind))


class CommitViewSimulation:
    def __init__(self, node_inputs, events, max_delay_ms=100, wait_ms=500,
                 primary_down_policy=PRIMARY_DOWN_POLICY_DEFAULT):
        if not 1 <= len(node_inputs) <= 12:
            raise ValueError('需要1–12个节点')
        if not 1 <= len(events) <= 100:
            raise ValueError('需要1–100个时序事件')
        self.max_delay_ms = _integer(max_delay_ms, '延迟门槛')
        self.wait_ms = _integer(wait_ms, '等待上限')
        if self.max_delay_ms < 0 or self.wait_ms < 0:
            raise ValueError('延迟门槛和等待上限不得为负')
        if primary_down_policy not in POLICIES:
            raise ValueError('未知的主库下线策略：' + str(primary_down_policy))
        self.policy = primary_down_policy

        names = [n['name'] for n in node_inputs]
        if len(set(names)) != len(names):
            raise ValueError('节点名必须唯一')
        primaries = [n for n in node_inputs if n['role'] == 'primary']
        if len(primaries) != 1:
            raise ValueError('必须且只能配置一个主节点')
        primary_input = primaries[0]
        base = _integer(primary_input['position'], '主库位点')
        if base < 0:
            raise ValueError('主库位点不得为负')
        self.lineage = primary_input['lineage']
        self.events = events

        nodes = {}
        for item in node_inputs:
            node = copy.deepcopy(item)
            for key in ('name', 'role', 'status', 'lineage', 'delay_ms'):
                if key not in node:
                    raise ValueError('节点缺少字段：' + key)
            if _integer(node['delay_ms'], '节点延迟') < 0:
                raise ValueError('节点延迟不得为负')
            if node['role'] == 'primary':
                orders_pos = lines_pos = base
            else:
                orders_pos = _integer(node.get('orders_position', node['position']), '订单头位点')
                lines_pos = _integer(node.get('lines_position', node['position']), '商品行位点')
                if orders_pos > base or lines_pos > base:
                    raise ValueError('同谱系副本两表初始位点不得高于主库位点')
            nodes[node['name']] = {
                'name': node['name'], 'role': node['role'], 'status': node['status'],
                'lineage': node['lineage'], 'delay_ms': node['delay_ms'],
                'orders': _table(orders_pos), 'order_lines': _table(lines_pos),
                'seen_batches': set(),
            }
        self.world = {
            'nodes': nodes,
            'primary': nodes[primary_input['name']],
            'committed': base,
            'commit_log': [],
            'commit_time': {base: 0},
            'sessions': {},
        }
        self.trace = []
        self.stats = {}

    # ---------- 单事件 ----------
    def run(self):
        last_time = -1
        for index, event in enumerate(self.events):
            at = _integer(event.get('at_ms'), '事件时间')
            if at < last_time:
                raise ValueError('事件必须按时间非递减排列')
            last_time = at
            kind = event.get('kind')
            session = event.get('session', 'default')
            row = {
                'index': index + 1, 'at_ms': at, 'kind': kind, 'session': session,
                'node': None, 'decision': 'applied', 'reason': '',
                'required_position': self.world['sessions'].get(session, {}).get('required', 0),
            }
            if kind == 'query':
                self._query(index, row, event, at)
            elif kind in ('commit', 'replicate', 'node_down', 'node_up'):
                self._event(row, event, at)
            else:
                if kind in CLASSIC_KINDS:
                    raise ValueError('write/read 属于旧路由演练，请改用会话位点路由页面')
                raise ValueError('未知事件类型：' + str(kind))
            self.trace.append(row)
        return self._result()

    def _event(self, row, event, at):
        decision, node, extra = _apply(self.world, self.lineage, event)
        row['node'] = node['name']
        if decision == 'commit_rejected':
            row.update(decision='rejected', order=extra['order'],
                       reason='主库不可用，拒绝提交')
        elif decision == 'committed':
            row.update(decision='committed', order=extra['order'],
                       observed_position=extra['position'], required_position=extra['position'],
                       commit_version=extra['position'],
                       reason='订单头与商品行同一提交位点写入主库')
            self._stat('commits')
        elif decision == 'duplicate':
            row.update(decision='duplicate', table=extra['table'], batch=extra['batch'],
                       position=extra['position'], watermark=node[extra['table']]['watermark'],
                       viewpoint=_viewpoint(node),
                       reason='同批重试，批次键已应用过，不重复推进位点')
            self._stat('duplicate_batches')
        elif decision == 'noop':
            row.update(decision='applied', table=extra['table'], batch=extra['batch'],
                       position=extra['position'], watermark=node[extra['table']]['watermark'],
                       viewpoint=_viewpoint(node),
                       reason='批次位点落在连续区间内，幂等忽略')
            self._stat('noop_batches')
        elif decision in ('node_down', 'node_up'):
            row.update(decision=decision,
                       reason='节点下线' if decision == 'node_down' else '节点恢复')
        else:
            state = node[extra['table']]
            buffered = sorted(state['applied'])
            row.update(decision='applied', table=extra['table'], batch=extra['batch'],
                       position=extra['position'], watermark=state['watermark'],
                       buffered_positions=buffered, viewpoint=_viewpoint(node),
                       reason='批次应用，连续位点推进' if extra['advanced']
                       else '批次到达但存在缺口，暂存等待，位点不推进')
            self._stat('batches')
            if not extra['advanced']:
                self._stat('held_batches')
        if 'table' in row and row.get('decision') in ('applied', 'duplicate'):
            row['node_view'] = _node_view(node)
        if decision in ('node_down', 'node_up'):
            row['node_view'] = _node_view(node)

    # ---------- 查询 ----------
    def _query(self, index, row, event, at):
        order_id = event.get('order')
        if not order_id:
            raise ValueError('query 需要 order')
        self._stat('queries')
        session = event.get('session', 'default')
        required = self.world['sessions'].get(session, {}).get('required', 0)
        explicit = event.get('required_position')
        if explicit is not None:
            required = max(required, _integer(explicit, '查询会话位点'))
        target = self.world['committed']
        row.update(order_id=order_id, required_position=required,
                   committed_position=target)
        row['candidates'] = self._candidate_rows(self.world, required, order_id)
        primary = self.world['primary']
        if primary['status'] == 'healthy':
            self._probe(self.world, row, primary, target, at, 0,
                        'primary', '主库在线，按最新提交视点读取订单头与商品行')
            return

        fresh_node, fresh_vp = _best_replica(
            self.world, self.lineage, self.max_delay_ms, required, order_id,
            target, True)
        if fresh_node:
            self._probe(self.world, row, fresh_node, fresh_vp, at, 0, 'already_settled',
                        '主库下线，副本两表均已追齐提交位点且满足会话位点')
            return

        stale_node, stale_vp = _best_replica(
            self.world, self.lineage, self.max_delay_ms, required, order_id, 0, False)
        row['alternative'] = self._alternative(self.world, stale_node, stale_vp, at)

        if self.policy == 'stale_first':
            if stale_node:
                self._probe(self.world, row, stale_node, stale_vp, at, 0, 'stale_now',
                            '主库下线，按策略立即读取已齐旧视点，不等待追齐')
            else:
                self._reject_unreadable(row, self.world, order_id, at, 0, 'no_candidate',
                                        '主库下线且无满足会话位点的已齐副本，拒绝读取')
            return

        wait = self._project(index, at, required, order_id, target)
        caught = wait['caught_up_at']
        if caught is not None:
            world = wait['world']
            node = world['nodes'][wait['node']]
            self._probe(world, row, node, wait['viewpoint'], caught, caught - at, 'caught_up',
                        '等待副本追齐提交位点后读取，时效无落后')
            self._merge_candidates(row, world, required, order_id)
            return

        if self.policy == 'wait_then_stale':
            world = wait['world']
            node, vp = _best_replica(world, self.lineage, self.max_delay_ms, required,
                                     order_id, 0, False)
            if node:
                self._probe(world, row, node, vp, at + self.wait_ms, self.wait_ms,
                            'fell_back_stale',
                            '等待上限内未追齐，退回最后已齐旧视点（仍满足会话位点）')
            else:
                self._reject_unreadable(row, world, order_id, at + self.wait_ms,
                                        self.wait_ms, 'no_safe_view',
                                        '等待上限内未追齐，且无满足会话位点的旧视点，拒绝读取')
            self._merge_candidates(row, world, required, order_id)
            return

        self._reject_unreadable(row, wait['world'], order_id, at + self.wait_ms,
                                self.wait_ms, 'not_caught_up',
                                '等待上限内未追齐提交位点，按策略拒绝读取')

    def _reject_unreadable(self, row, world, order_id, read_at, waited, outcome, reason):
        self._reject(row, read_at, waited, outcome, reason)
        best = None
        for node in world['nodes'].values():
            if node['role'] != 'replica' or node['status'] != 'healthy':
                continue
            if node['lineage'] != self.lineage or node['delay_ms'] > self.max_delay_ms:
                continue
            if best is None or _viewpoint(node) > _viewpoint(best):
                best = node
        if best:
            evidence = self._splice_evidence(world, best, order_id)
            row['node'] = best['name']
            row['viewpoint'] = _viewpoint(best)
            row['splice_would_occur'] = evidence['splice_would_occur']
            row['naive_header_version'] = evidence['header']
            row['naive_lines_version'] = evidence['lines']
            if evidence['splice_would_occur']:
                row['reason'] += '；若两表各取自身位点会拼成不同提交版本'

    def _alternative(self, world, node, viewpoint, at):
        if not node:
            return {'available': False}
        lag = world['committed'] - viewpoint
        return {
            'available': True,
            'node': node['name'],
            'viewpoint': viewpoint,
            'freshness_lag': lag,
            'data_age_ms': max(0, at - world['commit_time'].get(viewpoint, 0)),
        }

    def _splice_evidence(self, world, node, order_id):
        header = _version_at(world['commit_log'], order_id, node['orders']['watermark'])
        lines = _version_at(world['commit_log'], order_id, node['order_lines']['watermark'])
        return {'header': header, 'lines': lines, 'splice_would_occur': header != lines}

    def _candidate_rows(self, world, required, order_id):
        rows = []
        for node in world['nodes'].values():
            if node['role'] != 'replica':
                continue
            vp = _viewpoint(node)
            rows.append({
                'name': node['name'],
                'healthy': node['status'] == 'healthy',
                'same_lineage': node['lineage'] == self.lineage,
                'orders_watermark': node['orders']['watermark'],
                'lines_watermark': node['order_lines']['watermark'],
                'viewpoint': vp,
                'delay_ms': node['delay_ms'],
                'delay_ok': node['delay_ms'] <= self.max_delay_ms,
                'meets_required': vp >= required,
                'order_visible': _version_at(world['commit_log'], order_id, vp) is not None,
            })
        for item in rows:
            item['eligible'] = (item['healthy'] and item['same_lineage'] and item['delay_ok']
                                and item['meets_required'] and item['order_visible'])
            item['fresh'] = item['eligible'] and item['viewpoint'] >= world['committed']
        return rows

    def _merge_candidates(self, row, world, required, order_id):
        row['candidates_after_wait'] = self._candidate_rows(world, required, order_id)

    def _probe(self, world, row, node, viewpoint, read_at, waited, outcome, reason):
        order_id = row['order_id']
        version = _version_at(world['commit_log'], order_id, viewpoint)
        if version is None:
            self._reject(row, read_at, waited, outcome,
                         '该订单在读取视点中不可见，拒绝不完整读取')
            return False
        naive_header = _version_at(world['commit_log'], order_id, node['orders']['watermark'])
        naive_lines = _version_at(world['commit_log'], order_id, node['order_lines']['watermark'])
        splice = naive_header != naive_lines
        lag = world['committed'] - viewpoint
        age_ms = max(0, read_at - world['commit_time'].get(viewpoint, 0))
        self._stat('routed')
        self._account_wait(waited)
        self._stat('fresh_reads' if lag == 0 else 'stale_reads')
        if splice:
            self._stat('splice_would_occur')
            self._stat('splice_avoided')
        self._max('max_freshness_lag', lag)
        row.update(decision='routed', node=node['name'], read_at_ms=read_at,
                   waited_ms=waited, wait_outcome=outcome, viewpoint=viewpoint,
                   header_version=version, lines_version=version,
                   naive_header_version=naive_header, naive_lines_version=naive_lines,
                   splice_would_occur=splice, freshness_lag=lag, data_age_ms=age_ms,
                   reason=reason)
        return True

    def _reject(self, row, read_at, waited, outcome, reason):
        self._stat('rejected')
        self._account_wait(waited)
        row.update(decision='rejected', read_at_ms=read_at, waited_ms=waited,
                   wait_outcome=outcome, reason=reason)

    def _account_wait(self, waited):
        if not waited:
            return
        self._stat('waited_queries')
        self._add('total_waited_ms', waited)
        self._max('max_waited_ms', waited)

    # ---------- 等待窗口投影 ----------
    def _project(self, index, at, required, order_id, target):
        world = copy.deepcopy(self.world)
        for node in world['nodes'].values():
            node['seen_batches'] = set(node['seen_batches'])
        horizon = at + self.wait_ms
        future = self.events[index + 1:]
        caught = None
        for event in future:
            event_at = event.get('at_ms')
            if event_at > horizon:
                break
            kind = event.get('kind')
            if kind not in ('commit', 'replicate', 'node_down', 'node_up'):
                continue
            _apply(world, self.lineage, event)
            if world['primary']['status'] == 'healthy':
                caught = {'at': event_at, 'node': world['primary']['name'],
                          'viewpoint': world['committed']}
                break
            node, vp = _best_replica(world, self.lineage, self.max_delay_ms,
                                     required, order_id, target, True)
            if node:
                caught = {'at': event_at, 'node': node['name'], 'viewpoint': vp}
                break
        if caught:
            return {'caught_up_at': caught['at'], 'node': caught['node'],
                    'viewpoint': caught['viewpoint'], 'world': world}
        return {'caught_up_at': None, 'node': None, 'viewpoint': None, 'world': world}

    # ---------- 汇总 ----------
    def _stat(self, key):
        self.stats[key] = self.stats.get(key, 0) + 1

    def _add(self, key, value):
        self.stats[key] = self.stats.get(key, 0) + value

    def _max(self, key, value):
        self.stats[key] = max(self.stats.get(key, 0), value)

    def _result(self):
        reads = [r for r in self.trace if r['kind'] == 'query' and r['decision'] == 'routed']
        final_nodes = [_node_view(n) for n in self.world['nodes'].values()]
        for node in final_nodes:
            node['lag_to_committed'] = self.world['committed'] - node['viewpoint']
        summary = dict(self.stats)
        for key in ('commits', 'duplicate_batches', 'noop_batches', 'batches', 'held_batches',
                    'queries', 'routed', 'rejected', 'waited_queries', 'total_waited_ms',
                    'max_waited_ms', 'fresh_reads', 'stale_reads', 'splice_would_occur',
                    'splice_avoided', 'max_freshness_lag'):
            summary.setdefault(key, 0)
        summary.update({
            'commits': summary['commits'],
            'committed_position': self.world['committed'],
            'reads': len(reads),
            'primary_reads': sum(1 for r in reads if r['node'] == self.world['primary']['name']),
            'replica_reads': sum(1 for r in reads if r['node'] != self.world['primary']['name']),
            'avg_waited_ms': (summary['total_waited_ms'] // summary['waited_queries']
                              if summary['waited_queries'] else 0),
            'reads_single_viewpoint': all(r['header_version'] == r['lines_version'] for r in reads),
            'all_routed_reads_consistent': all(r['header_version'] == r['lines_version']
                                               for r in reads),
            'sessions': {k: dict(v) for k, v in self.world['sessions'].items()},
        })
        result = {
            'mode': '提交视点模型 / 订单头与商品行分开复制 / 非真实复制集群',
            'lineage': self.lineage,
            'committed_position': self.world['committed'],
            'policy': {
                'max_delay_ms': self.max_delay_ms,
                'wait_ms': self.wait_ms,
                'primary_down_policy': self.policy,
                'primary_down_policy_label': POLICY_LABELS[self.policy],
                'viewpoint_rule': '节点视点 = min(订单头连续位点, 商品行连续位点)',
                'batch_rule': '同一节点同一批次的表内位点只应用一次，位点连续才推进视点',
                'wait_rule': '仅主库下线且无追齐副本时等待，最多 wait_ms；等待窗口为到达时刻起的投影事件',
            },
            'trace': self.trace,
            'commit_log': [{'position': p, 'order': o, 'session': s, 'at_ms': t}
                           for p, o, s, t in self.world['commit_log']],
            'final_nodes': final_nodes,
            'summary': summary,
        }
        result['checksum'] = hashlib.sha256(
            json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return result


def simulate(node_inputs, events, max_delay_ms=100, wait_ms=500,
             primary_down_policy=PRIMARY_DOWN_POLICY_DEFAULT):
    return CommitViewSimulation(node_inputs, events, max_delay_ms, wait_ms,
                                primary_down_policy).run()
