import hashlib
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from resistor_flow import handover
from resistor_flow.handover import (
    HandoverError,
    deliver_volumes,
    review_case,
    seal,
)
from resistor_flow.measurements import publish

# 与 test_measurements 同构的两电阻网络：r1 a=supply b=middle；r2 a=middle b=ground。
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

RUN = 'run-handover'


def reading(station, row_id, channel, tick, raw_volts):
    return {'station': station, 'row_id': row_id, 'channel': channel,
            'tick': tick, 'raw_volts': raw_volts}


def registration(station, channel, start, end, resistor, node,
                 gain='1', offset='0'):
    return {'station': station, 'channel': channel, 'start_tick': start,
            'end_tick': end, 'resistor_id': resistor,
            'positive_node': node, 'gain': gain, 'offset_volts': offset}


def pub_request(run_id, request_id, base_version, readings, registrations,
                network=None):
    payload = {'run_id': run_id, 'request_id': request_id,
               'base_version': base_version, 'readings': readings,
               'registrations': registrations}
    if network is not None:
        payload['network'] = network
    return payload


def base_registrations(gain='1', offset='0'):
    return [
        registration('S', 'cS', 0, 10_000_000, 'r1', 'supply',
                     gain=gain, offset=offset),
        registration('T', 'cT', 0, 10_000_000, 'r1', 'supply'),
        registration('U', 'cUU', 0, 10_000_000, 'r2', 'middle'),
    ]


def base_readings():
    return [
        reading('S', 'p', 'cS', 4, '8'),    # tick4 r1 同值来源之一
        reading('T', 'q', 'cT', 4, '8'),    # tick4 r1 同值来源之二
        reading('S', 'r', 'cS', 5, '8'),    # tick5 r1 冲突：8
        reading('T', 's', 'cT', 5, '9'),    # tick5 r1 冲突：9
        reading('S', 'u', 'cU', 6, '1'),    # cU 无登记：未绑定
        reading('U', 'w', 'cUU', 9, '3'),   # 与上述案无关的测点时刻
    ]


def publish_base(store, run=RUN):
    """首版：tick4/r1 双源同值、tick5/r1 双源异值冲突、S/u 未绑定、
    tick9/r2 为无关测点时刻。"""
    return publish(store, pub_request(
        run, 'req-1', 0, base_readings(), base_registrations(),
        network=NETWORK))


def load_catalog(catalog_path):
    with open(catalog_path, 'rb') as handle:
        return json.loads(handle.read())


def group_volume(catalog, tick, resistor_id):
    for group in catalog['groups']:
        if group['tick'] == tick and group['resistor_id'] == resistor_id:
            return group['verdict']['volume']
    raise KeyError((tick, resistor_id))


def evidence_volume(catalog, station, row_id):
    for entry in catalog['evidence']:
        if entry['station'] == station and entry['row_id'] == row_id:
            return entry['volume']
    raise KeyError((station, row_id))


def copy_volumes(src_dir, dst_dir, exclude=()):
    """模拟按卷交接：复制除 exclude 外的全部卷。"""
    os.makedirs(dst_dir, exist_ok=True)
    for name in os.listdir(src_dir):
        vid = name[:-len('.json')]
        if vid in exclude:
            continue
        shutil.copy(os.path.join(src_dir, name), os.path.join(dst_dir, name))


def deliver_and_copy(sealed_dir, order, recv_dir):
    """发卷后把交付的卷复制到接收方卷目录，返回交付结果。"""
    delivery = deliver_volumes(sealed_dir, order)
    for item in delivery['volumes']:
        shutil.copy(item['path'],
                    os.path.join(recv_dir, item['volume_id'] + '.json'))
    return delivery


def obs_request(run, version, tick, resistor_id):
    return {'run_id': run, 'version': version, 'action': 'observation',
            'tick': tick, 'resistor_id': resistor_id}


def id_request(run, version, station, row_id):
    return {'run_id': run, 'version': version, 'action': 'identity',
            'station': station, 'row_id': row_id}


class HandoverTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.store = os.path.join(self.root, 'store')

    def make_seal(self, run=RUN, version=1, limit=1, name='sealed'):
        self.sealed_dir = os.path.join(self.root, name)
        info = seal(self.store, run, version, self.sealed_dir, limit)
        # 接收方另收目录文件本身；摘要经独立渠道交接（此处为返回值）。
        self.catalog_copy = os.path.join(self.root, 'recv-catalog.json')
        shutil.copy(info['catalog_path'], self.catalog_copy)
        return info

    # ------------------------------------------------------------------
    # 验收一：一案两卷一次索取、按身份只取裁决卷、夹带拒绝、分次补卷、
    # 无关卷损坏不阻塞且不读取、冲突/未绑定/不存在可辨。
    # ------------------------------------------------------------------

    def test_acceptance_case_chain(self):
        publish_base(self.store)
        info = self.make_seal(limit=1)
        catalog = load_catalog(info['catalog_path'])
        digest = info['catalog_digest']
        v_verdict4 = group_volume(catalog, 4, 'r1')
        v_tq = evidence_volume(catalog, 'T', 'q')
        v_su = evidence_volume(catalog, 'S', 'u')
        unrelated = {group_volume(catalog, 9, 'r2'),
                     evidence_volume(catalog, 'U', 'w')}

        recv = os.path.join(self.root, 'recv')
        copy_volumes(info['volumes_dir'], recv, exclude={v_verdict4, v_tq})
        # 损坏无关卷：换成同名目录，任何打开都会失败。
        for vid in unrelated:
            os.remove(os.path.join(recv, vid + '.json'))
            os.makedirs(os.path.join(recv, vid + '.json'))

        opened = []
        real_read = handover._read_file

        def spy(path):
            opened.append(os.fspath(path))
            return real_read(path)

        req_obs4 = obs_request(RUN, 1, 4, 'r1')
        req_sp = id_request(RUN, 1, 'S', 'p')
        with mock.patch.object(handover, '_read_file', side_effect=spy):
            # 按 tick4/r1 查：一次列齐裁决卷与 T/q 证据卷两卷。
            result = review_case(digest, self.catalog_copy, recv, req_obs4)
            self.assertEqual(result['status'], 'awaiting_volumes')
            self.assertEqual(result['missing_volumes'],
                             sorted([v_verdict4, v_tq]))
            self.assertEqual(result['catalog_digest'], digest)
            self.assertEqual(result['version'], 1)
            self.assertEqual(result['run_id'], RUN)
            self.assertNotIn('verdict', result)
            self.assertNotIn('evidence', result)

            # 按 S/p 查：只索取裁决卷（S/p 证据卷已在，不需要同组其他证据）。
            result = review_case(digest, self.catalog_copy, recv, req_sp)
            self.assertEqual(result['status'], 'awaiting_volumes')
            self.assertEqual(result['missing_volumes'], [v_verdict4])

            # 按 S/p 的补卷单夹带 T/q 卷：发卷端整单拒绝，裁决卷也不发出。
            with self.assertRaises(HandoverError):
                deliver_volumes(self.sealed_dir,
                                {'catalog_digest': digest, 'request': req_sp,
                                 'volumes': [v_verdict4, v_tq]})
            self.assertFalse(os.path.exists(
                os.path.join(recv, v_verdict4 + '.json')))

            # 合法单先补裁决卷；按 S/p 已可复核，测点案仍待 T/q。
            delivery = deliver_and_copy(
                self.sealed_dir,
                {'catalog_digest': digest, 'request': req_sp,
                 'volumes': [v_verdict4]}, recv)
            self.assertEqual([item['volume_id'] for item in
                              delivery['volumes']], [v_verdict4])
            self.assertEqual(delivery['catalog_digest'], digest)
            result = review_case(digest, self.catalog_copy, recv, req_sp)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['action'], 'identity')
            self.assertEqual(result['disposition'], 'accepted')
            self.assertEqual(result['group'], [4, 'r1'])
            self.assertEqual(result['evidence']['identity'], ['S', 'p'])
            self.assertEqual(result['evidence']['converted_volts'], '8')
            self.assertEqual(result['evidence']['registration']['gain'], '1')
            self.assertEqual(result['evidence']['observation_key'], [4, 'r1'])
            self.assertNotIn('verdict', result)  # 按身份不交付裁决正文
            result = review_case(digest, self.catalog_copy, recv, req_obs4)
            self.assertEqual(result['status'], 'awaiting_volumes')
            self.assertEqual(result['missing_volumes'], [v_tq])

            # 再补 T/q：测点案得到完整双方证据。
            deliver_and_copy(self.sealed_dir,
                             {'catalog_digest': digest, 'request': req_obs4,
                              'volumes': [v_tq]}, recv)
            result = review_case(digest, self.catalog_copy, recv, req_obs4)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['verdict'], {
                'kind': 'accepted', 'tick': 4, 'resistor_id': 'r1',
                'volts': '8', 'sources': [['S', 'p'], ['T', 'q']]})
            self.assertEqual([entry['identity'] for entry in
                              result['evidence']],
                             [['S', 'p'], ['T', 'q']])
            self.assertEqual([entry['status'] for entry in
                              result['evidence']], ['accepted', 'accepted'])

            # 冲突照样可辨：整键不发布值，交付全部来源与各自换算值。
            result = review_case(digest, self.catalog_copy, recv,
                                 obs_request(RUN, 1, 5, 'r1'))
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['verdict']['kind'], 'conflict')
            self.assertEqual(result['verdict']['sources'],
                             [['S', 'r'], ['T', 's']])
            self.assertNotIn('volts', result['verdict'])
            self.assertEqual(sorted(entry['converted_volts'] for entry in
                                    result['evidence']), ['8', '9'])
            self.assertEqual([entry['status'] for entry in
                              result['evidence']], ['conflict', 'conflict'])

            # 未绑定须看到完整证据才可确认；缺证据时只能待补卷。
            req_su = id_request(RUN, 1, 'S', 'u')
            result = review_case(digest, self.catalog_copy, recv, req_su)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['disposition'], 'unbound')
            self.assertIsNone(result['group'])
            self.assertIsNone(result['evidence']['registration'])
            os.remove(os.path.join(recv, v_su + '.json'))
            result = review_case(digest, self.catalog_copy, recv, req_su)
            self.assertEqual(result['status'], 'awaiting_volumes')
            self.assertEqual(result['missing_volumes'], [v_su])
            self.assertNotIn('evidence', result)

            # 确实不存在：合法但无绑定组的键、无此身份，且不需要读卷。
            def volumes_read():
                return [path for path in opened
                        if path.startswith(recv + os.sep)]
            before = len(volumes_read())
            result = review_case(digest, self.catalog_copy, recv,
                                 obs_request(RUN, 1, 7, 'r1'))
            self.assertEqual(result['status'], 'no_bound_evidence')
            self.assertEqual(result['key'], {'tick': 7, 'resistor_id': 'r1'})
            result = review_case(digest, self.catalog_copy, recv,
                                 id_request(RUN, 1, 'S', 'zzz'))
            self.assertEqual(result['status'], 'no_such_reading')
            self.assertEqual(result['identity'], ['S', 'zzz'])
            self.assertEqual(len(volumes_read()), before)  # 确认不存在没有读卷

        # 可观察手段：整条链从未读取被损坏的无关卷。
        for vid in unrelated:
            self.assertNotIn(os.path.join(recv, vid + '.json'), opened)

    # ------------------------------------------------------------------
    # 验收二：新版只改增益/偏置、换算值不变但登记证据不同；旧卷冒名
    # 替换待补卷；旧目录加足旧卷仍可复核；补卷端不认旧卷；坏目录与
    # 卷内容不符。
    # ------------------------------------------------------------------

    def test_versioned_registration_change(self):
        publish_base(self.store)
        # v2：只改 S 的增益与偏置，换算值仍是 8，裁决与来源不变。
        publish(self.store, pub_request(
            RUN, 'req-2', 1, [],
            base_registrations(gain='2', offset='-8')))
        sealed1 = seal(self.store, RUN, 1, os.path.join(self.root, 's1'), 1)
        sealed2 = seal(self.store, RUN, 2, os.path.join(self.root, 's2'), 1)
        cat1 = load_catalog(sealed1['catalog_path'])
        cat2 = load_catalog(sealed2['catalog_path'])
        digest1 = sealed1['catalog_digest']
        digest2 = sealed2['catalog_digest']
        self.assertNotEqual(digest1, digest2)
        v1_sp = evidence_volume(cat1, 'S', 'p')
        v2_sp = evidence_volume(cat2, 'S', 'p')
        self.assertNotEqual(v1_sp, v2_sp)  # 登记正文不同 -> 卷标识不同

        # 接收方持有新版目录与除 v2 S/p 证据卷外的新版卷，并混入旧版 S/p 卷。
        recv2 = os.path.join(self.root, 'recv2')
        copy_volumes(sealed2['volumes_dir'], recv2, exclude={v2_sp})
        shutil.copy(os.path.join(sealed1['volumes_dir'], v1_sp + '.json'),
                    os.path.join(recv2, v1_sp + '.json'))
        cat2_copy = os.path.join(self.root, 'cat2.json')
        shutil.copy(sealed2['catalog_path'], cat2_copy)
        req_sp2 = id_request(RUN, 2, 'S', 'p')

        # 旧卷冒名替换：待补卷，不返回旧登记，也不因裁决相同而放行。
        result = review_case(digest2, cat2_copy, recv2, req_sp2)
        self.assertEqual(result['status'], 'awaiting_volumes')
        self.assertEqual(result['missing_volumes'], [v2_sp])
        self.assertNotIn('evidence', result)
        # 卷已在但内容不符：把旧卷内容放到新卷名下，仍不可用。
        shutil.copy(os.path.join(sealed1['volumes_dir'], v1_sp + '.json'),
                    os.path.join(recv2, v2_sp + '.json'))
        result = review_case(digest2, cat2_copy, recv2, req_sp2)
        self.assertEqual(result['status'], 'awaiting_volumes')
        self.assertEqual(result['missing_volumes'], [v2_sp])
        os.remove(os.path.join(recv2, v2_sp + '.json'))

        # 补卷端不得拿旧目录对应卷响应新版案；目录摘要不符同样整单拒绝。
        with self.assertRaises(HandoverError):
            deliver_volumes(os.path.join(self.root, 's2'),
                            {'catalog_digest': digest2, 'request': req_sp2,
                             'volumes': [v1_sp]})
        with self.assertRaises(HandoverError):
            deliver_volumes(os.path.join(self.root, 's2'),
                            {'catalog_digest': digest1, 'request': req_sp2,
                             'volumes': [v2_sp]})
        # 合法补卷单：只交付所请求的新版证据卷。
        delivery = deliver_and_copy(os.path.join(self.root, 's2'),
                                    {'catalog_digest': digest2,
                                     'request': req_sp2,
                                     'volumes': [v2_sp]}, recv2)
        self.assertEqual([item['volume_id'] for item in
                          delivery['volumes']], [v2_sp])
        result = review_case(digest2, cat2_copy, recv2, req_sp2)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['disposition'], 'accepted')
        # 最终精确值不变，历史登记是新版增益/偏置。
        self.assertEqual(result['evidence']['converted_volts'], '8')
        self.assertEqual(result['evidence']['registration']['gain'], '2')
        self.assertEqual(result['evidence']['registration']['offset_volts'],
                         '-8')
        result = review_case(digest2, cat2_copy, recv2,
                             obs_request(RUN, 2, 4, 'r1'))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['verdict']['volts'], '8')
        self.assertEqual(result['verdict']['sources'],
                         [['S', 'p'], ['T', 'q']])

        # 旧目录加足旧卷仍可离线复核（此时原归档已移走）。
        recv1 = os.path.join(self.root, 'recv1')
        copy_volumes(sealed1['volumes_dir'], recv1)
        cat1_copy = os.path.join(self.root, 'cat1.json')
        shutil.copy(sealed1['catalog_path'], cat1_copy)
        shutil.move(self.store, os.path.join(self.root, 'store-gone'))
        result = review_case(digest1, cat1_copy, recv1,
                             id_request(RUN, 1, 'S', 'p'))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['evidence']['converted_volts'], '8')
        self.assertEqual(result['evidence']['registration']['gain'], '1')
        self.assertEqual(result['evidence']['registration']['offset_volts'],
                         '0')

        # 坏目录：摘要不符、无法读出、结构非法都整个请求报错。
        with self.assertRaises(HandoverError):
            review_case('0' * 64, cat1_copy, recv1,
                        id_request(RUN, 1, 'S', 'p'))
        garbage = os.path.join(self.root, 'garbage.json')
        with open(garbage, 'wb') as handle:
            handle.write(b'not json')
        with self.assertRaises(HandoverError):
            review_case(hashlib.sha256(b'not json').hexdigest(), garbage,
                        recv1, id_request(RUN, 1, 'S', 'p'))
        malformed = os.path.join(self.root, 'malformed.json')
        with open(malformed, 'wb') as handle:
            handle.write(b'{"format": "x"}')
        with self.assertRaises(HandoverError):
            review_case(hashlib.sha256(b'{"format": "x"}').hexdigest(),
                        malformed, recv1, id_request(RUN, 1, 'S', 'p'))
        # 未知运行或版号不能回落。
        with self.assertRaises(HandoverError):
            review_case(digest1, cat1_copy, recv1,
                        id_request('other-run', 1, 'S', 'p'))
        with self.assertRaises(HandoverError):
            review_case(digest1, cat1_copy, recv1,
                        id_request(RUN, 2, 'S', 'p'))

    # ------------------------------------------------------------------
    # 验收三：每卷上限大于一时，组跨多证据卷、不同案共用同一卷的复核
    # 与补卷；清单去重；同卷其他记录不混入案结果；移走原归档后仅凭
    # 已交接资料完成；重复封装内容标识一致。
    # ------------------------------------------------------------------

    def publish_shared(self):
        run = 'run-shared'
        readings = [
            reading('A', 'a1', 'cA', 1, '5'),
            reading('B', 'b1', 'cB', 1, '5'),
            reading('C', 'c1', 'cC', 1, '5'),
            reading('S', 'x1', 'cS', 2, '7'),
            reading('S', 'x2', 'cS', 2, '9'),
            reading('D', 'd1', 'cD', 3, '4'),
        ]
        regs = [
            registration('A', 'cA', 0, 10_000_000, 'r1', 'supply'),
            registration('B', 'cB', 0, 10_000_000, 'r1', 'supply'),
            registration('C', 'cC', 0, 10_000_000, 'r1', 'supply'),
            registration('S', 'cS', 0, 10_000_000, 'r1', 'supply'),
            registration('D', 'cD', 0, 10_000_000, 'r2', 'middle'),
        ]
        publish(self.store, pub_request(run, 'req-1', 0, readings, regs,
                                        network=NETWORK))
        return run

    def test_shared_volumes_and_multi_volume_groups(self):
        run = self.publish_shared()
        info = seal(self.store, run, 1, os.path.join(self.root, 'sealed'), 2)
        catalog = load_catalog(info['catalog_path'])
        digest = info['catalog_digest']
        cat_copy = os.path.join(self.root, 'cat.json')
        shutil.copy(info['catalog_path'], cat_copy)
        # 身份排序 A/a1,B/b1,C/c1,D/d1,S/x1,S/x2，每卷两条：
        ev_ab = evidence_volume(catalog, 'A', 'a1')
        ev_cd = evidence_volume(catalog, 'C', 'c1')
        ev_sx = evidence_volume(catalog, 'S', 'x1')
        self.assertEqual(ev_ab, evidence_volume(catalog, 'B', 'b1'))
        self.assertEqual(ev_cd, evidence_volume(catalog, 'D', 'd1'))
        self.assertEqual(ev_sx, evidence_volume(catalog, 'S', 'x2'))
        self.assertEqual(len({ev_ab, ev_cd, ev_sx}), 3)
        vv1 = group_volume(catalog, 1, 'r1')
        self.assertEqual(vv1, group_volume(catalog, 2, 'r1'))
        vv2 = group_volume(catalog, 3, 'r2')
        self.assertNotEqual(vv1, vv2)

        recv = os.path.join(self.root, 'recv')
        copy_volumes(info['volumes_dir'], recv, exclude={ev_cd})

        # 组跨多个证据卷：tick1/r1 需要 ev_ab 与 ev_cd，缺 ev_cd 待补。
        result = review_case(digest, cat_copy, recv,
                             obs_request(run, 1, 1, 'r1'))
        self.assertEqual(result['status'], 'awaiting_volumes')
        self.assertEqual(result['missing_volumes'], [ev_cd])
        # 不同案共用同一卷：tick3/r2 与身份 D/d1、C/c1 都待同一卷。
        for request in (obs_request(run, 1, 3, 'r2'),
                        id_request(run, 1, 'D', 'd1'),
                        id_request(run, 1, 'C', 'c1')):
            result = review_case(digest, cat_copy, recv, request)
            self.assertEqual(result['status'], 'awaiting_volumes')
            self.assertEqual(result['missing_volumes'], [ev_cd])
        # 冲突案材料齐备：不受他案缺卷阻塞；同卷还有别的裁决也不混入。
        result = review_case(digest, cat_copy, recv,
                             obs_request(run, 1, 2, 'r1'))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['verdict']['kind'], 'conflict')
        self.assertEqual(result['verdict']['tick'], 2)
        self.assertEqual([entry['identity'] for entry in
                          result['evidence']], [['S', 'x1'], ['S', 'x2']])
        # 按身份只交付该条证据：ev_ab 里还有 B/b1，不混入结果。
        result = review_case(digest, cat_copy, recv,
                             id_request(run, 1, 'A', 'a1'))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['evidence']['identity'], ['A', 'a1'])
        self.assertEqual(result['group'], [1, 'r1'])

        # 同一证据卷装同一组两条来源：缺失清单去重，只列一次。
        os.remove(os.path.join(recv, ev_sx + '.json'))
        result = review_case(digest, cat_copy, recv,
                             obs_request(run, 1, 2, 'r1'))
        self.assertEqual(result['status'], 'awaiting_volumes')
        self.assertEqual(result['missing_volumes'], [ev_sx])

        # 补卷：按案范围各取所缺卷；ev_cd 同时是多个案的合法范围。
        deliver_and_copy(os.path.join(self.root, 'sealed'),
                         {'catalog_digest': digest,
                          'request': obs_request(run, 1, 1, 'r1'),
                          'volumes': [ev_cd]}, recv)
        deliver_and_copy(os.path.join(self.root, 'sealed'),
                         {'catalog_digest': digest,
                          'request': obs_request(run, 1, 2, 'r1'),
                          'volumes': [ev_sx]}, recv)
        result = review_case(digest, cat_copy, recv,
                             obs_request(run, 1, 1, 'r1'))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['verdict']['volts'], '5')
        self.assertEqual([entry['identity'] for entry in
                          result['evidence']],
                         [['A', 'a1'], ['B', 'b1'], ['C', 'c1']])

        # 移走原归档：仅凭已交接资料完成复核与补卷。
        shutil.move(self.store, os.path.join(self.root, 'store-gone'))
        result = review_case(digest, cat_copy, recv,
                             id_request(run, 1, 'S', 'x1'))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['disposition'], 'conflict')
        delivery = deliver_volumes(os.path.join(self.root, 'sealed'),
                                   {'catalog_digest': digest,
                                    'request': id_request(run, 1, 'D', 'd1'),
                                    'volumes': [ev_cd, vv2]})
        self.assertEqual([item['volume_id'] for item in
                          delivery['volumes']], sorted([ev_cd, vv2]))

    def test_seal_determinism_and_limits(self):
        run = self.publish_shared()
        first = seal(self.store, run, 1, os.path.join(self.root, 'a'), 2)
        second = seal(self.store, run, 1, os.path.join(self.root, 'b'), 2)
        self.assertEqual(first['catalog_digest'], second['catalog_digest'])
        self.assertEqual(first['volumes'], second['volumes'])
        with open(first['catalog_path'], 'rb') as handle:
            data_first = handle.read()
        with open(second['catalog_path'], 'rb') as handle:
            self.assertEqual(data_first, handle.read())
        third = seal(self.store, run, 1, os.path.join(self.root, 'c'), 3)
        self.assertNotEqual(first['catalog_digest'],
                            third['catalog_digest'])

    # ------------------------------------------------------------------
    # 资料不一致：完整性通过但正文与目录路由、身份、归属或裁决矛盾。
    # ------------------------------------------------------------------

    def test_inconsistent_package(self):
        publish_base(self.store)
        info = self.make_seal(limit=1)
        catalog = load_catalog(info['catalog_path'])
        recv = os.path.join(self.root, 'recv')
        copy_volumes(info['volumes_dir'], recv)

        # 篡改路由：S/p 与 T/q 的证据位置互换，目录其余部分不动。
        crafted = json.loads(json.dumps(catalog))
        entries = {tuple([e['station'], e['row_id']]): e
                   for e in crafted['evidence']}
        sp, tq = entries[('S', 'p')], entries[('T', 'q')]
        sp['volume'], tq['volume'] = tq['volume'], sp['volume']
        data = json.dumps(crafted, sort_keys=True).encode('utf-8')
        bad_catalog = os.path.join(self.root, 'crafted.json')
        with open(bad_catalog, 'wb') as handle:
            handle.write(data)
        digest = hashlib.sha256(data).hexdigest()
        result = review_case(digest, bad_catalog, recv,
                             id_request(RUN, 1, 'S', 'p'))
        self.assertEqual(result['status'], 'inconsistent')
        self.assertEqual(result['catalog_digest'], digest)
        self.assertEqual(result['version'], 1)
        self.assertNotIn('evidence', result)
        result = review_case(digest, bad_catalog, recv,
                             obs_request(RUN, 1, 4, 'r1'))
        self.assertEqual(result['status'], 'inconsistent')

        # 篡改裁决路由：tick4/r1 的裁决指向 tick9/r2 的裁决卷。
        crafted = json.loads(json.dumps(catalog))
        verdict9 = None
        for group in crafted['groups']:
            if group['tick'] == 9:
                verdict9 = group['verdict']
        for group in crafted['groups']:
            if group['tick'] == 4:
                group['verdict'] = dict(verdict9)
        data = json.dumps(crafted, sort_keys=True).encode('utf-8')
        with open(bad_catalog, 'wb') as handle:
            handle.write(data)
        digest = hashlib.sha256(data).hexdigest()
        result = review_case(digest, bad_catalog, recv,
                             obs_request(RUN, 1, 4, 'r1'))
        self.assertEqual(result['status'], 'inconsistent')
        # 不按待补卷反复索取：不是 awaiting_volumes。
        self.assertNotEqual(result['status'], 'awaiting_volumes')

    # ------------------------------------------------------------------
    # 参数与失败边界。
    # ------------------------------------------------------------------

    def test_seal_and_request_validation(self):
        publish_base(self.store)
        out = os.path.join(self.root, 'out')
        with self.assertRaises(HandoverError):
            seal(self.store, RUN, 0, out, 1)          # 版号必须为正整数
        with self.assertRaises(HandoverError):
            seal(self.store, RUN, 9, out, 1)          # 未知版本不回落
        with self.assertRaises(HandoverError):
            seal(self.store, 'no-such-run', 1, out, 1)
        for bad in (0, 257, '2', True, 1.5):
            with self.assertRaises(HandoverError):
                seal(self.store, RUN, 1, out, bad)
        info = seal(self.store, RUN, 1, out, 1)
        with self.assertRaises(HandoverError):
            seal(self.store, RUN, 1, out, 1)          # 输出目录必须未存在

        digest = info['catalog_digest']
        recv = os.path.join(self.root, 'recv')
        copy_volumes(info['volumes_dir'], recv)
        cat = os.path.join(self.root, 'cat.json')
        shutil.copy(info['catalog_path'], cat)
        # 复核请求字段非法。
        bad_requests = [
            {'run_id': RUN, 'version': 1, 'action': 'observation',
             'tick': 4, 'resistor_id': 'r1', 'extra': 1},
            {'run_id': RUN, 'version': 1, 'action': 'tick',
             'tick': 4, 'resistor_id': 'r1'},
            {'run_id': RUN, 'version': 1, 'action': 'observation',
             'tick': True, 'resistor_id': 'r1'},
            {'run_id': RUN, 'version': 1, 'action': 'observation',
             'tick': -1, 'resistor_id': 'r1'},
            {'run_id': RUN, 'version': 1, 'action': 'observation',
             'tick': 10_000_000, 'resistor_id': 'r1'},
            {'run_id': RUN, 'version': 0, 'action': 'identity',
             'station': 'S', 'row_id': 'p'},
            {'run_id': RUN, 'version': '1', 'action': 'identity',
             'station': 'S', 'row_id': 'p'},
            {'run_id': RUN, 'version': 1, 'action': 'identity',
             'station': 'bad id!', 'row_id': 'p'},
        ]
        for request in bad_requests:
            with self.assertRaises(HandoverError):
                review_case(digest, cat, recv, request)
        with self.assertRaises(HandoverError):
            review_case('zz', cat, recv, id_request(RUN, 1, 'S', 'p'))

        # 补卷单：目录能证明不存在时该案无权取用数据卷。
        with self.assertRaises(HandoverError):
            deliver_volumes(os.path.join(self.root, 'out'),
                            {'catalog_digest': digest,
                             'request': obs_request(RUN, 1, 7, 'r1'),
                             'volumes': [info['volumes'][0]]})
        # 不存在的身份同样无权取用。
        with self.assertRaises(HandoverError):
            deliver_volumes(os.path.join(self.root, 'out'),
                            {'catalog_digest': digest,
                             'request': id_request(RUN, 1, 'S', 'zzz'),
                             'volumes': [info['volumes'][0]]})
        # 未知卷标识、非列表、缺字段都整单拒绝。
        with self.assertRaises(HandoverError):
            deliver_volumes(os.path.join(self.root, 'out'),
                            {'catalog_digest': digest,
                             'request': id_request(RUN, 1, 'S', 'p'),
                             'volumes': ['1' * 64]})
        with self.assertRaises(HandoverError):
            deliver_volumes(os.path.join(self.root, 'out'),
                            {'catalog_digest': digest,
                             'request': id_request(RUN, 1, 'S', 'p'),
                             'volumes': 'not-a-list'})
        with self.assertRaises(HandoverError):
            deliver_volumes(os.path.join(self.root, 'out'),
                            {'catalog_digest': digest,
                             'request': id_request(RUN, 1, 'S', 'p')})

        # 发卷方副本缺所请求卷：整单拒绝，不交付部分。
        broken = os.path.join(self.root, 'broken-copy')
        shutil.copytree(os.path.join(self.root, 'out'), broken)
        v_sp = evidence_volume(load_catalog(info['catalog_path']), 'S', 'p')
        os.remove(os.path.join(broken, 'volumes', v_sp + '.json'))
        with self.assertRaises(HandoverError):
            deliver_volumes(broken, {'catalog_digest': digest,
                                     'request': id_request(RUN, 1, 'S', 'p'),
                                     'volumes': [v_sp]})


if __name__ == '__main__':
    unittest.main()
