import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from resistor_flow.measurements import publish, read_current
from resistor_flow.review import (
    STATUS_AMBIGUOUS,
    STATUS_AS_PUBLISHED,
    STATUS_ISOLATED,
    STATUS_NO_CANDIDATE,
    UNBOUNDED,
    ReviewInputError,
    review,
)


def reading(station, row_id, channel, tick, raw_volts):
    return {'station': station, 'row_id': row_id, 'channel': channel,
            'tick': tick, 'raw_volts': raw_volts}


def registration(station, channel, resistor, node, start=0, end=10_000_000):
    return {'station': station, 'channel': channel, 'start_tick': start,
            'end_tick': end, 'resistor_id': resistor,
            'positive_node': node, 'gain': '1', 'offset_volts': '0'}


def request(run_id, request_id, base_version, readings, registrations,
            network=None):
    payload = {'run_id': run_id, 'request_id': request_id,
               'base_version': base_version, 'readings': readings,
               'registrations': registrations}
    if network is not None:
        payload['network'] = network
    return payload


def network(pairs):
    """pairs: [(resistor_id, node_a, node_b), ...]，节点电位仅供网络校验。"""
    nodes = []
    seen = set()
    for _rid, a, b in pairs:
        for node in (a, b):
            if node not in seen:
                seen.add(node)
                nodes.append({'id': node, 'voltage': '0'})
    return {'nodes': nodes,
            'resistors': [{'id': rid, 'a': a, 'b': b, 'ohms': '1'}
                          for rid, a, b in pairs]}


def make_station_readings(events):
    """events: [(tick, station, row_id, channel, resistor, raw), ...]。"""
    rows = []
    for tick, station, row_id, channel, _resistor, raw in events:
        rows.append(reading(station, row_id, channel, tick, raw))
    return rows


def make_registrations(events, endpoints):
    """为每条 (站, 通道) 建一条登记，正探针固定接电阻的 a 端。"""
    seen = set()
    result = []
    for _tick, station, _row_id, channel, resistor, _raw in events:
        key = (station, channel)
        if key in seen:
            continue
        seen.add(key)
        node_a = endpoints[resistor][0]
        result.append(registration(station, channel, resistor, node_a))
    return result


def publish_events(store, run_id, request_id, base_version, pairs, events,
                   network_data=None, extra_readings=()):
    endpoints = {rid: (a, b) for rid, a, b in pairs}
    readings = make_station_readings(events) + list(extra_readings)
    registrations = make_registrations(events, endpoints)
    if network_data is None:
        network_data = network(pairs)
    return publish(store, request(
        run_id, request_id, base_version, readings, registrations,
        network=network_data))


# 验收一的接线：两个三角形、一个孤立对、一条 A 与他站共同支持的边。
ACCEPTANCE_PAIRS = [
    ('ab', 'a', 'b'), ('bc', 'b', 'c'), ('ac', 'a', 'c'),
    ('de', 'd', 'e'), ('ef', 'e', 'f'), ('df', 'd', 'f'),
    ('gh', 'g', 'h'), ('ij', 'i', 'j'),
]

ACCEPTANCE_EVENTS = [
    # tick 1：B、C 支持短边，只有 A 支持长边，9 与 2+3 矛盾。
    (1, 'B', 'b1', 'B_ab', 'ab', '2'),
    (1, 'C', 'c1', 'C_bc', 'bc', '3'),
    (1, 'A', 'a1', 'A_ac', 'ac', '9'),
    # tick 2：另一三角形，D、E 支持短边，A 支持长边 8 与 4+1 矛盾。
    (2, 'D', 'd1', 'D_de', 'de', '4'),
    (2, 'E', 'e1', 'E_ef', 'ef', '1'),
    (2, 'A', 'a2', 'A_df', 'df', '8'),
    # tick 3：原本自洽的 tick。gh 由 A 独自支持，撤下后两端不再经剩余观测
    # 相连（无界）；ij 由 A 与 B 共同支持，撤下 A 后只删 A 的来源。
    (3, 'A', 'a3', 'A_gh', 'gh', '5'),
    (3, 'A', 'a4', 'A_ij', 'ij', '1'),
    (3, 'B', 'b3', 'B_ij', 'ij', '1'),
]

ACCEPTANCE_WIDTHS = {'ab': '1', 'bc': '1', 'ac': '0', 'de': '0',
                     'ef': '0', 'df': '0', 'gh': '0', 'ij': '0'}


class AcceptanceOneTests(unittest.TestCase):
    """两个矛盾 tick 只准隔离 A；补测区间精确；新版泄漏归零、旧版不变。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = self.temp.name
        self.net = network(ACCEPTANCE_PAIRS)
        publish_events(self.store, 'R', 'r1', 0, ACCEPTANCE_PAIRS,
                       ACCEPTANCE_EVENTS, network_data=self.net)

    def test_single_candidate_a_and_exact_retests(self):
        result = review(self.store, 'R', 1, [0, 20], ACCEPTANCE_WIDTHS)
        self.assertEqual(result['run_id'], 'R')
        self.assertEqual(result['version'], 1)
        self.assertEqual(result['window'], [0, 20])
        self.assertEqual(result['status'], STATUS_ISOLATED)
        self.assertEqual(result['candidates'], ['A'])

        # 只有 A 独自支持的整行被撤下；A 与他人共有的行只删 A 的来源。
        keys = [(row['tick'], row['resistor_id']) for row in result['rows']]
        self.assertEqual(keys, sorted(keys))
        self.assertNotIn((1, 'ac'), keys)
        self.assertNotIn((2, 'df'), keys)
        self.assertNotIn((3, 'gh'), keys)
        by_key = {(row['tick'], row['resistor_id']): row
                  for row in result['rows']}
        self.assertEqual(by_key[(3, 'ij')]['volts'], '1')
        self.assertEqual(by_key[(3, 'ij')]['sources'], [['B', 'b3']])
        self.assertEqual(by_key[(1, 'ab')]['sources'], [['B', 'b1']])
        self.assertEqual(by_key[(1, 'ab')]['volts'], '2')
        self.assertEqual(by_key[(1, 'bc')]['volts'], '3')
        # 保留行覆盖每个 tick 的其余观测，原值不变。
        self.assertEqual(set(by_key), {(1, 'ab'), (1, 'bc'), (2, 'de'),
                                       (2, 'ef'), (3, 'ij')})

        retests = {(item['tick'], item['resistor_id']): item
                   for item in result['retests']}
        self.assertEqual([(item['tick'], item['resistor_id'])
                          for item in result['retests']],
                         [(1, 'ac'), (2, 'df'), (3, 'gh')])
        self.assertEqual((retests[(1, 'ac')]['min_volts'],
                          retests[(1, 'ac')]['max_volts']), ('3', '7'))
        self.assertEqual((retests[(2, 'df')]['min_volts'],
                          retests[(2, 'df')]['max_volts']), ('5', '5'))
        self.assertEqual((retests[(3, 'gh')]['min_volts'],
                          retests[(3, 'gh')]['max_volts']),
                         (UNBOUNDED, UNBOUNDED))
        for item in result['retests']:
            self.assertIs(item['inferred'], True)
        self.assertEqual(retests[(1, 'ac')]['isolated_sources'], [['A', 'a1']])
        self.assertEqual(retests[(2, 'df')]['isolated_sources'], [['A', 'a2']])
        self.assertEqual(retests[(3, 'gh')]['isolated_sources'], [['A', 'a3']])
        # 补测区间独立标识，绝不回填成实测值。
        self.assertNotIn('volts', retests[(1, 'ac')])

    def test_new_source_on_second_conflict_removes_all_candidates(self):
        before = review(self.store, 'R', 1, [0, 20], ACCEPTANCE_WIDTHS)
        self.assertEqual(read_current(self.store, 'R')['version'], 1)

        # v2：新站 N 为 tick 2 的 df 增加一条同值来源；df 不再只由 A 支持。
        events_v2 = [(2, 'N', 'n1', 'N_df', 'df', '8')]
        endpoints = {rid: (a, b) for rid, a, b in ACCEPTANCE_PAIRS}
        publish(self.store, request(
            'R', 'r2', 1,
            make_station_readings(ACCEPTANCE_EVENTS + events_v2),
            make_registrations(ACCEPTANCE_EVENTS + events_v2, endpoints)))

        second = review(self.store, 'R', 2, [0, 20], ACCEPTANCE_WIDTHS)
        self.assertEqual(second['status'], STATUS_NO_CANDIDATE)
        self.assertEqual(second['candidates'], [])
        # 不交付任何行或补测区间。
        self.assertEqual(set(second), {'run_id', 'version', 'window',
                                       'status', 'candidates'})

        # 旧版复核结果逐字段不变，归档当前版未被复核推进。
        again = review(self.store, 'R', 1, [0, 20], ACCEPTANCE_WIDTHS)
        self.assertEqual(again, before)
        self.assertEqual(read_current(self.store, 'R')['version'], 2)


class SolverEdgesTests(unittest.TestCase):
    """反向端点、平行测点、闭区间相接、多路径收紧补测、排除非标准观测。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = self.temp.name

    def _publish(self, run, pairs, events, request_id='q1', base=0,
                 network_data=None, extra_readings=()):
        return publish_events(self.store, run, request_id, base, pairs, events,
                              network_data=network_data,
                              extra_readings=extra_readings)

    def test_reverse_endpoint_parallel_and_touching_closed_intervals(self):
        pairs = [('pq', 'p', 'q'), ('pq2', 'p', 'q'),
                 ('qr', 'q', 'r'), ('pr', 'p', 'r')]
        net = network(pairs)
        events = [
            # 正探针接 b：raw 2 -> V(p)-V(q) = -2。
            (1, 'Rb', 'x1', 'Rb_pq', 'pq', '2'),
            # 平行支路 p-q，正探针接 a：raw -2 -> V(p)-V(q) = -2，与上一致。
            (1, 'Pa', 'x2', 'Pa_pq2', 'pq2', '-2'),
            (1, 'Qa', 'x3', 'Qa_qr', 'qr', '3'),
            # 闭区间刚好相接：-2 + 3 = 1，长边 1 精确成立。
            (1, 'Wa', 'x4', 'Wa_pr', 'pr', '1'),
        ]
        # 正探针接 b：raw 2 -> V(p)-V(q) = -2；其余读数为正向。
        readings = make_station_readings(events)
        registrations = [
            registration('Rb', 'Rb_pq', 'pq', 'q'),
            registration('Pa', 'Pa_pq2', 'pq2', 'p'),
            registration('Qa', 'Qa_qr', 'qr', 'q'),
            registration('Wa', 'Wa_pr', 'pr', 'p'),
        ]
        # 干扰项：tick 2 的 pr 同键异值（冲突键不发布），以及两条未绑定读数。
        readings += [
            reading('Z1', 'z1', 'z1', 2, '7'),
            reading('Z2', 'z2', 'z2', 2, '8'),
        ]
        registrations += [
            registration('Z1', 'z1', 'pr', 'p'),
            registration('Z2', 'z2', 'pr', 'p'),
        ]
        # 完全未绑定的读数（通道无登记），窗口内外各一条。
        readings.append(reading('U', 'u1', 'nowhere', 2, '5'))
        readings.append(reading('U', 'u2', 'nowhere', 99, '5'))
        publish(self.store, request('rev', 'q1', 0, readings, registrations,
                                    network=net))

        widths = {'pq': '0', 'pq2': '0', 'qr': '0', 'pr': '0'}
        result = review(self.store, 'rev', 1, [0, 3], widths)
        self.assertEqual(result['status'], STATUS_AS_PUBLISHED)
        self.assertEqual(result['candidates'], [])
        by_key = {(row['tick'], row['resistor_id']): row
                  for row in result['rows']}
        self.assertEqual(set(by_key), {(1, 'pq'), (1, 'pq2'),
                                       (1, 'qr'), (1, 'pr')})
        self.assertEqual(by_key[(1, 'pq')]['volts'], '-2')
        self.assertEqual(by_key[(1, 'pq2')]['volts'], '-2')
        self.assertEqual(by_key[(1, 'pr')]['volts'], '1')
        # 冲突键与未绑定读数都未进入交付行。
        self.assertEqual(len(result['rows']), 4)

    def test_multiple_remaining_paths_tighten_retest_interval(self):
        pairs = [('ab', 'a', 'b'), ('bc', 'b', 'c'), ('ac', 'a', 'c'),
                 ('ad', 'a', 'd'), ('dc', 'd', 'c')]
        net = network(pairs)
        events = [
            (1, 'B', 'b1', 'B_ab', 'ab', '2'),
            (1, 'C', 'c1', 'C_bc', 'bc', '3'),
            (1, 'D', 'd1', 'D_ad', 'ad', '2'),
            (1, 'E', 'e1', 'E_dc', 'dc', '3'),
            (1, 'A', 'a1', 'A_ac', 'ac', '9'),
        ]
        publish_events(self.store, 'path', 'q1', 0, pairs, events,
                       network_data=net)
        widths = {'ab': '1', 'bc': '1', 'ac': '0',
                  'ad': '0.5', 'dc': '0.5'}
        result = review(self.store, 'path', 1, [0, 2], widths)
        self.assertEqual(result['status'], STATUS_ISOLATED)
        self.assertEqual(result['candidates'], ['A'])
        retest = result['retests'][0]
        self.assertEqual((retest['tick'], retest['resistor_id']), (1, 'ac'))
        # 经 b 的路径给出 [3,7]；经 d 的平行路径收紧为 [4,6]。
        self.assertEqual((retest['min_volts'], retest['max_volts']),
                         ('4', '6'))
        self.assertEqual(retest['isolated_sources'], [['A', 'a1']])

    def test_ambiguous_candidates_deliver_nothing(self):
        # 单三角形只有一条长边由 A 支持，短边各由 B、C：三者都是候选。
        pairs = [('ab', 'a', 'b'), ('bc', 'b', 'c'), ('ac', 'a', 'c')]
        net = network(pairs)
        events = [
            (1, 'B', 'b1', 'B_ab', 'ab', '2'),
            (1, 'C', 'c1', 'C_bc', 'bc', '3'),
            (1, 'A', 'a1', 'A_ac', 'ac', '9'),
        ]
        publish_events(self.store, 'amb', 'q1', 0, pairs, events,
                       network_data=net)
        result = review(self.store, 'amb', 1, [0, 2],
                        {'ab': '1', 'bc': '1', 'ac': '0'})
        self.assertEqual(result['status'], STATUS_AMBIGUOUS)
        self.assertEqual(result['candidates'], ['A', 'B', 'C'])
        self.assertEqual(set(result), {'run_id', 'version', 'window',
                                       'status', 'candidates'})

    def test_empty_window_is_consistent_and_requires_empty_widths(self):
        pairs = [('ab', 'a', 'b'), ('bc', 'b', 'c'), ('ac', 'a', 'c')]
        net = network(pairs)
        events = [(5, 'A', 'a1', 'A_ac', 'ac', '9')]
        publish_events(self.store, 'emp', 'q1', 0, pairs, events,
                       network_data=net)
        result = review(self.store, 'emp', 1, [100, 200], {})
        self.assertEqual(result['status'], STATUS_AS_PUBLISHED)
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['rows'], [])
        with self.assertRaises(ReviewInputError):
            review(self.store, 'emp', 1, [100, 200], {'ac': '0'})

    def test_bad_inputs_are_rejected(self):
        pairs = [('ab', 'a', 'b'), ('bc', 'b', 'c'), ('ac', 'a', 'c')]
        net = network(pairs)
        events = [(5, 'A', 'a1', 'A_ac', 'ac', '9')]
        publish_events(self.store, 'err', 'q1', 0, pairs, events,
                       network_data=net)
        good = {'ac': '0'}
        bad_cases = [
            (self.store, 'err', 1, [5, 6], {'ac': '-1'}),
            (self.store, 'err', 1, [5, 6], {'ac': '1.1234567'}),
            (self.store, 'err', 1, [5, 6], {'ac': 0}),
            (self.store, 'err', 1, [5, 6], {}),
            (self.store, 'err', 1, [5, 6], {'ac': '0', 'zz': '0'}),
            (self.store, 'err', 1, [5, 5], good),
            (self.store, 'err', 1, [6, 5], good),
            (self.store, 'err', 1, [0], good),
            (self.store, 'err', 0, [5, 6], good),
            (self.store, 'err', True, [5, 6], good),
            (self.store, 'err', 9, [5, 6], good),
            (self.store, 'missing', 1, [5, 6], good),
        ]
        for store, run, version, window, widths in bad_cases:
            with self.assertRaises(ReviewInputError):
                review(store, run, version, window, widths)
        # 合法调用仍可工作，且非法调用没有改动归档。
        self.assertEqual(
            review(self.store, 'err', 1, [5, 6], good)['status'],
            STATUS_AS_PUBLISHED)


def _benchmark_network():
    pairs = [('ab', 'a', 'b'), ('bc', 'b', 'c'), ('ac', 'a', 'c'),
             ('de', 'd', 'e'), ('ef', 'e', 'f'), ('df', 'd', 'f')]
    # 与三角形分离的连通分量：54 个节点的平行/树混合，194 条支路。
    onodes = ['o%d' % index for index in range(54)]
    for index in range(53):
        pairs.append(('p%d' % index, onodes[index], onodes[index + 1]))
    seed = 20240101
    for index in range(53, 194):
        seed = (1103515245 * seed + 12345) % (1 << 31)
        first = onodes[seed % 54]
        seed = (1103515245 * seed + 12345) % (1 << 31)
        second = onodes[seed % 54]
        if first == second:
            second = onodes[(onodes.index(second) + 1) % 54]
        pairs.append(('p%d' % index, first, second))
    return pairs


BENCHMARK_TICKS = 400
BENCHMARK_TRIANGLE = ['ab', 'bc', 'ac', 'de', 'ef', 'df']
BENCHMARK_CHAIN = ['p%d' % index for index in range(194)]
BENCHMARK_A_SOLE = ['ac', 'df']
BENCHMARK_STATION = {'ab': 'B', 'bc': 'C', 'ac': 'A',
                     'de': 'D', 'ef': 'E', 'df': 'A'}


def _station_for(resistor_id):
    if resistor_id in BENCHMARK_STATION:
        return BENCHMARK_STATION[resistor_id]
    return 'S%d' % int(resistor_id[1:])


def prepare_benchmark(store):
    """发布准备（不计时）：400 tick * 200 条标准观测的已备归档。

    为缩短准备时间，仅在准备阶段关闭 ``os.fsync``；落盘内容与正常发布
    逐字节相同，耐久性不属于本用例的计时对象。
    """
    pairs = _benchmark_network()
    net = network(pairs)
    potentials_by_tick = []
    seed = 7
    for tick in range(BENCHMARK_TICKS):
        potentials = {}
        for node in ['o%d' % index for index in range(54)]:
            seed = (1103515245 * seed + 12345) % (1 << 31)
            potentials[node] = seed % 9 - 4
        potentials_by_tick.append(potentials)
    endpoints = {rid: (a, b) for rid, a, b in pairs}

    events = []
    for tick in range(BENCHMARK_TICKS):
        for resistor_id in BENCHMARK_TRIANGLE:
            value = 0
            if tick == BENCHMARK_TICKS - 2 and resistor_id == 'ac':
                value = 1
            if tick == BENCHMARK_TICKS - 1 and resistor_id == 'df':
                value = 1
            events.append((tick, _station_for(resistor_id),
                           'r_%d_%s' % (tick, resistor_id),
                           '%s_%s' % (_station_for(resistor_id), resistor_id),
                           resistor_id, str(value)))
        potentials = potentials_by_tick[tick]
        for resistor_id in BENCHMARK_CHAIN:
            a, b = endpoints[resistor_id]
            value = potentials[a] - potentials[b]
            station = _station_for(resistor_id)
            events.append((tick, station, 'r_%d_%s' % (tick, resistor_id),
                           '%s_%s' % (station, resistor_id), resistor_id,
                           str(value)))

    readings = make_station_readings(events)
    registrations = make_registrations(events, endpoints)
    original = os.fsync
    os.fsync = lambda _fd: None
    try:
        publish(store, request('bench', 'prep', 0, readings, registrations,
                               network=net))
    finally:
        os.fsync = original
    widths = {rid: '0' for rid, _a, _b in pairs}
    return widths


class BenchmarkTests(unittest.TestCase):
    """400 tick * 200 观测；新进程读取已备归档并复核，CPU <= 25 秒。"""

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux only')
    def test_large_window_unique_candidate_and_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            store = os.path.join(temp, 'store')
            widths_path = os.path.join(temp, 'widths.json')
            script_path = os.path.join(temp, 'check.py')
            os.makedirs(store)
            widths = prepare_benchmark(store)
            self.assertEqual(len(widths), 200)
            with open(widths_path, 'w', encoding='utf-8') as handle:
                json.dump(widths, handle)
            source_root = str(Path(__file__).resolve().parents[1] / 'src')
            with open(script_path, 'w', encoding='utf-8') as handle:
                handle.write(CHECK_SCRIPT.replace('__SRC__', source_root))
            completed = subprocess.run(
                [sys.executable, '-I', script_path, store, widths_path],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(completed.returncode, 0,
                             completed.stderr or completed.stdout)
            summary = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertEqual(summary['status'], STATUS_ISOLATED)
            self.assertEqual(summary['candidates'], ['A'])
            self.assertEqual(summary['rows'], 79_200)
            self.assertEqual(summary['retests'], 800)
            self.assertLess(summary['cpu_seconds'], 25.0)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux only')
    def test_review_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            store = os.path.join(temp, 'store')
            os.makedirs(store)
            widths = prepare_benchmark(store)
            first = review(store, 'bench', 1, [0, BENCHMARK_TICKS], widths)
            second = review(store, 'bench', 1, [0, BENCHMARK_TICKS], widths)
            self.assertEqual(first, second)
            self.assertEqual(first['status'], STATUS_ISOLATED)
            self.assertEqual(first['candidates'], ['A'])
            self.assertEqual(len(first['rows']), 79_200)
            self.assertEqual(len(first['retests']), 800)
            self.assertTrue(all(item['inferred'] for item in
                                first['retests']))
            self.assertEqual(
                {(item['min_volts'], item['max_volts'])
                 for item in first['retests']}, {('0', '0')})
            self.assertEqual(read_current(store, 'bench')['version'], 1)


CHECK_SCRIPT = '''\
import json, sys, time
sys.path.insert(0, '__SRC__')
from resistor_flow.review import review

store, widths_path = sys.argv[1], sys.argv[2]
with open(widths_path, 'r', encoding='utf-8') as handle:
    widths = json.load(handle)
started = time.process_time()
result = review(store, 'bench', 1, [0, 400], widths)
elapsed = time.process_time() - started
assert result['status'] == 'isolated_single_station', result['status']
assert result['candidates'] == ['A'], result['candidates']
assert len(result['rows']) == 79200, len(result['rows'])
assert len(result['retests']) == 800, len(result['retests'])
# 保留行与补测项都按数值 tick、电阻 ID 升序。
row_keys = [(row['tick'], row['resistor_id']) for row in result['rows']]
assert row_keys == sorted(row_keys)
retest_keys = [(item['tick'], item['resistor_id'])
               for item in result['retests']]
assert retest_keys == sorted(retest_keys)
# 两条长边在每个 tick 都被撤下，区间恰好 [0,0]；含原本完全自洽的时刻。
assert retest_keys == [(tick, rid) for tick in range(400)
                       for rid in ('ac', 'df')], retest_keys[:4]
for item in result['retests']:
    assert item['inferred'] is True
    assert item['min_volts'] == '0' and item['max_volts'] == '0'
    assert [source[0] for source in item['isolated_sources']] == ['A']
# 保留行覆盖每个 tick 的其余 198 条观测，来源原样、数值精确。
by_tick = {}
for row in result['rows']:
    by_tick.setdefault(row['tick'], []).append(row)
assert sorted(by_tick) == list(range(400))
assert all(len(items) == 198 for items in by_tick.values())
for row in result['rows']:
    assert row['resistor_id'] not in ('ac', 'df'), row
    assert row['sources'], row
# 与三角形分离的分量在每个 tick 精确自洽，不产生补测。
assert all(item['resistor_id'] not in ('p0', 'p1')
           for item in result['retests'])
print(json.dumps({'status': result['status'],
                  'candidates': result['candidates'],
                  'rows': len(result['rows']),
                  'retests': len(result['retests']),
                  'cpu_seconds': round(elapsed, 3)}))
'''


if __name__ == '__main__':
    unittest.main()
