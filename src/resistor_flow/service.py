from fractions import Fraction
from .model import parse
from .nodal import solve

def process(data):
    nodes,edges=parse(data);voltages=solve(nodes,edges);outgoing={key:Fraction(0) for key in nodes};resistors=[];heat=Fraction(0)
    for e in edges:
        drop=voltages[e['a']]-voltages[e['b']];current=drop/e['ohms'];power=current*drop
        outgoing[e['a']]+=current;outgoing[e['b']]-=current;heat+=power
        resistors.append({'id':e['id'],'current_a_to_b':str(current),'voltage_a_minus_b':str(drop),'watts':str(power),'over_rating':e['rating'] is not None and power>e['rating']})
    sources=[];node_results=[]
    for key in nodes:
        fixed=nodes[key] is not None
        if not fixed and outgoing[key]!=0:raise ValueError('KCL conservation failed')
        node_results.append({'id':key,'voltage':str(voltages[key]),'fixed':fixed,'resistor_current_out':str(outgoing[key])})
        if fixed:sources.append({'node':key,'supplied_amps':str(outgoing[key]),'supplied_watts':str(voltages[key]*outgoing[key])})
    supply=sum((voltages[k]*outgoing[k] for k in nodes if nodes[k] is not None),Fraction(0))
    if supply!=heat:raise ValueError('energy conservation failed')
    return {'nodes':node_results,'resistors':resistors,'sources':sources,'power_balance_residual':'0','summary':{'nodes':len(nodes),'resistors':len(edges),'dissipated_watts':str(heat),'supplied_watts':str(supply),'over_rating':sum(r['over_rating'] for r in resistors)}}
