import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from resistor_flow.datasets import (
    DatasetInputError,
    HoldoutInfeasible,
    build_datasets,
)
from resistor_flow.measurements import publish, read_current

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

RESISTOR = 'r1'


def reading(station, row_id, tick, raw_volts, channel='c'):
    return {'station': station, 'row_id': row_id, 'channel': channel,
            'tick': tick, 'raw_volts': raw_volts}


def row(tick, raw_volts, sources):
    return {'tick': tick, 'raw_volts': raw_volts, 'sources': sources}


def _registration(station='S', channel='c', end=10_000_000):
    return {'station': station, 'channel': channel, 'start_tick': 0,
            'end_tick': end, 'resistor_id': RESISTOR,
            'positive_node': 'supply', 'gain': '1', 'offset_volts': '0'}


def publish_rows(store, run_id, rows, request_id, base_version, network=None,
                 extras=()):
    readings = []
    for item in rows:
        for source in item['sources']:
            readings.append(reading('S', source, item['tick'],
                                    item['raw_volts']))
    readings.extend(extras)
    request = {'run_id': run_id, 'request_id': request_id,
               'base_version': base_version, 'readings': readings,
               'registrations': [_registration()]}
    if network is not None:
        request['network'] = network
    return publish(store, request)


def specimen_entries(mapping):
    return [{'run_id': run_id, 'tick': tick, 'resistor_id': resistor,
             'specimen_id': specimen, 'condition': condition}
            for (run_id, tick, resistor), (specimen, condition)
            in mapping.items()]


def batch_entries(mapping):
    return [{'run_id': run_id, 'station': station, 'row_id': row_id,
             'batch_id': batch}
            for (run_id, station, row_id), batch in mapping.items()]


def manifest(versions, specimens, batches, counts):
    return {
        'versions': [{'run_id': run_id, 'version': version}
                     for run_id, version in versions.items()],
        'specimens': specimen_entries(specimens),
        'calibration_batches': batch_entries(batches),
        'holdout_counts': dict(counts),
    }


def row_keys(dataset):
    return [(item['run_id'], item['tick'], item['resistor_id'])
            for item in dataset['rows']]


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


class AcceptanceOneTests(unittest.TestCase):
    """A–F 六行：加来源后传递成组、旧版仍选 A、B。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = self.temp.name
        rows = [row(1, '10', ['a']), row(2, '20', ['b'])]
        # 三个运行复用同样的原始来源局部 ID a/b；来源键必须带运行作用域。
        for run_id in ('run-1', 'run-2', 'run-3'):
            publish_rows(self.store, run_id, rows, 'v1', 0, network=NETWORK)

        self.specimens_v1 = {
            ('run-1', 1, RESISTOR): ('sa', 'L'),
            ('run-1', 2, RESISTOR): ('sb', 'H'),
            ('run-2', 1, RESISTOR): ('sc', 'L'),
            ('run-2', 2, RESISTOR): ('sd', 'H'),
            ('run-3', 1, RESISTOR): ('se', 'L'),
            ('run-3', 2, RESISTOR): ('sc', 'H'),
        }
        self.batches_v1 = {
            ('run-1', 'S', 'a'): 'bA',
            ('run-1', 'S', 'b'): 'bB',
            ('run-2', 'S', 'a'): 'bC',
            ('run-2', 'S', 'b'): 'bD',
            ('run-3', 'S', 'a'): 'bE',
            ('run-3', 'S', 'b'): 'bF',
        }
        self.versions_v1 = {'run-1': 1, 'run-2': 1, 'run-3': 1}
        self.manifest_v1 = manifest(self.versions_v1, self.specimens_v1,
                                    self.batches_v1, {'L': 1, 'H': 1})

    def _publish_v2_for_run_one(self):
        # A 增加一条数值一致的来源；另加未绑定与冲突读数，二者都不是标准观测。
        publish_rows(
            self.store, 'run-1', [row(1, '10', ['a2'])], 'v2', 1,
            extras=[reading('X', 'u1', 999, '7'),
                    reading('S', 'c1', 9, '1'),
                    reading('S', 'c2', 9, '2')])

    def test_source_addition_groups_a_c_f_and_old_version_stays(self):
        first = build_datasets(self.store, self.manifest_v1)
        self.assertEqual(first['versions'],
                         [{'run_id': 'run-1', 'version': 1},
                          {'run_id': 'run-2', 'version': 1},
                          {'run_id': 'run-3', 'version': 1}])
        self.assertEqual(row_keys(first['holdout']),
                         [('run-1', 1, RESISTOR),
                          ('run-1', 2, RESISTOR)])
        self.assertEqual(first['holdout']['counts'], {'L': 1, 'H': 1})
        self.assertEqual(row_keys(first['fit']),
                         [('run-2', 1, RESISTOR), ('run-2', 2, RESISTOR),
                          ('run-3', 1, RESISTOR), ('run-3', 2, RESISTOR)])

        # 记录旧版构建结果与归档摘要，稍后重建必须完全一致。
        self._publish_v2_for_run_one()
        self.assertEqual(read_current(self.store, 'run-1')['version'], 2)
        digest_before = tree_digest(self.store)

        specimens_v2 = dict(self.specimens_v1)
        batches_v2 = dict(self.batches_v1)
        batches_v2[('run-1', 'S', 'a2')] = 'bC'  # 与 C 的来源同校准批次
        versions_v2 = dict(self.versions_v1)
        versions_v2['run-1'] = 2
        manifest_v2 = manifest(versions_v2, specimens_v2, batches_v2,
                               {'L': 1, 'H': 1})

        second = build_datasets(self.store, manifest_v2)
        self.assertEqual(second['versions'],
                         [{'run_id': 'run-1', 'version': 2},
                          {'run_id': 'run-2', 'version': 1},
                          {'run_id': 'run-3', 'version': 1}])
        # A、C、F 传递成一组，组标识取组内最小行键 A。
        a_key = ['run-1', 1, RESISTOR]
        for item in second['fit']['rows']:
            if (item['run_id'], item['tick']) in {
                    ('run-1', 1), ('run-2', 1), ('run-3', 2)}:
                self.assertEqual(item['group_id'], a_key, item)
        self.assertEqual(row_keys(second['holdout']),
                         [('run-1', 2, RESISTOR), ('run-3', 1, RESISTOR)])
        self.assertEqual(second['holdout']['counts'], {'L': 1, 'H': 1})
        self.assertEqual(row_keys(second['fit']),
                         [('run-1', 1, RESISTOR), ('run-2', 1, RESISTOR),
                          ('run-2', 2, RESISTOR), ('run-3', 2, RESISTOR)])
        # 非标准观测未进入候选：冲突 tick 9 与未绑定 X/u1 都不在任一侧。
        all_keys = row_keys(second['fit']) + row_keys(second['holdout'])
        self.assertEqual(len(all_keys), 6)
        self.assertNotIn(9, [key[1] for key in all_keys])
        # A 的完整来源与批次都交付，包括新增的非首条来源。
        a_row = second['fit']['rows'][0]
        self.assertEqual(a_row['sources'], [
            {'run_id': 'run-1', 'station': 'S', 'row_id': 'a',
             'batch_id': 'bA'},
            {'run_id': 'run-1', 'station': 'S', 'row_id': 'a2',
             'batch_id': 'bC'},
        ])
        self.assertEqual(a_row['volts'], '10')
        self.assertEqual(a_row['condition'], 'L')
        self.assertEqual(a_row['specimen_id'], 'sa')
        self.assertEqual(a_row['version'], 2)

        # 归档未被构建推进或改写。
        self.assertEqual(tree_digest(self.store), digest_before)
        self.assertEqual(read_current(self.store, 'run-1')['version'], 2)

        # 用旧版及旧补充资料重建仍选 A、B，且结果与首次一致。
        third = build_datasets(self.store, self.manifest_v1)
        self.assertEqual(third, first)

    def test_missing_non_first_source_batch_is_rejected(self):
        self._publish_v2_for_run_one()
        batches = dict(self.batches_v1)  # 故意不含 (run-1,S,a2)
        versions = dict(self.versions_v1)
        versions['run-1'] = 2
        bad = manifest(versions, self.specimens_v1, batches, {'L': 1, 'H': 1})
        with self.assertRaises(DatasetInputError):
            build_datasets(self.store, bad)
        # 多余批次键同样拒绝。
        batches_extra = dict(self.batches_v1)
        batches_extra[('run-1', 'S', 'a2')] = 'bC'
        batches_extra[('run-1', 'S', 'ghost')] = 'bZ'
        bad_extra = manifest(versions, self.specimens_v1, batches_extra,
                             {'L': 1, 'H': 1})
        with self.assertRaises(DatasetInputError):
            build_datasets(self.store, bad_extra)

    def test_cross_run_source_ids_are_not_merged(self):
        # 旧版清单中三个运行都用 (S,a)/(S,b)，但批次各异，不能因此成组。
        result = build_datasets(self.store, self.manifest_v1)
        group_ids = {item['group_id'][0] + '/%d' % item['tick']:
                     item['group_id'] for item in
                     result['fit']['rows'] + result['holdout']['rows']}
        # C 与 F 因样件成组；A、B、D、E 各自独立。
        rows = result['fit']['rows'] + result['holdout']['rows']
        by_key = {(item['run_id'], item['tick']): item for item in rows}
        self.assertEqual(by_key[('run-2', 1)]['group_id'],
                         by_key[('run-3', 2)]['group_id'])
        for run_id, tick in (('run-1', 1), ('run-1', 2),
                             ('run-2', 2), ('run-3', 1)):
            self.assertEqual(by_key[(run_id, tick)]['group_id'],
                             [run_id, tick, RESISTOR])
        self.assertIn('run-2/1', group_ids)


class AcceptanceTwoTests(unittest.TestCase):
    """三个有序组 (1,1)、(2,0)、(0,2)，目标 (2,2) 必须交后两组。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = self.temp.name
        rows = [row(tick, str(tick), ['a%d' % tick])
                for tick in range(1, 7)]
        publish_rows(self.store, 'grp', rows, 'v1', 0, network=NETWORK)
        # 组内两行分别由同一样件绑定，故三组行数依次为 (1,1)、(2,0)、(0,2)。
        self.specimens = {
            ('grp', 1, RESISTOR): ('g1', 'L'),
            ('grp', 2, RESISTOR): ('g1', 'H'),
            ('grp', 3, RESISTOR): ('g2', 'L'),
            ('grp', 4, RESISTOR): ('g2', 'L'),
            ('grp', 5, RESISTOR): ('g3', 'H'),
            ('grp', 6, RESISTOR): ('g3', 'H'),
        }
        self.batches = {('grp', 'S', 'a%d' % tick): 'b%d' % tick
                        for tick in range(1, 7)}
        self.versions = {'grp': 1}

    def _manifest(self, counts):
        return manifest(self.versions, self.specimens, self.batches, counts)

    def test_prefix_group_skipped_for_later_groups(self):
        result = build_datasets(self.store, self._manifest({'L': 2, 'H': 2}))
        self.assertEqual(row_keys(result['holdout']),
                         [('grp', 3, RESISTOR), ('grp', 4, RESISTOR),
                          ('grp', 5, RESISTOR), ('grp', 6, RESISTOR)])
        self.assertEqual(result['holdout']['counts'], {'L': 2, 'H': 2})
        self.assertEqual(row_keys(result['fit']),
                         [('grp', 1, RESISTOR), ('grp', 2, RESISTOR)])
        self.assertEqual(
            {tuple(item['group_id']) for item in result['holdout']['rows']},
            {('grp', 3, RESISTOR), ('grp', 5, RESISTOR)})
        # 每组完整交付，组内行数即 (1,1)/(2,0)/(0,2)。
        by_group = {}
        for item in result['fit']['rows'] + result['holdout']['rows']:
            by_group.setdefault(tuple(item['group_id']), []).append(
                item['condition'])
        self.assertEqual(by_group[('grp', 1, RESISTOR)].count('L'), 1)
        self.assertEqual(by_group[('grp', 1, RESISTOR)].count('H'), 1)
        self.assertEqual(by_group[('grp', 3, RESISTOR)].count('L'), 2)
        self.assertEqual(by_group[('grp', 5, RESISTOR)].count('H'), 2)

    def test_lexicographically_smallest_selection(self):
        # 三个组：(1,0)、(0,1)、(1,1)，目标 (1,1)。g1+g2 与 g3 都可行，
        # 排序后序列 [g1,g2] 字典序小于 [g3]。
        with tempfile.TemporaryDirectory() as temp:
            store = os.path.join(temp, 'store')
            os.makedirs(store)
            rows = [row(1, '1', ['a1']), row(2, '1', ['a2']),
                    row(3, '1', ['a3'])]
            publish_rows(store, 'lex', rows, 'v1', 0, network=NETWORK)
            specimens = {('lex', 1, RESISTOR): ('x1', 'L'),
                         ('lex', 2, RESISTOR): ('x2', 'H'),
                         ('lex', 3, RESISTOR): ('x3', 'L')}
            # 第三组另加一条 H 行，使它是 (1,1)。
            rows = [row(4, '1', ['a4'])]
            publish_rows(store, 'lex', rows, 'v2', 1)
            specimens[('lex', 4, RESISTOR)] = ('x3', 'H')
            batches = {('lex', 'S', 'a%d' % i): 'b%d' % i
                       for i in range(1, 5)}
            result = build_datasets(
                store, manifest({'lex': 2}, specimens, batches,
                                {'L': 1, 'H': 1}))
            self.assertEqual(row_keys(result['holdout']),
                             [('lex', 1, RESISTOR), ('lex', 2, RESISTOR)])
            self.assertEqual(row_keys(result['fit']),
                             [('lex', 3, RESISTOR), ('lex', 4, RESISTOR)])

    def test_infeasible_is_distinct_and_returns_nothing(self):
        with self.assertRaises(HoldoutInfeasible) as caught:
            build_datasets(self.store, self._manifest({'L': 2, 'H': 3}))
        self.assertNotIsInstance(caught.exception, DatasetInputError)
        # 失败不改变归档。
        self.assertEqual(read_current(self.store, 'grp')['version'], 1)

    def test_manifest_reordering_does_not_change_result(self):
        baseline = build_datasets(self.store,
                                  self._manifest({'L': 2, 'H': 2}))
        reordered = manifest(self.versions, self.specimens, self.batches,
                             {'H': 2, 'L': 2})
        reordered['versions'].reverse()
        reordered['specimens'].reverse()
        reordered['calibration_batches'].reverse()
        self.assertEqual(build_datasets(self.store, reordered), baseline)

    def test_unknown_version_and_corrupt_manifest_are_input_errors(self):
        bad = self._manifest({'L': 2, 'H': 2})
        bad['versions'] = [{'run_id': 'grp', 'version': 9}]
        with self.assertRaises(DatasetInputError):
            build_datasets(self.store, bad)

        bad = self._manifest({'L': 2, 'H': 2})
        bad['versions'] = [{'run_id': 'missing', 'version': 1}]
        with self.assertRaises(DatasetInputError):
            build_datasets(self.store, bad)

        bad = self._manifest({'L': 2, 'H': 2})
        bad['holdout_counts'] = {'L': True, 'H': 2}
        with self.assertRaises(DatasetInputError):
            build_datasets(self.store, bad)

        bad = self._manifest({'L': 2, 'H': 2})
        bad['versions'] = [{'run_id': 'grp', 'version': True}]
        with self.assertRaises(DatasetInputError):
            build_datasets(self.store, bad)

        bad = self._manifest({'L': 2, 'H': 2})
        bad['specimens'][0]['condition'] = 'M'
        with self.assertRaises(DatasetInputError):
            build_datasets(self.store, bad)

        bad = self._manifest({'L': 2, 'H': 2})
        bad['specimens'].append(dict(bad['specimens'][0]))
        with self.assertRaises(DatasetInputError):
            build_datasets(self.store, bad)

        bad = self._manifest({'L': 2, 'H': 2})
        bad['calibration_batches'].pop()
        with self.assertRaises(DatasetInputError):
            build_datasets(self.store, bad)

        bad = self._manifest({'L': 2, 'H': 2})
        bad['holdout_counts'] = {'L': 2}
        with self.assertRaises(DatasetInputError):
            build_datasets(self.store, bad)

        bad = self._manifest({'L': 2, 'H': 2})
        bad['holdout_counts'] = {'L': 0, 'H': 2}
        with self.assertRaises(DatasetInputError):
            build_datasets(self.store, bad)

        bad = self._manifest({'L': 2, 'H': 2})
        bad['versions'].append({'run_id': 'grp', 'version': 1})
        with self.assertRaises(DatasetInputError):
            build_datasets(self.store, bad)


def prepare_benchmark(store):
    """生成 1,050 L 单行组 + 1,050 H 单行组 + 600 混合组。"""
    rows = []
    specimens = {}
    batches = {}
    for index in range(1050):
        tick = 1 + index
        rows.append(row(tick, '1', ['r%05d' % tick]))
        specimens[('bench', tick, RESISTOR)] = ('sl%04d' % index, 'L')
        batches[('bench', 'S', 'r%05d' % tick)] = 'bl%04d' % index
    for index in range(1050):
        tick = 2001 + index
        rows.append(row(tick, '1', ['r%05d' % tick]))
        specimens[('bench', tick, RESISTOR)] = ('sh%04d' % index, 'H')
        batches[('bench', 'S', 'r%05d' % tick)] = 'bh%04d' % index
    for index in range(600):
        tick_l = 4001 + 2 * index
        tick_h = tick_l + 1
        rows.append(row(tick_l, '1', ['r%05d' % tick_l]))
        rows.append(row(tick_h, '1', ['r%05d' % tick_h]))
        specimens[('bench', tick_l, RESISTOR)] = ('mx%04d' % index, 'L')
        specimens[('bench', tick_h, RESISTOR)] = ('mx%04d' % index, 'H')
        batches[('bench', 'S', 'r%05d' % tick_l)] = 'bml%04d' % index
        batches[('bench', 'S', 'r%05d' % tick_h)] = 'bmh%04d' % index
    publish_rows(store, 'bench', rows, 'v1', 0, network=NETWORK)
    return manifest({'bench': 1}, specimens, batches, {'L': 1500, 'H': 1500})


class BenchmarkTests(unittest.TestCase):
    """128 MiB 地址空间、CPython 3.11 单核下 <= 45 秒的复验负载。"""

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux only')
    def test_large_workload_under_address_space_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            store = os.path.join(temp, 'store')
            manifest_path = os.path.join(temp, 'manifest.json')
            script_path = os.path.join(temp, 'check.py')
            os.makedirs(store)
            prepared = prepare_benchmark(store)
            with open(manifest_path, 'w', encoding='utf-8') as handle:
                json.dump(prepared, handle)
            source_root = str(Path(__file__).resolve().parents[1] / 'src')
            with open(script_path, 'w', encoding='utf-8') as handle:
                handle.write(CHECK_SCRIPT.replace('__SRC__', source_root))
            started = time.monotonic()
            completed = subprocess.run(
                ['bash', '-c',
                 'ulimit -v 131072; exec %s -I %s %s %s'
                 % (sys.executable, script_path, store, manifest_path)],
                capture_output=True, text=True, timeout=120)
            elapsed = time.monotonic() - started
            self.assertEqual(completed.returncode, 0,
                             completed.stderr or completed.stdout)
            self.assertLess(elapsed, 45.0)
            summary = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertEqual(summary['holdout'], {'L': 1500, 'H': 1500})
            self.assertEqual(summary['fit'], {'L': 150, 'H': 150})
            self.assertEqual(summary['groups'], 2700)


# 在 128 MiB 地址空间的新进程内构建并逐项核对两份完整数据。
CHECK_SCRIPT = '''\
import json, sys, time
sys.path.insert(0, '__SRC__')
from resistor_flow.datasets import build_datasets

R = 'r1'
store, manifest_path = sys.argv[1], sys.argv[2]
with open(manifest_path, 'r', encoding='utf-8') as handle:
    manifest = json.load(handle)
started = time.monotonic()
result = build_datasets(store, manifest)
elapsed = time.monotonic() - started
assert result['versions'] == [{'run_id': 'bench', 'version': 1}]
fit, holdout = result['fit'], result['holdout']
assert holdout['counts'] == {'L': 1500, 'H': 1500}, holdout['counts']
assert fit['counts'] == {'L': 150, 'H': 150}, fit['counts']
assert len(fit['rows']) == 300 and len(holdout['rows']) == 3300 - 300
fit_ticks = sorted(item['tick'] for item in fit['rows'])
assert fit_ticks == list(range(4901, 5201)), fit_ticks[:5]
holdout_keys = [(item['run_id'], item['tick'], item['resistor_id'])
                for item in holdout['rows']]
assert holdout_keys == sorted(holdout_keys)
fit_keys = [(item['run_id'], item['tick'], item['resistor_id'])
            for item in fit['rows']]
assert fit_keys == sorted(fit_keys)
# 单行组全部入选，混合组按组标识顺序取最早 450 组。
mix_holdout = sorted({item['tick'] if item['tick'] % 2 else item['tick'] - 1
                      for item in holdout['rows']
                      if item['specimen_id'].startswith('mx')})
assert mix_holdout == list(range(4001, 4901, 2)), mix_holdout[:5]
assert all(item['specimen_id'].startswith('mx') for item in fit['rows'])
assert holdout['counts'] == {'L': 1500, 'H': 1500}
# 每行来源、批次、条件、组标识与清单逐项一致。
expected_batch = {}
for entry in manifest['calibration_batches']:
    expected_batch[(entry['run_id'], entry['station'], entry['row_id'])] = \
        entry['batch_id']
expected_specimen = {}
for entry in manifest['specimens']:
    expected_specimen[(entry['run_id'], entry['tick'],
                       entry['resistor_id'])] = (entry['specimen_id'],
                                                 entry['condition'])
for item in fit['rows'] + holdout['rows']:
    key = (item['run_id'], item['tick'], item['resistor_id'])
    specimen, condition = expected_specimen[key]
    assert item['specimen_id'] == specimen and item['condition'] == condition
    assert item['volts'] == '1' and item['version'] == 1
    assert item['sources'], item
    for source in item['sources']:
        source_key = (source['run_id'], source['station'], source['row_id'])
        assert source['batch_id'] == expected_batch[source_key]
    # 组标识为组内最小行键：混合组的 H 行指向同组更早的 L 行。
    expected_group = key
    if item['specimen_id'].startswith('mx') and item['condition'] == 'H':
        expected_group = (key[0], key[1] - 1, key[2])
    assert tuple(item['group_id']) == expected_group, (key, item['group_id'])
print(json.dumps({'holdout': holdout['counts'], 'fit': fit['counts'],
                  'groups': 2700, 'seconds': round(elapsed, 3)}))
'''


if __name__ == '__main__':
    unittest.main()
