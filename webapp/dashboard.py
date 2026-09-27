"""Paged dashboard projections read under one authorization snapshot."""
import json

from webapp.access import audit_scope, current_actor


def collect(store, actor, company=None, page=1, page_size=24):
    scope, args = audit_scope(actor)
    base = ' FROM audits a LEFT JOIN clients c ON c.id=a.client_id WHERE ' + scope
    identity = "COALESCE(NULLIF(a.taxpayer_id,''),a.company_name)"
    with store.connect() as db:
        db.execute('BEGIN')
        current_actor(db, actor, {'org_admin', 'accountant', 'teacher'})
        companies = [dict(row) for row in db.execute(
            'SELECT ' + identity + ' AS key,MAX(a.company_name) AS name' + base + ' GROUP BY 1 ORDER BY 2', args)]
        clients = []
        if actor['role'] != 'teacher':
            where = 'c.org_id=?' + (' AND c.accountant_id=?' if actor['role'] == 'accountant' else '')
            parameters = [actor['org_id']] + ([actor['id']] if actor['role'] == 'accountant' else [])
            clients = [dict(row) for row in db.execute(
                'SELECT c.*,u.display_name AS accountant_name,u.username AS accountant_username '
                'FROM clients c LEFT JOIN users u ON u.id=c.accountant_id AND u.org_id=c.org_id WHERE ' + where,
                parameters)]
        names = {item['key']:item['name'] for item in companies}
        names.update({item['taxpayer_id']:item['name'] for item in clients})
        selected = company if company in names else next(iter(names), '') if company is None else ''
        selected_base = base + ' AND ' + identity + '=?'
        selected_args = [*args, selected]
        total = db.execute('SELECT COUNT(DISTINCT a.period)' + selected_base, selected_args).fetchone()[0]
        rows = db.execute('''SELECT * FROM (SELECT a.*, ROW_NUMBER() OVER
            (PARTITION BY a.period ORDER BY a.audited_at DESC,a.rowid DESC) AS latest,
            a.rowid AS saved_order''' + selected_base + ''') WHERE latest=1
            ORDER BY audited_at DESC,saved_order DESC LIMIT ? OFFSET ?''',
            [*selected_args, page_size, (page-1)*page_size]).fetchall()
        history = [dict(row) for row in db.execute(
            'SELECT a.id,a.company_name,a.taxpayer_id,a.industry,a.period,a.audited_at,a.summary_json,a.client_id'
            + selected_base + ' ORDER BY a.audited_at DESC,a.rowid DESC LIMIT 101', selected_args)]
        records = []
        for row in rows:
            dataset, findings = json.loads(row['dataset_json']), json.loads(row['findings_json'])
            records.append({**{key:row[key] for key in ('id','company_name','taxpayer_id','industry','period','audited_at','client_id')},
                'summary':json.loads(row['summary_json']),
                'metrics':{key:{'value':value['value'],'source':value['source']} for key,value in dataset['metrics'].items()},
                'risks':[{'id':f['rule']['id'],'name':f['rule']['name'],'severity':f['rule']['severity'],
                          'category':f['rule']['category'],'status':f['status'],'reason':f['skip_reason']}
                         for f in findings if f['status']!='pass']})
        for row in history:
            row['summary'] = json.loads(row.pop('summary_json'))
    return {'records':records,'history':history[:100],'history_truncated':len(history)>100,'clients':clients,
            'companies':[{'key':key,'name':name} for key,name in names.items()], 'company':selected,
            'page':page,'page_size':page_size,'total_periods':total,'has_more':page*page_size<total}
