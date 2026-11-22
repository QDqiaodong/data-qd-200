from fractions import Fraction

def solve(nodes,edges):
    unknown=[key for key,v in nodes.items() if v is None];index={key:i for i,key in enumerate(unknown)};n=len(unknown)
    matrix=[[Fraction(0) for _ in range(n+1)] for _ in range(n)]
    for e in edges:
        conductance=1/e['ohms']
        for a,b in ((e['a'],e['b']),(e['b'],e['a'])):
            if a not in index:continue
            i=index[a];matrix[i][i]+=conductance
            if b in index:matrix[i][index[b]]-=conductance
            else:matrix[i][-1]+=conductance*nodes[b]
    for col in range(n):
        pivot=next((i for i in range(col,n) if matrix[i][col]),None)
        if pivot is None:raise ValueError('singular voltage system')
        matrix[col],matrix[pivot]=matrix[pivot],matrix[col];scale=matrix[col][col]
        matrix[col]=[v/scale for v in matrix[col]]
        for i in range(n):
            if i==col:continue
            scale=matrix[i][col]
            matrix[i]=[x-scale*y for x,y in zip(matrix[i],matrix[col])]
    result=dict(nodes)
    for key,i in index.items():result[key]=matrix[i][-1]
    return result
