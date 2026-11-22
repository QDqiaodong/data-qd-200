from .checks import fields,array,ident,number

def parse(data):
    fields(data,('nodes','resistors'));nodes={};edges=[];seen=set()
    for item in array(data['nodes'],1,60):
        fields(item,('id','voltage'));key=ident(item['id'])
        if key in nodes:raise ValueError('duplicate node')
        nodes[key]=None if item['voltage'] is None else number(item['voltage'])
    for item in array(data['resistors'],0,300):
        fields(item,('id','a','b','ohms'),('rated_watts',));key=ident(item['id']);a=ident(item['a']);b=ident(item['b']);r=number(item['ohms'])
        if key in seen or a not in nodes or b not in nodes or a==b or r<=0:raise ValueError('invalid resistor')
        seen.add(key);rating=number(item['rated_watts']) if 'rated_watts' in item else None
        if rating is not None and rating<=0:raise ValueError('positive rating required')
        edges.append({'id':key,'a':a,'b':b,'ohms':r,'rating':rating})
    adjacency={key:set() for key in nodes}
    for e in edges:adjacency[e['a']].add(e['b']);adjacency[e['b']].add(e['a'])
    seen=set()
    for key in nodes:
        if key in seen:continue
        pending=[key];component=set()
        while pending:
            v=pending.pop()
            if v in component:continue
            component.add(v);pending.extend(adjacency[v]-component)
        seen.update(component)
        if all(nodes[v] is None for v in component):raise ValueError('floating component without fixed voltage')
    return nodes,edges
