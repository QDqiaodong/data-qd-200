"""离线标定可行集、资格复验与盲测交付的验收测试。

覆盖任务书“自动验收”三条：

1. 小批数据上的资格、耦合差值区间、宽度边界、超覆盖检查，以及 H 参考上界
   收紧后“相交但不包含”导致 H 失格而 L 不变；逐项检查见证与请求。
2. 不拆组改变选组（A、B -> A、C）、单 x 覆盖不足、空拟合/矛盾拟合/线段/点
   可行集、错误参考表、合法但无法拆分清单、归档推进后按旧版重建。
3. 12,004 观测 / 36,000 请求的规模验收在独立进程、256 MiB 地址空间上限下
   运行 ``acceptance/scale_acceptance.py``，CPU <= 20 秒并逐项核对精确区间。
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from resistor_flow.calibration import (
    CalibrationInputError,
    FeasibleSpace,
    MAX_REQUESTS,
    _attach_rows,
    calibration_batch,
    parse_references,
    parse_requests,
)
from resistor_flow.datasets import DatasetInputError, HoldoutInfeasible, build_datasets
from resistor_flow.measurements import publish

NETWORK = {
    'nodes': [
        {'id': 'supply', 'voltage': '12'},
        {'id': 'middle', 'voltage': None},
        {'id': 'ground', 'voltage': '0'},
    ],
    'resistors': [
        {'id': 'r1', 'a': 'supply', 'b': 'middle', 'ohms': '2'},
    ],
}
RESISTOR = 'r1'


def publish_simple(store, run_id, tick_x, request_id='v1', base_version=0):
    """tick_x: 可迭代的 (tick, x)；每 tick 一条来源 a<tick>。"""
    readings = [{
        'station': 'S', 'row_id': 'a%d' % tick, 'channel': 'c',
        'tick': tick, 'raw_volts': str(x),
    } for tick, x in tick_x]
    request = {
        'run_id': run_id, 'request_id': request_id,
        'base_version': base_version, 'readings': readings,
        'registrations': [{
            'station': 'S', 'channel': 'c', 'start_tick': 0,
            'end_tick': 10_000_000, 'resistor_id': RESISTOR,
            'positive_node': 'supply', 'gain': '1',
            'offset_volts': '0',
        }],
    }
    if base_version == 0:
        request['network'] = NETWORK
    return publish(store, request)


def make_manifest(run_id, spec_by_tick, counts, version=1):
    """spec_by_tick: {tick: (specimen_id, condition)}。"""
    return {
        'versions': [{'run_id': run_id, 'version': version}],
        'specimens': [
            {'run_id': run_id, 'tick': tick, 'resistor_id': RESISTOR,
             'specimen_id': specimen, 'condition': condition}
            for tick, (specimen, condition) in sorted(spec_by_tick.items())
        ],
        'calibration_batches': [
            {'run_id': run_id, 'station': 'S', 'row_id': 'a%d' % tick,
             'batch_id': 'b%d' % tick}
            for tick in sorted(spec_by_tick)
        ],
        'holdout_counts': dict(counts),
    }


def refs_from(run_id, bounds_by_tick):
    return {
        (run_id, tick, RESISTOR): list(bounds)
        for tick, bounds in bounds_by_tick.items()
    }


def tree_digest(root):
    digest = hashlib.sha256()
    for base, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(base, name)
            digest.update(os.path.relpath(path, root).encode('utf-8'))
            with open(path, 'rb') as handle:
                digest.update(handle.read())
    return digest.hexdigest()


def by_request(result):
    return {item['request_id']: item for item in result['requests']}


# --------------------------------------------------------------------------
# 自动验收 1
# --------------------------------------------------------------------------

class AcceptanceOneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = self.temp.name
        self.run = 'acc1'
        # tick 1..4 = L x1,x3 / H x1,x3；tick5/6 为拟合行 x1,x3。
        self.tick_x = [(1, 1), (2, 3), (3, 1), (4, 3), (5, 1), (6, 3)]
        publish_simple(self.store, self.run, self.tick_x)
        self.spec = {
            1: ('wL1', 'L'), 2: ('wL3', 'L'),
            3: ('wH1', 'H'), 4: ('wH3', 'H'),
            5: ('f1', 'L'), 6: ('f3', 'H'),
        }
        self.manifest = make_manifest(
            self.run, self.spec, {'L': 2, 'H': 2})
        self.refs = refs_from(self.run, {
            1: ['0', '12'], 2: ['0', '12'],
            3: ['0', '12'], 4: ['0', '12'],
            5: ['2', '8'], 6: ['0', '12'],
        })
        self.requests = [
            {'request_id': 'diff1',
             'first': {'condition': 'L', 'x': '3'},
             'second': {'condition': 'H', 'x': '2'},
             'max_width': '1'},
            {'request_id': 'abs8',
             'reading': {'condition': 'L', 'x': '3'},
             'max_width': '8'},
            {'request_id': 'abs7',
             'reading': {'condition': 'L', 'x': '3'},
             'max_width': '7'},
            {'request_id': 'oob44',
             'first': {'condition': 'L', 'x': '4'},
             'second': {'condition': 'H', 'x': '4'},
             'max_width': '100'},
        ]

    def run_batch(self, refs=None, requests=None):
        return calibration_batch(
            self.store, self.manifest,
            self.refs if refs is None else refs,
            ['1', '2'], ['0', '10'],
            self.requests if requests is None else requests)

    def test_qualifications_and_delivery_numbers(self):
        result = self.run_batch()
        self.assertEqual(result['status'], 'feasible')
        self.assertTrue(result['feasible'])
        self.assertEqual(
            result['versions'], [{'run_id': self.run, 'version': 1}])

        for condition in ('L', 'H'):
            qual = result['qualifications'][condition]
            self.assertTrue(qual['issued'], condition)
            self.assertEqual(qual['coverage'], ['1', '3'])
            self.assertTrue(qual['reference_passed'])
            self.assertTrue(qual['coverage_sufficient'])
            self.assertEqual(
                [w['tick'] for w in qual['witnesses']],
                [1, 2] if condition == 'L' else [3, 4])

        # 逐见证极值：x=1 -> [2,8]，x=3 -> [4,12]，闭区间包含于 [0,12]。
        l_witness = result['qualifications']['L']['witnesses']
        self.assertEqual(
            [(w['min_value'], w['max_value'], w['passed'])
             for w in l_witness],
            [('2', '8', True), ('4', '12', True)])
        witness = l_witness[0]
        self.assertEqual(witness['key'], [self.run, 1, RESISTOR])
        self.assertEqual(witness['version'], 1)
        self.assertEqual(witness['reference'], ['0', '12'])

        items = by_request(result)
        diff = items['diff1']
        self.assertEqual(diff['status'], 'delivered')
        self.assertEqual(diff['range'], ['1', '2'])
        self.assertEqual(diff['width'], '1')

        abs8 = items['abs8']
        self.assertEqual(abs8['range'], ['4', '12'])
        self.assertEqual(abs8['width'], '8')

        abs7 = items['abs7']
        self.assertEqual(abs7['status'], 'insufficient_precision')
        self.assertEqual(abs7['status_text'], '精度不足')
        self.assertNotIn('lower', abs7)
        self.assertNotIn('upper', abs7)
        self.assertNotIn('range', abs7)
        self.assertNotIn('width', abs7)

        oob = items['oob44']
        self.assertEqual(oob['status'], 'out_of_coverage')
        self.assertEqual(oob['status_text'], '超出覆盖范围')
        self.assertNotIn('range', oob)

        # 请求按 ID 确定性排序。
        self.assertEqual(
            [item['request_id'] for item in result['requests']],
            sorted(item['request_id'] for item in self.requests))

    def test_h_upper_bound_11_loses_h_only(self):
        refs = dict(self.refs)
        refs[(self.run, 4, RESISTOR)] = ['0', '11']
        result = self.run_batch(refs=refs)

        h = result['qualifications']['H']
        l = result['qualifications']['L']
        self.assertFalse(h['issued'])
        self.assertTrue(h['reference_passed'] is False)
        self.assertTrue(h['coverage_sufficient'])
        self.assertEqual(h['failure_reasons'], ['reference_not_contained'])
        self.assertIsNone(h['coverage'])
        self.assertTrue(l['issued'])
        self.assertEqual(l['coverage'], ['1', '3'])

        bad_witness = next(w for w in h['witnesses'] if w['tick'] == 4)
        # 换算范围 [4,12] 与 [0,11] 仍相交，但不是包含，故不通过。
        self.assertEqual((bad_witness['min_value'],
                          bad_witness['max_value']), ('4', '12'))
        self.assertFalse(bad_witness['passed'])

        items = by_request(result)
        self.assertEqual(items['abs8']['range'], ['4', '12'])  # L 不变
        for request_id in ('diff1', 'oob44'):
            item = items[request_id]
            self.assertEqual(item['status'], 'condition_not_qualified')
            self.assertEqual(item['status_text'], '条件未签发')
            self.assertNotIn('range', item)
            self.assertNotIn('lower', item)

    def test_empty_requests_still_returns_qualifications(self):
        result = self.run_batch(requests=[])
        self.assertEqual(result['requests'], [])
        self.assertEqual(
            result['qualifications']['L']['coverage'], ['1', '3'])


# --------------------------------------------------------------------------
# 自动验收 2：不拆组的实际影响
# --------------------------------------------------------------------------

class GroupingAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = self.temp.name
        self.run = 'grp'
        # A..E = tick1..5 的 L，x=1,3,1,3,1；F/G = tick6/7 的 H x=1,3。
        self.tick_x = [(1, 1), (2, 3), (3, 1), (4, 3), (5, 1),
                       (6, 1), (7, 3)]
        publish_simple(self.store, self.run, self.tick_x)
        self.base_spec = {
            1: ('A', 'L'), 2: ('B', 'L'), 3: ('C', 'L'),
            4: ('D', 'L'), 5: ('E', 'L'),
            6: ('F', 'H'), 7: ('G', 'H'),
        }
        self.refs = refs_from(self.run, {
            1: ['0', '12'], 2: ['0', '12'], 3: ['2', '8'],
            4: ['0', '12'], 5: ['2', '8'],
            6: ['0', '12'], 7: ['0', '12'],
        })

    def _result(self, merge_c=False):
        spec = dict(self.base_spec)
        if merge_c:
            spec[3] = ('A', 'L')  # 只把 C 的样件改成与 A 相同。
        manifest = make_manifest(self.run, spec, {'L': 2, 'H': 2})
        return calibration_batch(
            self.store, manifest, self.refs, ['1', '2'], ['0', '10'], [])

    def test_independent_groups_select_a_b_and_issue(self):
        result = self._result(merge_c=False)
        l_witness = result['qualifications']['L']['witnesses']
        self.assertEqual([w['tick'] for w in l_witness], [1, 2])  # A,B
        self.assertTrue(result['qualifications']['L']['issued'])
        self.assertEqual(
            result['qualifications']['L']['coverage'], ['1', '3'])
        self.assertTrue(result['qualifications']['H']['issued'])

    def test_merging_c_with_a_selects_ac_and_coverage_shortage(self):
        self._result(merge_c=False)  # 基线选 A、B，见上一测试。
        after = self._result(merge_c=True)

        lq = after['qualifications']['L']
        self.assertEqual([w['tick'] for w in lq['witnesses']], [1, 3])  # A,C
        # 两个见证自身参考都通过（C 的范围恰为 [2,8]）。
        self.assertTrue(all(w['passed'] for w in lq['witnesses']))
        self.assertTrue(lq['reference_passed'])
        self.assertFalse(lq['coverage_sufficient'])
        self.assertFalse(lq['issued'])
        self.assertEqual(lq['failure_reasons'],
                         ['insufficient_distinct_coverage'])
        self.assertIsNone(lq['coverage'])
        # H 不受影响。
        self.assertTrue(after['qualifications']['H']['issued'])
        self.assertEqual(
            after['qualifications']['H']['coverage'], ['1', '3'])

        # 拟合可行集合不变：对一批 x，前后绝对极值完全相同。
        independent_manifest = make_manifest(
            self.run, self.base_spec, {'L': 2, 'H': 2})
        merged_spec = dict(self.base_spec)
        merged_spec[3] = ('A', 'L')
        merged_manifest = make_manifest(
            self.run, merged_spec, {'L': 2, 'H': 2})
        space_before = _build_space(self.store, independent_manifest,
                                    self.refs)
        space_after = _build_space(self.store, merged_manifest, self.refs)
        for x in range(-3, 9):
            x = Fraction(x)
            self.assertEqual(space_before.absolute_range(x),
                             space_after.absolute_range(x))

    def test_legal_but_infeasible_quota_still_holdout_infeasible(self):
        # 把 L 行绑成 3+2 两个组，目标 L=1 无法精确铺满。
        spec = {
            1: ('G3', 'L'), 3: ('G3', 'L'), 5: ('G3', 'L'),
            2: ('G2', 'L'), 4: ('G2', 'L'),
            6: ('F', 'H'), 7: ('G', 'H'),
        }
        manifest = make_manifest(self.run, spec, {'L': 1, 'H': 2})
        with self.assertRaises(HoldoutInfeasible):
            calibration_batch(
                self.store, manifest, self.refs,
                ['1', '2'], ['0', '10'], [])

    def test_archive_advance_rebuilds_same_historical_version(self):
        manifest = make_manifest(
            self.run, self.base_spec, {'L': 2, 'H': 2}, version=1)
        first = calibration_batch(
            self.store, manifest, self.refs, ['1', '2'], ['0', '10'], [])

        # 归档另行推进到 v2（新增一行），旧版清单仍指向 v1。
        publish_simple(
            self.store, self.run, [(8, 9)], request_id='v2',
            base_version=1)
        with open(os.path.join(self.store, self.run, 'current.json'),
                  encoding='utf-8') as handle:
            self.assertEqual(json.load(handle)['version'], 2)

        digest_after_publish = tree_digest(self.store)
        second = calibration_batch(
            self.store, manifest, self.refs, ['1', '2'], ['0', '10'], [])
        # 按相同历史版本重建，结果逐字段一致，且批处理本身不写归档。
        self.assertEqual(second, first)
        self.assertEqual(tree_digest(self.store), digest_after_publish)


def _build_space(store, manifest, refs):
    """按与公共接口相同的步骤重建拟合可行集（测试等价性核对用）。"""
    parsed_refs = parse_references(refs)
    dataset = build_datasets(store, manifest)
    fit_rows, _holdout, actual_keys = _attach_rows(dataset, parsed_refs)
    assert set(parsed_refs) == actual_keys
    return FeasibleSpace(
        fit_rows, parsed_refs,
        (Fraction(1), Fraction(2)), (Fraction(0), Fraction(10)))


# --------------------------------------------------------------------------
# 空拟合 / 矛盾拟合 / 线段 / 点
# --------------------------------------------------------------------------

class BatchStateAndGeometryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = self.temp.name

    def _tiny(self, run_id, tick_x, spec, counts, refs):
        publish_simple(self.store, run_id, tick_x)
        manifest = make_manifest(run_id, spec, counts)
        return calibration_batch(
            self.store, manifest, refs, ['1', '2'], ['0', '10'],
            [{'request_id': 'q',
              'reading': {'condition': 'L', 'x': '1'},
              'max_width': '100'}])

    def test_empty_fit_is_distinct_state(self):
        run = 'empty'
        result = self._tiny(
            run, [(1, 1), (2, 3)],
            {1: ('a', 'L'), 2: ('b', 'H')},
            {'L': 1, 'H': 1},
            refs_from(run, {1: ['0', '12'], 2: ['0', '12']}))
        self.assertEqual(result['status'], 'fit_empty')
        self.assertNotEqual(result['status'], 'fit_infeasible')
        self.assertFalse(result['feasible'])
        for condition in ('L', 'H'):
            qual = result['qualifications'][condition]
            self.assertFalse(qual['issued'])
            self.assertEqual(qual['failure_reasons'], ['fit_empty'])
        self.assertEqual(
            result['items'][0]['status'], 'condition_not_qualified')

    def test_contradictory_fit_is_other_state(self):
        run = 'contradiction'
        # tick3 拟合 x=1 要求 [20,30]，但 g<=2,b<=10 使 x=1 时至多 12。
        result = self._tiny(
            run, [(1, 1), (2, 1), (3, 1)],
            {1: ('a', 'L'), 2: ('b', 'H'), 3: ('c', 'L')},
            {'L': 1, 'H': 1},
            refs_from(run, {
                1: ['0', '12'], 2: ['0', '12'], 3: ['20', '30']}))
        self.assertEqual(result['status'], 'fit_infeasible')
        for condition in ('L', 'H'):
            self.assertFalse(
                result['qualifications'][condition]['issued'])
            self.assertEqual(
                result['qualifications'][condition]['failure_reasons'],
                ['fit_infeasible'])

    def test_segment_feasible_set_end_to_end(self):
        run = 'segment'
        # tick5 拟合 x=1 参考 [2,2] => g+b=2，在矩形内是线段。
        tick_x = [(1, 1), (2, 2), (3, 1), (4, 2), (5, 1)]
        publish_simple(self.store, run, tick_x)
        spec = {
            1: ('l1', 'L'), 2: ('l2', 'L'),
            3: ('h1', 'H'), 4: ('h2', 'H'), 5: ('f', 'L'),
        }
        refs = refs_from(run, {
            1: ['2', '2'], 2: ['3', '4'],
            3: ['0', '100'], 4: ['0', '100'], 5: ['2', '2'],
        })
        manifest = make_manifest(run, spec, {'L': 2, 'H': 2})
        requests = [
            {'request_id': 'exact',
             'reading': {'condition': 'L', 'x': '1'}, 'max_width': '0'},
            {'request_id': 'x2ok',
             'reading': {'condition': 'L', 'x': '2'}, 'max_width': '1'},
            {'request_id': 'x2tight',
             'reading': {'condition': 'L', 'x': '2'}, 'max_width': '0'},
        ]
        result = calibration_batch(
            self.store, manifest, refs, ['1', '2'], ['0', '10'], requests)
        self.assertEqual(result['status'], 'feasible')
        self.assertTrue(result['qualifications']['L']['issued'])
        items = by_request(result)
        self.assertEqual(items['exact']['range'], ['2', '2'])
        self.assertEqual(items['x2ok']['range'], ['3', '4'])
        self.assertEqual(
            items['x2tight']['status'], 'insufficient_precision')

    def test_reference_failure_and_coverage_shortage_can_coexist(self):
        run = 'bothfail'
        tick_x = [(1, 1), (2, 1), (3, 1), (4, 3), (5, 1)]
        publish_simple(self.store, run, tick_x)
        spec = {
            1: ('l1', 'L'), 2: ('l2', 'L'),
            3: ('h1', 'H'), 4: ('h2', 'H'), 5: ('f', 'L'),
        }
        refs = refs_from(run, {
            1: ['0', '12'],
            2: ['0', '1'],     # x=1 换算 [2,8]，不被包含 -> 参考失败
            3: ['0', '12'], 4: ['0', '12'], 5: ['2', '8'],
        })
        manifest = make_manifest(run, spec, {'L': 2, 'H': 2})
        result = calibration_batch(
            self.store, manifest, refs, ['1', '2'], ['0', '10'], [])
        lq = result['qualifications']['L']
        self.assertFalse(lq['reference_passed'])
        self.assertFalse(lq['coverage_sufficient'])
        self.assertFalse(lq['issued'])
        self.assertEqual(
            set(lq['failure_reasons']),
            {'reference_not_contained',
             'insufficient_distinct_coverage'})
        # H 仍然签发，不受 L 影响。
        self.assertTrue(result['qualifications']['H']['issued'])

    def test_geometry_segment_point_vertical(self):
        one = Fraction(1)
        # 线段 g+b=2, g∈[1,2]。
        segment = FeasibleSpace(
            [{'x': one, 'ref_low': Fraction(2), 'ref_high': Fraction(2)}],
            None, (one, Fraction(2)), (Fraction(0), Fraction(10)))
        self.assertFalse(segment.empty)
        self.assertEqual((segment.g_min, segment.g_max), (one, Fraction(2)))
        self.assertEqual(segment.absolute_range(Fraction(3)),
                         (Fraction(4), Fraction(6)))

        # 点：g+b=2 与 2g+b=3 => (g,b)=(1,1)。
        point = FeasibleSpace([
            {'x': one, 'ref_low': Fraction(2), 'ref_high': Fraction(2)},
            {'x': Fraction(2), 'ref_low': Fraction(3),
             'ref_high': Fraction(3)},
        ], None, (Fraction('0.5'), Fraction(2)),
            (Fraction(0), Fraction(5)))
        self.assertEqual(point.g_min, point.g_max)
        self.assertEqual(point.absolute_range(Fraction(7)),
                         (Fraction(8), Fraction(8)))

        # 竖直线段：g 固定为 1，b∈[0,2]。
        vertical = FeasibleSpace(
            [], None, (one, one), (Fraction(0), Fraction(2)))
        self.assertEqual(vertical.absolute_range(Fraction(3)),
                         (Fraction(3), Fraction(5)))
        self.assertEqual(
            vertical.difference_range(Fraction(5), Fraction(2)),
            (Fraction(3), Fraction(3)))


# --------------------------------------------------------------------------
# 输入校验 / 确定性 / 调用形式
# --------------------------------------------------------------------------

class InputAndDeterminismTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = self.temp.name
        self.run = 'in'
        self.tick_x = [(1, 1), (2, 3), (3, 1), (4, 3), (5, 1), (6, 3)]
        publish_simple(self.store, self.run, self.tick_x)
        self.spec = {
            1: ('wL1', 'L'), 2: ('wL3', 'L'),
            3: ('wH1', 'H'), 4: ('wH3', 'H'),
            5: ('f1', 'L'), 6: ('f3', 'H'),
        }
        self.manifest = make_manifest(
            self.run, self.spec, {'L': 2, 'H': 2})
        self.refs = refs_from(self.run, {
            1: ['0', '12'], 2: ['0', '12'],
            3: ['0', '12'], 4: ['0', '12'],
            5: ['2', '8'], 6: ['0', '12'],
        })
        self.kwargs = dict(
            gain_range=['1', '2'], offset_range=['0', '10'], requests=[])

    def _call(self, **overrides):
        params = dict(
            store_dir=self.store, manifest=self.manifest,
            references=self.refs)
        params.update(self.kwargs)
        params.update(overrides)
        return calibration_batch(**params)

    def test_bad_requests_are_input_errors(self):
        bad_batches = [
            [{'reading': {'condition': 'L', 'x': '3'},
              'max_width': '1'}],  # 缺 request_id
            [{'request_id': 'a b',
              'reading': {'condition': 'L', 'x': '3'},
              'max_width': '1'}],  # 非法 id
            [{'request_id': 'z',
              'reading': {'condition': 'L', 'x': '3'},
              'max_width': '1'},
             {'request_id': 'z',
              'reading': {'condition': 'H', 'x': '1'},
              'max_width': '0'}],  # 重复 id
            [{'request_id': 'z',
              'reading': {'condition': 'X', 'x': '3'},
              'max_width': '1'}],  # 非法 condition
            [{'request_id': 'z',
              'reading': {'condition': 'L', 'x': '3.1415926'},
              'max_width': '1'}],  # 超过 6 位小数
            [{'request_id': 'z',
              'reading': {'condition': 'L', 'x': 3},
              'max_width': '1'}],  # 非字符串
            [{'request_id': 'z',
              'reading': {'condition': 'L', 'x': '3'},
              'max_width': '-1'}],  # 负宽度
            [{'request_id': 'z',
              'first': {'condition': 'L', 'x': '1'},
              'max_width': '1'}],  # 差值缺 second
        ]
        for batch in bad_batches:
            with self.assertRaises(CalibrationInputError):
                self._call(requests=batch)

    def test_request_count_limit(self):
        with self.assertRaises(CalibrationInputError):
            parse_requests([
                {'request_id': 'q%06d' % i,
                 'reading': {'condition': 'L', 'x': '1'},
                 'max_width': '0'}
                for i in range(MAX_REQUESTS + 1)])
        # 边界 36000 能通过解析。
        parsed = parse_requests([
            {'request_id': 'q%06d' % i,
             'reading': {'condition': 'L', 'x': '1'},
             'max_width': '0'}
            for i in range(MAX_REQUESTS)])
        self.assertEqual(len(parsed), MAX_REQUESTS)

    def test_bad_ranges_are_input_errors(self):
        for gain in (['0', '2'], ['-1', '2'], ['3', '2'], ['1', 'nope']):
            with self.assertRaises(CalibrationInputError):
                self._call(gain_range=gain)
        with self.assertRaises(CalibrationInputError):
            self._call(offset_range=['9', '1'])

    def test_reference_coverage_and_values(self):
        missing = dict(self.refs)
        missing.pop((self.run, 1, RESISTOR))
        with self.assertRaises(CalibrationInputError):
            self._call(references=missing)

        extra = dict(self.refs)
        extra[(self.run, 999, RESISTOR)] = ['0', '1']
        with self.assertRaises(CalibrationInputError):
            self._call(references=extra)

        bad_bounds = dict(self.refs)
        bad_bounds[(self.run, 1, RESISTOR)] = ['9', '1']
        with self.assertRaises(CalibrationInputError):
            self._call(references=bad_bounds)

        with self.assertRaises(CalibrationInputError):
            calibration_batch(
                self.store, self.manifest, None,
                ['1', '2'], ['0', '10'], [])

    def test_duplicate_reference_key_rejected(self):
        refs_list = [
            {'run_id': self.run, 'tick': tick, 'resistor_id': RESISTOR,
             'lower': bounds[0], 'upper': bounds[1]}
            for tick, bounds in (
                (1, ['0', '12']), (1, ['0', '12']))]
        with self.assertRaises(CalibrationInputError):
            from resistor_flow.calibration import parse_references
            parse_references(refs_list)

    def test_unknown_version_is_dataset_input_error(self):
        manifest = make_manifest(
            self.run, self.spec, {'L': 2, 'H': 2}, version=41)
        with self.assertRaises(CalibrationInputError) as caught:
            calibration_batch(
                self.store, manifest, self.refs,
                ['1', '2'], ['0', '10'], [])
        self.assertIsInstance(caught.exception, DatasetInputError)

    def test_invalid_manifest_is_input_error(self):
        bad = dict(self.manifest)
        bad['holdout_counts'] = {'L': True, 'H': 2}
        with self.assertRaises(CalibrationInputError):
            calibration_batch(
                self.store, bad, self.refs, ['1', '2'], ['0', '10'], [])

    def test_request_input_error_precedes_infeasible_manifest(self):
        spec = {
            1: ('G3', 'L'), 3: ('G3', 'L'), 5: ('G3', 'L'),
            2: ('G2', 'L'), 4: ('G2', 'L'),
            6: ('F', 'H'), 7: ('G', 'H'),
        }
        infeasible = make_manifest(
            self.run, spec, {'L': 1, 'H': 2})
        bad_requests = [
            {'request_id': 'dup',
             'reading': {'condition': 'L', 'x': '1'}, 'max_width': '0'},
            {'request_id': 'dup',
             'reading': {'condition': 'H', 'x': '1'}, 'max_width': '0'},
        ]
        with self.assertRaises(CalibrationInputError):
            calibration_batch(
                self.store, infeasible, self.refs,
                ['1', '2'], ['0', '10'], bad_requests)

    def test_reordering_materials_does_not_change_result(self):
        requests = [
            {'request_id': 'q%d' % i,
             'first': {'condition': 'L', 'x': str((i % 4) + 1)},
             'second': {'condition': 'H', 'x': str((i % 3) + 1)},
             'max_width': str(i % 5)}
            for i in range(20)]
        baseline = calibration_batch(
            self.store, self.manifest, self.refs,
            ['1', '2'], ['0', '10'], requests)

        shuffled_manifest = json.loads(json.dumps(self.manifest))
        shuffled_manifest['specimens'].reverse()
        shuffled_manifest['calibration_batches'].reverse()
        refs_list = [{
            'run_id': k[0], 'tick': k[1], 'resistor_id': k[2],
            'lower': v[0], 'upper': v[1]} for k, v in self.refs.items()]
        refs_list.reverse()
        shuffled_requests = list(reversed(requests))
        reordered = calibration_batch(
            self.store, shuffled_manifest, refs_list,
            ['1', '2'], ['0', '10'], shuffled_requests)

        canonical = lambda value: json.dumps(
            value, sort_keys=True, ensure_ascii=False)
        self.assertEqual(canonical(reordered), canonical(baseline))

    def test_payload_and_alias_entrypoints(self):
        from resistor_flow.qualification import qualify
        payload = {
            'manifest': self.manifest,
            'references': self.refs,
            'gain_range': ['1', '2'],
            'offset_range': ['0', '10'],
            'requests': [],
        }
        via_payload = calibration_batch(self.store, payload)
        via_alias = qualify(
            self.store, self.manifest, self.refs,
            g_range=['1', '2'], b_range=['0', '10'])
        self.assertEqual(
            via_payload['qualifications']['L']['coverage'], ['1', '3'])
        self.assertEqual(
            via_alias['qualifications']['H']['coverage'], ['1', '3'])


# --------------------------------------------------------------------------
# 自动验收 3：规模 / CPU / 地址空间
# --------------------------------------------------------------------------

class ScaleAcceptanceTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith('linux'),
                         'Linux address-space/CPU acceptance only')
    def test_12004_rows_36000_requests_under_limits(self):
        script = (Path(__file__).resolve().parents[1]
                  / 'acceptance' / 'scale_acceptance.py')
        self.assertTrue(script.is_file(), script)
        with tempfile.TemporaryDirectory() as root:
            completed = subprocess.run(
                [sys.executable, '-I', str(script), 'all', root],
                capture_output=True, text=True, timeout=300)
            if completed.returncode != 0:
                self.fail('scale acceptance failed:\nSTDOUT:%s\nSTDERR:%s'
                          % (completed.stdout, completed.stderr))
            line = completed.stdout.strip().splitlines()[-1]
            summary = json.loads(line)
            self.assertEqual(summary['mismatches'], [])
            self.assertEqual(summary['request_count'], 36_000)
            self.assertEqual(summary['status'], 'feasible')
            self.assertLessEqual(summary['cpu_seconds'], 20.0)
            self.assertLessEqual(summary['maxrss_kib'], 262_144)
            self.assertTrue(summary['passed'], summary)


if __name__ == '__main__':
    unittest.main()
