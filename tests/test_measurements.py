import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from resistor_flow import measurements as m
from resistor_flow.measurements import (
    MeasurementError,
    publish,
    read_current,
    read_evidence,
    read_version,
)

# 与 fixtures/input.json 同构的两电阻网络：r1 a=supply b=middle；r2 a=middle b=ground。
NETWORK = {
    'nodes': [
        {'id': 'supply', 'voltage': '12'},
        {'id': 'middle', 'voltage': None},
        {'id': 'ground', 'voltage': '0'},
    ],
    'resistors': [
        {'id': 'r1', 'a': 'supply', 'b': 'middle', 'ohms': '2'},
        {'id': 'r2', 'a': 'middle', 'b': 'ground', 'ohms': '4'},
    ],
}


def reading(station, row_id, channel, tick, raw_volts):
    return {'station': station, 'row_id': row_id, 'channel': channel,
            'tick': tick, 'raw_volts': raw_volts}


def registration(station, channel, start, end, resistor, node,
                 gain='1', offset='0'):
    return {'station': station, 'channel': channel, 'start_tick': start,
            'end_tick': end, 'resistor_id': resistor,
            'positive_node': node, 'gain': gain, 'offset_volts': offset}


_NO_NETWORK = object()


def request(run_id, request_id, base_version, readings, registrations,
            network=_NO_NETWORK):
    payload = {'run_id': run_id, 'request_id': request_id,
               'base_version': base_version, 'readings': readings,
               'registrations': registrations}
    if network is not _NO_NETWORK:
        payload['network'] = network
    return payload


def row_map(snapshot):
    return {(row['tick'], row['resistor_id']): row
            for row in snapshot['observations']}


def _boom(_point):
    raise RuntimeError('simulated crash')


def conflict_keys(snapshot):
    return {(row['tick'], row['resistor_id'])
            for row in snapshot['conflicts']}


def apply_changeset(consumer_table, changeset):
    for key in changeset['removed']:
        del consumer_table[tuple(key)]
    for row in changeset['upserted']:
        consumer_table[(row['tick'], row['resistor_id'])] = row
    return consumer_table


class MeasurementTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = self.temp.name

    # -- 验收主链：首版 -> 跨键撤回/冲突 -> 恢复并补入 ---------------------

    def test_acceptance_chain_correction_conflict_recovery(self):
        run = 'run-main'
        v1_req = request(
            run, 'req-1', 0,
            [reading('A', 'rowA', 'cA', 7, '4'),
             reading('B', 'rowB', 'cB', 7, '2')],
            [registration('A', 'cA', 0, 10_000_000, 'r1', 'supply'),
             registration('B', 'cB', 0, 10_000_000, 'r2', 'middle')],
            network=NETWORK)
        v1 = publish(self.store, v1_req)
        self.assertEqual(v1['version'], 1)
        rows = row_map(v1)
        self.assertEqual(set(rows), {(7, 'r1'), (7, 'r2')})
        self.assertEqual(rows[(7, 'r1')]['volts'], '4')
        self.assertEqual(rows[(7, 'r2')]['volts'], '2')
        self.assertEqual(rows[(7, 'r1')]['sources'], [['A', 'rowA']])
        self.assertEqual(rows[(7, 'r2')]['sources'], [['B', 'rowB']])
        self.assertEqual(v1['conflicts'], [])
        self.assertEqual(v1['changeset']['from_version'], 0)
        self.assertEqual(v1['changeset']['to_version'], 1)
        self.assertEqual(len(v1['changeset']['upserted']), 2)
        self.assertEqual(v1['changeset']['removed'], [])

        # v2：不送新读数，B 改绑 r1 反向 -> B=-2，r1 整键冲突；r2 撤下。
        v2_regs = [
            registration('A', 'cA', 0, 10_000_000, 'r1', 'supply'),
            registration('B', 'cB', 0, 10_000_000, 'r1', 'middle'),
        ]
        v2 = publish(self.store, request(run, 'req-2', 1, [], v2_regs))
        self.assertEqual(v2['version'], 2)
        self.assertEqual(row_map(v2), {})  # r1 冲突不发布，r2 被撤下
        self.assertEqual(conflict_keys(v2), {(7, 'r1')})
        conflict = v2['conflicts'][0]
        self.assertEqual(conflict['sources'],
                         [['A', 'rowA'], ['B', 'rowB']])
        removed = {tuple(key) for key in v2['changeset']['removed']}
        self.assertEqual(removed, {(7, 'r1'), (7, 'r2')})
        self.assertEqual(v2['changeset']['upserted'], [])

        # 逐身份证据：A、B 都是 conflict，B 记录了反号后的换算值与所用登记。
        ev_a = read_evidence(self.store, run, 'A', 'rowA', version=2)
        ev_b = read_evidence(self.store, run, 'B', 'rowB', version=2)
        self.assertEqual(ev_a['status'], 'conflict')
        self.assertEqual(ev_b['status'], 'conflict')
        self.assertEqual(ev_b['converted_volts'], '-2')
        self.assertEqual(ev_b['registration']['resistor_id'], 'r1')
        self.assertEqual(ev_b['observation_key'], [7, 'r1'])

        # v3：B 的偏置更正为 -6 -> B: 2-6=-4，反号后 +4，与 A 的 4 一致。
        v3_regs = [
            registration('A', 'cA', 0, 10_000_000, 'r1', 'supply'),
            registration('B', 'cB', 0, 10_000_000, 'r1', 'middle',
                         offset='-6'),
        ]
        v3 = publish(self.store, request(run, 'req-3', 2, [], v3_regs))
        self.assertEqual(v3['version'], 3)
        rows3 = row_map(v3)
        self.assertEqual(set(rows3), {(7, 'r1')})
        self.assertEqual(rows3[(7, 'r1')]['volts'], '4')
        self.assertEqual(rows3[(7, 'r1')]['sources'],
                         [['A', 'rowA'], ['B', 'rowB']])
        self.assertEqual(v3['conflicts'], [])
        self.assertEqual(v3['changeset']['removed'], [])
        self.assertEqual(len(v3['changeset']['upserted']), 1)
        self.assertEqual(v3['changeset']['upserted'][0]['resistor_id'], 'r1')

        # 消费者只保存观测表，逐版应用变更集应与各版快照逐行一致。
        consumer = {}
        for version in (1, 2, 3):
            snap = read_version(self.store, run, version)
            apply_changeset(consumer, snap['changeset'])
            self.assertEqual(consumer, row_map(snap))

        # 重新打开（新对象、只给目录）：首版仍是两条原观测与原证据。
        reopened = read_version(self.store, run, 1)
        self.assertEqual(set(row_map(reopened)), {(7, 'r1'), (7, 'r2')})
        ev_b_v1 = read_evidence(self.store, run, 'B', 'rowB', version=1)
        self.assertEqual(ev_b_v1['status'], 'accepted')
        self.assertEqual(ev_b_v1['converted_volts'], '2')
        self.assertEqual(ev_b_v1['registration']['resistor_id'], 'r2')
        self.assertEqual(read_current(self.store, run)['version'], 3)

    # -- 只有来源集合变化的更新 ------------------------------------------

    def test_source_set_only_change_is_upsert(self):
        run = 'run-sources'
        regs = [registration('A', 'cA', 0, 10_000_000, 'r1', 'supply')]
        publish(self.store, request(
            run, 's1', 0, [reading('A', 'a1', 'cA', 3, '5')],
            regs, network=NETWORK))
        # 新增同值的第二来源；观测值不变，但来源集合变化必须作为 upsert 交付。
        v2 = publish(self.store, request(
            run, 's2', 1, [reading('A', 'a2', 'cA', 3, '5')], regs))
        self.assertEqual(len(v2['changeset']['upserted']), 1)
        self.assertEqual(v2['changeset']['removed'], [])
        row = v2['changeset']['upserted'][0]
        self.assertEqual(row['volts'], '5')
        self.assertEqual(row['sources'], [['A', 'a1'], ['A', 'a2']])

    # -- 区间相接、缺失与左闭右开边界触发点 -------------------------------

    def test_interval_touch_gap_and_half_open_boundaries(self):
        run = 'run-intervals'
        # 同一站/通道：[0,5) 与 [5,10) 相接；[10,20) 缺失后再开 [20,30)。
        regs = [
            registration('B', 'c', 0, 5, 'r1', 'supply'),
            registration('B', 'c', 5, 10, 'r2', 'middle'),
            registration('B', 'c', 20, 30, 'r1', 'supply'),
        ]
        readings = [
            reading('B', 't4', 'c', 4, '1'),    # 落 [0,5)
            reading('B', 't5', 'c', 5, '2'),    # 左闭：落 [5,10)
            reading('B', 't9', 'c', 9, '3'),    # 右开：仍落 [5,10)
            reading('B', 't10', 'c', 10, '4'),  # 缺失区间：未绑定
            reading('B', 't20', 'c', 20, '5'),  # 落 [20,30)
            reading('B', 't29', 'c', 29, '6'),
            reading('B', 't30', 'c', 30, '7'),  # 右端点开区间外：未绑定
        ]
        snap = publish(self.store, request(
            run, 'i1', 0, readings, regs, network=NETWORK))
        rows = row_map(snap)
        self.assertEqual(rows[(4, 'r1')]['volts'], '1')
        self.assertEqual(rows[(5, 'r2')]['volts'], '2')
        self.assertEqual(rows[(9, 'r2')]['volts'], '3')
        self.assertEqual(rows[(20, 'r1')]['volts'], '5')
        self.assertEqual(rows[(29, 'r1')]['volts'], '6')
        self.assertNotIn((10, 'r1'), rows)
        self.assertNotIn((30, 'r1'), rows)
        unbound = [e['identity'] for e in snap['evidence']
                   if e['status'] == 'unbound']
        self.assertEqual(unbound, [['B', 't10'], ['B', 't30']])

    # -- 精确有理数换算（含增益、偏置、反号与分数形式） -------------------

    def test_exact_rational_conversion_and_polarity(self):
        run = 'run-rational'
        regs = [
            registration('P', 'c', 0, 100, 'r1', 'middle',
                         gain='1.5', offset='0.25'),  # 正探针接 b -> 反号
        ]
        snap = publish(self.store, request(
            run, 'q1', 0, [reading('P', 'p1', 'c', 1, '2')],
            regs, network=NETWORK))
        # 2*1.5+0.25 = 3.25 = 13/4，反号 -> -13/4
        self.assertEqual(row_map(snap)[(1, 'r1')]['volts'], '-13/4')
        # 再发一条同键不同换算值 -> 整键冲突，不发布。
        snap2 = publish(self.store, request(
            run, 'q2', 1, [reading('P', 'p2', 'c', 1, '1')], regs))
        self.assertEqual(row_map(snap2), {})
        self.assertEqual(conflict_keys(snap2), {(1, 'r1')})

    # -- 空变更也保留版本证据；版本 0 基线；未知版本报错 ------------------

    def test_empty_change_keeps_version_and_unknown_version_errors(self):
        run = 'run-empty'
        regs = [registration('A', 'cA', 0, 10_000_000, 'r1', 'supply')]
        v1 = publish(self.store, request(
            run, 'e1', 0, [reading('A', 'a1', 'cA', 1, '4')],
            regs, network=NETWORK))
        v2 = publish(self.store, request(run, 'e2', 1, [], regs))
        self.assertEqual(v2['version'], 2)
        self.assertEqual(v2['changeset']['removed'], [])
        self.assertEqual(v2['changeset']['upserted'], [])
        self.assertEqual(row_map(v2), row_map(v1))
        zero = read_version(self.store, run, 0)
        self.assertEqual(zero['version'], 0)
        self.assertEqual(zero['observations'], [])
        self.assertIsNone(zero['changeset'])
        with self.assertRaises(MeasurementError):
            read_version(self.store, run, 3)
        with self.assertRaises(MeasurementError):
            read_version(self.store, run, -1)

    # -- 幂等重放：同请求内容回原版号与原变更集，旧底版也重放 -------------

    def test_request_replay_returns_original_version(self):
        run = 'run-replay'
        req1 = request(
            run, 'id-1', 0, [reading('A', 'a1', 'cA', 1, '4')],
            [registration('A', 'cA', 0, 10_000_000, 'r1', 'supply')],
            network=NETWORK)
        first = publish(self.store, req1)
        publish(self.store, request(
            run, 'id-2', 1, [],
            [registration('A', 'cA', 0, 10_000_000, 'r2', 'middle')]))
        self.assertEqual(read_current(self.store, run)['version'], 2)
        # 字典键序不同不影响身份；重放仍返回 v1 的原结果（含 r1 非 r2）。
        shuffled = dict(reversed(list(req1.items())))
        replay = publish(self.store, shuffled)
        self.assertEqual(replay['version'], 1)
        self.assertEqual(replay['changeset'], first['changeset'])
        self.assertEqual(set(row_map(replay)), {(1, 'r1')})
        self.assertEqual(read_current(self.store, run)['version'], 2)
        # 同 ID 改内容必须报错。
        changed = dict(req1)
        changed['base_version'] = 1
        with self.assertRaises(MeasurementError):
            publish(self.store, changed)
        # 数值字符串原文参与身份："4" 与 "4.0" 视为不同内容。
        changed2 = json.loads(json.dumps(req1))
        changed2['readings'][0]['raw_volts'] = '4.0'
        with self.assertRaises(MeasurementError):
            publish(self.store, changed2)
        # 数组顺序参与身份：换序后同 ID 报错。
        changed3 = json.loads(json.dumps(req1))
        changed3['readings'] = list(reversed(changed3['readings']))
        changed3['readings'].append(reading('A', 'a1', 'cA', 1, '4'))
        with self.assertRaises(MeasurementError):
            publish(self.store, changed3)

    # -- 同底版竞争：最多一个推进 ----------------------------------------

    def test_concurrent_requests_same_base_only_one_advances(self):
        run = 'run-contention'
        regs = [registration('A', 'cA', 0, 10_000_000, 'r1', 'supply')]
        base = request(run, 'c0', 0, [reading('A', 'a1', 'cA', 1, '4')],
                       regs, network=NETWORK)
        publish(self.store, base)
        winner = {}
        errors = []

        def attempt(rid, raw):
            req = request(run, rid, 1,
                          [reading('A', raw, 'cA', 2, raw)], regs)
            try:
                result = publish(self.store, req)
                winner.setdefault('id', rid)
                winner.setdefault('version', result['version'])
            except MeasurementError as exc:
                errors.append(str(exc))

        threads = [
            threading.Thread(target=attempt, args=('c1', '5')),
            threading.Thread(target=attempt, args=('c2', '6')),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(errors), 1)
        self.assertIn('stale base_version', errors[0])
        self.assertEqual(winner['version'], 2)
        self.assertEqual(read_current(self.store, run)['version'], 2)
        # 失败请求不占用 ID：用该 ID 以当前底版发新请求应当成功。
        loser_id = 'c2' if winner['id'] == 'c1' else 'c1'
        again = publish(self.store, request(
            run, loser_id, 2, [reading('A', 'a3', 'cA', 3, '7')], regs))
        self.assertEqual(again['version'], 3)

    # -- 原始身份异内容：批内与历史冲突都拒绝整次发布 ---------------------

    def test_conflicting_raw_identity_rejects_whole_publish(self):
        run = 'run-raw-conflict'
        regs = [registration('A', 'cA', 0, 10_000_000, 'r1', 'supply')]
        good = request(run, 'g1', 0,
                       [reading('A', 'a1', 'cA', 1, '4')],
                       regs, network=NETWORK)
        publish(self.store, good)
        # 批内同身份异内容。
        bad_batch = request(run, 'g2', 1, [
            reading('A', 'a1', 'cA', 1, '4'),
            reading('A', 'a1', 'cA', 1, '9'),
        ], regs)
        with self.assertRaises(MeasurementError):
            publish(self.store, bad_batch)
        # 与已提交历史同身份异内容，整次拒绝。
        bad_history = request(run, 'g3', 1,
                              [reading('A', 'a1', 'cA', 1, '9')], regs)
        with self.assertRaises(MeasurementError):
            publish(self.store, bad_history)
        # 快照未变，版本未推进，新身份未落盘；既有身份证据仍是首版内容。
        self.assertEqual(read_current(self.store, run)['version'], 1)
        self.assertEqual(
            set(row_map(read_current(self.store, run))), {(1, 'r1')})
        surviving = read_evidence(self.store, run, 'A', 'a1', version=1)
        self.assertEqual(surviving['status'], 'accepted')
        self.assertEqual(surviving['converted_volts'], '4')
        # 同身份同内容重发只保留一条。
        dedup = publish(self.store, request(
            run, 'g4', 1,
            [reading('A', 'a1', 'cA', 1, '4'),
             reading('A', 'a1', 'cA', 1, '4')], regs))
        self.assertEqual(len(dedup['evidence']), 1)

    # -- 登记区间重叠与无效探针端点等登记校验 -----------------------------

    def test_registration_overlap_and_invalid_probe(self):
        run = 'run-bad-reg'
        overlap = request(run, 'b1', 0,
                          [reading('A', 'a1', 'cA', 1, '4')], [
                              registration('A', 'cA', 0, 5, 'r1', 'supply'),
                              registration('A', 'cA', 4, 10, 'r1', 'supply')],
                          network=NETWORK)
        with self.assertRaises(MeasurementError):
            publish(self.store, overlap)
        # 探针不是该电阻的端点（ground 不是 r1 的端点）。
        bad_probe = request(run, 'b2', 0,
                            [reading('A', 'a1', 'cA', 1, '4')],
                            [registration('A', 'cA', 0, 5, 'r1', 'ground')],
                            network=NETWORK)
        with self.assertRaises(MeasurementError):
            publish(self.store, bad_probe)
        # 登记的电阻不存在。
        missing_r = request(run, 'b3', 0,
                            [reading('A', 'a1', 'cA', 1, '4')],
                            [registration('A', 'cA', 0, 5, 'rX', 'supply')],
                            network=NETWORK)
        with self.assertRaises(MeasurementError):
            publish(self.store, missing_r)
        # gain 非正、空区间、越界 tick 都拒绝。
        cases = [
            registration('A', 'cA', 0, 5, 'r1', 'supply', gain='0'),
            registration('A', 'cA', 5, 5, 'r1', 'supply'),
            registration('A', 'cA', -1, 5, 'r1', 'supply'),
            registration('A', 'cA', 0, 10_000_001, 'r1', 'supply'),
        ]
        for reg in cases:
            with self.assertRaises(MeasurementError):
                publish(self.store, request(
                    run, 'bx', 0, [reading('A', 'a1', 'cA', 1, '4')],
                    [reg], network=NETWORK))
        # 任何失败都不得泄漏采集行或产生版本（锁文件目录本身允许存在）。
        self.assertFalse(os.path.exists(os.path.join(
            self.store, run, 'raw', 'A', 'a1.json')))
        self.assertFalse(os.path.exists(
            os.path.join(self.store, run, 'versions')))
        self.assertFalse(os.path.exists(os.path.join(self.store, run,
                                                     'current.json')))
        with self.assertRaises(MeasurementError):
            read_version(self.store, run, 1)

    # -- 失败不泄漏：非法读数/过期底版/网络规则 --------------------------

    def test_failures_do_not_mutate_committed_state(self):
        run = 'run-immutable'
        regs = [registration('A', 'cA', 0, 10_000_000, 'r1', 'supply')]
        publish(self.store, request(
            run, 'm1', 0, [reading('A', 'a1', 'cA', 1, '4')],
            regs, network=NETWORK))
        raw_files_before = list(Path(self.store, run, 'raw').rglob('*.json'))
        # 过期底版。
        with self.assertRaises(MeasurementError):
            publish(self.store, request(run, 'm2', 0, [], regs))
        # 有界十进制字符串口径：非法电压与增益。
        with self.assertRaises(MeasurementError):
            publish(self.store, request(
                run, 'm3', 1, [reading('A', 'a2', 'cA', 1, '4.0000007')],
                regs))
        with self.assertRaises(MeasurementError):
            publish(self.store, request(
                run, 'm4', 1, [reading('A', 'a2', 'cA', 1, '4')],
                [registration('A', 'cA', 0, 5, 'r1', 'supply', gain='-1')]))
        # 后续版本不得再提交 network。
        with self.assertRaises(MeasurementError):
            publish(self.store, request(
                run, 'm5', 1, [], regs, network=NETWORK))
        raw_files_after = list(Path(self.store, run, 'raw').rglob('*.json'))
        self.assertEqual(raw_files_before, raw_files_after)
        self.assertEqual(read_current(self.store, run)['version'], 1)

    def test_first_version_requires_valid_network(self):
        run = 'run-net'
        regs = [registration('A', 'cA', 0, 5, 'r1', 'supply')]
        with self.assertRaises(MeasurementError):
            publish(self.store, request(run, 'n1', 0, [], regs))
        bad_network = {'nodes': [{'id': 'x', 'voltage': None}],
                       'resistors': []}  # 悬浮分量，过不了现有网络校验
        with self.assertRaises(MeasurementError):
            publish(self.store, request(run, 'n2', 0, [], regs,
                                        network=bad_network))

    # -- 提交可见前的进程中断：原子性与重试推进 ---------------------------

    def test_crash_before_visibility_then_retry_completes(self):
        run = 'run-crash'
        v1_regs = [
            registration('A', 'cA', 0, 10_000_000, 'r1', 'supply'),
            registration('B', 'cB', 0, 10_000_000, 'r2', 'middle'),
        ]
        publish(self.store, request(
            run, 'k1', 0,
            [reading('A', 'rowA', 'cA', 7, '4'),
             reading('B', 'rowB', 'cB', 7, '2')],
            v1_regs, network=NETWORK))
        v2_regs = [
            registration('A', 'cA', 0, 10_000_000, 'r1', 'supply'),
            registration('B', 'cB', 0, 10_000_000, 'r1', 'middle'),
        ]
        v2_req = request(run, 'k2', 1, [], v2_regs)

        # 故障注入：版本目录与请求记录已写、current 切换前“进程中断”。
        with self.assertRaises(RuntimeError):
            publish(self.store, v2_req,
                    _hooks={'before_current_swap': _boom})
        # 重新打开只能看见完整旧版：不得只撤一个键。
        visible = read_current(self.store, run)
        self.assertEqual(visible['version'], 1)
        self.assertEqual(set(row_map(visible)), {(7, 'r1'), (7, 'r2')})
        # 不得出现新版冲突证据配旧版观测。
        self.assertEqual(
            read_evidence(self.store, run, 'B', 'rowB', version=1)['status'],
            'accepted')
        self.assertEqual(conflict_keys(visible), set())
        # 暂存残骸不得残留。
        versions_dir = Path(self.store, run, 'versions')
        self.assertEqual(sorted(p.name for p in versions_dir.iterdir()),
                         ['v1', 'v2'])

        # 同请求重试：恢复逻辑补完切换，完整推进到撤回版。
        retried = publish(self.store, v2_req)
        self.assertEqual(retried['version'], 2)
        self.assertEqual(row_map(retried), {})
        self.assertEqual(
            {tuple(key) for key in retried['changeset']['removed']},
            {(7, 'r1'), (7, 'r2')})
        self.assertEqual(conflict_keys(retried), {(7, 'r1')})
        self.assertEqual(
            read_evidence(self.store, run, 'B', 'rowB', version=2)['status'],
            'conflict')
        self.assertEqual(read_current(self.store, run)['version'], 2)

    def test_crash_after_raw_written_leaves_old_version(self):
        run = 'run-crash-raw'
        regs = [registration('A', 'cA', 0, 5, 'r1', 'supply')]
        publish(self.store, request(
            run, 'z1', 0, [reading('A', 'a1', 'cA', 1, '4')],
            regs, network=NETWORK))
        req = request(run, 'z2', 1,
                      [reading('A', 'a2', 'cA', 2, '8')], regs)
        with self.assertRaises(RuntimeError):
            publish(self.store, req,
                    _hooks={'raw_written': _boom})
        self.assertEqual(read_current(self.store, run)['version'], 1)
        # 重试成功推进，原始读数不重复写入。
        again = publish(self.store, req)
        self.assertEqual(again['version'], 2)
        self.assertEqual(set(row_map(again)), {(1, 'r1'), (2, 'r1')})

    # -- 新版已可见但调用方未收到结果：重放只能取回既有版 -----------------

    def test_visible_then_advanced_replay_returns_original_changeset(self):
        run = 'run-visible-replay'
        publish(self.store, request(
            run, 'w1', 0,
            [reading('A', 'rowA', 'cA', 7, '4'),
             reading('B', 'rowB', 'cB', 7, '2')], [
                registration('A', 'cA', 0, 10_000_000, 'r1', 'supply'),
                registration('B', 'cB', 0, 10_000_000, 'r2', 'middle')],
            network=NETWORK))
        withdraw_req = request(run, 'w2', 1, [], [
            registration('A', 'cA', 0, 10_000_000, 'r1', 'supply'),
            registration('B', 'cB', 0, 10_000_000, 'r1', 'middle')])
        withdraw = publish(self.store, withdraw_req)
        self.assertEqual(withdraw['version'], 2)
        # 之后又推进到恢复 r1 的版本。
        recover_req = request(run, 'w3', 2, [], [
            registration('A', 'cA', 0, 10_000_000, 'r1', 'supply'),
            registration('B', 'cB', 0, 10_000_000, 'r1', 'middle',
                         offset='-6')])
        publish(self.store, recover_req)
        self.assertEqual(read_current(self.store, run)['version'], 3)
        # 重送撤回请求：只能取回 v2 及其双删除，不能再发布。
        replay = publish(self.store, withdraw_req)
        self.assertEqual(replay['version'], 2)
        self.assertEqual(
            {tuple(key) for key in replay['changeset']['removed']},
            {(7, 'r1'), (7, 'r2')})
        self.assertEqual(replay['changeset']['upserted'], [])
        self.assertEqual(conflict_keys(replay), {(7, 'r1')})
        self.assertEqual(read_current(self.store, run)['version'], 3)
        # 旧底版（base=1）重放同样返回 v2，而非当前版内容。
        self.assertEqual(withdraw_req['base_version'], 1)

    # -- 运行隔离与跨进程可读性 -------------------------------------------

    def test_runs_are_isolated(self):
        regs = [registration('A', 'cA', 0, 5, 'r1', 'supply')]
        publish(self.store, request(
            'run-one', 'o1', 0, [reading('A', 'a1', 'cA', 1, '4')],
            regs, network=NETWORK))
        publish(self.store, request(
            'run-two', 't1', 0, [reading('A', 'a1', 'cA', 1, '9')],
            regs, network=NETWORK))
        self.assertEqual(
            row_map(read_current(self.store, 'run-one'))[(1, 'r1')]['volts'],
            '4')
        self.assertEqual(
            row_map(read_current(self.store, 'run-two'))[(1, 'r1')]['volts'],
            '9')

    def test_persists_across_processes(self):
        run = 'run-subprocess'
        regs = [registration('A', 'cA', 0, 5, 'r1', 'supply')]
        publish(self.store, request(
            run, 'p1', 0, [reading('A', 'a1', 'cA', 1, '4')],
            regs, network=NETWORK))
        script = (
            "import sys; sys.path.insert(0, %r);\n"
            "from resistor_flow.measurements import read_version;\n"
            "snap = read_version(%r, %r, 1);\n"
            "print(snap['observations'][0]['volts'], snap['version'])\n"
            % (str(Path(__file__).resolve().parents[1] / 'src'),
               self.store, run))
        completed = subprocess.run(
            [sys.executable, '-I', '-c', script],
            check=True, capture_output=True, text=True)
        self.assertEqual(completed.stdout.strip(), '4 1')

    # -- 容量上限 ---------------------------------------------------------

    def test_limits_on_readings_and_registrations(self):
        run = 'run-limits'
        with mock.patch.object(m, 'MAX_READINGS', 3):
            with self.assertRaises(MeasurementError):
                publish(self.store, request(run, 'l1', 0, [
                    reading('A', 'a%d' % i, 'c', i, '1')
                    for i in range(4)], [], network=NETWORK))
        regs = [registration('A', 'cA', 0, 5, 'r1', 'supply')]
        with mock.patch.object(m, 'MAX_REGISTRATIONS', 1):
            with self.assertRaises(MeasurementError):
                publish(self.store, request(
                    run, 'l2', 0, [],
                    regs + [registration('A', 'cB', 0, 5, 'r1', 'supply')],
                    network=NETWORK))
        # 上限检查失败不产生任何版本或原始读数。
        self.assertFalse(os.path.exists(
            os.path.join(self.store, run, 'versions')))
        self.assertFalse(os.path.exists(os.path.join(self.store, run,
                                                     'current.json')))


if __name__ == '__main__':
    unittest.main()
