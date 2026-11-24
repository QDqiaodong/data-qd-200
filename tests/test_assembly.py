"""模块制模与装配批处理验收测试。

覆盖任务书“自动验收”三条：

1. 同一装配位置的两个修订：串联 r1(10 W)、r2(20 W)，初版阻值 2、4 Ω，另一
   修订交换阻值；两版端口电流同为 +2、-2。初版 p=12 交付，换修订后仅因
   r1=16 W 拒绝；同批 p=6 不被换修订全部拦住。复核两版端口等效、实际交付
   数据与全部超额件归属/数值，并在移走源模型后的新进程只用包与装配输入
   完成批处理；检查包不含原接线/内部节点身份，缺少必需功率资料时整批报错。
2. 三端口星形与负载模块给出 n=1 V、电流 1、-1/2、-1/2 与负载 1 A；x 改为
   6 V 时同批拒绝并定位后两件（各 2 W）。再造含两个互不连通单端口分量的
   模块，验证只驱动一网报浮空、两网并为同一驱动网则不浮空。另覆盖同包多
   实例局部电阻 ID 不串账、非法映射/激励表、零电流与录入顺序稳定交付。
3. 48 个两端口实例串成 N0..N48、每模块 12 个 1 Ω 串联、4,096 条激励的冷
   启动批次在独立进程、256 MiB 地址空间下运行
   ``acceptance/assembly_acceptance.py``，CPU <= 35 秒并核对两份完整结果。
"""

import json
import os
import random
import subprocess
import sys
import tempfile
import textwrap
import unittest
from fractions import Fraction
from pathlib import Path

from resistor_flow.assembly import (
    AssemblyInputError,
    assemble_batch,
)
from resistor_flow.modules import (
    ModulePackageError,
    build_module_package,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def series_module(revision, first_ohms, second_ohms, model='M'):
    return {
        'model': model,
        'revision': revision,
        'nodes': ['p', 'c', 'q'],
        'ports': ['p', 'q'],
        'resistors': [
            {'id': 'r1', 'a': 'p', 'b': 'c', 'ohms': first_ohms,
             'rated_watts': '10'},
            {'id': 'r2', 'a': 'c', 'b': 'q', 'ohms': second_ohms,
             'rated_watts': '20'},
        ],
    }


def two_port(ohms, model='L', revision='1', rated=None):
    resistor = {'id': 'r', 'a': 'a', 'b': 'b', 'ohms': ohms}
    if rated is not None:
        resistor['rated_watts'] = rated
    return {'model': model, 'revision': revision, 'nodes': ['a', 'b'],
            'ports': ['a', 'b'], 'resistors': [resistor]}


def single_instance(model, revision, bindings, instance='I'):
    return {'instance': instance, 'model': model, 'revision': revision,
            'bindings': bindings}


class ScenarioOneTests(unittest.TestCase):
    """规格 1：两修订端口等效但内部超额不同。"""

    def setUp(self):
        self.package_a = build_module_package(
            series_module('A', '2', '4'))
        self.package_b = build_module_package(
            series_module('B', '4', '2'))
        self.packages = [self.package_a, self.package_b]
        self.excitations = [
            {'sample_id': 's12', 'voltages': {'P': '12', 'Q': '0'}},
            {'sample_id': 's6', 'voltages': {'P': '6', 'Q': '0'}},
        ]

    def run_revision(self, revision):
        instances = [single_instance('M', revision, {'p': 'P', 'q': 'Q'})]
        return assemble_batch(self.packages, instances, ['P', 'Q'],
                              self.excitations)

    def test_revisions_share_port_response_but_differ_in_power(self):
        # 端口等效：Y 相同，端口电流同为 +2/-2。
        self.assertEqual(self.package_a['admittance'],
                         self.package_b['admittance'])
        self.assertEqual(self.package_a['admittance'],
                         [['1/6', '-1/6'], ['-1/6', '1/6']])
        accepted_a = self.run_revision('A')['accepted']
        self.assertEqual([s['sample_id'] for s in accepted_a['samples']],
                         ['s12', 's6'])
        self.assertEqual(accepted_a['samples'][0]['currents']['I'],
                         {'p': '2', 'q': '-2'})

        accepted_b = self.run_revision('B')['accepted']
        self.assertEqual(accepted_b['samples'][0]['sample_id'], 's6')
        self.assertEqual(accepted_b['samples'][0]['currents']['I'],
                         {'p': '1', 'q': '-1'})

        rejected_b = self.run_revision('B')['rejected']
        self.assertEqual(rejected_b['sample_count'], 1)
        row = rejected_b['samples'][0]
        self.assertEqual(row['sample_id'], 's12')
        self.assertEqual(row['over_rating'], [{
            'instance': 'I', 'model': 'M', 'revision': 'B',
            'resistor_id': 'r1', 'watts': '16', 'rated_watts': '10',
        }])
        # 不把后一修订所有样本都拦住：p=6 仍在通过集。
        self.assertEqual([s['sample_id'] for s in accepted_b['samples']],
                         ['s6'])

    def test_original_revision_delivers_both(self):
        result = self.run_revision('A')
        accepted = result['accepted']
        self.assertEqual([s['sample_id'] for s in accepted['samples']],
                         ['s12', 's6'])
        self.assertEqual(result['rejected']['samples'], [])
        for sample in accepted['samples']:
            self.assertEqual(sample['currents']['I'],
                             {'p': '2', 'q': '-2'} if sample['sample_id'] ==
                             's12' else {'p': '1', 'q': '-1'})

    def test_assembly_manifest_in_both_results(self):
        result = self.run_revision('B')
        for section in ('accepted', 'rejected'):
            manifest = result[section]['assembly']
            self.assertEqual(manifest['instances'], [{
                'instance': 'I', 'model': 'M', 'revision': 'B',
                'bindings': [{'port': 'p', 'net': 'P'},
                             {'port': 'q', 'net': 'Q'}],
            }])
            self.assertEqual(manifest['nets'], ['P', 'Q'])

    def test_package_has_no_internal_identity_or_wiring(self):
        secret = build_module_package({
            'model': 'M', 'revision': 'SECRET',
            'nodes': ['p', 'INTERNAL_SECRET_NODE', 'q'],
            'ports': ['p', 'q'],
            'resistors': [
                {'id': 'r1', 'a': 'p', 'b': 'INTERNAL_SECRET_NODE',
                 'ohms': '2', 'rated_watts': '10'},
                {'id': 'r2', 'a': 'INTERNAL_SECRET_NODE', 'b': 'q',
                 'ohms': '4', 'rated_watts': '20'},
            ],
        })
        text = json.dumps(secret)
        self.assertNotIn('INTERNAL_SECRET_NODE', text)
        self.assertNotIn('source', text)
        self.assertNotIn('path', text)
        self.assertNotIn('callback', text)
        # 只出现端口身份与本包派生的功率资料 ID。
        self.assertEqual(secret['ports'], ['p', 'q'])
        self.assertEqual(secret['components'], [['p', 'q']])
        self.assertEqual([item['id'] for item in secret['power_checks']],
                         ['r1', 'r2'])
        self.assertEqual(
            set(secret),
            {'format', 'model', 'revision', 'ports', 'components',
             'admittance', 'power_checks'})

    def test_new_process_uses_only_package_and_inputs(self):
        with tempfile.TemporaryDirectory() as root:
            packages_path = os.path.join(root, 'packages.json')
            batch_path = os.path.join(root, 'batch.json')
            source_path = os.path.join(root, 'source-model.json')
            # 先在制模工位写下源模型，制出包后立即移走源模型文件。
            with open(source_path, 'w', encoding='utf-8') as handle:
                json.dump(series_module('B', '4', '2'), handle)
            built = build_module_package(
                json.loads(Path(source_path).read_text(encoding='utf-8')))
            os.unlink(source_path)
            self.assertFalse(os.path.exists(source_path))
            self.assertEqual(built['admittance'], self.package_b['admittance'])
            with open(packages_path, 'w', encoding='utf-8') as handle:
                json.dump([built], handle)
            with open(batch_path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'instances': [single_instance('M', 'B',
                                                  {'p': 'P', 'q': 'Q'})],
                    'driven_nets': ['P', 'Q'],
                    'excitations': self.excitations,
                }, handle)
            script = textwrap.dedent('''
                import json, sys
                sys.path.insert(0, sys.argv[3])
                from resistor_flow.assembly import assemble_batch
                with open(sys.argv[1], encoding='utf-8') as handle:
                    packages = json.load(handle)
                with open(sys.argv[2], encoding='utf-8') as handle:
                    batch = json.load(handle)
                result = assemble_batch(packages, batch['instances'],
                                        batch['driven_nets'],
                                        batch['excitations'])
                print(json.dumps(result, sort_keys=True))
            ''')
            completed = subprocess.run(
                [sys.executable, '-I', '-c', script, packages_path,
                 batch_path, str(REPO_ROOT / 'src')],
                capture_output=True, text=True, cwd=root)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result['rejected']['samples'][0]['over_rating'],
                             [{'instance': 'I', 'model': 'M', 'revision': 'B',
                               'resistor_id': 'r1', 'watts': '16',
                               'rated_watts': '10'}])

    def test_error_types_are_value_errors(self):
        self.assertTrue(issubclass(AssemblyInputError, ValueError))
        self.assertTrue(issubclass(ModulePackageError, ValueError))

    def test_missing_power_check_data_rejects_whole_batch(self):
        broken = json.loads(json.dumps(self.package_b))
        del broken['power_checks']
        with self.assertRaises(AssemblyInputError):
            assemble_batch([self.package_a, broken],
                           [single_instance('M', 'B', {'p': 'P', 'q': 'Q'})],
                           ['P', 'Q'], self.excitations)

    def test_empty_power_checks_means_no_rated_parts(self):
        package = build_module_package(two_port('2', model='N', revision='1'))
        self.assertEqual(package['power_checks'], [])
        result = assemble_batch(
            [package],
            [single_instance('N', '1', {'a': 'A', 'b': 'B'})],
            ['A', 'B'],
            [{'sample_id': 's', 'voltages': {'A': '100', 'B': '0'}}])
        # 无额定件不参与筛选，再大的功率也交付。
        self.assertEqual(result['accepted']['sample_count'], 1)
        self.assertEqual(result['rejected']['samples'], [])

    def test_missing_power_checks_is_not_empty_no_rated(self):
        package = build_module_package(two_port('2', model='N', revision='1'))
        broken = json.loads(json.dumps(package))
        del broken['power_checks']
        with self.assertRaises(AssemblyInputError):
            assemble_batch([broken],
                           [single_instance('N', '1', {'a': 'A', 'b': 'B'})],
                           ['A', 'B'], [])

    def test_package_serializes_roundtrip(self):
        restored = json.loads(json.dumps(self.package_a))
        result = assemble_batch([restored],
                                [single_instance('M', 'A',
                                                 {'p': 'P', 'q': 'Q'})],
                                ['P', 'Q'], self.excitations)
        self.assertEqual(result['accepted']['sample_count'], 2)


class ScenarioTwoTests(unittest.TestCase):
    """规格 2：三端口星形 + 负载模块，以及单端口分量浮空。"""

    def setUp(self):
        self.star = build_module_package({
            'model': 'S', 'revision': '1',
            'nodes': ['x', 'y', 'z', 'c'],
            'ports': ['x', 'y', 'z'],
            'resistors': [
                {'id': 'rx', 'a': 'x', 'b': 'c', 'ohms': '1'},
                {'id': 'ry', 'a': 'y', 'b': 'c', 'ohms': '2',
                 'rated_watts': '0.5'},
                {'id': 'rz', 'a': 'z', 'b': 'c', 'ohms': '2',
                 'rated_watts': '0.5'},
            ],
        })
        self.load = build_module_package(two_port('1'))
        self.instances = [
            single_instance('S', '1', {'x': 'X', 'y': 'N', 'z': 'N'}, 'S1'),
            single_instance('L', '1', {'a': 'N', 'b': 'G'}, 'L1'),
        ]

    def run_case(self, x_volts):
        return assemble_batch(
            [self.star, self.load], self.instances, ['X', 'G'],
            [{'sample_id': 'v', 'voltages': {'X': x_volts, 'G': '0'}}])

    def test_three_port_response_and_load(self):
        result = self.run_case('3')['accepted']
        sample = result['samples'][0]
        self.assertEqual(sample['voltages'], {'G': '0', 'N': '1', 'X': '3'})
        self.assertEqual(sample['currents'], {
            'L1': {'a': '1', 'b': '-1'},
            'S1': {'x': '1', 'y': '-1/2', 'z': '-1/2'},
        })
        self.assertEqual(self.run_case('3')['rejected']['samples'], [])

    def test_six_volts_rejects_both_rated_parts(self):
        rejected = self.run_case('6')['rejected']
        self.assertEqual(rejected['sample_count'], 1)
        self.assertEqual(rejected['samples'][0]['over_rating'], [
            {'instance': 'S1', 'model': 'S', 'revision': '1',
             'resistor_id': 'ry', 'watts': '2', 'rated_watts': '1/2'},
            {'instance': 'S1', 'model': 'S', 'revision': '1',
             'resistor_id': 'rz', 'watts': '2', 'rated_watts': '1/2'},
        ])
        self.assertEqual(self.run_case('6')['accepted']['samples'], [])

    def test_equality_is_not_rejected(self):
        # x=3 时 ry/rz 各 1/4 W，低于 0.5；x 使功率恰等于额定值时可用。
        star = build_module_package({
            'model': 'S', 'revision': '2',
            'nodes': ['x', 'y', 'c'], 'ports': ['x', 'y'],
            'resistors': [
                {'id': 'r1', 'a': 'x', 'b': 'c', 'ohms': '1',
                 'rated_watts': '1'},
                {'id': 'r2', 'a': 'c', 'b': 'y', 'ohms': '1',
                 'rated_watts': '1'},
            ],
        })
        instances = [single_instance('S', '2', {'x': 'X', 'y': 'Y'}, 'S')]
        result = assemble_batch(
            [star], instances, ['X', 'Y'],
            [{'sample_id': 'eq', 'voltages': {'X': '2', 'Y': '0'}}])
        # 2 V 跨两个 1 Ω：各 1 W，恰好等于额定值 -> 交付。
        self.assertEqual(result['accepted']['sample_count'], 1)
        self.assertEqual(result['rejected']['samples'], [])

    def test_disconnected_single_port_components(self):
        module = build_module_package({
            'model': 'D', 'revision': '1', 'nodes': ['a', 'b'],
            'ports': ['a', 'b'], 'resistors': [],
        })
        instances = [single_instance('D', '1', {'a': 'A', 'b': 'B'}, 'D1')]
        with self.assertRaisesRegex(AssemblyInputError, 'floating'):
            assemble_batch([module], instances, ['A'],
                           [{'sample_id': 'one', 'voltages': {'A': '1'}}])
        # 两网外部并为同一驱动网则不浮空。
        instances = [single_instance('D', '1', {'a': 'N', 'b': 'N'}, 'D1')]
        result = assemble_batch(
            [module], instances, ['N'],
            [{'sample_id': 'one', 'voltages': {'N': '1'}}])
        sample = result['accepted']['samples'][0]
        self.assertEqual(sample['currents']['D1'], {'a': '0', 'b': '0'})

    def test_same_package_instances_do_not_cross_resistor_ids(self):
        # 两个实例共用同一包：局部电阻 ID 相同，但必须按实例分开记账。
        star = build_module_package({
            'model': 'S', 'revision': '3',
            'nodes': ['x', 'y', 'c'], 'ports': ['x', 'y'],
            'resistors': [
                {'id': 'r1', 'a': 'x', 'b': 'c', 'ohms': '1',
                 'rated_watts': '10'},
                {'id': 'r2', 'a': 'c', 'b': 'y', 'ohms': '1',
                 'rated_watts': '10'},
            ],
        })
        instances = [
            single_instance('S', '3', {'x': 'X', 'y': 'M'}, 'S1'),
            single_instance('S', '3', {'x': 'M', 'y': 'G'}, 'S2'),
        ]
        result = assemble_batch(
            [star], instances, ['X', 'G'],
            [{'sample_id': 'chain', 'voltages': {'X': '6', 'G': '0'}}])
        sample = result['accepted']['samples'][0]
        self.assertEqual(sample['voltages'], {'G': '0', 'M': '3', 'X': '6'})
        self.assertNotIn('powers', sample)  # 通过样本不附功率明细
        self.assertEqual(sample['currents']['S1'],
                         {'x': '3/2', 'y': '-3/2'})
        self.assertEqual(sample['currents']['S2'],
                         {'x': '3/2', 'y': '-3/2'})

    def test_same_package_instances_power_attributed_per_instance(self):
        star = build_module_package({
            'model': 'S', 'revision': '4',
            'nodes': ['x', 'y', 'c'], 'ports': ['x', 'y'],
            'resistors': [
                {'id': 'r1', 'a': 'x', 'b': 'c', 'ohms': '1',
                 'rated_watts': '10'},
                {'id': 'r2', 'a': 'c', 'b': 'y', 'ohms': '1',
                 'rated_watts': '10'},
            ],
        })
        instances = [
            single_instance('S', '4', {'x': 'X', 'y': 'M'}, 'S1'),
            single_instance('S', '4', {'x': 'M', 'y': 'G'}, 'S2'),
        ]
        result = assemble_batch(
            [star], instances, ['X', 'G'],
            [{'sample_id': 'chain', 'voltages': {'X': '20', 'G': '0'}}])
        # 两件各 10 Ω? 不对：两件 1 Ω 串在两实例间，总 4 Ω，电流 5 A，
        # r1/r2 各 25 W > 10 W；两实例都应被列出，按实例 ID 排序。
        rows = result['rejected']['samples'][0]['over_rating']
        self.assertEqual([(r['instance'], r['resistor_id'], r['watts'])
                          for r in rows],
                         [('S1', 'r1', '25'), ('S1', 'r2', '25'),
                          ('S2', 'r1', '25'), ('S2', 'r2', '25')])

    def test_zero_current_sample_delivers(self):
        result = assemble_batch(
            [self.star, self.load], self.instances, ['X', 'G'],
            [{'sample_id': 'zero', 'voltages': {'X': '0', 'G': '0'}}])
        sample = result['accepted']['samples'][0]
        self.assertEqual(sample['currents'], {
            'L1': {'a': '0', 'b': '0'},
            'S1': {'x': '0', 'y': '0', 'z': '0'},
        })

    def test_reordering_inputs_is_stable(self):
        excitations = [
            {'sample_id': 'b', 'voltages': {'X': '3', 'G': '0'}},
            {'sample_id': 'a', 'voltages': {'X': '6', 'G': '0'}},
            {'sample_id': 'c', 'voltages': {'X': '0', 'G': '0'}},
        ]
        baseline = assemble_batch([self.star, self.load], self.instances,
                                  ['X', 'G'], excitations)
        shuffled = assemble_batch(
            [self.load, self.star], list(reversed(self.instances)),
            ['G', 'X'], list(reversed(excitations)))
        canonical = lambda value: json.dumps(value, sort_keys=True)
        self.assertEqual(canonical(baseline), canonical(shuffled))
        self.assertEqual([s['sample_id'] for s in
                          baseline['accepted']['samples']], ['b', 'c'])
        self.assertEqual([s['sample_id'] for s in
                          baseline['rejected']['samples']], ['a'])

    def test_invalid_mapping_and_excitation_tables(self):
        with self.assertRaises(AssemblyInputError):
            assemble_batch([self.star, self.load],
                           [single_instance('S', '1',
                                            {'x': 'X', 'y': 'N'}, 'S1')],
                           ['X', 'G'], [])
        with self.assertRaises(AssemblyInputError):
            assemble_batch([self.star, self.load],
                           [single_instance('S', '1',
                                            {'x': 'X', 'y': 'N', 'z': 'N',
                                             'w': 'W'}, 'S1')],
                           ['X', 'G'], [])
        missing = [{'sample_id': 'v', 'voltages': {'X': '1'}}]
        with self.assertRaises(AssemblyInputError):
            assemble_batch([self.star, self.load], self.instances,
                           ['X', 'G'], missing)
        extra = [{'sample_id': 'v',
                  'voltages': {'X': '1', 'G': '0', 'Z': '0'}}]
        with self.assertRaises(AssemblyInputError):
            assemble_batch([self.star, self.load], self.instances,
                           ['X', 'G'], extra)
        duplicate = [{'sample_id': 'v', 'voltages': {'X': '1', 'G': '0'}},
                     {'sample_id': 'v', 'voltages': {'X': '2', 'G': '0'}}]
        with self.assertRaises(AssemblyInputError):
            assemble_batch([self.star, self.load], self.instances,
                           ['X', 'G'], duplicate)
        bad_value = [{'sample_id': 'v',
                      'voltages': {'X': '1e3', 'G': '0'}}]
        with self.assertRaises(AssemblyInputError):
            assemble_batch([self.star, self.load], self.instances,
                           ['X', 'G'], bad_value)

    def test_invalid_instances_and_packages(self):
        with self.assertRaises(AssemblyInputError):
            assemble_batch([self.star, self.load],
                           [single_instance('S', '9',
                                            {'x': 'X', 'y': 'N', 'z': 'N'},
                                            'S1')], ['X', 'G'], [])
        with self.assertRaises(AssemblyInputError):
            assemble_batch([self.star, self.star], self.instances,
                           ['X', 'G'], [])
        broken = json.loads(json.dumps(self.star))
        broken['admittance'] = broken['admittance'][:2]
        with self.assertRaises(AssemblyInputError):
            assemble_batch([broken, self.load], self.instances,
                           ['X', 'G'], [])
        missing_checks = json.loads(json.dumps(self.star))
        del missing_checks['power_checks']
        with self.assertRaises(AssemblyInputError):
            assemble_batch([missing_checks, self.load], self.instances,
                           ['X', 'G'], [])


class ModuleBuilderTests(unittest.TestCase):
    def test_permutation_invariance_and_port_order(self):
        base = {
            'model': 'M', 'revision': 'R',
            'nodes': ['p', 'a', 'b', 'q'],
            'ports': ['p', 'q'],
            'resistors': [
                {'id': 'r1', 'a': 'p', 'b': 'a', 'ohms': '2',
                 'rated_watts': '10'},
                {'id': 'r2', 'a': 'a', 'b': 'b', 'ohms': '3'},
                {'id': 'r3', 'a': 'b', 'b': 'q', 'ohms': '4',
                 'rated_watts': '20'},
            ],
        }
        canonical = json.dumps(build_module_package(base), sort_keys=True)
        reordered = {
            'model': 'M', 'revision': 'R',
            'nodes': ['q', 'b', 'a', 'p'],
            'ports': ['p', 'q'],
            'resistors': list(reversed(base['resistors'])),
        }
        self.assertEqual(
            json.dumps(build_module_package(reordered), sort_keys=True),
            canonical)
        swapped = dict(base, ports=['q', 'p'])
        self.assertEqual(build_module_package(swapped)['ports'], ['q', 'p'])
        self.assertEqual(build_module_package(base)['components'],
                         [['p', 'q']])

    def test_multiple_port_components_and_isolated_node(self):
        package = build_module_package({
            'model': 'M', 'revision': 'R',
            'nodes': ['a', 'b', 'iso'], 'ports': ['a', 'b', 'iso'],
            'resistors': [{'id': 'r', 'a': 'a', 'b': 'b', 'ohms': '1'}],
        })
        self.assertEqual(package['components'], [['a', 'b'], ['iso']])
        self.assertEqual([row[2] for row in package['admittance']],
                         ['0', '0', '0'])

    def test_component_without_port_rejected(self):
        with self.assertRaises(ModulePackageError):
            build_module_package({
                'model': 'M', 'revision': 'R',
                'nodes': ['a', 'b', 'c'], 'ports': ['a'],
                'resistors': [{'id': 'r', 'a': 'b', 'b': 'c', 'ohms': '1'}],
            })

    def test_invalid_module_inputs(self):
        good = series_module('R', '2', '4')
        for mutate in (
            lambda d: d.update(ports=['p', 'p']),
            lambda d: d.update(ports=['p', 'absent']),
            lambda d: d.pop('model'),
            lambda d: d.update(nodes=['p', 'c']),
            lambda d: d['resistors'][0].update(ohms='0'),
            lambda d: d['resistors'][0].update(a='c'),
            lambda d: d['resistors'][0].update(rated_watts='0'),
            lambda d: d['resistors'].append(dict(d['resistors'][0])),
            lambda d: d.update(model='bad model'),
        ):
            data = json.loads(json.dumps(good))
            mutate(data)
            with self.assertRaises(ModulePackageError):
                build_module_package(data)

    def test_parallel_branches_counted_per_part(self):
        package = build_module_package({
            'model': 'M', 'revision': 'R', 'nodes': ['p', 'q'],
            'ports': ['p', 'q'],
            'resistors': [
                {'id': 'a', 'a': 'p', 'b': 'q', 'ohms': '2',
                 'rated_watts': '5'},
                {'id': 'b', 'a': 'p', 'b': 'q', 'ohms': '2',
                 'rated_watts': '5'},
            ],
        })
        result = assemble_batch(
            [package],
            [single_instance('M', 'R', {'p': 'P', 'q': 'Q'})],
            ['P', 'Q'],
            [{'sample_id': 's', 'voltages': {'P': '4', 'Q': '0'}}])
        # 4 V 跨两只 2 Ω 并联 = 每只 2 A -> 各 8 W > 5 W，逐件列出。
        rows = result['rejected']['samples'][0]['over_rating']
        self.assertEqual([(r['resistor_id'], r['watts']) for r in rows],
                         [('a', '8'), ('b', '8')])


class RandomisedCrossCheckTests(unittest.TestCase):
    """随机连通图：端口电流与额定件压降对齐既有节点求解器。"""

    def test_random_graphs_match_direct_solver(self):
        from resistor_flow.nodal import solve
        rng = random.Random(7)
        for trial in range(40):
            size = rng.randint(2, 8)
            nodes = ['n%d' % index for index in range(size)]
            ports = rng.sample(nodes, rng.randint(1, min(4, size)))
            edges = []
            order = nodes[:]
            rng.shuffle(order)
            connected = {order[0]}
            identifier = 0
            for node in order[1:]:
                parent = rng.choice(sorted(connected))
                edges.append({'id': 'r%d' % identifier, 'a': parent,
                              'b': node, 'ohms': str(rng.randint(1, 5))})
                identifier += 1
                connected.add(node)
            for _ in range(rng.randint(0, 6)):
                first, second = rng.sample(nodes, 2)
                edges.append({
                    'id': 'r%d' % identifier, 'a': first, 'b': second,
                    'ohms': str(rng.randint(1, 5)),
                    'rated_watts': str(rng.randint(1, 9)),
                })
                identifier += 1
            module = {
                'model': 'M', 'revision': 'R%d' % trial, 'nodes': nodes,
                'ports': ports,
                'resistors': [dict(edge) for edge in edges],
            }
            package = build_module_package(module)
            potentials = {port: Fraction(rng.randint(-6, 6),
                                         rng.choice([1, 2, 4]))
                          for port in ports}
            bindings = {port: 'W%d' % index
                        for index, port in enumerate(ports)}
            instances = [single_instance('M', 'R%d' % trial, bindings, 'I')]
            result = assemble_batch(
                [package], instances, sorted(bindings.values()),
                [{'sample_id': 's', 'voltages': {
                    bindings[port]: _decimal(potentials[port])
                    for port in ports}}])
            solved = solve(
                {node: potentials.get(node) for node in nodes},
                [{'id': edge['id'], 'a': edge['a'], 'b': edge['b'],
                  'ohms': Fraction(edge['ohms'])} for edge in edges])

            def drop(edge):
                return solved[edge['a']] - solved[edge['b']]

            over = []
            for item in package['power_checks']:
                edge = next(edge for edge in edges if edge['id'] == item['id'])
                expected = drop(edge)
                exported = sum(Fraction(coefficient) * potentials[ports[index]]
                               for index, coefficient in item['drop'])
                self.assertEqual(exported, expected)
                if expected * expected / Fraction(edge['ohms']) > \
                        Fraction(item['rated_watts']):
                    over.append(item['id'])
            if over:
                self.assertEqual(result['accepted']['sample_count'], 0)
                rows = result['rejected']['samples'][0]['over_rating']
                self.assertEqual([row['resistor_id'] for row in rows], over)
                continue
            sample = result['accepted']['samples'][0]
            for port in ports:
                # 外部注入 = 端口节点经电阻流出的电流代数和（正号流入实例）。
                injection = Fraction(0)
                for edge in edges:
                    current = drop(edge) / Fraction(edge['ohms'])
                    if edge['a'] == port:
                        injection += current
                    if edge['b'] == port:
                        injection -= current
                self.assertEqual(Fraction(sample['currents']['I'][port]),
                                 injection)


def _decimal(value):
    sign = '-' if value < 0 else ''
    magnitude = abs(value)
    scaled = magnitude.numerator * (10 ** 6 // magnitude.denominator)
    if scaled % 10 ** 6 == 0:
        return sign + str(scaled // 10 ** 6)
    return sign + ('%d.%06d' % (scaled // 10 ** 6,
                                scaled % 10 ** 6)).rstrip('0')


class ScaleAcceptanceTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith('linux'),
                         'Linux address-space/CPU acceptance only')
    def test_cold_start_batch_under_limits(self):
        script = REPO_ROOT / 'acceptance' / 'assembly_acceptance.py'
        self.assertTrue(script.is_file(), script)
        with tempfile.TemporaryDirectory() as root:
            completed = subprocess.run(
                [sys.executable, '-I', str(script), 'all', root],
                capture_output=True, text=True, timeout=300)
            if completed.returncode != 0:
                self.fail('assembly acceptance failed:\nSTDOUT:%s\nSTDERR:%s'
                          % (completed.stdout, completed.stderr))
            summary = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertEqual(summary['mismatches'], [])
            self.assertEqual(summary['accepted'], 2_880)
            self.assertEqual(summary['rejected'], 1_216)
            self.assertLessEqual(summary['cpu_seconds'], 35.0)
            self.assertLessEqual(summary['maxrss_kib'], 262_144)
            self.assertTrue(summary['passed'], summary)


if __name__ == '__main__':
    unittest.main()
