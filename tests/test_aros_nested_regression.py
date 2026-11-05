"""Regression tests: AROS sub-resources, source totals and manual review."""
from decimal import Decimal
from pto_agent.asbuilt.aros import parse_aros_markdown, aros_positions
from pto_agent.asbuilt.acts_plan import plan_acts, plan_to_specs
from pto_agent.asbuilt.validation import validate_dates
from pto_agent.procurement import build_request, compare_quotes


def row(*cells):
    return '<tr>' + ''.join('<td>'+str(c)+'</td>' for c in cells) + '</tr>'


def fixture():
    return '<table>' + row('Раздел 1: Электромонтажные работы') + \
        row('1','ГЭСНм08-02-409-09','Труба гофрированная для кабеля','100 м','5,6','','5,6','','','','','') + \
        row('1.1','24.3.01.02-0012','Труба гофрированная','м','102','','571,2','','','10,97','','6 266,06') + \
        row('2','ГЭСНм08-02-409-09','Труба гофрированная для кабеля','100 м','0,58','','0,58','','','','','') + \
        row('2.1','24.3.01.02-0012','Труба гофрированная','м','102','','59,16','','','10,97','','648,99') + \
        row('3','ГЭСНм08-03-610-04','Светильник светодиодный','100 шт','0,77','','0,77','','','','','') + \
        row('3.1','20.3.03.07-0094','Светильник потолочный','шт','100','','77','','','4 563','','351 351') + \
        row('','01.7.15.07-0152','Дюбель ненумерованный','шт','1','','5','','','','','') + '</table>'


def test_nested_total_column_not_rate_and_main_positions_unchanged():
    doc = parse_aros_markdown(fixture())
    assert len(doc['positions']) == 3
    assert len(doc['nested_resources']) == 3
    assert doc['nested_resources'][0]['qty'] == 571.2
    assert doc['nested_resources'][0]['parent_n'] == 1
    assert doc['nested_resources'][2]['qty'] == 77
    assert not any(x['code'] == '01.7.15.07-0152' for x in aros_positions(doc))


def test_parent_link_to_proper_act_and_date_is_never_fabricated():
    plan = plan_acts(aros_positions(parse_aros_markdown(fixture())))
    assert plan['positions_total'] == 3
    assert plan['act_date'] == ''
    trassy = next(a for a in plan['acts'] if a['group'] == 'trassy')
    light = next(a for a in plan['acts'] if a['group'] == 'osveschenie')
    tube = next(x for x in trassy['materials'] if x['code'] == '24.3.01.02-0012')
    assert tube['qty'] == 630.36
    assert [x['parent_position'] for x in tube['sources']] == [1,2]
    assert tube['resource_role_unverified']
    assert light['materials'][0]['qty'] == 77
    assert all('act_date' in s['needs_input'] for s in plan_to_specs(plan))
    assert any(i['code']=='act_date_missing' for i in validate_dates({}))


def test_precision_and_same_code_different_units_do_not_merge():
    items = [
        {'kind':'material','code':'C','name':'Cable','qty':0.3264,'unit':'1000 м'},
        {'kind':'material','code':'C','name':'Cable','qty':0.04896,'unit':'1000 м'},
        {'kind':'material','code':'C','name':'Cable','qty':7,'unit':'м'},
    ]
    req = build_request(items)
    assert len(req['items']) == 2
    assert Decimal(str(req['items'][0]['qty'])) == Decimal('0.37536')
    assert req['items'][1]['qty'] == 7


def test_missing_qty_never_silently_turns_to_zero():
    req=build_request([{'kind':'material','code':'C','name':'Cable','qty':1,'unit':'м'},
                       {'kind':'material','code':'C','name':'Cable','qty':None,'unit':'м'}])
    assert req['items'][0]['qty'] is None
    assert 'qty' in req['items'][0]['needs_input']


def test_nested_resource_blocks_procurement_comparison_until_review():
    pos=aros_positions(parse_aros_markdown(fixture()))
    req=build_request(pos)
    light=next(x for x in req['items'] if x['code']=='20.3.03.07-0094')
    assert light['qty']==77 and 'resource_role' in light['needs_input']
    offer={'supplier':'A','code':light['code'],'price':100,'unit':'шт','vat_rate':0,
           'delivery_days':1,'valid_until':'2099-01-01','quote_date':'2026-01-01','quote_no':'1'}
    comparison=compare_quotes(req,[offer])
    assert not any(x['code']==light['code'] for x in comparison['lines'])
    assert any(x['code']==light['code'] and 'подтвердить' in x['reason'] for x in comparison['no_quotes'])


def test_quote_unit_mismatch_cannot_be_used_as_winner():
    from datetime import date
    req = build_request([{'kind':'material','code':'C','name':'Cable','qty':1,'unit':'1000 м'}])
    offer={'supplier':'A','code':'C','price':100,'unit':'м','vat_rate':0,
           'delivery_days':1,'valid_until':'2099-01-01','quote_date':'2026-01-01','quote_no':'1'}
    comparison=compare_quotes(req,[offer],today=date(2026,11,5))
    assert not comparison['lines']
    assert 'единица КП' in comparison['not_comparable'][0]['reasons'][0]


def test_unverified_resource_blocks_draft_even_if_certificate_present():
    from pto_agent.asbuilt.validation import validate_materials
    issues = validate_materials({'materials':[{'name':'Сверло','certificate':'Паспорт',
                                                'resource_role_unverified':True}]})
    assert any(x['code']=='resource_role_unverified' and x['severity']=='blocking' for x in issues)


def test_aosr_does_not_invent_current_date(tmp_path):
    from pto_agent.asbuilt.aosr import build_aosr
    result = build_aosr({'act_number':'1'}, tmp_path)
    assert result['act_date'] == '—'
    assert not result['can_transfer']
    assert any(x['code'] == 'act_date_missing' for x in result['validation']['blocking'])
