"""提交视点与旧路由演练的回归测试（不依赖 MySQL/Redis）。"""
import importlib.util
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.services import commit_routing as cv
from src.services import routing as classic

NODES = [
    {'name': 'harbor-primary', 'role': 'primary', 'status': 'healthy',
     'lineage': 'qiyuan-log-01', 'position': 105, 'delay_ms': 0},
    {'name': 'south-replica', 'role': 'replica', 'status': 'healthy',
     'lineage': 'qiyuan-log-01', 'position': 105, 'delay_ms': 20},
    {'name': 'north-replica', 'role': 'replica', 'status': 'healthy',
     'lineage': 'qiyuan-log-01', 'position': 104, 'delay_ms': 150},
]


def run(events, wait=500, policy='wait_then_stale', delay=100, nodes=None):
    return cv.simulate(nodes or NODES, events, delay, wait, policy)


def queries(result):
    return [r for r in result['trace'] if r['kind'] == 'query']


class SplitReplicationTest(unittest.TestCase):
    def test_out_of_order_lines_hold_viewpoint(self):
        result = run([
            {'at_ms': 0, 'kind': 'commit', 'session': 's', 'order': 'A'},         # 106
            {'at_ms': 10, 'kind': 'commit', 'session': 's', 'order': 'B'},        # 107
            {'at_ms': 20, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'orders', 'batch': 'o-106', 'position': 106},
            {'at_ms': 30, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'order_lines', 'batch': 'l-107', 'position': 107},
            {'at_ms': 40, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'order_lines', 'batch': 'l-107', 'position': 107},
        ])
        rep = [r for r in result['trace'] if r['kind'] == 'replicate']
        self.assertEqual(rep[1]['decision'], 'applied')
        self.assertEqual(rep[1]['watermark'], 105)
        self.assertEqual(rep[1]['buffered_positions'], [107])
        self.assertEqual(rep[2]['decision'], 'duplicate')
        self.assertEqual(rep[2]['watermark'], 105)
        self.assertEqual(result['summary']['duplicate_batches'], 1)
        self.assertEqual(result['summary']['held_batches'], 1)

    def test_gap_fill_advances_both_tables(self):
        result = run([
            {'at_ms': 0, 'kind': 'commit', 'session': 's', 'order': 'A'},
            {'at_ms': 10, 'kind': 'commit', 'session': 's', 'order': 'B'},
            {'at_ms': 20, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'orders', 'batch': 'o-106', 'position': 106},
            {'at_ms': 30, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'orders', 'batch': 'o-107', 'position': 107},
            {'at_ms': 40, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'order_lines', 'batch': 'l-107', 'position': 107},
            {'at_ms': 50, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'order_lines', 'batch': 'l-106', 'position': 106},
        ])
        south = [n for n in result['final_nodes'] if n['name'] == 'south-replica'][0]
        self.assertEqual((south['orders_watermark'], south['lines_watermark']), (107, 107))
        self.assertEqual(south['viewpoint'], 107)

    def test_duplicate_retry_same_batch_counted_once(self):
        result = run([
            {'at_ms': 0, 'kind': 'commit', 'session': 's', 'order': 'A'},
            {'at_ms': 10, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'orders', 'batch': 'o-106', 'position': 106},
            {'at_ms': 20, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'orders', 'batch': 'o-106', 'position': 106},
            {'at_ms': 30, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'orders', 'batch': 'o-106', 'position': 106},
        ])
        self.assertEqual(result['summary']['batches'], 1)
        self.assertEqual(result['summary']['duplicate_batches'], 2)

    def test_replicate_beyond_commit_is_rejected(self):
        with self.assertRaises(ValueError):
            run([{'at_ms': 0, 'kind': 'replicate', 'node': 'south-replica',
                  'table': 'orders', 'batch': 'o-200', 'position': 200}])


class ViewpointQueryTest(unittest.TestCase):
    def test_primary_read_is_always_fresh_and_single_view(self):
        result = run([
            {'at_ms': 0, 'kind': 'commit', 'session': 's', 'order': 'A'},
            {'at_ms': 10, 'kind': 'query', 'session': 's', 'order': 'A'},
        ])
        q = queries(result)[0]
        self.assertEqual((q['header_version'], q['lines_version']), (106, 106))
        self.assertEqual(q['freshness_lag'], 0)

    def test_replica_query_never_splices_when_order_has_two_versions(self):
        # 同一订单在 106 与 107 各提交一次；订单头追到 107、商品行只到 106。
        result = run([
            {'at_ms': 0, 'kind': 'commit', 'session': 's', 'order': 'A'},   # 106
            {'at_ms': 10, 'kind': 'commit', 'session': 's', 'order': 'A'},  # 107 改单
            {'at_ms': 20, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'orders', 'batch': 'o-106', 'position': 106},
            {'at_ms': 30, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'orders', 'batch': 'o-107', 'position': 107},
            {'at_ms': 40, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'order_lines', 'batch': 'l-106', 'position': 106},
            {'at_ms': 50, 'kind': 'node_down', 'node': 'harbor-primary'},
            {'at_ms': 60, 'kind': 'query', 'session': 'visitor', 'order': 'A'},
        ])
        q = queries(result)[0]
        self.assertEqual(q['decision'], 'routed')
        self.assertEqual(q['viewpoint'], 106)
        self.assertEqual((q['header_version'], q['lines_version']), (106, 106))
        self.assertEqual((q['naive_header_version'], q['naive_lines_version']), (107, 106))
        self.assertTrue(q['splice_would_occur'], '两表各取自身位点会拼成 107 与 106')

    def test_missing_order_is_rejected_not_spliced(self):
        result = run([
            {'at_ms': 0, 'kind': 'commit', 'session': 's', 'order': 'A'},   # 106
            {'at_ms': 10, 'kind': 'commit', 'session': 's', 'order': 'B'},  # 107
            {'at_ms': 20, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'orders', 'batch': 'o-106', 'position': 106},
            {'at_ms': 30, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'order_lines', 'batch': 'l-106', 'position': 106},
            {'at_ms': 40, 'kind': 'node_down', 'node': 'harbor-primary'},
            {'at_ms': 50, 'kind': 'query', 'session': 'visitor', 'order': 'B'},
        ])
        q = queries(result)[0]
        self.assertEqual(q['decision'], 'rejected')
        self.assertEqual(q['viewpoint'], 106)


class PrimaryDownPolicyTest(unittest.TestCase):
    # 提交到 107，但商品行只追到 106：副本视点 106，无法立即满足最新时效。
    EVENTS = [
        {'at_ms': 0, 'kind': 'commit', 'session': 's', 'order': 'A'},    # 106
        {'at_ms': 10, 'kind': 'commit', 'session': 's', 'order': 'A'},   # 107 改单
        {'at_ms': 20, 'kind': 'replicate', 'node': 'south-replica',
         'table': 'orders', 'batch': 'o-106', 'position': 106},
        {'at_ms': 30, 'kind': 'replicate', 'node': 'south-replica',
         'table': 'orders', 'batch': 'o-107', 'position': 107},
        {'at_ms': 40, 'kind': 'replicate', 'node': 'south-replica',
         'table': 'order_lines', 'batch': 'l-106', 'position': 106},
        {'at_ms': 50, 'kind': 'node_down', 'node': 'harbor-primary'},
        {'at_ms': 500, 'kind': 'query', 'session': 'visitor', 'order': 'A'},
    ]

    def test_wait_then_stale_falls_back_after_full_wait(self):
        result = run(self.EVENTS, wait=500, policy='wait_then_stale')
        q = queries(result)[0]
        self.assertEqual(q['decision'], 'routed')
        self.assertEqual(q['wait_outcome'], 'fell_back_stale')
        self.assertEqual(q['waited_ms'], 500)
        self.assertEqual(q['viewpoint'], 106)
        self.assertEqual(q['freshness_lag'], 1)
        self.assertEqual((q['header_version'], q['lines_version']), (106, 106))
        self.assertTrue(q['splice_would_occur'], '若不按视点读取，两表会分别落到 107 与 106')
        self.assertEqual(result['summary']['waited_queries'], 1)
        self.assertEqual(result['summary']['avg_waited_ms'], 500)
        self.assertEqual(result['summary']['splice_avoided'], 1)

    def test_stale_first_reads_immediately(self):
        result = run(self.EVENTS, wait=500, policy='stale_first')
        q = queries(result)[0]
        self.assertEqual(q['wait_outcome'], 'stale_now')
        self.assertEqual(q['waited_ms'], 0)
        self.assertEqual(q['freshness_lag'], 1)
        self.assertEqual(result['summary']['total_waited_ms'], 0)

    def test_wait_then_reject_returns_no_rows(self):
        result = run(self.EVENTS, wait=500, policy='wait_then_reject')
        q = queries(result)[0]
        self.assertEqual(q['decision'], 'rejected')
        self.assertEqual(q['wait_outcome'], 'not_caught_up')
        self.assertEqual(q['waited_ms'], 500)
        self.assertEqual(result['summary']['routed'], 0)
        self.assertEqual(result['summary']['rejected'], 1)

    def test_wait_catches_up_after_late_replica(self):
        events = [
            {'at_ms': 0, 'kind': 'commit', 'session': 's', 'order': 'A'},   # 106
            {'at_ms': 10, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'orders', 'batch': 'o-106', 'position': 106},
            {'at_ms': 20, 'kind': 'node_down', 'node': 'harbor-primary'},
            {'at_ms': 30, 'kind': 'query', 'session': 'visitor', 'order': 'A'},
            {'at_ms': 120, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'order_lines', 'batch': 'l-106', 'position': 106},
        ]
        result = run(events, wait=200, policy='wait_then_stale')
        q = queries(result)[0]
        self.assertEqual(q['wait_outcome'], 'caught_up')
        self.assertEqual(q['waited_ms'], 90)
        self.assertEqual(q['freshness_lag'], 0)
        self.assertEqual(q['read_at_ms'], 120)

    def test_without_catch_up_window_rejects_immediately_when_no_stale_view(self):
        events = [
            {'at_ms': 0, 'kind': 'commit', 'session': 's', 'order': 'A'},
            {'at_ms': 10, 'kind': 'node_down', 'node': 'harbor-primary'},
            {'at_ms': 20, 'kind': 'query', 'session': 's', 'order': 'A'},
        ]
        result = run(events, wait=100, policy='wait_then_stale')
        q = queries(result)[0]
        self.assertEqual(q['decision'], 'rejected')
        self.assertEqual(q['wait_outcome'], 'no_safe_view')
        self.assertEqual(result['summary']['avg_waited_ms'], 100)

    def test_replicate_beyond_commit_inside_wait_window_is_rejected(self):
        events = self.EVENTS + [
            {'at_ms': 520, 'kind': 'replicate', 'node': 'south-replica',
             'table': 'order_lines', 'batch': 'l-108', 'position': 108},
        ]
        with self.assertRaises(ValueError):
            run(events, wait=500, policy='wait_then_stale')


class ValidationTest(unittest.TestCase):
    def test_requires_single_primary(self):
        bad = [n for n in NODES if n['role'] != 'primary']
        with self.assertRaises(ValueError):
            cv.simulate(bad, [{'at_ms': 0, 'kind': 'commit', 'session': 's', 'order': 'A'}])

    def test_replica_initial_position_above_primary(self):
        bad = [dict(NODES[0])] + [dict(NODES[1], position=999)]
        with self.assertRaises(ValueError):
            cv.simulate(bad, [{'at_ms': 0, 'kind': 'commit', 'session': 's', 'order': 'A'}])

    def test_classic_kinds_rejected(self):
        with self.assertRaises(ValueError):
            run([{'at_ms': 0, 'kind': 'read', 'session': 's'}])

    def test_unknown_policy_rejected(self):
        with self.assertRaises(ValueError):
            run([{'at_ms': 0, 'kind': 'commit', 'session': 's', 'order': 'A'}],
                policy='best_effort')

    def test_negative_wait_rejected(self):
        with self.assertRaises(ValueError):
            run([{'at_ms': 0, 'kind': 'commit', 'session': 's', 'order': 'A'}], wait=-1)

    def test_same_input_same_checksum(self):
        events = [
            {'at_ms': 0, 'kind': 'commit', 'session': 's', 'order': 'A'},
            {'at_ms': 10, 'kind': 'query', 'session': 's', 'order': 'A'},
        ]
        self.assertEqual(run(events)['checksum'], run(events)['checksum'])


class ClassicRegressionTest(unittest.TestCase):
    def test_old_routing_demo_still_matches_readme(self):
        events = [
            {'at_ms': 0, 'kind': 'write', 'session': 'lab-A'},
            {'at_ms': 100, 'kind': 'read', 'session': 'lab-A'},
            {'at_ms': 600, 'kind': 'replicate', 'node': 'south-replica',
             'position': 106, 'delay_ms': 20},
            {'at_ms': 700, 'kind': 'read', 'session': 'lab-A'},
            {'at_ms': 800, 'kind': 'node_down', 'node': 'south-replica'},
            {'at_ms': 900, 'kind': 'read', 'session': 'lab-A'},
            {'at_ms': 1000, 'kind': 'node_down', 'node': 'harbor-primary'},
            {'at_ms': 1100, 'kind': 'read', 'session': 'lab-A'},
        ]
        result = classic.simulate(NODES, events, 100, 500)
        self.assertEqual(result['summary']['requests'], 5)
        self.assertEqual(result['summary']['rejected'], 1)
        self.assertTrue(result['summary']['all_routed_reads_safe'])
        self.assertEqual(result['trace'][-1]['decision'], 'rejected')

    def test_classic_rejects_commit_view_events(self):
        with self.assertRaises(ValueError):
            classic.simulate(NODES, [{'at_ms': 0, 'kind': 'commit', 'session': 's'}], 100, 500)


if __name__ == '__main__':
    unittest.main()
