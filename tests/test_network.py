import copy,json,unittest
from pathlib import Path
from resistor_flow.service import process

class NetworkTests(unittest.TestCase):
    def setUp(self):self.data=json.loads(Path('fixtures/input.json').read_text())
    def test_divider_power_and_kcl(self):
        r=process(self.data);self.assertEqual(r['nodes'][1]['voltage'],'8');self.assertEqual(r['nodes'][1]['resistor_current_out'],'0')
        self.assertEqual([e['watts'] for e in r['resistors']],['8','16']);self.assertEqual(r['summary']['supplied_watts'],'24')
        self.assertEqual(r['summary']['over_rating'],1);self.assertEqual(r['power_balance_residual'],'0')
    def test_parallel(self):
        self.data['resistors'].append({'id':'r3','a':'middle','b':'ground','ohms':'4'})
        r=process(self.data);self.assertEqual(r['nodes'][1]['voltage'],'6');self.assertEqual(r['summary']['dissipated_watts'],'36')
    def test_fixed_to_fixed_and_negative_current(self):
        d={'nodes':[{'id':'a','voltage':'0'},{'id':'b','voltage':'10'}],'resistors':[{'id':'r','a':'a','b':'b','ohms':'5'}]}
        r=process(d);self.assertEqual(r['resistors'][0]['current_a_to_b'],'-2');self.assertEqual(r['summary']['dissipated_watts'],'20')
    def test_floating_component_rejected(self):
        self.data['nodes'].append({'id':'isolated','voltage':None})
        with self.assertRaisesRegex(ValueError,'floating'):process(self.data)
    def test_invalid_edges(self):
        for values in ({'ohms':'0'},{'a':'absent'},{'b':'supply'},{'rated_watts':'-1'}):
            d=copy.deepcopy(self.data);d['resistors'][0].update(values)
            with self.assertRaises(ValueError):process(d)
    def test_disconnected_anchored_and_exact_rating(self):
        self.data['nodes'].append({'id':'unused_anchor','voltage':'3'})
        self.data['resistors'][1]['rated_watts']='16'
        r=process(self.data);self.assertEqual(r['summary']['over_rating'],0);self.assertEqual(r['sources'][-1]['supplied_watts'],'0')
